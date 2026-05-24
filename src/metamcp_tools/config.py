from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .security import is_secret_key
from .types import ChildServerConfig, GatewayConfig


DEFAULT_DENY_SERVERS = {"metamcp-tools", "metamcp_tools", "metamcp"}
WIRE_MODES = {"auto", "framed", "ndjson"}
WIRE_PROBE_MODES = {"framed", "ndjson"}
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def expand_path(value: str | Path | None, base: Path | None = None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def load_gateway_config(
    config_path: Path | None = None,
    codex_config_path: Path | None = None,
    allow_servers: list[str] | None = None,
    no_dynamic_tools: bool = False,
    log_file: Path | None = None,
) -> GatewayConfig:
    config_data: dict[str, Any] = {}
    config_base = Path.cwd()
    diagnostics: dict[str, Any] = {
        "config_path": None,
        "real_config_path": None,
        "config_base": str(config_base),
        "include_dirs": [],
        "included_files": [],
        "missing_include_dirs": [],
        "missing_include_files": [],
        "duplicate_servers": [],
        "warnings": [],
    }
    if config_path is not None:
        config_base = config_path.expanduser().resolve().parent
        diagnostics["config_path"] = str(config_path)
        diagnostics["real_config_path"] = str(config_path.expanduser().resolve())
        diagnostics["config_base"] = str(config_base)
        with config_path.expanduser().open("rb") as handle:
            config_data = tomllib.load(handle)

    gateway_data = config_data.get("gateway", {})
    if not isinstance(gateway_data, dict):
        raise ValueError("[gateway] must be a TOML table")

    dynamic_tools = True
    if "dynamic_tools" in gateway_data:
        diagnostics["warnings"].append(
            "[gateway].dynamic_tools is deprecated and ignored; child tools are always published dynamically."
        )
    if no_dynamic_tools:
        diagnostics["warnings"].append(
            "--no-dynamic-tools is deprecated and ignored; child tools are always published dynamically."
        )
    allow_dynamic_registration = bool(gateway_data.get("allow_dynamic_registration", False))
    dynamic_registration_dir = expand_path(gateway_data.get("dynamic_registration_dir", "dynamic.d"), config_base)
    cache_path = expand_path(gateway_data.get("cache_path"), config_base)
    resolved_log_file = log_file or expand_path(gateway_data.get("log_file"), config_base)

    servers: dict[str, ChildServerConfig] = {}
    include_sources = _load_include_sources(config_data, config_base, diagnostics)
    dedicated_servers = config_data.get("servers", {})
    if dedicated_servers:
        if not isinstance(dedicated_servers, dict):
            raise ValueError("[servers] must be a TOML table")
        for name, raw in dedicated_servers.items():
            _add_server(
                servers,
                server_from_table(
                    name,
                    raw,
                    source=f"{config_path or '<memory>'}:servers",
                    base=config_base,
                    source_path=config_path,
                ),
                diagnostics,
            )
    for include_path, include_data in include_sources:
        include_base = include_path.resolve().parent
        include_servers = include_data.get("servers", {})
        if include_servers:
            if not isinstance(include_servers, dict):
                raise ValueError(f"{include_path}: [servers] must be a TOML table")
            for name, raw in include_servers.items():
                _add_server(
                    servers,
                    server_from_table(
                        name,
                        raw,
                        source=f"{include_path}:servers",
                        base=include_base,
                        source_path=include_path,
                    ),
                    diagnostics,
                )

    import_data = config_data.get("import", {})
    codex_data = import_data.get("codex", {}) if isinstance(import_data, dict) else {}
    config_allow = _string_list(codex_data.get("allow_servers", [])) if isinstance(codex_data, dict) else []
    config_deny = set(_string_list(codex_data.get("deny_servers", []))) if isinstance(codex_data, dict) else set()
    cli_allow = allow_servers or []
    imported_allow = set(config_allow) | set(cli_allow)

    configured_codex_path = None
    if isinstance(codex_data, dict):
        configured_codex_path = expand_path(codex_data.get("path"), config_base)
    codex_path = codex_config_path or configured_codex_path
    if codex_path is not None and imported_allow:
        for name, server in load_codex_servers(codex_path, imported_allow, config_deny | DEFAULT_DENY_SERVERS).items():
            if name in servers:
                diagnostics["warnings"].append(f"Skipped Codex import for duplicate server {name!r}; explicit config wins")
            else:
                servers[name] = server

    return GatewayConfig(
        servers=servers,
        dynamic_tools=dynamic_tools,
        allow_dynamic_registration=allow_dynamic_registration,
        dynamic_registration_dir=dynamic_registration_dir,
        cache_path=cache_path,
        log_file=resolved_log_file,
        diagnostics=diagnostics,
    )


def validate_gateway_config(config: GatewayConfig) -> dict[str, Any]:
    server_reports = []
    for server in config.servers.values():
        server_reports.append(
            {
                "name": server.name,
                "enabled": server.enabled,
                "template": server.template,
                "startable": server.startable,
                "reason": server.start_block_reason,
                "source": server.source,
                "description": server.description,
                "env_keys": sorted(set(server.env) | set(server.env_vars)),
                "has_cwd": server.cwd is not None,
                "arg_count": len(server.args),
                "wire_mode": server.wire_mode,
                "wire_probe_modes": list(server.wire_probe_modes),
                "wire_probe_timeout_sec": server.wire_probe_timeout_sec,
                "dynamic_registration": server.dynamic_registration,
                "dynamic_persistence": server.dynamic_persistence,
            }
        )
    return {
        "ok": (
            not config.diagnostics.get("missing_include_dirs")
            and not config.diagnostics.get("missing_include_files")
            and not config.diagnostics.get("duplicate_servers")
        ),
        "allow_dynamic_registration": config.allow_dynamic_registration,
        "dynamic_registration_dir": str(config.dynamic_registration_dir) if config.dynamic_registration_dir else None,
        "cache_path": str(config.cache_path) if config.cache_path else None,
        "log_file": str(config.log_file) if config.log_file else None,
        "diagnostics": config.diagnostics,
        "summary": {
            "server_count": len(server_reports),
            "startable_count": sum(1 for item in server_reports if item["startable"]),
            "disabled_count": sum(1 for item in server_reports if not item["enabled"]),
            "template_count": sum(1 for item in server_reports if item["template"]),
        },
        "servers": server_reports,
    }


def _add_server(
    servers: dict[str, ChildServerConfig],
    server: ChildServerConfig,
    diagnostics: dict[str, Any],
) -> None:
    previous = servers.get(server.name)
    if previous is not None:
        diagnostics["duplicate_servers"].append(
            {
                "name": server.name,
                "previous_source": previous.source,
                "override_source": server.source,
            }
        )
        diagnostics["warnings"].append(
            f"Server {server.name!r} from {server.source} overrides earlier definition from {previous.source}"
        )
    servers[server.name] = server


def _load_include_sources(
    config_data: dict[str, Any],
    config_base: Path,
    diagnostics: dict[str, Any],
) -> list[tuple[Path, dict[str, Any]]]:
    include_data = config_data.get("include", {})
    if not include_data:
        return []
    if not isinstance(include_data, dict):
        raise ValueError("[include] must be a TOML table")

    paths: list[Path] = []
    for item in _string_list(include_data.get("files", [])):
        path = expand_path(item, config_base)
        if path is not None:
            paths.append(path)
    for item in _string_list(include_data.get("dirs", [])):
        directory = expand_path(item, config_base)
        if directory is None:
            continue
        diagnostics["include_dirs"].append(str(directory))
        if not directory.exists():
            diagnostics["missing_include_dirs"].append(str(directory))
            diagnostics["warnings"].append(f"Include directory does not exist: {directory}")
            continue
        if not directory.is_dir():
            raise ValueError(f"include dir is not a directory: {directory}")
        paths.extend(sorted(directory.glob("*.toml")))

    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if not path.exists():
            diagnostics["missing_include_files"].append(str(path))
            diagnostics["warnings"].append(f"Include file does not exist: {path}")
            continue
        if not path.is_file():
            raise ValueError(f"include path is not a file: {path}")
        with path.open("rb") as handle:
            loaded.append((path, tomllib.load(handle)))
        diagnostics["included_files"].append(str(path))
    return loaded


def load_codex_servers(path: Path, allow_servers: set[str], deny_servers: set[str]) -> dict[str, ChildServerConfig]:
    with path.expanduser().open("rb") as handle:
        data = tomllib.load(handle)
    raw_servers = data.get("mcp_servers", {})
    if not isinstance(raw_servers, dict):
        return {}

    servers: dict[str, ChildServerConfig] = {}
    for name, raw in raw_servers.items():
        if name not in allow_servers or name in deny_servers:
            continue
        if not isinstance(raw, dict):
            continue
        if raw.get("enabled") is False:
            continue
        if raw.get("url") or raw.get("transport") == "remote_http_server":
            servers[name] = _remote_placeholder(name, raw, source=f"{path}:mcp_servers", source_path=path)
            continue
        if "command" not in raw:
            continue
        servers[name] = server_from_table(
            name,
            raw,
            source=f"{path}:mcp_servers",
            base=path.expanduser().resolve().parent,
            source_path=path,
        )
    return servers


def validate_server_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("server_name is required")
    normalized = name.strip()
    if not SERVER_NAME_RE.fullmatch(normalized):
        raise ValueError("server_name must use only letters, numbers, underscore, or hyphen, and start with a letter or number")
    return normalized


def server_from_table(
    name: str,
    raw: Any,
    source: str,
    base: Path,
    source_path: Path | None = None,
    dynamic_registration: bool | None = None,
    dynamic_persistence: str | None = None,
) -> ChildServerConfig:
    name = validate_server_name(str(name))
    if not isinstance(raw, dict):
        raise ValueError(f"Server {name!r} must be a table")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"Server {name!r} requires a non-empty command")
    args = tuple(str(item) for item in _string_list(raw.get("args", [])))
    env = {str(key): str(value) for key, value in (raw.get("env") or {}).items()}
    env_vars = tuple(_string_list(raw.get("env_vars", [])))
    cwd = expand_path(raw.get("cwd"), base)
    startup_timeout = _float(raw.get("startup_timeout_sec"), 10.0, minimum=0.1)
    tool_timeout = _float(raw.get("tool_timeout_sec"), _float(raw.get("timeout_sec"), 60.0, minimum=0.1), minimum=0.1)
    wire_mode = str(raw.get("wire_mode") or "auto").strip().lower()
    if wire_mode not in WIRE_MODES:
        raise ValueError(f"Server {name!r} wire_mode must be 'auto', 'framed', or 'ndjson'")
    wire_probe_modes = tuple(
        item.strip().lower()
        for item in _string_list(raw.get("wire_probe_modes", ["framed", "ndjson"]))
        if item.strip()
    )
    if not wire_probe_modes:
        raise ValueError(f"Server {name!r} wire_probe_modes cannot be empty")
    invalid_probe_modes = sorted(set(wire_probe_modes) - WIRE_PROBE_MODES)
    if invalid_probe_modes:
        raise ValueError(
            f"Server {name!r} wire_probe_modes contains unsupported mode(s): {', '.join(invalid_probe_modes)}"
        )
    wire_probe_timeout = _float(raw.get("wire_probe_timeout_sec"), 5.0, minimum=0.1)
    return ChildServerConfig(
        name=name,
        command=command,
        args=args,
        cwd=cwd,
        env=env,
        env_vars=env_vars,
        startup_timeout_sec=startup_timeout,
        tool_timeout_sec=tool_timeout,
        wire_mode=wire_mode,
        wire_probe_modes=wire_probe_modes,
        wire_probe_timeout_sec=wire_probe_timeout,
        description=str(raw.get("description") or ""),
        source=source,
        source_path=source_path.expanduser().resolve() if source_path is not None else None,
        enabled=raw.get("enabled") is not False,
        template=bool(raw.get("template", False)),
        dynamic_registration=bool(raw.get("dynamic_registration", False))
        if dynamic_registration is None
        else dynamic_registration,
        dynamic_persistence=dynamic_persistence or ("config" if raw.get("dynamic_registration") else None),
    )


def validate_registration_env(env: Any, allow_inline_secrets: bool = False) -> None:
    if not isinstance(env, dict):
        return
    if allow_inline_secrets:
        return
    secret_keys = sorted(str(key) for key in env if is_secret_key(str(key)))
    if secret_keys:
        raise ValueError(
            "inline env contains secret-like key(s); use env_vars instead or set allow_inline_secrets=true: "
            + ", ".join(secret_keys)
        )


def write_dynamic_server_config(directory: Path, server: ChildServerConfig, replace: bool = False) -> Path:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{server.name}.toml"
    target = (directory / filename).resolve()
    if target.parent != directory:
        raise ValueError("dynamic config path escaped dynamic_registration_dir")
    if target.exists() and not replace:
        raise FileExistsError(f"dynamic config already exists for {server.name!r}: {target}")
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(_server_to_toml(server), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _server_to_toml(server: ChildServerConfig) -> str:
    lines = [
        "# Generated by metamcp-tools dynamic registration.",
        "# Edit with care; metamcp_unregister_server may remove this file.",
        f"[servers.{server.name}]",
        "dynamic_registration = true",
        f"description = {_toml_string(server.description)}",
        f"command = {_toml_string(server.command)}",
        f"args = {_toml_array(server.args)}",
        f"startup_timeout_sec = {_toml_number(server.startup_timeout_sec)}",
        f"tool_timeout_sec = {_toml_number(server.tool_timeout_sec)}",
        f"wire_mode = {_toml_string(server.wire_mode)}",
        f"wire_probe_modes = {_toml_array(server.wire_probe_modes)}",
        f"wire_probe_timeout_sec = {_toml_number(server.wire_probe_timeout_sec)}",
    ]
    if server.cwd is not None:
        lines.append(f"cwd = {_toml_string(str(server.cwd))}")
    if server.env:
        items = ", ".join(f"{_toml_string(key)} = {_toml_string(value)}" for key, value in sorted(server.env.items()))
        lines.append(f"env = {{ {items} }}")
    if server.env_vars:
        lines.append(f"env_vars = {_toml_array(server.env_vars)}")
    lines.append("")
    return "\n".join(lines)


def _toml_string(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\b", "\\b").replace("\t", "\\t").replace("\n", "\\n").replace("\f", "\\f").replace("\r", "\\r")
    return f'"{text}"'


def _toml_array(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(_toml_string(item) for item in values) + "]"


def _toml_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(float(value))


def _remote_placeholder(name: str, raw: dict[str, Any], source: str, source_path: Path | None = None) -> ChildServerConfig:
    return ChildServerConfig(
        name=name,
        command="",
        args=(),
        description=str(raw.get("description") or "Remote HTTP MCP servers are listed but cannot be subprocess-started."),
        source=source,
        source_path=source_path.expanduser().resolve() if source_path is not None else None,
        enabled=False,
        template=bool(raw.get("template", False)),
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _float(value: Any, default: float, minimum: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)
