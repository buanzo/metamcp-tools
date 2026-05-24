from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from metamcp_tools.child import dynamic_tool_name
from metamcp_tools.config import load_gateway_config, validate_gateway_config
from metamcp_tools.server import MetaMCPServer


def text_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def write_config(tmp_path: Path) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config = tmp_path / "gateway.toml"
    config.write_text(
        f"""
[gateway]
cache_path = "{tmp_path / 'cache.json'}"

[servers.fake]
description = "Fake child"
command = "{sys.executable}"
args = ["{child}"]
startup_timeout_sec = 5
tool_timeout_sec = 5
wire_mode = "ndjson"
""".strip(),
        encoding="utf-8",
    )
    return config


def write_refresh_failure_config(tmp_path: Path, failure_mode: str) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config = tmp_path / "gateway.toml"
    config.write_text(
        f"""
[gateway]
cache_path = "{tmp_path / 'cache.json'}"

[servers.fake]
description = "Fake child"
command = "{sys.executable}"
args = ["{child}"]
startup_timeout_sec = 5
tool_timeout_sec = 5
wire_mode = "ndjson"
env = {{ FAKE_CHILD_TOOLS_LIST_FAILURE = "{failure_mode}" }}
""".strip(),
        encoding="utf-8",
    )
    return config


def write_auto_wire_config(tmp_path: Path, child_wire_mode: str, probe_modes: list[str]) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config = tmp_path / "gateway.toml"
    probe = ", ".join(json.dumps(mode) for mode in probe_modes)
    config.write_text(
        f"""
[gateway]

[servers.fake]
description = "Fake child"
command = "{sys.executable}"
args = ["{child}"]
startup_timeout_sec = 5
tool_timeout_sec = 5
wire_mode = "auto"
wire_probe_modes = [{probe}]
wire_probe_timeout_sec = 0.5
env = {{ FAKE_CHILD_WIRE_MODE = "{child_wire_mode}" }}
""".strip(),
        encoding="utf-8",
    )
    return config


def write_included_config(tmp_path: Path) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    root = tmp_path / "repo" / "gateway.toml"
    confd = root.parent / "conf.d"
    confd.mkdir(parents=True)
    root.write_text(
        f"""
[gateway]
cache_path = "{tmp_path / 'cache.json'}"

[include]
dirs = ["conf.d"]
""".strip(),
        encoding="utf-8",
    )
    (confd / "child.toml").write_text(
        f"""
[servers.fake]
description = "Fake child from include"
command = "{sys.executable}"
args = ["{child}"]
startup_timeout_sec = 5
tool_timeout_sec = 5
""".strip(),
        encoding="utf-8",
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    link = shared / "metamcp.toml"
    link.symlink_to(root)
    return link


def write_template_config(tmp_path: Path) -> Path:
    root = tmp_path / "config.toml"
    root.write_text(
        """
[gateway]

[servers.template_child]
enabled = false
template = true
description = "Disabled template"
command = "python3"
args = ["unused.py"]
""".strip(),
        encoding="utf-8",
    )
    return root


def write_secret_env_config(tmp_path: Path) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config = tmp_path / "gateway.toml"
    config.write_text(
        f"""
[gateway]

[servers.secret_fake]
description = "Fake child with env"
command = "{sys.executable}"
args = ["{child}"]
env = {{ API_TOKEN = "supersecret-value", NORMAL_FLAG = "normal-value" }}
env_vars = ["EXTERNAL_SECRET"]
wire_mode = "ndjson"
""".strip(),
        encoding="utf-8",
    )
    return config


def write_duplicate_config(tmp_path: Path) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    root = tmp_path / "config.toml"
    confd = tmp_path / "conf.d"
    confd.mkdir()
    root.write_text(
        f"""
[gateway]

[include]
dirs = ["conf.d"]

[servers.fake]
description = "Root fake"
command = "{sys.executable}"
args = ["{child}"]
""".strip(),
        encoding="utf-8",
    )
    (confd / "override.toml").write_text(
        f"""
[servers.fake]
description = "Override fake"
command = "{sys.executable}"
args = ["{child}"]
""".strip(),
        encoding="utf-8",
    )
    return root


def write_examples_exclusion_config(tmp_path: Path) -> Path:
    root = tmp_path / "config.toml"
    confd = tmp_path / "conf.d"
    examples = confd / "examples"
    examples.mkdir(parents=True)
    root.write_text(
        """
[gateway]

[include]
dirs = ["conf.d"]
""".strip(),
        encoding="utf-8",
    )
    (examples / "template.toml").write_text(
        """
[servers.template_only]
enabled = false
template = true
description = "Nested example template"
command = "python3"
args = ["unused.py"]
""".strip(),
        encoding="utf-8",
    )
    return root


def write_registration_config(tmp_path: Path) -> Path:
    root = tmp_path / "config.toml"
    dynamic_dir = tmp_path / "dynamic.d"
    dynamic_dir.mkdir()
    root.write_text(
        """
[gateway]
allow_dynamic_registration = true
dynamic_registration_dir = "dynamic.d"

[include]
dirs = ["dynamic.d"]
""".strip(),
        encoding="utf-8",
    )
    return root


def call_tool(server: MetaMCPServer, name: str, arguments: dict) -> dict:
    response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response is not None
    assert "error" not in response
    return response["result"]


def test_catalog_does_not_start_child(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_config(tmp_path))
    server = MetaMCPServer(config)
    result = call_tool(server, "metamcp_catalog", {})
    payload = text_payload(result)
    assert payload["servers"][0]["name"] == "fake"
    assert payload["servers"][0]["running"] is False
    assert server.registry.require("fake").running is False


def test_initialize_always_advertises_tool_list_changed(tmp_path: Path) -> None:
    server = MetaMCPServer(load_gateway_config(config_path=write_config(tmp_path)))
    response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response is not None
    assert response["result"]["capabilities"]["tools"]["listChanged"] is True


def test_explain_capabilities_is_advertised_and_does_not_start_child(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_config(tmp_path))
    server = MetaMCPServer(config)
    tools_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools_response is not None
    names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert "metamcp_explain_capabilities" in names
    assert "explain_capabilities" not in names

    result = call_tool(server, "metamcp_explain_capabilities", {})
    payload = text_payload(result)
    assert payload["read_only"] is True
    assert payload["summary"]["server_count"] == 1
    assert payload["summary"]["startable_count"] == 1
    assert payload["summary"]["running_count"] == 0
    assert payload["summary"]["cached_tool_count"] == 0
    assert "No child MCP servers are running" in " ".join(payload["phrases"])
    assert any("metamcp_catalog" in step for step in payload["recommended_next_steps"])
    assert payload["servers"][0]["name"] == "fake"
    assert payload["servers"][0]["next_action"] == "call metamcp_start to launch and refresh tool metadata"
    assert server.registry.require("fake").running is False


def test_explain_capabilities_reflects_running_and_cached_tools(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_config(tmp_path))
    server = MetaMCPServer(config)
    call_tool(server, "metamcp_start", {"server_name": "fake"})
    result = call_tool(server, "metamcp_explain_capabilities", {})
    payload = text_payload(result)

    assert payload["summary"]["running_count"] == 1
    assert payload["summary"]["cached_tool_count"] == 1
    assert payload["servers"][0]["tools"][0]["name"] == "echo"
    assert payload["servers"][0]["tools"][0]["dynamic_name"] == dynamic_tool_name("fake", "echo")
    assert any("metamcp_search" in step for step in payload["recommended_next_steps"])
    server.registry.stop_all()


def test_explain_capabilities_reports_template_and_dynamic_registration(tmp_path: Path) -> None:
    template_server = MetaMCPServer(load_gateway_config(config_path=write_template_config(tmp_path)))
    template_payload = text_payload(call_tool(template_server, "metamcp_explain_capabilities", {}))
    assert template_payload["summary"]["template_count"] == 1
    assert template_payload["summary"]["disabled_count"] == 1
    assert template_payload["servers"][0]["reason"] == "template"
    assert "templates" in " ".join(template_payload["phrases"])

    registration_server = MetaMCPServer(load_gateway_config(config_path=write_registration_config(tmp_path)))
    registration_payload = text_payload(call_tool(registration_server, "metamcp_explain_capabilities", {}))
    assert registration_payload["summary"]["server_count"] == 0
    assert any("Dynamic child MCP registration is enabled" in phrase for phrase in registration_payload["phrases"])
    assert any("metamcp_register_server" in step for step in registration_payload["recommended_next_steps"])


def test_explain_capabilities_redacts_env_values(tmp_path: Path) -> None:
    server = MetaMCPServer(load_gateway_config(config_path=write_secret_env_config(tmp_path)))
    result = call_tool(server, "metamcp_explain_capabilities", {})
    raw = result["content"][0]["text"]
    payload = json.loads(raw)

    assert "supersecret-value" not in raw
    assert "normal-value" not in raw
    assert payload["servers"][0]["env_keys"] == ["API_TOKEN", "EXTERNAL_SECRET", "NORMAL_FLAG"]


def test_start_then_proxy_call_and_dynamic_tool(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_config(tmp_path))
    server = MetaMCPServer(config)
    start_result = call_tool(server, "metamcp_start", {"server_name": "fake"})
    payload = text_payload(start_result)
    assert payload["status"]["running"] is True
    assert payload["status"]["wire_mode"] == "ndjson"
    assert payload["status"]["active_wire_mode"] == "ndjson"
    assert payload["status"]["tools"] == ["echo"]
    assert (tmp_path / "cache.json").exists()

    proxy = call_tool(server, "metamcp_call", {"server_name": "fake", "tool_name": "echo", "arguments": {"message": "hi"}})
    assert json.loads(proxy["content"][0]["text"]) == {"message": "hi"}

    tools_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools_response is not None
    names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert dynamic_tool_name("fake", "echo") in names
    server.registry.stop_all()


def test_start_recovers_stale_stdout_during_running_refresh(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_refresh_failure_config(tmp_path, "close_stdout_after_first"))
    server = MetaMCPServer(config)
    first_payload = text_payload(call_tool(server, "metamcp_start", {"server_name": "fake"}))
    old_pid = first_payload["status"]["pid"]

    second_payload = text_payload(call_tool(server, "metamcp_start", {"server_name": "fake"}))
    status = second_payload["status"]
    recovery = status["last_recovery"]

    assert status["running"] is True
    assert status["tools"] == ["echo"]
    assert status["restart_count"] == 1
    assert status["last_error"] is None
    assert recovery["ok"] is True
    assert recovery["old_pid"] == old_pid
    assert recovery["new_pid"] is not None
    assert "closed stdout" in recovery["error"]
    server.registry.stop_all()


def test_start_does_not_recover_live_tools_list_error(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_refresh_failure_config(tmp_path, "json_error_after_first"))
    server = MetaMCPServer(config)
    call_tool(server, "metamcp_start", {"server_name": "fake"})

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "metamcp_start", "arguments": {"server_name": "fake"}},
        }
    )
    assert response is not None
    assert "error" in response
    assert "synthetic tools/list failure" in response["error"]["message"]

    status = server.registry.require("fake").status()
    assert status["running"] is True
    assert status["restart_count"] == 0
    assert status["tools"] == ["echo"]
    server.registry.stop_all()


def test_dynamic_tools_deprecated_config_is_ignored(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("[gateway]\n", "[gateway]\ndynamic_tools = false\n", 1), encoding="utf-8")
    config = load_gateway_config(config_path=config_path)
    server = MetaMCPServer(config)
    call_tool(server, "metamcp_start", {"server_name": "fake"})
    tools_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools_response is not None
    names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert dynamic_tool_name("fake", "echo") in names
    assert any("dynamic_tools" in item for item in config.diagnostics["warnings"])
    server.registry.stop_all()


def test_auto_wire_mode_falls_back_to_ndjson_child(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_auto_wire_config(tmp_path, "ndjson", ["framed", "ndjson"]))
    server = MetaMCPServer(config)
    payload = text_payload(call_tool(server, "metamcp_start", {"server_name": "fake"}))

    status = payload["status"]
    assert status["wire_mode"] == "auto"
    assert status["active_wire_mode"] == "ndjson"
    assert [item["mode"] for item in status["wire_attempts"]] == ["framed", "ndjson"]
    assert status["wire_attempts"][0]["ok"] is False
    assert status["wire_attempts"][1]["ok"] is True
    assert status["tools"] == ["echo"]
    server.registry.stop_all()


def test_auto_wire_mode_detects_framed_child_first(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_auto_wire_config(tmp_path, "framed", ["framed", "ndjson"]))
    server = MetaMCPServer(config)
    payload = text_payload(call_tool(server, "metamcp_start", {"server_name": "fake"}))

    status = payload["status"]
    assert status["wire_mode"] == "auto"
    assert status["active_wire_mode"] == "framed"
    assert [item["mode"] for item in status["wire_attempts"]] == ["framed"]
    assert status["tools"] == ["echo"]
    server.registry.stop_all()


def test_tool_cache_rehydrates_catalog_without_starting_child(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_gateway_config(config_path=config_path)
    server = MetaMCPServer(config)
    call_tool(server, "metamcp_start", {"server_name": "fake"})
    server.registry.stop_all()

    fresh = MetaMCPServer(load_gateway_config(config_path=config_path))
    catalog = text_payload(call_tool(fresh, "metamcp_catalog", {}))
    assert catalog["servers"][0]["running"] is False
    assert catalog["servers"][0]["tools"][0]["name"] == "echo"


def test_include_dir_resolves_beside_symlink_target(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_included_config(tmp_path))
    server = MetaMCPServer(config)
    catalog = text_payload(call_tool(server, "metamcp_catalog", {}))
    assert catalog["servers"][0]["name"] == "fake"
    assert "conf.d/child.toml" in catalog["servers"][0]["source"]


def test_disabled_template_catalogs_but_refuses_start(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_template_config(tmp_path))
    server = MetaMCPServer(config)
    catalog = text_payload(call_tool(server, "metamcp_catalog", {}))
    entry = catalog["servers"][0]
    assert entry["enabled"] is False
    assert entry["template"] is True
    assert entry["startable"] is False
    assert entry["reason"] == "template"

    result = call_tool(server, "metamcp_start", {"server_name": "template_child"})
    assert result["isError"] is True
    assert "template_child" in result["content"][0]["text"]


def test_examples_subdir_is_not_included_by_default(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_examples_exclusion_config(tmp_path))
    assert config.servers == {}
    report = validate_gateway_config(config)
    assert report["summary"]["server_count"] == 0


def test_registration_tools_are_hidden_by_default(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_config(tmp_path))
    server = MetaMCPServer(config)
    tools_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools_response is not None
    names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert "metamcp_register_server" not in names
    assert "metamcp_unregister_server" not in names
    report = validate_gateway_config(config)
    assert report["allow_dynamic_registration"] is False
    assert report["dynamic_registration_dir"].endswith("dynamic.d")


def test_session_registration_starts_child_and_publishes_tool(tmp_path: Path) -> None:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config = load_gateway_config(config_path=write_registration_config(tmp_path))
    server = MetaMCPServer(config)
    tools_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools_response is not None
    names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert "metamcp_register_server" in names
    assert "metamcp_unregister_server" in names

    result = call_tool(
        server,
        "metamcp_register_server",
        {
            "server_name": "runtime_fake",
            "description": "Runtime fake",
            "command": sys.executable,
            "args": [str(child)],
            "wire_mode": "ndjson",
            "persistence": "session",
            "start": True,
        },
    )
    payload = text_payload(result)
    assert payload["registered"] is True
    assert payload["started"] is True
    assert payload["status"]["running"] is True

    direct_name = dynamic_tool_name("runtime_fake", "echo")
    tools_response = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    assert tools_response is not None
    assert direct_name in {tool["name"] for tool in tools_response["result"]["tools"]}
    proxy = call_tool(server, direct_name, {"message": "hi"})
    assert json.loads(proxy["content"][0]["text"]) == {"message": "hi"}
    server.registry.stop_all()


def test_config_registration_persists_and_reloads(tmp_path: Path) -> None:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config_path = write_registration_config(tmp_path)
    server = MetaMCPServer(load_gateway_config(config_path=config_path))
    result = call_tool(
        server,
        "metamcp_register_server",
        {
            "server_name": "persisted_fake",
            "description": "Persisted fake",
            "command": sys.executable,
            "args": [str(child)],
            "wire_mode": "ndjson",
            "persistence": "config",
        },
    )
    payload = text_payload(result)
    config_file = Path(payload["config_path"])
    assert config_file.exists()
    assert "dynamic_registration = true" in config_file.read_text(encoding="utf-8")

    fresh_config = load_gateway_config(config_path=config_path)
    assert fresh_config.servers["persisted_fake"].dynamic_registration is True
    assert fresh_config.servers["persisted_fake"].dynamic_persistence == "config"
    fresh = MetaMCPServer(fresh_config)
    start_payload = text_payload(call_tool(fresh, "metamcp_start", {"server_name": "persisted_fake"}))
    assert start_payload["status"]["tools"] == ["echo"]
    fresh.registry.stop_all()


def test_unregister_removes_dynamic_config_file(tmp_path: Path) -> None:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config_path = write_registration_config(tmp_path)
    server = MetaMCPServer(load_gateway_config(config_path=config_path))
    registered = text_payload(
        call_tool(
            server,
            "metamcp_register_server",
            {
                "server_name": "delete_me",
                "command": sys.executable,
                "args": [str(child)],
                "wire_mode": "ndjson",
                "persistence": "config",
            },
        )
    )
    config_file = Path(registered["config_path"])
    assert config_file.exists()
    result = text_payload(call_tool(server, "metamcp_unregister_server", {"server_name": "delete_me"}))
    assert result["unregistered"] is True
    assert not config_file.exists()
    assert "delete_me" not in server.registry.sessions


def test_registration_rejects_secret_like_inline_env(tmp_path: Path) -> None:
    child = ROOT / "tests" / "fake_child_mcp.py"
    server = MetaMCPServer(load_gateway_config(config_path=write_registration_config(tmp_path)))
    result = call_tool(
        server,
        "metamcp_register_server",
        {
            "server_name": "secret_fake",
            "command": sys.executable,
            "args": [str(child)],
            "env": {"OPENAI_API_KEY": "secret"},
        },
    )
    assert result["isError"] is True
    assert "env_vars" in result["content"][0]["text"]


def test_registration_rejects_invalid_names_and_duplicates(tmp_path: Path) -> None:
    child = ROOT / "tests" / "fake_child_mcp.py"
    server = MetaMCPServer(load_gateway_config(config_path=write_registration_config(tmp_path)))
    invalid = call_tool(
        server,
        "metamcp_register_server",
        {"server_name": "bad/name", "command": sys.executable, "args": [str(child)]},
    )
    assert invalid["isError"] is True

    first = text_payload(
        call_tool(
            server,
            "metamcp_register_server",
            {"server_name": "dupe", "command": sys.executable, "args": [str(child)]},
        )
    )
    assert first["registered"] is True
    duplicate = call_tool(
        server,
        "metamcp_register_server",
        {"server_name": "dupe", "command": sys.executable, "args": [str(child)]},
    )
    assert duplicate["isError"] is True


def test_duplicate_server_names_are_reported_and_later_include_wins(tmp_path: Path) -> None:
    config = load_gateway_config(config_path=write_duplicate_config(tmp_path))
    assert config.servers["fake"].description == "Override fake"
    report = validate_gateway_config(config)
    assert report["ok"] is False
    assert report["diagnostics"]["duplicate_servers"][0]["name"] == "fake"


def test_missing_include_dir_is_reported(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[include]
dirs = ["missing.d"]
""".strip(),
        encoding="utf-8",
    )
    report = validate_gateway_config(load_gateway_config(config_path=config_path))
    assert report["ok"] is False
    assert report["diagnostics"]["missing_include_dirs"]


def test_invalid_wire_mode_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[servers.bad]
command = "python3"
wire_mode = "xml"
""".strip(),
        encoding="utf-8",
    )
    try:
        load_gateway_config(config_path=config_path)
    except ValueError as exc:
        assert "wire_mode" in str(exc)
    else:
        raise AssertionError("invalid wire_mode was accepted")


def test_invalid_wire_probe_mode_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[servers.bad]
command = "python3"
wire_mode = "auto"
wire_probe_modes = ["framed", "xml"]
""".strip(),
        encoding="utf-8",
    )
    try:
        load_gateway_config(config_path=config_path)
    except ValueError as exc:
        assert "wire_probe_modes" in str(exc)
    else:
        raise AssertionError("invalid wire_probe_modes was accepted")


def test_example_config_uses_auto_wire_mode() -> None:
    config = load_gateway_config(config_path=ROOT / "config.example.toml")
    example = config.servers["example_child"]
    assert example.wire_mode == "auto"
    assert example.wire_probe_modes == ("framed", "ndjson")


def test_validate_cli_does_not_start_child(tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    child = tmp_path / "marker_child.py"
    child.write_text(
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text('started', encoding='utf-8')
""".strip(),
        encoding="utf-8",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[servers.marker]
command = "{sys.executable}"
args = ["{child}"]
""".strip(),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "server.py"),
            "--config",
            str(config),
            "--validate",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(proc.stdout)["summary"]["server_count"] == 1
    assert not marker.exists()
