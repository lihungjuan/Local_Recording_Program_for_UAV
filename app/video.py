from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from typing import Callable

import av
import cv2
import numpy as np

from . import config

FrameCb = Callable[[np.ndarray], None]
StateCb = Callable[[str], None]
StatsCb = Callable[[dict], None]
LogCb = Callable[[str, str], None]

H264_TYPE = 0x03
JPEG_TYPE = 0x02


def iter_jl_chunks(data: bytes):
    pos = 0
    n = len(data)
    while pos + 20 <= n:
        try:
            stream_type, _reserved, payload_len, seq, frame_size, offset, ts = struct.unpack_from(
                "<BBHIIII", data, pos
            )
        except struct.error:
            break
        base = stream_type & 0x7F
        if base not in (1, 2, 3):
            break
        if payload_len <= 0 or pos + 20 + payload_len > n:
            break
        yield {
            "stream_type": stream_type,
            "payload_len": payload_len,
            "sequence": seq,
            "frame_size": frame_size,
            "offset": offset,
            "timestamp": ts,
            "payload": data[pos + 20 : pos + 20 + payload_len],
        }
        pos += 20 + payload_len


class FrameReassembler:
    def __init__(self, max_frame_size: int = 2_000_000, stale_after_s: float = 1.2) -> None:
        self.max_frame_size = max_frame_size
        self.stale_after_s = stale_after_s
        self._buf = bytearray()
        self._expected = 0
        self._ranges: list[tuple[int, int]] = []
        self._stream_type = H264_TYPE
        self._seq = 0
        self._ts = 0
        self._last_seen = 0.0
        self._active = False
        self.dropped = 0
        self.completed = 0

    def reset(self) -> None:
        self._buf = bytearray()
        self._expected = 0
        self._ranges = []
        self._active = False

    def _is_complete(self) -> bool:
        if not self._ranges:
            return False
        merged: list[list[int]] = []
        for start, end in sorted(self._ranges):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return len(merged) == 1 and merged[0][0] == 0 and merged[0][1] >= self._expected

    def push_packet(self, pkt: dict) -> dict | None:
        now = time.monotonic()
        if self._active and now - self._last_seen > self.stale_after_s:
            self.dropped += 1
            self.reset()
        base = pkt["stream_type"] & 0x7F
        if base not in (H264_TYPE, JPEG_TYPE):
            return None
        frame_size = int(pkt["frame_size"])
        offset = int(pkt["offset"])
        seq = int(pkt["sequence"])
        payload = pkt["payload"]
        if frame_size <= 0 or frame_size > self.max_frame_size or offset < 0 or not payload:
            return None
        if self._active and seq != self._seq:
            if not self._is_complete():
                self.dropped += 1
            self.reset()
        if not self._active:
            self._buf = bytearray(frame_size)
            self._expected = frame_size
            self._ranges = []
            self._stream_type = pkt["stream_type"]
            self._seq = seq
            self._ts = pkt["timestamp"]
            self._active = True
        end = offset + len(payload)
        if offset >= self._expected:
            return None
        if end > self._expected:
            payload = payload[: self._expected - offset]
            end = self._expected
        self._buf[offset:end] = payload
        self._ranges.append((offset, end))
        self._last_seen = now
        if self._is_complete():
            out = {
                "stream_type": self._stream_type & 0x7F,
                "data": bytes(self._buf[: self._expected]),
                "sequence": self._seq,
                "timestamp": self._ts,
            }
            self.completed += 1
            self.reset()
            return out
        return None


def find_annex_b_offset(data: bytes) -> int:
    for sc in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        idx = data.find(sc)
        if idx >= 0:
            return idx
    return 0


def ensure_start_code(data: bytes) -> bytes:
    if data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01"):
        return data
    return b"\x00\x00\x00\x01" + data


def nal_types(data: bytes) -> list[int]:
    types: list[int] = []
    i = 0
    n = len(data)
    while i + 3 < n:
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            sc = 4
        elif data[i : i + 3] == b"\x00\x00\x01":
            sc = 3
        else:
            i += 1
            continue
        if i + sc < n:
            types.append(data[i + sc] & 0x1F)
        i += sc
    return types


class VideoPipeline:
    def __init__(
        self,
        on_frame: FrameCb,
        on_state: StateCb,
        on_stats: StatsCb,
        on_log: LogCb,
    ) -> None:
        self._on_frame = on_frame
        self._on_state = on_state
        self._on_stats = on_stats
        self._log = on_log

        self._stop = threading.Event()
        self._udp_thread: threading.Thread | None = None
        self._decode_thread: threading.Thread | None = None
        self._frames: queue.Queue = queue.Queue(maxsize=12)
        self._lock = threading.RLock()
        self._active = False
        self._reset_requested = True
        self._codec = None
        self._wait_keyframe = True
        self._latest: np.ndarray | None = None
        self._latest_at = 0.0
        self._state = "waiting"
        self._last_state_emit = 0.0

        self._udp_packets = 0
        self._udp_bytes = 0
        self._frames_decoded = 0
        self._frames_dropped = 0
        self._decode_failures = 0
        self._last_stats_at = time.monotonic()
        self._last_stats_packets = 0
        self._last_stats_bytes = 0
        self._last_frame_at = 0.0
        self._first_frame_seen = False
        self._resolution = "—"

    @property
    def latest_frame(self) -> np.ndarray | None:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def start(self) -> None:
        if self._udp_thread and self._udp_thread.is_alive():
            return
        self._stop.clear()
        self._udp_thread = threading.Thread(target=self._udp_loop, name="video-udp", daemon=True)
        self._decode_thread = threading.Thread(target=self._decode_loop, name="video-decode", daemon=True)
        self._udp_thread.start()
        self._decode_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._udp_thread, self._decode_thread):
            if thread:
                thread.join(timeout=2.0)
        with self._lock:
            self._latest = None

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = bool(active)
            self._reset_requested = True
            self._wait_keyframe = True
            if not active:
                self._latest = None
                self._latest_at = 0.0
                self._first_frame_seen = False
        if hasattr(self, "_reassembler"):
            self._reassembler.reset()
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break
        self._set_state("recovering" if active else "waiting")

    def _set_state(self, state: str) -> None:
        now = time.monotonic()
        with self._lock:
            if self._state == state and now - self._last_state_emit < 0.5:
                return
            self._state = state
            self._last_state_emit = now
        self._on_state(state)

    def _udp_loop(self) -> None:
        sock = None
        while not self._stop.is_set():
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
                    except OSError:
                        pass
                    sock.bind(("0.0.0.0", config.VIDEO_PORT))
                    sock.settimeout(1.0)
                    self._log("INFO", f"视频端口已监听 UDP {config.VIDEO_PORT}")
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                self._log("WARN", f"视频端口异常：{exc}")
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
                time.sleep(0.5)
                continue

            with self._lock:
                self._udp_packets += 1
                self._udp_bytes += len(data)
                active = self._active
            if not active:
                continue
            for pkt in iter_jl_chunks(data):
                assembled = self._reassembler_push(pkt)
                if assembled is None:
                    continue
                try:
                    self._frames.put_nowait(assembled)
                except queue.Full:
                    try:
                        self._frames.get_nowait()
                        self._frames.put_nowait(assembled)
                    except Exception:
                        pass
                    with self._lock:
                        self._frames_dropped += 1
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _reassembler_push(self, pkt: dict):
        if not hasattr(self, "_reassembler"):
            self._reassembler = FrameReassembler()
        return self._reassembler.push_packet(pkt)

    def _reset_codec(self) -> None:
        try:
            codec = av.CodecContext.create("h264", "r")
            try:
                codec.thread_type = "FRAME"
                codec.thread_count = 2
            except Exception:
                pass
            self._codec = codec
        except Exception as exc:
            self._codec = None
            self._log("ERROR", f"创建 H.264 解码器失败：{exc}")
        self._wait_keyframe = True

    def _decode_loop(self) -> None:
        while not self._stop.is_set():
            self._maybe_emit_stats()
            with self._lock:
                reset = self._reset_requested
                if reset:
                    self._reset_requested = False
            if reset:
                self._reset_codec()
            try:
                item = self._frames.get(timeout=0.2)
            except queue.Empty:
                continue
            # A reconnect can request a reset while this thread is blocked in
            # queue.get(). Apply it before decoding the frame that just woke us,
            # otherwise that first keyframe is lost and the decoder waits for a
            # keyframe that already passed.
            with self._lock:
                reset = self._reset_requested
                if reset:
                    self._reset_requested = False
            if reset:
                self._reset_codec()
            if not self._active:
                continue
            blob = item["data"]
            stream_type = item["stream_type"]
            if stream_type == JPEG_TYPE:
                raw = blob[blob.find(b"\xff\xd8") :] if b"\xff\xd8" in blob else blob
                img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    self._publish(img)
                continue

            bitstream = ensure_start_code(blob[find_annex_b_offset(blob) :])
            types = nal_types(bitstream)
            if self._wait_keyframe and not (5 in types or 7 in types):
                continue
            if types and (5 in types or 7 in types):
                self._wait_keyframe = False
            if self._codec is None:
                self._reset_codec()
            try:
                for packet in self._codec.parse(bitstream):
                    for frame in self._codec.decode(packet):
                        img = frame.to_ndarray(format="bgr24")
                        img = np.ascontiguousarray(img)
                        self._publish(img)
                        self._set_state("live")
                        self._resolution = f"{frame.width}×{frame.height}"
            except Exception as exc:
                self._decode_failures += 1
                if self._decode_failures <= 5 or self._decode_failures % 50 == 0:
                    self._log("WARN", f"H.264 解码失败：{exc}")
                self._reset_codec()

    def _publish(self, img: np.ndarray) -> None:
        with self._lock:
            self._latest = img
            self._latest_at = time.monotonic()
            self._last_frame_at = time.monotonic()
            self._frames_decoded += 1
            self._first_frame_seen = True
        try:
            self._on_frame(img)
        except Exception as exc:
            self._log("ERROR", f"预览回调失败：{exc}")

    def _maybe_emit_stats(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_stats_at < 1.0:
                if (
                    self._active
                    and self._first_frame_seen
                    and now - self._latest_at > 2.5
                    and self._state == "live"
                ):
                    self._set_state("stalled")
                return
            elapsed = max(0.001, now - self._last_stats_at)
            pps = (self._udp_packets - self._last_stats_packets) / elapsed
            mbps = (self._udp_bytes - self._last_stats_bytes) * 8 / elapsed / 1e6
            fps = self._frames_decoded / elapsed
            self._last_stats_at = now
            self._last_stats_packets = self._udp_packets
            self._last_stats_bytes = self._udp_bytes
            self._frames_decoded = 0
            stats = {
                "fps": fps,
                "mbps": mbps,
                "pps": pps,
                "udp_packets": self._udp_packets,
                "frames_dropped": self._frames_dropped,
                "decode_failures": self._decode_failures,
                "resolution": self._resolution,
                "last_frame_age": None if not self._latest_at else now - self._latest_at,
                "state": self._state,
            }
        self._on_stats(stats)
