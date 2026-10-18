"""Runtime MCP sessions with per-call snapshot verification."""
from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
from typing import Any, Mapping

from .errors import PolicyViolation, ToolExecutionError
from .tools import COMMAND_ENV_KEYS


PROCESS_ENV_KEYS = COMMAND_ENV_KEYS
DEFAULT_MCP_READ_TIMEOUT_SECONDS = 30


def _description(value: str | None) -> str:
    return " ".join((value or "").split())


def _schema(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class McpRuntime:
    def __init__(self, servers: list[dict], *, environ: Mapping[str, str] | None = None,
                 additional_env_vars: list[str] | None = None,
                 read_timeout_seconds: int = DEFAULT_MCP_READ_TIMEOUT_SECONDS):
        self.server_configs = {server["name"]: server for server in servers}
        self.environ = os.environ if environ is None else environ
        self.additional_env_vars = additional_env_vars or []
        self.read_timeout_seconds = read_timeout_seconds
        self.stack = AsyncExitStack()
        self.sessions: dict[str, Any] = {}

    async def __aenter__(self) -> "McpRuntime":
        await self.stack.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stack.__aexit__(*args)

    async def _session(self, name: str):
        if name in self.sessions:
            return self.sessions[name]
        config = self.server_configs.get(name)
        if config is None:
            raise PolicyViolation(f"MCP server is not registered: {name}")
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as error:
            raise ToolExecutionError("MCP tools require the 'snapshot' optional dependency") from error
        try:
            if config["type"] == "stdio":
                missing = [variable for variable in config["env"] if not self.environ.get(variable)]
                if missing:
                    raise ToolExecutionError(f"Required MCP environment variable is not set: {missing[0]}")
                environment = {key: self.environ[key] for key in PROCESS_ENV_KEYS if key in self.environ}
                environment.update({variable: self.environ[variable] for variable in config["env"]})
                for variable in self.additional_env_vars:
                    if variable in self.environ:
                        environment[variable] = self.environ[variable]
                streams = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=config["command"], args=config["args"], env=environment)))
                read, write = streams
            else:
                bearer_token_env = config.get("bearerTokenEnv")
                if bearer_token_env:
                    token = self.environ.get(bearer_token_env)
                    if not token:
                        raise ToolExecutionError(f"Required MCP environment variable is not set: {bearer_token_env}")
                    client = streamablehttp_client(config["url"], headers={"Authorization": f"Bearer {token}"})
                else:
                    client = streamablehttp_client(config["url"])
                read, write, _ = await self.stack.enter_async_context(client)
            session = await self.stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=self.read_timeout_seconds)))
            await session.initialize()
        except ToolExecutionError:
            raise
        except Exception as error:
            raise ToolExecutionError(f"MCP server {name} failed to connect ({type(error).__name__})") from error
        self.sessions[name] = session
        return session

    @staticmethod
    async def _tools(session: Any) -> dict[str, Any]:
        tools: dict[str, Any] = {}
        cursor = None
        for _ in range(32):
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                tools[tool.name] = tool
                if len(tools) > 256:
                    raise PolicyViolation("MCP server now exposes more than 256 tools")
            cursor = page.nextCursor
            if not cursor:
                return tools
        raise PolicyViolation("MCP tool pagination did not terminate")

    async def call(self, compiled_tool: dict, arguments: dict) -> str:
        session = await self._session(compiled_tool["server"])
        try:
            current = (await self._tools(session)).get(compiled_tool["remoteName"])
        except PolicyViolation:
            raise
        except Exception as error:
            raise ToolExecutionError(f"MCP schema check failed ({type(error).__name__})") from error
        snapshot = compiled_tool["snapshot"]
        if current is None:
            raise PolicyViolation(f"Selected MCP tool disappeared: {compiled_tool['remoteName']}")
        if (_description(current.description) != _description(snapshot["description"])
                or _schema(current.inputSchema) != _schema(snapshot["inputSchema"])):
            raise PolicyViolation(f"Selected MCP tool schema changed: {compiled_tool['remoteName']}")
        try:
            result = await session.call_tool(compiled_tool["remoteName"], arguments=arguments)
            value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception as error:
            raise ToolExecutionError(f"MCP call failed ({type(error).__name__})") from error
