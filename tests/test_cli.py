import argparse
import asyncio
import json

import pytest

from harness import cli
from harness.errors import WallClockExceeded


class SlowAdapter:
    """Simulates a provider call that outlasts a very tight wall-clock limit."""

    async def complete(self, **_kwargs):
        await asyncio.sleep(2)
        raise AssertionError("should not resolve before the wall-clock limit fires")


def _write_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "wall_clock_limit_seconds": 1,
        "capabilities": [],
        "mounts": [],
    }), encoding="utf-8")
    return manifest_path


def test_run_command_relies_solely_on_asyncio_timeout_for_wall_clock(tmp_path, monkeypatch):
    """Regression: cli.run_command must not install a redundant SIGALRM timer.

    A raw SIGALRM-based timeout could fire while the interpreter is inside
    asyncio's own event-loop driver (outside run_agent's try/except), which
    would raise WallClockExceeded straight out of asyncio.run() and skip the
    run.completed trace event emitted by harness.loop.run_agent. Enforcement
    must come solely from run_agent's asyncio.timeout, so the trace is always
    complete before the process reports exit code 6.
    """
    monkeypatch.setattr("harness.loop.make_adapter", lambda *_a, **_k: SlowAdapter())

    manifest_path = _write_manifest(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    args = argparse.Namespace(manifest=manifest_path, prompt="work", trace_file=trace_path)

    exit_code = cli.run_command(args)

    assert exit_code == cli.EXIT_TIMEOUT
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert events[0]["event"] == "run.started"
    completed = [event for event in events if event["event"] == "run.completed"]
    assert len(completed) == 1
    assert completed[0]["exit_reason"] == "wall_clock_limit"
    assert completed[0]["success"] is False


def test_run_command_has_no_signal_based_timer():
    """The standalone CLI must not reach for signal.setitimer/SIGALRM; the
    asyncio.timeout enforcement inside harness.loop.run_agent is sufficient
    and always emits a complete trace before WallClockExceeded propagates."""
    import inspect

    source = inspect.getsource(cli.run_command)
    assert "signal" not in source
    assert "setitimer" not in source


@pytest.mark.parametrize("provider", ["anthropic", "bedrock", "openai-compatible"])
def test_init_writes_a_manifest_that_compiles(tmp_path, monkeypatch, provider):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--provider", provider, "--region", "eu-central-1"]) == 0
    manifest = json.loads((tmp_path / "harness-pro.json").read_text())
    assert manifest["provider"] == provider
    assert manifest["mounts"] == [{"host_path": str(tmp_path), "target": "repo", "read_only": True}]
    assert cli.main(["compile", "--manifest", "harness-pro.json", "-o", "runtime.json"]) == 0
    if provider == "bedrock":
        assert manifest["bedrock_auth"] == "sigv4" and manifest["region"] == "eu-central-1"
        assert json.loads((tmp_path / "runtime.json").read_text())["provider"]["auth"] == "sigv4"


def test_init_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harness-pro.json").write_text("{}")
    assert cli.main(["init"]) == cli.EXIT_MALFORMED
    assert "already exists" in capsys.readouterr().err
    assert (tmp_path / "harness-pro.json").read_text() == "{}"
