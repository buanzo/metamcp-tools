from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .child import ChildRegistry


def load_tool_cache(path: Path | None, registry: ChildRegistry) -> None:
    if path is None or not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    servers = payload.get("servers", {})
    if not isinstance(servers, dict):
        return
    for name, metadata in servers.items():
        session = registry.sessions.get(name)
        if session is None or not isinstance(metadata, dict):
            continue
        tools = metadata.get("tools", {})
        if not isinstance(tools, dict):
            continue
        parsed = {
            str(tool_name): tool
            for tool_name, tool in tools.items()
            if isinstance(tool_name, str) and isinstance(tool, dict)
        }
        if parsed:
            session.tools = parsed


def save_tool_cache(path: Path | None, registry: ChildRegistry) -> None:
    if path is None:
        return
    payload: dict[str, Any] = {"version": 1, "updated_at": time.time(), "servers": {}}
    for name, session in registry.sessions.items():
        if not session.tools:
            continue
        payload["servers"][name] = {
            "source": session.config.source,
            "tool_count": len(session.tools),
            "tools": session.tools,
            "updated_at": time.time(),
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    except OSError:
        return

