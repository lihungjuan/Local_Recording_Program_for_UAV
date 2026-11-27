from __future__ import annotations

import json
import struct
from typing import Any


def build_ctp(topic: str, payload: Any | None = None) -> bytes:
    topic_bytes = topic.encode("utf-8")
    if payload is None:
        content = b""
    elif isinstance(payload, bytes):
        content = payload
    else:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"".join(
        (
            b"CTP:",
            struct.pack("<h", len(topic_bytes)),
            topic_bytes,
            struct.pack("<i", len(content)),
            content,
        )
    )


class CTPParser:
    """Incremental CTP parser for a TCP stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[tuple[str, Any]]:
        if data:
            self._buf.extend(data)
        out: list[tuple[str, Any]] = []
        while True:
            idx = self._buf.find(b"CTP:")
            if idx < 0:
                if len(self._buf) > 3:
                    del self._buf[:-3]
                break
            if idx:
                del self._buf[:idx]
            if len(self._buf) < 10:
                break
            topic_len = struct.unpack_from("<h", self._buf, 4)[0]
            if topic_len < 0 or topic_len > 512:
                del self._buf[:4]
                continue
            header_end = 10 + topic_len
            if len(self._buf) < header_end:
                break
            topic = bytes(self._buf[6 : 6 + topic_len]).decode("utf-8", "replace")
            json_len = struct.unpack_from("<i", self._buf, 6 + topic_len)[0]
            if json_len < 0 or json_len > 2_000_000:
                del self._buf[:4]
                continue
            total = header_end + json_len
            if len(self._buf) < total:
                break
            raw = bytes(self._buf[header_end:total])
            del self._buf[:total]
            payload: Any = None
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    payload = raw.decode("utf-8", "replace")
            out.append((topic, payload))
        return out
