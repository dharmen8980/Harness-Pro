"""Interactive and non-interactive OAuth callback handlers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from html import escape
from typing import NoReturn
from urllib.parse import parse_qs, urlsplit
import webbrowser


CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT_SECONDS = 300.0
_MAX_REQUEST_BYTES = 16 * 1024


class CallbackError(RuntimeError):
    """The local OAuth callback was invalid."""


class CallbackTimeout(TimeoutError):
    """The local OAuth callback did not arrive in time."""


class NonInteractiveAuthorizationRequired(RuntimeError):
    """A refresh attempted to enter the interactive authorization flow."""


class OAuthCallbackListener:
    """A one-shot HTTP listener that accepts one valid callback request."""

    def __init__(self, port: int, timeout: float = CALLBACK_TIMEOUT_SECONDS):
        self.port = port
        self.timeout = timeout
        self.bound_port: int | None = None
        self.ready = asyncio.Event()
        self.startup_error: OSError | None = None
        self._result: asyncio.Future[tuple[str, str | None]] | None = None

    async def wait_for_callback(self) -> tuple[str, str | None]:
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        try:
            server = await asyncio.start_server(self._handle_connection, CALLBACK_HOST, self.port)
        except OSError as error:
            self.startup_error = error
            self.ready.set()
            raise
        socket = server.sockets[0]
        self.bound_port = int(socket.getsockname()[1])
        self.ready.set()
        try:
            try:
                return await asyncio.wait_for(self._result, timeout=self.timeout)
            except TimeoutError as error:
                raise CallbackTimeout("Timed out waiting for the local OAuth callback.") from error
        finally:
            server.close()
            await server.wait_closed()

    async def wait_until_ready(self) -> None:
        await self.ready.wait()
        if self.startup_error is not None:
            raise self.startup_error

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._result is None or self._result.done():
                await self._respond(writer, 410, "OAuth callback is no longer accepting requests.")
                return

            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            total_bytes = len(request_line)
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                total_bytes += len(header_line)
                if total_bytes > _MAX_REQUEST_BYTES:
                    raise CallbackError("OAuth callback request was too large.")
                if header_line in {b"\r\n", b"\n", b""}:
                    break

            try:
                method, target, _ = request_line.decode("ascii").strip().split(" ", 2)
            except (UnicodeDecodeError, ValueError) as error:
                raise CallbackError("OAuth callback request was malformed.") from error

            parsed = urlsplit(target)
            if method != "GET" or parsed.path != CALLBACK_PATH:
                await self._respond(writer, 404, "Not found.")
                return

            parameters = parse_qs(parsed.query, keep_blank_values=True)
            codes = parameters.get("code", [])
            states = parameters.get("state", [])
            if len(codes) != 1 or not codes[0]:
                await self._respond(writer, 400, "Authentication failed: the authorization code was missing.")
                self._set_exception(CallbackError("OAuth callback did not contain one authorization code."))
                return
            if len(states) > 1:
                await self._respond(writer, 400, "Authentication failed: the state parameter was invalid.")
                self._set_exception(CallbackError("OAuth callback contained multiple state parameters."))
                return

            state = states[0] if states else None
            body = "Authenticated — you can close this tab."
            await self._respond(writer, 200, body)
            if self._result is not None and not self._result.done():
                self._result.set_result((codes[0], state))
        except CallbackError as error:
            with suppress(Exception):
                await self._respond(writer, 400, "Authentication failed: the callback request was invalid.")
            self._set_exception(error)
        except Exception:
            self._set_exception(CallbackError("OAuth callback could not be processed."))
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    def _set_exception(self, error: CallbackError) -> None:
        if self._result is not None and not self._result.done():
            self._result.set_exception(error)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 410: "Gone"}[status]
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>OAuth</title></head>"
            f"<body><p>{escape(message)}</p></body></html>"
        ).encode("utf-8")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()


class InteractiveOAuthHandlers:
    """Coordinate listener startup so it is ready before opening a browser."""

    def __init__(self, port: int, timeout: float = CALLBACK_TIMEOUT_SECONDS):
        self.listener = OAuthCallbackListener(port, timeout)
        self._callback_task: asyncio.Task[tuple[str, str | None]] | None = None

    async def redirect(self, authorization_url: str) -> None:
        if self._callback_task is None:
            self._callback_task = asyncio.create_task(self.listener.wait_for_callback())
            try:
                await self.listener.wait_until_ready()
            except OSError:
                with suppress(Exception):
                    await self._callback_task
                raise
        print(f"Open this URL to authenticate:\n{authorization_url}", flush=True)
        with suppress(Exception):
            await asyncio.to_thread(webbrowser.open, authorization_url)

    async def callback(self) -> tuple[str, str | None]:
        if self._callback_task is None:
            self._callback_task = asyncio.create_task(self.listener.wait_for_callback())
            await self.listener.wait_until_ready()
        return await self._callback_task

    async def close(self) -> None:
        if self._callback_task is not None:
            if not self._callback_task.done():
                self._callback_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._callback_task


async def reject_interactive_redirect(_authorization_url: str) -> NoReturn:
    raise NonInteractiveAuthorizationRequired("Interactive authorization is disabled for refresh.")


async def reject_interactive_callback() -> NoReturn:
    raise NonInteractiveAuthorizationRequired("Interactive authorization is disabled for refresh.")
