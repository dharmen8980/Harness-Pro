"""Provider-neutral agent loop."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import os
import time
from typing import Mapping

from .errors import PolicyViolation, ProviderError, ToolExecutionError, TurnLimitExceeded, WallClockExceeded
from .mcp import DEFAULT_MCP_READ_TIMEOUT_SECONDS, McpRuntime
from .providers import ProviderAdapter, Usage, compute_cost, make_adapter
from .tools import MAX_TOOL_OUTPUT, ToolDispatcher
from .trace import TraceEmitter


MAX_PROVIDER_RETRIES = 3
PROVIDER_RETRY_BACKOFF_SECONDS = (1, 2, 4)


@dataclass(frozen=True)
class AgentResult:
    text: str
    turns: int
    usage: Usage
    wall_time_seconds: float
    exit_reason: str = "completed"


def _add_usage(first: Usage, second: Usage) -> Usage:
    costs = [value for value in (first.cost_usd, second.cost_usd) if value is not None]
    return Usage(first.input_tokens + second.input_tokens, first.output_tokens + second.output_tokens,
                 first.cache_read_input_tokens + second.cache_read_input_tokens,
                 first.cache_write_input_tokens + second.cache_write_input_tokens, sum(costs) if costs else None)


async def _conversation(config: dict, prompt: str, adapter: ProviderAdapter, trace: TraceEmitter,
                        mcp: McpRuntime, started: float) -> AgentResult:
    dispatcher = ToolDispatcher(config, mcp)
    messages = [{"role": "user", "content": prompt}]
    usage = Usage()
    known_call_ids: set[str] = set()
    tool_definitions = [{key: tool[key] for key in ("name", "description", "inputSchema")} for tool in config["tools"]]

    for turn in range(1, config["maxTurns"] + 1):
        trace.emit("turn.started", turn=turn)
        for attempt in range(MAX_PROVIDER_RETRIES + 1):
            remaining = config["wallClockLimitSeconds"] - (time.monotonic() - started)
            if remaining <= 0:
                raise WallClockExceeded("Run exceeded its wall-clock limit")
            try:
                response = await adapter.complete(
                    system_prompt=config["systemPrompt"], messages=messages, tools=tool_definitions,
                    model=config["model"], max_tokens=config["maxOutputTokens"],
                    timeout=remaining, trace=trace,
                )
                break
            except ProviderError as error:
                if error.status_code not in (None, 429, 500, 502, 503, 504) or attempt >= MAX_PROVIDER_RETRIES:
                    raise
                sleep_seconds = PROVIDER_RETRY_BACKOFF_SECONDS[attempt]
                remaining = config["wallClockLimitSeconds"] - (time.monotonic() - started)
                if remaining - sleep_seconds <= 0:
                    raise
                trace.emit("provider.retry", attempt=attempt + 1, status_code=error.status_code,
                           sleep_seconds=sleep_seconds)
                await asyncio.sleep(sleep_seconds)
        turn_usage = replace(response.usage, cost_usd=compute_cost(response.usage, config["pricing"]))
        usage = _add_usage(usage, turn_usage)
        calls = response.tool_calls
        if any(not call.id or call.id in known_call_ids for call in calls) or len({call.id for call in calls}) != len(calls):
            raise ProviderError("Provider returned an empty or duplicate tool-call ID")
        known_call_ids.update(call.id for call in calls)
        messages.append({"role": "assistant", "content": response.text,
                         "tool_calls": [call.as_dict() for call in calls]})
        trace.emit("turn.completed", turn=turn, stop_reason=response.stop_reason,
                   assistant_text=response.text, tool_calls=[call.as_dict() for call in calls],
                   usage=turn_usage.as_dict())
        if not calls:
            return AgentResult(response.text, turn, usage, time.monotonic() - started)

        for call in calls:
            trace.emit("tool.started", turn=turn, tool_call_id=call.id, tool=call.name, arguments=call.arguments)
            try:
                result = await dispatcher.execute(call.name, call.arguments)
                if len(result.encode("utf-8")) > MAX_TOOL_OUTPUT:
                    result = result.encode("utf-8")[:MAX_TOOL_OUTPUT].decode("utf-8", errors="ignore") + "\n[output truncated]"
                trace.emit("policy.decision", turn=turn, tool_call_id=call.id, tool=call.name, allowed=True)
                trace.emit("tool.completed", turn=turn, tool_call_id=call.id, tool=call.name,
                           success=True, result=result)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": result, "is_error": False})
            except PolicyViolation as error:
                trace.emit("policy.decision", turn=turn, tool_call_id=call.id, tool=call.name,
                           allowed=False, reason=str(error))
                trace.emit("tool.completed", turn=turn, tool_call_id=call.id, tool=call.name,
                           success=False, rejected=True, error=str(error))
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": str(error), "is_error": True})
            except ToolExecutionError as error:
                trace.emit("policy.decision", turn=turn, tool_call_id=call.id, tool=call.name, allowed=True)
                trace.emit("tool.completed", turn=turn, tool_call_id=call.id, tool=call.name,
                           success=False, rejected=False, error=str(error))
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": str(error), "is_error": True})

    raise TurnLimitExceeded(f"Run exhausted the {config['maxTurns']}-turn limit")


async def run_agent(config: dict, prompt: str, trace: TraceEmitter, *, adapter: ProviderAdapter | None = None,
                    environ: Mapping[str, str] | None = None) -> AgentResult:
    """Run one headless conversation and emit a complete provider-agnostic trace."""
    runtime_environment = os.environ if environ is None else environ
    credential_names = [config["provider"].get("apiKeyEnv")]
    credential_names.extend(variable for server in config["mcpServers"] for variable in server.get("env", []))
    credential_names.extend(server.get("bearerTokenEnv") for server in config["mcpServers"])
    credential_names.extend(config.get("additionalEnvVars", []))
    for variable in credential_names:
        if variable:
            trace.add_secret(runtime_environment.get(variable))
    started = time.monotonic()
    trace.emit("run.started", manifest=config["name"], provider=config["provider"]["kind"], model=config["model"],
               max_turns=config["maxTurns"], wall_clock_limit_seconds=config["wallClockLimitSeconds"])
    try:
        selected_adapter = adapter or make_adapter(config["provider"], environ=runtime_environment)
        async with asyncio.timeout(config["wallClockLimitSeconds"]):
            async with McpRuntime(
                config["mcpServers"], environ=runtime_environment,
                additional_env_vars=config.get("additionalEnvVars", []),
                read_timeout_seconds=config.get("mcpReadTimeoutSeconds", DEFAULT_MCP_READ_TIMEOUT_SECONDS),
            ) as mcp:
                result = await _conversation(config, prompt, selected_adapter, trace, mcp, started)
    except TimeoutError as error:
        elapsed = time.monotonic() - started
        trace.emit("run.completed", exit_reason="wall_clock_limit", success=False, wall_time_seconds=elapsed)
        raise WallClockExceeded(f"Run exceeded {config['wallClockLimitSeconds']} seconds") from error
    except WallClockExceeded:
        trace.emit("run.completed", exit_reason="wall_clock_limit", success=False,
                   wall_time_seconds=time.monotonic() - started)
        raise
    except TurnLimitExceeded:
        trace.emit("run.completed", exit_reason="turn_limit", success=False,
                   wall_time_seconds=time.monotonic() - started)
        raise
    except ProviderError:
        trace.emit("run.completed", exit_reason="provider_error", success=False,
                   wall_time_seconds=time.monotonic() - started)
        raise
    except Exception:
        trace.emit("run.completed", exit_reason="runtime_error", success=False,
                   wall_time_seconds=time.monotonic() - started)
        raise
    trace.emit("run.completed", exit_reason=result.exit_reason, success=True, final_text=result.text,
               turns=result.turns, usage=result.usage.as_dict(), wall_time_seconds=result.wall_time_seconds)
    return result
