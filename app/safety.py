from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import config
from .protocol.telemetry import Telemetry


@dataclass(slots=True)
class SafetySnapshot:
    status: str = "未连接"
    color: str = "#7b8794"
    warnings: list[str] = field(default_factory=list)
    link_state: str = "disconnected"
    armed: bool = False
    auto_reason: str | None = None
    flying: bool = False
    link_lost_elapsed: float | None = None
    link_lost_expected_s: float = config.LINK_LOSS_EXPECTED_S
    battery_warning: bool = False
    battery_critical: bool = False
    battery_lockout: bool = False
    conflict: bool = False
    can_arm: bool = False
    can_takeoff: bool = False
    can_land: bool = False
    can_move: bool = False
    telemetry_age: float | None = None
    video_state: str = "waiting"


class SafetyController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.telemetry: Telemetry | None = None
        self.telemetry_at: float | None = None
        self.link_state = "disconnected"
        self.link_lost_at: float | None = None
        self.connected_at: float | None = None
        self.reconnect_at: float | None = None
        self.reconnect_alt: int | None = None
        self.armed = False
        self.auto_reason: str | None = None
        self.last_auto_reason: str | None = None
        self.battery_lockout = False
        self.conflict = False
        self.video_state = "waiting"
        self.battery_warning = False
        self.battery_critical = False
        self.battery_level: int | None = None
        self._alt_history: deque[tuple[float, int]] = deque(maxlen=200)
        self._landed_since: float | None = None
        self._battery_critical_in_flight = False
        self._last_user_input_at = 0.0

    def set_video_state(self, state: str) -> None:
        with self._lock:
            self.video_state = state

    def update_battery_level(self, level: int | None) -> None:
        with self._lock:
            self.battery_level = level
            self._recompute_battery()

    def note_user_input(self) -> None:
        with self._lock:
            self._last_user_input_at = time.monotonic()

    def on_link_connecting(self) -> None:
        with self._lock:
            self.link_state = "connecting"

    def on_link_connected(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self.link_state = "connected"
            self.connected_at = now
            self.reconnect_at = now
            self.reconnect_alt = None
            if self.link_lost_at is not None:
                elapsed = now - self.link_lost_at
                if elapsed >= config.LINK_LOSS_EXPECTED_S:
                    self.auto_reason = "link"
                else:
                    self.link_state = "reconnected_observing"
            self.armed = False

    def on_link_lost(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self.link_state = "link_lost"
            self.link_lost_at = now
            self.reconnect_at = None
            self.reconnect_alt = None
            self.armed = False
            self.auto_reason = None

    def on_reconnecting(self) -> None:
        with self._lock:
            if self.link_state != "reconnected_observing":
                self.link_state = "reconnecting"

    def _set_idle_state(self, state: str, preserve_auto: bool = False) -> None:
        self.link_state = state
        self.link_lost_at = None
        self.connected_at = None
        self.reconnect_at = None
        self.reconnect_alt = None
        self.armed = False
        if not preserve_auto:
            self.auto_reason = None

    def on_disconnected(self) -> None:
        with self._lock:
            self._set_idle_state("disconnected")

    def on_connect_timeout(self) -> None:
        with self._lock:
            # A link-loss auto-descend, once detected, stays latched so that a
            # later reconnect cannot silently re-enable control.
            self._set_idle_state("connect_timeout", preserve_auto=True)

    def on_no_hotspot(self) -> None:
        with self._lock:
            self._set_idle_state("no_hotspot")

    def on_conflict(self, conflict: bool = True) -> None:
        with self._lock:
            self.conflict = bool(conflict)
            if conflict:
                self.armed = False

    def clear_conflict(self) -> None:
        with self._lock:
            self.conflict = False

    def update_telemetry(self, t: Telemetry, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self.telemetry = t
            self.telemetry_at = now
            if self.battery_level is not None:
                t.battery_level = self.battery_level
            self._alt_history.append((now, t.alt_cm))
            while self._alt_history and now - self._alt_history[0][0] > 6.0:
                self._alt_history.popleft()
            self._recompute_battery()
            if t.flying and self.battery_critical:
                self._battery_critical_in_flight = True

            if t.alt_cm <= 2 and t.fly_status == 0:
                if self._landed_since is None:
                    self._landed_since = now
                if self.battery_critical or self._battery_critical_in_flight:
                    self.battery_lockout = True
            else:
                self._landed_since = None

            if self.link_state == "reconnected_observing":
                if self.reconnect_alt is None:
                    self.reconnect_alt = t.alt_cm
                if self.reconnect_alt is not None and t.alt_cm < self.reconnect_alt - 15:
                    self.auto_reason = "link"
                if t.alt_cm <= 2 and t.fly_status == 0 and self.auto_reason != "link":
                    self.link_state = "connected"
                    self.link_lost_at = None
                    self.reconnect_alt = None

            if self.auto_reason is None and t.flying:
                descent = self._descent_cm(now, 2.0)
                if descent <= -15:
                    if self.reconnect_alt is not None and t.alt_cm < self.reconnect_alt - 15:
                        self.auto_reason = "link"

            if self.auto_reason is not None and t.alt_cm <= 2 and t.fly_status == 0:
                self.last_auto_reason = self.auto_reason
                self.auto_reason = None
                self.armed = False
                if self.last_auto_reason == "link":
                    self.link_lost_at = None
                    self.link_state = "connected"
                    self.reconnect_alt = None
                self._landed_since = now

    def tick(self, now: float | None = None) -> SafetySnapshot:
        now = time.monotonic() if now is None else now
        with self._lock:
            if (
                self.link_lost_at is not None
                and self.link_state in ("link_lost", "reconnecting")
                and now - self.link_lost_at >= config.LINK_LOSS_EXPECTED_S
                and self.auto_reason is None
            ):
                self.auto_reason = "link"
            return self.snapshot(now)

    def snapshot(self, now: float | None = None) -> SafetySnapshot:
        now = time.monotonic() if now is None else now
        with self._lock:
            t = self.telemetry
            age = None if self.telemetry_at is None else max(0.0, now - self.telemetry_at)
            flying = bool(t and t.flying)
            link_lost_elapsed = None
            if self.link_lost_at is not None and self.link_state in ("link_lost", "reconnecting"):
                link_lost_elapsed = max(0.0, now - self.link_lost_at)

            can_arm = self._can_arm(now, age)
            can_takeoff = (
                self.armed
                and not flying
                and self.link_state == "connected"
                and not self.auto_reason
                and not self.battery_warning
                and not self.battery_critical
                and not self.battery_lockout
                and not self.conflict
            )
            can_land = self.armed and flying and not self.auto_reason and not self.conflict
            can_move = (
                self.armed
                and flying
                and not self.auto_reason
                and not self.conflict
                and self.link_state in ("connected", "reconnected_observing")
            )

            status, color = self._status(now, flying)
            warnings = self._warnings(t, age)
            return SafetySnapshot(
                status=status,
                color=color,
                warnings=warnings,
                link_state=self.link_state,
                armed=self.armed,
                auto_reason=self.auto_reason,
                flying=flying,
                link_lost_elapsed=link_lost_elapsed,
                link_lost_expected_s=config.LINK_LOSS_EXPECTED_S,
                battery_warning=self.battery_warning,
                battery_critical=self.battery_critical,
                battery_lockout=self.battery_lockout,
                conflict=self.conflict,
                can_arm=can_arm,
                can_takeoff=can_takeoff,
                can_land=can_land,
                can_move=can_move,
                telemetry_age=age,
                video_state=self.video_state,
            )

    def arm(self) -> tuple[bool, str]:
        with self._lock:
            snap = self.snapshot()
            if not snap.can_arm:
                return False, "当前状态不允许接管"
            self.armed = True
            self._last_user_input_at = time.monotonic()
            return True, "控制已接管"

    def disarm(self) -> None:
        with self._lock:
            self.armed = False

    def on_battery_replaced(self) -> tuple[bool, str]:
        with self._lock:
            if self.battery_critical:
                return False, "电压仍处于临界值"
            self.battery_lockout = False
            self._battery_critical_in_flight = False
            return True, "电池锁定已解除"

    def _can_arm(self, now: float, age: float | None) -> bool:
        if self.conflict or self.auto_reason or self.battery_lockout:
            return False
        if self.link_state not in ("connected", "reconnected_observing"):
            return False
        if age is None or age > 1.5:
            return False
        base_time = self.reconnect_at if self.link_state == "reconnected_observing" else self.connected_at
        if base_time is not None and now - base_time < config.CONTROL_ARM_STABLE_S:
            return False
        t = self.telemetry
        if t is None:
            return False
        if not t.flying:
            return t.alt_cm <= 5
        # In-flight takeover is allowed only after a stable link and no descent.
        return self._descent_cm(now, 2.0) > -10

    def _recompute_battery(self) -> None:
        t = self.telemetry
        if t is None:
            self.battery_warning = False
            self.battery_critical = False
            return
        level = self.battery_level
        self.battery_warning = (
            t.battery_mv <= config.BATTERY_WARN_MV
            or t.battery_raw <= config.BATTERY_WARN_RAW
            or (level is not None and level <= 2)
        )
        self.battery_critical = (
            t.battery_mv <= config.BATTERY_CRITICAL_MV
            or t.battery_raw <= config.BATTERY_CRITICAL_RAW
            or (level is not None and level <= config.BATTERY_BLOCK_LEVEL)
        )
        if self.battery_critical:
            self.battery_warning = True

    def _descent_cm(self, now: float, window_s: float) -> int:
        if len(self._alt_history) < 2:
            return 0
        oldest = None
        for ts, alt in self._alt_history:
            if now - ts <= window_s:
                oldest = (ts, alt)
                break
        if oldest is None:
            oldest = self._alt_history[0]
        return self.telemetry.alt_cm - oldest[1] if self.telemetry else 0

    def _status(self, now: float, flying: bool) -> tuple[str, str]:
        if self.conflict:
            return "控制端冲突", "#ff5d5d"
        if self.link_state == "no_hotspot":
            return "未连接热点", "#ff5d5d"
        if self.link_state == "connect_timeout":
            return "连接超时", "#f0b35a"
        if self.auto_reason == "link":
            return "断联自动缓降", "#ff8a3d"
        if self.link_state == "link_lost":
            if self.link_lost_at is not None:
                elapsed = now - self.link_lost_at
                if elapsed >= config.LINK_LOSS_EARLY_S:
                    return "可能已开始缓降", "#ff8a3d"
            return "控制链路丢失", "#ff5d5d"
        if self.link_state == "reconnecting":
            return "重连中", "#f0b35a"
        if self.link_state == "reconnected_observing":
            return "已重连 · 观察中", "#f0b35a"
        if self.battery_lockout:
            return "低电落地 · 需换电", "#ff8a3d"
        if self.battery_critical:
            return "电池临界", "#ff5d5d"
        if self.battery_warning:
            return "低电量", "#f0b35a"
        if flying:
            return "飞行中", "#37d6c0" if self.armed else "#f0b35a"
        if self.armed:
            return "已接管", "#37d6c0"
        if self.link_state == "connected":
            return "已连接", "#37d6c0"
        return "未连接", "#7b8794"

    def _warnings(self, t: Telemetry | None, age: float | None) -> list[str]:
        warnings: list[str] = []
        if self.conflict:
            warnings.append("检测到可能的另一控制端，本程序已撤销控制。")
        if self.link_state == "link_lost" and self.link_lost_at is not None:
            elapsed = time.monotonic() - self.link_lost_at
            remain = max(0.0, config.LINK_LOSS_EXPECTED_S - elapsed)
            warnings.append(f"控制链路丢失，预计约 {remain:.0f} s 后飞控自动缓降。")
        if self.link_state == "reconnected_observing":
            warnings.append("已重连，但不自动恢复控制；正在观察飞控是否已进入缓降。")
        if self.link_state == "no_hotspot":
            warnings.append("未检测到无人机热点，请先在系统 Wi-Fi 中连接无人机热点。")
        if self.link_state == "connect_timeout":
            try:
                window = float(config.PROFILE["link"]["connect_total_timeout_ms"]) / 1000.0
            except (KeyError, TypeError, ValueError):
                window = config.CONNECT_TOTAL_TIMEOUT_S
            warnings.append(
                f"约 {window:.0f} s 内未能连接无人机，已停止重试；"
                "请检查无人机电源后点击“重新连接”。"
            )
        if self.auto_reason == "link":
            warnings.append("飞控可能已因断链自动缓降，飞行控制已锁定。")
        if self.battery_lockout:
            warnings.append("电池临界落地后禁止起飞，请更换电池或确认电压恢复。")
        elif self.battery_critical:
            warnings.append("电池临界，程序仅告警、不自动锁定飞行控制；如在空中请立即降落。")
        elif self.battery_warning:
            warnings.append("低电量，建议尽快降落；起飞已禁用，飞行控制保持可用。")
        if age is not None and age > config.TELEMETRY_TIMEOUT_S and self.link_state in ("connected", "reconnected_observing"):
            warnings.append("遥测超时，正在确认链路状态。")
        return warnings
