from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHeaderView, QPushButton, QTableWidget, QTableWidgetItem,
)

from tgmt5.analytics.stats import compute, export_csv, outcomes
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import (
    BarChart, Card, Kpi, LineChart, ResponsiveGrid, ResponsiveRow, Toast, hbox, label, money, vbox,
)


def _table(cols: list[str], h: int = 180) -> QTableWidget:
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    t.setMinimumHeight(h)
    t.setAlternatingRowColors(True)
    return t


def _fill(t: QTableWidget, rows: list[list]) -> None:
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(str(v)))


class HistoryPage(Page):
    title = "Trade History & Analytics"

    def build(self) -> None:
        self.which = QComboBox()
        self.which.addItems(["DEMO account results", "Paper simulation results"])
        self.which.currentIndexChanged.connect(self.reload)
        exp = QPushButton("Export CSV…")
        exp.clicked.connect(self._export)
        rel = QPushButton("Refresh")
        rel.clicked.connect(self.reload)
        self.header.addWidget(self.which)
        self.header.addWidget(rel)
        self.header.addWidget(exp)
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        self.banner_text = label("", wrap=True)
        self.banner.setLayout(vbox(self.banner_text, margins=(14, 10, 14, 10)))
        self.body.addWidget(self.banner)

        names = ["Total trades", "Win rate", "Profit factor", "Expectancy", "Average win", "Average loss",
                 "Net P&L", "Max drawdown", "Average R", "Exec latency", "Avg slippage", "Avg spread"]
        self.k = {n: Kpi(n) for n in names}
        self.body.addWidget(ResponsiveGrid([self.k[n] for n in names], min_width=195))

        self.cum = LineChart("profit")
        self.rej = BarChart(signed=False, tone="warn", horizontal=True)
        self.body.addWidget(ResponsiveRow(Card("Cumulative P&L").add(self.cum),
                                          Card("Rejected / expired signals by reason").add(self.rej),
                                          breakpoint=820))
        self.sym = _table(["Symbol", "Trades", "Win rate", "Net"])
        self.src = _table(["Provider", "Trades", "Win rate", "Net"])
        self.body.addWidget(ResponsiveRow(Card("Performance per symbol").add(self.sym),
                                          Card("Performance per signal provider").add(self.src),
                                          breakpoint=820))
        self.lat = label("", "muted", wrap=True)
        self.body.addWidget(Card("Execution quality").add(self.lat))
        self.trades = _table(["Signal", "Symbol", "Side", "Opened", "Closed", "Net", "R"], 260)
        self.body.addWidget(Card("Closed trades").add(self.trades))
        self.body.addStretch()

    @property
    def simulated(self) -> bool:
        return self.which.currentIndex() == 1

    def activated(self) -> None:
        self.reload()

    def reload(self) -> None:
        sim = self.simulated
        if sim:
            self.banner.setStyleSheet("QFrame#Banner { background: rgba(167,139,250,0.12);"
                                      " border: 1px solid rgba(167,139,250,0.5); }")
            self.banner_text.setText("SIMULATED RESULTS - fills are modelled with simplified spread, "
                                     "slippage and commission. They are not evidence of realistic or "
                                     "profitable execution.")
        else:
            self.banner.setStyleSheet("QFrame#Banner { background: rgba(34,197,94,0.10);"
                                      " border: 1px solid rgba(34,197,94,0.45); }")
            self.banner_text.setText("DEMO ACCOUNT RESULTS - executed by the broker on a demo account. "
                                     "Demo fills can differ from live market conditions.")
        st = compute(self.svc.db, simulated=sim)
        k = self.k
        k["Total trades"].set(str(st.trades), f"{st.wins} wins / {st.losses} losses")
        k["Win rate"].set(f"{st.win_rate:.1f}%")
        k["Profit factor"].set(f"{st.profit_factor:.2f}" if st.profit_factor else "—")
        k["Expectancy"].set(money(st.expectancy, sign=True), "per signal")
        k["Average win"].set(money(st.avg_win), tone="profit" if st.avg_win else None)
        k["Average loss"].set(money(st.avg_loss), tone="loss" if st.avg_loss else None)
        k["Net P&L"].set(money(st.net, sign=True), tone="profit" if st.net > 0 else "loss" if st.net < 0 else None)
        k["Max drawdown"].set(money(st.max_drawdown), "closed-trade basis")
        k["Average R"].set(f"{st.avg_r:+.2f}R" if st.avg_r is not None else "—")
        k["Exec latency"].set(f"{st.exec_latency_ms.get('avg', 0):.0f} ms" if st.exec_latency_ms else "—",
                              f"p95 {st.exec_latency_ms.get('p95', 0):.0f} ms" if st.exec_latency_ms else "")
        k["Avg slippage"].set(f"{st.slippage_points.get('avg', 0):.1f} pts" if st.slippage_points else "—")
        k["Avg spread"].set(f"{st.spread_points.get('avg', 0):.0f} pts" if st.spread_points else "—")
        self.cum.set_data(st.cumulative, baseline=0)
        self.rej.set_data(sorted(st.rejections.items(), key=lambda x: -x[1])[:8])
        _fill(self.sym, [[s, v["trades"], f"{v['win_rate']:.0f}%", f"{v['net']:+,.2f}"]
                         for s, v in sorted(st.per_symbol.items(), key=lambda x: -x[1]["net"])])
        names = {c.chat_id: c.title or str(c.chat_id) for c in self.svc.config.sources}
        _fill(self.src, [[names.get(s, s), v["trades"], f"{v['win_rate']:.0f}%", f"{v['net']:+,.2f}"]
                         for s, v in st.per_source.items()])
        sl = st.signal_latency_ms
        self.lat.setText(
            f"Telegram delivery latency: avg {sl.get('avg', 0) / 1000:.2f}s, p95 {sl.get('p95', 0) / 1000:.2f}s "
            f"({int(sl.get('n', 0))} signals)\n"
            f"Order round-trip: avg {st.exec_latency_ms.get('avg', 0):.0f} ms · "
            f"Slippage: avg {st.slippage_points.get('avg', 0):.1f}, max {st.slippage_points.get('max', 0):.1f} pts · "
            f"Spread at entry: avg {st.spread_points.get('avg', 0):.0f} pts\n"
            f"Signal outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(st.status_counts.items())))
        outs = list(reversed(outcomes(self.svc.db, sim)))[:200]
        _fill(self.trades, [[f"#{o.signal_id}", o.symbol, o.direction,
                             o.opened.strftime("%d %b %H:%M") if o.opened else "—",
                             o.closed.strftime("%d %b %H:%M") if o.closed else "—", f"{o.net:+,.2f}",
                             f"{o.r_multiple:+.2f}" if o.r_multiple is not None else "—"] for o in outs])

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export trades", str(Path.home() / "trades.csv"),
                                              "CSV (*.csv)")
        if path:
            Path(path).write_text(export_csv(self.svc.db, self.simulated), encoding="utf-8")
            Toast.show_message(f"Exported to {path}", "profit")

    def refresh(self, snap: Snapshot) -> None:
        pass
