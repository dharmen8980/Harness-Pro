import asyncio
from copy import deepcopy
from io import StringIO
import json
import subprocess

import pytest

from builder.standalone import compile_standalone
from harness.errors import ProviderError, TurnLimitExceeded, WallClockExceeded
from harness.loop import run_agent
from harness.providers import AdapterResponse, ToolCall, Usage
from harness.trace import TraceEmitter


class FakeAdapter:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return next(self.responses)


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_loop_dispatches_tools_and_emits_normalized_trace(tmp_path):
    config = compile_standalone({
        "capabilities": ["file_edit"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
    })
    adapter = FakeAdapter([
        AdapterResponse(tool_calls=[ToolCall("c1", "Write", {"path": "repo/result.txt", "content": "ok"})],
                        stop_reason="tool_use", usage=Usage(3, 2, 1)),
        AdapterResponse(text="Finished", stop_reason="stop", usage=Usage(4, 1, 0)),
    ])
    stream = StringIO()
    result = asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))
    assert result.text == "Finished" and result.usage.input_tokens == 7
    assert (tmp_path / "result.txt").read_text() == "ok"
    events = records(stream)
    assert events[0]["event"] == "run.started" and events[-1]["exit_reason"] == "completed"
    assert any(event["event"] == "policy.decision" and event["allowed"] for event in events)


def test_additional_environment_variable_values_are_redacted_from_trace(tmp_path, monkeypatch):
    secret = "custom-secret-value"
    monkeypatch.setenv("MY_CUSTOM_TOKEN", secret)
    config = compile_standalone({
        "capabilities": ["custom_commands"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "commands": [{
            "name": "print_token", "description": "Print the configured token.",
            "argv": ["python3", "-c", "import os; print(os.environ['MY_CUSTOM_TOKEN'])"],
            "working_directory": "repo",
        }],
        "additional_env_vars": ["MY_CUSTOM_TOKEN"],
    })
    adapter = FakeAdapter([
        AdapterResponse(tool_calls=[ToolCall("c1", "print_token", {})]),
        AdapterResponse(text="Finished", stop_reason="stop"),
    ])
    stream = StringIO()

    asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))

    trace_output = stream.getvalue()
    assert secret not in trace_output
    assert "[REDACTED]" in trace_output


def test_loop_aggregates_priced_usage_across_turns(tmp_path):
    config = compile_standalone({
        "capabilities": ["file_edit"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
        "pricing": {"input_per_million": 2, "output_per_million": 4,
                    "cache_read_per_million": 1, "cache_write_per_million": 3},
    })
    adapter = FakeAdapter([
        AdapterResponse(tool_calls=[ToolCall("c1", "Write", {"path": "repo/result.txt", "content": "ok"})],
                        stop_reason="tool_use", usage=Usage(100, 20, 30, 40)),
        AdapterResponse(text="Finished", stop_reason="stop", usage=Usage(50, 10, 20, 0)),
    ])
    result = asyncio.run(run_agent(config, "work", TraceEmitter(StringIO()), adapter=adapter))
    expected = ((100 * 2 + 20 * 4 + 30 * 1 + 40 * 3) + (50 * 2 + 10 * 4 + 20 * 1)) / 1_000_000
    assert result.usage.cost_usd == pytest.approx(expected)


def test_rejected_tool_call_is_traced_and_returned_to_model(tmp_path):
    config = compile_standalone({
        "capabilities": ["file_edit"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
    })
    adapter = FakeAdapter([
        AdapterResponse(tool_calls=[ToolCall("c1", "Write", {"path": "repo/../escape", "content": "x"})]),
        AdapterResponse(text="done", stop_reason="stop"),
    ])
    stream = StringIO()
    result = asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))
    assert result.text == "done"
    assert adapter.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "Write",
        "content": "Absolute paths and path traversal are not allowed",
        "is_error": True,
    }
    events = records(stream)
    assert any(event["event"] == "policy.decision" and not event["allowed"] for event in events)
    assert events[-1]["exit_reason"] == "completed"


def test_rejected_tool_call_can_be_corrected_on_next_turn(tmp_path):
    config = compile_standalone({
        "capabilities": ["file_edit"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
    })
    adapter = FakeAdapter([
        AdapterResponse(tool_calls=[ToolCall("c1", "Write", {"path": "repo/result.txt"})]),
        AdapterResponse(tool_calls=[ToolCall(
            "c2", "Write", {"path": "repo/result.txt", "content": "corrected"},
        )]),
        AdapterResponse(text="recovered", stop_reason="stop"),
    ])
    stream = StringIO()

    result = asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))

    assert result.text == "recovered"
    assert result.exit_reason == "completed"
    assert (tmp_path / "result.txt").read_text() == "corrected"
    events = records(stream)
    decision = next(event for event in events
                    if event["event"] == "policy.decision" and event["tool_call_id"] == "c1")
    rejected = next(event for event in events
                    if event["event"] == "tool.completed" and event["tool_call_id"] == "c1")
    assert decision["allowed"] is False
    assert decision["reason"] == "Arguments rejected for Write: 'content' is a required property"
    assert rejected["success"] is False and rejected["rejected"] is True
    assert rejected["error"] == decision["reason"]
    assert any(event["event"] == "tool.completed" and event["tool_call_id"] == "c2"
               and event["success"] is True for event in events)
    assert events[-1]["exit_reason"] == "completed" and events[-1]["success"] is True


def test_turn_limit_is_a_distinct_failure(tmp_path):
    config = compile_standalone({"turn_limit": 1, "capabilities": [], "mounts": []})
    adapter = FakeAdapter([AdapterResponse(tool_calls=[ToolCall("c1", "missing", {})])])
    stream = StringIO()
    with pytest.raises(TurnLimitExceeded):
        asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))
    events = records(stream)
    assert any(event["event"] == "tool.completed" and event["rejected"] is True for event in events)
    assert events[-1]["exit_reason"] == "turn_limit"

    config["tools"] = [{"name": "noop", "description": "noop", "inputSchema": {
        "type": "object", "properties": {}, "additionalProperties": False}, "kind": "action",
        "argv": ["python3", "-c", "print('ok')"], "cwd": "repo", "timeoutSeconds": 2}]
    config["mounts"] = [{"target": "repo", "hostPath": str(tmp_path), "readOnly": True}]
    adapter = FakeAdapter([AdapterResponse(tool_calls=[ToolCall("c2", "noop", {})])])
    with pytest.raises(TurnLimitExceeded):
        asyncio.run(run_agent(config, "work", TraceEmitter(StringIO()), adapter=adapter))


def test_wall_clock_limit_is_enforced_and_traced():
    class SlowAdapter:
        async def complete(self, **_kwargs):
            await asyncio.sleep(0.1)
            return AdapterResponse(text="too late")

    config = compile_standalone({"capabilities": [], "mounts": []})
    config["wallClockLimitSeconds"] = 0.01
    stream = StringIO()
    with pytest.raises(WallClockExceeded):
        asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=SlowAdapter()))
    assert records(stream)[-1]["exit_reason"] == "wall_clock_limit"


def test_retryable_provider_error_retries_then_succeeds(monkeypatch):
    class RetryAdapter:
        def __init__(self):
            self.requests = []

        async def complete(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            if len(self.requests) <= 2:
                raise ProviderError("rate limited", status_code=429)
            return AdapterResponse(text="recovered", stop_reason="stop")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("harness.loop.asyncio.sleep", no_sleep)
    config = compile_standalone({"capabilities": [], "mounts": [], "max_output_tokens": 1234})
    adapter = RetryAdapter()
    stream = StringIO()

    result = asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))

    assert result.text == "recovered"
    assert len(adapter.requests) == 3
    assert all(request["max_tokens"] == 1234 for request in adapter.requests)
    retry_events = [event for event in records(stream) if event["event"] == "provider.retry"]
    assert [(event["attempt"], event["status_code"]) for event in retry_events] == [(1, 429), (2, 429)]


def test_non_retryable_provider_error_is_not_retried():
    class BadRequestAdapter:
        def __init__(self):
            self.calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            raise ProviderError("bad request", status_code=400)

    config = compile_standalone({"capabilities": [], "mounts": []})
    adapter = BadRequestAdapter()
    stream = StringIO()

    with pytest.raises(ProviderError, match="bad request"):
        asyncio.run(run_agent(config, "work", TraceEmitter(stream), adapter=adapter))

    events = records(stream)
    assert adapter.calls == 1
    assert not any(event["event"] == "provider.retry" for event in events)
    assert events[-1]["event"] == "run.completed"
    assert events[-1]["exit_reason"] == "provider_error" and events[-1]["success"] is False


def test_loop_commits_with_bounded_message_and_traces_result(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.name", "Harness Test")
    git("config", "user.email", "harness@example.invalid")
    (tmp_path / "tracked.txt").write_text("before\n")
    git("add", "tracked.txt")
    git("commit", "-m", "Initial")
    (tmp_path / "tracked.txt").write_text("after\n")
    config = compile_standalone({
        "capabilities": ["git_write"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
        "git_directory": "repo",
    })
    message = "fix: tracked file"
    adapter = FakeAdapter([
        AdapterResponse(tool_calls=[ToolCall("commit-1", "GitCommit", {"commit_message": message})]),
        AdapterResponse(text="Committed", stop_reason="stop"),
    ])
    stream = StringIO()
    result = asyncio.run(run_agent(config, "commit it", TraceEmitter(stream), adapter=adapter))
    assert result.text == "Committed"
    assert git("log", "-1", "--pretty=%B").stdout.strip() == message
    trace = records(stream)
    started = next(event for event in trace if event["event"] == "tool.started")
    completed = next(event for event in trace if event["event"] == "tool.completed")
    assert started["arguments"] == {"commit_message": message}
    assert completed["success"] is True and '"exit_code": 0' in completed["result"]
