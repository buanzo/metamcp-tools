# metamcp-tools

`metamcp-tools` is an on-demand MCP gateway. It starts quickly, exposes a small stable set of `metamcp_*` tools, and launches configured child MCP servers only when an agent asks for them.

This is useful for slow, situational, or heavyweight MCP servers that you want discoverable without paying their startup cost on every client launch.

## Tools

- `metamcp_catalog`: list child MCP servers and cached tool metadata.
- `metamcp_search`: search configured child servers and cached tool metadata.
- `metamcp_start`: start one child MCP and cache its `tools/list`.
- `metamcp_validate_config`: validate loaded config without launching children.
- `metamcp_call`: call a child tool through the stable gateway proxy.
- `metamcp_status`: inspect child lifecycle state.
- `metamcp_stop`: stop one child or all child MCP processes.

When dynamic tools are enabled, the gateway also emits `notifications/tools/list_changed` after a child starts and exposes child tools as namespaced direct tools such as `child__example__echo`. The stable `metamcp_call` path is the compatibility contract; dynamic direct tools depend on client refresh behavior.

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

Then call `metamcp_catalog` to inspect configured children, `metamcp_start` to launch one child, and `metamcp_call` to proxy a child tool call.

## Configuration

Child MCP definitions use TOML:

```toml
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

`wire_mode = "auto"` probes `Content-Length` JSON-RPC framing and newline-delimited JSON in the configured `wire_probe_modes` order. Set `wire_mode = "framed"` or `wire_mode = "ndjson"` only when you want to skip probing.

Includes are resolved beside the real config file target, so symlinked configs can still include repo-local `conf.d/*.toml`.

Disabled starter templates live under `conf.d/examples/`. They are examples, not active defaults.

## Security Model

- Child MCP servers are explicit allowlist entries.
- Normal gateway startup performs no child MCP launch or remote I/O before `initialize`.
- Tool output redacts environment values and summarizes commands without dumping full arguments.
- Secrets should come from inherited environment variables, not committed config files.
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

