# metamcp-tools

`metamcp-tools` is an on-demand MCP gateway. It starts quickly, exposes a small stable set of `metamcp_*` tools, and launches configured child MCP servers only when an agent asks for them.

This is useful for slow, situational, or heavyweight MCP servers that you want discoverable without paying their startup cost on every client launch.

## Tools

- `metamcp_catalog`: list child MCP servers and cached tool metadata.
- `metamcp_explain_capabilities`: explain current gateway capabilities, loaded config facts, and safe discovery next steps.
- `metamcp_search`: search configured child servers and cached tool metadata.
- `metamcp_start`: start one child MCP and cache its `tools/list`.
- `metamcp_validate_config`: validate loaded config without launching children.
- `metamcp_call`: call a child tool through the stable gateway proxy.
- `metamcp_status`: inspect child lifecycle state.
- `metamcp_stop`: stop one child or all child MCP processes.
- `metamcp_register_server`: when enabled, register a stdio child MCP for the current session or persist it into generated config.
- `metamcp_unregister_server`: when enabled, remove a dynamically registered child MCP.

The gateway emits `notifications/tools/list_changed` after child tools are discovered and publishes them as namespaced direct tools such as `child__example__echo`. The stable `metamcp_call` path remains available for control-plane use and bootstrap flows.

## Quick Start

Copy the example config:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` and add child MCP definitions directly or through `conf.d/*.toml`.

Register the gateway with a local MCP client such as Codex:

```bash
codex mcp add metamcp-tools -- python3 /path/to/metamcp-tools/server.py --config /path/to/metamcp-tools/config.toml
```

Probe or validate without starting children:

```bash
python3 server.py --config config.toml --probe
python3 server.py --config config.toml --validate
```

Then call `metamcp_explain_capabilities` for an agent-oriented explanation of the loaded config, `metamcp_catalog` to inspect configured children, and `metamcp_start` to launch one child. Once its tools are discovered, they are published as direct namespaced tools.

## Configuration

Child MCP definitions use TOML:

```toml
[gateway]
allow_dynamic_registration = false
dynamic_registration_dir = "dynamic.d"

[servers.example_child]
description = "Example stdio MCP child."
command = "python3"
args = ["/path/to/child_mcp_server.py"]
startup_timeout_sec = 10
tool_timeout_sec = 60
wire_mode = "auto"
wire_probe_modes = ["framed", "ndjson"]
wire_probe_timeout_sec = 5
cwd = "/tmp"
env = { EXAMPLE_NON_SECRET = "value" }
env_vars = ["TOKEN_FROM_PARENT_ENV"]
```

Dynamic registration is disabled by default. When `allow_dynamic_registration = true`, `metamcp_register_server` can add a child for the current gateway process with `persistence = "session"` or write a generated TOML file under `dynamic_registration_dir` with `persistence = "config"`. Include that directory from config if you want generated registrations to survive gateway restarts.

`wire_mode = "auto"` probes `Content-Length` JSON-RPC framing and newline-delimited JSON in the configured `wire_probe_modes` order. Set `wire_mode = "framed"` or `wire_mode = "ndjson"` only when you want to skip probing.

Includes are resolved beside the real config file target, so symlinked configs can still include repo-local `conf.d/*.toml`.

Disabled starter templates live under `conf.d/examples/`. They are examples, not active defaults.

## Security Model

- Child MCP servers are explicit allowlist entries.
- Runtime registration tools are hidden unless `allow_dynamic_registration = true`.
- Normal gateway startup performs no child MCP launch or remote I/O before `initialize`.
- Tool output redacts environment values and summarizes commands without dumping full arguments.
- Secrets should come from inherited environment variables, not committed config files.
- Dynamic registration rejects inline secret-like `env` keys by default; use `env_vars` for tokens.
- Child startup and tool calls are timeout-bounded.
- Remote HTTP MCP servers are listed as placeholders only; this gateway currently starts and proxies stdio child MCPs.

## Development

Run tests:

```bash
python3 -m pytest tests
python3 -m py_compile server.py src/metamcp_tools/*.py tests/fake_child_mcp.py
```

Build/install locally:

```bash
python3 -m pip install -e .
metamcp-tools --config config.example.toml --validate
```
