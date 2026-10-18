import asyncio
from contextlib import asynccontextmanager

from oauth_sidecar import client


def test_initialize_mcp_connection_passes_timeout_to_transport(monkeypatch):
    captured = {}
    auth = object()

    @asynccontextmanager
    async def fake_streamablehttp_client(server_url, **kwargs):
        captured["server_url"] = server_url
        captured["kwargs"] = kwargs
        yield object(), object(), None

    class FakeClientSession:
        def __init__(self, _read, _write, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc_value, _traceback):
            pass

        async def initialize(self):
            pass

    monkeypatch.setattr(client, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(client, "ClientSession", FakeClientSession)

    asyncio.run(client.initialize_mcp_connection("https://mcp.example.com/mcp", auth, timeout=330.0))

    assert captured == {
        "server_url": "https://mcp.example.com/mcp",
        "kwargs": {"auth": auth, "timeout": 330.0},
    }
