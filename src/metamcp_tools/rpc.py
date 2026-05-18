from __future__ import annotations

import json
import os
import select
import sys
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

MAX_PAYLOAD_BYTES = 2_000_000


class RPCError(RuntimeError):
    def __init__(self, message: str, code: int = -32000, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def encode_message(message: dict[str, Any], wire_mode: str = "framed") -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if wire_mode == "ndjson":
        return payload + b"\n"
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


def decode_json(payload: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RPCError(f"Invalid JSON-RPC payload: {exc}", code=-32700) from exc
    if not isinstance(decoded, dict):
        raise RPCError("JSON-RPC payload must be an object", code=-32600)
    return decoded


@dataclass
class StreamPeer:
    reader: BinaryIO
    writer: BinaryIO
    default_wire_mode: str = "framed"
    _buffer: bytearray = field(default_factory=bytearray)

    def write(self, message: dict[str, Any], wire_mode: str | None = None) -> None:
        self.writer.write(encode_message(message, wire_mode or self.default_wire_mode))
        self.writer.flush()

    def read(self, timeout: float | None = None) -> tuple[dict[str, Any] | None, str]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            parsed = self._try_parse_buffer()
            if parsed is not None:
                return parsed

            wait = None
            if deadline is not None:
                wait = max(0.0, deadline - time.monotonic())
                if wait <= 0:
                    raise TimeoutError("Timed out waiting for JSON-RPC message")

            fd = self.reader.fileno()
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                raise TimeoutError("Timed out waiting for JSON-RPC message")

            chunk = os.read(fd, 4096)
            if not chunk:
                if self._buffer.strip():
                    raise RPCError("EOF with partial JSON-RPC frame", code=-32700)
                return None, "unknown"
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_PAYLOAD_BYTES:
                raise RPCError("Incoming JSON-RPC message is too large", code=-32600)

    def _try_parse_buffer(self) -> tuple[dict[str, Any], str] | None:
        while self._buffer.startswith((b"\r", b"\n", b" ", b"\t")):
            del self._buffer[0]
        if not self._buffer:
            return None

        stripped = bytes(self._buffer).lstrip()
        if stripped.startswith(b"{"):
            newline = self._buffer.find(b"\n")
            if newline < 0:
                return None
            line = bytes(self._buffer[:newline]).strip()
            del self._buffer[: newline + 1]
            return decode_json(line), "ndjson"

        header_end = self._find_header_end()
        if header_end is None:
            return None
        header_bytes = bytes(self._buffer[:header_end])
        body_start = header_end
        content_length = self._content_length(header_bytes)
        if content_length is None:
            raise RPCError("Missing Content-Length header", code=-32600)
        if content_length > MAX_PAYLOAD_BYTES:
            raise RPCError("Incoming JSON-RPC message is too large", code=-32600)
        body_end = body_start + content_length
        if len(self._buffer) < body_end:
            return None
        body = bytes(self._buffer[body_start:body_end])
        del self._buffer[:body_end]
        return decode_json(body), "framed"

    def _find_header_end(self) -> int | None:
        crlf = self._buffer.find(b"\r\n\r\n")
        lf = self._buffer.find(b"\n\n")
        candidates = [idx + 4 for idx in [crlf] if idx >= 0]
        candidates.extend(idx + 2 for idx in [lf] if idx >= 0)
        return min(candidates) if candidates else None

    @staticmethod
    def _content_length(header_bytes: bytes) -> int | None:
        text = header_bytes.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.lower() == "content-length":
                try:
                    return int(value.strip())
                except ValueError as exc:
                    raise RPCError("Invalid Content-Length header", code=-32600) from exc
        return None


STDIO = StreamPeer(sys.stdin.buffer, sys.stdout.buffer)

