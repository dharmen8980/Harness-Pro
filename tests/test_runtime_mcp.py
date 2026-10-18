import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from harness.errors import PolicyViolation, ToolExecutionError
from harness.mcp import McpRuntime


class Result:
    def model_dump(self, **_kwargs):
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


class Session:
    def __init__(self):
        self.description = "Read docs"

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=[SimpleNamespace(
            name="read_doc", description=self.description,
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )], nextCursor=None)

    async def call_tool(self, name, arguments):
        assert name == "read_doc" and arguments == {}
        return Result()


class ConnectedSession:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def initialize(self):
        pass


def compiled_tool():
    return {
        "server": "docs", "remoteName": "read_doc",
        "snapshot": {"name": "read_doc", "description": "Read docs",
                     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    }


def test_runtime_checks_mcp_snapshot_immediately_before_each_call():
    runtime = McpRuntime([])
    session = Session()
    runtime.sessions["docs"] = session
    assert '"text": "ok"' in asyncio.run(runtime.call(compiled_tool(), {}))
    session.description = "Changed"
    with pytest.raises(PolicyViolation, match="schema changed"):
        asyncio.run(runtime.call(compiled_tool(), {}))


def test_stdio_runtime_selectively_passes_additional_environment_variables(monkeypatch):
    import mcp
    from mcp.client import stdio

    environments = []

    @asynccontextmanager
    async def client(parameters):
        environments.append(parameters.env)
        yield object(), object()

    monkeypatch.setattr(mcp, "ClientSession", ConnectedSession)
    monkeypatch.setattr(stdio, "stdio_client", client)
    config = {"name": "docs", "type": "stdio", "command": "server", "args": [], "env": []}
    environ = {
        "PATH": "/bin",
        "MY_CUSTOM_TOKEN": "custom-token-value",
        "UNDECLARED_TOKEN": "must-not-be-passed",
    }

    async def connect():
        async with McpRuntime(
            [config], environ=environ, additional_env_vars=["MY_CUSTOM_TOKEN"],
        ) as runtime:
            await runtime._session("docs")

    asyncio.run(connect())

    assert environments == [{"PATH": "/bin", "MY_CUSTOM_TOKEN": "custom-token-value"}]


def test_streamable_http_runtime_passes_bearer_header_and_preserves_unauthenticated_call(monkeypatch):
    import mcp
    from mcp.client import streamable_http

    calls = []

    @asynccontextmanager
    async def client(*args, **kwargs):
        calls.append((args, kwargs))
        yield object(), object(), None

    monkeypatch.setattr(mcp, "ClientSession", ConnectedSession)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", client)

    async def connect(config, environ):
        async with McpRuntime([config], environ=environ) as runtime:
            await runtime._session("docs")

    asyncio.run(connect({
        "name": "docs", "type": "streamable-http", "url": "https://example.com/mcp",
        "bearerTokenEnv": "DOCS_MCP_TOKEN",
    }, {"DOCS_MCP_TOKEN": "secret-token"}))
    asyncio.run(connect({
        "name": "docs", "type": "streamable-http", "url": "https://example.com/mcp",
    }, {}))
    assert calls == [
        (("https://example.com/mcp",), {"headers": {"Authorization": "Bearer secret-token"}}),
        (("https://example.com/mcp",), {}),
    ]


def test_streamable_http_runtime_rejects_a_missing_bearer_environment_variable():
    runtime = McpRuntime([{
        "name": "docs", "type": "streamable-http", "url": "https://example.com/mcp",
        "bearerTokenEnv": "DOCS_MCP_TOKEN",
    }], environ={})
    with pytest.raises(ToolExecutionError, match="Required MCP environment variable is not set: DOCS_MCP_TOKEN"):
        asyncio.run(runtime._session("docs"))
