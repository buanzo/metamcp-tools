from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .rpc import RPCError, StreamPeer
from .security import command_summary, redact_mapping
from .types import ChildServerConfig

LOGGER = logging.getLogger("metamcp-tools.child")
PROTOCOL_VERSION = "2024-11-05"


@dataclass
class ChildSession:
    config: ChildServerConfig
    process: subprocess.Popen[bytes] | None = None
    peer: StreamPeer | None = None
    request_id: int = 0
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_error: str | None = None
    started_at: float | None = None
    active_wire_mode: str | None = None
    wire_attempts: list[dict[str, Any]] = field(default_factory=list)
    restart_count: int = 0
    last_recovery: dict[str, Any] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, refresh_tools: bool = True) -> dict[str, Any]:
        with self.lock:
            if not self.config.startable:
                reason = self.config.start_block_reason or "not_startable"
                raise RuntimeError(f"Server {self.config.name!r} is not startable: {reason}")
            if self.running:
                if refresh_tools or not self.tools:
                    self.refresh_tools_with_recovery_locked()
                return self.status()
            if not self.config.command:
                raise RuntimeError(f"Server {self.config.name!r} has no subprocess command")

            self.wire_attempts = []
            env = os.environ.copy()
            for key in self.config.env_vars:
                if key in os.environ:
                    env[key] = os.environ[key]
            env.update(self.config.env)

            LOGGER.info("starting_child server=%s command=%s", self.config.name, command_summary(self.config.command, self.config.args))
            if self.config.wire_mode == "auto":
                return self._start_auto_locked(env, refresh_tools=refresh_tools)
            return self._start_explicit_locked(env, self.config.wire_mode, refresh_tools=refresh_tools)

    def _start_explicit_locked(self, env: dict[str, str], wire_mode: str, refresh_tools: bool) -> dict[str, Any]:
        attempt = {"mode": wire_mode, "ok": False, "timeout_sec": self.config.startup_timeout_sec}
        self.wire_attempts.append(attempt)
        self._spawn_process_locked(env)
        self.active_wire_mode = wire_mode
        try:
            self._initialize_locked(timeout=self.config.startup_timeout_sec)
            attempt["ok"] = True
            if refresh_tools:
                self.refresh_tools_locked()
        except Exception as exc:
            attempt["error"] = _brief_error(exc)
            self.stop_locked(kill=True)
            self.active_wire_mode = None
            raise
        return self.status()

    def _start_auto_locked(self, env: dict[str, str], refresh_tools: bool) -> dict[str, Any]:
        seen_modes: set[str] = set()
        probe_modes: list[str] = []
        for mode in self.config.wire_probe_modes:
            if mode in seen_modes:
                continue
            seen_modes.add(mode)
            probe_modes.append(mode)
        for wire_mode in probe_modes:
            attempt = {"mode": wire_mode, "ok": False, "timeout_sec": self.config.wire_probe_timeout_sec}
            self.wire_attempts.append(attempt)
            try:
                self._spawn_process_locked(env)
                self.active_wire_mode = wire_mode
                self._initialize_locked(timeout=self.config.wire_probe_timeout_sec)
            except Exception as exc:
                attempt["error"] = _brief_error(exc)
                stderr_preview = self.stop_locked(kill=True, collect_stderr=True)
                if stderr_preview:
                    attempt["stderr_preview"] = stderr_preview
                self.active_wire_mode = None
                continue

            attempt["ok"] = True
            self.last_error = None
            try:
                if refresh_tools:
                    self.refresh_tools_locked()
            except Exception:
                self.stop_locked(kill=True)
                raise
            return self.status()

        attempts = "; ".join(f"{item['mode']}: {item.get('error') or 'failed'}" for item in self.wire_attempts)
        message = f"Could not auto-detect wire_mode for {self.config.name!r}: {attempts}"
        self.last_error = message
        raise RuntimeError(message)

    def _spawn_process_locked(self, env: dict[str, str]) -> None:
        try:
            self.process = subprocess.Popen(
                [self.config.command, *self.config.args],
                cwd=str(self.config.cwd) if self.config.cwd else None,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.last_error = str(exc)
            raise RuntimeError(f"Failed to start {self.config.name}: {exc}") from exc

        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.peer = StreamPeer(self.process.stdout, self.process.stdin, default_wire_mode="framed")
        self.started_at = time.time()
        self.last_error = None

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            if not self.running:
                self.start(refresh_tools=True)
            if tool_name not in self.tools:
                self.refresh_tools_locked()
            if self.tools and tool_name not in self.tools:
                raise RuntimeError(f"Tool {tool_name!r} is not known for server {self.config.name!r}")
            response = self._request_locked(
                "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
                timeout=self.config.tool_timeout_sec,
            )
            if "error" in response:
                error = response["error"]
                raise RuntimeError(f"Child tool error: {error.get('message', error)}")
            result = response.get("result")
            if isinstance(result, dict):
                return result
            return {"content": [{"type": "text", "text": str(result)}]}

    def refresh_tools(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            if not self.running:
                self.start(refresh_tools=False)
            return self.refresh_tools_locked()

    def refresh_tools_locked(self) -> dict[str, dict[str, Any]]:
        response = self._request_locked("tools/list", {}, timeout=self.config.startup_timeout_sec)
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"Could not list tools for {self.config.name}: {error.get('message', error)}")
        result = response.get("result") or {}
        tools = result.get("tools", []) if isinstance(result, dict) else []
        parsed: dict[str, dict[str, Any]] = {}
        if isinstance(tools, list):
            for item in tools:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str) and name:
                    parsed[name] = item
        self.tools = parsed
        return self.tools

    def refresh_tools_with_recovery_locked(self) -> dict[str, dict[str, Any]]:
        try:
            return self.refresh_tools_locked()
        except Exception as exc:
            if not self._is_recoverable_refresh_failure(exc):
                raise
            return self._recover_after_refresh_failure_locked(exc)

    def _recover_after_refresh_failure_locked(self, exc: BaseException) -> dict[str, dict[str, Any]]:
        old_pid = self.process.pid if self.process is not None else None
        error = _brief_error(exc)
        stderr_preview = self.stop_locked(kill=True, collect_stderr=True)
        recovery: dict[str, Any] = {
            "ok": False,
            "reason": "refresh_transport_failure",
            "error": error,
            "old_pid": old_pid,
            "at": time.time(),
        }
        if stderr_preview:
            recovery["stderr_preview"] = stderr_preview
        self.restart_count += 1
        self.last_recovery = recovery
        LOGGER.warning("recovering_child_after_refresh_failure server=%s error=%s", self.config.name, error)
        try:
            self.start(refresh_tools=True)
        except Exception as restart_exc:
            recovery["restart_error"] = _brief_error(restart_exc)
            self.last_recovery = recovery
            raise
        recovery["ok"] = True
        recovery["new_pid"] = self.process.pid if self.process is not None else None
        self.last_recovery = recovery
        return self.tools

    def _is_recoverable_refresh_failure(self, exc: BaseException) -> bool:
        if isinstance(exc, RPCError):
            return False
        message = str(exc)
        if message.startswith("Could not list tools for "):
            return False
        if isinstance(exc, BrokenPipeError):
            return True
        if isinstance(exc, TimeoutError):
            return self.process is not None and self.process.poll() is not None
        lowered = message.lower()
        if "closed stdout" in lowered or "broken pipe" in lowered:
            return True
        if "is not running" in lowered:
            return self.process is not None and self.process.poll() is not None
        return self.process is not None and self.process.poll() is not None

    def stop(self, kill: bool = False) -> dict[str, Any]:
        with self.lock:
            self.stop_locked(kill=kill)
            return self.status()

    def stop_locked(self, kill: bool = False, collect_stderr: bool = False) -> str | None:
        proc = self.process
        self.process = None
        self.peer = None
        self.active_wire_mode = None
        stderr_preview = None
        if proc is None:
            return None
        if proc.poll() is None:
            if kill:
                proc.kill()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        if collect_stderr:
            stderr_preview = _stderr_preview(proc)
        return stderr_preview

    def status(self) -> dict[str, Any]:
        proc = self.process
        return {
            "name": self.config.name,
            "running": self.running,
            "pid": proc.pid if self.running and proc is not None else None,
            "returncode": proc.poll() if proc is not None else None,
            "tool_count": len(self.tools),
            "tools": sorted(self.tools),
            "last_error": self.last_error,
            "started_at": self.started_at,
            "source": self.config.source,
            "description": self.config.description,
            "enabled": self.config.enabled,
            "template": self.config.template,
            "startable": self.config.startable,
            "reason": self.config.start_block_reason,
            "wire_mode": self.config.wire_mode,
            "active_wire_mode": self.active_wire_mode,
            "wire_probe_modes": list(self.config.wire_probe_modes),
            "wire_probe_timeout_sec": self.config.wire_probe_timeout_sec,
            "wire_attempts": list(self.wire_attempts),
            "restart_count": self.restart_count,
            "last_recovery": dict(self.last_recovery) if self.last_recovery else None,
            "env_keys": sorted(set(self.config.env) | set(self.config.env_vars)),
            "command": command_summary(self.config.command, self.config.args) if self.config.command else None,
        }

    def _initialize_locked(self, timeout: float) -> None:
        response = self._request_locked(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "metamcp-tools", "version": __version__},
            },
            timeout=timeout,
        )
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"Could not initialize {self.config.name}: {error.get('message', error)}")
        self._notify_locked("notifications/initialized", {})

    def _request_locked(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.peer is None or self.process is None or self.process.poll() is not None:
            raise RuntimeError(f"Server {self.config.name!r} is not running")
        wire_mode = self._current_wire_mode()
        self.request_id += 1
        request = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        try:
            self.peer.write(request, wire_mode)
            while True:
                message, _wire_mode = self.peer.read(timeout=timeout)
                if message is None:
                    raise RuntimeError(f"Server {self.config.name!r} closed stdout")
                if message.get("id") == self.request_id:
                    return message
                LOGGER.debug("ignored_child_message server=%s message=%s", self.config.name, message)
        except (TimeoutError, RPCError, RuntimeError, OSError) as exc:
            self.last_error = str(exc)
            raise

    def _notify_locked(self, method: str, params: dict[str, Any]) -> None:
        if self.peer is None:
            return
        self.peer.write({"jsonrpc": "2.0", "method": method, "params": params}, self._current_wire_mode())

    def _current_wire_mode(self) -> str:
        if self.active_wire_mode in {"framed", "ndjson"}:
            return self.active_wire_mode
        if self.config.wire_mode in {"framed", "ndjson"}:
            return self.config.wire_mode
        raise RuntimeError(f"Server {self.config.name!r} has not detected an active wire_mode")


class ChildRegistry:
    def __init__(self, configs: dict[str, ChildServerConfig]) -> None:
        self.sessions = {name: ChildSession(config) for name, config in sorted(configs.items())}

    def add(self, config: ChildServerConfig, replace: bool = False) -> ChildSession:
        existing = self.sessions.get(config.name)
        if existing is not None and not replace:
            raise ValueError(f"Child MCP server {config.name!r} already exists")
        if existing is not None:
            existing.stop(kill=True)
        session = ChildSession(config)
        self.sessions[config.name] = session
        self.sessions = dict(sorted(self.sessions.items()))
        return session

    def remove_dynamic(self, name: str, kill: bool = False) -> ChildServerConfig:
        session = self.require(name)
        if not session.config.dynamic_registration:
            raise ValueError(f"Child MCP server {name!r} is not a dynamic registration")
        session.stop(kill=kill)
        del self.sessions[name]
        return session.config

    def catalog(self, include_tools: bool = True) -> dict[str, Any]:
        return {
            "servers": [
                self._catalog_entry(session, include_tools=include_tools)
                for session in self.sessions.values()
            ]
        }

    def _catalog_entry(self, session: ChildSession, include_tools: bool) -> dict[str, Any]:
        config = session.config
        entry: dict[str, Any] = {
            "name": config.name,
            "enabled": config.enabled,
            "template": config.template,
            "startable": config.startable,
            "reason": config.start_block_reason,
            "running": session.running,
            "description": config.description,
            "source": config.source,
            "tool_count": len(session.tools),
            "wire_mode": config.wire_mode,
            "active_wire_mode": session.active_wire_mode,
            "wire_probe_modes": list(config.wire_probe_modes),
            "env_keys": sorted(set(config.env) | set(config.env_vars)),
            "command": command_summary(config.command, config.args) if config.command else None,
        }
        if include_tools:
            entry["tools"] = [
                {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "dynamic_name": dynamic_tool_name(config.name, name),
                }
                for name, tool in sorted(session.tools.items())
            ]
        return entry

    def require(self, name: str) -> ChildSession:
        try:
            return self.sessions[name]
        except KeyError as exc:
            raise KeyError(f"Unknown child MCP server {name!r}") from exc

    def statuses(self) -> dict[str, Any]:
        return {"servers": [session.status() for session in self.sessions.values()]}

    def stop_all(self) -> dict[str, Any]:
        return {"servers": [session.stop() for session in self.sessions.values()]}

    def search(self, query: str, max_results: int = 20) -> dict[str, Any]:
        needle = query.strip().lower()
        if not needle:
            raise ValueError("query cannot be empty")
        matches: list[dict[str, Any]] = []
        for session in self.sessions.values():
            haystack = " ".join([session.config.name, session.config.description]).lower()
            if needle in haystack:
                matches.append({"kind": "server", "server_name": session.config.name, "description": session.config.description})
            for tool_name, tool in sorted(session.tools.items()):
                tool_haystack = " ".join([tool_name, str(tool.get("description") or "")]).lower()
                if needle in tool_haystack:
                    matches.append(
                        {
                            "kind": "tool",
                            "server_name": session.config.name,
                            "tool_name": tool_name,
                            "dynamic_name": dynamic_tool_name(session.config.name, tool_name),
                            "description": str(tool.get("description") or ""),
                        }
                    )
            if len(matches) >= max_results:
                break
        return {"query": query, "matches": matches[:max_results]}

    def redact_env_for_debug(self) -> dict[str, dict[str, Any]]:
        return {name: redact_mapping(session.config.env) for name, session in self.sessions.items()}


def dynamic_tool_name(server_name: str, tool_name: str) -> str:
    return f"child__{_safe_name(server_name)}__{_safe_name(tool_name)}"


def split_dynamic_tool_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("child__"):
        return None
    parts = name.split("__", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def _safe_name(name: str) -> str:
    safe = []
    for char in name:
        if char.isalnum() or char == "_":
            safe.append(char)
        else:
            safe.append("_")
    normalized = "".join(safe).strip("_")
    return normalized or "unnamed"


def _brief_error(exc: BaseException) -> str:
    return _brief_text(str(exc) or exc.__class__.__name__)


def _stderr_preview(proc: subprocess.Popen[bytes]) -> str | None:
    if proc.stderr is None:
        return None
    try:
        _stdout, stderr = proc.communicate(timeout=0.2)
    except subprocess.TimeoutExpired:
        return None
    if not stderr:
        return None
    return _brief_text(stderr.decode("utf-8", errors="replace"))


def _brief_text(text: str, limit: int = 1000) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."
