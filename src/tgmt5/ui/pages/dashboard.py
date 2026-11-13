from __future__ import annotations

import time

from tgmt5.analytics.stats import compute
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import (
    BarChart, Card, Donut, Dot, Kpi, LineChart, Meter, Pill, ResponsiveGrid, ResponsiveRow, hbox, label,
    money, vbox,
)

MODE_TEXT = {"MONITOR": "Monitor only", "PAPER": "Paper trading", "DEMO": "DEMO auto-execution",
             "LIVE": "REAL ACCOUNT auto-execution"}
MODE_TONE = {"MONITOR": "info", "PAPER": "paper", "DEMO": "profit", "LIVE": "loss"}


class DashboardPage(Page):
    title = "Dashboard"

    def build(self) -> None:
        # ---- status row
        self.acct_card = Card("MT5 account")
        self.acct_dot = Dot("muted")
        self.acct_title = label("Not connected", "h2")
        self.acct_badge = Pill("—", "muted")
        self.acct_sub = label("Connect an IC Markets SC DEMO account in Connection", "muted", wrap=True)
        self.acct_card.add(layout=hbox(self.acct_dot, self.acct_title, "stretch", self.acct_badge))
        self.acct_card.add(self.acct_sub)

        self.tg_card = Card("Telegram")
        self.tg_dot = Dot("muted")
        self.tg_title = label("Not connected", "h2")
        self.tg_sub = label("", "muted", wrap=True)
        self.tg_card.add(layout=hbox(self.tg_dot, self.tg_title, "stretch"))
        self.tg_card.add(self.tg_sub)

        self.auto_card = Card("Automation")
        self.auto_pill = Pill("MONITOR", "info")
        self.auto_title = label("Monitor only", "h2")
        self.auto_sub = label("", "muted", wrap=True)
        self.auto_card.add(layout=hbox(self.auto_title, "stretch", self.auto_pill))
        self.auto_card.add(self.auto_sub)
        self.body.addWidget(ResponsiveRow(self.acct_card, self.tg_card, self.auto_card, breakpoint=900))

        # ---- KPI grid (6, 4, 3 or 2 columns depending on the width, so values never get cut)
        names = ["Balance", "Equity", "Floating P&L", "Daily P&L", "Free margin", "Drawdown",
                 "Positions", "Pending orders", "Signals today", "Executed today", "Rejected today",
                 "Review queue"]
        self.k = {n: Kpi(n) for n in names}
        self.body.addWidget(ResponsiveGrid([self.k[n] for n in names], min_width=195))

        risk = Card("Risk utilisation")
        self.risk_meter = Meter()
        self.risk_text = label("—", "muted")
        risk.add(layout=hbox(self.risk_text, "stretch"))
        risk.add(self.risk_meter)
        self.body.addWidget(risk)

        # ---- charts
        self.eq = LineChart("accent", empty_text="Equity history appears after the first minute "
                                                 "of a connected session")
        self.dd = LineChart("loss", fmt=lambda v: f"{v:.1f}%")
        self.pnl = BarChart()
        self.donut = Donut()
        self.dist = BarChart(signed=False, tone="info")
        c1 = Card("Equity curve").add(self.eq)
        c2 = Card("Drawdown").add(self.dd)
        c3 = Card("Daily P&L (closed signals)").add(self.pnl)
        c4 = Card("Win rate").add(self.donut)
        c5 = Card("Trades by symbol").add(self.dist)
        self.body.addWidget(ResponsiveRow(c1, c2, breakpoint=760))
        self.body.addWidget(ResponsiveRow(c3, c4, c5, breakpoint=900))
        self._last_stats = 0.0

    def refresh(self, s: Snapshot) -> None:
        a = s.account
        sim = s.mode == "PAPER"
        if a and s.mt5_connected:
            self.acct_dot.set_tone("profit")
            self.acct_title.setText(f"{a.company or 'Broker'}")
            demo = a.is_demo
            real_ok = self.svc.config.mt5.allow_real_account
            self.acct_badge.set("SANDBOX" if s.sandbox else ("PAPER" if sim else
                                ("DEMO" if demo else "REAL ACCOUNT" if real_ok else "LIVE - BLOCKED")),
                                "warn" if s.sandbox else "paper" if sim else ("profit" if demo else "loss"))
            self.acct_sub.setText(f"Account {a.login} · {a.server} · {a.currency} · 1:{a.leverage} · "
                                  f"{'hedging' if a.is_hedging else 'netting'}")
            ccy = a.currency
            self.k["Balance"].set(money(a.balance, ccy))
            self.k["Equity"].set(money(a.equity, ccy))
            self.k["Free margin"].set(money(a.margin_free, ccy), f"margin used {money(a.margin)}")
        else:
            self.acct_dot.set_tone("loss" if not s.mt5_connected else "muted")
            self.acct_title.setText("Not connected")
            self.acct_badge.set("OFFLINE", "muted")
            ccy = ""
            for n in ("Balance", "Equity", "Free margin"):
                self.k[n].set("—")
        tg = s.telegram
        self.tg_dot.set_tone("profit" if tg["connected"] and tg["authorized"] else
                             "warn" if tg["connected"] else "loss")
        self.tg_title.setText(("@" + tg["user"]) if tg["user"] else
                              ("Connected" if tg["connected"] else "Not connected"))
        lat = f"{tg['latency_ms']:.0f} ms" if tg["latency_ms"] else "—"
        dl = f"{tg['avg_delivery_ms'] / 1000:.1f}s" if tg["avg_delivery_ms"] else "—"
        self.tg_sub.setText(f"ping {lat} · avg delivery {dl} · {tg['messages']} msgs · "
                            f"{tg['reconnects']} reconnects")
        self.auto_pill.set("REAL" if s.mode == "LIVE" else s.mode, MODE_TONE[s.mode])
        self.auto_title.setText(MODE_TEXT[s.mode])
        self.auto_sub.setText(s.blocking_reason or ("No lockouts · " +
                              ("execution confirmed for this account" if s.confirmed
                               else "first execution not yet confirmed")))
        self.auto_sub.setStyleSheet("color: #F05252;" if s.blocking_reason else "")
        self.k["Floating P&L"].set(money(s.floating_pnl, sign=True),
                                   tone="profit" if s.floating_pnl > 0 else "loss" if s.floating_pnl < 0 else None)
        self.k["Daily P&L"].set(money(s.daily_pnl, sign=True), "since start of day" if not sim else
                                "vs paper starting balance",
                                tone="profit" if s.daily_pnl > 0 else "loss" if s.daily_pnl < 0 else None)
        self.k["Drawdown"].set(f"{s.drawdown_pct:.2f}%", "from equity peak",
                               tone="warn" if s.drawdown_pct > 2 else None)
        self.k["Positions"].set(str(len(s.positions)))
        self.k["Pending orders"].set(str(len(s.orders)))
        self.k["Signals today"].set(str(s.signals_today))
        self.k["Executed today"].set(str(s.executed_today))
        self.k["Rejected today"].set(str(s.rejected_today), tone="loss" if s.rejected_today else None)
        self.k["Review queue"].set(str(s.review_queue), tone="warn" if s.review_queue else None)
        frac = s.open_risk / s.open_risk_cap if s.open_risk_cap else 0
        self.risk_meter.set(frac)
        self.risk_text.setText(f"Open risk {money(s.open_risk)} of {money(s.open_risk_cap)} allowed "
                               f"({frac * 100:.0f}%)")
        if time.monotonic() - self._last_stats > 15:
            self._last_stats = time.monotonic()
            self._refresh_stats(sim)

    def _refresh_stats(self, sim: bool) -> None:
        st = compute(self.svc.db, simulated=sim)
        self.eq.set_data(st.equity_curve)
        if st.equity_curve:
            peak, dd = 0.0, []
            for ts, e in st.equity_curve:
                peak = max(peak, e)
                dd.append((ts, -(peak - e) / peak * 100 if peak else 0))
            self.dd.set_data(dd, baseline=0)
        self.pnl.set_data(st.daily_pnl[-14:])
        self.donut.set(st.wins, st.losses)
        self.dist.set_data(sorted(((k, v["trades"]) for k, v in st.per_symbol.items()),
                                  key=lambda x: -x[1])[:8])
