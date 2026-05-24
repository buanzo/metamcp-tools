#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys


TOOLS = [
    {
        "name": "echo",
        "description": "Echo arguments back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
        },
    }
]


WIRE_MODE = os.environ.get("FAKE_CHILD_WIRE_MODE", "both").strip().lower()
TOOLS_LIST_FAILURE = os.environ.get("FAKE_CHILD_TOOLS_LIST_FAILURE", "").strip().lower()
TOOLS_LIST_COUNT = 0


def allows_wire(mode: str) -> bool:
    return WIRE_MODE not in {"framed", "ndjson"} or WIRE_MODE == mode


def read_message() -> tuple[dict | None, str]:
    first = sys.stdin.buffer.readline()
    if not first:
        return None, "unknown"
    if first.lstrip().startswith(b"{"):
        if not allows_wire("ndjson"):
            return None, "unknown"
        return json.loads(first.decode("utf-8")), "ndjson"
    if not allows_wire("framed"):
        return None, "unknown"
    header = bytearray(first)
    while not (header.endswith(b"\r\n\r\n") or header.endswith(b"\n\n")):
        chunk = sys.stdin.buffer.readline()
        if not chunk:
            return None, "unknown"
        header.extend(chunk)
    length = None
    for line in bytes(header).decode("utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.lower() == "content-length":
            length = int(value.strip())
    if length is None:
        return None, "unknown"
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8")), "framed"


def send_message(message: dict, mode: str) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if mode == "ndjson":
        sys.stdout.buffer.write(payload + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main() -> int:
    global TOOLS_LIST_COUNT
    while True:
        message, mode = read_message()
        if message is None:
            return 0
        method = message.get("method")
        request_id = message.get("id")
        if method in {"initialized", "notifications/initialized"}:
            continue
        if method == "initialize":
            send_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-child", "version": "0.1.0"},
                    },
                },
                mode,
            )
            continue
        if method == "tools/list":
            TOOLS_LIST_COUNT += 1
            if TOOLS_LIST_FAILURE == "close_stdout_after_first" and TOOLS_LIST_COUNT > 1:
                return 0
            if TOOLS_LIST_FAILURE == "json_error_after_first" and TOOLS_LIST_COUNT > 1:
                send_message(
                    {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "synthetic tools/list failure"}},
                    mode,
                )
                continue
            send_message({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}, mode)
            continue
        if method == "tools/call":
            params = message.get("params") or {}
            send_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(params.get("arguments") or {})}]},
                },
                mode,
            )
            continue
        send_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown"}}, mode)


if __name__ == "__main__":
    raise SystemExit(main())
