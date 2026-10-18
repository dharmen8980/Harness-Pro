import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from builder.models import McpServer
from builder.snapshot import capture
from harness.errors import ToolExecutionError


def test_local_snapshot_captures_schema_without_selecting_tools_implicitly():
    fixture = Path(__file__).parent / "fixtures/snapshot_server.py"
    server = McpServer(name="docs", command="python3", args=[str(fixture)])
    snapshot = asyncio.run(capture(server))
    assert snapshot.selected_tools == []
    assert snapshot.snapshot[0].name == "read_doc"
    assert snapshot.snapshot[0].inputSchema == {"type": "object", "properties": {}, "additionalProperties": False}


class Session:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def initialize(self):
        pass

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=[], nextCursor=None)


def test_remote_snapshot_passes_bearer_header_and_preserves_unauthenticated_call(monkeypatch):
    import mcp
    from mcp.client import streamable_http

    calls = []

    @asynccontextmanager
    async def client(*args, **kwargs):
        calls.append((args, kwargs))
        yield object(), object(), None

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", client)
    monkeypatch.setenv("DOCS_MCP_TOKEN", "secret-token")
    asyncio.run(capture(McpServer(
        name="docs", type="streamable-http", url="https://example.com/mcp",
        bearer_token_env="DOCS_MCP_TOKEN",
    )))
    asyncio.run(capture(McpServer(name="docs", type="streamable-http", url="https://example.com/mcp")))
    assert calls == [
        (("https://example.com/mcp",), {"headers": {"Authorization": "Bearer secret-token"}}),
        (("https://example.com/mcp",), {}),
    ]


def test_remote_snapshot_rejects_a_missing_bearer_environment_variable(monkeypatch):
    monkeypatch.delenv("DOCS_MCP_TOKEN", raising=False)
    server = McpServer(
        name="docs", type="streamable-http", url="https://example.com/mcp",
        bearer_token_env="DOCS_MCP_TOKEN",
    )
    with pytest.raises(ToolExecutionError, match="Required MCP environment variable is not set: DOCS_MCP_TOKEN"):
        asyncio.run(capture(server))
