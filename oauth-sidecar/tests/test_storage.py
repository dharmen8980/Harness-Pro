import asyncio
import json
import stat

from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken, ProtectedResourceMetadata

from oauth_sidecar.storage import FileTokenStorage, write_private_text


def test_storage_absent_values_round_trip_and_permissions(tmp_path):
    path = tmp_path / "credentials" / "example.json"
    storage = FileTokenStorage(path)

    assert asyncio.run(storage.get_tokens()) is None
    assert asyncio.run(storage.get_client_info()) is None

    tokens = OAuthToken(
        access_token="short-lived-access",
        expires_in=3600,
        scope="read write",
        refresh_token="long-lived-refresh",
    )
    client_info = OAuthClientInformationFull(
        redirect_uris=["http://localhost:8765/callback"],
        client_name="harness-oauth-sidecar",
        client_id="registered-client",
        client_secret="registered-secret",
        token_endpoint_auth_method="client_secret_post",
        issuer="https://auth.example.com/",
    )
    oauth_metadata = OAuthMetadata(
        issuer="https://auth.example.com/",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        registration_endpoint="https://auth.example.com/register",
    )
    resource_metadata = ProtectedResourceMetadata(
        resource="https://mcp.example.com/mcp",
        authorization_servers=["https://auth.example.com/"],
    )
    asyncio.run(storage.set_client_info(client_info))
    asyncio.run(storage.set_tokens(tokens))
    asyncio.run(storage.set_discovery_metadata(oauth_metadata, resource_metadata))

    assert asyncio.run(storage.get_tokens()) == tokens
    assert asyncio.run(storage.get_client_info()) == client_info
    assert asyncio.run(storage.get_discovery_metadata()) == (oauth_metadata, resource_metadata)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["tokens"] == tokens.model_dump(mode="json")
    assert document["client_info"] == client_info.model_dump(mode="json")
    assert document["oauth_metadata"] == oauth_metadata.model_dump(mode="json")
    assert document["protected_resource_metadata"] == resource_metadata.model_dump(mode="json")


def test_private_output_replaces_permissive_file_as_mode_0600(tmp_path):
    output = tmp_path / "token.txt"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)

    write_private_text(output, "new-access-token\n")

    assert output.read_text(encoding="utf-8") == "new-access-token\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
