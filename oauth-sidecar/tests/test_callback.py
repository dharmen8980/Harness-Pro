import asyncio
import errno

import httpx
import pytest

from oauth_sidecar.callback import (
    CallbackError,
    NonInteractiveAuthorizationRequired,
    OAuthCallbackListener,
    reject_interactive_callback,
    reject_interactive_redirect,
)


async def _wait_until_ready_or_skip(listener, task):
    try:
        await asyncio.wait_for(listener.wait_until_ready(), timeout=1.0)
    except PermissionError as error:
        with pytest.raises(PermissionError):
            await task
        if error.errno == errno.EPERM:
            pytest.skip("sandbox forbids binding a loopback listener")
        raise


class _MemoryWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def test_callback_request_parsing_without_a_network_socket():
    async def scenario():
        listener = OAuthCallbackListener(port=0)
        listener._result = asyncio.get_running_loop().create_future()
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /callback?code=abc&state=expected HTTP/1.1\r\nHost: localhost\r\n\r\n")
        reader.feed_eof()
        writer = _MemoryWriter()

        await listener._handle_connection(reader, writer)

        assert await listener._result == ("abc", "expected")
        assert bytes(writer.data).startswith(b"HTTP/1.1 200 OK\r\n")
        assert writer.closed

    asyncio.run(scenario())


def test_callback_captures_values_and_shuts_down_after_one_request():
    async def scenario():
        listener = OAuthCallbackListener(port=0, timeout=2.0)
        task = asyncio.create_task(listener.wait_for_callback())
        await _wait_until_ready_or_skip(listener, task)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{listener.bound_port}/callback",
                params={"code": "authorization-code", "state": "csrf-state"},
            )
        result = await task

        assert response.status_code == 200
        assert "Authenticated" in response.text
        assert result == ("authorization-code", "csrf-state")
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", listener.bound_port)

    asyncio.run(scenario())


def test_callback_rejects_missing_code():
    async def scenario():
        listener = OAuthCallbackListener(port=0, timeout=2.0)
        task = asyncio.create_task(listener.wait_for_callback())
        await _wait_until_ready_or_skip(listener, task)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{listener.bound_port}/callback",
                params={"state": "csrf-state"},
            )
        assert response.status_code == 400
        with pytest.raises(CallbackError, match="authorization code"):
            await task

    asyncio.run(scenario())


def test_refresh_handlers_fail_immediately_without_interactivity(monkeypatch):
    def browser_must_not_open(_url):
        raise AssertionError("browser interaction was attempted")

    monkeypatch.setattr("webbrowser.open", browser_must_not_open)

    with pytest.raises(NonInteractiveAuthorizationRequired):
        asyncio.run(reject_interactive_redirect("https://auth.example/authorize"))
    with pytest.raises(NonInteractiveAuthorizationRequired):
        asyncio.run(reject_interactive_callback())
