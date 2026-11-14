"""Vector icons drawn with QPainter (no icon font or image files needed), plus the app icon.

Every icon is drawn on a 24 x 24 grid and scaled, so it stays sharp at any DPI.
``nav_icon`` returns a QIcon with a muted "off" state and a highlighted "on" state, which
checkable buttons (the sidebar) switch between automatically.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

ASSETS = Path(__file__).resolve().parent / "assets"


def app_icon() -> QIcon:
    """The TGMT5 tile (same artwork as packaging/tgmt5.ico) for windows, tray and dialogs."""
    p = ASSETS / "tgmt5.png"
    return QIcon(str(p)) if p.exists() else QIcon()


def _path(*segments) -> QPainterPath:
    """Build a path from ("M", x, y), ("L", x, y), ("C", x1, y1, x2, y2, x, y), ("Z",) tuples."""
    path = QPainterPath()
    for seg in segments:
        op, *v = seg
        if op == "M":
            path.moveTo(*v)
        elif op == "L":
            path.lineTo(*v)
        elif op == "C":
            path.cubicTo(*v)
        elif op == "Z":
            path.closeSubpath()
    return path


def _draw(p: QPainter, name: str, c: QColor) -> None:
    pen = QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if name == "dashboard":
        for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)):
            p.drawRoundedRect(QRectF(x, y, 7, 7), 2, 2)
    elif name == "signals":
        p.drawPath(_path(("M", 3, 12), ("L", 7, 12), ("L", 9.5, 5.5), ("L", 14.5, 18.5), ("L", 17, 12),
                         ("L", 21, 12)))
    elif name == "positions":
        p.drawLine(QPointF(8, 3.5), QPointF(8, 20.5))
        p.drawLine(QPointF(16, 3.5), QPointF(16, 17.5))
        p.setBrush(c)
        p.drawRoundedRect(QRectF(5.5, 8, 5, 8), 1.2, 1.2)
        p.setBrush(QColor(0, 0, 0, 0))
        p.drawRoundedRect(QRectF(13.5, 6, 5, 7.5), 1.2, 1.2)
    elif name == "risk":
        p.drawPath(_path(("M", 12, 3), ("L", 19.5, 6), ("L", 19.5, 11.5),
                         ("C", 19.5, 16, 16.3, 19.3, 12, 21), ("C", 7.7, 19.3, 4.5, 16, 4.5, 11.5),
                         ("L", 4.5, 6), ("Z",)))
        p.drawPath(_path(("M", 9, 12), ("L", 11.2, 14.2), ("L", 15.2, 9.8)))
    elif name == "sources":
        p.drawPath(_path(("M", 3, 11.2), ("L", 20.5, 4), ("L", 16.8, 20), ("L", 11.4, 14.6), ("Z",)))
        p.drawLine(QPointF(11.4, 14.6), QPointF(20.5, 4))
    elif name == "history":
        p.drawRoundedRect(QRectF(4, 13, 4, 7), 1, 1)
        p.drawRoundedRect(QRectF(10, 8.5, 4, 11.5), 1, 1)
        p.drawRoundedRect(QRectF(16, 4, 4, 16), 1, 1)
    elif name == "log":
        for y in (6.5, 12, 17.5):
            p.drawLine(QPointF(9, y), QPointF(20, y))
            p.setBrush(c)
            p.drawEllipse(QPointF(5, y), 1.1, 1.1)
            p.setBrush(Qt.NoBrush)
    elif name == "connection":
        p.drawLine(QPointF(9, 3), QPointF(9, 7))
        p.drawLine(QPointF(15, 3), QPointF(15, 7))
        p.drawPath(_path(("M", 6, 7), ("L", 18, 7), ("L", 18, 10.5), ("C", 18, 13.8, 15.4, 16, 12, 16),
                         ("C", 8.6, 16, 6, 13.8, 6, 10.5), ("Z",)))
        p.drawLine(QPointF(12, 16), QPointF(12, 21))
    elif name == "flatten":
        p.drawPath(_path(("M", 4, 7), ("L", 20, 7)))
        p.drawPath(_path(("M", 4, 12), ("L", 20, 12)))
        p.drawPath(_path(("M", 4, 17), ("L", 20, 17)))
    elif name == "stop":
        p.drawPath(_path(("M", 8.3, 3), ("L", 15.7, 3), ("L", 21, 8.3), ("L", 21, 15.7), ("L", 15.7, 21),
                         ("L", 8.3, 21), ("L", 3, 15.7), ("L", 3, 8.3), ("Z",)))
        p.drawLine(QPointF(9, 12), QPointF(15, 12))


def pixmap(name: str, color: str, size: int = 18, dpr: float = 2.0) -> QPixmap:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24, size / 24)
    _draw(p, name, QColor(color))
    p.end()
    return pm


def nav_icon(name: str, off: str, on: str, size: int = 18) -> QIcon:
    icon = QIcon()
    icon.addPixmap(pixmap(name, off, size), QIcon.Normal, QIcon.Off)
    icon.addPixmap(pixmap(name, on, size), QIcon.Normal, QIcon.On)
    icon.addPixmap(pixmap(name, on, size), QIcon.Active, QIcon.Off)
    icon.addPixmap(pixmap(name, on, size), QIcon.Active, QIcon.On)
    return icon


def icon(name: str, color: str, size: int = 16) -> QIcon:
    return QIcon(pixmap(name, color, size))
