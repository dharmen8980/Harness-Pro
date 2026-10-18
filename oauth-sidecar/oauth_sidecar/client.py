"""Minimal MCP connection used to drive the SDK OAuth provider."""

from __future__ import annotations

from contextlib import AsyncExitStack, contextmanager
from datetime import timedelta
import logging
from typing import Iterator

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


@contextmanager
def _silence_sdk_auth_logging() -> Iterator[None]:
    """Prevent SDK exception logging from echoing OAuth response bodies."""
    logger = logging.getLogger("mcp.client.auth")
    old_propagate = logger.propagate
    null_handler = logging.NullHandler()
    logger.addHandler(null_handler)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(null_handler)
        logger.propagate = old_propagate


async def initialize_mcp_connection(server_url: str, auth: object, timeout: float) -> None:
    """Open one streamable-HTTP session and perform MCP initialization."""
    with _silence_sdk_auth_logging():
        async with AsyncExitStack() as stack:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(server_url, auth=auth, timeout=timeout)
            )
            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20))
            )
            await session.initialize()
