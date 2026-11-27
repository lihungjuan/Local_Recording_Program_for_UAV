from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import config


def prepare_media_root(path: Path | str) -> Path:
    """Create and validate a writable directory for photos and videos."""
    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError:
        pass
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{target}")
    probe = target / ".uav_gcs_write_test"
    try:
        probe.write_bytes(b"")
    except OSError as exc:
        raise OSError(f"目录不可写：{target}（{exc}）") from exc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return target


@dataclass
class AppSettings:
    media_root: Path
    timestamp_watermark: bool = True

    @classmethod
    def load(cls) -> "AppSettings":
        raw_path = None
        watermark = True
        try:
            if config.UI_SETTINGS_PATH.exists():
                data = json.loads(config.UI_SETTINGS_PATH.read_text(encoding="utf-8"))
                raw_path = data.get("media_root")
                watermark = bool(data.get("timestamp_watermark", True))
        except Exception:
            raw_path = None
            watermark = True

        if raw_path:
            try:
                return cls(
                    media_root=prepare_media_root(raw_path),
                    timestamp_watermark=watermark,
                )
            except Exception:
                pass
        try:
            media = prepare_media_root(config.RECORDINGS_DIR)
        except Exception:
            media = config.RECORDINGS_DIR
            media.mkdir(parents=True, exist_ok=True)
        return cls(media_root=media, timestamp_watermark=watermark)

    def save(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "media_root": str(self.media_root),
            "timestamp_watermark": bool(self.timestamp_watermark),
        }
        config.UI_SETTINGS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set_media_root(self, path: Path | str) -> Path:
        target = prepare_media_root(path)
        self.media_root = target
        self.save()
        return target
