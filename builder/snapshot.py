"""Explicit local MCP discovery. Never invoked by the compile-only web API."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import os
from pathlib import Path
import sys

from harness.errors import ToolExecutionError
from harness.mcp import DEFAULT_MCP_READ_TIMEOUT_SECONDS

from .models import McpServer
from .serialization import json_text


async def capture(server: McpServer) -> McpServer:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    async with AsyncExitStack() as stack:
        if server.type == "stdio":
            read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=server.command, args=server.args, env=server.env)))
        else:
            if server.bearer_token_env:
                token = os.environ.get(server.bearer_token_env)
                if not token:
                    raise ToolExecutionError(f"Required MCP environment variable is not set: {server.bearer_token_env}")
                client = streamablehttp_client(server.url, headers={"Authorization": f"Bearer {token}"})
            else:
                client = streamablehttp_client(server.url)
            read, write, _ = await stack.enter_async_context(client)
        session = await stack.enter_async_context(ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=DEFAULT_MCP_READ_TIMEOUT_SECONDS)))
        await session.initialize()
        tools, cursor = [], None
        for _ in range(32):
            page = await session.list_tools(cursor=cursor)
            tools.extend({"name": tool.name, "description": tool.description or tool.name, "inputSchema": tool.inputSchema} for tool in page.tools)
            if len(tools) > 256:
                raise ValueError("Server exceeds v1's 256-tool snapshot limit")
            cursor = page.nextCursor
            if not cursor:
                break
        else:
            raise ValueError("MCP pagination did not terminate")
    return McpServer.model_validate({**server.model_dump(mode="json"), "snapshot": sorted(tools, key=lambda t: t["name"]), "selected_tools": []})


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture MCP schemas locally; a stdio config executes its named server")
    parser.add_argument("server", type=Path, help="MCP server JSON with name, type and launch configuration")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        server = McpServer.model_validate_json(args.server.read_text())
        snapshot = asyncio.run(asyncio.wait_for(capture(server), timeout=60))
        args.output.write_text(json_text(snapshot.model_dump(mode="json")))
        print(f"Captured {len(snapshot.snapshot)} tools. Import {args.output} into the builder and select tools.")
        return 0
    except Exception as error:
        # Client errors may contain headers; show the failure type without credentials.
        print(f"Snapshot failed ({type(error).__name__}). Check the local server configuration and connectivity.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
