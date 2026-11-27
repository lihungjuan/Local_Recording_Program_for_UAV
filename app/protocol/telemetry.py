from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

FLY_LABELS = {
    0: "锁定 / 落地",
    1: "飞行中",
    2: "起飞中",
    3: "降落中",
}


@dataclass(slots=True)
class Telemetry:
    flow_x: int = 0
    flow_y: int = 0
    yaw: int = 0
    alt_cm: int = 0
    battery_mv: int = 0
    battery_raw: int = 0
    flags: int = 0
    fly_status: int = 0
    photo: bool = False
    video: bool = False
    flow_ok: bool = False
    ins_calib: bool = False
    bit7: bool = False
    raw: bytes = b""
    monotonic: float = 0.0
    wall_time: float = 0.0
    battery_level: int | None = None
    sd_online: bool | None = None

    @property
    def flying(self) -> bool:
        return self.fly_status != 0 or self.alt_cm > 10

    @property
    def fly_label(self) -> str:
        return FLY_LABELS.get(self.fly_status, f"状态 {self.fly_status}")

    @property
    def battery_bars(self) -> int:
        if self.battery_level is not None:
            return max(0, min(4, int(self.battery_level)))
        raw = self.battery_raw
        if raw >= 75:
            return 4
        if raw >= 45:
            return 3
        if raw >= 15:
            return 2
        if raw > 0:
            return 1
        return 0

    @property
    def raw_hex(self) -> str:
        return self.raw.hex(" ")


def decode_telemetry(payload: Any) -> Telemetry | None:
    if not isinstance(payload, dict):
        return None
    param = payload.get("param") or {}
    try:
        raw = bytes(int(param[f"D{i}"]) & 0xFF for i in range(16))
    except Exception:
        return None
    if len(raw) != 16 or raw[0] != 0xCC or raw[15] != 0x33 or raw[13] != 0xAA:
        return None
    calc = (255 - (sum(raw[1:14]) & 0xFF)) & 0xFF
    if calc != raw[14]:
        return None

    flow_x = int.from_bytes(raw[1:3], "little", signed=True)
    flow_y = int.from_bytes(raw[3:5], "little", signed=True)
    yaw = int.from_bytes(raw[5:7], "little", signed=True)
    packed = raw[7] | (raw[8] << 8) | (raw[9] << 16)
    alt_cm = packed & 0x0FFF
    battery_mv = ((packed >> 12) & 0x0FFF) * 10
    battery_raw = raw[10]
    flags = raw[11] | (raw[12] << 8)

    return Telemetry(
        flow_x=flow_x,
        flow_y=flow_y,
        yaw=yaw,
        alt_cm=alt_cm,
        battery_mv=battery_mv,
        battery_raw=battery_raw,
        flags=flags,
        fly_status=(flags >> 12) & 3,
        photo=bool((flags >> 9) & 1),
        video=bool((flags >> 10) & 1),
        flow_ok=bool((flags >> 3) & 1),
        ins_calib=bool((flags >> 4) & 1),
        bit7=bool((flags >> 7) & 1),
        raw=raw,
        monotonic=time.monotonic(),
        wall_time=time.time(),
    )


def is_control_ack(payload: Any) -> bool:
    """True for the drone's control-frame confirmation message.

    Observed only while the client is sending GENERIC_CMD control frames:
    {"op":"NOTIFY","param":{"status":"1"}}
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("op") != "NOTIFY":
        return False
    param = payload.get("param")
    if not isinstance(param, dict):
        return False
    return str(param.get("status")) == "1"
