from __future__ import annotations

import math
import threading
import time
from typing import Callable

from .config import CONTROL_INTERVAL_S, CONTROL_PULSE_S
from .protocol.ctp import build_ctp

SendRaw = Callable[[bytes], bool]


def _stick_byte(value: float, invert: bool = False) -> int:
    value = max(-1.0, min(1.0, float(value)))
    dead = 0.1
    if abs(value) <= dead:
        return 128
    magnitude = round(128.0 * (abs(value) - dead) / (1.0 - dead))
    delta = magnitude if value > 0 else -magnitude
    if invert:
        delta = -delta
    return max(0, min(255, 128 + int(delta)))


class ControlFrameScheduler:
    """Builds and sends the 14-byte control frame at 40 Hz.

    The scheduler always sends neutral frames while connected, which keeps the
    control session alive. Flight sticks are only forwarded when armed, flying
    and not inhibited. Auto-descend states force neutral and lock user input.
    """

    def __init__(self, send_raw: SendRaw) -> None:
        self._send_raw = send_raw
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._enabled = True
        self._armed = False
        self._flying = False
        self._inhibit = True
        self._first_frame = True

        self._left_x = 0.0   # yaw
        self._left_y = 0.0   # throttle
        self._right_x = 0.0  # side
        self._right_y = 0.0  # forward/back

        self._pulse_bits = 0
        self._pulse_until = 0.0
        self._emergency_until = 0.0
        self._ground_combo: tuple[int, int, int, int] | None = None
        self._ground_combo_until = 0.0
        self._last_send_ok = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="control-tx", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def set_enabled(self, enabled: bool) -> None:
        """Allow or suppress control frames without stopping the sender thread.

        Before takeover this stays False, so merely being connected does not
        claim control from the RC.
        """
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._left_x = self._left_y = self._right_x = self._right_y = 0.0
                self._pulse_bits = 0
                self._pulse_until = 0.0
                self._emergency_until = 0.0
                self._ground_combo = None
                self._ground_combo_until = 0.0

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def reset_connection(self) -> None:
        with self._lock:
            self._first_frame = True
            self._pulse_bits = 0
            self._pulse_until = 0.0
            self._emergency_until = 0.0
            self._ground_combo = None
            self._ground_combo_until = 0.0
            self._inhibit = True
            self._armed = False
            self.center(emit=False)

    def set_ground_combo(self, combo: tuple[int, int, int, int], duration: float = 0.5) -> bool:
        """Send a raw D1/D2/D4/D5 combo on the ground (unlock/lock)."""
        with self._lock:
            if not self._enabled or self._inhibit:
                return False
            self._ground_combo = tuple(int(v) & 0xFF for v in combo)
            self._ground_combo_until = time.monotonic() + max(0.1, float(duration))
            return True

    def set_gate(self, *, armed: bool, flying: bool, inhibit: bool) -> None:
        with self._lock:
            self._armed = bool(armed)
            self._flying = bool(flying)
            self._inhibit = bool(inhibit)
            if self._inhibit:
                self._left_x = self._left_y = self._right_x = self._right_y = 0.0

    def set_sticks(self, left_x: float, left_y: float, right_x: float, right_y: float) -> None:
        with self._lock:
            if self._inhibit or not self._armed or not self._flying:
                self._left_x = self._left_y = self._right_x = self._right_y = 0.0
                return
            self._left_x = max(-1.0, min(1.0, float(left_x)))
            self._left_y = max(-1.0, min(1.0, float(left_y)))
            self._right_x = max(-1.0, min(1.0, float(right_x)))
            self._right_y = max(-1.0, min(1.0, float(right_y)))

    def center(self, emit: bool = True) -> None:
        with self._lock:
            self._left_x = self._left_y = self._right_x = self._right_y = 0.0
        # The next 40 Hz frame is neutral; no separate callback is required.

    def pulse_takeoff_land(self) -> bool:
        with self._lock:
            if self._inhibit:
                return False
            now = time.monotonic()
            self._pulse_bits = 0x08
            self._pulse_until = now + CONTROL_PULSE_S
            return True

    def emergency_stop(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._emergency_until = now + 0.5
            return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            start = time.monotonic()
            ok = self._send_one()
            self._last_send_ok = ok
            delay = CONTROL_INTERVAL_S if ok else 0.05
            elapsed = time.monotonic() - start
            if delay > elapsed:
                time.sleep(delay - elapsed)

    def _send_one(self) -> bool:
        with self._lock:
            if not self._enabled:
                return True
            now = time.monotonic()
            armed = self._armed
            flying = self._flying
            inhibit = self._inhibit
            first = self._first_frame
            pulse = self._pulse_bits if now < self._pulse_until else 0
            if pulse:
                self._pulse_bits = 0
                self._pulse_until = 0.0
            emergency = now < self._emergency_until

            ground = self._ground_combo
            if ground is not None and (inhibit or now >= self._ground_combo_until):
                ground = None
                self._ground_combo = None
                self._ground_combo_until = 0.0

            if ground is not None:
                d1, d2, d4, d5 = ground
            else:
                if armed and flying and not inhibit:
                    left_x, left_y = self._left_x, self._left_y
                    right_x, right_y = self._right_x, self._right_y
                else:
                    left_x = left_y = right_x = right_y = 0.0
                d1 = _stick_byte(left_y)
                d2 = _stick_byte(left_x)
                d4 = _stick_byte(right_y)
                d5 = _stick_byte(right_x)

            d3 = (192 if first else 48) | (0x08 if emergency else 0)
            d6 = 64
            d7 = 64
            d8 = 0x01 | pulse
            d9 = 0
            d10 = 0
            d11 = 122
            values = [204, d1, d2, d3, d4, d5, d6, d7, d8, d9, d10, d11]
            checksum = (255 - (sum(values[1:]) & 0xFF)) & 0xFF
            raw = bytes(values + [checksum, 51])
            self._first_frame = False

        param = {f"D{i}": str(b) for i, b in enumerate(raw)}
        packet = build_ctp("GENERIC_CMD", {"op": "PUT", "param": param})
        return bool(self._send_raw(packet))
