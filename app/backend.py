from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from . import config, storage
from .control import ControlFrameScheduler
from .link import LinkManager
from .protocol.telemetry import Telemetry, decode_telemetry, is_control_ack
from .recorder import Recorder
from .safety import SafetyController, SafetySnapshot
from .settings import AppSettings
from .storage import MediaIndex, snapshot as save_snapshot
from .video import VideoPipeline


class Backend(QObject):
    sig_link_state = Signal(str, str)
    sig_telemetry = Signal(object)
    sig_frame = Signal(QImage)
    sig_video_state = Signal(str)
    sig_stats = Signal(dict)
    sig_log = Signal(str, str)
    sig_safety = Signal(object)
    sig_recording = Signal(bool, str)
    sig_video_meta = Signal(dict)
    sig_takeover_state = Signal(str, str)
    sig_media_dir_changed = Signal(str)
    sig_motor_action = Signal(bool, str)
    sig_watermark_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings.load()
        try:
            storage.set_media_root(self.settings.media_root)
        except Exception:
            storage.set_media_root(config.RECORDINGS_DIR)
        self.media_dir = str(storage.media_root())
        self.watermark_enabled = bool(self.settings.timestamp_watermark)
        self.index = MediaIndex()
        self.safety = SafetyController()
        self.latest_telemetry: Telemetry | None = None
        self.latest_stats: dict = {}
        self.ssid = ""
        self.sd_online: bool | None = None
        self.drone_info = ""
        self._closing = False
        self._manual_disconnect = False
        self._last_connected_at = 0.0
        self._short_link_events = 0
        self._control_engaged = False
        self._control_confirmed = False
        self._takeover_pending = False
        self._takeover_acked = False
        self._auto_reconnect = False

        self.link = LinkManager(
            on_state=self._on_link_state,
            on_topic=self._on_topic,
            on_log=self._log,
        )
        self.video = VideoPipeline(
            on_frame=self._on_frame,
            on_state=self._on_video_state,
            on_stats=self._on_stats,
            on_log=self._log,
        )
        self.scheduler = ControlFrameScheduler(send_raw=self.link.send_raw)
        self.scheduler.set_enabled(False)
        self.recorder = Recorder(self.index, self._log, on_stopped=self._on_recorder_stopped)

        self._takeover_timer = QTimer(self)
        self._takeover_timer.setSingleShot(True)
        self._takeover_timer.setInterval(int(config.TAKEOVER_ACK_TIMEOUT_S * 1000))
        self._takeover_timer.timeout.connect(self._on_takeover_timeout)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.video.start()
        self.scheduler.start()

    # ---------------- lifecycle ----------------

    def connect_drone(self) -> None:
        self._log("INFO", "开始连接无人机（默认只读，不发送控制帧）")
        self._auto_reconnect = False
        self._manual_disconnect = False
        self._reset_control_state("idle", "只读")
        self.safety.on_link_connecting()
        self.sig_link_state.emit("connecting", "正在连接")
        self._emit_safety()
        self.link.start()

    def disconnect_drone(self, force: bool = False) -> bool:
        if not force and self._control_confirmed:
            ok, message = self._lock_before_disconnect()
            if not ok:
                self._log("ERROR", "加锁未确认，已取消断开")
                self.sig_motor_action.emit(False, f"{message}；已取消断开")
                return False
        self._log("INFO", "主动断开无人机控制链路")
        self._auto_reconnect = False
        self._manual_disconnect = True
        self._reset_control_state("idle", "已断开")
        self.link.disconnect()
        self.video.set_active(False)
        self.safety.on_disconnected()
        self.sig_link_state.emit("disconnected", "已断开")
        self._emit_safety()
        return True

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._auto_reconnect = False
        self._takeover_timer.stop()
        self.scheduler.set_enabled(False)
        try:
            self.recorder.stop()
        except Exception:
            pass
        try:
            self.video.stop()
        except Exception:
            pass
        try:
            self.link.disconnect()
        except Exception:
            pass
        try:
            self.scheduler.stop()
        except Exception:
            pass
        try:
            self.index.close()
        except Exception:
            pass

    @property
    def control_confirmed(self) -> bool:
        return self._control_confirmed

    # ---------------- link callbacks ----------------

    def _reset_control_state(self, state: str, detail: str = "") -> None:
        self._control_engaged = False
        self._control_confirmed = False
        self._takeover_pending = False
        self._takeover_acked = False
        self._takeover_timer.stop()
        self.scheduler.set_enabled(False)
        self.scheduler.set_gate(armed=False, flying=False, inhibit=True)
        self.scheduler.center()
        self.safety.disarm()
        self.sig_takeover_state.emit(state, detail)

    def _on_link_state(self, state: str, detail: str) -> None:
        now = time.monotonic()
        if state == "connected":
            self._last_connected_at = now
            self._manual_disconnect = False
            self._reset_control_state("idle", "只读")
            self.safety.on_link_connected()
            self.scheduler.reset_connection()
            self.video.set_active(True)
            self._sync_gate()
            self._log("INFO", f"控制链路已连接（只读）：{detail}")
            self.open_stream(delay=0.25)
        elif state == "link_lost":
            if self.latest_telemetry is not None and self.latest_telemetry.bit7:
                self._log(
                    "WARN",
                    "链路丢失，电机可能在约 10 秒后停止；请远离桨叶并准备断电",
                )
            if not self._manual_disconnect and self._last_connected_at:
                if now - self._last_connected_at < 2.0:
                    self._short_link_events += 1
                    if self._short_link_events >= 3:
                        self.safety.on_conflict(True)
                        self._log(
                            "WARN",
                            "连接在短时间内被反复重置，检测到可能的另一控制端或链路冲突。",
                        )
                        self._short_link_events = 0
            self._reset_control_state("idle", "链路丢失")
            self.safety.on_link_lost()
            self.video.set_active(False)
            self._log("WARN", f"控制链路断开：{detail}")
        elif state == "reconnecting":
            self.safety.on_reconnecting()
            self._sync_gate()
            self._log("WARN", f"正在重连：{detail}")
        elif state == "connecting":
            self.safety.on_link_connecting()
            self._log("INFO", f"正在连接：{detail}")
        elif state == "connect_timeout":
            self._manual_disconnect = True
            self._reset_control_state("idle", "连接超时")
            self.safety.on_connect_timeout()
            self.video.set_active(False)
            self._log("WARN", "连接超时，已停止重试，等待手动重新连接")
        elif state == "no_hotspot":
            self._manual_disconnect = True
            self._reset_control_state("idle", "未连接热点")
            self.safety.on_no_hotspot()
            self.video.set_active(False)
            self._log("WARN", "未检测到无人机热点，已停止重试")
        elif state == "disconnected":
            self._reset_control_state("idle", "已断开")
            self.safety.on_disconnected()
            self.video.set_active(False)
        self.sig_link_state.emit(state, detail)
        self._emit_safety()

    def _on_topic(self, topic: str, payload: Any) -> None:
        if topic == "GENERIC_CMD":
            if is_control_ack(payload):
                self._handle_control_ack()
                return
            t = decode_telemetry(payload)
            if t is not None:
                self.latest_telemetry = t
                self.safety.update_telemetry(t)
                self._sync_gate()
                self.sig_telemetry.emit(t)
                self._emit_safety()
            return
        if topic == "BAT_STATUS" and isinstance(payload, dict):
            try:
                level = int((payload.get("param") or {}).get("level"))
            except Exception:
                level = None
            if level is not None:
                self.safety.update_battery_level(level)
                if self.latest_telemetry is not None:
                    self.latest_telemetry.battery_level = level
            return
        if topic == "SD_STATUS" and isinstance(payload, dict):
            try:
                self.sd_online = str((payload.get("param") or {}).get("online")) == "1"
            except Exception:
                pass
            return
        if topic == "AP_SSID_INFO" and isinstance(payload, dict):
            self.ssid = str((payload.get("param") or {}).get("ssid") or "")
            self._log("INFO", f"无人机热点：{self.ssid or '未知'}")
            return
        if topic == "APP_ACCESS" and isinstance(payload, dict):
            p = payload.get("param") or {}
            self.drone_info = f"{p.get('vendor', '')} {p.get('product', '')} / ver {p.get('ver', '')}"
            self._log("INFO", f"飞控信息：{self.drone_info}")
            return
        if topic == "KEEP_ALIVE_INTERVAL" and isinstance(payload, dict):
            timeout = (payload.get("param") or {}).get("timeout")
            self._log("INFO", f"飞控心跳超时：{timeout} ms")
            return
        if topic in ("OPEN_RT_STREAM", "CLOSE_RT_STREAM"):
            self._log("INFO", f"飞控视频状态：{topic}")

    # ---------------- video callbacks ----------------

    def _on_frame(self, frame_bgr: np.ndarray) -> None:
        img = np.ascontiguousarray(frame_bgr)
        qimg = QImage(
            img.data,
            img.shape[1],
            img.shape[0],
            img.strides[0],
            QImage.Format_BGR888,
        ).copy()
        self.sig_frame.emit(qimg)
        if self.recorder.active:
            self.recorder.submit(img)

    def _on_video_state(self, state: str) -> None:
        self.safety.set_video_state(state)
        self.sig_video_state.emit(state)
        self._emit_safety()

    def _on_stats(self, stats: dict) -> None:
        self.latest_stats = stats
        self.sig_stats.emit(stats)

    # ---------------- user actions ----------------

    def start_takeover(self) -> tuple[bool, str]:
        if not self.link.connected:
            message = "链路未连接，无法接管"
            self._log("WARN", message)
            return False, message
        if self._control_engaged:
            return True, "已处于接管状态"
        ok, message = self.safety.arm()
        if not ok:
            self._log("WARN", f"接管被拒绝：{message}")
            return False, message
        self._control_engaged = True
        self._control_confirmed = False
        self._takeover_pending = True
        self._takeover_acked = False
        self._takeover_timer.stop()
        self._takeover_timer.start()
        self._log(
            "WARN",
            f"开始接管控制，等待飞控确认（{config.TAKEOVER_ACK_TIMEOUT_S:.0f} s）",
        )
        self.sig_takeover_state.emit("pending", "等待飞控确认")
        self.scheduler.reset_connection()
        self.scheduler.set_enabled(True)
        self._sync_gate()
        self._emit_safety()
        return True, "已开始接管，等待飞控确认"

    def _handle_control_ack(self) -> None:
        if (
            not self._control_engaged
            or self._takeover_acked
            or not self._takeover_pending
        ):
            return
        self._takeover_pending = False
        self._takeover_acked = True
        self._takeover_timer.stop()
        self._log("INFO", "收到飞控控制帧确认（status=1，不代表控制权）")
        self.sig_takeover_state.emit(
            "acked", "飞控已接收控制帧，请用遥控器确认控制权"
        )
        self._emit_safety()

    def confirm_takeover(self, confirmed: bool) -> None:
        if not self._control_engaged:
            return
        if confirmed:
            self._control_confirmed = True
            self._takeover_pending = False
            self._takeover_acked = True
            self._takeover_timer.stop()
            self._log("INFO", "人工确认：遥控器不能控制，程序接管")
            self.sig_takeover_state.emit("confirmed", "控制权已人工确认")
            self._emit_safety()
        else:
            self._log("WARN", "人工确认：遥控器仍能控制，接管失败，交还控制")
            self.release_control()

    def _on_takeover_timeout(self) -> None:
        if not self._takeover_pending:
            return
        self._takeover_pending = False
        self.scheduler.center()
        self._log("WARN", "接管超时：未收到飞控确认（status=1）")
        self.sig_takeover_state.emit("timeout", "未收到飞控确认，接管可能失败")
        self._emit_safety()

    def _motor_ready(self) -> tuple[bool, Telemetry | None, str]:
        if not self.link.connected:
            return False, self.latest_telemetry, "链路未连接"
        if not self._control_engaged or not self._control_confirmed:
            return False, self.latest_telemetry, "控制权尚未人工确认"
        t = self.latest_telemetry
        if t is None:
            return False, t, "没有遥测，无法判断电机状态"
        if t.fly_status != 0 or t.alt_cm > 5:
            return False, t, "飞行中禁止解锁/加锁"
        snap = self.safety.snapshot()
        if snap.auto_reason or snap.conflict:
            return False, t, "安全状态不允许切换电机"
        return True, t, ""

    def toggle_motor_lock(self) -> tuple[bool, str]:
        ready, telemetry, message = self._motor_ready()
        if not ready or telemetry is None:
            self._log("WARN", f"电机切换被拒绝：{message}")
            self.sig_motor_action.emit(False, message)
            return False, message
        target_unlocked = not telemetry.bit7
        label = "解锁" if target_unlocked else "加锁"
        self._log("WARN", f"发送电机{label}组合（App 组合）")
        for attempt in range(2):
            if not self.scheduler.set_ground_combo(config.MOTOR_COMBO, 0.55):
                message = "控制帧发送被拦截，无法切换电机状态"
                self._log("ERROR", message)
                self.sig_motor_action.emit(False, message)
                return False, message
            end = time.monotonic() + config.MOTOR_CONFIRM_S
            while time.monotonic() < end:
                time.sleep(0.05)
                cur = self.latest_telemetry
                if cur is not None and cur.bit7 == target_unlocked:
                    message = "电机已解锁" if target_unlocked else "电机已加锁"
                    self._log("INFO", message)
                    self.sig_motor_action.emit(True, message)
                    self._emit_safety()
                    return True, message
            self._log("WARN", f"电机{label}未确认，重试 {attempt + 1}/2")
        message = "电机状态未确认；请用遥控器检查或断电"
        self._log("ERROR", message)
        self.sig_motor_action.emit(False, message)
        return False, message

    def _lock_before_disconnect(self) -> tuple[bool, str]:
        if not self._control_confirmed:
            return True, ""
        t = self.latest_telemetry
        if t is None or not t.bit7:
            return True, ""
        if t.fly_status != 0 or t.alt_cm > 5:
            return True, ""
        return self.toggle_motor_lock()

    def lock_before_close(self) -> tuple[bool, str]:
        if not self._control_confirmed:
            return True, ""
        t = self.latest_telemetry
        if t is None or not t.bit7:
            return True, ""
        if t.fly_status != 0 or t.alt_cm > 5:
            return True, "airborne"
        return self.toggle_motor_lock()

    def release_control(self, force: bool = False) -> bool:
        if not force and self._control_confirmed:
            ok, message = self._lock_before_disconnect()
            if not ok:
                self._log("ERROR", "加锁未确认，已取消交还控制")
                self.sig_motor_action.emit(False, f"{message}；已取消交还控制")
                return False
        was_engaged = self._control_engaged
        self._reset_control_state("released", "已交还控制")
        if was_engaged:
            self._log("INFO", "交还控制：已停止控制帧，断开并只读重连")
        else:
            self._log("INFO", "已停止控制帧")
        if self.link.connected:
            self._auto_reconnect = True
            self.link.disconnect()
            self.video.set_active(False)
            if not self._closing:
                QTimer.singleShot(700, self._reconnect_read_only)
        self._emit_safety()
        return True

    def _reconnect_read_only(self) -> None:
        if self._closing or not self._auto_reconnect:
            return
        self._auto_reconnect = False
        self._log("INFO", "以只读模式重新连接无人机")
        self.connect_drone()

    def arm(self) -> None:
        ok, message = self.start_takeover()
        if not ok:
            self._log("WARN", f"接管失败：{message}")

    def disarm(self) -> None:
        self._reset_control_state("released", "已停止控制")
        self._log("INFO", "已停止控制")

    # ---------------- settings ----------------

    def set_media_dir(self, path: str) -> tuple[bool, str]:
        if self.recorder.active:
            message = "请先停止录制，再切换存储目录"
            self._log("WARN", message)
            return False, message
        try:
            target = self.settings.set_media_root(path)
            storage.set_media_root(target)
        except Exception as exc:
            message = f"无法使用该目录：{exc}"
            self._log("ERROR", message)
            return False, message
        self.media_dir = str(target)
        self._log("INFO", f"存储目录已切换：{target}")
        self.sig_media_dir_changed.emit(self.media_dir)
        return True, self.media_dir

    def set_watermark_enabled(self, enabled: bool) -> tuple[bool, str]:
        if self.recorder.active:
            message = "录制中不能修改时间戳水印，请先停止录制"
            self._log("WARN", message)
            return False, message
        enabled = bool(enabled)
        if enabled == self.watermark_enabled:
            return True, ""
        self.watermark_enabled = enabled
        self.settings.timestamp_watermark = enabled
        try:
            self.settings.save()
        except Exception as exc:
            self._log("WARN", f"保存水印设置失败：{exc}")
        self.sig_watermark_changed.emit(enabled)
        self._log(
            "INFO",
            "已开启时间戳水印（照片/录像）" if enabled else "已关闭时间戳水印（照片/录像）",
        )
        return True, ""

    def takeoff_land(self) -> None:
        if not self._control_confirmed:
            message = "控制权尚未人工确认，禁止起飞/降落"
            self._log("WARN", message)
            self.sig_motor_action.emit(False, message)
            return
        snap = self.safety.snapshot()
        if snap.can_takeoff:
            t = self.latest_telemetry
            if t is None or not t.bit7:
                message = "请先解锁电机"
                self._log("WARN", message)
                self.sig_motor_action.emit(False, message)
                return
            if self.scheduler.pulse_takeoff_land():
                self.safety.note_user_input()
                self._log("INFO", "发送一键起飞脉冲")
            else:
                self._log("WARN", "起飞脉冲被控制闸门拒绝")
        elif snap.can_land:
            if self.scheduler.pulse_takeoff_land():
                self.safety.note_user_input()
                self._log("INFO", "发送一键降落脉冲")
            else:
                self._log("WARN", "降落脉冲被控制闸门拒绝")
        else:
            self._log("WARN", "当前状态不允许起飞/降落")

    def emergency_stop(self) -> None:
        if not self.link.connected or not self._control_engaged:
            self._log("WARN", "当前未接管控制，无法发送紧急停桨")
            return
        self.scheduler.emergency_stop()
        self.safety.disarm()
        self._sync_gate()
        self._emit_safety()
        self._log("WARN", "已发送紧急停桨指令（未验证，谨慎使用）")

    def set_sticks(self, left_x: float, left_y: float, right_x: float, right_y: float) -> None:
        if not self._control_confirmed:
            self.scheduler.center(emit=False)
            return
        snap = self.safety.snapshot()
        if snap.can_move:
            self.scheduler.set_sticks(left_x, left_y, right_x, right_y)
            self.safety.note_user_input()
        else:
            self.scheduler.center(emit=False)

    def open_stream(self, delay: float = 0.0) -> None:
        if delay > 0:
            threading.Thread(target=self._open_stream_after, args=(delay,), daemon=True).start()
        else:
            self._open_stream_now()

    def _open_stream_after(self, delay: float) -> None:
        time.sleep(delay)
        self._open_stream_now()

    def _open_stream_now(self) -> None:
        if not self.link.connected:
            return
        self.video.set_active(True)
        self.link.send_ctp("CLOSE_RT_STREAM", {"op": "PUT", "param": {"status": "0"}})
        time.sleep(0.05)
        self.link.send_ctp(
            "OPEN_RT_STREAM",
            {
                "op": "PUT",
                "param": {
                    "format": "1",
                    "w": str(config.VIDEO_WIDTH),
                    "h": str(config.VIDEO_HEIGHT),
                    "fps": str(config.VIDEO_FPS),
                },
            },
        )
        self._log("INFO", "已请求视频流 1280×720@25")

    def start_recording(self) -> None:
        frame = self.video.latest_frame
        if frame is None:
            self._log("WARN", "没有可用画面，无法开始录制")
            return
        ok, message = self.recorder.start(
            frame,
            note="UAV GCS recording",
            timestamp_enabled=self.watermark_enabled,
        )
        if ok:
            self.sig_recording.emit(True, message)
            self._log("INFO", f"开始本地录制：{message}")
        else:
            self._log("WARN", f"录制失败：{message}")

    def stop_recording(self) -> None:
        ok, message = self.recorder.stop()
        if ok:
            self.sig_recording.emit(False, message)
            self._log("INFO", f"录制文件：{message}")

    def snapshot(self) -> None:
        frame = self.video.latest_frame
        if frame is None:
            self._log("WARN", "没有可用画面，无法拍照")
            return
        try:
            path = save_snapshot(
                frame,
                self.index,
                note="UAV GCS snapshot",
                timestamp=self.watermark_enabled,
            )
            self._log("INFO", f"照片已保存：{path}")
        except Exception as exc:
            self._log("ERROR", f"保存照片失败：{exc}")

    def _on_recorder_stopped(self, message: str) -> None:
        self.sig_recording.emit(False, message)

    def battery_replaced(self) -> None:
        ok, message = self.safety.on_battery_replaced()
        self._sync_gate()
        self._emit_safety()
        self._log("INFO" if ok else "WARN", message)

    def clear_conflict(self) -> None:
        self.safety.clear_conflict()
        self._short_link_events = 0
        self._sync_gate()
        self._emit_safety()
        self._log("INFO", "已清除连接冲突标记，请确认另一控制端已关闭后再接管")

    # ---------------- periodic ----------------

    def _tick(self) -> None:
        now = time.monotonic()
        if self.link.connected and self._last_connected_at and now - self._last_connected_at > 10.0:
            self._short_link_events = 0
        snap = self.safety.tick(now)
        self._sync_gate()
        self.sig_safety.emit(snap)

    def _sync_gate(self) -> None:
        snap = self.safety.snapshot()
        # Until the user explicitly starts takeover, no control frame is sent.
        inhibit = (
            not self.link.connected
            or not self._control_engaged
            or snap.auto_reason is not None
            or snap.conflict
        )
        self.scheduler.set_gate(armed=snap.armed, flying=snap.flying, inhibit=inhibit)

    def _emit_safety(self) -> None:
        self.sig_safety.emit(self.safety.snapshot())

    def _log(self, level: str, message: str) -> None:
        self.sig_log.emit(level, message)
