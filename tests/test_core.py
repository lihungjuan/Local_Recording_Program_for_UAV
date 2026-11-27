from __future__ import annotations

import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

from app import config, storage
from app.control import ControlFrameScheduler
from app.link import LinkManager
from app.protocol.ctp import CTPParser, build_ctp
from app.protocol.telemetry import Telemetry, decode_telemetry, is_control_ack
from app.recorder import Recorder, _allocate_segment_path
from app.safety import SafetyController
from app.settings import AppSettings
from app.storage import MediaIndex
from app.video import FrameReassembler, nal_types
from app.watermark import draw_timestamp, format_timestamp


def make_telemetry_payload(
    *,
    alt_cm: int = 0,
    battery_mv: int = 3720,
    battery_raw: int = 107,
    flags: int = 0,
) -> dict:
    data = [0] * 16
    data[0] = 0xCC
    packed = (alt_cm & 0x0FFF) | (((battery_mv // 10) & 0x0FFF) << 12)
    data[7] = packed & 0xFF
    data[8] = (packed >> 8) & 0xFF
    data[9] = (packed >> 16) & 0xFF
    data[10] = battery_raw
    data[11] = flags & 0xFF
    data[12] = (flags >> 8) & 0xFF
    data[13] = 0xAA
    data[14] = (255 - (sum(data[1:14]) & 0xFF)) & 0xFF
    data[15] = 0x33
    return {"op": "NOTIFY", "param": {f"D{i}": str(v) for i, v in enumerate(data)}}


class ProtocolTests(unittest.TestCase):
    def test_ctp_roundtrip_and_fragmentation(self) -> None:
        packet = build_ctp("GENERIC_CMD", {"op": "PUT", "param": {"D0": "204"}})
        parser = CTPParser()
        parsed = parser.feed(packet[:5]) + parser.feed(packet[5:])
        self.assertEqual(parsed[0][0], "GENERIC_CMD")
        self.assertEqual(parsed[0][1]["param"]["D0"], "204")

    def test_telemetry_decode(self) -> None:
        payload = make_telemetry_payload(alt_cm=92, battery_mv=3720, battery_raw=107, flags=0x1080)
        t = decode_telemetry(payload)
        self.assertIsNotNone(t)
        assert t is not None
        self.assertEqual(t.alt_cm, 92)
        self.assertEqual(t.battery_mv, 3720)
        self.assertEqual(t.battery_raw, 107)
        self.assertEqual(t.fly_status, 1)
        self.assertTrue(t.bit7)
        self.assertTrue(t.flying)

    def test_control_frame_checksum_and_pulse(self) -> None:
        sent: list[bytes] = []
        scheduler = ControlFrameScheduler(lambda b: sent.append(b) or True)
        scheduler.reset_connection()
        scheduler.set_gate(armed=True, flying=False, inhibit=False)
        self.assertTrue(scheduler.pulse_takeoff_land())
        self.assertTrue(scheduler._send_one())
        parsed = CTPParser().feed(sent[-1])[0][1]
        values = [int(parsed["param"][f"D{i}"]) for i in range(14)]
        self.assertEqual(values[0], 204)
        self.assertEqual(values[8], 9)
        self.assertEqual(values[13], 51)
        self.assertEqual(values[12], (255 - (sum(values[1:12]) & 0xFF)) & 0xFF)
        self.assertEqual(values[3], 192)

    def test_frame_reassembler(self) -> None:
        frame = bytes(range(100))
        reassembler = FrameReassembler()
        first = {
            "stream_type": 3,
            "sequence": 7,
            "frame_size": 100,
            "offset": 0,
            "timestamp": 0,
            "payload": frame[:50],
        }
        second = dict(first, offset=50, payload=frame[50:])
        self.assertIsNone(reassembler.push_packet(first))
        result = reassembler.push_packet(second)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["data"], frame)

    def test_nal_types(self) -> None:
        blob = b"\x00\x00\x00\x01\x67" + b"\x00\x00\x00\x01\x68" + b"\x00\x00\x00\x01\x65"
        self.assertEqual(nal_types(blob), [7, 8, 5])

    def test_control_ack_payload(self) -> None:
        self.assertTrue(is_control_ack({"op": "NOTIFY", "param": {"status": "1"}}))
        self.assertFalse(is_control_ack({"op": "PUT", "param": {"D0": "204"}}))
        self.assertFalse(is_control_ack(None))

    def test_disabled_scheduler_does_not_send(self) -> None:
        sent: list[bytes] = []
        scheduler = ControlFrameScheduler(lambda b: sent.append(b) or True)
        scheduler.set_enabled(False)
        scheduler.reset_connection()
        scheduler.set_gate(armed=True, flying=True, inhibit=False)
        self.assertTrue(scheduler._send_one())
        self.assertEqual(sent, [])
        scheduler.set_enabled(True)
        self.assertTrue(scheduler._send_one())
        self.assertEqual(len(sent), 1)

    def test_stick_axes_and_ground_combo(self) -> None:
        sent: list[bytes] = []
        scheduler = ControlFrameScheduler(lambda b: sent.append(b) or True)
        scheduler.reset_connection()
        scheduler.set_gate(armed=True, flying=True, inhibit=False)

        scheduler.set_sticks(0.0, 1.0, 0.0, 1.0)
        scheduler._send_one()
        values = [int(v) for v in CTPParser().feed(sent[-1])[0][1]["param"].values()]
        self.assertGreater(values[1], 128)  # D1: left up = ascend
        self.assertGreater(values[4], 128)  # D4: right up = forward

        scheduler.set_sticks(0.0, -1.0, 0.0, -1.0)
        scheduler._send_one()
        values = [int(v) for v in CTPParser().feed(sent[-1])[0][1]["param"].values()]
        self.assertLess(values[1], 128)
        self.assertLess(values[4], 128)

        scheduler.set_gate(armed=False, flying=False, inhibit=False)
        self.assertTrue(scheduler.set_ground_combo((45, 36, 39, 212), 0.5))
        scheduler._send_one()
        values = [int(v) for v in CTPParser().feed(sent[-1])[0][1]["param"].values()]
        self.assertEqual(values[1:6], [45, 36, values[3], 39, 212])


class StorageTests(unittest.TestCase):
    def test_media_root_controls_snapshot_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            old_root = storage.media_root()
            index = MediaIndex(Path(tmp) / "index.db")
            try:
                storage.set_media_root(root)
                frame = np.zeros((8, 8, 3), dtype=np.uint8)
                path = storage.snapshot(frame, index, note="test")
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, storage.today_dir())
                self.assertTrue(str(path).startswith(str(root)))
            finally:
                index.close()
                storage.set_media_root(old_root)

    def test_snapshot_timestamp_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            old_root = storage.media_root()
            index = MediaIndex(Path(tmp) / "index.db")
            try:
                storage.set_media_root(root)
                frame = np.zeros((32, 32, 3), dtype=np.uint8)
                with mock.patch("app.watermark.draw_timestamp", side_effect=lambda f, *a, **k: f) as draw:
                    storage.snapshot(frame, index, timestamp=False)
                    draw.assert_not_called()
                    storage.snapshot(frame, index, timestamp=True)
                    draw.assert_called_once()
            finally:
                index.close()
                storage.set_media_root(old_root)


class RecorderTests(unittest.TestCase):
    def test_allocate_segment_path_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            stamp = "20261126_215355_123"
            first = _allocate_segment_path(folder, stamp, 1)
            first.write_bytes(b"keep me")
            second = _allocate_segment_path(folder, stamp, 1)
            self.assertNotEqual(first, second)
            self.assertEqual(second.name, "rec_20261126_215355_123_2.mp4")
            self.assertEqual(first.read_bytes(), b"keep me")

    def test_back_to_back_recordings_use_distinct_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = storage.media_root()
            root = Path(tmp) / "media"
            db_path = Path(tmp) / "index.db"
            index = MediaIndex(db_path)
            frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in range(4)]
            recorder = Recorder(index, lambda level, message: None)
            try:
                storage.set_media_root(root)
                with (
                    mock.patch("app.recorder.disk_free_gb", return_value=100.0),
                    mock.patch("app.recorder._now_stamp", return_value="20261126_215355_123"),
                ):
                    paths: list[Path] = []
                    for _ in range(2):
                        ok, message = recorder.start(frames[0], timestamp_enabled=False)
                        self.assertTrue(ok, message)
                        for frame in frames:
                            recorder.submit(frame)
                        ok, message = recorder.stop()
                        self.assertTrue(ok, message)
                        paths.append(Path(message))
                self.assertNotEqual(paths[0], paths[1])
                for path in paths:
                    self.assertTrue(path.exists())
                    self.assertGreater(path.stat().st_size, 0)
                conn = sqlite3.connect(db_path)
                try:
                    rows = conn.execute(
                        "SELECT path, size_bytes FROM media ORDER BY id"
                    ).fetchall()
                finally:
                    conn.close()
                self.assertEqual(len(rows), 2)
                self.assertNotEqual(rows[0][0], rows[1][0])
                self.assertTrue(all(size and size > 0 for _, size in rows))
            finally:
                if recorder.active:
                    recorder.stop()
                index.close()
                storage.set_media_root(old_root)


class WatermarkTests(unittest.TestCase):
    def test_format_timestamp_is_local_seconds(self) -> None:
        when = datetime(2026, 11, 26, 9, 8, 7)
        self.assertEqual(format_timestamp(when), "2026-11-26 09:08:07")

    def test_draw_timestamp_marks_top_right_only(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        out = draw_timestamp(frame, datetime(2026, 11, 26, 9, 8, 7))
        self.assertEqual(int(frame.sum()), 0)
        self.assertGreater(int(out[0:60, 1000:1280].sum()), 0)
        self.assertEqual(int(out[120:220, 80:220].sum()), 0)

    def test_draw_timestamp_disabled_returns_original(self) -> None:
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        out = draw_timestamp(frame, enabled=False)
        self.assertIs(out, frame)
        self.assertEqual(int(out.sum()), 0)


class SettingsTests(unittest.TestCase):
    def test_watermark_setting_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_path = config.UI_SETTINGS_PATH
            config.UI_SETTINGS_PATH = Path(tmp) / "ui.json"
            try:
                root = Path(tmp) / "media"
                settings = AppSettings(media_root=root, timestamp_watermark=False)
                settings.save()
                loaded = AppSettings.load()
                self.assertFalse(loaded.timestamp_watermark)
                self.assertEqual(loaded.media_root, root.resolve())
            finally:
                config.UI_SETTINGS_PATH = old_path


class SafetyTests(unittest.TestCase):
    def test_link_loss_auto_descend_after_5s(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=100.0)
        safety.on_link_lost(now=101.0)
        self.assertIsNone(safety.tick(now=104.0).auto_reason)
        snapshot = safety.tick(now=106.2)
        self.assertEqual(snapshot.auto_reason, "link")
        self.assertFalse(snapshot.can_arm)

    def test_critical_battery_warns_but_keeps_control(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=0.0)
        safety.armed = True
        safety.update_telemetry(
            Telemetry(alt_cm=100, battery_mv=3350, battery_raw=10, fly_status=1, flags=1 << 12),
            now=1.0,
        )
        safety.update_telemetry(
            Telemetry(alt_cm=80, battery_mv=3350, battery_raw=10, fly_status=1, flags=1 << 12),
            now=2.1,
        )
        snapshot = safety.snapshot(now=2.2)
        self.assertIsNone(snapshot.auto_reason)
        self.assertTrue(snapshot.battery_critical)
        self.assertTrue(snapshot.can_move)
        self.assertFalse(snapshot.battery_lockout)
        self.assertTrue(any("仅告警" in w for w in snapshot.warnings))

        safety.update_telemetry(
            Telemetry(alt_cm=0, battery_mv=3600, battery_raw=70, fly_status=0, flags=0),
            now=3.0,
        )
        snapshot = safety.snapshot(now=3.1)
        self.assertTrue(snapshot.battery_lockout)
        self.assertFalse(snapshot.can_takeoff)

        ok, _ = safety.on_battery_replaced()
        self.assertTrue(ok)
        self.assertFalse(safety.snapshot(now=3.2).battery_lockout)

    def test_reconnect_does_not_auto_arm(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=0.0)
        safety.on_link_lost(now=1.0)
        safety.on_link_connected(now=3.0)
        self.assertFalse(safety.snapshot(now=4.0).armed)
        self.assertFalse(safety.snapshot(now=4.0).can_arm)

    def test_conflict_blocks_arm_and_can_be_cleared(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=0.0)
        safety.on_conflict(True)
        self.assertTrue(safety.snapshot(now=2.0).conflict)
        self.assertFalse(safety.snapshot(now=2.0).can_arm)
        safety.clear_conflict()
        self.assertFalse(safety.snapshot(now=2.0).conflict)

    def test_manual_disconnect_clears_link_lost(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=0.0)
        safety.on_link_lost(now=1.0)
        self.assertEqual(safety.snapshot(now=2.0).link_state, "link_lost")
        safety.on_disconnected()
        snapshot = safety.snapshot(now=2.0)
        self.assertEqual(snapshot.link_state, "disconnected")
        self.assertIsNone(snapshot.link_lost_elapsed)
        self.assertFalse(snapshot.can_arm)
        self.assertEqual(snapshot.status, "未连接")

    def test_disconnect_clears_stale_battery_auto_reason(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=0.0)
        # Plan A: low battery never sets an auto-descend latch anymore, so any
        # stale "battery" value must not survive a disconnect.
        safety.auto_reason = "battery"
        safety.on_disconnected()
        snapshot = safety.snapshot(now=1.0)
        self.assertEqual(snapshot.link_state, "disconnected")
        self.assertIsNone(snapshot.auto_reason)

    def test_connect_timeout_and_no_hotspot_states(self) -> None:
        safety = SafetyController()
        safety.on_link_connecting()
        safety.on_connect_timeout()
        snapshot = safety.snapshot()
        self.assertEqual(snapshot.link_state, "connect_timeout")
        self.assertEqual(snapshot.status, "连接超时")
        self.assertTrue(any("重新连接" in w for w in snapshot.warnings))

        safety.on_no_hotspot()
        snapshot = safety.snapshot()
        self.assertEqual(snapshot.link_state, "no_hotspot")
        self.assertEqual(snapshot.status, "未连接热点")
        self.assertTrue(any("无人机热点" in w for w in snapshot.warnings))

    def test_connect_timeout_keeps_link_auto_descend_latch(self) -> None:
        safety = SafetyController()
        safety.on_link_connected(now=0.0)
        safety.on_link_lost(now=1.0)
        safety.tick(now=7.0)
        safety.on_connect_timeout()
        snapshot = safety.snapshot(now=8.0)
        self.assertEqual(snapshot.link_state, "connect_timeout")
        self.assertEqual(snapshot.auto_reason, "link")
        self.assertEqual(snapshot.status, "连接超时")
        self.assertFalse(snapshot.can_arm)


class _FakeCTPServer:
    """Minimal TCP listener that accepts and holds CTP connections."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self.accepted = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            self.accepted += 1
            self._conns.append(conn)
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class LinkTests(unittest.TestCase):
    def _manager(self, states: list[str]) -> LinkManager:
        return LinkManager(
            on_state=lambda state, _detail: states.append(state),
            on_topic=lambda _topic, _payload: None,
            on_log=lambda _level, _message: None,
        )

    def test_connect_timeout_stops_retrying(self) -> None:
        states: list[str] = []
        link = self._manager(states)
        with (
            mock.patch.dict(
                config.PROFILE["link"], {"connect_total_timeout_ms": 200}, clear=False
            ),
            mock.patch.object(config, "RECONNECT_INITIAL_S", 0.01),
            mock.patch.object(config, "RECONNECT_MAX_S", 0.02),
            mock.patch.object(link, "_has_drone_network", return_value=True),
            mock.patch.object(link, "_try_connect", return_value=False),
        ):
            link.start()
            assert link._thread is not None
            link._thread.join(timeout=2.0)
        self.assertFalse(link._thread.is_alive())
        self.assertEqual(states[-1], "connect_timeout")
        self.assertEqual(link._state, "connect_timeout")

    def test_no_hotspot_stops_immediately(self) -> None:
        states: list[str] = []
        link = self._manager(states)
        with mock.patch.object(link, "_has_drone_network", return_value=False):
            link.start()
            assert link._thread is not None
            link._thread.join(timeout=1.0)
        self.assertFalse(link._thread.is_alive())
        self.assertEqual(states[-1], "no_hotspot")
        self.assertEqual(link._state, "no_hotspot")

    def test_hotspot_detection_uses_wlan_ssid(self) -> None:
        with mock.patch.object(LinkManager, "_wlan_ssids", return_value=["Tspeed7-9253f084"]):
            self.assertTrue(LinkManager._has_drone_network())
        with mock.patch.object(LinkManager, "_wlan_ssids", return_value=["HomeWiFi"]):
            self.assertFalse(LinkManager._has_drone_network())
        with mock.patch.object(LinkManager, "_wlan_ssids", return_value=[]):
            self.assertFalse(LinkManager._has_drone_network())
        with mock.patch.object(
            LinkManager, "_wlan_ssids", return_value=["HomeWiFi", "Tspeed7-9253f084"]
        ):
            self.assertTrue(LinkManager._has_drone_network())

    def test_manual_disconnect_cancels_and_can_start_again(self) -> None:
        states: list[str] = []
        link = self._manager(states)
        with (
            mock.patch.dict(
                config.PROFILE["link"], {"connect_total_timeout_ms": 500}, clear=False
            ),
            mock.patch.object(config, "RECONNECT_INITIAL_S", 0.1),
            mock.patch.object(config, "RECONNECT_MAX_S", 0.1),
            mock.patch.object(link, "_has_drone_network", return_value=True),
            mock.patch.object(link, "_try_connect", return_value=False),
        ):
            link.start()
            assert link._thread is not None
            link.disconnect()
            link._thread.join(timeout=1.0)
            self.assertFalse(link._thread.is_alive())
            self.assertEqual(link._state, "disconnected")

            link.start()
            assert link._thread is not None
            link._thread.join(timeout=2.0)
            self.assertFalse(link._thread.is_alive())
            self.assertEqual(link._state, "connect_timeout")

    def test_disconnect_then_reconnect_against_fake_server(self) -> None:
        server = _FakeCTPServer()
        states: list[str] = []
        link = self._manager(states)
        try:
            with (
                mock.patch.object(config, "DRONE_HOST", "127.0.0.1"),
                mock.patch.object(config, "CTP_PORT", server.port),
                mock.patch.object(link, "_has_drone_network", return_value=True),
            ):
                link.start()
                self.assertTrue(_wait_until(lambda: link.connected), "first connect failed")
                link.disconnect()
                self.assertTrue(
                    _wait_until(lambda: link._state == "disconnected" and not link.connected)
                )
                link.start()
                self.assertTrue(_wait_until(lambda: link.connected), "reconnect failed")
                self.assertGreaterEqual(server.accepted, 2)
                link.disconnect()
                self.assertTrue(_wait_until(lambda: link._state == "disconnected"))
        finally:
            try:
                link.disconnect()
            except Exception:
                pass
            server.close()


if __name__ == "__main__":
    unittest.main()
