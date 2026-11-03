"""Main window: sidebar navigation, top status bar with EMERGENCY STOP, page stack."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
    QSystemTrayIcon, QVBoxLayout, QWidget,
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
from tgmt5.ui.widgets import Dot, Pill, Toast, hbox, label, run_async


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
        self.resize(1480, 940)
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
        sl.setContentsMargins(14, 20, 14, 18)
        sl.setSpacing(4)
        brand = label("◆ TGMT5", "h1")
        brand.setStyleSheet("font-size: 20px; letter-spacing: 1px;")
        sl.addWidget(brand)
        sl.addWidget(label("Telegram → MT5 copier", "muted"))
        sl.addSpacing(18)
        self.stack = QStackedWidget()
        self.pages = []
        self.navgroup = QButtonGroup(self)
        for i, (name, cls) in enumerate(NAV):
            b = QPushButton(f"  {name}")
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.navgroup.addButton(b, i)
            sl.addWidget(b)
            page = cls(svc)
            self.pages.append(page)
            self.stack.addWidget(page)
        self.navgroup.idClicked.connect(self.goto)
        self.navgroup.button(0).setChecked(True)
        sl.addStretch()
        self.review_badge = label("", "muted", wrap=True)
        sl.addWidget(self.review_badge)
        sl.addWidget(label("DEMO accounts only.\nLive trading is blocked.", "muted", wrap=True))
        outer.addWidget(side)

        # ---- top bar
        main = QVBoxLayout()
        main.setSpacing(0)
        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(64)
        self.mode_pill = Pill("MONITOR", "info")
        self.sandbox_pill = Pill("SANDBOX — SIMULATED BROKER & TELEGRAM", "warn")
        self.sandbox_pill.setVisible(svc.sandbox)
        self.mt5_dot, self.tg_dot = Dot(), Dot()
        self.mt5_lbl, self.tg_lbl = label("MT5", "muted"), label("Telegram", "muted")
        self.lock_lbl = label("", wrap=False)
        self.stop = QPushButton("EMERGENCY STOP")
        self.stop.setObjectName("Stop")
        self.stop.setCursor(Qt.PointingHandCursor)
        self.stop.clicked.connect(self._emergency)
        self.flatten = QPushButton("Flatten all…")
        self.flatten.setProperty("kind", "ghost")
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
            self.tray.setIcon(self.windowIcon())
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
        tone = {"MONITOR": "info", "PAPER": "paper", "DEMO": "profit"}[s.mode]
        self.mode_pill.set({"MONITOR": "MONITOR ONLY", "PAPER": "PAPER TRADING",
                            "DEMO": "DEMO AUTO-EXECUTION"}[s.mode], tone)
        self.mt5_dot.set_tone("profit" if s.mt5_connected else "loss")
        tg = s.telegram
        self.tg_dot.set_tone("profit" if tg["connected"] and tg["authorized"] else
                             "warn" if tg["connected"] else "loss")
        self.lock_lbl.setText(f"⚠ {s.blocking_reason}" if s.blocking_reason else "")
        self.lock_lbl.setStyleSheet("color: #F05252; font-weight: 600;")
        self.review_badge.setText(f"⚑ {s.review_queue} signal(s) awaiting review" if s.review_queue else "")
        self.review_badge.setStyleSheet("color: #F5A524;")
        page = self.stack.currentWidget()
        page.refresh(s)
        for p in self.pages:
            if p is not page and isinstance(p, (DashboardPage,)):
                pass

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
