from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    origin_server: str | None = None
    origin_tool: str | None = None


@dataclass(frozen=True)
class ChildServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_vars: tuple[str, ...] = ()
    startup_timeout_sec: float = 10.0
    tool_timeout_sec: float = 60.0
    wire_mode: str = "auto"
    wire_probe_modes: tuple[str, ...] = ("framed", "ndjson")
    wire_probe_timeout_sec: float = 5.0
    description: str = ""
    source: str = "config"
    enabled: bool = True
    template: bool = False

    @property
    def startable(self) -> bool:
        return self.enabled and not self.template and bool(self.command)

    @property
    def start_block_reason(self) -> str | None:
        if self.template:
            return "template"
        if not self.enabled:
            return "disabled"
        if not self.command:
            return "missing_command"
        return None


@dataclass(frozen=True)
class GatewayConfig:
    servers: dict[str, ChildServerConfig]
    dynamic_tools: bool = True
    cache_path: Path | None = None
    log_file: Path | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
