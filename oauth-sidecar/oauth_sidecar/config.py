"""Configuration loading and validation for the OAuth sidecar."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


DEFAULT_REDIRECT_PORT = 8765
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ConfigError(ValueError):
    """A server configuration is absent or invalid."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    server_url: str
    redirect_port: int = DEFAULT_REDIRECT_PORT


def _required_string(document: dict[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Configuration field '{field}' is required and must be a non-empty string.")
    return value.strip()


def load_config(path: Path) -> ServerConfig:
    """Load a small, deliberately strict server JSON document."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError("Configuration is not valid JSON.") from error
    except OSError as error:
        raise ConfigError("Configuration file could not be read.") from error

    if not isinstance(document, dict):
        raise ConfigError("Configuration must be a JSON object.")

    name = _required_string(document, "name")
    if not _SAFE_NAME.fullmatch(name):
        raise ConfigError(
            "Configuration field 'name' must start with a letter or digit and contain only letters, digits, '.', '_', or '-'."
        )

    server_url = _required_string(document, "server_url")
    parsed_url = urlsplit(server_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ConfigError("Configuration field 'server_url' must be an absolute HTTP or HTTPS URL.")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ConfigError("Configuration field 'server_url' must not contain credentials.")

    redirect_port = document.get("redirect_port", DEFAULT_REDIRECT_PORT)
    if type(redirect_port) is not int or not 1 <= redirect_port <= 65535:
        raise ConfigError("Configuration field 'redirect_port' must be an integer from 1 through 65535.")

    return ServerConfig(name=name, server_url=server_url, redirect_port=redirect_port)
