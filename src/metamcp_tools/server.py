from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cache import load_tool_cache, save_tool_cache
from .child import ChildRegistry, dynamic_tool_name
from .config import (
    load_gateway_config,
    server_from_table,
    validate_gateway_config,
    validate_registration_env,
    write_dynamic_server_config,
)
from .rpc import RPCError, STDIO, StreamPeer, error_response, success_response
from .types import ChildServerConfig, GatewayConfig, ToolDefinition

SERVER_NAME = "metamcp-tools"
PROTOCOL_VERSION = "2024-11-05"
LOGGER = logging.getLogger(SERVER_NAME)


class MetaMCPServer:
    def __init__(self, config: GatewayConfig, peer: StreamPeer = STDIO) -> None:
        self.config = config
        self.peer = peer
        self.initialized = False
        self.registry = ChildRegistry(config.servers)
        load_tool_cache(config.cache_path, self.registry)
        self.base_tools = self._build_base_tools()
        self.pending_tools_changed = False

    def run(self) -> int:
        while True:
            try:
                message, wire_mode = self.peer.read(timeout=None)
            except RPCError as exc:
                self.peer.write(error_response(None, exc.code, str(exc), exc.data))
                continue
            if message is None:
                self.registry.stop_all()
                return 0
            response = self.handle_message(message)
            if response is not None:
                try:
                    self.peer.write(response, wire_mode)
                except BrokenPipeError:
                    self.registry.stop_all()
                    return 0
                if self.pending_tools_changed:
                    self.pending_tools_changed = False
                    try:
                        self.peer.write({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}, wire_mode)
                    except BrokenPipeError:
                        self.registry.stop_all()
                        return 0

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return error_response(request_id, -32602, "params must be an object")

        if method in {"initialized", "notifications/initialized"}:
            return None
        if method == "initialize":
            self.initialized = True
            return success_response(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                    "instructions": "Use metamcp_explain_capabilities, metamcp_catalog, metamcp_start, and metamcp_call to discover and run child MCP servers on demand. Dynamic registration tools are available only when enabled in config.",
                },
            )

        if request_id is None:
            return None

        try:
            if method in {"tools/list", "list_tools"}:
                return success_response(request_id, {"tools": self._list_tools()})
            if method in {"tools/call", "call_tool"}:
                return success_response(request_id, self._call_tool(params))
            if method in {"resources/list", "list_resources"}:
                return success_response(request_id, {"resources": []})
            if method in {"resources/templates/list", "list_resource_templates"}:
                return success_response(request_id, {"resourceTemplates": []})
            if method in {"prompts/list", "list_prompts"}:
                return success_response(request_id, {"prompts": []})
            return error_response(request_id, -32601, f"Unknown method: {method}")
        except KeyError as exc:
            return error_response(request_id, -32602, str(exc))
        except Exception as exc:
            LOGGER.exception("tool_or_method_failed method=%s", method)
            return error_response(request_id, -32000, str(exc))

    def _build_base_tools(self) -> dict[str, ToolDefinition]:
        tools = {
            "metamcp_catalog": ToolDefinition(
                name="metamcp_catalog",
                description="List configured child MCP servers and cached tool metadata without exposing secrets.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "include_tools": {"type": "boolean", "description": "Include cached child tool names and descriptions.", "default": True}
                    },
                },
            ),
            "metamcp_explain_capabilities": ToolDefinition(
                name="metamcp_explain_capabilities",
                description="Explain the currently loaded gateway capabilities, config-derived facts, and safe discovery next steps without starting child MCPs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "include_servers": {"type": "boolean", "description": "Include concise per-child server status.", "default": True}
                    },
                },
            ),
            "metamcp_search": ToolDefinition(
                name="metamcp_search",
                description="Search configured child servers and cached tool metadata.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    },
                    "required": ["query"],
                },
            ),
            "metamcp_start": ToolDefinition(
                name="metamcp_start",
                description="Start one allowed child MCP server and cache its tools/list result.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string"},
                        "refresh_tools": {"type": "boolean", "default": True},
                    },
                    "required": ["server_name"],
                },
            ),
            "metamcp_validate_config": ToolDefinition(
                name="metamcp_validate_config",
                description="Validate loaded gateway config without launching child MCP servers.",
                input_schema={"type": "object", "properties": {}},
            ),
            "metamcp_call": ToolDefinition(
                name="metamcp_call",
                description="Call a child MCP tool through the stable gateway proxy.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string"},
                        "tool_name": {"type": "string"},
                        "arguments": {"type": "object", "default": {}},
                    },
                    "required": ["server_name", "tool_name"],
                },
            ),
            "metamcp_stop": ToolDefinition(
                name="metamcp_stop",
                description="Stop one child MCP server, or all children when all=true.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string"},
                        "all": {"type": "boolean", "default": False},
                        "kill": {"type": "boolean", "default": False},
                    },
                },
            ),
            "metamcp_status": ToolDefinition(
                name="metamcp_status",
                description="Return lifecycle status for one child MCP server or every configured child.",
                input_schema={
                    "type": "object",
                    "properties": {"server_name": {"type": "string"}},
                },
            ),
        }
        if self.config.allow_dynamic_registration:
            tools["metamcp_register_server"] = ToolDefinition(
                name="metamcp_register_server",
                description="Register a stdio child MCP server for this session or persist it into the dynamic config directory.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string"},
                        "description": {"type": "string", "default": ""},
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}, "default": []},
                        "cwd": {"type": "string"},
                        "env": {"type": "object", "additionalProperties": {"type": "string"}, "default": {}},
                        "env_vars": {"type": "array", "items": {"type": "string"}, "default": []},
                        "startup_timeout_sec": {"type": "number", "minimum": 0.1, "default": 10},
                        "tool_timeout_sec": {"type": "number", "minimum": 0.1, "default": 60},
                        "wire_mode": {"type": "string", "enum": ["auto", "framed", "ndjson"], "default": "auto"},
                        "wire_probe_modes": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["framed", "ndjson"]},
                            "default": ["framed", "ndjson"],
                        },
                        "wire_probe_timeout_sec": {"type": "number", "minimum": 0.1, "default": 5},
                        "persistence": {"type": "string", "enum": ["session", "config"], "default": "session"},
                        "replace": {"type": "boolean", "default": False},
                        "allow_inline_secrets": {"type": "boolean", "default": False},
                        "start": {"type": "boolean", "default": False},
                        "refresh_tools": {"type": "boolean", "default": True},
                    },
                    "required": ["server_name", "command"],
                },
            )
            tools["metamcp_unregister_server"] = ToolDefinition(
                name="metamcp_unregister_server",
                description="Unregister a server created through dynamic registration and optionally delete its generated config file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string"},
                        "delete_config": {"type": "boolean", "default": True},
                        "kill": {"type": "boolean", "default": False},
                    },
                    "required": ["server_name"],
                },
            )
        return tools

    def _list_tools(self) -> list[dict[str, Any]]:
        tools = [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
            for tool in self.base_tools.values()
        ]
        for session in self.registry.sessions.values():
            if not session.config.startable and not session.running:
                continue
            for child_name, child_tool in sorted(session.tools.items()):
                schema = child_tool.get("inputSchema") or child_tool.get("input_schema") or {"type": "object", "properties": {}}
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                tools.append(
                    {
                        "name": dynamic_tool_name(session.config.name, child_name),
                        "description": f"[{session.config.name}] {child_tool.get('description') or child_name}",
                        "inputSchema": schema,
                    }
                )
        return tools

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call requires a tool name")
        if not isinstance(arguments, dict):
            raise ValueError("tools/call arguments must be an object")

        if name in self.base_tools:
            return self._call_base_tool(name, arguments)
        for session in self.registry.sessions.values():
            if not session.config.startable and not session.running:
                continue
            for child_tool_name in session.tools:
                if dynamic_tool_name(session.config.name, child_tool_name) == name:
                    return session.call_tool(child_tool_name, arguments)
        raise KeyError(f"Unknown tool {name!r}")

    def _call_base_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "metamcp_catalog": self._tool_catalog,
            "metamcp_explain_capabilities": self._tool_explain_capabilities,
            "metamcp_search": self._tool_search,
            "metamcp_start": self._tool_start,
            "metamcp_validate_config": self._tool_validate_config,
            "metamcp_call": self._tool_call,
            "metamcp_stop": self._tool_stop,
            "metamcp_status": self._tool_status,
        }
        if self.config.allow_dynamic_registration:
            handlers["metamcp_register_server"] = self._tool_register_server
            handlers["metamcp_unregister_server"] = self._tool_unregister_server
        return handlers[name](arguments)

    def _tool_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return json_content(self.registry.catalog(include_tools=bool(arguments.get("include_tools", True))))

    def _tool_explain_capabilities(self, arguments: dict[str, Any]) -> dict[str, Any]:
        include_servers = bool(arguments.get("include_servers", True))
        return json_content(self._capabilities_explanation(include_servers=include_servers))

    def _capabilities_explanation(self, *, include_servers: bool = True) -> dict[str, Any]:
        sessions = list(self.registry.sessions.values())
        config_report = validate_gateway_config(self.config)
        running_count = sum(1 for session in sessions if session.running)
        cached_tool_count = sum(len(session.tools) for session in sessions)
        dynamic_registration_count = sum(1 for session in sessions if session.config.dynamic_registration)
        startable_not_running = [session for session in sessions if session.config.startable and not session.running]
        not_startable = [session for session in sessions if not session.config.startable]
        auto_wire_count = sum(1 for session in sessions if session.config.wire_mode == "auto")
        summary = {
            **config_report["summary"],
            "running_count": running_count,
            "not_startable_count": len(not_startable),
            "cached_tool_count": cached_tool_count,
            "dynamic_registered_count": dynamic_registration_count,
            "base_tool_count": len(self.base_tools),
            "auto_wire_count": auto_wire_count,
        }

        result: dict[str, Any] = {
            "tool": "metamcp_explain_capabilities",
            "read_only": True,
            "summary": summary,
            "phrases": self._capability_phrases(summary=summary, config_ok=bool(config_report["ok"])),
            "operational_notes": self._capability_operational_notes(config_report=config_report, summary=summary),
            "recommended_next_steps": self._capability_next_steps(config_report=config_report, startable_not_running=startable_not_running),
        }
        if include_servers:
            result["servers"] = [self._capability_server_entry(session) for session in sessions]
        return result

    def _capability_phrases(self, *, summary: dict[str, Any], config_ok: bool) -> list[str]:
        server_count = int(summary["server_count"])
        startable_count = int(summary["startable_count"])
        running_count = int(summary["running_count"])
        cached_tool_count = int(summary["cached_tool_count"])
        phrases: list[str] = []
        if server_count == 0:
            phrases.append("No child MCP servers are configured in the currently loaded gateway config.")
        else:
            phrases.append(f"{server_count} child MCP server(s) are configured; {startable_count} are startable.")
        if running_count:
            phrases.append(f"{running_count} child MCP server(s) are already running.")
        else:
            phrases.append("No child MCP servers are running; this explanation did not start any child process.")
        if cached_tool_count:
            phrases.append(f"{cached_tool_count} child tool(s) are known from a running child or cached tool metadata.")
        else:
            phrases.append("No child tool metadata is known yet; start a child MCP to discover its tools.")
        if self.config.allow_dynamic_registration:
            phrases.append("Dynamic child MCP registration is enabled for this gateway instance.")
        else:
            phrases.append("Dynamic child MCP registration is disabled; configured children come from static config/imports.")
        if int(summary["template_count"]):
            phrases.append(f"{summary['template_count']} configured child MCP server(s) are templates and cannot be started directly.")
        if int(summary["disabled_count"]):
            phrases.append(f"{summary['disabled_count']} configured child MCP server(s) are disabled.")
        if not config_ok:
            phrases.append("The loaded config has diagnostics that should be reviewed before relying on the gateway.")
        return phrases

    def _capability_operational_notes(self, *, config_report: dict[str, Any], summary: dict[str, Any]) -> list[str]:
        notes = [
            "This tool is read-only: it does not start, stop, register, unregister, or call child MCP servers.",
            "Child MCP servers are explicit allowlist entries loaded from config files, include directories, dynamic config, or approved Codex imports.",
            "Environment values are never returned; only environment key names are listed for operator context.",
            "The stable proxy path is metamcp_call; discovered child tools may also be published as namespaced dynamic tools after tools/list metadata is known.",
        ]
        if int(summary["auto_wire_count"]):
            notes.append("At least one child uses wire_mode=auto, so startup probes Content-Length JSON-RPC framing and NDJSON in the configured order.")
        if self.config.cache_path:
            notes.append(f"Tool metadata cache path: {self.config.cache_path}")
        else:
            notes.append("No tool metadata cache path is configured.")
        if self.config.allow_dynamic_registration:
            notes.append(f"Dynamic registration config directory: {self.config.dynamic_registration_dir}")
        else:
            notes.append("Dynamic registration tools are hidden because allow_dynamic_registration is false.")
        warnings = config_report["diagnostics"].get("warnings") or []
        if warnings:
            notes.append(f"{len(warnings)} config diagnostic warning(s) are present; call metamcp_validate_config for full details.")
        return notes

    def _capability_next_steps(self, *, config_report: dict[str, Any], startable_not_running: list[Any]) -> list[str]:
        steps: list[str] = []
        if not config_report["ok"]:
            steps.append("Call metamcp_validate_config and fix missing include paths or duplicate server definitions before depending on this gateway.")
        if not self.registry.sessions:
            steps.append("Add a child MCP under [servers] or include a conf.d/*.toml file, then restart the gateway.")
            if self.config.allow_dynamic_registration:
                steps.append("Use metamcp_register_server to add a session or persisted config child MCP at runtime.")
            return steps
        steps.append("Call metamcp_catalog for the full loaded child list and cached child tool metadata.")
        if any(session.tools for session in self.registry.sessions.values()):
            steps.append("Call metamcp_search when you know the capability you need but not which child MCP owns it.")
        if startable_not_running:
            names = ", ".join(session.config.name for session in startable_not_running[:3])
            suffix = "" if len(startable_not_running) <= 3 else f", plus {len(startable_not_running) - 3} more"
            steps.append(f"Start useful child MCPs with metamcp_start; currently startable and idle: {names}{suffix}.")
        if self.config.allow_dynamic_registration:
            steps.append("Use metamcp_register_server only for deliberate runtime additions; prefer env_vars for secrets.")
        else:
            steps.append("Edit config files and restart the gateway to add children; runtime dynamic registration is disabled.")
        return steps

    def _capability_server_entry(self, session: Any) -> dict[str, Any]:
        config = session.config
        if session.running:
            next_action = "call metamcp_call or the dynamic child tool"
        elif config.startable:
            next_action = "call metamcp_start to launch and refresh tool metadata"
        else:
            next_action = f"not startable: {config.start_block_reason}"
        return {
            "name": config.name,
            "description": config.description,
            "startable": config.startable,
            "reason": config.start_block_reason,
            "running": session.running,
            "tool_count": len(session.tools),
            "source": config.source,
            "wire_mode": config.wire_mode,
            "active_wire_mode": session.active_wire_mode,
            "dynamic_registration": config.dynamic_registration,
            "dynamic_persistence": config.dynamic_persistence,
            "env_keys": sorted(set(config.env) | set(config.env_vars)),
            "next_action": next_action,
            "tools": [
                {
                    "name": name,
                    "dynamic_name": dynamic_tool_name(config.name, name),
                    "description": str(tool.get("description") or ""),
                }
                for name, tool in sorted(session.tools.items())
            ],
        }

    def _tool_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        max_results = _clamp_int(arguments.get("max_results"), 20, 1, 100)
        return json_content(self.registry.search(str(arguments.get("query") or ""), max_results=max_results))

    def _tool_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server_name = _required_str(arguments, "server_name")
        refresh_tools = bool(arguments.get("refresh_tools", True))
        session = self.registry.require(server_name)
        if not session.config.startable:
            return tool_error_content(
                {
                    "server_name": session.config.name,
                    "reason": session.config.start_block_reason,
                    "message": f"Server {session.config.name!r} is not startable",
                }
            )
        status = session.start(refresh_tools=refresh_tools)
        save_tool_cache(self.config.cache_path, self.registry)
        if session.tools:
            self.pending_tools_changed = True
        return json_content({"status": status, "tool_publication": "live"})

    def _tool_validate_config(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return json_content(validate_gateway_config(self.config))

    def _tool_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server_name = _required_str(arguments, "server_name")
        tool_name = _required_str(arguments, "tool_name")
        child_args = arguments.get("arguments") or {}
        if not isinstance(child_args, dict):
            raise ValueError("arguments must be an object")
        session = self.registry.require(server_name)
        if not session.config.startable and not session.running:
            return tool_error_content(
                {
                    "server_name": session.config.name,
                    "reason": session.config.start_block_reason,
                    "message": f"Server {session.config.name!r} is not startable",
                }
            )
        return session.call_tool(tool_name, child_args)

    def _tool_register_server(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            server = self._registered_server_from_args(arguments)
            replace_existing = bool(arguments.get("replace", False))
            persistence = str(arguments.get("persistence") or "session").strip().lower()
            if persistence not in {"session", "config"}:
                raise ValueError("persistence must be 'session' or 'config'")
            if server.name in self.registry.sessions and not replace_existing:
                raise ValueError(f"Child MCP server {server.name!r} already exists")
            existing = self.registry.sessions.get(server.name)
            if existing is not None and replace_existing and not existing.config.dynamic_registration:
                raise ValueError(f"Refusing to replace static child MCP server {server.name!r}; edit its config file instead")
            if persistence == "config":
                if self.config.dynamic_registration_dir is None:
                    raise ValueError("dynamic_registration_dir is not configured")
                target = write_dynamic_server_config(self.config.dynamic_registration_dir, server, replace=replace_existing)
                server = dataclass_replace(
                    server,
                    source=f"{target}:servers",
                    source_path=target,
                    dynamic_persistence="config",
                )
            session = self.registry.add(server, replace=replace_existing)
            status = session.status()
            started = False
            if bool(arguments.get("start", False)):
                status = session.start(refresh_tools=bool(arguments.get("refresh_tools", True)))
                save_tool_cache(self.config.cache_path, self.registry)
                started = True
            self.pending_tools_changed = True
            return json_content(
                {
                    "registered": True,
                    "server_name": server.name,
                    "persistence": persistence,
                    "config_path": str(server.source_path) if server.source_path else None,
                    "started": started,
                    "status": status,
                }
            )
        except Exception as exc:
            return tool_error_content({"registered": False, "message": str(exc)})

    def _registered_server_from_args(self, arguments: dict[str, Any]) -> ChildServerConfig:
        server_name = _required_str(arguments, "server_name")
        raw = {
            "description": str(arguments.get("description") or ""),
            "command": _required_str(arguments, "command"),
            "args": _string_list(arguments.get("args", [])),
            "env": arguments.get("env") or {},
            "env_vars": _string_list(arguments.get("env_vars", [])),
            "startup_timeout_sec": arguments.get("startup_timeout_sec", 10),
            "tool_timeout_sec": arguments.get("tool_timeout_sec", 60),
            "wire_mode": arguments.get("wire_mode", "auto"),
            "wire_probe_modes": _string_list(arguments.get("wire_probe_modes", ["framed", "ndjson"])),
            "wire_probe_timeout_sec": arguments.get("wire_probe_timeout_sec", 5),
        }
        if "cwd" in arguments and arguments.get("cwd"):
            raw["cwd"] = arguments["cwd"]
        validate_registration_env(raw["env"], allow_inline_secrets=bool(arguments.get("allow_inline_secrets", False)))
        persistence = str(arguments.get("persistence") or "session").strip().lower()
        return server_from_table(
            server_name,
            raw,
            source=f"dynamic:{persistence}",
            base=Path.cwd(),
            dynamic_registration=True,
            dynamic_persistence=persistence,
        )

    def _tool_unregister_server(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server_name = _required_str(arguments, "server_name")
        delete_config = bool(arguments.get("delete_config", True))
        kill = bool(arguments.get("kill", False))
        try:
            config = self.registry.remove_dynamic(server_name, kill=kill)
            deleted_path = None
            if delete_config and config.dynamic_persistence == "config" and config.source_path is not None:
                if not _path_is_within(config.source_path, self.config.dynamic_registration_dir):
                    raise ValueError(f"Refusing to delete config outside dynamic_registration_dir: {config.source_path}")
                if config.source_path.exists():
                    config.source_path.unlink()
                    deleted_path = str(config.source_path)
            self.pending_tools_changed = True
            return json_content(
                {
                    "unregistered": True,
                    "server_name": server_name,
                    "deleted_config": deleted_path,
                }
            )
        except Exception as exc:
            return tool_error_content({"unregistered": False, "server_name": server_name, "message": str(exc)})

    def _tool_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        stop_all = bool(arguments.get("all", False))
        kill = bool(arguments.get("kill", False))
        if stop_all:
            if kill:
                for session in self.registry.sessions.values():
                    session.stop(kill=True)
                return json_content(self.registry.statuses())
            return json_content(self.registry.stop_all())
        server_name = _required_str(arguments, "server_name")
        return json_content(self.registry.require(server_name).stop(kill=kill))

    def _tool_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server_name = arguments.get("server_name")
        if server_name:
            return json_content(self.registry.require(str(server_name)).status())
        return json_content(self.registry.statuses())


def json_content(data: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=True, sort_keys=True)}]}


def tool_error_content(data: Any) -> dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=True, sort_keys=True)}],
    }


def _required_str(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError("expected a list of strings")
    return [str(item) for item in value]


def _path_is_within(path: Path | None, directory: Path | None) -> bool:
    if path is None or directory is None:
        return False
    try:
        path.expanduser().resolve().relative_to(directory.expanduser().resolve())
    except ValueError:
        return False
    return True


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def configure_logging(log_file: Path | None = None, level: str = "INFO") -> None:
    logging.basicConfig(
        filename=str(log_file) if log_file else None,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex-first on-demand MCP gateway.")
    parser.add_argument("--config", type=Path, help="Gateway config TOML path.")
    parser.add_argument("--codex-config", type=Path, help="Import allowed servers from a Codex config.toml.")
    parser.add_argument("--allow-server", action="append", default=[], help="Allow a Codex mcp_servers entry by name. Repeatable.")
    parser.add_argument("--no-dynamic-tools", action="store_true", help="Deprecated no-op; child tools are always published dynamically.")
    parser.add_argument("--log-file", type=Path, help="Write logs to this path instead of stderr.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")
    parser.add_argument("--probe", action="store_true", help="Parse config and print a redacted server summary, then exit.")
    parser.add_argument("--validate", action="store_true", help="Validate config and print diagnostics without launching children.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = load_gateway_config(
        config_path=args.config,
        codex_config_path=args.codex_config,
        allow_servers=args.allow_server,
        no_dynamic_tools=args.no_dynamic_tools,
        log_file=args.log_file,
    )
    configure_logging(config.log_file, args.log_level)
    server = MetaMCPServer(config)
    if args.validate:
        print(json.dumps(validate_gateway_config(config), indent=2, ensure_ascii=True, sort_keys=True))
        return 0
    if args.probe:
        print(json.dumps(server.registry.catalog(include_tools=False), indent=2, ensure_ascii=True, sort_keys=True))
        return 0
    return server.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
