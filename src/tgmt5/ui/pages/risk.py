"""Risk page.

1. Position sizing: one selector for every sizing method (simple / advanced), with the
   explanation of the chosen method and only the input it needs.
2. Advanced sizing rules: TP handling, TP volume allocation, small-volume policy, lot limits.
3. "What will be opened": the engine's own sizing run against the DETECTED account
   (or an example balance offline), per symbol: lot on every TP, money at risk, margin, verdict.
4. Protections with on/off switches (percentage caps, drawdown protection) and exposure limits.
Critical fields are red with a ✓ button until the user ticks them (pre-trade review).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QSlider, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QWidget,
)
from pydantic import ValidationError

from tgmt5.config import (
    PER_TP_MODES, AppConfig, RiskSettings, SizingMode, SmallVolumePolicy, TpHitAction, TpStrategy,
    ZonePolicy,
)
from tgmt5.review import pending_reviews, required_keys, review_value
from tgmt5.risk.preview import NOT_AT_BROKER
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import (
    Card, Pill, ResponsiveRow, ReviewTick, Toast, color, hbox, label, money, run_async, vbox,
)

METHODS: dict[SizingMode, tuple[str, str, str]] = {   # title, size-row label, explanation
    SizingMode.LOT_PER_TP: (
        "Fixed lot on every TP", "Lot on every TP",
        "Every take profit gets its own position with this lot. A signal with 3 TPs opens "
        "3 × 0.01 = 0.03 lots. The money at risk depends on how far each signal's stop loss is."),
    SizingMode.FIXED_LOT: (
        "Fixed total lot per signal", "Total lot per signal",
        "The whole signal opens this many lots, split across its TPs with the TP volume allocation "
        "(for example 0.06 with 3 TPs → 0.02 + 0.02 + 0.02)."),
    SizingMode.FIXED_MONEY: (
        "Fixed money at risk per signal", "Money at risk per signal",
        "The lot is calculated so that, if the stop loss is hit, the whole signal loses at most this "
        "amount (account currency). Wider stop loss → smaller lot."),
    SizingMode.PCT_EQUITY: (
        "% of equity at risk per signal", "Risk per signal",
        "Like fixed money, but the amount is a % of your current equity, so trades grow and shrink "
        "with the account."),
    SizingMode.PCT_BALANCE: (
        "% of balance at risk per signal", "Risk per signal",
        "Same as % of equity, but based on the balance (ignores the floating profit/loss of open trades)."),
    SizingMode.LOT_PER_BALANCE: (
        "Lot per TP that grows with the balance", "Lot per TP / balance step",
        "Every TP gets the lot below for each full step of balance. Example 0.01 per 300: balance "
        "300–599 → 0.01 per TP, 600–899 → 0.02 per TP. Below one step, one step's lot is used."),
}
SIMPLE = (SizingMode.LOT_PER_TP, SizingMode.FIXED_LOT, SizingMode.FIXED_MONEY)
ADVANCED = (SizingMode.PCT_EQUITY, SizingMode.PCT_BALANCE, SizingMode.LOT_PER_BALANCE)

TP_STRATEGIES = {
    TpStrategy.SPLIT: "One position per TP (recommended; needs a hedging account)",
    TpStrategy.SINGLE_MANAGED: "One position, close part of it at each TP",
    TpStrategy.FIRST_TP_ONLY: "One position, TP1 only",
}
SMALL_POLICIES = {
    SmallVolumePolicy.REDUCE: "Use fewer TPs (TP1, TP2…), never risk more than planned",
    SmallVolumePolicy.MIN_PER_TP: "Open the minimum lot on every TP (may risk more than planned)",
    SmallVolumePolicy.REJECT: "Skip the signal",
}
ALLOC_PRESETS: list[tuple[str, list[float] | None]] = [
    ("Equal 1 : 1 : 1 (recommended for 3-TP signals)", [1.0, 1.0, 1.0]),
    ("Front-loaded 50 / 30 / 20", [0.5, 0.3, 0.2]),
    ("Balanced 40 / 30 / 30", [0.4, 0.3, 0.3]),
    ("Back-loaded 20 / 30 / 50", [0.2, 0.3, 0.5]),
    ("Custom…", None),
]
PRESETS = [
    "Apply a preset…",
    "Small account (200–500): 0.01 on every TP, no % caps, no drawdown lockouts",
    "Growing account: 0.01 per TP for every 300 of balance",
    "Conservative: 0.5% of equity per signal, all protections on",
]
PREVIEW_COLUMNS = ["Symbol", "SL", "Lots that open", "Loss if SL hit", "% equity", "Margin", "Opens?"]


def dspin(lo, hi, step, dec=2, suffix=""):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setSingleStep(step)
    w.setDecimals(dec)
    if suffix:
        w.setSuffix(suffix)
    return w


def ispin(lo, hi, suffix=""):
    w = QSpinBox()
    w.setRange(lo, hi)
    if suffix:
        w.setSuffix(suffix)
    return w


def wrap(*widgets) -> QWidget:
    w = QWidget()
    w.setLayout(hbox(*widgets, spacing=8))
    return w


def _norm(v: list[float]) -> tuple[float, ...]:
    s = sum(v) or 1.0
    return tuple(round(x / s, 4) for x in v)


class RiskPage(Page):
    title = "Risk Management"
    subtitle = ("Choose how big each trade is, see exactly what that opens on your account, then tick every "
                "red setting. All limits are enforced by the backend on every order. Changes apply after Save.")

    def build(self) -> None:
        self._stamps: dict[str, str] = {}
        self._ticks: dict[str, ReviewTick] = {}
        self._loading = False
        self._equity: float | None = None
        self._token = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._changed_now)

        self.revert_btn = QPushButton("Revert")
        self.revert_btn.clicked.connect(self.load)
        self.save_btn = QPushButton("Save risk settings")
        self.save_btn.setProperty("kind", "primary")
        self.save_btn.clicked.connect(self.save)
        self.header.addWidget(self.revert_btn)
        self.header.addWidget(self.save_btn)

        # ---------------------------------------------------------- review banner
        banner = Card("Pre-trade review")
        self.rv_pill = Pill("", "loss")
        self.rv_text = label("", "muted", wrap=True)
        self.preset = QComboBox()
        self.preset.addItems(PRESETS)
        self.preset.activated.connect(self._apply_preset)
        banner.add(layout=hbox(self.rv_pill, self.rv_text, self.preset))
        self.body.addWidget(banner)

        # ---------------------------------------------------------- 1. sizing
        sizing = Card("1 · Position sizing")
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        self.method = QComboBox()
        self.method.addItem("SIMPLE")
        for m in SIMPLE:
            self.method.addItem(f"   {METHODS[m][0]}", m.value)
        self.method.addItem("ADVANCED")
        for m in ADVANCED:
            self.method.addItem(f"   {METHODS[m][0]}", m.value)
        for i in (0, len(SIMPLE) + 1):
            self.method.model().item(i).setEnabled(False)
        self._row(g, 0, "sizing_mode", "Sizing method", self.method)
        self.method_desc = label("", "muted", wrap=True)
        g.addWidget(self.method_desc, 1, 1)
        self.lot_tp = dspin(0.01, 100, 0.01)
        self.fixed_lot = dspin(0.01, 100, 0.01)
        self.fixed_money = dspin(1, 1e7, 5)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, 200)                       # 0.05% .. 10% in 0.05 steps
        self.pct = dspin(0.05, 10, 0.05, suffix=" %")
        self.slider.valueChanged.connect(lambda v: self.pct.setValue(v * 0.05))
        self.pct.valueChanged.connect(lambda v: self.slider.setValue(round(v / 0.05)))
        self.bal_lot = dspin(0.01, 100, 0.01)
        self.bal_step = dspin(1, 1e7, 50)
        self.size_stack = QStackedWidget()
        self._size_page = {
            SizingMode.LOT_PER_TP: wrap(self.lot_tp, label("lots on every TP", "muted"), "stretch"),
            SizingMode.FIXED_LOT: wrap(self.fixed_lot, label("lots in total, split across the TPs", "muted"),
                                       "stretch"),
            SizingMode.FIXED_MONEY: wrap(self.fixed_money, label("account currency, lost if the SL is hit",
                                                                 "muted"), "stretch"),
            SizingMode.PCT_EQUITY: wrap(self.slider, self.pct),
            SizingMode.LOT_PER_BALANCE: wrap(self.bal_lot, label("lots per TP for every", "muted"),
                                             self.bal_step, label("of balance", "muted"), "stretch"),
        }
        self._size_page[SizingMode.PCT_BALANCE] = self._size_page[SizingMode.PCT_EQUITY]
        for w in dict.fromkeys(self._size_page.values()):
            self.size_stack.addWidget(w)
        self.size_label = self._row(g, 2, "size", "Size", self.size_stack,
                                    fields=[self.lot_tp, self.fixed_lot, self.fixed_money, self.pct,
                                            self.bal_lot, self.bal_step])
        g.setColumnStretch(1, 1)
        sizing.add(layout=g)

        # ---------------------------------------------------------- 2. advanced rules
        adv = Card("2 · Advanced sizing rules")
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        self.tp_strategy = QComboBox()
        for k, v in TP_STRATEGIES.items():
            self.tp_strategy.addItem(v, k.value)
        self._row(g, 0, "tp_strategy", "Multiple TPs", self.tp_strategy,
                  tip="How a signal with several take profits is opened.")
        self.alloc_preset = QComboBox()
        for text, _ in ALLOC_PRESETS:
            self.alloc_preset.addItem(text)
        self.alloc = QLineEdit()
        self.alloc.setPlaceholderText("e.g. 1, 1, 1")
        self._row(g, 1, "tp_allocation", "TP volume allocation", wrap(self.alloc_preset, self.alloc),
                  fields=[self.alloc_preset, self.alloc],
                  tip="How the total volume is shared between TP1, TP2, TP3… when it is split.")
        self.alloc_note = label("", "muted", wrap=True)
        g.addWidget(self.alloc_note, 2, 1)
        self.small = QComboBox()
        for k, v in SMALL_POLICIES.items():
            self.small.addItem(v, k.value)
        self._row(g, 3, "small_volume_policy", "If the volume is too small", self.small,
                  tip="When the calculated volume is below the broker minimum or cannot cover every TP.")
        self.max_lot = dspin(0.01, 100, 0.01)
        self._row(g, 4, "max_lot_per_order", "Max lot per position", self.max_lot)
        self.max_sym = dspin(0.01, 500, 0.1)
        self._row(g, 5, None, "Max lots per symbol", self.max_sym)
        g.setColumnStretch(1, 1)
        adv.add(layout=g)

        # ---------------------------------------------------------- 3. preview
        pv = Card("What will be opened: with your account")
        self.pv_account = label("", wrap=True)
        self.pv_account.setTextFormat(Qt.RichText)
        self.ex_bal = dspin(10, 1e7, 50)
        self.ex_bal.setValue(300)
        self.rb_typ = QRadioButton("Typical signal:")
        self.rb_recent = QRadioButton("Your recent signals")
        self.rb_typ.setChecked(True)
        self.ex_sl = dspin(5, 2000, 5, dec=0, suffix=" pips SL")
        self.ex_sl.setValue(55)
        self.ex_tps = ispin(0, 6, " TPs")
        self.ex_tps.setValue(3)
        pv.add(self.pv_account)
        pv.add(layout=hbox(label("Example balance (when not connected)", "muted"), self.ex_bal, "stretch"))
        pv.add(layout=hbox(self.rb_typ, self.ex_sl, self.ex_tps, self.rb_recent, "stretch"))
        self.pv_table = QTableWidget(0, len(PREVIEW_COLUMNS))
        self.pv_table.setHorizontalHeaderLabels(PREVIEW_COLUMNS)
        self.pv_table.verticalHeader().setVisible(False)
        self.pv_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pv_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.pv_table.setAlternatingRowColors(True)
        hh = self.pv_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(len(PREVIEW_COLUMNS) - 1, QHeaderView.Stretch)
        self.pv_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pv_table.setWordWrap(True)
        self.pv_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        pv.add(self.pv_table)
        self.pv_summary = label("", wrap=True)
        self.pv_summary.setTextFormat(Qt.RichText)
        pv.add(self.pv_summary)

        left = QWidget()
        left.setLayout(vbox(sizing, adv, "stretch", spacing=16))
        self.body.addWidget(ResponsiveRow(left, pv, breakpoint=1180, stretches=[5, 6]))

        # ---------------------------------------------------------- 4. protections
        prot = Card("3 · Protections")
        self.w = {
            "max_risk_per_signal_pct": dspin(0.05, 25, 0.05, suffix=" %"),
            "max_total_open_risk_pct": dspin(0.1, 100, 0.5, suffix=" %"),
            "max_correlated_risk_pct": dspin(0.1, 100, 0.5, suffix=" %"),
            "max_daily_loss_pct": dspin(0.1, 50, 0.5, suffix=" %"),
            "max_drawdown_pct": dspin(0.5, 90, 0.5, suffix=" %"),
            "max_consecutive_losses": ispin(0, 50),
            "max_concurrent_trades": ispin(1, 100),
            "max_trades_per_hour": ispin(1, 100),
            "max_margin_usage_pct": dspin(1, 100, 1, suffix=" %"),
            "min_free_margin": dspin(0, 1e7, 10),
            "max_spread_sl_fraction": dspin(0.01, 1, 0.01),
            "max_entry_deviation_sl_fraction": dspin(0, 2, 0.05),
            "max_slippage_points": ispin(0, 10000, " pts"),
            "min_risk_reward": dspin(0, 10, 0.1),
        }
        self.caps = QCheckBox("Percentage risk caps")
        self.caps.setToolTip("Limit the money at risk as a % of equity. Turn off for small accounts, where "
                             "one minimum lot is already several % of the account.")
        self.ddp = QCheckBox("Drawdown protection")
        self.ddp.setToolTip("Stop trading for the day / until reset after losses. Turn off to keep trading "
                            "every signal whatever the losses.")
        gl, gr = QGridLayout(), QGridLayout()
        for gg in (gl, gr):
            gg.setHorizontalSpacing(12)
            gg.setColumnStretch(1, 1)
        self._row(gl, 0, "percent_caps_enabled", "", self.caps)
        self._row(gl, 1, "max_risk_per_signal_pct", "   Max risk per signal", self.w["max_risk_per_signal_pct"])
        self._row(gl, 2, "max_total_open_risk_pct", "   Max total open risk", self.w["max_total_open_risk_pct"])
        self._row(gl, 3, None, "   Max correlated risk", self.w["max_correlated_risk_pct"])
        self._row(gl, 4, "drawdown_protection", "", self.ddp)
        self._row(gl, 5, "max_daily_loss_pct", "   Daily loss lockout", self.w["max_daily_loss_pct"])
        self._row(gl, 6, "max_drawdown_pct", "   Max drawdown lockout", self.w["max_drawdown_pct"])
        self._row(gl, 7, None, "   Losing-streak breaker (0 = off)", self.w["max_consecutive_losses"])
        gr.addWidget(label("EXPOSURE & MARGIN", "caption"), 0, 0, 1, 3)
        self._row(gr, 1, "max_concurrent_trades", "Max signals open at once", self.w["max_concurrent_trades"])
        self._row(gr, 2, None, "Max trades / hour", self.w["max_trades_per_hour"])
        self._row(gr, 3, None, "Max margin usage", self.w["max_margin_usage_pct"])
        self._row(gr, 4, "min_free_margin", "Min free margin to keep", self.w["min_free_margin"])
        gr.addWidget(label("PRICE FILTERS", "caption"), 5, 0, 1, 3)
        self._row(gr, 6, None, "Max spread (× SL distance)", self.w["max_spread_sl_fraction"])
        self._row(gr, 7, "max_entry_deviation_sl_fraction", "Entry tolerance (× SL distance)",
                  self.w["max_entry_deviation_sl_fraction"],
                  tip="How far the price may be from the signal's entry and still open at market. "
                      "0.30 × a 55-pip SL = about 16 pips.")
        self._row(gr, 8, None, "Max slippage", self.w["max_slippage_points"])
        self._row(gr, 9, None, "Min R:R (0 = off)", self.w["min_risk_reward"])
        gl.setRowStretch(8, 1)
        gr.setRowStretch(10, 1)
        prot.add(ResponsiveRow(gl, gr, breakpoint=880, spacing=28))
        self.whitelist = QLineEdit()
        self.blacklist = QLineEdit()
        self.whitelist.setPlaceholderText("empty = all symbols")
        prot.add(layout=hbox(label("Whitelist", "muted"), self.whitelist,
                             label("Blacklist", "muted"), self.blacklist))
        self.hours = QCheckBox("Restrict trading hours (UTC)")
        self.h_from, self.h_to = ispin(0, 23), ispin(1, 24)
        prot.add(layout=hbox(self.hours, self.h_from, label("to"), self.h_to, "stretch"))
        self.body.addWidget(prot)

        # ---------------------------------------------------------- 5. management + lockouts
        mgmt = Card("4 · Entries & trade management")
        self.zone = QComboBox()
        self.zone.addItems([z.value for z in ZonePolicy])
        self.zone.setToolTip("WAIT: wait for price to reach the zone · REJECT · PENDING: place a limit order "
                             "at the zone edge (only if you choose it)")
        self.be_after = ispin(0, 5)
        self.be_off = ispin(0, 10000, " pts")
        self.trail = QCheckBox("Trailing stop after breakeven")
        self.trail_frac = dspin(0.1, 2, 0.05)
        self.tp_msg = QComboBox()
        self.tp_msg.addItems([t.value for t in TpHitAction])
        self.age = ispin(5, 3600, " s")
        self.wait_exp = ispin(1, 1440, " min")
        self.pend_exp = ispin(1, 10080, " min")
        self.req_tp = QCheckBox("Require a take profit")
        self.edits = QCheckBox("Apply provider edits of SL to open trades")
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setColumnStretch(1, 1)
        self._row(g, 0, None, "Entry zone policy", self.zone)
        self._row(g, 1, None, "Max signal age", self.age)
        self._row(g, 2, None, "Waiting signal expiry", self.wait_exp)
        self._row(g, 3, None, "Pending order expiry", self.pend_exp)
        self._row(g, 4, "move_sl_to_be_after_tp", "Move SL to breakeven after TP # (0 = off)", self.be_after)
        self._row(g, 5, None, "Breakeven offset", self.be_off)
        self._row(g, 6, None, "", wrap(self.trail, self.trail_frac, "stretch"))
        self._row(g, 7, None, "'TP hit' messages", self.tp_msg)
        g.addWidget(self.req_tp, 8, 1)
        g.addWidget(self.edits, 9, 1)
        mgmt.add(layout=g)

        self.lock = Card("Equity protection status")
        self.lock_pill = Pill("OK", "profit")
        self.lock_text = label("", "muted", wrap=True)
        self.reset_dd = QPushButton("Reset drawdown lockout")
        self.reset_br = QPushButton("Reset loss breaker")
        self.clear_em = QPushButton("Clear emergency stop")
        self.reset_dd.clicked.connect(self._reset_dd)
        self.reset_br.clicked.connect(lambda: (self.svc.risk.reset_breaker(),
                                               Toast.show_message("Breaker reset", "info")))
        self.clear_em.clicked.connect(lambda: run_async(self.svc.clear_emergency(),
                                                        lambda _: Toast.show_message("Emergency cleared")))
        self.lock.add(layout=hbox(self.lock_pill, self.lock_text, "stretch"))
        self.lock.add(layout=hbox(self.reset_dd, self.reset_br, self.clear_em, "stretch"))
        self.lock.lay.addStretch(1)
        self.body.addWidget(ResponsiveRow(mgmt, self.lock, breakpoint=980))
        self.body.addStretch()

        # ---------------------------------------------------------- change tracking
        for w in [self.lot_tp, self.fixed_lot, self.fixed_money, self.pct, self.bal_lot, self.bal_step,
                  self.max_lot, self.max_sym, self.be_after, self.be_off, self.trail_frac, self.age,
                  self.wait_exp, self.pend_exp, self.ex_bal, self.ex_sl, self.ex_tps, self.h_from, self.h_to,
                  *self.w.values()]:
            w.valueChanged.connect(self._on_change)
        for w in [self.method, self.tp_strategy, self.alloc_preset, self.small, self.zone, self.tp_msg]:
            w.currentIndexChanged.connect(self._on_change)
        for w in [self.caps, self.ddp, self.trail, self.req_tp, self.edits, self.hours, self.rb_typ,
                  self.rb_recent]:
            w.toggled.connect(self._on_change)
        for w in [self.alloc, self.whitelist, self.blacklist]:
            w.textChanged.connect(self._on_change)
        self.load()

    # ------------------------------------------------------------------ helpers
    def _row(self, grid: QGridLayout, r: int, key: str | None, text: str, field: QWidget,
             fields: list[QWidget] | None = None, tip: str = "") -> QLabel | None:
        lab = label(text, "muted") if text else None
        if lab is not None:
            grid.addWidget(lab, r, 0)
        grid.addWidget(field, r, 1)
        if tip:
            field.setToolTip(tip)
            if lab is not None:
                lab.setToolTip(tip)
        if key:
            marked = ([lab] if lab is not None else []) + (fields if fields is not None else [field])
            t = ReviewTick(key, *marked)
            t.toggled.connect(lambda checked, k=key: self._tick(k, checked))
            self._ticks[key] = t
            grid.addWidget(t, r, 2)
        return lab

    def _mode(self) -> SizingMode:
        return SizingMode(self.method.currentData() or SizingMode.PCT_EQUITY.value)

    def _allocation(self) -> list[float]:
        weights = ALLOC_PRESETS[self.alloc_preset.currentIndex()][1]
        if weights is not None:
            return list(weights)
        return [float(x) for x in self.alloc.text().replace(";", ",").split(",") if x.strip()]

    # ------------------------------------------------------------------ load / collect
    def load(self) -> None:
        self._fill(self.svc.config)
        self._stamps = dict(self.svc.config.reviewed)
        self._changed_now()

    def _fill(self, cfg: AppConfig) -> None:
        r, e = cfg.risk, cfg.execution
        self._loading = True
        try:
            self.method.setCurrentIndex(self.method.findData(r.sizing_mode.value))
            self.lot_tp.setValue(r.lot_per_tp)
            self.bal_lot.setValue(r.lot_per_tp)
            self.bal_step.setValue(r.balance_step)
            self.fixed_lot.setValue(r.fixed_lot)
            self.fixed_money.setValue(r.fixed_money)
            self.pct.setValue(r.risk_percent)
            self.small.setCurrentIndex(self.small.findData(r.small_volume_policy.value))
            self.max_lot.setValue(r.max_lot_per_order)
            self.max_sym.setValue(r.max_lots_per_symbol)
            self.caps.setChecked(r.percent_caps_enabled)
            self.ddp.setChecked(r.drawdown_protection)
            for k, w in self.w.items():
                w.setValue(getattr(r, k))
            self.whitelist.setText(", ".join(r.symbol_whitelist))
            self.blacklist.setText(", ".join(r.symbol_blacklist))
            self.hours.setChecked(r.trading_hours_utc is not None)
            if r.trading_hours_utc:
                self.h_from.setValue(r.trading_hours_utc[0])
                self.h_to.setValue(r.trading_hours_utc[1])
            self.tp_strategy.setCurrentIndex(self.tp_strategy.findData(e.tp_strategy.value))
            idx = next((i for i, (_, wts) in enumerate(ALLOC_PRESETS)
                        if wts is not None and _norm(wts) == _norm(e.tp_allocation)), len(ALLOC_PRESETS) - 1)
            self.alloc_preset.setCurrentIndex(idx)
            self.alloc.setText(", ".join(f"{x:g}" for x in e.tp_allocation))
            self.zone.setCurrentText(e.zone_policy.value)
            self.be_after.setValue(e.move_sl_to_be_after_tp)
            self.be_off.setValue(e.breakeven_offset_points)
            self.trail.setChecked(e.trailing_enabled)
            self.trail_frac.setValue(e.trailing_distance_sl_fraction)
            self.tp_msg.setCurrentText(e.on_tp_hit_message.value)
            self.age.setValue(e.max_signal_age_seconds)
            self.wait_exp.setValue(e.waiting_signal_expiry_minutes)
            self.pend_exp.setValue(e.pending_order_expiry_minutes)
            self.req_tp.setChecked(e.require_take_profit)
            self.edits.setChecked(e.apply_edits_to_open_trades)
        finally:
            self._loading = False

    def _collect(self) -> AppConfig:
        cfg = self.svc.config.model_copy(deep=True)
        mode = self._mode()
        r = cfg.risk.model_dump()
        r.update(sizing_mode=mode.value, risk_percent=self.pct.value(),
                 fixed_lot=self.fixed_lot.value(), fixed_money=self.fixed_money.value(),
                 lot_per_tp=self.bal_lot.value() if mode is SizingMode.LOT_PER_BALANCE else self.lot_tp.value(),
                 balance_step=self.bal_step.value(), small_volume_policy=self.small.currentData(),
                 max_lot_per_order=self.max_lot.value(), max_lots_per_symbol=self.max_sym.value(),
                 percent_caps_enabled=self.caps.isChecked(), drawdown_protection=self.ddp.isChecked(),
                 symbol_whitelist=[s.strip().upper() for s in self.whitelist.text().split(",") if s.strip()],
                 symbol_blacklist=[s.strip().upper() for s in self.blacklist.text().split(",") if s.strip()],
                 trading_hours_utc=(self.h_from.value(), self.h_to.value()) if self.hours.isChecked() else None)
        for k, w in self.w.items():
            r[k] = w.value()
        e = cfg.execution.model_dump()
        e.update(zone_policy=self.zone.currentText(), tp_strategy=self.tp_strategy.currentData(),
                 tp_allocation=self._allocation(),
                 move_sl_to_be_after_tp=self.be_after.value(), breakeven_offset_points=self.be_off.value(),
                 trailing_enabled=self.trail.isChecked(),
                 trailing_distance_sl_fraction=self.trail_frac.value(),
                 on_tp_hit_message=self.tp_msg.currentText(), max_signal_age_seconds=self.age.value(),
                 waiting_signal_expiry_minutes=self.wait_exp.value(),
                 pending_order_expiry_minutes=self.pend_exp.value(),
                 require_take_profit=self.req_tp.isChecked(),
                 apply_edits_to_open_trades=self.edits.isChecked())
        data = cfg.model_dump()
        data["risk"], data["execution"] = r, e
        data["reviewed"] = dict(self._stamps)
        return AppConfig.model_validate(data)

    def _try_collect(self) -> tuple[AppConfig | None, str]:
        try:
            return self._collect(), ""
        except (ValidationError, ValueError) as e:
            lines = [ln for ln in str(e).splitlines() if ln.strip()]
            return None, (lines[-1] if lines else str(e))

    def _dirty(self, cfg: AppConfig) -> bool:
        skip = {"reviewed", "mode"}
        return cfg.model_dump(exclude=skip) != self.svc.config.model_dump(exclude=skip)

    # ------------------------------------------------------------------ reactions
    def _on_change(self, *_a) -> None:
        if not self._loading:
            self._update_method_ui()
            self._timer.start()

    def _changed_now(self) -> None:
        self._update_method_ui()
        self._refresh_reviews()
        self._request_preview()

    def _update_method_ui(self) -> None:
        mode = self._mode()
        _title, size_label, desc = METHODS[mode]
        self.method_desc.setText(desc)
        self.size_stack.setCurrentWidget(self._size_page[mode])
        if self.size_label is not None:
            self.size_label.setText(size_label)
        custom = ALLOC_PRESETS[self.alloc_preset.currentIndex()][1] is None
        self.alloc.setVisible(custom)
        split = self.tp_strategy.currentData() == TpStrategy.SPLIT.value
        if mode in PER_TP_MODES:
            note = "Not used: with this sizing method every TP gets the same lot."
        elif not split:
            note = "Not used: positions are not split per TP with the chosen TP handling."
        else:
            note = ("Equal suits signals like 'TP1 0.6R · TP2 1R · TP3 2R': with SL to breakeven after TP1, "
                    "the TP2/TP3 positions run risk-free.")
        self.alloc_note.setText(note)
        usable = mode not in PER_TP_MODES and split
        self.alloc_preset.setEnabled(usable)
        self.alloc.setEnabled(usable)
        for k in ("max_risk_per_signal_pct", "max_total_open_risk_pct", "max_correlated_risk_pct"):
            self.w[k].setEnabled(self.caps.isChecked())
        for k in ("max_daily_loss_pct", "max_drawdown_pct", "max_consecutive_losses"):
            self.w[k].setEnabled(self.ddp.isChecked())

    def _refresh_reviews(self) -> None:
        cfg, err = self._try_collect()
        if cfg is None:
            self.rv_pill.set("INVALID", "loss")
            self.rv_text.setText(f"⚠ {err}")
            return
        req = set(required_keys(cfg))
        done = 0
        for key, t in self._ticks.items():
            t.set_required(key in req)
            if key in req:
                ok = self._stamps.get(key) == review_value(cfg, key)
                t.set_pending(not ok)
                done += ok
        total = len(req)
        self.rv_pill.set(f"{done} / {total} CHECKED", "profit" if done == total else "loss")
        if done == total:
            text = ("All critical settings are checked. DEMO or REAL ACCOUNT auto-execution can be switched "
                    "on in Connection.")
        else:
            text = ("Settings in red must be checked before DEMO or REAL ACCOUNT auto-execution can be switched "
                    "on: look at each one and click its ✓. Changing a value later turns it red again.")
        if self._dirty(cfg):
            text += "  You have unsaved changes: press Save."
        self.rv_text.setText(text)

    def _tick(self, key: str, checked: bool) -> None:
        cfg, err = self._try_collect()
        if cfg is None:
            Toast.show_error(f"Fix the invalid setting first: {err}")
            self._refresh_reviews()
            return
        if checked:
            self._stamps[key] = review_value(cfg, key)
        else:
            self._stamps.pop(key, None)
        self._refresh_reviews()
        if not self._dirty(cfg):         # nothing else changed: keep the tick right away
            cfg.reviewed = dict(self._stamps)
            run_async(self.svc.save_config(cfg))

    def _apply_preset(self, index: int) -> None:
        if index <= 0:
            return
        cfg, err = self._try_collect()
        if cfg is None:
            Toast.show_error(f"Fix the invalid setting first: {err}")
            self.preset.setCurrentIndex(0)
            return
        r, e = cfg.risk, cfg.execution
        if index == 3:
            cfg.risk = RiskSettings()
        else:
            r.percent_caps_enabled = False
            r.small_volume_policy = SmallVolumePolicy.MIN_PER_TP
            r.lot_per_tp = 0.01
            r.max_concurrent_trades = 10
            r.max_trades_per_hour = 10
            r.max_margin_usage_pct = 80
            if index == 1:
                r.sizing_mode = SizingMode.LOT_PER_TP
                r.drawdown_protection = False
                r.max_lot_per_order = 0.10
                r.max_lots_per_symbol = 0.50
                r.min_free_margin = 20
            else:
                r.sizing_mode = SizingMode.LOT_PER_BALANCE
                r.balance_step = 300
                r.drawdown_protection = True
                r.max_daily_loss_pct = 10
                r.max_drawdown_pct = 30
                r.max_consecutive_losses = 0
                r.max_lot_per_order = 1.0
                r.max_lots_per_symbol = 3.0
                r.min_free_margin = 50
        e.tp_strategy = TpStrategy.SPLIT
        e.tp_allocation = [1.0, 1.0, 1.0]
        e.move_sl_to_be_after_tp = 1
        self._fill(cfg)
        self.preset.setCurrentIndex(0)
        self._changed_now()
        Toast.show_message("Preset applied: check the red settings, then Save", "info")

    # ------------------------------------------------------------------ preview
    def _request_preview(self) -> None:
        cfg, _ = self._try_collect()
        if cfg is None:
            return
        self._token += 1
        token = self._token
        run_async(self.svc.sizing_preview(cfg, fallback_balance=self.ex_bal.value(),
                                          sl_pips=self.ex_sl.value(), n_tps=self.ex_tps.value(),
                                          recent=self.rb_recent.isChecked()),
                  lambda p, t=token: self._show_preview(p, t),
                  lambda e: self.pv_summary.setText(f"Preview not available: {e}"))

    def _show_preview(self, pv, token: int) -> None:
        if token != self._token:
            return
        ccy = pv.currency
        muted, warn = color("muted"), color("warn")
        if pv.detected:
            self.pv_account.setText(
                f"Account <b>{money(pv.equity, ccy)}</b> equity · {money(pv.balance, ccy)} balance "
                f"<span style='color:{muted}'>· {pv.account}</span>"
                + ("" if pv.hedging else "<br>Netting account: one position per signal, closed in parts "
                                          "at the TPs."))
        else:
            self.pv_account.setText(
                f"<span style='color:{warn}'>Not connected to MT5</span>: showing an example balance "
                f"of <b>{money(pv.equity)}</b> (approximate values). Connect MT5 to see your real account.")
        self.ex_bal.setEnabled(not pv.detected)
        self.ex_sl.setEnabled(self.rb_typ.isChecked())
        self.ex_tps.setEnabled(self.rb_typ.isChecked())
        t = self.pv_table
        t.setRowCount(len(pv.rows))
        good, bad, muted_c = QColor(color("profit")), QColor(color("loss")), QColor(color("muted"))
        for i, row in enumerate(pv.rows):
            if len(row.positions) > 1:
                lots = "+".join(f"{v:g}" for _, v in row.positions) + f" = {row.total:g}"
            elif row.positions:
                name, v = row.positions[0]
                lots = f"{v:g}  ({name})"
            else:
                lots = "—"
            cells = [row.symbol, f"{row.sl_pips:g} pips" if row.sl_pips else "—", lots,
                     money(-row.risk_money, ccy) if row.risk_money is not None else "—",
                     f"{row.risk_pct:.1f} %" if row.risk_pct is not None else "—",
                     money(row.margin, ccy) if row.margin is not None else "—",
                     "✓" if row.ok else ("—" if row.reason == NOT_AT_BROKER else "✗")]
            tip = "\n".join(([row.label] if row.label not in ("typical", "example") else [])
                            + ([row.reason] if not row.ok and row.reason else []) + row.notes)
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if tip:
                    item.setToolTip(tip)
                if j == len(cells) - 1:
                    item.setForeground(good if row.ok else muted_c if row.reason == NOT_AT_BROKER else bad)
                    item.setTextAlignment(Qt.AlignCenter)
                    if not tip:
                        item.setToolTip("opens" if row.ok else row.reason)
                t.setItem(i, j, item)
        t.resizeRowsToContents()
        rows_h = sum(t.rowHeight(i) for i in range(t.rowCount()))
        t.setFixedHeight(min(640, t.horizontalHeader().height() + rows_h + 6))
        loss = color("loss")
        self.pv_summary.setText("<br>".join(
            f"<span style='color:{loss}'>• {s}</span>" if "would NOT open" in s else f"• {s}"
            for s in pv.summary))

    # ------------------------------------------------------------------ actions
    def save(self) -> None:
        cfg, err = self._try_collect()
        if cfg is None:
            Toast.show_error(f"Invalid settings: {err}")
            return

        def ok(_):
            left = len(pending_reviews(self.svc.config))
            Toast.show_message("Risk settings saved" + (f" · {left} critical setting(s) still to check"
                                                        if left else " · all critical settings checked"),
                               "warn" if left else "profit")
            self._refresh_reviews()
        run_async(self.svc.save_config(cfg), ok)

    def _reset_dd(self) -> None:
        a = self.svc.engine.account
        if a is None:
            Toast.show_error("Connect MT5 first")
            return
        if QMessageBox.question(self, "Reset drawdown lockout",
                                f"Reset the drawdown lockout and set the equity peak to {a.equity:,.2f}?") \
                == QMessageBox.Yes:
            self.svc.risk.reset_drawdown_lockout(a.equity)

    def activated(self) -> None:
        self._request_preview()

    def refresh(self, s: Snapshot) -> None:
        eq = s.account.equity if s.account else None
        if (eq is None) != (self._equity is None) or (eq is not None and abs(eq - self._equity) > 0.5):
            self._equity = eq
            if self.isVisible():
                self._request_preview()
        st = self.svc.risk.state
        if s.blocking_reason:
            self.lock_pill.set("LOCKED", "loss")
            self.lock_text.setText(s.blocking_reason)
        else:
            self.lock_pill.set("OK", "profit")
            self.lock_text.setText(f"Consecutive losses: {st.consecutive_losses} · day start equity "
                                   f"{money(st.day_start_equity)} · equity peak {money(st.equity_peak)}"
                                   + ("" if self.svc.config.risk.drawdown_protection else
                                      " · drawdown protection OFF"))
        self.reset_dd.setEnabled(st.drawdown_lockout)
        self.reset_br.setEnabled(st.breaker_tripped)
        self.clear_em.setEnabled(st.emergency_stop)
