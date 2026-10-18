import json

from fastapi.testclient import TestClient

import builder.api as builder_api
from builder.api import app
from builder.models import Manifest
from builder.standalone import compile_standalone

client = TestClient(app)


def test_catalog_and_compile_use_the_standalone_contract():
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    catalog = response.json()
    assert isinstance(catalog["bash_programs"], list)
    assert catalog["bash_programs"]
    assert all(isinstance(program, str) for program in catalog["bash_programs"])
    assert {capability["id"] for capability in catalog["capabilities"]} == {
        "repo_read", "file_edit", "run_tests", "custom_commands", "bash_commands", "git_read", "git_write", "mcp_tools",
        "build_test", "gitlab_mr", "artifact_io",
    }
    manifest = catalog["default_manifest"]
    response = client.post("/api/compile", json=manifest)
    assert response.status_code == 200
    assert response.json() == compile_standalone(manifest)
    for preset in catalog["presets"]:
        compiled = client.post("/api/compile", json=preset["manifest"])
        assert compiled.status_code == 200
        assert compiled.json()["version"] == 1


def test_compile_supports_openai_compatible_manifests():
    manifest = Manifest.model_validate({
        "provider": "openai-compatible", "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": None, "model": "qwen3-coder", "capabilities": [], "mounts": [],
    })
    response = client.post("/api/compile", json=manifest.model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["provider"] == {
        "kind": "openai-compatible", "baseUrl": "http://127.0.0.1:11434/v1", "region": None,
        "apiKeyEnv": None,
    }


def test_compile_supports_full_prompt_overrides():
    override = "Custom system prompt\nwith exact spacing.  "
    response = client.post("/api/compile", json={
        "capabilities": [], "mounts": [], "prompt": {"behavior": "Ignored", "override": override},
    })
    assert response.status_code == 200
    assert response.json()["systemPrompt"] == override


def test_rejected_credentials_are_not_echoed():
    secret = "sk-ant-" + "x" * 40
    response = client.post("/api/compile", json={"description": secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_schema_rejects_invented_fields_and_invalid_mounts():
    assert client.post("/api/compile", json={"allow_shell": True}).status_code == 422
    assert client.post("/api/compile", json={"capabilities": ["file_edit"]}).status_code == 422


def test_removed_legacy_endpoints_are_not_available():
    assert client.post("/api/preview", json={}).status_code in {404, 405}
    assert client.post("/api/generate", json={}).status_code in {404, 405}


def test_cli_binaries_returns_empty_when_manifest_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(builder_api, "CLI_DIST", tmp_path / "missing")
    response = client.get("/api/cli-binaries")
    assert response.status_code == 200
    assert response.json() == {"binaries": []}


def test_cli_binaries_only_returns_files_present_on_disk(tmp_path, monkeypatch):
    cli_dist = tmp_path / "cli"
    cli_dist.mkdir()
    present = {"filename": "harness-present", "os": "linux", "arch": "x86_64"}
    missing = {"filename": "harness-missing", "os": "macos", "arch": "arm64"}
    (cli_dist / present["filename"]).touch()
    (cli_dist / "manifest.json").write_text(
        json.dumps({"binaries": [present, missing]}), encoding="utf-8"
    )
    monkeypatch.setattr(builder_api, "CLI_DIST", cli_dist)

    response = client.get("/api/cli-binaries")
    assert response.status_code == 200
    assert response.json() == {"binaries": [present]}


def test_cli_binary_download_serves_file_created_after_process_start(tmp_path, monkeypatch):
    # CLI_DIST may not exist yet when the server process starts (no build has run). The
    # download route must still work once a binary shows up later, without a restart.
    cli_dist = tmp_path / "cli-created-later"
    monkeypatch.setattr(builder_api, "CLI_DIST", cli_dist)
    assert client.get("/dl/cli/harness-built-later").status_code == 404

    cli_dist.mkdir()
    (cli_dist / "harness-built-later").write_bytes(b"fake binary contents")
    response = client.get("/dl/cli/harness-built-later")
    assert response.status_code == 200
    assert response.content == b"fake binary contents"


def test_cli_binary_download_rejects_path_traversal(tmp_path, monkeypatch):
    cli_dist = tmp_path / "cli"
    cli_dist.mkdir()
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(builder_api, "CLI_DIST", cli_dist)
    response = client.get("/dl/cli/..%2Fsecret.txt")
    assert response.status_code == 404


def test_cli_binary_download_404s_for_unknown_filename(tmp_path, monkeypatch):
    cli_dist = tmp_path / "cli"
    cli_dist.mkdir()
    monkeypatch.setattr(builder_api, "CLI_DIST", cli_dist)
    assert client.get("/dl/cli/does-not-exist").status_code == 404
