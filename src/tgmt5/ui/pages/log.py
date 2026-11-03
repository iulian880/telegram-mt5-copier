from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem,
)

from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.theme import palette
from tgmt5.ui.widgets import Card, hbox

LEVELS = ["All", "CRITICAL", "ERROR", "WARNING", "INFO"]


class LogPage(Page):
    title = "Activity & Error Log"
    subtitle = "Messages, interpretations, risk decisions, broker requests and responses, reconnects and " \
               "safety events. Credentials are redacted before anything is written."

    def build(self) -> None:
        self.level = QComboBox()
        self.level.addItems(LEVELS)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.level.currentIndexChanged.connect(self.reload)
        self.search.textChanged.connect(self.reload)
        self.body.addLayout(hbox(self.level, self.search))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "Level", "Category", "Message"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMinimumHeight(560)
        self.table.setWordWrap(True)
        self.body.addWidget(Card(None).add(self.table))
        self._t = 0.0
        self._dirty = True

    def on_event(self, kind: str, data: dict) -> None:
        if kind == "log":
            self._dirty = True

    def activated(self) -> None:
        self.reload()

    def refresh(self, snap: Snapshot) -> None:
        if self._dirty and self.isVisible() and time.monotonic() - self._t > 1.5:
            self.reload()

    def reload(self) -> None:
        self._dirty = False
        self._t = time.monotonic()
        lvl = self.level.currentText()
        rows = self.svc.db.recent_events(400, None if lvl == "All" else lvl, self.search.text() or None)
        pal = palette()
        tones = {"CRITICAL": pal.loss, "ERROR": pal.loss, "WARNING": pal.warn, "INFO": pal.muted}
        self.table.setRowCount(len(rows))
        for r, e in enumerate(rows):
            ts = e.ts.astimezone().strftime("%d %b %H:%M:%S") if e.ts.tzinfo else e.ts.strftime("%d %b %H:%M:%S")
            items = [QTableWidgetItem(ts), QTableWidgetItem(e.level), QTableWidgetItem(e.category),
                     QTableWidgetItem(e.message)]
            items[1].setForeground(QColor(tones.get(e.level, pal.text)))
            for c, it in enumerate(items):
                it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(r, c, it)
