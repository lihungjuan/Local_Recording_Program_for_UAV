from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


class VideoWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._image: QImage | None = None
        self._status = "等待视频流"
        self._status_color = QColor("#7b8794")
        self._stats = ""
        self._warning = ""
        self._recording = False
        self._rec_elapsed = 0.0
        self._rec_blink_on = True

    def set_frame(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_status(self, text: str, color: str = "#37d6c0") -> None:
        self._status = text
        self._status_color = QColor(color)
        self.update()

    def set_stats(self, text: str) -> None:
        self._stats = text
        self.update()

    def set_warning(self, text: str) -> None:
        self._warning = text
        self.update()

    def set_recording(self, recording: bool, elapsed: float = 0.0) -> None:
        self._recording = recording
        self._rec_elapsed = max(0.0, float(elapsed))
        self._rec_blink_on = True
        self.update()

    def set_recording_elapsed(self, elapsed: float, blink_on: bool = True) -> None:
        self._rec_elapsed = max(0.0, float(elapsed))
        self._rec_blink_on = bool(blink_on)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#05080c"))

        if self._image is not None and not self._image.isNull():
            target = self._fit_rect(self._image.width(), self._image.height())
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawImage(target, self._image)
        else:
            self._draw_empty(painter)

        self._draw_corners(painter)
        self._draw_crosshair(painter)
        self._draw_top_bar(painter)
        if self._warning:
            self._draw_warning(painter)
        if self._recording:
            self._draw_recording(painter)

    def _fit_rect(self, iw: int, ih: int) -> QRectF:
        w = self.width()
        h = self.height()
        if iw <= 0 or ih <= 0:
            return QRectF(0, 0, w, h)
        scale = min(w / iw, h / ih)
        tw, th = iw * scale, ih * scale
        return QRectF((w - tw) / 2, (h - th) / 2, tw, th)

    def _draw_empty(self, painter: QPainter) -> None:
        painter.setPen(QColor("#2b3d4c"))
        painter.setBrush(Qt.NoBrush)
        rect = QRectF(24, 24, self.width() - 48, self.height() - 48)
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(QColor("#5f7485"))
        font = QFont(self.font())
        font.setPointSize(18)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, "等待视频流")
        painter.setPen(QColor("#3f5363"))
        font.setPointSize(10)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(0, 38, 0, 0),
            Qt.AlignCenter,
            "连接无人机后自动请求 1280×720 H.264",
        )

    def _draw_corners(self, painter: QPainter) -> None:
        pen = QPen(QColor(55, 214, 192, 120), 1.2)
        painter.setPen(pen)
        m = 14
        n = 22
        w, h = self.width(), self.height()
        for x, y, dx, dy in (
            (m, m, 1, 1),
            (w - m, m, -1, 1),
            (m, h - m, 1, -1),
            (w - m, h - m, -1, -1),
        ):
            painter.drawLine(QPointF(x, y), QPointF(x + dx * n, y))
            painter.drawLine(QPointF(x, y), QPointF(x, y + dy * n))

    def _draw_crosshair(self, painter: QPainter) -> None:
        c = self.rect().center()
        painter.setPen(QPen(QColor(55, 214, 192, 55), 1))
        painter.drawLine(c.x() - 9, c.y(), c.x() + 9, c.y())
        painter.drawLine(c.x(), c.y() - 9, c.x(), c.y() + 9)

    def _pill(self, painter: QPainter, x: float, y: float, text: str, color: QColor) -> None:
        font = QFont(self.font())
        font.setPointSize(9)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        w = metrics.horizontalAdvance(text) + 22
        h = 25
        rect = QRectF(x, y, w, h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(5, 12, 17, 190))
        painter.drawRoundedRect(rect, 12, 12)
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 45))
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignCenter, text)

    def _draw_top_bar(self, painter: QPainter) -> None:
        self._pill(painter, 20, 18, self._status, self._status_color)
        if self._stats:
            font = QFont(self.font())
            font.setPointSize(9)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            w = metrics.horizontalAdvance(self._stats) + 20
            rect = QRectF(self.width() - w - 20, 18, w, 25)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(5, 12, 17, 175))
            painter.drawRoundedRect(rect, 12, 12)
            painter.setPen(QColor("#8fa6b8"))
            painter.drawText(rect, Qt.AlignCenter, self._stats)

    def _draw_warning(self, painter: QPainter) -> None:
        rect = QRectF(20, self.height() - 58, self.width() - 40, 38)
        painter.setPen(Qt.NoPen)
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        gradient.setColorAt(0, QColor(255, 93, 93, 220))
        gradient.setColorAt(1, QColor(240, 179, 90, 220))
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(QColor("#120b08"))
        font = QFont(self.font())
        font.setPointSize(10)
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        painter.drawText(rect.adjusted(14, 0, -14, 0), Qt.AlignVCenter | Qt.AlignLeft, self._warning)

    def _draw_recording(self, painter: QPainter) -> None:
        rect = QRectF(20, 50, 150, 28)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(5, 12, 17, 190))
        painter.drawRoundedRect(rect, 8, 8)

        dot_color = QColor("#ff5d5d") if self._rec_blink_on else QColor(255, 93, 93, 55)
        painter.setBrush(dot_color)
        painter.drawEllipse(QPointF(rect.left() + 15, rect.center().y()), 4.5, 4.5)

        seconds = int(self._rec_elapsed)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        text = f"REC {hours:02d}:{minutes:02d}:{secs:02d}"
        painter.setPen(QColor("#ffe3e5"))
        font = QFont(self.font())
        font.setPointSize(9)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.drawText(
            QRectF(rect.left() + 26, rect.top(), rect.width() - 32, rect.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            text,
        )


class Joystick(QWidget):
    value_changed = Signal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(120, 120)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._x = 0.0
        self._y = 0.0
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)

    def value(self) -> tuple[float, float]:
        return self._x, self._y

    def set_value(self, x: float, y: float) -> None:
        """Update the knob without emitting value_changed (keyboard display)."""
        if self._dragging:
            self._dragging = False
            self.releaseMouse()
            self.setCursor(Qt.OpenHandCursor)
        self._x = max(-1.0, min(1.0, float(x)))
        self._y = max(-1.0, min(1.0, float(y)))
        self.update()

    def cancel_drag(self) -> None:
        """Release an active mouse grab and return to neutral."""
        if self._dragging:
            self._dragging = False
            self.releaseMouse()
            self.setCursor(Qt.OpenHandCursor)
            self.center(emit=True)

    def center(self, emit: bool = True) -> None:
        self._x = self._y = 0.0
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        self.update()
        if emit:
            self.value_changed.emit(0.0, 0.0)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self.isEnabled():
            return
        self._dragging = True
        self.setCursor(Qt.ClosedHandCursor)
        self._update_from_pos(event.position())
        self.grabMouse()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._update_from_pos(event.position())

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            self.cancel_drag()

    def focusOutEvent(self, event) -> None:
        self.cancel_drag()
        super().focusOutEvent(event)

    def hideEvent(self, event) -> None:
        self.cancel_drag()
        super().hideEvent(event)

    def _update_from_pos(self, pos: QPointF) -> None:
        cx = self.width() / 2
        cy = self.height() / 2
        radius = max(1.0, min(self.width(), self.height()) / 2 - 12)
        dx = (pos.x() - cx) / radius
        dy = -(pos.y() - cy) / radius
        length = math.hypot(dx, dy)
        if length > 1.0:
            dx /= length
            dy /= length
        self._x, self._y = dx, dy
        self.update()
        self.value_changed.emit(dx, dy)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cx = self.width() / 2
        cy = self.height() / 2
        radius = min(self.width(), self.height()) / 2 - 12
        enabled = self.isEnabled()

        base = QRadialGradient(cx, cy, radius)
        base.setColorAt(0, QColor(20, 34, 44, 245))
        base.setColorAt(1, QColor(8, 14, 19, 245))
        painter.setBrush(base)
        painter.setPen(QPen(QColor(43, 195, 171, 120 if enabled else 45), 2))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        painter.setPen(QPen(QColor(80, 110, 128, 100 if enabled else 35), 1))
        painter.drawEllipse(QPointF(cx, cy), radius * 0.66, radius * 0.66)
        painter.drawLine(QPointF(cx - radius * 0.8, cy), QPointF(cx + radius * 0.8, cy))
        painter.drawLine(QPointF(cx, cy - radius * 0.8), QPointF(cx, cy + radius * 0.8))

        knob_x = cx + self._x * radius * 0.68
        knob_y = cy - self._y * radius * 0.68
        knob = QRadialGradient(knob_x - 6, knob_y - 6, 26)
        if enabled:
            knob.setColorAt(0, QColor(120, 245, 225))
            knob.setColorAt(0.55, QColor(45, 190, 170))
            knob.setColorAt(1, QColor(16, 90, 82))
        else:
            knob.setColorAt(0, QColor(95, 110, 120))
            knob.setColorAt(1, QColor(45, 58, 68))
        painter.setBrush(knob)
        painter.setPen(QPen(QColor(220, 255, 250, 130 if enabled else 40), 1.5))
        painter.drawEllipse(QPointF(knob_x, knob_y), 19, 19)

        if not enabled:
            painter.setPen(QColor(110, 130, 145, 140))
            painter.drawText(self.rect(), Qt.AlignCenter, "锁定")


class BatteryBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._bars = 0
        self._warning = False
        self._critical = False

    def set_level(self, bars: int, warning: bool = False, critical: bool = False) -> None:
        self._bars = max(0, min(4, int(bars)))
        self._warning = warning
        self._critical = critical
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        n = 5
        gap = 5
        h = 12
        w = max(8.0, (self.width() - gap * (n - 1)) / n)
        y = (self.height() - h) / 2
        for i in range(n):
            rect = QRectF(i * (w + gap), y, w, h)
            if i < self._bars:
                if self._critical:
                    color = QColor("#ff5d5d")
                elif self._warning:
                    color = QColor("#f0b35a")
                else:
                    color = QColor("#37d6c0")
            else:
                color = QColor("#1b2a36")
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 3, 3)
