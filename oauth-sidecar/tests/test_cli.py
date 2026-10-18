import asyncio
import json
import stat

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from oauth_sidecar import cli
from oauth_sidecar.config import ServerConfig
from oauth_sidecar.storage import FileTokenStorage


def test_refresh_without_login_is_clear_and_has_no_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "server.json"
    config_path.write_text(
        json.dumps({"name": "atlassian", "server_url": "https://mcp.atlassian.com/v2/mcp"}),
        encoding="utf-8",
    )

    result = cli.main(["refresh", str(config_path)])
    captured = capsys.readouterr()

    assert result == cli.EXIT_LOGIN_REQUIRED
    assert captured.out == ""
    assert "Run 'python -m oauth_sidecar login" in captured.err
    assert "Traceback" not in captured.err


def test_refresh_forces_sdk_refresh_and_returns_persisted_access_token(tmp_path, monkeypatch):
    async def scenario():
        storage = FileTokenStorage(tmp_path / "credentials" / "example.json")
        await storage.set_client_info(
            OAuthClientInformationFull(
                redirect_uris=["http://localhost:8765/callback"],
                client_id="client-id",
            )
        )
        await storage.set_tokens(OAuthToken(access_token="old", refresh_token="refresh", expires_in=3600))

        async def fake_initialize(server_url, provider, timeout):
            assert server_url == "https://mcp.example.com/mcp"
            assert timeout == cli.REFRESH_TIMEOUT_SECONDS
            assert provider.context.token_expiry_time == 1.0
            provider.context.current_tokens = OAuthToken(
                access_token="fresh", refresh_token="rotated-refresh", expires_in=3600
            )
            await provider.context.storage.set_tokens(provider.context.current_tokens)

        monkeypatch.setattr(cli, "initialize_mcp_connection", fake_initialize)
        config = ServerConfig(name="example", server_url="https://mcp.example.com/mcp")
        assert await cli.refresh_access_token(config, storage) == "fresh"

    asyncio.run(scenario())


def test_login_uses_transport_timeout_longer_than_callback_timeout(tmp_path, monkeypatch):
    async def scenario():
        captured_timeout = None

        async def fake_initialize(server_url, _provider, timeout):
            nonlocal captured_timeout
            assert server_url == "https://mcp.example.com/mcp"
            captured_timeout = timeout

        monkeypatch.setattr(cli, "initialize_mcp_connection", fake_initialize)
        config = ServerConfig(name="example", server_url="https://mcp.example.com/mcp")
        storage = FileTokenStorage(tmp_path / "credentials" / "example.json")

        await cli.login(config, storage)

        assert captured_timeout == cli.LOGIN_TIMEOUT_SECONDS
        assert captured_timeout > cli.CALLBACK_TIMEOUT_SECONDS

    asyncio.run(scenario())


def test_refresh_output_file_is_private_and_not_echoed(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "server.json"
    config_path.write_text(
        json.dumps({"name": "example", "server_url": "https://mcp.example.com/mcp"}),
        encoding="utf-8",
    )
    output_path = tmp_path / "access-token"

    async def fake_refresh(_config, _storage):
        return "only-access-token"

    monkeypatch.setattr(cli, "refresh_access_token", fake_refresh)
    result = cli.main(["refresh", str(config_path), "-o", str(output_path)])
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out == ""
    assert captured.err == ""
    assert output_path.read_text(encoding="utf-8") == "only-access-token\n"
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600


def test_refresh_error_does_not_echo_exception_secrets(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "server.json"
    config_path.write_text(
        json.dumps({"name": "example", "server_url": "https://mcp.example.com/mcp"}),
        encoding="utf-8",
    )

    async def failed_refresh(_config, _storage):
        raise RuntimeError("refresh-token-value client-secret-value")

    monkeypatch.setattr(cli, "refresh_access_token", failed_refresh)
    result = cli.main(["refresh", str(config_path)])
    captured = capsys.readouterr()

    assert result == cli.EXIT_AUTH
    assert captured.out == ""
    assert "refresh-token-value" not in captured.err
    assert "client-secret-value" not in captured.err
    assert "Traceback" not in captured.err
