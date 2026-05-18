from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any


SECRET_KEY_PARTS = (
    "api",
    "at_",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "key",
    "pass",
    "password",
    "secret",
    "session",
    "token",
)


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def redact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if is_secret_key(key):
        text = str(value)
        return f"<redacted:{stable_digest(text)}>"
    return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: redact_value(key, value) for key, value in sorted(values.items())}


def command_summary(command: str, args: Sequence[str]) -> dict[str, Any]:
    return {
        "command": command,
        "arg_count": len(args),
        "command_digest": stable_digest(" ".join([command, *args])),
    }

