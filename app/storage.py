from __future__ import annotations

import datetime as _dt
import shutil
import sqlite3
import threading
from pathlib import Path

import cv2

from . import config
from .settings import prepare_media_root

_media_root = config.RECORDINGS_DIR


def set_media_root(path: Path | str) -> Path:
    global _media_root
    _media_root = prepare_media_root(path)
    return _media_root


def media_root() -> Path:
    return _media_root


class MediaIndex:
    def __init__(self, db_path: Path = config.DB_PATH) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                duration_s REAL,
                size_bytes INTEGER,
                note TEXT
            )
            """
        )
        self._conn.commit()

    def add(self, kind: str, path: Path, started_at: str, note: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO media(kind,path,started_at,note) VALUES(?,?,?,?)",
                (kind, str(path), started_at, note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish(self, row_id: int, ended_at: str, duration_s: float, size_bytes: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE media SET ended_at=?,duration_s=?,size_bytes=? WHERE id=?",
                (ended_at, duration_s, size_bytes, row_id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def today_dir() -> Path:
    day = _dt.datetime.now().strftime("%Y-%m-%d")
    path = _media_root / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def disk_free_gb(path: Path | None = None) -> float:
    target = path or _media_root
    target.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(str(target)).free / (1024 ** 3)


def snapshot(
    frame_bgr,
    index: MediaIndex,
    note: str = "",
    timestamp: bool = True,
) -> Path:
    folder = today_dir()
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = folder / f"snap_{stamp}.jpg"
    image = frame_bgr
    if timestamp:
        from .watermark import draw_timestamp

        image = draw_timestamp(frame_bgr)
    if not cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
        raise OSError(f"无法写入照片: {path}")
    index.add("photo", path, _dt.datetime.now().isoformat(timespec="seconds"), note)
    return path
