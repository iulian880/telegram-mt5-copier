"""Design tokens and Qt style sheet for the dark (default) and light themes."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    surface2: str
    border: str
    text: str
    muted: str
    accent: str
    accent_soft: str
    profit: str
    loss: str
    warn: str
    info: str
    paper: str


DARK = Palette(bg="#0A0E16", surface="#111725", surface2="#172033", border="#222C42",
               text="#E7ECF5", muted="#8792A8", accent="#5B8CFF", accent_soft="#1C2A4D",
               profit="#22C55E", loss="#F05252", warn="#F5A524", info="#38BDF8", paper="#A78BFA")
LIGHT = Palette(bg="#F4F6FB", surface="#FFFFFF", surface2="#F0F3F9", border="#DDE3EE",
                text="#111827", muted="#5F6B85", accent="#3B6EF5", accent_soft="#E3EBFF",
                profit="#16A34A", loss="#DC2626", warn="#D97706", info="#0284C7", paper="#7C3AED")

_current = DARK


def palette() -> Palette:
    return _current


def set_theme(name: str) -> Palette:
    global _current
    _current = LIGHT if name == "light" else DARK
    return _current


FONT_STACK = '"Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'
MONO_STACK = '"Cascadia Mono", "JetBrains Mono", Consolas, "DejaVu Sans Mono", monospace'


def stylesheet(p: Palette) -> str:
    return f"""
* {{ font-family: {FONT_STACK}; font-size: 13px; color: {p.text}; }}
QMainWindow, QWidget#Root {{ background: {p.bg}; }}
QWidget#Page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {p.bg}; border: none; }}
QLabel {{ background: transparent; }}
QLabel[role="h1"] {{ font-size: 22px; font-weight: 650; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {p.muted}; }}
QLabel[role="kpi"] {{ font-size: 22px; font-weight: 650; }}
QLabel[role="caption"] {{ color: {p.muted}; font-size: 11px; font-weight: 600; letter-spacing: 0.6px; }}
QLabel[role="mono"] {{ font-family: {MONO_STACK}; font-size: 12px; color: {p.text}; }}
QFrame#Card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 14px; }}
QFrame#Sidebar {{ background: {p.surface}; border-right: 1px solid {p.border}; }}
QFrame#TopBar {{ background: {p.bg}; border-bottom: 1px solid {p.border}; }}
QFrame#Message {{ background: {p.surface2}; border-radius: 10px; border: 1px solid {p.border}; }}
QFrame#Banner {{ border-radius: 10px; }}
QPushButton {{ background: {p.surface2}; border: 1px solid {p.border}; border-radius: 9px;
              padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:disabled {{ color: {p.muted}; }}
QPushButton[kind="primary"] {{ background: {p.accent}; border-color: {p.accent}; color: white; }}
QPushButton[kind="danger"] {{ background: {p.loss}; border-color: {p.loss}; color: white; }}
QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; color: {p.muted}; }}
QPushButton#Nav {{ background: transparent; border: none; border-radius: 10px; text-align: left;
                   padding: 10px 14px; color: {p.muted}; font-weight: 600; }}
QPushButton#Nav:hover {{ background: {p.surface2}; color: {p.text}; }}
QPushButton#Nav:checked {{ background: {p.accent_soft}; color: {p.text}; }}
QPushButton#Stop {{ background: {p.loss}; color: white; border: none; border-radius: 10px;
                    padding: 9px 18px; font-weight: 750; letter-spacing: 0.5px; }}
QPushButton#Stop:hover {{ background: #ff3b3b; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p.surface2}; border: 1px solid {p.border}; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {p.accent}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; border: none; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {p.accent}; }}
QComboBox QAbstractItemView {{ background: {p.surface}; border: 1px solid {p.border}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {p.border};
                        background: {p.surface2}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QSlider::groove:horizontal {{ height: 6px; background: {p.surface2}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; border: 2px solid {p.accent}; width: 14px;
                              margin: -6px 0; border-radius: 9px; }}
QProgressBar {{ background: {p.surface2}; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
QTableView, QTableWidget {{ background: {p.surface}; border: none; gridline-color: transparent;
    alternate-background-color: {p.surface2}; selection-background-color: {p.accent_soft};
    selection-color: {p.text}; }}
QHeaderView::section {{ background: {p.surface}; color: {p.muted}; border: none;
    border-bottom: 1px solid {p.border}; padding: 8px 6px; font-weight: 600; font-size: 11px; }}
QTableCornerButton::section {{ background: {p.surface}; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 4px; min-width: 30px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {p.muted}; padding: 8px 14px; font-weight: 600;
                border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom-color: {p.accent}; }}
QToolTip {{ background: {p.surface2}; color: {p.text}; border: 1px solid {p.border}; padding: 6px; }}
QDialog {{ background: {p.surface}; }}
QMessageBox {{ background: {p.surface}; }}
"""
