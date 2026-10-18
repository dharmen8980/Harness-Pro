"""Anthropic Messages and OpenAI-compatible provider adapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import httpx

from .errors import ProviderError
from .trace import TraceEmitter


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    cost_usd: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def compute_cost(usage: Usage, pricing: dict | None) -> float | None:
    if usage.cost_usd is not None:
        return usage.cost_usd
    names = ("input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million")
    rates = tuple(pricing.get(name) for name in names) if pricing is not None else (None,) * len(names)
    if all(rate is None for rate in rates):
        return None
    tokens = (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens, usage.cache_write_input_tokens)
    if any(tokens) and not any(token and rate is not None for token, rate in zip(tokens, rates)):
        return None
    return sum(token * (rate or 0) for token, rate in zip(tokens, rates)) / 1_000_000


def _reported_cost(raw_usage: Mapping[str, Any]) -> float | None:
    value = raw_usage.get("cost_usd", raw_usage.get("cost"))
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AdapterResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "stop_reason": self.stop_reason,
            "usage": self.usage.as_dict(),
        }


class ProviderAdapter(Protocol):
    async def complete(self, *, system_prompt: str, messages: list[dict], tools: list[dict], model: str,
                       max_tokens: int, timeout: float, trace: TraceEmitter) -> AdapterResponse: ...


def _tool_definitions(tools: list[dict]) -> list[dict]:
    return [{"name": tool["name"], "description": tool["description"], "input_schema": tool["inputSchema"]} for tool in tools]


class _HttpAdapter:
    kind: str

    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client

    async def _post(self, endpoint: str, payload: dict, headers: dict[str, str], timeout: float, trace: TraceEmitter) -> dict:
        trace.emit("provider.request", provider=self.kind, endpoint=endpoint, request=payload)
        try:
            if self.client is None:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(endpoint, json=payload, headers=headers, timeout=timeout)
            else:
                response = await self.client.post(endpoint, json=payload, headers=headers, timeout=timeout)
            if response.is_error:
                try:
                    error_body: Any = response.json()
                except (json.JSONDecodeError, ValueError):
                    error_body = {"body": response.text[:10000]}
                trace.emit("provider.response", provider=self.kind, status_code=response.status_code, response=error_body)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("response is not a JSON object")
        except httpx.HTTPStatusError as error:
            raise ProviderError(f"{self.kind} returned HTTP {error.response.status_code}",
                                status_code=error.response.status_code) from error
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
            raise ProviderError(f"{self.kind} request failed ({type(error).__name__})", status_code=None) from error
        trace.emit("provider.response", provider=self.kind, response=data)
        return data


class AnthropicAdapter(_HttpAdapter):
    kind = "anthropic"

    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None = None,
                 endpoint: str = "https://api.anthropic.com/v1/messages"):
        super().__init__(api_key, client)
        self.endpoint = endpoint

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        translated: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "assistant":
                content: list[dict] = []
                if message.get("content"):
                    content.append({"type": "text", "text": message["content"]})
                content.extend({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]}
                               for call in message.get("tool_calls", []))
                translated.append({"role": "assistant", "content": content})
                continue
            if role == "tool":
                block = {"type": "tool_result", "tool_use_id": message["tool_call_id"],
                         "content": message.get("content", ""), "is_error": bool(message.get("is_error"))}
                if translated and translated[-1]["role"] == "user" and isinstance(translated[-1]["content"], list):
                    translated[-1]["content"].append(block)
                else:
                    translated.append({"role": "user", "content": [block]})
                continue
            content = message.get("content", "")
            if translated and translated[-1]["role"] == "user":
                previous = translated[-1]["content"]
                if not isinstance(previous, list):
                    previous = [{"type": "text", "text": previous}]
                    translated[-1]["content"] = previous
                previous.append({"type": "text", "text": content})
            else:
                translated.append({"role": "user", "content": content})
        return translated

    def _request_endpoint(self, model: str) -> str:
        return self.endpoint

    def _request_headers(self) -> dict[str, str]:
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _include_model_in_payload(self) -> bool:
        return True

    def _extra_payload_fields(self) -> dict[str, Any]:
        return {}

    async def complete(self, *, system_prompt: str, messages: list[dict], tools: list[dict], model: str,
                       max_tokens: int, timeout: float, trace: TraceEmitter) -> AdapterResponse:
        trace.add_secret(self.api_key)
        translated = self._messages(messages)
        if translated:
            last = translated[-1]
            content = last["content"]
            if not isinstance(content, list):
                content = [{"type": "text", "text": content}]
                last["content"] = content
            if content:
                content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
        payload: dict[str, Any] = {**self._extra_payload_fields(), "max_tokens": max_tokens, "system": [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
        ],
                                   "messages": translated}
        if self._include_model_in_payload():
            payload["model"] = model
        if tools:
            payload["tools"] = _tool_definitions(tools)
        data = await self._post(self._request_endpoint(model), payload, self._request_headers(), timeout, trace)
        try:
            blocks = data.get("content", [])
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            for block in blocks:
                if block.get("type") == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError("text block is invalid")
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    identifier, name, arguments = block.get("id"), block.get("name"), block.get("input")
                    if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                        raise ValueError("tool block is invalid")
                    calls.append(ToolCall(identifier, name, arguments))
            raw_usage = data.get("usage") or {}
            usage = Usage(int(raw_usage.get("input_tokens", 0)), int(raw_usage.get("output_tokens", 0)),
                          int(raw_usage.get("cache_read_input_tokens", 0)),
                          int(raw_usage.get("cache_creation_input_tokens", 0)), _reported_cost(raw_usage))
            return AdapterResponse("\n".join(text_parts), calls, data.get("stop_reason"), usage)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise ProviderError("Anthropic returned an invalid Messages response") from error


class BedrockInvokeAdapter(AnthropicAdapter):
    """Amazon Bedrock's Invoke API: model id goes in the URL path (not the request
    body), auth is a bearer token (Bedrock API keys), not `x-api-key`, and the
    response body is otherwise the same Anthropic Messages API shape. This is a
    different Bedrock surface than the Mantle endpoint (native Anthropic Messages
    API at a fixed URL) — accounts provisioned for standard Bedrock model access
    are not necessarily provisioned for Mantle, and vice versa."""
    kind = "bedrock"

    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None, region: str):
        super().__init__(api_key, client)
        self.region = region

    def _request_endpoint(self, model: str) -> str:
        return f"https://bedrock-runtime.{self.region}.amazonaws.com/model/{quote(model, safe='')}/invoke"

    def _request_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _include_model_in_payload(self) -> bool:
        return False

    def _extra_payload_fields(self) -> dict[str, Any]:
        # Bedrock's Anthropic wire format requires this fixed version marker
        # instead of the anthropic-version header used by the native API.
        return {"anthropic_version": "bedrock-2023-05-31"}


class OpenAICompatibleAdapter(_HttpAdapter):
    kind = "openai-compatible"

    def __init__(self, base_url: str, api_key: str | None, client: httpx.AsyncClient | None = None):
        super().__init__(api_key, client)
        self.endpoint = base_url.rstrip("/") + "/chat/completions"

    @staticmethod
    def _messages(system_prompt: str, messages: list[dict]) -> list[dict]:
        translated = [{"role": "system", "content": system_prompt}]
        for message in messages:
            role = message["role"]
            if role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.get("content") or None}
                if message.get("tool_calls"):
                    item["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"], separators=(",", ":"))}}
                        for call in message["tool_calls"]]
                translated.append(item)
            elif role == "tool":
                translated.append({"role": "tool", "tool_call_id": message["tool_call_id"],
                                   "content": message.get("content", "")})
            else:
                translated.append({"role": "user", "content": message.get("content", "")})
        return translated

    async def complete(self, *, system_prompt: str, messages: list[dict], tools: list[dict], model: str,
                       max_tokens: int, timeout: float, trace: TraceEmitter) -> AdapterResponse:
        trace.add_secret(self.api_key)
        payload: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens, "messages": self._messages(system_prompt, messages),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": tool["name"], "description": tool["description"], "parameters": tool["inputSchema"]}}
                for tool in tools]
            payload["tool_choice"] = "auto"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        data = await self._post(self.endpoint, payload, headers, timeout, trace)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            if not isinstance(content, str):
                raise ValueError("assistant content is invalid")
            calls = []
            for raw in message.get("tool_calls") or []:
                identifier, function = raw.get("id"), raw.get("function")
                if not isinstance(identifier, str) or not identifier or not isinstance(function, dict):
                    raise ValueError("tool call is invalid")
                name, encoded_arguments = function.get("name"), function.get("arguments") or "{}"
                if not isinstance(name, str) or not name:
                    raise ValueError("tool name is invalid")
                arguments = encoded_arguments if isinstance(encoded_arguments, dict) else json.loads(encoded_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments are not an object")
                calls.append(ToolCall(identifier, name, arguments))
            raw_usage = data.get("usage") or {}
            details = raw_usage.get("prompt_tokens_details") or {}
            prompt_tokens, cached_tokens = int(raw_usage.get("prompt_tokens", 0)), int(details.get("cached_tokens", 0))
            usage = Usage(max(prompt_tokens - cached_tokens, 0), int(raw_usage.get("completion_tokens", 0)),
                          cached_tokens, 0, _reported_cost(raw_usage))
            return AdapterResponse(str(content), calls, choice.get("finish_reason"), usage)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError("OpenAI-compatible endpoint returned an invalid chat-completions response") from error


def make_adapter(config: Mapping[str, Any], *, environ: Mapping[str, str] | None = None,
                 client: httpx.AsyncClient | None = None) -> ProviderAdapter:
    environ = os.environ if environ is None else environ
    variable = config.get("apiKeyEnv")
    api_key = environ.get(variable) if variable else None
    if variable and not api_key:
        raise ProviderError(f"Required provider credential environment variable is not set: {variable}")
    if config["kind"] == "anthropic":
        return AnthropicAdapter(api_key, client)
    if config["kind"] == "bedrock":
        return BedrockInvokeAdapter(api_key, client, config["region"])
    if config["kind"] == "openai-compatible":
        return OpenAICompatibleAdapter(config["baseUrl"], api_key, client)
    raise ProviderError(f"Unsupported provider: {config['kind']}")
