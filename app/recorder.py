from __future__ import annotations

import datetime as _dt
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

import av

from . import config
from .storage import MediaIndex, disk_free_gb, today_dir
from .watermark import draw_timestamp


def _now_stamp() -> str:
    """Filename stamp with millisecond precision (matches snapshot naming)."""
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _allocate_segment_path(folder: Path, stamp: str, segment: int) -> Path:
    """Atomically reserve a unique path for a recording segment.

    ``O_CREAT | O_EXCL`` guarantees two recorders (or a clock rollback) can
    never pick the same filename, so an existing recording is never truncated
    by ``av.open(..., mode="w")``.
    """
    suffix = "" if segment == 1 else f"_part{segment}"
    base = f"rec_{stamp}{suffix}"
    for index in range(1, 1000):
        extra = "" if index == 1 else f"_{index}"
        path = folder / f"{base}{extra}.mp4"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise OSError(f"无法为录像分配唯一文件名：{base}")


class Recorder:
    """Records decoded frames to local H.264 MP4 files.

    Fragmented MP4 is used so a crash or power loss leaves a playable file.
    A new segment is created automatically at the configured size limit.
    """

    def __init__(
        self,
        index: MediaIndex,
        log: Callable[[str, str], None],
        on_stopped: Callable[[str], None] | None = None,
    ) -> None:
        self._index = index
        self._log = log
        self._on_stopped = on_stopped
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue(maxsize=60)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._container = None
        self._stream = None
        self._path: Path | None = None
        self._row_id: int | None = None
        self._started_mono = 0.0
        self._started_wall = ""
        self._pts = 0
        self._frames = 0
        self._dropped = 0
        self._segment = 1
        self._width = config.VIDEO_WIDTH
        self._height = config.VIDEO_HEIGHT
        self._fps = config.VIDEO_FPS
        self._note = ""
        self._timestamp_enabled = True

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def path(self) -> Path | None:
        with self._lock:
            return self._path

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def start(self, frame_bgr, note: str = "", timestamp_enabled: bool = True) -> tuple[bool, str]:
        with self._lock:
            if self._active:
                return False, "已经在录制"
        free = disk_free_gb()
        if free < config.DISK_MIN_GB:
            return False, f"磁盘空间不足：剩余 {free:.1f} GB"
        if free < config.DISK_WARN_GB:
            self._log("WARN", f"磁盘剩余空间较低：{free:.1f} GB")

        height, width = frame_bgr.shape[:2]
        self._width, self._height = int(width), int(height)
        self._note = note
        self._timestamp_enabled = bool(timestamp_enabled)
        self._segment = 1
        self._pts = 0
        self._frames = 0
        self._dropped = 0
        self._stop.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._open_segment()
        except Exception as exc:
            self._log("ERROR", f"无法创建录像文件：{exc}")
            return False, str(exc)
        self._active = True
        self._thread = threading.Thread(target=self._run, name="mp4-recorder", daemon=True)
        self._thread.start()
        self._log("INFO", f"开始录制：{self._path}")
        return True, str(self._path)

    def submit(self, frame_bgr) -> None:
        with self._lock:
            if not self._active:
                return
        item = (frame_bgr, time.time())
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except Exception:
                pass
            with self._lock:
                self._dropped += 1

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self._active:
                return False, "当前没有录制"
            self._active = False
            path = str(self._path)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=8.0)
        self._log("INFO", f"录制已停止：{path}，帧数 {self._frames}，丢帧 {self._dropped}")
        return True, path

    def _open_segment(self) -> None:
        folder = today_dir()
        path = _allocate_segment_path(folder, _now_stamp(), self._segment)
        options = {"movflags": "frag_keyframe+empty_moov"}
        container = None
        try:
            try:
                container = av.open(str(path), mode="w", format="mp4", options=options)
            except Exception:
                container = av.open(str(path), mode="w", format="mp4")
            stream = container.add_stream("libx264", rate=self._fps)
            stream.width = self._width
            stream.height = self._height
            stream.pix_fmt = "yuv420p"
            stream.codec_context.bit_rate = config.RECORD_BITRATE
            try:
                stream.codec_context.max_b_frames = 0
            except Exception:
                pass
            try:
                stream.codec_context.thread_type = "FRAME"
                stream.codec_context.thread_count = 2
            except Exception:
                pass
            self._container = container
            self._stream = stream
            self._path = path
            self._started_mono = time.monotonic()
            self._started_wall = _dt.datetime.now().isoformat(timespec="seconds")
            note = self._note if self._segment == 1 else f"{self._note} (part {self._segment})"
            self._row_id = self._index.add("video", path, self._started_wall, note)
            self._pts = 0
            self._log("INFO", f"录像分段：{path}")
        except Exception:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self._container = None
            self._stream = None
            self._path = None
            self._row_id = None
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _finish_segment(self) -> None:
        container, stream, path, row_id = self._container, self._stream, self._path, self._row_id
        if container is None or stream is None:
            return
        try:
            for pkt in stream.encode():
                container.mux(pkt)
        except Exception as exc:
            self._log("WARN", f"录像收尾失败：{exc}")
        try:
            container.close()
        except Exception:
            pass
        if path and row_id is not None:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._index.finish(
                row_id,
                _dt.datetime.now().isoformat(timespec="seconds"),
                max(0.0, time.monotonic() - self._started_mono),
                size,
            )
        self._container = None
        self._stream = None
        self._path = None
        self._row_id = None

    def _rotate_if_needed(self) -> None:
        path = self._path
        if path is None:
            return
        try:
            if path.stat().st_size < config.RECORD_SEGMENT_BYTES:
                return
        except OSError:
            return
        self._log("WARN", "录像达到 2 GB，正在切换新分段")
        self._finish_segment()
        self._segment += 1
        self._open_segment()

    def _run(self) -> None:
        failed_message = ""
        while True:
            item = self._queue.get()
            if item is None:
                break
            if item is False:
                continue
            try:
                frame_bgr, timestamp = item
                if self._timestamp_enabled:
                    frame_bgr = draw_timestamp(frame_bgr, timestamp)
                frame = av.VideoFrame.from_ndarray(frame_bgr, format="bgr24")
                frame.pts = self._pts
                self._pts += 1
                for pkt in self._stream.encode(frame):
                    self._container.mux(pkt)
                with self._lock:
                    self._frames += 1
                self._rotate_if_needed()
            except Exception as exc:
                failed_message = f"录像编码失败：{exc}"
                self._log("ERROR", failed_message)
                with self._lock:
                    self._active = False
                break
        self._finish_segment()
        if failed_message and self._on_stopped is not None:
            try:
                self._on_stopped(failed_message)
            except Exception:
                pass
