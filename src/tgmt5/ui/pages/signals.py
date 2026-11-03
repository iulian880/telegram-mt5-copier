from __future__ import annotations

import time
from datetime import timezone

from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QLineEdit, QPushButton, QWidget
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tgmt5.persistence.models import SignalRow
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import Card, Pill, Timeline, Toast, hbox, label, run_async, vbox

FILTERS = ["All", "Manual review", "Active", "Executed", "Rejected / failed", "Monitored", "Simulated"]
FILTER_STATUSES = {
    "Manual review": ["MANUAL_REVIEW"],
    "Active": ["WAITING", "QUEUED", "SUBMITTING", "PENDING_PLACED", "EXECUTED", "PARTIAL", "UNCERTAIN"],
    "Executed": ["EXECUTED", "PARTIAL", "CLOSED", "PENDING_PLACED"],
    "Rejected / failed": ["REJECTED", "FAILED", "EXPIRED", "CANCELLED"],
    "Monitored": ["MONITORED"],
    "Simulated": ["SIMULATED"],
}


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:g}"


class SignalCard(Card):
    def __init__(self, sig: SignalRow, page: "SignalsPage") -> None:
        super().__init__(None, padding=16, shadow=False)
        m = sig.message
        src = m.source_title or str(sig.source_id)
        ts = m.date.replace(tzinfo=timezone.utc) if m.date.tzinfo is None else m.date
        head = hbox(label(f"#{sig.id}", "h2"), label(src, "muted"),
                    label(ts.astimezone().strftime("%d %b %H:%M:%S"), "muted"), "stretch")
        if sig.is_simulated:
            head.addWidget(Pill("PAPER", "paper"))
        head.addWidget(Pill(sig.status))
        self.add(layout=head)

        msg = QFrame()
        msg.setObjectName("Message")
        txt = label(m.text[:600] + ("…" if len(m.text) > 600 else ""), "mono", wrap=True)
        txt.setTextInteractionFlags(txt.textInteractionFlags())
        msg.setLayout(vbox(txt, margins=(12, 10, 12, 10)))

        info = QGridLayout()
        info.setHorizontalSpacing(18)
        info.setVerticalSpacing(4)
        if sig.kind == "NEW_SIGNAL" and sig.direction:
            entry = (f"{_fmt(sig.entry_low)} – {_fmt(sig.entry_high)}" if sig.entry_low is not None
                     else _fmt(sig.entry_price) if sig.entry_price is not None else "market")
            tone = "profit" if sig.direction == "BUY" else "loss"
            fields = [
                ("SYMBOL", sig.broker_symbol or sig.symbol or "—"), ("ENTRY", f"{entry} ({sig.entry_type})"),
                ("STOP LOSS", _fmt(sig.stop_loss)),
                ("TAKE PROFITS", ", ".join(_fmt(t) for t in sig.take_profits) or "—"),
                ("LOTS", _fmt(sig.lot_total)), ("RISK", f"{sig.risk_money:,.2f}" if sig.risk_money else "—"),
            ]
            info.addWidget(label("DIRECTION", "caption"), 0, 0)
            d = label(sig.direction, "h2")
            d.setStyleSheet(f"color: {'#22C55E' if tone == 'profit' else '#F05252'};")
            info.addWidget(d, 1, 0)
            for i, (k, v) in enumerate(fields, start=1):
                info.addWidget(label(k, "caption"), 0, i)
                info.addWidget(label(v), 1, i)
        elif sig.management:
            mg = sig.management
            desc = mg.get("type", "?")
            if mg.get("fraction"):
                desc += f" {mg['fraction']:.0%}"
            if mg.get("price"):
                desc += f" @ {mg['price']}"
            info.addWidget(label("MANAGEMENT", "caption"), 0, 0)
            info.addWidget(label(desc + (f" → signal #{sig.parent_signal_id}" if sig.parent_signal_id
                                         else ""), "h2"), 1, 0)
        left = vbox(msg, spacing=8)
        right = vbox(info, "stretch", spacing=8)
        self.add(layout=hbox(left, right, spacing=18))

        tl = Timeline()
        tl.set([e.status_to for e in sig.events], sig.status)
        self.add(tl)
        if sig.reject_reason:
            r = label(f"Reason: {sig.reject_reason}", wrap=True)
            r.setStyleSheet("color: #F5A524;" if sig.status == "MANUAL_REVIEW" else "color: #F05252;")
            self.add(r)
        tickets = [str(e.position_ticket or e.order_ticket) for e in sig.executions
                   if e.position_ticket or e.order_ticket]
        if tickets:
            self.add(label("MT5 tickets: " + ", ".join(tickets) +
                           "  ·  legs: " + ", ".join(f"{e.volume}@TP{e.tp_index or '-'} {e.state}"
                                                      for e in sig.executions), "muted", wrap=True))
        if sig.status == "MANUAL_REVIEW" and sig.kind != "NEW_SIGNAL":
            no = QPushButton("Dismiss")
            no.clicked.connect(lambda: page.review(sig.id, False))
            self.add(layout=hbox(label("Ambiguous follow-up: apply it yourself in MT5 or reply-quote the "
                                       "original signal in Telegram.", "muted"), "stretch", no))
        elif sig.status == "MANUAL_REVIEW":
            ok = QPushButton("Re-validate && execute")
            ok.setProperty("kind", "primary")
            ok.setToolTip("Runs the complete validation and risk checks again. Only executes if the "
                          "signal is now complete and every check passes in the current mode.")
            no = QPushButton("Dismiss")
            ok.clicked.connect(lambda: page.review(sig.id, True))
            no.clicked.connect(lambda: page.review(sig.id, False))
            self.add(layout=hbox("stretch", no, ok))


class SignalsPage(Page):
    title = "Live Signal Monitor"
    subtitle = ("Every Telegram message from your selected sources, how it was interpreted, and what "
                "the risk engine decided.")

    def build(self) -> None:
        self.filter = QComboBox()
        self.filter.addItems(FILTERS)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search symbol or text…")
        self.filter.currentIndexChanged.connect(self.reload)
        self.search.returnPressed.connect(self.reload)
        self.count = label("", "muted")
        self.body.addLayout(hbox(self.filter, self.search, "stretch", self.count))
        self.list_host = QWidget()
        self.list = vbox(spacing=12)
        self.list_host.setLayout(self.list)
        self.body.addWidget(self.list_host)
        self.body.addStretch()
        self._dirty = True
        self._last = 0.0

    def on_event(self, kind: str, data: dict) -> None:
        if kind == "signal":
            self._dirty = True

    def activated(self) -> None:
        self._dirty = True
        self.reload()

    def refresh(self, snap: Snapshot) -> None:
        if self._dirty and self.isVisible() and time.monotonic() - self._last > 1.0:
            self.reload()

    def reload(self) -> None:
        self._dirty = False
        self._last = time.monotonic()
        f = self.filter.currentText()
        q = (select(SignalRow).options(selectinload(SignalRow.message), selectinload(SignalRow.events),
                                       selectinload(SignalRow.executions))
             .order_by(SignalRow.id.desc()).limit(60))
        if f in FILTER_STATUSES:
            q = q.where(SignalRow.status.in_(FILTER_STATUSES[f]))
        term = self.search.text().strip().upper()
        with self.svc.db.session() as s:
            rows = list(s.scalars(q))
            if term:
                rows = [r for r in rows if term in (r.symbol or "") or term in r.message.text.upper()]
            while self.list.count():
                it = self.list.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            for r in rows:
                self.list.addWidget(SignalCard(r, self))
            if not rows:
                self.list.addWidget(label("No signals yet. Messages from enabled Telegram sources "
                                          "appear here in real time.", "muted"))
        self.count.setText(f"{len(rows)} shown")

    def review(self, sid: int, approve: bool) -> None:
        def ok(st):
            Toast.show_message(f"Signal #{sid}: {st.value}", "info")
            self._dirty = True
        run_async(self.svc.resolve_review(sid, approve), ok)
