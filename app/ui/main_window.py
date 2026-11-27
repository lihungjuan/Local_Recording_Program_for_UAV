from __future__ import annotations

import html
import math
import time
from collections import deque

from PySide6.QtCore import QDateTime, QEvent, QTimer, Qt
from PySide6.QtGui import QFont, QImage
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..backend import Backend
from ..protocol.telemetry import Telemetry
from ..safety import SafetySnapshot
from .style import APP_QSS
from .widgets import BatteryBar, Joystick, VideoWidget


LINK_TEXT = {
    "disconnected": "未连接",
    "connecting": "连接中",
    "connected": "链路正常",
    "reconnecting": "重连中",
    "link_lost": "链路丢失",
    "reconnected_observing": "已重连 · 观察中",
    "connect_timeout": "连接超时",
    "no_hotspot": "未连接热点",
}

ACTIVE_LINK_STATES = (
    "connected",
    "connecting",
    "reconnecting",
    "link_lost",
    "reconnected_observing",
)

VIDEO_TEXT = {
    "waiting": "等待视频",
    "recovering": "视频恢复中",
    "live": "视频正常",
    "stalled": "视频中断",
    "error": "视频错误",
}

KEYBOARD_STICK_MAP = {
    Qt.Key_W: (1, 1.0),     # left Y, throttle up
    Qt.Key_S: (1, -1.0),    # left Y, throttle down
    Qt.Key_A: (0, -1.0),    # left X, yaw left
    Qt.Key_D: (0, 1.0),     # left X, yaw right
    Qt.Key_Up: (3, 1.0),    # right Y, forward
    Qt.Key_Down: (3, -1.0), # right Y, backward
    Qt.Key_Left: (2, -1.0), # right X, left
    Qt.Key_Right: (2, 1.0), # right X, right
}


class PreflightDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("连接前说明")
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("只读连接说明")
        title.setObjectName("Title")
        layout.addWidget(title)

        body = QLabel(
            "连接后程序默认只读：只接收视频和遥测，不发送飞行控制帧。\n\n"
            "• 遥控器可以随时连接并控制，程序不会阻止。\n"
            "• 只有点击“接管控制”后，程序才会尝试发送控制帧；接管可能锁住遥控器，"
            "且不保证能抢过已在控制的遥控器。\n"
            "• 连接期间，无人机 Wi-Fi 只允许本程序一个客户端，官方 App 无法同时入网。\n"
            "• 首次测试或真机使用请卸桨、固定机身，并保留遥控器作为独立处置手段。"
        )
        body.setObjectName("Muted")
        body.setWordWrap(True)
        layout.addWidget(body)

        self.checks: list[QCheckBox] = []
        cb = QCheckBox("我已知晓以上说明。")
        cb.toggled.connect(self._refresh)
        layout.addWidget(cb)
        self.checks.append(cb)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("连接无人机")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._refresh()

    def _refresh(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(all(c.isChecked() for c in self.checks))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{config.APP_NAME}  v{config.APP_VERSION}")
        self.resize(1520, 940)
        self.setMinimumSize(1320, 760)
        self.setStyleSheet(APP_QSS)

        self.backend = Backend()
        self._latest_telemetry: Telemetry | None = None
        self._latest_safety = SafetySnapshot()
        self._latest_stats: dict = {}
        self._recording = False
        self._recording_started_at: float | None = None
        self._rec_blink_on = True
        self._log_lines = deque(maxlen=2000)
        self._last_telemetry_ui_at = 0.0
        self._last_diag_at = 0.0
        self._takeover_state = "idle"
        self._takeover_detail = ""
        self._keyboard_enabled = False
        self._keyboard_user_override_off = False
        self._last_motor_unlocked = False
        self._last_flying = False
        self._keys_down: set[int] = set()
        self._kb_values = [0.0, 0.0, 0.0, 0.0]
        self._takeover_box = None
        self._bottom_expanded = False
        self._bottom_saved_sizes: list[int] | None = None
        self.setFocusPolicy(Qt.StrongFocus)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 12, 18, 12)
        root_layout.setSpacing(10)

        root_layout.addWidget(self._build_header())

        self.body_widget = QWidget()
        body = QHBoxLayout(self.body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(self._build_video_panel(), 1)
        left.addWidget(self._build_control_panel())
        body.addLayout(left, 7)
        body.addWidget(self._build_right_panel(), 3)

        self.bottom_panel = self._build_bottom_panel()
        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.body_widget)
        self.main_splitter.addWidget(self.bottom_panel)
        self.main_splitter.setStretchFactor(0, 7)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([650, 220])
        self.main_splitter.setCollapsible(0, True)
        self.main_splitter.setCollapsible(1, False)
        root_layout.addWidget(self.main_splitter, 1)

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_clock)
        self._clock.start()
        self._update_clock()

        self._kb_timer = QTimer(self)
        self._kb_timer.setInterval(50)
        self._kb_timer.timeout.connect(self._keyboard_tick)
        self._kb_timer.start()

        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(500)
        self._rec_timer.timeout.connect(self._update_recording_clock)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self._wire_backend()
        self._apply_snapshot(self._latest_safety)
        self._on_takeover_state("idle", "只读")
        self._on_media_dir_changed(self.backend.media_dir)
        self._append_log("INFO", "程序启动。默认只读，连接后不会发送控制帧。")

    # ---------------- layout ----------------

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Panel")
        frame.setFixedHeight(72)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 10, 16, 10)
        layout.setSpacing(14)

        logo = QLabel("AG")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(42, 42)
        logo.setStyleSheet(
            "background:#123c38;border:1px solid #2bc3ab;border-radius:11px;"
            "color:#7dffe8;font-size:17px;font-weight:800;"
        )
        layout.addWidget(logo)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel("空地协同 · 无人机地面站")
        title.setObjectName("Title")
        sub = QLabel("APEX G149 / GD240 · Wi-Fi 控制 · 本地录像")
        sub.setObjectName("SubTitle")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        layout.addLayout(title_box)
        layout.addStretch(1)

        self.link_pill = QLabel("未连接")
        self.link_pill.setObjectName("Badge")
        self.link_pill.setAlignment(Qt.AlignCenter)
        self.link_pill.setMinimumWidth(88)
        layout.addWidget(self.link_pill)

        self.video_pill = QLabel("等待视频")
        self.video_pill.setObjectName("Badge")
        self.video_pill.setAlignment(Qt.AlignCenter)
        self.video_pill.setMinimumWidth(88)
        layout.addWidget(self.video_pill)

        self.clock_label = QLabel("--:--:--")
        self.clock_label.setObjectName("ValueSmall")
        self.clock_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.clock_label.setMinimumWidth(100)
        layout.addWidget(self.clock_label)
        return frame

    def _build_video_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.video_widget = VideoWidget()
        layout.addWidget(self.video_widget, 1)

        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 4, 0)
        self.video_detail = QLabel("视频：等待连接")
        self.video_detail.setObjectName("Muted")
        row.addWidget(self.video_detail)
        row.addStretch(1)
        self.recording_label = QLabel("本地录制：关闭")
        self.recording_label.setObjectName("Muted")
        row.addWidget(self.recording_label)
        self.chk_watermark = QCheckBox("时间戳水印")
        self.chk_watermark.setToolTip("在照片和录像右上角烧录本地时间；录制中不可修改")
        self.chk_watermark.setChecked(self.backend.watermark_enabled)
        self.chk_watermark.toggled.connect(self._toggle_watermark)
        row.addWidget(self.chk_watermark)
        layout.addLayout(row)
        return frame

    def _build_control_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Panel")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.btn_connect = QPushButton("连接无人机")
        self.btn_connect.setProperty("kind", "primary")
        self.btn_connect.clicked.connect(self._toggle_connection)
        layout.addWidget(self.btn_connect)

        self.btn_arm = QPushButton("接管控制")
        self.btn_arm.setProperty("kind", "ghost")
        self.btn_arm.setToolTip("点击后才发送控制帧尝试接管；接管可能锁住遥控器")
        self.btn_arm.clicked.connect(self._toggle_arm)
        layout.addWidget(self.btn_arm)

        self.takeover_label = QLabel("只读")
        self.takeover_label.setObjectName("Muted")
        self.takeover_label.setMinimumWidth(92)
        layout.addWidget(self.takeover_label)

        self.btn_takeoff = QPushButton("一键起飞")
        self.btn_takeoff.setProperty("kind", "primary")
        self.btn_takeoff.setMinimumWidth(128)
        self.btn_takeoff.clicked.connect(self._takeoff_land)
        layout.addWidget(self.btn_takeoff)

        layout.addSpacing(6)
        self.btn_snapshot = QPushButton("拍照")
        self.btn_snapshot.setToolTip("保存当前视频帧到当前存储目录/日期/ 目录")
        self.btn_snapshot.clicked.connect(self.backend.snapshot)
        layout.addWidget(self.btn_snapshot)

        self.btn_record = QPushButton("开始录制")
        self.btn_record.setProperty("kind", "record")
        self.btn_record.setToolTip("将本地视频保存为 MP4，不依赖无人机 SD 卡")
        self.btn_record.clicked.connect(self._toggle_recording)
        layout.addWidget(self.btn_record)

        self.btn_stream = QPushButton("重开视频")
        self.btn_stream.setToolTip(
            "重新请求视频流并重置解码器。\n"
            "用于预览卡死、花屏或视频中断，但链路仍正常时。\n"
            "不会重连控制链路；录制中点击可能造成短暂丢帧。"
        )
        self.btn_stream.clicked.connect(lambda: self.backend.open_stream())
        layout.addWidget(self.btn_stream)

        self.btn_storage = QPushButton("存储目录…")
        self.btn_storage.setProperty("kind", "ghost")
        self.btn_storage.setToolTip("选择照片和录像的本地存储目录")
        self.btn_storage.clicked.connect(self._choose_storage_dir)
        layout.addWidget(self.btn_storage)

        layout.addStretch(1)
        self.btn_emergency = QPushButton("紧急停桨")
        self.btn_emergency.setProperty("kind", "danger")
        self.btn_emergency.setToolTip("未验证的紧急停桨位；仅用于紧急情况")
        self.btn_emergency.clicked.connect(self._emergency_stop)
        layout.addWidget(self.btn_emergency)
        return frame

    def _build_right_panel(self) -> QWidget:
        content = QFrame()
        content.setObjectName("PanelStrong")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        section = QLabel("飞行状态")
        section.setObjectName("Section")
        layout.addWidget(section)

        self.safety_card = QFrame()
        self.safety_card.setObjectName("SafetyCard")
        safety_layout = QVBoxLayout(self.safety_card)
        safety_layout.setContentsMargins(12, 9, 12, 9)
        safety_layout.setSpacing(3)
        self.safety_status = QLabel("未连接")
        self.safety_status.setObjectName("StatusText")
        self.safety_detail = QLabel("等待连接")
        self.safety_detail.setObjectName("Muted")
        self.safety_detail.setWordWrap(True)
        safety_layout.addWidget(self.safety_status)
        safety_layout.addWidget(self.safety_detail)
        layout.addWidget(self.safety_card)

        motor_row = QHBoxLayout()
        motor_row.setSpacing(8)
        self.btn_motor = QPushButton("解锁电机")
        self.btn_motor.setProperty("kind", "ghost")
        self.btn_motor.setToolTip("发送 App 组合切换电机解锁/加锁；仅地面且控制权确认后可用")
        self.btn_motor.clicked.connect(self._toggle_motor)
        motor_row.addWidget(self.btn_motor)
        self.motor_label = QLabel("电机：—")
        self.motor_label.setObjectName("Muted")
        motor_row.addWidget(self.motor_label)
        motor_row.addStretch(1)
        layout.addLayout(motor_row)

        cards = QGridLayout()
        cards.setSpacing(8)
        self.alt_value, self.alt_card = self._mini_card("高度", "—", "m")
        self.bat_value, self.bat_card = self._mini_card("电池", "—", "V")
        self.state_value, self.state_card = self._mini_card("飞行", "落地", "")
        self.link_value, self.link_card = self._mini_card("链路", "未连接", "")
        cards.addWidget(self.alt_card, 0, 0)
        cards.addWidget(self.bat_card, 0, 1)
        cards.addWidget(self.state_card, 1, 0)
        cards.addWidget(self.link_card, 1, 1)
        layout.addLayout(cards)

        self.battery_bar = BatteryBar()
        layout.addWidget(self.battery_bar)
        self.battery_detail = QLabel("D10 — · 电量格 —")
        self.battery_detail.setObjectName("Muted")
        layout.addWidget(self.battery_detail)

        joystick_section = QLabel("飞行摇杆")
        joystick_section.setObjectName("Section")
        layout.addWidget(joystick_section)

        sticks = QHBoxLayout()
        sticks.setSpacing(8)
        left_box = QVBoxLayout()
        left_box.setSpacing(4)
        left_label = QLabel("左杆 · 油门 / 偏航")
        left_label.setObjectName("Muted")
        left_label.setAlignment(Qt.AlignCenter)
        self.left_stick = Joystick()
        self.left_stick.value_changed.connect(self._sticks_changed)
        left_box.addWidget(left_label)
        left_box.addWidget(self.left_stick, 0, Qt.AlignCenter)

        right_box = QVBoxLayout()
        right_box.setSpacing(4)
        right_label = QLabel("右杆 · 前后 / 左右")
        right_label.setObjectName("Muted")
        right_label.setAlignment(Qt.AlignCenter)
        self.right_stick = Joystick()
        self.right_stick.value_changed.connect(self._sticks_changed)
        right_box.addWidget(right_label)
        right_box.addWidget(self.right_stick, 0, Qt.AlignCenter)
        sticks.addStretch(1)
        sticks.addLayout(left_box)
        sticks.addLayout(right_box)
        sticks.addStretch(1)
        layout.addLayout(sticks)

        self.chk_keyboard = QCheckBox("启用键盘摇杆（需接管并飞行）")
        self.chk_keyboard.setToolTip("WASD 控制左杆，方向键控制右杆；窗口失焦自动回中")
        self.chk_keyboard.toggled.connect(self._toggle_keyboard)
        layout.addWidget(self.chk_keyboard)
        kb_hint = QLabel(
            "键盘：W/S 油门 · A/D 偏航\n"
            "方向键：↑/↓ 前后 · ←/→ 平移"
        )
        kb_hint.setObjectName("Muted")
        kb_hint.setWordWrap(True)
        layout.addWidget(kb_hint)
        self.keyboard_state_label = QLabel("键盘摇杆：关闭")
        self.keyboard_state_label.setObjectName("Muted")
        layout.addWidget(self.keyboard_state_label)

        self.btn_battery_replaced = QPushButton("已更换电池 / 解除低电锁定")
        self.btn_battery_replaced.setProperty("kind", "ghost")
        self.btn_battery_replaced.clicked.connect(self.backend.battery_replaced)
        self.btn_battery_replaced.setVisible(False)
        layout.addWidget(self.btn_battery_replaced)

        self.btn_clear_conflict = QPushButton("清除连接冲突标记")
        self.btn_clear_conflict.setProperty("kind", "ghost")
        self.btn_clear_conflict.clicked.connect(self.backend.clear_conflict)
        self.btn_clear_conflict.setVisible(False)
        layout.addWidget(self.btn_clear_conflict)

        layout.addStretch(1)
        divider = QFrame()
        divider.setObjectName("Divider")
        layout.addWidget(divider)
        self.connection_detail = QLabel("热点：—\n设备：—\n策略：默认只读 / 接管需确认")
        self.connection_detail.setObjectName("Muted")
        self.connection_detail.setWordWrap(True)
        layout.addWidget(self.connection_detail)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(content)
        return scroll

    def _mini_card(self, title: str, value: str, unit: str) -> tuple[QLabel, QFrame]:
        card = QFrame()
        card.setObjectName("MiniCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(1)
        label = QLabel(title)
        label.setObjectName("Muted")
        row = QHBoxLayout()
        row.setSpacing(3)
        val = QLabel(value)
        val.setObjectName("ValueSmall")
        unit_label = QLabel(unit)
        unit_label.setObjectName("Unit")
        unit_label.setAlignment(Qt.AlignBottom)
        row.addWidget(val)
        row.addWidget(unit_label)
        row.addStretch(1)
        layout.addWidget(label)
        layout.addLayout(row)
        return val, card

    def _build_bottom_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("Panel")
        frame.setMinimumHeight(120)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        self.tabs = QTabWidget()
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(2000)
        self.diag_view = QTextEdit()
        self.diag_view.setReadOnly(True)
        self.diag_view.setFont(QFont("Cascadia Mono", 9))
        self.tabs.addTab(self.log_view, "运行日志")
        self.tabs.addTab(self.diag_view, "诊断 / 原始数据")
        layout.addWidget(self.tabs, 1)

        row = QHBoxLayout()
        self.footer_label = QLabel("默认只读 · 接管需确认 · 存储目录 —")
        self.footer_label.setObjectName("Muted")
        self.footer_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.footer_label)
        row.addStretch(1)
        self.btn_expand_bottom = QPushButton("放大日志/诊断")
        self.btn_expand_bottom.setProperty("kind", "ghost")
        self.btn_expand_bottom.clicked.connect(self._toggle_bottom_expand)
        row.addWidget(self.btn_expand_bottom)
        btn_clear = QPushButton("清空日志")
        btn_clear.setProperty("kind", "ghost")
        btn_clear.clicked.connect(self.log_view.clear)
        row.addWidget(btn_clear)
        layout.addLayout(row)
        return frame

    # ---------------- wiring ----------------

    def _wire_backend(self) -> None:
        self.backend.sig_link_state.connect(self._on_link_state)
        self.backend.sig_telemetry.connect(self._on_telemetry)
        self.backend.sig_safety.connect(self._on_safety)
        self.backend.sig_frame.connect(self._on_frame)
        self.backend.sig_video_state.connect(self._on_video_state)
        self.backend.sig_stats.connect(self._on_stats)
        self.backend.sig_log.connect(self._on_log)
        self.backend.sig_recording.connect(self._on_recording)
        self.backend.sig_takeover_state.connect(self._on_takeover_state)
        self.backend.sig_media_dir_changed.connect(self._on_media_dir_changed)
        self.backend.sig_motor_action.connect(self._on_motor_action)
        self.backend.sig_watermark_changed.connect(self._on_watermark_changed)

    # ---------------- actions ----------------

    def _toggle_connection(self) -> None:
        if self._latest_safety.link_state in ACTIVE_LINK_STATES:
            if self.backend.control_confirmed and self._latest_safety.flying:
                box = QMessageBox(self)
                box.setWindowTitle("飞行中断开")
                box.setIcon(QMessageBox.Critical)
                box.setText("飞机仍在飞行，断开程序会触发断联缓降。")
                box.setInformativeText("建议先降落或让遥控器接管。确定断开吗？")
                box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
                box.button(QMessageBox.Yes).setText("仍要断开")
                box.button(QMessageBox.Cancel).setText("取消")
                if box.exec() != QMessageBox.Yes:
                    return
            ok = None
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                ok = self.backend.disconnect_drone()
            finally:
                QApplication.restoreOverrideCursor()
            if not ok:
                self._offer_force("断开连接", lambda: self.backend.disconnect_drone(force=True))
            return
        dialog = PreflightDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.backend.connect_drone()

    def _toggle_arm(self) -> None:
        self._clear_stick_input()
        engaged = (
            self._takeover_state in ("pending", "acked", "timeout")
            or self._latest_safety.armed
        )
        if engaged:
            box = QMessageBox(self)
            box.setWindowTitle("交还控制")
            box.setIcon(QMessageBox.Warning)
            box.setText("交还控制会停止控制帧，并断开后以只读模式重连。")
            box.setInformativeText("如果飞机正在飞行，请确认遥控器可以接管或飞机处于安全状态。")
            box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
            box.button(QMessageBox.Yes).setText("交还控制")
            box.button(QMessageBox.Cancel).setText("取消")
            if box.exec() == QMessageBox.Yes:
                ok = None
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    ok = self.backend.release_control()
                finally:
                    QApplication.restoreOverrideCursor()
                if not ok:
                    self._offer_force("交还控制", lambda: self.backend.release_control(force=True))
            return

        box = QMessageBox(self)
        box.setWindowTitle("接管控制")
        box.setIcon(QMessageBox.Warning)
        box.setText("接管后程序会开始发送控制帧，可能锁住遥控器。")
        box.setInformativeText(
            "如果遥控器已经在控制，程序不保证能抢过控制权。"
            "点击后程序会等待飞控的 status=1 确认；未收到会提示。"
        )
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.button(QMessageBox.Yes).setText("尝试接管")
        box.button(QMessageBox.Cancel).setText("取消")
        if box.exec() != QMessageBox.Yes:
            return
        ok, message = self.backend.start_takeover()
        if not ok:
            QMessageBox.warning(self, "接管未开始", message)

    def _offer_force(self, title: str, action) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Warning)
        box.setText("加锁未确认，操作已取消。")
        box.setInformativeText(
            "电机可能继续转动约 10 秒。强制继续会停止控制帧并断开链路，"
            "但无法保证加锁。建议直接断开无人机电源。"
        )
        force = box.addButton("强制继续", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is force:
            action()

    def _toggle_motor(self) -> None:
        self._clear_stick_input()
        t = self._latest_telemetry
        if t is None:
            QMessageBox.warning(self, "电机", "没有遥测，无法判断电机状态")
            return
        action = "加锁（停止电机）" if t.bit7 else "解锁（启动电机）"
        box = QMessageBox(self)
        box.setWindowTitle("切换电机状态")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"即将执行：{action}")
        box.setInformativeText(
            "解锁会启动电机。请确认已卸桨或飞机已固定，周围无人。\n"
            "加锁只在越障地面状态可用，飞行中禁止加锁。"
        )
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.button(QMessageBox.Yes).setText("继续")
        box.button(QMessageBox.Cancel).setText("取消")
        if box.exec() != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, message = self.backend.toggle_motor_lock()
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_motor_ui()
        if not ok:
            QMessageBox.warning(self, "电机状态未确认", message)

    def _on_motor_action(self, ok: bool, message: str) -> None:
        self._refresh_motor_ui()
        if ok:
            self._append_log("INFO", message)

    def _refresh_motor_ui(self) -> None:
        t = self._latest_telemetry
        snap = self._latest_safety
        if t is None:
            self.motor_label.setText("电机：—")
            self.motor_label.setStyleSheet("")
            self.btn_motor.setEnabled(False)
            self.btn_motor.setText("解锁电机")
            self._set_kind(self.btn_motor, "ghost")
            self._last_motor_unlocked = False
            return
        unlocked = t.bit7
        if unlocked and not self._last_motor_unlocked:
            self._keyboard_user_override_off = False
            self._maybe_auto_enable_keyboard()
        elif not unlocked and self._last_motor_unlocked:
            if self.chk_keyboard.isChecked():
                self._set_keyboard_enabled_programmatic(False)
        self._last_motor_unlocked = unlocked
        self.motor_label.setText("电机：已解锁" if unlocked else "电机：已锁定")
        self.motor_label.setStyleSheet("color:#f0b35a;" if unlocked else "color:#7b8794;")
        ground = t.fly_status == 0 and t.alt_cm <= 5
        ready = (
            self._takeover_state == "confirmed"
            and self.backend.link.connected
            and ground
            and not snap.auto_reason
            and not snap.conflict
        )
        self.btn_motor.setEnabled(ready)
        self.btn_motor.setText("加锁电机" if unlocked else "解锁电机")
        self._set_kind(self.btn_motor, "active" if unlocked else "ghost")

    def _choose_storage_dir(self) -> None:
        self._clear_stick_input()
        path = QFileDialog.getExistingDirectory(
            self,
            "选择照片/录像存储目录",
            self.backend.media_dir,
        )
        if not path:
            return
        ok, message = self.backend.set_media_dir(path)
        if not ok:
            QMessageBox.warning(self, "存储目录", message)

    def _on_media_dir_changed(self, path: str) -> None:
        self.btn_snapshot.setToolTip(f"保存当前视频帧到 {path}/日期/ 目录")
        self.btn_storage.setToolTip(f"当前存储目录：{path}")
        self._update_footer()
        self._refresh_diag(force=True)

    def _update_footer(self) -> None:
        state = {
            "idle": "只读",
            "released": "只读",
            "pending": "接管中",
            "acked": "待确认控制权",
            "confirmed": "已确认",
            "timeout": "接管未确认",
        }.get(self._takeover_state, self._takeover_state)
        self.footer_label.setText(
            f"默认只读 · 接管状态：{state} · 存储目录 {self.backend.media_dir}"
        )

    def _toggle_bottom_expand(self) -> None:
        total = max(600, sum(self.main_splitter.sizes()))
        if not self._bottom_expanded:
            self._bottom_saved_sizes = self.main_splitter.sizes()
            self.main_splitter.setSizes([0, total])
            self._bottom_expanded = True
            self.btn_expand_bottom.setText("还原布局")
        else:
            if self._bottom_saved_sizes:
                self.main_splitter.setSizes(self._bottom_saved_sizes)
            else:
                self.main_splitter.setSizes([650, 220])
            self._bottom_expanded = False
            self.btn_expand_bottom.setText("放大日志/诊断")
        self.footer_label.setToolTip(f"存储目录：{self.backend.media_dir}")

    def _takeoff_land(self) -> None:
        self.backend.takeoff_land()

    def _toggle_recording(self) -> None:
        if self._recording:
            self.backend.stop_recording()
        else:
            self.backend.start_recording()

    def _toggle_watermark(self, checked: bool) -> None:
        ok, message = self.backend.set_watermark_enabled(checked)
        if not ok:
            self.chk_watermark.blockSignals(True)
            self.chk_watermark.setChecked(self.backend.watermark_enabled)
            self.chk_watermark.blockSignals(False)
            QMessageBox.warning(self, "时间戳水印", message)

    def _on_watermark_changed(self, enabled: bool) -> None:
        if self.chk_watermark.isChecked() != enabled:
            self.chk_watermark.blockSignals(True)
            self.chk_watermark.setChecked(enabled)
            self.chk_watermark.blockSignals(False)
        self.video_widget.update()

    def _update_recording_clock(self) -> None:
        if not self._recording or self._recording_started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - self._recording_started_at)
        self._rec_blink_on = not self._rec_blink_on
        self.video_widget.set_recording_elapsed(elapsed, self._rec_blink_on)

    def _sticks_changed(self, *_args) -> None:
        lx, ly = self.left_stick.value()
        rx, ry = self.right_stick.value()
        self.backend.set_sticks(lx, ly, rx, ry)

    def _toggle_keyboard(self, checked: bool) -> None:
        """User interaction with the keyboard-joystick checkbox."""
        self._keyboard_user_override_off = not bool(checked)
        self._apply_keyboard_enabled(bool(checked), focus=bool(checked))

    def _apply_keyboard_enabled(self, enabled: bool, focus: bool = False) -> None:
        self._keyboard_enabled = enabled
        self.keyboard_state_label.setText(
            "键盘摇杆：启用（W/A/S/D 或方向键）"
            if enabled
            else "键盘摇杆：关闭"
        )
        if not enabled:
            self._clear_keyboard()
        elif focus:
            self.setFocus(Qt.OtherFocusReason)

    def _set_keyboard_enabled_programmatic(self, enabled: bool) -> None:
        if self.chk_keyboard.isChecked() == enabled:
            return
        self.chk_keyboard.blockSignals(True)
        self.chk_keyboard.setChecked(enabled)
        self.chk_keyboard.blockSignals(False)
        self._apply_keyboard_enabled(enabled, focus=False)

    def _maybe_auto_enable_keyboard(self) -> None:
        t = self._latest_telemetry
        if t is None or not t.bit7:
            return
        if self._takeover_state != "confirmed":
            return
        if self._keyboard_user_override_off:
            return
        if not self.chk_keyboard.isChecked():
            self._set_keyboard_enabled_programmatic(True)

    def _clear_keyboard(self) -> None:
        self._clear_stick_input()

    def _clear_stick_input(self) -> None:
        self._keys_down.clear()
        self._kb_values = [0.0, 0.0, 0.0, 0.0]
        self.left_stick.cancel_drag()
        self.right_stick.cancel_drag()
        self.left_stick.set_value(0.0, 0.0)
        self.right_stick.set_value(0.0, 0.0)
        if hasattr(self, "backend"):
            self.backend.set_sticks(0.0, 0.0, 0.0, 0.0)

    def _keyboard_tick(self) -> None:
        if not self._keyboard_enabled:
            return
        if not self._latest_safety.can_move:
            if self._keys_down or any(abs(v) > 0.001 for v in self._kb_values):
                self._clear_keyboard()
            return
        control_ready = self._takeover_state == "confirmed"
        if not control_ready:
            if self._keys_down or any(abs(v) > 0.001 for v in self._kb_values):
                self._clear_keyboard()
            return

        targets = [0.0, 0.0, 0.0, 0.0]
        for key in self._keys_down:
            item = KEYBOARD_STICK_MAP.get(key)
            if item is not None:
                index, value = item
                targets[index] += value
        targets = [max(-1.0, min(1.0, value)) for value in targets]
        for x_index, y_index in ((0, 1), (2, 3)):
            length = math.hypot(targets[x_index], targets[y_index])
            if length > 1.0:
                targets[x_index] /= length
                targets[y_index] /= length
        if not self._keys_down and all(abs(v) < 0.001 for v in self._kb_values):
            return

        step = 0.18
        for i, target in enumerate(targets):
            current = self._kb_values[i]
            if current < target:
                current = min(target, current + step)
            elif current > target:
                current = max(target, current - step)
            self._kb_values[i] = current
        self.left_stick.set_value(self._kb_values[0], self._kb_values[1])
        self.right_stick.set_value(self._kb_values[2], self._kb_values[3])
        self.backend.set_sticks(*self._kb_values)

    def _text_input_focused(self) -> bool:
        widget = QApplication.focusWidget()
        if isinstance(widget, QLineEdit):
            return True
        if isinstance(widget, (QTextEdit, QPlainTextEdit)):
            return not widget.isReadOnly()
        return False

    def eventFilter(self, obj, event) -> bool:
        if self._keyboard_enabled and event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
            if not self.isActiveWindow() or QApplication.activeModalWidget() is not None:
                return super().eventFilter(obj, event)
            if self._text_input_focused():
                return super().eventFilter(obj, event)
            key = event.key()
            if key in KEYBOARD_STICK_MAP:
                if event.type() == QEvent.KeyPress:
                    if not event.isAutoRepeat():
                        self._keys_down.add(key)
                else:
                    if not event.isAutoRepeat():
                        self._keys_down.discard(key)
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self._clear_stick_input()
        super().changeEvent(event)

    def _emergency_stop(self) -> None:
        self._clear_stick_input()
        if not self.backend.link.connected:
            self._append_log("WARN", "链路未连接，无法发送紧急停桨")
            return
        box = QMessageBox(self)
        box.setWindowTitle("紧急停桨")
        box.setIcon(QMessageBox.Critical)
        box.setText("此操作会让电机立即停转，仅用于紧急情况。")
        box.setInformativeText("确认发送未验证的紧急停桨指令？")
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box.button(QMessageBox.Yes).setText("继续")
        box.button(QMessageBox.Cancel).setText("取消")
        if box.exec() != QMessageBox.Yes:
            return
        box2 = QMessageBox(self)
        box2.setWindowTitle("再次确认")
        box2.setIcon(QMessageBox.Critical)
        box2.setText("再次确认：立即停桨？")
        box2.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
        box2.button(QMessageBox.Yes).setText("确认停桨")
        box2.button(QMessageBox.Cancel).setText("取消")
        if box2.exec() == QMessageBox.Yes:
            self.backend.emergency_stop()

    # ---------------- slots ----------------

    def _on_link_state(self, state: str, detail: str) -> None:
        if state in ("link_lost", "connect_timeout", "no_hotspot", "disconnected", "reconnecting"):
            self._clear_stick_input()
        if state in ("link_lost", "connect_timeout", "no_hotspot", "disconnected"):
            self._close_takeover_box()
        text = LINK_TEXT.get(state, state)
        self.link_value.setText(text)
        self._set_pill(self.link_pill, text, self._link_color(state))
        if state in ACTIVE_LINK_STATES:
            if state == "connecting":
                self.btn_connect.setText("取消连接")
            else:
                self.btn_connect.setText("断开连接")
        elif state in ("connect_timeout", "no_hotspot"):
            self.btn_connect.setText("重新连接")
        else:
            self.btn_connect.setText("连接无人机")
        self.btn_connect.setEnabled(True)
        self.btn_stream.setEnabled(state in ("connected", "reconnected_observing"))
        self._refresh_diag()

    def _on_takeover_state(self, state: str, detail: str) -> None:
        self._takeover_state = state
        self._takeover_detail = detail
        if state != "confirmed" and self.chk_keyboard.isChecked():
            self._set_keyboard_enabled_programmatic(False)
        if state in ("idle", "released"):
            self._keyboard_user_override_off = False
        if state in ("pending", "acked", "timeout", "released", "idle"):
            self._clear_stick_input()
        if state in ("idle", "released"):
            self._close_takeover_box()
        text = {
            "idle": "只读",
            "released": "已交还 · 只读",
            "pending": "接管中 · 等待飞控",
            "acked": "飞控已接收 · 待确认",
            "confirmed": "已接管 · 已确认",
            "timeout": "接管未确认",
        }.get(state, state)
        color = {
            "pending": "#f0b35a",
            "acked": "#f0b35a",
            "confirmed": "#37d6c0",
            "timeout": "#ff5d5d",
        }.get(state, "#7b8794")
        self.takeover_label.setText(text)
        self.takeover_label.setStyleSheet(f"color:{color};")
        self.takeover_label.setToolTip(detail)
        self._apply_takeover_ui(self._latest_safety)
        self._refresh_motor_ui()
        if state == "confirmed":
            self._maybe_auto_enable_keyboard()
        self._update_footer()
        self._refresh_diag(force=True)
        if state in ("acked", "timeout"):
            QTimer.singleShot(0, self._show_control_verification)

    def _close_takeover_box(self) -> None:
        box = self._takeover_box
        if box is not None:
            try:
                box.close()
            except Exception:
                pass
            self._takeover_box = None

    def _show_control_verification(self) -> None:
        if self._takeover_state not in ("acked", "timeout"):
            return
        self._clear_stick_input()
        self._close_takeover_box()
        timed_out = self._takeover_state == "timeout"
        box = QMessageBox(self)
        box.setWindowTitle("确认控制权")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "未收到飞控确认（status=1），接管可能失败。"
            if timed_out
            else "飞控已接收控制帧（status=1），但这不代表控制权已经归程序。"
        )
        box.setInformativeText(
            "请现在用遥控器尝试控制：\n"
            "• 遥控器仍能控制 → 接管失败，程序交还控制。\n"
            "• 遥控器不能控制 → 确认由程序接管。"
        )
        fail = box.addButton("遥控器仍能控制 / 交还控制", QMessageBox.RejectRole)
        success = box.addButton("遥控器不能控制 / 确认接管", QMessageBox.AcceptRole)
        box.setDefaultButton(fail)
        box.setEscapeButton(fail)
        box.exec()
        if box.clickedButton() is success:
            self.backend.confirm_takeover(True)
        else:
            self.backend.confirm_takeover(False)

    def _apply_takeover_ui(self, snap: SafetySnapshot) -> None:
        state = self._takeover_state
        if state == "pending":
            self.btn_arm.setText("取消接管")
            self.btn_arm.setEnabled(True)
            self._set_kind(self.btn_arm, "active")
            return
        if state in ("acked", "timeout"):
            self.btn_arm.setText("待确认")
            self.btn_arm.setEnabled(False)
            self._set_kind(self.btn_arm, "ghost")
            return
        if state == "confirmed":
            self.btn_arm.setText("交还控制")
            self.btn_arm.setEnabled(True)
            self._set_kind(self.btn_arm, "active")
            return
        self.btn_arm.setText("接管控制")
        self.btn_arm.setEnabled(snap.can_arm)
        self._set_kind(self.btn_arm, "primary" if snap.can_arm else "ghost")

    def _on_telemetry(self, t: Telemetry) -> None:
        self._latest_telemetry = t
        now = time.monotonic()
        if now - self._last_telemetry_ui_at < 0.08:
            return
        self._last_telemetry_ui_at = now
        self.alt_value.setText(f"{t.alt_cm / 100.0:.2f}")
        self.bat_value.setText(f"{t.battery_mv / 1000.0:.2f}")
        self.battery_detail.setText(
            f"D10 {t.battery_raw} · 电量格 {t.battery_bars} · "
            f"飞行状态 {t.fly_status} ({t.fly_label})"
        )
        self.battery_bar.set_level(t.battery_bars, self._latest_safety.battery_warning, self._latest_safety.battery_critical)
        self.state_value.setText("飞行中" if t.flying else "落地")
        flying = bool(t.flying)
        if self._last_flying and not flying and self.chk_keyboard.isChecked():
            self._set_keyboard_enabled_programmatic(False)
        self._last_flying = flying
        self._refresh_motor_ui()
        self._refresh_diag()

    def _on_safety(self, snap: SafetySnapshot) -> None:
        self._latest_safety = snap
        self._apply_snapshot(snap)
        self._refresh_diag()

    def _on_frame(self, image: QImage) -> None:
        self.video_widget.set_frame(image)

    def _on_video_state(self, state: str) -> None:
        text = VIDEO_TEXT.get(state, state)
        self.video_detail.setText(f"视频：{text}")
        self._set_pill(self.video_pill, text, self._video_color(state))
        self.video_widget.set_status(text, self._video_color(state))
        self.btn_snapshot.setEnabled(state == "live")
        self._refresh_diag()

    def _on_stats(self, stats: dict) -> None:
        self._latest_stats = stats
        resolution = stats.get("resolution") or "—"
        self.video_widget.set_stats(
            f"{resolution} · {stats.get('fps', 0):.1f} fps · {stats.get('mbps', 0):.2f} Mbps"
        )
        self._refresh_diag()

    def _on_recording(self, active: bool, path: str) -> None:
        self._recording = active
        if active:
            self._recording_started_at = time.monotonic()
            self._rec_blink_on = True
            self._rec_timer.start()
            self.video_widget.set_recording(True)
            self.recording_label.setText("本地录制：录制中（点击停止）")
            self.btn_record.setText("停止录制")
            self._set_kind(self.btn_record, "active")
            self.chk_watermark.setEnabled(False)
            self.chk_watermark.setText("时间戳水印（录制中已锁定）")
            self.chk_watermark.setToolTip("录制中已锁定，停止录制后可修改")
        else:
            self._rec_timer.stop()
            self._recording_started_at = None
            self.video_widget.set_recording(False)
            self.recording_label.setText("本地录制：关闭")
            self.btn_record.setText("开始录制")
            self._set_kind(self.btn_record, "record")
            self.chk_watermark.setEnabled(True)
            self.chk_watermark.setText("时间戳水印")
            self.chk_watermark.setToolTip("在照片和录像右上角烧录本地时间；录制中不可修改")
        self.recording_label.setToolTip(path)

    def _on_log(self, level: str, message: str) -> None:
        self._append_log(level, message)

    # ---------------- rendering ----------------

    def _apply_snapshot(self, snap: SafetySnapshot) -> None:
        status = snap.status
        color = snap.color
        local_link = snap.link_state in ("connected", "reconnected_observing")
        if snap.armed and not snap.auto_reason and local_link:
            if self._takeover_state == "pending":
                status, color = "接管中 · 等待飞控确认", "#f0b35a"
            elif self._takeover_state == "timeout":
                status, color = "接管未确认", "#ff5d5d"
            elif self._takeover_state == "acked":
                status, color = "飞控已接收 · 待确认控制权", "#f0b35a"
            elif self._takeover_state == "confirmed":
                status, color = "已接管 · 飞控确认完成", "#37d6c0"
        self.safety_status.setText(status)
        self.safety_status.setStyleSheet(f"color:{color};")
        detail = snap.warnings[0] if snap.warnings else self._link_hint(snap)
        self.safety_detail.setText(detail)
        bg, border = self._safety_colors(snap)
        self.safety_card.setStyleSheet(
            f"QFrame#SafetyCard {{ background:{bg}; border:1px solid {border}; border-radius:10px; }}"
        )

        self.link_value.setText(LINK_TEXT.get(snap.link_state, snap.link_state))
        self.state_value.setText("飞行中" if snap.flying else "落地")
        if snap.auto_reason == "link":
            self.state_value.setText("断联缓降")

        self._apply_takeover_ui(snap)

        control_ready = self._takeover_state == "confirmed"
        t = self._latest_telemetry
        unlocked = bool(t and t.bit7)
        self.btn_takeoff.setText("一键降落" if snap.flying else "一键起飞")
        self.btn_takeoff.setEnabled(
            control_ready and unlocked and (snap.can_takeoff or snap.can_land)
        )
        if not control_ready:
            self.btn_takeoff.setToolTip("请先完成接管并人工确认控制权")
        elif not unlocked:
            self.btn_takeoff.setToolTip("请先解锁电机")
        else:
            self.btn_takeoff.setToolTip("")

        sticks_enabled = snap.can_move and control_ready
        self.left_stick.setEnabled(sticks_enabled)
        self.right_stick.setEnabled(sticks_enabled)
        if not sticks_enabled:
            self.left_stick.center(emit=False)
            self.right_stick.center(emit=False)
        self._refresh_motor_ui()

        self.btn_emergency.setEnabled(snap.armed and local_link)
        self.btn_battery_replaced.setVisible(snap.battery_lockout)
        self.btn_clear_conflict.setVisible(snap.conflict)
        self.btn_stream.setEnabled(local_link)

        warning = ""
        if snap.auto_reason:
            warning = "自动缓降中 · 飞行控制已锁定"
        elif self._takeover_state == "pending":
            warning = "接管中 · 等待飞控确认"
        elif self._takeover_state == "acked":
            warning = "飞控已接收控制帧 · 请用遥控器确认控制权"
        elif self._takeover_state == "timeout":
            warning = "接管未确认 · 请用遥控器复核"
        elif snap.link_state == "no_hotspot":
            warning = "未连接热点 · 请先在系统 Wi-Fi 中连接无人机热点"
        elif snap.link_state == "connect_timeout":
            warning = "连接超时 · 请检查无人机电源后点击“重新连接”"
        elif snap.link_state == "link_lost":
            warning = "控制链路丢失 · 预计约 5 s 后自动缓降"
        elif snap.battery_critical:
            warning = "电池临界 · 仅告警，请立即降落"
        elif snap.battery_warning:
            warning = "低电量 · 建议尽快降落"
        elif snap.link_state == "reconnected_observing":
            warning = "已重连 · 正在确认飞控状态"
        self.video_widget.set_warning(warning)

        self.connection_detail.setText(
            f"热点：{self.backend.ssid or '—'}\n"
            f"设备：{self.backend.drone_info or '—'}\n"
            "策略：默认只读 / 接管需确认"
        )

        t = self._latest_telemetry
        if t is not None:
            self.battery_bar.set_level(t.battery_bars, snap.battery_warning, snap.battery_critical)
        self._refresh_diag()

    def _link_hint(self, snap: SafetySnapshot) -> str:
        if snap.link_state == "connected":
            if self._takeover_state == "confirmed":
                return "已接管：请先解锁电机，再一键起飞；飞行中才允许摇杆。"
            return "默认只读：不发送控制帧；点击“接管控制”并人工确认控制权后才可操作。"
        if snap.link_state == "reconnecting":
            return "正在重连，重连成功不会自动恢复控制。"
        if snap.link_state == "no_hotspot":
            return "未连接热点：请先在 Windows Wi-Fi 中连接无人机热点（本机需获得 192.168.1.x 地址）。"
        if snap.link_state == "connect_timeout":
            return "连接超时，已停止重试；请检查无人机电源与热点后点击“重新连接”。"
        return "等待连接无人机热点并建立 TCP 3333。"

    def _safety_colors(self, snap: SafetySnapshot) -> tuple[str, str]:
        if (
            snap.conflict
            or snap.auto_reason
            or snap.link_state in ("link_lost", "no_hotspot")
            or self._takeover_state == "timeout"
        ):
            return "rgba(255,93,93,0.12)", "#7d3038"
        if (
            snap.battery_warning
            or self._takeover_state in ("pending", "acked")
            or snap.link_state in (
                "reconnecting",
                "reconnected_observing",
                "connect_timeout",
            )
        ):
            return "rgba(240,179,90,0.12)", "#6b5428"
        if snap.armed:
            return "rgba(55,214,192,0.10)", "#1f6f63"
        return "rgba(55,214,192,0.06)", "#1c3d3a"

    def _set_pill(self, label: QLabel, text: str, color: str) -> None:
        label.setText(text)
        label.setStyleSheet(
            f"background:rgba(5,12,17,0.55); border:1px solid {color}; color:{color};"
            "border-radius:10px; padding:3px 10px; font-size:11px; font-weight:700;"
        )

    def _link_color(self, state: str) -> str:
        if state in ("connected",):
            return "#37d6c0"
        if state in ("connecting", "reconnecting", "reconnected_observing", "connect_timeout"):
            return "#f0b35a"
        if state in ("link_lost", "no_hotspot"):
            return "#ff5d5d"
        return "#7b8794"

    def _video_color(self, state: str) -> str:
        if state == "live":
            return "#37d6c0"
        if state in ("recovering",):
            return "#f0b35a"
        if state in ("stalled", "error"):
            return "#ff5d5d"
        return "#7b8794"

    def _set_kind(self, button: QPushButton, kind: str) -> None:
        if button.property("kind") == kind:
            return
        button.setProperty("kind", kind)
        button.style().unpolish(button)
        button.style().polish(button)

    def _append_log(self, level: str, message: str) -> None:
        colors = {"INFO": "#8fd8cd", "WARN": "#f0b35a", "ERROR": "#ff7b7b"}
        color = colors.get(level, "#9fb4c5")
        stamp = time.strftime("%H:%M:%S")
        safe = html.escape(message)
        self.log_view.append(
            f'<span style="color:#5f7485">[{stamp}]</span> '
            f'<span style="color:{color};font-weight:600">{level:5s}</span> '
            f'<span style="color:#c7d6e2">{safe}</span>'
        )
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _refresh_diag(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_diag_at < 0.2:
            return
        self._last_diag_at = now
        t = self._latest_telemetry
        s = self._latest_stats
        snap = self._latest_safety
        lines = [
            "策略        : 默认只读 / 接管需确认 / 飞控确认 status=1",
            f"链路状态    : {snap.link_state}",
            f"接管状态    : {self._takeover_state} / 本地 armed={snap.armed} / 自动原因 {snap.auto_reason or '—'}",
            f"电机状态    : {'已解锁' if (t and t.bit7) else '已锁定' if t else '—'} / bit7={t.bit7 if t else '—'}",
            f"键盘摇杆    : {'启用' if self._keyboard_enabled else '关闭'} / 可控制={snap.can_move}",
            f"视频状态    : {snap.video_state} / {s.get('resolution', '—')}",
            f"UDP 包      : {s.get('udp_packets', 0)} / 丢帧 {s.get('frames_dropped', 0)} / 解码失败 {s.get('decode_failures', 0)}",
            f"码率        : {s.get('mbps', 0):.2f} Mbps / {s.get('fps', 0):.1f} fps",
        ]
        if t is not None:
            lines += [
                f"遥测        : alt={t.alt_cm} cm  mv={t.battery_mv}  D10={t.battery_raw}  flags=0x{t.flags:04x}",
                f"飞行/标志   : fly={t.fly_status} ({t.fly_label})  photo={t.photo}  video={t.video}  bit7={t.bit7}",
                f"原始 16B    : {t.raw_hex}",
                f"遥测年龄    : {snap.telemetry_age:.2f} s" if snap.telemetry_age is not None else "遥测年龄    : —",
            ]
        lines += [
            f"热点        : {self.backend.ssid or '—'}",
            f"设备        : {self.backend.drone_info or '—'}",
            f"SD 在线     : {self.backend.sd_online}",
            f"录制        : {'进行中' if self._recording else '关闭'}",
            f"存储目录    : {self.backend.media_dir}",
        ]
        bar = self.diag_view.verticalScrollBar()
        old_value = bar.value()
        old_max = bar.maximum()
        at_bottom = old_max > 0 and old_value >= old_max - 2
        self.diag_view.setPlainText("\n".join(lines))
        if at_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(old_value, bar.maximum()))

    def _update_clock(self) -> None:
        self.clock_label.setText(QDateTime.currentDateTime().toString("HH:mm:ss"))

    def closeEvent(self, event) -> None:
        self._clear_keyboard()
        t = self._latest_telemetry
        if self.backend.control_confirmed and t is not None and (
            t.fly_status != 0 or t.alt_cm > 5
        ):
            box = QMessageBox(self)
            box.setWindowTitle("飞行中退出")
            box.setIcon(QMessageBox.Critical)
            box.setText("飞机仍在飞行，退出程序会断开控制链路。")
            box.setInformativeText("确定要退出吗？建议先降落或让遥控器接管。")
            box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
            box.button(QMessageBox.Yes).setText("仍要退出")
            box.button(QMessageBox.Cancel).setText("取消")
            if box.exec() != QMessageBox.Yes:
                event.ignore()
                return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, message = self.backend.lock_before_close()
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            box = QMessageBox(self)
            box.setWindowTitle("加锁未确认")
            box.setIcon(QMessageBox.Critical)
            box.setText("退出前加锁未确认，电机可能继续转动约 10 秒。")
            box.setInformativeText("建议先断开无人机电源。仍要退出吗？")
            box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Yes)
            box.button(QMessageBox.Yes).setText("仍要退出")
            box.button(QMessageBox.Cancel).setText("取消")
            if box.exec() != QMessageBox.Yes:
                event.ignore()
                return
        self._takeover_state = "idle"
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.backend.shutdown()
        super().closeEvent(event)
