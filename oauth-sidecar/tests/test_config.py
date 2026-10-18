import json

import pytest

from oauth_sidecar.config import ConfigError, DEFAULT_REDIRECT_PORT, load_config


def test_config_defaults_redirect_port(tmp_path):
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"name": "atlassian", "server_url": "https://mcp.atlassian.com/v2/mcp"}))

    config = load_config(path)

    assert config.redirect_port == DEFAULT_REDIRECT_PORT


def test_config_missing_server_url_has_clear_error(tmp_path):
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"name": "atlassian"}))

    with pytest.raises(ConfigError, match="server_url.*required"):
        load_config(path)


def test_config_malformed_json_has_clear_error(tmp_path):
    path = tmp_path / "server.json"
    path.write_text("{not-json")

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)
