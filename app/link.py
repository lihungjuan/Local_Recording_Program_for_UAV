from __future__ import annotations

import datetime as _dt
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import config
from .protocol.ctp import CTPParser, build_ctp

StateCb = Callable[[str, str], None]
TopicCb = Callable[[str, Any], None]
LogCb = Callable[[str, str], None]


class LinkManager:
    """Owns the TCP control link and the CTP stream parser."""

    def __init__(self, on_state: StateCb, on_topic: TopicCb, on_log: LogCb) -> None:
        self._on_state = on_state
        self._on_topic = on_topic
        self._log = on_log
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._parser = CTPParser()
        self._connected = False
        self._state = "disconnected"
        self._last_rx = 0.0
        self._last_keepalive = 0.0
        self._connected_at = 0.0
        self._reconnect_attempts = 0
        self._manual_disconnect = False
        self._connect_deadline: float | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def connected_at(self) -> float:
        with self._lock:
            return self._connected_at

    def start(self) -> None:
        old = self._thread
        if old is not None and old.is_alive():
            self._stop.set()
            self._close_socket()
            old.join(timeout=3.0)
            if old.is_alive():
                self._log("WARN", "上一条连接线程尚未退出，本次连接已取消")
                return
        with self._lock:
            self._stop = threading.Event()
            self._manual_disconnect = False
            self._state = "disconnected"
            self._connect_deadline = time.monotonic() + self._connect_window_s()
            self._thread = threading.Thread(target=self._run, name="ctp-link", daemon=True)
            self._thread.start()

    @staticmethod
    def _connect_window_s() -> float:
        raw = config.PROFILE.get("link", {}).get(
            "connect_total_timeout_ms", config.CONNECT_TOTAL_TIMEOUT_S * 1000
        )
        try:
            seconds = float(raw) / 1000.0
        except (TypeError, ValueError):
            seconds = config.CONNECT_TOTAL_TIMEOUT_S
        return max(0.01, seconds)

    def disconnect(self) -> None:
        with self._lock:
            self._manual_disconnect = True
            self._stop.set()
            self._connect_deadline = None
            self._close_socket()
        self._set_state("disconnected", "已断开")

    def send_raw(self, data: bytes) -> bool:
        with self._lock:
            sock = self._sock
            if sock is None or not self._connected:
                return False
            try:
                sock.sendall(data)
                return True
            except OSError as exc:
                self._log("WARN", f"发送失败：{exc}")
        self._mark_lost("发送失败")
        return False

    def send_ctp(self, topic: str, payload: Any | None = None) -> bool:
        return self.send_raw(build_ctp(topic, payload))

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            if self._state == state:
                return
            self._state = state
        self._on_state(state, detail)

    def _run(self) -> None:
        if not self._has_drone_network():
            with self._lock:
                self._manual_disconnect = True
            self._log("WARN", "未检测到无人机热点，请先在系统 Wi-Fi 中连接无人机热点")
            self._set_state("no_hotspot", "未连接热点")
            return
        backoff = config.RECONNECT_INITIAL_S
        window = self._connect_window_s()
        timed_out = False
        while not self._stop.is_set():
            if self._manual_disconnect:
                break
            if self._connect_window_expired():
                timed_out = True
                break
            if self._try_connect():
                backoff = config.RECONNECT_INITIAL_S
                with self._lock:
                    self._connect_deadline = None
                self._reader = threading.Thread(target=self._read_loop, name="ctp-rx", daemon=True)
                self._reader.start()
                while not self._stop.is_set() and self.connected:
                    time.sleep(0.1)
                if self._reader and self._reader.is_alive():
                    self._reader.join(timeout=1.0)
                if self._stop.is_set() or self._manual_disconnect:
                    break
                # 链路中途丢失后，重新给满一个连接窗口
                with self._lock:
                    self._connect_deadline = time.monotonic() + window
            if self._stop.is_set() or self._manual_disconnect:
                break
            self._set_state("reconnecting", f"{backoff:.1f}s 后重试")
            self._sleep_backoff(backoff)
            backoff = min(config.RECONNECT_MAX_S, backoff * 1.5)
        self._close_socket()
        if self._manual_disconnect:
            self._set_state("disconnected", "已断开")
            return
        if timed_out:
            self._log(
                "WARN",
                f"连接无人机超时（{window:.0f} 秒），已停止重试",
            )
            self._set_state("connect_timeout", "连接超时")
        else:
            self._set_state("disconnected", "已断开")

    @staticmethod
    def _wlan_ssids() -> list[str] | None:
        """Return associated WLAN SSIDs, [] when none, or None if unknown."""
        if not sys.platform.startswith("win"):
            return None
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        ssids: list[str] = []
        for line in result.stdout.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() == "ssid":
                ssid = value.strip()
                if ssid:
                    ssids.append(ssid)
        return ssids

    @staticmethod
    def _has_drone_network() -> bool:
        ssids = LinkManager._wlan_ssids()
        if ssids is not None:
            prefixes = config.PROFILE.get("link", {}).get(
                "drone_ssid_prefixes", list(config.DRONE_SSID_PREFIXES)
            )
            return any(
                ssid.lower().startswith(str(prefix).lower())
                for ssid in ssids
                for prefix in prefixes
            )
        # Fallback when the WLAN state cannot be queried: check the local route.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((config.DRONE_HOST, config.CTP_PORT))
            local_ip = probe.getsockname()[0]
        except OSError:
            return False
        finally:
            probe.close()
        return local_ip.startswith("192.168.1.")

    def _connect_window_expired(self) -> bool:
        with self._lock:
            deadline = self._connect_deadline
        return deadline is not None and time.monotonic() >= deadline

    def _remaining_connect_window(self) -> float | None:
        with self._lock:
            deadline = self._connect_deadline
        if deadline is None:
            return None
        return deadline - time.monotonic()

    def _sleep_backoff(self, seconds: float) -> None:
        target = time.monotonic() + seconds
        remaining = self._remaining_connect_window()
        if remaining is not None:
            target = min(target, time.monotonic() + max(0.0, remaining))
        while not self._stop.is_set() and time.monotonic() < target:
            time.sleep(0.05)

    def _try_connect(self) -> bool:
        self._set_state("connecting", f"连接 {config.DRONE_HOST}:{config.CTP_PORT}")
        sock = None
        remaining = self._remaining_connect_window()
        timeout = 2.0 if remaining is None else max(0.2, min(2.0, remaining))
        try:
            sock = socket.create_connection(
                (config.DRONE_HOST, config.CTP_PORT),
                timeout=timeout,
            )
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(0.5)
            with self._lock:
                self._sock = sock
                self._connected = True
                self._connected_at = time.monotonic()
                self._last_rx = time.monotonic()
                self._last_keepalive = 0.0
                self._parser.reset()
                self._reconnect_attempts += 1
            self._log("INFO", f"TCP 已连接 {config.DRONE_HOST}:{config.CTP_PORT}")
            self.send_ctp("APP_ACCESS", {"op": "PUT", "param": {"ver": "35", "type": "0"}})
            self.send_ctp(
                "DATE_TIME",
                {
                    "op": "PUT",
                    "param": {"date": _dt.datetime.now().strftime("%Y%m%d%H%M%S")},
                },
            )
            if not self.connected:
                return False
            self._set_state("connected", "控制链路已连接")
            return True
        except OSError as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._lock:
                self._sock = None
                self._connected = False
            self._log("WARN", f"TCP 连接失败：{exc}")
            return False

    def _read_loop(self) -> None:
        while not self._stop.is_set() and self.connected:
            now = time.monotonic()
            if now - self._last_keepalive >= 1.5:
                self._last_keepalive = now
                if not self.send_ctp("CTP_KEEP_ALIVE", {"op": "PUT"}):
                    break
            with self._lock:
                sock = self._sock
            if sock is None:
                break
            try:
                data = sock.recv(8192)
            except socket.timeout:
                if time.monotonic() - self._last_rx > config.LINK_IDLE_TIMEOUT_S:
                    self._mark_lost("遥测超时")
                    break
                continue
            except OSError as exc:
                self._mark_lost(str(exc))
                break
            if not data:
                self._mark_lost("对端关闭")
                break
            with self._lock:
                self._last_rx = time.monotonic()
            for topic, payload in self._parser.feed(data):
                try:
                    self._on_topic(topic, payload)
                except Exception as exc:
                    self._log("ERROR", f"处理 {topic} 失败：{exc}")

    def _mark_lost(self, reason: str) -> None:
        with self._lock:
            if not self._connected:
                return
            self._connected = False
            sock = self._sock
            self._sock = None
            self._parser.reset()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._log("WARN", f"控制链路断开：{reason}")
        self._on_state("link_lost", reason)

    def _close_socket(self) -> None:
        with self._lock:
            sock = self._sock
            self._sock = None
            self._connected = False
            self._parser.reset()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
