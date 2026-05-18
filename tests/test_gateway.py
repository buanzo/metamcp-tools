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
dynamic_tools = true
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


def write_auto_wire_config(tmp_path: Path, child_wire_mode: str, probe_modes: list[str]) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    config = tmp_path / "gateway.toml"
    probe = ", ".join(json.dumps(mode) for mode in probe_modes)
    config.write_text(
        f"""
[gateway]
dynamic_tools = true

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
dynamic_tools = true
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
dynamic_tools = true

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


def write_duplicate_config(tmp_path: Path) -> Path:
    child = ROOT / "tests" / "fake_child_mcp.py"
    root = tmp_path / "config.toml"
    confd = tmp_path / "conf.d"
    confd.mkdir()
    root.write_text(
        f"""
[gateway]
dynamic_tools = true

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
dynamic_tools = true

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
