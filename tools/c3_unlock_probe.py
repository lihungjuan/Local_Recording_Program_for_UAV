"""C3 bench probe: send unlock/lock stick combinations over Wi-Fi.

This is a temporary diagnostic tool. It does not import or modify the main
application UI. It connects directly to the drone on TCP 3333, claims control
with neutral control frames, and sends one of the observed unlock/lock combos.

SAFETY:
  * Remove propellers and secure the aircraft.
  * Keep the RC powered OFF so this probe can be the controlling client.
  * Close the ground-station program and the official App.
  * Be ready to disconnect power.

Usage examples:
  python tools\\c3_unlock_probe.py --dry-run
  python tools\\c3_unlock_probe.py --combo unlock
  python tools\\c3_unlock_probe.py --combo lock
  python tools\\c3_unlock_probe.py --combo app
  python tools\\c3_unlock_probe.py --combo app_twice
  python tools\\c3_unlock_probe.py --combo sequence
  python tools\\c3_unlock_probe.py --combo app --stream
"""

from __future__ import annotations

import argparse
import datetime as _dt
import socket
import sys
import threading
import time
from pathlib import Path

# Make "app" importable when this file is executed as tools\c3_unlock_probe.py.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.protocol.ctp import CTPParser, build_ctp
from app.protocol.telemetry import decode_telemetry

NEUTRAL = (128, 128, 128, 128)  # D1 throttle, D2 yaw, D4 fwd/back, D5 side
MANUAL_UNLOCK = (0, 0, 0, 255)  # left bottom-left + right bottom-right
MANUAL_LOCK = (0, 255, 0, 0)    # left bottom-right + right bottom-left
APP_COMBO = (45, 36, 39, 212)   # observed from the official App capture

COMBOS = {
    "unlock": MANUAL_UNLOCK,
    "lock": MANUAL_LOCK,
    "app": APP_COMBO,
}


def make_control(d1: int, d2: int, d4: int, d5: int, first: bool = False,
                 d8: int = 1) -> bytes:
    d3 = 192 if first else 48
    values = [204, d1, d2, d3, d4, d5, 64, 64, d8, 0, 0, 122]
    checksum = (255 - (sum(values[1:]) & 0xFF)) & 0xFF
    raw = bytes(values + [checksum, 51])
    param = {f"D{i}": str(b) for i, b in enumerate(raw)}
    return build_ctp("GENERIC_CMD", {"op": "PUT", "param": param})


class Probe:
    def __init__(self, host: str, port: int, open_stream: bool) -> None:
        self._host = host
        self._port = port
        self._open_stream = open_stream
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._combo = NEUTRAL
        self._first = True
        self._last_keepalive = 0.0
        self._parser = CTPParser()
        self._control_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._ack = 0
        self._bit7: bool | None = None
        self._flags: int | None = None
        self._fly_status: int | None = None
        self._alt_cm: int | None = None
        self._d8_pulse = False
        self._last_telemetry_at = 0.0

    @property
    def ack(self) -> int:
        with self._lock:
            return self._ack

    @property
    def bit7(self) -> bool | None:
        with self._lock:
            return self._bit7

    @property
    def flags(self) -> int | None:
        with self._lock:
            return self._flags

    @property
    def fly_status(self) -> int | None:
        with self._lock:
            return self._fly_status

    @property
    def alt_cm(self) -> int | None:
        with self._lock:
            return self._alt_cm

    @property
    def airborne(self) -> bool:
        with self._lock:
            if self._fly_status not in (None, 0):
                return True
            return self._alt_cm is not None and self._alt_cm > 5

    def connect(self) -> None:
        self._sock = socket.create_connection((self._host, self._port), timeout=3.0)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(0.5)
        print(f"TCP connected {self._host}:{self._port}")
        self._send("APP_ACCESS", {"op": "PUT", "param": {"ver": "35", "type": "0"}})
        self._send(
            "DATE_TIME",
            {
                "op": "PUT",
                "param": {"date": _dt.datetime.now().strftime("%Y%m%d%H%M%S")},
            },
        )
        if self._open_stream:
            self._send("CLOSE_RT_STREAM", {"op": "PUT", "param": {"status": "0"}})
            time.sleep(0.1)
            self._send(
                "OPEN_RT_STREAM",
                {
                    "op": "PUT",
                    "param": {"format": "1", "w": "1280", "h": "720", "fps": "25"},
                },
            )
            print("stream requested")

        self._control_thread = threading.Thread(target=self._control_loop, name="c3-tx", daemon=True)
        self._reader_thread = threading.Thread(target=self._reader_loop, name="c3-rx", daemon=True)
        self._control_thread.start()
        self._reader_thread.start()

    def _send(self, topic: str, payload) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(build_ctp(topic, payload))
        except OSError as exc:
            print(f"send failed: {exc}")

    def _send_raw(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(data)
        except OSError as exc:
            print(f"send failed: {exc}")

    def set_combo(self, combo: tuple[int, int, int, int], label: str) -> None:
        with self._lock:
            self._combo = combo
        print(f"combo -> {label} {combo}")

    def _control_loop(self) -> None:
        interval = 0.025
        while not self._stop.is_set():
            start = time.monotonic()
            with self._lock:
                combo = self._combo
                first = self._first
                self._first = False
                d8 = 9 if self._d8_pulse else 1
                self._d8_pulse = False
                keepalive = start - self._last_keepalive >= 1.5
                if keepalive:
                    self._last_keepalive = start
            self._send_raw(make_control(combo[0], combo[1], combo[2], combo[3], first, d8))
            if keepalive:
                self._send("CTP_KEEP_ALIVE", {"op": "PUT"})
            elapsed = time.monotonic() - start
            if elapsed < interval:
                time.sleep(interval - elapsed)

    def _reader_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            for topic, payload in self._parser.feed(data):
                if topic != "GENERIC_CMD" or not isinstance(payload, dict):
                    continue
                param = payload.get("param") or {}
                if "status" in param:
                    with self._lock:
                        self._ack += 1
                    continue
                t = decode_telemetry(payload)
                if t is None:
                    continue
                with self._lock:
                    changed = t.bit7 != self._bit7
                    state_changed = (t.fly_status != self._fly_status or t.alt_cm != self._alt_cm)
                    self._bit7 = t.bit7
                    self._flags = t.flags
                    self._fly_status = t.fly_status
                    self._alt_cm = t.alt_cm
                    self._last_telemetry_at = time.monotonic()
                if changed or state_changed:
                    print(
                        f"TELEMETRY flags=0x{t.flags:04x} bit7={t.bit7} "
                        f"fly={t.fly_status} alt={t.alt_cm} mv={t.battery_mv}"
                    )

    def wait_for_ack(self, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self._stop.is_set():
            if self.ack > 0:
                print(f"status=1 ack received ({self.ack})")
                return True
            time.sleep(0.05)
        print("WARNING: no status=1 ack within timeout; continuing, combo may be ignored")
        return False

    def settle(self, seconds: float) -> None:
        time.sleep(seconds)

    def pulse_takeoff(self) -> None:
        with self._lock:
            self._d8_pulse = True
        print("one-key takeoff pulse (one control frame, D8 bit3)")

    def observe(self, seconds: float) -> None:
        time.sleep(seconds)
        print(
            f"OBSERVE bit7={self.bit7} flags={self.flags} "
            f"fly={self.fly_status} alt={self.alt_cm} airborne={self.airborne}"
        )

    def send_combo(self, combo: tuple[int, int, int, int], label: str,
                   hold: float = 0.5, settle: float = 1.5) -> None:
        before = self.bit7
        self.set_combo(combo, label)
        time.sleep(hold)
        self.set_combo(NEUTRAL, "neutral")
        time.sleep(settle)
        after = self.bit7
        print(f"RESULT {label}: bit7 {before} -> {after} / flags={self.flags}")
        if after is True:
            print("WARNING: bit7 is still True (motors may be unlocked)")

    def safety_lock(self) -> None:
        if self.airborne:
            print("SAFETY: airborne/alt>5cm; NOT sending lock. Land with RC or program.")
            return
        # C3 result: the reverse manual combo did NOT lock over Wi-Fi.
        # The App combo toggles unlocked/locked, so use it for safety.
        for attempt in range(2):
            if self.bit7 is not True:
                return
            print(f"SAFETY: bit7=True, sending App toggle (attempt {attempt + 1})")
            self.set_combo(APP_COMBO, "safety_toggle")
            time.sleep(0.5)
            self.set_combo(NEUTRAL, "neutral")
            time.sleep(1.0)
            print(f"SAFETY RESULT {attempt + 1}: bit7={self.bit7}")
            if self.bit7 is not True:
                return
        print("SAFETY WARNING: bit7 still True; power off the drone")

    def close(self) -> None:
        self._stop.set()
        try:
            if self._sock is not None:
                try:
                    self._send_raw(make_control(*NEUTRAL))
                    time.sleep(0.05)
                except Exception:
                    pass
                self._sock.close()
        except OSError:
            pass
        for thread in (self._control_thread, self._reader_thread):
            if thread:
                thread.join(timeout=1.0)

    def drop(self) -> None:
        """Close TCP without sending neutral or lock (C4c link-loss test)."""
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for thread in (self._control_thread, self._reader_thread):
            if thread:
                thread.join(timeout=1.0)


def phase_sequence() -> list[tuple[str, tuple[int, int, int, int] | None, float, float]]:
    # C3 showed: manual unlock works, reverse manual lock does not, and the
    # App combo toggles each time. The sequence ends with a toggle intended
    # to leave the motors locked; safety_lock attempts to toggle again if not.
    return [
        ("neutral", NEUTRAL, 1.0, 0.0),
        ("manual_unlock", MANUAL_UNLOCK, 0.5, 1.5),
        ("app_combo_1", APP_COMBO, 0.5, 1.5),
        ("app_combo_2", APP_COMBO, 0.5, 1.5),
        ("app_combo_3", APP_COMBO, 0.5, 1.5),
    ]


def read_status(host: str, port: int, duration: float = 3.0) -> None:
    """Read telemetry without sending any control frame (C4c follow-up)."""
    sock = socket.create_connection((host, port), timeout=3.0)
    sock.settimeout(0.5)
    parser = CTPParser()
    last_bit7 = None
    last_state = None
    end = time.monotonic() + duration
    print(f"status read for {duration:.0f}s (no control frames)")
    try:
        while time.monotonic() < end:
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError as exc:
                print(f"status recv error: {exc}")
                break
            if not data:
                break
            for topic, payload in parser.feed(data):
                if topic != "GENERIC_CMD":
                    continue
                t = decode_telemetry(payload)
                if t is None:
                    continue
                state = (t.bit7, t.flags, t.fly_status, t.alt_cm)
                if state != last_state:
                    print(
                        f"status bit7={t.bit7} flags=0x{t.flags:04x} "
                        f"fly={t.fly_status} alt={t.alt_cm} mv={t.battery_mv}"
                    )
                    last_bit7 = t.bit7
                    last_state = state
        print(f"final bit7={last_bit7}")
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="C3/C4 unlock/lock/takeoff bench probe")
    parser.add_argument("--host", default="192.168.1.1")
    parser.add_argument("--port", type=int, default=3333)
    parser.add_argument(
        "--combo",
        choices=[
            "unlock", "lock", "app", "app_twice", "sequence",
            "takeoff", "unlock_takeoff", "unlock_drop", "status",
        ],
        default="sequence",
    )
    parser.add_argument("--stream", action="store_true", help="also send CLOSE/OPEN_RT_STREAM")
    parser.add_argument("--yes", action="store_true", help="skip the safety prompt")
    parser.add_argument("--dry-run", action="store_true", help="print frames and exit")
    args = parser.parse_args()

    print("C3/C4 unlock/lock/takeoff bench probe")
    print("SAFETY: propellers removed, aircraft fixed, program/App closed.")
    print("RC state: follow the experiment procedure (C3: RC OFF; C3b: RC ON).")
    print(f"mode={args.combo} host={args.host}:{args.port} stream={args.stream}")

    if args.dry_run:
        for name, combo in (("unlock", MANUAL_UNLOCK), ("lock", MANUAL_LOCK), ("app", APP_COMBO)):
            payload = make_control(combo[0], combo[1], combo[2], combo[3], first=True)
            print(f"{name}: {len(payload)} bytes {payload.hex()}")
        payload = make_control(*NEUTRAL, first=True, d8=9)
        print(f"takeoff_pulse: {len(payload)} bytes {payload.hex()}")
        return 0

    if args.combo == "status":
        read_status(args.host, args.port, duration=3.0)
        return 0

    if not args.yes:
        answer = input("Type YES to send control frames: ").strip().upper()
        if answer != "YES":
            print("cancelled")
            return 1

    probe = Probe(args.host, args.port, args.stream)
    skip_safety = args.combo == "unlock_drop"
    try:
        probe.connect()
        probe.wait_for_ack(2.0)
        probe.settle(0.5)
        print(f"initial bit7={probe.bit7} flags={probe.flags}")

        if args.combo == "sequence":
            for label, combo, hold, settle in phase_sequence():
                if combo is None:
                    time.sleep(hold)
                elif label == "neutral":
                    probe.set_combo(NEUTRAL, "neutral")
                    time.sleep(hold)
                else:
                    probe.send_combo(combo, label, hold=hold, settle=settle)
        elif args.combo == "app_twice":
            probe.send_combo(APP_COMBO, "app_combo_1", hold=0.5, settle=1.0)
            probe.send_combo(APP_COMBO, "app_combo_2", hold=0.5, settle=1.5)
        elif args.combo == "takeoff":
            probe.set_combo(NEUTRAL, "neutral")
            probe.settle(1.0)
            probe.pulse_takeoff()
            probe.observe(3.0)
        elif args.combo == "unlock_takeoff":
            probe.set_combo(NEUTRAL, "neutral")
            probe.settle(1.0)
            probe.send_combo(APP_COMBO, "unlock_before_takeoff", hold=0.5, settle=1.0)
            if probe.bit7 is not True:
                print("ABORT: unlock did not produce bit7=True; not sending takeoff pulse")
            else:
                probe.pulse_takeoff()
                probe.observe(3.0)
        elif args.combo == "unlock_drop":
            probe.set_combo(NEUTRAL, "neutral")
            probe.settle(1.0)
            probe.send_combo(APP_COMBO, "unlock_before_drop", hold=0.5, settle=1.0)
            if probe.bit7 is not True:
                print("ABORT: unlock did not produce bit7=True")
            else:
                print("DROPPING TCP NOW (no neutral, no lock)")
                probe.drop()
                print("TCP dropped. Watch the motors for 10 seconds, then run:")
                print("  python tools\\c3_unlock_probe.py --combo status")
            return 0
        else:
            probe.send_combo(COMBOS[args.combo], args.combo, hold=0.5, settle=1.5)

        probe.safety_lock()
        print(
            f"final bit7={probe.bit7} flags={probe.flags} fly={probe.fly_status} "
            f"alt={probe.alt_cm} acks={probe.ack}"
        )
        return 0
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2
    finally:
        if not skip_safety:
            try:
                probe.safety_lock()
            except Exception:
                pass
        probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
