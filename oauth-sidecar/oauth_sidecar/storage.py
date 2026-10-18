"""Restrictive file-backed storage for MCP OAuth credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken, ProtectedResourceMetadata
from pydantic import ValidationError


class StorageError(RuntimeError):
    """Stored credentials could not be safely read or written."""


def default_storage_path(name: str) -> Path:
    return Path.home() / ".config" / "harness-oauth" / f"{name}.json"


def _ensure_private_directory(directory: Path) -> None:
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        metadata = os.lstat(directory)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise StorageError("Credential storage directory is not a real directory.")
        os.chmod(directory, 0o700)
    except StorageError:
        raise
    except OSError as error:
        raise StorageError("Credential storage directory could not be secured.") from error


def write_private_text(path: Path, text: str, *, create_parent: bool = False) -> None:
    """Atomically replace *path* with a file created as mode 0600."""
    if create_parent:
        _ensure_private_directory(path.parent)
    elif not path.parent.is_dir():
        raise OSError("Output directory does not exist.")

    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class FileTokenStorage(TokenStorage):
    """Store one server's OAuth token and registered client in one JSON file."""

    def __init__(self, path: Path):
        self.path = path

    def _read_document(self) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise StorageError("Credential storage file could not be opened safely.") from error

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise StorageError("Credential storage path is not a regular file.")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                document = json.load(handle)
        except StorageError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StorageError("Credential storage file is invalid or unreadable.") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if not isinstance(document, dict):
            raise StorageError("Credential storage file must contain a JSON object.")
        return document

    def _write_document(self, document: dict[str, Any]) -> None:
        serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
        try:
            write_private_text(self.path, serialized, create_parent=True)
        except StorageError:
            raise
        except OSError as error:
            raise StorageError("Credential storage file could not be written.") from error

    async def get_tokens(self) -> OAuthToken | None:
        value = self._read_document().get("tokens")
        if value is None:
            return None
        try:
            return OAuthToken.model_validate(value)
        except ValidationError as error:
            raise StorageError("Stored OAuth token data is invalid.") from error

    async def set_tokens(self, tokens: OAuthToken) -> None:
        document = self._read_document()
        document["tokens"] = tokens.model_dump(mode="json")
        self._write_document(document)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self._read_document().get("client_info")
        if value is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(value)
        except ValidationError as error:
            raise StorageError("Stored OAuth client data is invalid.") from error

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        document = self._read_document()
        document["client_info"] = client_info.model_dump(mode="json")
        self._write_document(document)

    async def get_discovery_metadata(self) -> tuple[OAuthMetadata | None, ProtectedResourceMetadata | None]:
        """Load SDK discovery results needed by a later refresh process."""
        document = self._read_document()
        oauth_value = document.get("oauth_metadata")
        resource_value = document.get("protected_resource_metadata")
        try:
            oauth_metadata = OAuthMetadata.model_validate(oauth_value) if oauth_value is not None else None
            resource_metadata = (
                ProtectedResourceMetadata.model_validate(resource_value) if resource_value is not None else None
            )
        except ValidationError as error:
            raise StorageError("Stored OAuth discovery data is invalid.") from error
        return oauth_metadata, resource_metadata

    async def set_discovery_metadata(
        self,
        oauth_metadata: OAuthMetadata | None,
        resource_metadata: ProtectedResourceMetadata | None,
    ) -> None:
        """Persist the SDK models rather than reimplementing OAuth discovery."""
        document = self._read_document()
        if oauth_metadata is not None:
            document["oauth_metadata"] = oauth_metadata.model_dump(mode="json")
        if resource_metadata is not None:
            document["protected_resource_metadata"] = resource_metadata.model_dump(mode="json")
        self._write_document(document)
