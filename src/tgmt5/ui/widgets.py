"""Reusable custom widgets: cards, KPI tiles, status pills, charts, timeline, toasts."""
from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Any, Callable, Coroutine, Sequence

from PySide6.QtCore import (
    QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer,
)
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QBoxLayout, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout,
    QLabel, QLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from tgmt5.ui.theme import palette

STATUS_COLORS = {
    "RECEIVED": "muted", "PARSED": "info", "VALIDATED": "info", "QUEUED": "info",
    "SUBMITTING": "warn", "WAITING": "warn", "PENDING_PLACED": "accent", "EXECUTED": "profit",
    "PARTIAL": "warn", "CLOSED": "muted", "REJECTED": "loss", "EXPIRED": "muted",
    "MANUAL_REVIEW": "warn", "FAILED": "loss", "CANCELLED": "muted", "UNCERTAIN": "loss",
    "MONITORED": "info", "SIMULATED": "paper",
}


def color(name: str) -> str:
    return getattr(palette(), name, name)


def label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    l = QLabel(text)
    if role:
        l.setProperty("role", role)
    l.setWordWrap(wrap)
    return l


def run_async(coro: Coroutine, ok: Callable[[Any], None] | None = None,
              err: Callable[[BaseException], None] | None = None) -> asyncio.Future:
    """Schedule a coroutine on the qasync loop; callbacks run on the GUI thread."""
    fut = asyncio.ensure_future(coro)

    def done(f: asyncio.Future) -> None:
        if f.cancelled():
            return
        e = f.exception()
        if e is not None:
            (err or (lambda x: Toast.show_error(str(x))))(e)
        elif ok is not None:
            ok(f.result())
    fut.add_done_callback(done)
    return fut


def money(v: float | None, ccy: str = "", sign: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{v:+,.2f}" if sign else f"{v:,.2f}"
    return f"{s} {ccy}".strip()


class Card(QFrame):
    def __init__(self, title: str | None = None, parent: QWidget | None = None,
                 padding: int = 18, shadow: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(padding, padding, padding, padding)
        self.lay.setSpacing(10)
        if title:
            hdr = QHBoxLayout()
            hdr.addWidget(label(title.upper(), "caption"))
            hdr.addStretch()
            self.header = hdr
            self.lay.addLayout(hdr)
        if shadow:
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(28)
            eff.setOffset(0, 6)
            eff.setColor(QColor(0, 0, 0, 70))
            self.setGraphicsEffect(eff)

    def add(self, w: QWidget | None = None, layout=None, stretch: int = 0):
        if w is not None:
            self.lay.addWidget(w, stretch)
        if layout is not None:
            self.lay.addLayout(layout, stretch)
        return self


class Pill(QLabel):
    def __init__(self, text: str = "", tone: str = "muted") -> None:
        super().__init__()
        self.set(text, tone)

    def set(self, text: str, tone: str | None = None) -> None:
        tone = tone or STATUS_COLORS.get(text, "muted")
        c = QColor(color(tone))
        bg = QColor(c)
        bg.setAlpha(38)
        self.setText(text.replace("_", " "))
        self.setStyleSheet(
            f"QLabel {{ color: {c.name()}; background: rgba({bg.red()},{bg.green()},{bg.blue()},0.16);"
            f" border: 1px solid rgba({c.red()},{c.green()},{c.blue()},0.45); border-radius: 9px;"
            f" padding: 2px 9px; font-size: 11px; font-weight: 700; }}")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)    # a pill never clips its text


class Dot(QWidget):
    def __init__(self, tone: str = "muted", size: int = 9) -> None:
        super().__init__()
        self._tone = tone
        self.setFixedSize(size + 6, size + 6)
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.update()

    def _tick(self) -> None:
        self._pulse = (self._pulse + 0.06) % 1.0
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(color(self._tone))
        r = self.width() / 2
        if self._tone in ("profit", "warn"):
            halo = QColor(c)
            halo.setAlphaF(0.35 * (1 - self._pulse))
            p.setBrush(halo)
            p.setPen(Qt.NoPen)
            rr = r * (0.6 + 0.4 * self._pulse)
            p.drawEllipse(QPointF(r, r), rr, rr)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(r, r), r - 3.5, r - 3.5)


class Kpi(Card):
    def __init__(self, title: str, value: str = "—", sub: str = "") -> None:
        super().__init__(None, padding=16)
        self.title = label(title.upper(), "caption")
        self.value = label(value, "kpi")
        self.sub = label(sub, "muted")
        self.lay.setSpacing(4)
        self.sub.setWordWrap(True)
        self.add(self.title).add(self.value).add(self.sub)
        self.setMinimumWidth(150)

    def set(self, value: str, sub: str | None = None, tone: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color(tone)};" if tone else "")
        if sub is not None:
            self.sub.setText(sub)


class Meter(QWidget):
    """Horizontal utilisation bar with threshold colouring."""

    def __init__(self) -> None:
        super().__init__()
        self.value = 0.0
        self.setFixedHeight(10)

    def set(self, frac: float) -> None:
        self.value = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(palette().surface2))
        p.drawRoundedRect(r, 5, 5)
        tone = "profit" if self.value < 0.6 else "warn" if self.value < 0.85 else "loss"
        g = QLinearGradient(0, 0, r.width(), 0)
        c = QColor(color(tone))
        g.setColorAt(0, c.lighter(115))
        g.setColorAt(1, c)
        p.setBrush(g)
        p.drawRoundedRect(QRectF(0, 0, max(10, r.width() * self.value), r.height()), 5, 5)


class LineChart(QWidget):
    """Anti-aliased line/area chart with gradient fill, axis labels and hover read-out."""

    def __init__(self, tone: str = "accent", fmt: Callable[[float], str] = lambda v: f"{v:,.2f}",
                 empty_text: str = "No data yet") -> None:
        super().__init__()
        self.tone = tone
        self.fmt = fmt
        self.empty_text = empty_text
        self.points: list[tuple[float, float]] = []
        self.labels: tuple[str, str] = ("", "")
        self.baseline: float | None = None
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self._hover: int | None = None

    def set_data(self, pts: Sequence[tuple[datetime | float, float]], baseline: float | None = None) -> None:
        conv = []
        for x, y in pts:
            conv.append((x.timestamp() if isinstance(x, datetime) else float(x), float(y)))
        self.points = conv
        self.baseline = baseline
        if pts and isinstance(pts[0][0], datetime):
            self.labels = (pts[0][0].strftime("%d %b %H:%M"), pts[-1][0].strftime("%d %b %H:%M"))
        self.update()

    def mouseMoveEvent(self, e) -> None:
        if len(self.points) > 1:
            frac = (e.position().x() - 72) / max(1, self.width() - 84)
            self._hover = max(0, min(len(self.points) - 1, round(frac * (len(self.points) - 1))))
            self.update()

    def leaveEvent(self, _e) -> None:
        self._hover = None
        self.update()

    def paintEvent(self, _e) -> None:
        pal = palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 72, 12, 10, 22
        cw, ch = w - left - right, h - top - bottom
        p.setPen(QPen(QColor(pal.muted)))
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        if len(self.points) < 2:
            p.drawText(self.rect(), Qt.AlignCenter, self.empty_text)
            return
        xs = [x for x, _ in self.points]
        ys = [y for _, y in self.points]
        y0, y1 = min(ys), max(ys)
        if self.baseline is not None:
            y0, y1 = min(y0, self.baseline), max(y1, self.baseline)
        if math.isclose(y0, y1):
            y0, y1 = y0 - 1, y1 + 1
        pad = (y1 - y0) * 0.1
        y0, y1 = y0 - pad, y1 + pad
        x0, x1 = xs[0], xs[-1] if xs[-1] != xs[0] else xs[0] + 1

        def pt(i: int) -> QPointF:
            return QPointF(left + (xs[i] - x0) / (x1 - x0) * cw, top + (1 - (ys[i] - y0) / (y1 - y0)) * ch)

        grid = QColor(pal.border)
        for k in range(4):
            yy = top + ch * k / 3
            p.setPen(QPen(grid, 1, Qt.DashLine))
            p.drawLine(QPointF(left, yy), QPointF(w - right, yy))
            p.setPen(QColor(pal.muted))
            v = y1 - (y1 - y0) * k / 3
            p.drawText(QRectF(0, yy - 8, left - 6, 16), Qt.AlignRight | Qt.AlignVCenter, self.fmt(v))
        p.drawText(QRectF(left, h - 18, cw / 2, 16), Qt.AlignLeft, self.labels[0])
        p.drawText(QRectF(left + cw / 2, h - 18, cw / 2, 16), Qt.AlignRight, self.labels[1])
        if self.baseline is not None:
            by = top + (1 - (self.baseline - y0) / (y1 - y0)) * ch
            p.setPen(QPen(QColor(pal.muted), 1, Qt.DotLine))
            p.drawLine(QPointF(left, by), QPointF(w - right, by))
        path = QPainterPath(pt(0))
        for i in range(1, len(xs)):
            path.lineTo(pt(i))
        c = QColor(color(self.tone))
        area = QPainterPath(path)
        area.lineTo(QPointF(pt(len(xs) - 1).x(), top + ch))
        area.lineTo(QPointF(left, top + ch))
        area.closeSubpath()
        g = QLinearGradient(0, top, 0, top + ch)
        c1, c2 = QColor(c), QColor(c)
        c1.setAlpha(90)
        c2.setAlpha(0)
        g.setColorAt(0, c1)
        g.setColorAt(1, c2)
        p.fillPath(area, QBrush(g))
        p.setPen(QPen(c, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        if self._hover is not None:
            hp = pt(self._hover)
            p.setPen(QPen(QColor(pal.muted), 1, Qt.DashLine))
            p.drawLine(QPointF(hp.x(), top), QPointF(hp.x(), top + ch))
            p.setBrush(c)
            p.setPen(QPen(QColor(pal.surface), 2))
            p.drawEllipse(hp, 4.5, 4.5)
            txt = self.fmt(ys[self._hover])
            box = QRectF(min(hp.x() + 8, w - 100), max(top, hp.y() - 28), 90, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.surface2))
            p.drawRoundedRect(box, 6, 6)
            p.setPen(QColor(pal.text))
            p.drawText(box, Qt.AlignCenter, txt)


class BarChart(QWidget):
    """Vertical bars; positive bars in profit colour, negative in loss colour."""

    def __init__(self, signed: bool = True, tone: str = "accent", horizontal: bool = False) -> None:
        super().__init__()
        self.items: list[tuple[str, float]] = []
        self.signed = signed
        self.tone = tone
        self.horizontal = horizontal
        self.setMinimumHeight(170)

    def set_data(self, items: Sequence[tuple[str, float]]) -> None:
        self.items = list(items)
        self.update()

    def paintEvent(self, _e) -> None:
        pal = palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        if not self.items:
            p.setPen(QColor(pal.muted))
            p.drawText(self.rect(), Qt.AlignCenter, "No data yet")
            return
        if self.horizontal:
            self._paint_h(p, pal)
            return
        w, h = self.width(), self.height()
        top, bottom = 12, 22
        ch = h - top - bottom
        vals = [v for _, v in self.items]
        mx = max(max(vals), 0)
        mn = min(min(vals), 0)
        rng = (mx - mn) or 1
        zero = top + mx / rng * ch
        n = len(self.items)
        slot = w / n
        bw = min(34, slot * 0.6)
        p.setPen(QPen(QColor(pal.border)))
        p.drawLine(QPointF(0, zero), QPointF(w, zero))
        for i, (lab, v) in enumerate(self.items):
            x = slot * i + (slot - bw) / 2
            hh = abs(v) / rng * ch
            y = zero - hh if v >= 0 else zero
            tone = (self.tone if not self.signed else ("profit" if v >= 0 else "loss"))
            c = QColor(color(tone))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x, y, bw, max(hh, 1.5)), 4, 4)
            if n <= 16:
                p.setPen(QColor(pal.muted))
                p.drawText(QRectF(slot * i, h - 18, slot, 16), Qt.AlignCenter, lab[-5:])

    def _paint_h(self, p: QPainter, pal) -> None:
        w = self.width()
        row = min(28, self.height() / max(1, len(self.items)))
        mx = max(v for _, v in self.items) or 1
        for i, (lab, v) in enumerate(self.items):
            y = i * row
            p.setPen(QColor(pal.muted))
            p.drawText(QRectF(0, y, 120, row), Qt.AlignVCenter | Qt.AlignLeft, lab)
            bw = (w - 170) * v / mx
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color(self.tone)))
            p.drawRoundedRect(QRectF(124, y + row * 0.25, max(bw, 2), row * 0.5), 4, 4)
            p.setPen(QColor(pal.text))
            p.drawText(QRectF(130 + bw, y, 40, row), Qt.AlignVCenter, f"{v:g}")


class Donut(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.wins = 0
        self.losses = 0
        self.setMinimumSize(QSize(150, 150))

    def set(self, wins: int, losses: int) -> None:
        self.wins, self.losses = wins, losses
        self.update()

    def paintEvent(self, _e) -> None:
        pal = palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = min(self.width(), self.height()) - 20
        r = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        total = self.wins + self.losses
        pen = QPen(QColor(pal.surface2), 14, Qt.SolidLine, Qt.FlatCap)
        p.setPen(pen)
        p.drawArc(r, 0, 360 * 16)
        if total:
            span = int(360 * 16 * self.wins / total)
            pen.setColor(QColor(pal.profit))
            p.setPen(pen)
            p.drawArc(r, 90 * 16, -span)
            pen.setColor(QColor(pal.loss))
            p.setPen(pen)
            p.drawArc(r, 90 * 16 - span, -(360 * 16 - span))
        p.setPen(QColor(pal.text))
        f = QFont(self.font())
        f.setPointSizeF(17)
        f.setBold(True)
        p.setFont(f)
        txt = f"{self.wins / total * 100:.0f}%" if total else "—"
        p.drawText(r.adjusted(0, -10, 0, -10), Qt.AlignCenter, txt)
        f.setPointSizeF(8.5)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(pal.muted))
        p.drawText(r.adjusted(0, 26, 0, 26), Qt.AlignCenter, f"{self.wins}W / {self.losses}L")


TIMELINE_OK = ["RECEIVED", "PARSED", "VALIDATED", "QUEUED", "EXECUTED"]


class Timeline(QWidget):
    """RECEIVED → PARSED → VALIDATED → QUEUED → EXECUTED with the terminal state highlighted."""

    def __init__(self) -> None:
        super().__init__()
        self.reached: set[str] = set()
        self.final = ""
        self.setFixedHeight(40)

    def set(self, history: Sequence[str], final: str) -> None:
        self.reached = set(history)
        self.final = final
        self.update()

    def paintEvent(self, _e) -> None:
        pal = palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        steps = list(TIMELINE_OK)
        off_path = self.final not in TIMELINE_OK
        if off_path:
            steps[-1] = self.final
        if self.final in ("CLOSED", "PENDING_PLACED", "SIMULATED", "PARTIAL"):
            steps[-1] = self.final
            off_path = False
        n = len(steps)
        w = self.width()
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        f.setBold(True)
        p.setFont(f)
        xs = [52 + (w - 104) * i / (n - 1) for i in range(n)]
        for i in range(n - 1):
            def hit(k: int) -> bool:
                return steps[k] in self.reached or (k == n - 1 and self.final == steps[-1])
            done = hit(i) and hit(i + 1)
            p.setPen(QPen(QColor(pal.accent if done else pal.border), 2))
            p.drawLine(QPointF(xs[i] + 6, 10), QPointF(xs[i + 1] - 6, 10))
        for i, s in enumerate(steps):
            last = i == n - 1
            reached = s in self.reached or (last and self.final == s)
            tone = STATUS_COLORS.get(s, "accent") if last else ("accent" if reached else "border")
            c = QColor(color(tone) if tone != "border" else pal.border)
            p.setPen(Qt.NoPen)
            p.setBrush(c if reached else QColor(pal.surface2))
            p.drawEllipse(QPointF(xs[i], 10), 6, 6)
            if not reached:
                p.setPen(QPen(c, 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(xs[i], 10), 5, 5)
            p.setPen(QColor(pal.text if reached else pal.muted))
            p.drawText(QRectF(xs[i] - 50, 20, 100, 16), Qt.AlignCenter, s.replace("_", " "))


class Toast(QFrame):
    _host: QWidget | None = None
    _stack: list["Toast"] = []

    @classmethod
    def install(cls, host: QWidget) -> None:
        cls._host = host

    @classmethod
    def show_message(cls, text: str, tone: str = "info", ms: int = 4500) -> None:
        if cls._host is None:
            return
        t = Toast(cls._host, text, tone)
        cls._stack.append(t)
        cls._relayout()
        t.show()
        t.raise_()
        QTimer.singleShot(ms, t._fade)

    @classmethod
    def show_error(cls, text: str) -> None:
        cls.show_message(text, "loss", 7000)

    @classmethod
    def _relayout(cls) -> None:
        if cls._host is None:
            return
        y = cls._host.height() - 24
        for t in reversed(cls._stack):
            t.adjustSize()
            y -= t.height() + 10
            t.move(cls._host.width() - t.width() - 24, y)

    def __init__(self, host: QWidget, text: str, tone: str) -> None:
        super().__init__(host)
        pal = palette()
        c = color(tone)
        self.setStyleSheet(f"QFrame {{ background: {pal.surface2}; border: 1px solid {pal.border};"
                           f" border-left: 4px solid {c}; border-radius: 10px; }}"
                           f" QLabel {{ border: none; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        l = label(text, wrap=True)
        l.setMaximumWidth(380)
        lay.addWidget(l)
        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._anim = QPropertyAnimation(self._eff, b"opacity", self)
        self._anim.setDuration(220)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()

    def _fade(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._remove)
        self._anim.start()

    def _remove(self) -> None:
        if self in Toast._stack:
            Toast._stack.remove(self)
        self.deleteLater()
        Toast._relayout()


class ReviewTick(QToolButton):
    """Red ✓ button next to a critical setting. Clicking it marks the setting as reviewed:
    the button and its fields go back to the normal theme colours. ``set_pending`` turns
    the fields red again (e.g. after the value changed)."""

    def __init__(self, key: str, *fields: QWidget) -> None:
        super().__init__()
        self.key = key
        self.fields = list(fields)
        self.setObjectName("ReviewTick")
        self.setText("✓")
        self.setCheckable(True)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.set_pending(True)

    def set_pending(self, pending: bool) -> None:
        self.blockSignals(True)
        self.setChecked(not pending)
        self.blockSignals(False)
        self.setToolTip("Not reviewed yet: check this setting, then click ✓" if pending else
                        "Reviewed. Changing the value makes it red again; click to un-tick.")
        for w in self.fields:
            _set_review_prop(w, "pending" if pending else "")

    def set_required(self, required: bool) -> None:
        """Hide the tick (and drop the red marking) for settings that do not apply right now."""
        self.setVisible(required)
        if not required:
            for w in self.fields:
                _set_review_prop(w, "")


def _as_widget(item) -> QWidget:
    if isinstance(item, QWidget):
        return item
    w = QWidget()
    w.setLayout(item)
    return w


class ResponsiveRow(QWidget):
    """Widgets side by side when the row is at least ``breakpoint`` px wide, stacked otherwise."""

    def __init__(self, *items, breakpoint: int = 900, spacing: int = 16,
                 stretches: Sequence[int] | None = None) -> None:
        super().__init__()
        self._bp = breakpoint
        self._lay = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(spacing)
        # the row must be allowed to get narrower than its side-by-side minimum, otherwise it could
        # never drop below the breakpoint and switch to the stacked layout
        self._lay.setSizeConstraint(QLayout.SetNoConstraint)
        self._widgets = [_as_widget(it) for it in items]
        for i, w in enumerate(self._widgets):
            self._lay.addWidget(w, stretches[i] if stretches else 1)

    def minimumSizeHint(self) -> QSize:
        w = max((x.minimumSizeHint().width() for x in self._widgets), default=0)
        return QSize(w, self._lay.minimumSize().height())

    def resizeEvent(self, e) -> None:
        d = QBoxLayout.LeftToRight if self.width() >= self._bp else QBoxLayout.TopToBottom
        if self._lay.direction() != d:
            self._lay.setDirection(d)
        super().resizeEvent(e)


class ResponsiveGrid(QWidget):
    """Grid that re-flows its widgets into as many columns (from ``columns``) as fit."""

    def __init__(self, widgets: Sequence[QWidget], *, min_width: int = 200,
                 columns: Sequence[int] = (6, 4, 3, 2, 1), spacing: int = 14) -> None:
        super().__init__()
        self._items = list(widgets)
        self._min, self._choices, self._cols = min_width, tuple(columns), 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self._place(self._choices[0])

    def _place(self, cols: int) -> None:
        if cols == self._cols:
            return
        for w in self._items:
            self._grid.removeWidget(w)
        for c in range(max(self._cols, cols)):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)
        for i, w in enumerate(self._items):
            self._grid.addWidget(w, i // cols, i % cols)
        self._cols = cols

    def resizeEvent(self, e) -> None:
        sp = self._grid.spacing()
        fit = next((c for c in self._choices if c * self._min + (c - 1) * sp <= self.width()), 1)
        self._place(fit)
        super().resizeEvent(e)


def _set_review_prop(w: QWidget, value: str) -> None:
    if w.property("review") == value:
        return
    w.setProperty("review", value)
    w.style().unpolish(w)
    w.style().polish(w)


def hbox(*widgets, spacing: int = 12, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    l = QHBoxLayout()
    l.setSpacing(spacing)
    l.setContentsMargins(*margins)
    for w in widgets:
        if w == "stretch":
            l.addStretch()
        elif isinstance(w, QWidget):
            l.addWidget(w)
        else:
            l.addLayout(w)
    return l


def vbox(*widgets, spacing: int = 12, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    l = QVBoxLayout()
    l.setSpacing(spacing)
    l.setContentsMargins(*margins)
    for w in widgets:
        if w == "stretch":
            l.addStretch()
        elif isinstance(w, QWidget):
            l.addWidget(w)
        else:
            l.addLayout(w)
    return l
