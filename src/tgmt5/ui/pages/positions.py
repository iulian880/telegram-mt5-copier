from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHeaderView,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
)

from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.theme import palette
from tgmt5.ui.widgets import Card, Toast, hbox, label, run_async

COLS = ["Ticket", "Symbol", "Side", "Lots", "Entry", "Price", "SL", "TP", "P&L", "Risk", "Duration",
        "Signal"]


def _item(text: str, color: str | None = None, align=Qt.AlignRight | Qt.AlignVCenter) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setTextAlignment(align)
    if color:
        it.setForeground(QColor(color))
    return it


def _dur(sec: float) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, _ = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


class PositionsPage(Page):
    title = "Positions"
    subtitle = "Positions and pending orders opened by this application (magic number filtered)."

    def build(self) -> None:
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(320)
        self.modify_btn = QPushButton("Modify SL/TP…")
        self.close_btn = QPushButton("Close position…")
        self.close_btn.setProperty("kind", "danger")
        self.modify_btn.clicked.connect(self._modify)
        self.close_btn.clicked.connect(self._close)
        c = Card("Open positions")
        c.header.addWidget(self.modify_btn)
        c.header.addWidget(self.close_btn)
        c.add(self.table)
        self.body.addWidget(c)
        self.orders = QTableWidget(0, 7)
        self.orders.setHorizontalHeaderLabels(["Ticket", "Symbol", "Type", "Lots", "Price", "SL / TP",
                                               "Signal"])
        self.orders.verticalHeader().setVisible(False)
        self.orders.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.orders.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.orders.setMinimumHeight(160)
        self.body.addWidget(Card("Pending orders").add(self.orders))
        self.body.addStretch()
        self._rows: list[dict] = []

    def refresh(self, s: Snapshot) -> None:
        pal = palette()
        sel = self._selected()
        self._rows = s.positions
        self.table.setRowCount(len(s.positions))
        now = time.time()
        for r, p in enumerate(s.positions):
            side_c = pal.profit if p["type"] == "BUY" else pal.loss
            pnl_c = pal.profit if p["profit"] > 0 else pal.loss if p["profit"] < 0 else None
            vals = [
                _item(str(p["ticket"]), align=Qt.AlignLeft | Qt.AlignVCenter),
                _item(p["symbol"], align=Qt.AlignLeft | Qt.AlignVCenter), _item(p["type"], side_c),
                _item(f"{p['volume']:g}"), _item(f"{p['price_open']:g}"), _item(f"{p['price_current']:g}"),
                _item(f"{p['sl']:g}" if p["sl"] else "—"), _item(f"{p['tp']:g}" if p["tp"] else "—"),
                _item(f"{p['profit']:+,.2f}", pnl_c), _item(f"{p['risk']:,.2f}"),
                _item(_dur(now - p["opened"]) if p["opened"] else "—"),
                _item(f"#{p['signal_id']}" if p["signal_id"] else "unlinked"),
            ]
            for c, it in enumerate(vals):
                self.table.setItem(r, c, it)
        if sel is not None and sel < len(s.positions):
            self.table.selectRow(sel)
        self.orders.setRowCount(len(s.orders))
        names = {2: "BUY LIMIT", 3: "SELL LIMIT", 4: "BUY STOP", 5: "SELL STOP"}
        for r, o in enumerate(s.orders):
            for c, v in enumerate([o["ticket"], o["symbol"], names.get(o["type"], o["type"]), o["volume"],
                                   o["price"], f"{o['sl']:g} / {o['tp']:g}",
                                   f"#{o['signal_id']}" if o["signal_id"] else "unlinked"]):
                self.orders.setItem(r, c, _item(str(v), align=Qt.AlignLeft | Qt.AlignVCenter))

    def _selected(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _current(self) -> dict | None:
        i = self._selected()
        if i is None or i >= len(self._rows):
            Toast.show_message("Select a position first", "warn")
            return None
        p = self._rows[i]
        if not p.get("leg_id"):
            Toast.show_message("This position is not linked to a tracked signal leg", "warn")
            return None
        return p

    def _close(self) -> None:
        p = self._current()
        if p is None:
            return
        if QMessageBox.question(self, "Close position",
                                f"Close {p['volume']} lots {p['type']} {p['symbol']} (ticket {p['ticket']})"
                                f" at market?\nFloating P&L: {p['profit']:+.2f}") != QMessageBox.Yes:
            return
        run_async(self.svc.close_position(p["leg_id"]),
                  lambda r: Toast.show_message(r[1], "profit" if r[0] else "loss"))

    def _modify(self) -> None:
        p = self._current()
        if p is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Modify {p['symbol']} #{p['ticket']}")
        form = QFormLayout(dlg)
        sl, tp = QDoubleSpinBox(), QDoubleSpinBox()
        for w, v in ((sl, p["sl"]), (tp, p["tp"])):
            w.setDecimals(5)
            w.setRange(0, 1e7)
            w.setValue(v or 0)
        form.addRow("Stop loss", sl)
        form.addRow("Take profit (0 = none)", tp)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        if sl.value() <= 0:
            Toast.show_error("A stop loss is mandatory")
            return
        run_async(self.svc.modify_position(p["leg_id"], sl.value(), tp.value() or None),
                  lambda r: Toast.show_message(r[1], "profit" if r[0] else "loss"))
