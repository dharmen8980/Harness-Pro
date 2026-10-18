import asyncio
from io import StringIO
import json

import httpx
import pytest

from harness.errors import ProviderError
from harness.providers import AnthropicAdapter, BedrockInvokeAdapter, OpenAICompatibleAdapter, Usage, compute_cost, make_adapter
from harness.trace import TraceEmitter


def test_anthropic_adapter_normalizes_tools_and_usage():
    requests = []
    trace_stream = StringIO()

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "echo secret"},
                        {"type": "tool_use", "id": "call-1", "name": "Read", "input": {"path": "repo/a.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_creation_input_tokens": 7,
                      "cache_read_input_tokens": 3},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AnthropicAdapter("secret", client).complete(
                system_prompt="system", messages=[{"role": "user", "content": "read"}],
                tools=[{"name": "Read", "description": "Read", "inputSchema": {"type": "object"}}],
                model="claude-test", max_tokens=1234, timeout=10, trace=TraceEmitter(trace_stream),
            )

    result = asyncio.run(run())
    assert result.text == "echo secret"
    assert result.tool_calls[0].arguments == {"path": "repo/a.py"}
    assert result.usage == Usage(10, 4, 3, 7)
    assert requests[0] == {
        "model": "claude-test",
        "max_tokens": 1234,
        "system": [{"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "read", "cache_control": {"type": "ephemeral"}},
        ]}],
        "tools": [{"name": "Read", "description": "Read", "input_schema": {"type": "object"}}],
    }
    assert "secret" not in trace_stream.getvalue()


def test_make_adapter_builds_bedrock_invoke_endpoint():
    adapter = make_adapter({
        "kind": "bedrock", "baseUrl": None, "region": "eu-west-1", "apiKeyEnv": "BEDROCK_TOKEN",
    }, environ={"BEDROCK_TOKEN": "secret"})
    assert isinstance(adapter, BedrockInvokeAdapter)
    assert adapter.region == "eu-west-1"


def test_bedrock_adapter_uses_invoke_wire_format():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 5, "cache_creation_input_tokens": 8,
                      "cache_read_input_tokens": 4},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BedrockInvokeAdapter("bearer-secret", client, "us-east-1").complete(
                system_prompt="system", messages=[{"role": "user", "content": "hello"}], tools=[],
                model="us.anthropic.claude-sonnet-4-5-20250929-v1:0", max_tokens=4096,
                timeout=10, trace=TraceEmitter(StringIO()),
            )

    result = asyncio.run(run())
    request = requests[0]
    assert str(request.url) == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
        "us.anthropic.claude-sonnet-4-5-20250929-v1%3A0/invoke"
    )
    assert request.headers["authorization"] == "Bearer bearer-secret"
    assert "x-api-key" not in request.headers
    assert json.loads(request.content) == {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "system": [{"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}},
        ]}],
    }
    assert result.text == "done" and result.stop_reason == "end_turn"
    assert result.usage == Usage(12, 5, 4, 8)


def test_anthropic_adapter_caches_last_message_of_growing_conversation():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    messages = [
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "Read", "arguments": {"path": "a"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": "contents", "is_error": False},
    ]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AnthropicAdapter("secret", client).complete(
                system_prompt="system", messages=messages, tools=[],
                model="claude-test", max_tokens=4096, timeout=10, trace=TraceEmitter(StringIO()),
            )

    asyncio.run(run())
    sent_messages = requests[0]["messages"]
    # The tool_result (last block of the last message) carries the breakpoint...
    assert sent_messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # ...and no earlier block does, so there is exactly one breakpoint on the messages array.
    earlier_blocks = [block for message in sent_messages[:-1] for block in
                      (message["content"] if isinstance(message["content"], list) else [message["content"]])]
    earlier_blocks += sent_messages[-1]["content"][:-1]
    assert all(not isinstance(block, dict) or "cache_control" not in block for block in earlier_blocks)


def test_openai_adapter_round_trips_normalized_tool_results():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 5}},
        })

    messages = [
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "Read", "arguments": {"path": "repo/a"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": "contents", "is_error": False},
    ]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenAICompatibleAdapter("http://localhost:1234/v1", None, client).complete(
                system_prompt="system", messages=messages, tools=[], model="local", max_tokens=2345, timeout=10,
                trace=TraceEmitter(StringIO()),
            )

    result = asyncio.run(run())
    assert result.text == "done" and result.usage == Usage(3, 2, 5, 0)
    assert requests[0]["max_tokens"] == 2345
    assert requests[0]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "contents"}


def test_compute_cost_requires_pricing():
    assert compute_cost(Usage(input_tokens=100, output_tokens=20), None) is None
    assert compute_cost(Usage(input_tokens=100, output_tokens=20), {
        "input_per_million": None, "output_per_million": None,
        "cache_read_per_million": None, "cache_write_per_million": None,
    }) is None


def test_compute_cost_prices_fresh_and_cached_tokens():
    usage = Usage(input_tokens=100, output_tokens=20, cache_read_input_tokens=30, cache_write_input_tokens=40)
    pricing = {"input_per_million": 2, "output_per_million": 4,
               "cache_read_per_million": 1, "cache_write_per_million": 3}
    assert compute_cost(usage, pricing) == pytest.approx((100 * 2 + 20 * 4 + 30 * 1 + 40 * 3) / 1_000_000)


def test_compute_cost_preserves_provider_reported_cost():
    class UnreadablePricing(dict):
        def get(self, _key):
            raise AssertionError("pricing should not be consulted")

    def handler(_request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "cost_usd": 0.25},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenAICompatibleAdapter("http://localhost:1234/v1", None, client).complete(
                system_prompt="system", messages=[], tools=[], model="local", max_tokens=4096, timeout=10,
                trace=TraceEmitter(StringIO()),
            )

    result = asyncio.run(run())
    assert result.usage.cost_usd == 0.25
    assert compute_cost(result.usage, UnreadablePricing()) == 0.25


@pytest.mark.parametrize("status_code", [429, 500])
def test_provider_error_carries_http_status_code(status_code):
    def handler(_request):
        return httpx.Response(status_code, json={"error": "unavailable"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AnthropicAdapter("secret", client).complete(
                system_prompt="system", messages=[], tools=[], model="claude-test", max_tokens=4096,
                timeout=10, trace=TraceEmitter(StringIO()),
            )

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.status_code == status_code


def test_provider_error_has_no_status_code_for_network_failure():
    def handler(request):
        raise httpx.ConnectError("connection failed", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenAICompatibleAdapter("http://localhost:1234/v1", None, client).complete(
                system_prompt="system", messages=[], tools=[], model="local", max_tokens=4096,
                timeout=10, trace=TraceEmitter(StringIO()),
            )

    with pytest.raises(ProviderError) as raised:
        asyncio.run(run())
    assert raised.value.status_code is None
