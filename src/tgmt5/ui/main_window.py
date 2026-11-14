"""Main window: sidebar navigation, top status bar with EMERGENCY STOP, page stack."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
    QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from tgmt5.services.app import AppService, Snapshot
from tgmt5.ui.pages.connection import ConnectionPage
from tgmt5.ui.pages.dashboard import DashboardPage
from tgmt5.ui.pages.history import HistoryPage
from tgmt5.ui.pages.log import LogPage
from tgmt5.ui.pages.positions import PositionsPage
from tgmt5.ui.pages.risk import RiskPage
from tgmt5.ui.pages.signals import SignalsPage
from tgmt5.ui.pages.sources import SourcesPage
from tgmt5.ui import icons
from tgmt5.ui.theme import palette
from tgmt5.ui.widgets import Dot, NavButton, Pill, Toast, hbox, label, run_async, vbox


class Bridge(QObject):
    """Thread-safe hop from the MT5 worker thread to the GUI thread."""
    event = Signal(str, dict)


NAV = [("Dashboard", DashboardPage), ("Signals", SignalsPage), ("Positions", PositionsPage),
       ("Risk", RiskPage), ("Sources", SourcesPage), ("History", HistoryPage), ("Log", LogPage),
       ("Connection", ConnectionPage)]


class MainWindow(QMainWindow):
    def __init__(self, svc: AppService) -> None:
        super().__init__()
        self.svc = svc
        self.setWindowTitle("TGMT5 — Telegram → MetaTrader 5" + ("  [SANDBOX]" if svc.sandbox else ""))
        self.setWindowIcon(icons.app_icon())
        self.resize(1480, 940)
        self.setMinimumSize(1024, 640)
        root = QWidget()
        root.setObjectName("Root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- sidebar
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 20, 14, 16)
        sl.setSpacing(3)
        pal = palette()
        logo = QLabel()
        logo.setPixmap(icons.app_icon().pixmap(34, 34))
        logo.setFixedSize(34, 34)
        name = label("TGMT5", "h1")
        name.setStyleSheet("font-size: 18px; letter-spacing: 0.5px;")
        tag = label("Telegram → MT5 copier", "muted")
        tag.setStyleSheet("font-size: 11px;")
        brand = hbox(logo, vbox(name, tag, spacing=0), "stretch", spacing=10)
        sl.addLayout(brand)
        sl.addSpacing(20)
        sl.addWidget(label("WORKSPACE", "caption"))
        sl.addSpacing(4)
        self.stack = QStackedWidget()
        self.pages = []
        self.navgroup = QButtonGroup(self)
        self.nav: list[NavButton] = []
        for i, (name, cls) in enumerate(NAV):
            if name == "Sources":
                sl.addSpacing(12)
                sl.addWidget(label("SETUP & RECORDS", "caption"))
                sl.addSpacing(4)
            b = NavButton(name, icons.nav_icon(name.lower(), pal.muted, pal.text), f"Ctrl+{i + 1}")
            self.navgroup.addButton(b, i)
            self.nav.append(b)
            sl.addWidget(b)
            page = cls(svc)
            self.pages.append(page)
            self.stack.addWidget(page)
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.goto(i))
        self.navgroup.idClicked.connect(self.goto)
        self.navgroup.button(0).setChecked(True)
        sl.addStretch()
        foot = QFrame()
        foot.setObjectName("SideFoot")
        self.review_badge = label("", wrap=True)
        self.review_badge.setStyleSheet(f"color: {pal.warn}; font-weight: 600;")
        self.review_badge.setCursor(Qt.PointingHandCursor)
        self.review_badge.mousePressEvent = lambda _e: self.goto(1)
        self.account_note = label("", "muted", wrap=True)
        self.account_note.setStyleSheet("font-size: 11px;")
        foot.setLayout(vbox(self.review_badge, self.account_note, spacing=6, margins=(12, 10, 12, 10)))
        sl.addWidget(foot)
        outer.addWidget(side)

        # ---- top bar
        main = QVBoxLayout()
        main.setSpacing(0)
        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(64)
        self.mode_pill = Pill("MONITOR", "info")
        self.sandbox_pill = Pill("SANDBOX · SIMULATED", "warn")
        self.sandbox_pill.setVisible(svc.sandbox)
        self.mt5_dot, self.tg_dot = Dot(), Dot()
        self.mt5_lbl, self.tg_lbl = label("MT5", "muted"), label("Telegram", "muted")
        self.lock_lbl = label("", wrap=False)
        self.lock_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # never widens the bar
        self.stop = QPushButton("EMERGENCY STOP")
        self.stop.setObjectName("Stop")
        self.stop.setCursor(Qt.PointingHandCursor)
        self.stop.clicked.connect(self._emergency)
        self.stop.setIcon(icons.icon("stop", "white", 16))
        self.stop.setToolTip("Disable execution immediately and cancel waiting signals.\n"
                             "Open positions keep their SL/TP and are not closed.")
        self.flatten = QPushButton("Flatten all…")
        self.flatten.setObjectName("Flatten")
        self.flatten.setCursor(Qt.PointingHandCursor)
        self.flatten.setToolTip("Close every position and pending order opened by this app")
        self.flatten.clicked.connect(self._flatten)
        top.setLayout(hbox(self.mode_pill, self.sandbox_pill, self.lock_lbl, "stretch",
                           self.mt5_dot, self.mt5_lbl, self.tg_dot, self.tg_lbl, self.flatten, self.stop,
                           spacing=10, margins=(24, 10, 24, 10)))
        main.addWidget(top)
        main.addWidget(self.stack, 1)
        outer.addLayout(main, 1)
        self.setCentralWidget(root)
        Toast.install(self)

        # ---- events & refresh
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        svc.subscribe(lambda k, d: self.bridge.event.emit(k, d) if k != "tick" else None)
        self.tray = QSystemTrayIcon(self) if QSystemTrayIcon.isSystemTrayAvailable() else None
        if self.tray:
            self.tray.setIcon(icons.app_icon())
            self.tray.setToolTip("TGMT5")
            self.tray.activated.connect(lambda _r: (self.showNormal(), self.raise_(), self.activateWindow()))
            self.tray.show()
        self._busy = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(1000)
        QTimer.singleShot(100, self._poll)

    def goto(self, i: int) -> None:
        self.navgroup.button(i).setChecked(True)
        self.stack.setCurrentIndex(i)
        self.pages[i].activated()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        Toast._relayout()

    # ------------------------------------------------------------------
    def _poll(self) -> None:
        if self._busy:
            return
        self._busy = True

        def done(s: Snapshot) -> None:
            self._busy = False
            self._apply(s)

        def fail(e) -> None:
            self._busy = False
        run_async(self.svc.snapshot(), done, fail)

    def _apply(self, s: Snapshot) -> None:
        tone = {"MONITOR": "info", "PAPER": "paper", "DEMO": "profit", "LIVE": "loss"}[s.mode]
        self.mode_pill.set({"MONITOR": "MONITOR ONLY", "PAPER": "PAPER TRADING",
                            "DEMO": "DEMO AUTO-EXECUTION", "LIVE": "REAL ACCOUNT · REAL MONEY"}[s.mode], tone)
        self.account_note.setText("Real-money accounts allowed\n(REAL ACCOUNT mode)."
                                  if self.svc.config.mt5.allow_real_account else
                                  "DEMO accounts only.\nReal-money trading is off.")
        pal = palette()
        self.lock_lbl.setToolTip(s.blocking_reason or "")
        self.mt5_dot.set_tone("profit" if s.mt5_connected else "loss")
        self.mt5_lbl.setToolTip("MetaTrader 5 " + ("connected" if s.mt5_connected else "not connected"))
        tg = s.telegram
        self.tg_dot.set_tone("profit" if tg["connected"] and tg["authorized"] else
                             "warn" if tg["connected"] else "loss")
        self.tg_lbl.setToolTip("Telegram " + ("listening" if tg["connected"] and tg["authorized"] else
                                              "connected, not signed in" if tg["connected"] else "offline"))
        self.lock_lbl.setText(f"⚠ {s.blocking_reason}" if s.blocking_reason else "")
        self.lock_lbl.setStyleSheet(f"color: {pal.loss}; font-weight: 600;")
        self.review_badge.setText(f"⚑ {s.review_queue} signal(s) awaiting review" if s.review_queue else "")
        self.review_badge.setVisible(bool(s.review_queue))
        self.nav[1].set_badge(str(s.review_queue) if s.review_queue else "", "warn")
        self.nav[2].set_badge(str(len(s.positions)) if s.positions else "", "accent")
        if self.tray:
            self.tray.setToolTip(f"TGMT5 · {s.mode}" + (f" · {len(s.positions)} position(s)" if s.positions else ""))
        page = self.stack.currentWidget()
        page.refresh(s)

    def _on_event(self, kind: str, data: dict) -> None:
        for p in self.pages:
            p.on_event(kind, data)
        if kind == "notify" and self.svc.config.notifications.desktop:
            ev = data.get("event", "")
            tone = {"executed": "profit", "rejected": "loss", "closed": "info"}.get(ev, "info")
            Toast.show_message(f"{data.get('title', '')}\n{data.get('body', '')}", tone)
            if self.tray and not self.isActiveWindow():
                self.tray.showMessage(data.get("title", "TGMT5"), data.get("body", ""))

    def _emergency(self) -> None:
        run_async(self.svc.emergency_stop("manual STOP button"),
                  lambda _: Toast.show_message("EMERGENCY STOP engaged: execution disabled, waiting "
                                               "signals cancelled. Open positions are untouched.", "loss"))

    def _flatten(self) -> None:
        if QMessageBox.warning(self, "Flatten all",
                               "Close ALL positions and cancel ALL pending orders opened by this "
                               "application?\nPositions you opened manually are not affected.",
                               QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        run_async(self.svc.flatten_all(),
                  lambda res: Toast.show_message(f"Flatten all: {len(res)} order(s) processed", "warn"))

    def closeEvent(self, e) -> None:
        self.timer.stop()
        super().closeEvent(e)
