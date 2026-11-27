from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
RECORDINGS_DIR = ROOT / "recordings"
LOGS_DIR = ROOT / "logs"
DB_PATH = ROOT / "media.db"
UI_SETTINGS_PATH = CONFIG_DIR / "ui_settings.json"

APP_NAME = "空地协同 · 无人机地面站"
APP_VERSION = "0.2.0"

DRONE_HOST = "192.168.1.1"
CTP_PORT = 3333
VIDEO_PORT = 2224

VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
VIDEO_FPS = 25

TELEMETRY_TIMEOUT_S = 1.0
LINK_IDLE_TIMEOUT_S = 2.0
LINK_LOSS_EARLY_S = 3.0
LINK_LOSS_EXPECTED_S = 5.0
RECONNECT_INITIAL_S = 0.5
RECONNECT_MAX_S = 2.0
CONNECT_TOTAL_TIMEOUT_S = 20.0
DRONE_SSID_PREFIXES = ("Tspeed7", "Tspeed", "AT66")
CONTROL_INTERVAL_S = 0.025
CONTROL_PULSE_S = 0.12
CONTROL_ARM_STABLE_S = 1.5
TAKEOVER_ACK_TIMEOUT_S = 2.0
MOTOR_COMBO = (45, 36, 39, 212)  # App unlock/lock toggle (D1,D2,D4,D5)
MOTOR_CONFIRM_S = 1.2

BATTERY_WARN_MV = 3500
BATTERY_CRITICAL_MV = 3300
BATTERY_WARN_RAW = 45
BATTERY_CRITICAL_RAW = 15
BATTERY_BLOCK_LEVEL = 1

RECORD_SEGMENT_BYTES = 2 * 1024 * 1024 * 1024
DISK_WARN_GB = 5.0
DISK_MIN_GB = 1.0
RECORD_BITRATE = 8_000_000

DEFAULT_PROFILE = {
    "firmware": {
        "low_battery_auto_descend": True,
        "link_loss_auto_descend": True,
        "link_loss_expected_delay_s": LINK_LOSS_EXPECTED_S,
        "link_loss_early_warning_s": LINK_LOSS_EARLY_S,
    },
    "link": {
        "telemetry_timeout_ms": int(TELEMETRY_TIMEOUT_S * 1000),
        "reconnect_interval_ms": int(RECONNECT_INITIAL_S * 1000),
        "reconnect_max_ms": int(RECONNECT_MAX_S * 1000),
        "connect_total_timeout_ms": int(CONNECT_TOTAL_TIMEOUT_S * 1000),
        "drone_ssid_prefixes": list(DRONE_SSID_PREFIXES),
    },
    "battery": {
        "warn_mv": BATTERY_WARN_MV,
        "critical_mv": BATTERY_CRITICAL_MV,
        "warn_raw": BATTERY_WARN_RAW,
        "critical_raw": BATTERY_CRITICAL_RAW,
        "block_level": BATTERY_BLOCK_LEVEL,
        "post_landing_lockout": "until_replaced_or_recovered",
    },
    "coexistence": {
        "require_exclusive": True,
        "peer_disconnect_assume_auto_descend": True,
    },
    "bit7": {
        "diagnostic_only": True,
    },
}


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_profile() -> dict:
    path = CONFIG_DIR / "safety_profile.json"
    if not path.exists():
        return DEFAULT_PROFILE
    try:
        patch = json.loads(path.read_text(encoding="utf-8"))
        return _merge(DEFAULT_PROFILE, patch)
    except Exception:
        return DEFAULT_PROFILE


PROFILE = load_profile()
