"""Versioned newline-delimited JSON tracing."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import IO, Any


TRACE_SCHEMA = "harness.trace.v1"


class TraceEmitter:
    def __init__(self, stream: IO[str], *, owned: bool = False):
        self.stream = stream
        self.owned = owned
        self.sequence = 0
        self._secrets: list[str] = []

    @classmethod
    def to_path(cls, path: Path) -> "TraceEmitter":
        return cls(path.open("w", encoding="utf-8", newline="\n"), owned=True)

    def emit(self, event: str, **data: Any) -> None:
        self.sequence += 1
        record = {
            "schema": TRACE_SCHEMA,
            "sequence": self.sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **self._redact(data),
        }
        self.stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def add_secret(self, value: str | None) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    def close(self) -> None:
        if self.owned:
            self.stream.close()

    def __enter__(self) -> "TraceEmitter":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
