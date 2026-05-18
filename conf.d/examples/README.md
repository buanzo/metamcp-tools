# Disabled Child MCP Templates

These files are useful starter entries for common MCP servers. They are intentionally disabled and marked `template = true`.

To use one:

1. Copy the file from `conf.d/examples/` to `conf.d/`.
2. Review the command, args, cwd, and environment requirements.
3. Set `enabled = true`.
4. Leave `wire_mode = "auto"` unless you need to force `framed` or `ndjson`.
5. Run `python3 server.py --config config.toml --probe`.

The default root config does not include this directory. If you explicitly include it, the gateway lists disabled templates in `metamcp_catalog`, but `metamcp_start` refuses to launch them until they are copied into an active config path and enabled.
