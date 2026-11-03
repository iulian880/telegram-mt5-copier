from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QLineEdit, QMessageBox,
    QPushButton, QSlider, QSpinBox,
)
from pydantic import ValidationError

from tgmt5.config import AppConfig, SizingMode, TpHitAction, TpStrategy, ZonePolicy
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import Card, Pill, Toast, hbox, label, money, run_async


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


class RiskPage(Page):
    title = "Risk Management"
    subtitle = ("All limits are enforced by the backend risk engine on every order, even if this window "
                "is closed. Changes apply after Save.")

    def build(self) -> None:
        self._equity = 0.0
        # --- sizing card
        sizing = Card("Position sizing")
        self.mode = QComboBox()
        self.mode.addItems([m.value for m in SizingMode])
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, 100)        # 0.05% .. 5.00% in 0.05 steps
        self.pct = dspin(0.05, 5, 0.05, suffix=" %")
        self.slider.valueChanged.connect(lambda v: self.pct.setValue(v * 0.05))
        self.pct.valueChanged.connect(lambda v: self.slider.setValue(round(v / 0.05)))
        self.fixed_lot = dspin(0.01, 100, 0.01)
        self.fixed_money = dspin(1, 1e6, 5, suffix=" ")
        f = QFormLayout()
        f.addRow("Sizing mode", self.mode)
        f.addRow("Risk per signal", hbox(self.slider, self.pct))
        f.addRow("Fixed lot", self.fixed_lot)
        f.addRow("Fixed money risk", self.fixed_money)
        sizing.add(layout=f)

        limits = Card("Trade limits")
        self.w = {
            "max_risk_per_signal_pct": dspin(0.05, 5, 0.05, suffix=" %"),
            "max_total_open_risk_pct": dspin(0.1, 25, 0.1, suffix=" %"),
            "max_daily_loss_pct": dspin(0.1, 50, 0.1, suffix=" %"),
            "max_drawdown_pct": dspin(0.5, 90, 0.5, suffix=" %"),
            "max_consecutive_losses": ispin(0, 50),
            "max_concurrent_trades": ispin(1, 100),
            "max_trades_per_hour": ispin(1, 100),
            "max_lot_per_order": dspin(0.01, 100, 0.01),
            "max_lots_per_symbol": dspin(0.01, 500, 0.1),
            "max_margin_usage_pct": dspin(1, 100, 1, suffix=" %"),
            "min_free_margin": dspin(0, 1e7, 50),
            "max_correlated_risk_pct": dspin(0.1, 25, 0.1, suffix=" %"),
            "max_spread_sl_fraction": dspin(0.01, 1, 0.01),
            "max_entry_deviation_sl_fraction": dspin(0, 2, 0.05),
            "max_slippage_points": ispin(0, 10000, " pts"),
            "min_risk_reward": dspin(0, 10, 0.1),
        }
        labels = {
            "max_risk_per_signal_pct": "Max risk per signal", "max_total_open_risk_pct": "Max total open risk",
            "max_daily_loss_pct": "Daily loss lockout", "max_drawdown_pct": "Max drawdown lockout",
            "max_consecutive_losses": "Consecutive-loss breaker (0=off)",
            "max_concurrent_trades": "Max concurrent trades", "max_trades_per_hour": "Max trades / hour",
            "max_lot_per_order": "Max lot per order", "max_lots_per_symbol": "Max lots per symbol",
            "max_margin_usage_pct": "Max margin usage", "min_free_margin": "Min free margin",
            "max_correlated_risk_pct": "Max correlated risk",
            "max_spread_sl_fraction": "Max spread (× SL distance)",
            "max_entry_deviation_sl_fraction": "Entry tolerance (× SL distance)",
            "max_slippage_points": "Max slippage", "min_risk_reward": "Min R:R (0=off)",
        }
        g = QGridLayout()
        g.setHorizontalSpacing(16)
        for i, (k, w) in enumerate(self.w.items()):
            g.addWidget(label(labels[k], "muted"), i // 2, (i % 2) * 2)
            g.addWidget(w, i // 2, (i % 2) * 2 + 1)
        limits.add(layout=g)
        self.whitelist = QLineEdit()
        self.blacklist = QLineEdit()
        self.whitelist.setPlaceholderText("empty = all symbols")
        limits.add(layout=hbox(label("Whitelist", "muted"), self.whitelist,
                               label("Blacklist", "muted"), self.blacklist))
        self.hours = QCheckBox("Restrict trading hours (UTC)")
        self.h_from, self.h_to = ispin(0, 23), ispin(1, 24)
        limits.add(layout=hbox(self.hours, self.h_from, label("to"), self.h_to, "stretch"))

        mgmt = Card("Entries, take profits & management")
        self.zone = QComboBox()
        self.zone.addItems([z.value for z in ZonePolicy])
        self.zone.setToolTip("WAIT: wait for price to reach the zone · REJECT · PENDING: place a limit order "
                             "at the zone edge (only if you choose it)")
        self.tp_strategy = QComboBox()
        self.tp_strategy.addItems([t.value for t in TpStrategy])
        self.alloc = QLineEdit()
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
        f2 = QFormLayout()
        f2.addRow("Entry zone policy", self.zone)
        f2.addRow("Max signal age", self.age)
        f2.addRow("Waiting signal expiry", self.wait_exp)
        f2.addRow("Pending order expiry", self.pend_exp)
        f2.addRow("Multiple TP handling", self.tp_strategy)
        f2.addRow("TP volume allocation", self.alloc)
        f2.addRow("Move SL to BE after TP # (0=off)", self.be_after)
        f2.addRow("Breakeven offset", self.be_off)
        f2.addRow(self.trail, self.trail_frac)
        f2.addRow("'TP hit' messages", self.tp_msg)
        f2.addRow(self.req_tp)
        f2.addRow(self.edits)
        mgmt.add(layout=f2)

        # --- consequences + lockouts
        self.conseq = Card("Estimated consequences")
        self.c_lines = label("", wrap=True)
        self.c_lines.setTextFormat(Qt.RichText)
        self.conseq.add(self.c_lines)
        save = QPushButton("Save risk settings")
        save.setProperty("kind", "primary")
        save.clicked.connect(self.save)
        revert = QPushButton("Revert")
        revert.clicked.connect(self.load)
        self.conseq.add(layout=hbox("stretch", revert, save))

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

        self.body.addLayout(hbox(sizing, self.conseq, spacing=16))
        self.body.addWidget(limits)
        self.body.addLayout(hbox(mgmt, self.lock, spacing=16))
        self.body.addStretch()
        for w in list(self.w.values()) + [self.pct, self.fixed_lot, self.fixed_money]:
            w.valueChanged.connect(self._update_conseq)
        self.mode.currentIndexChanged.connect(self._update_conseq)
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        r, e = self.svc.config.risk, self.svc.config.execution
        self.mode.setCurrentText(r.sizing_mode.value)
        self.pct.setValue(r.risk_percent)
        self.fixed_lot.setValue(r.fixed_lot)
        self.fixed_money.setValue(r.fixed_money)
        for k, w in self.w.items():
            w.setValue(getattr(r, k))
        self.whitelist.setText(", ".join(r.symbol_whitelist))
        self.blacklist.setText(", ".join(r.symbol_blacklist))
        self.hours.setChecked(r.trading_hours_utc is not None)
        if r.trading_hours_utc:
            self.h_from.setValue(r.trading_hours_utc[0])
            self.h_to.setValue(r.trading_hours_utc[1])
        self.zone.setCurrentText(e.zone_policy.value)
        self.tp_strategy.setCurrentText(e.tp_strategy.value)
        self.alloc.setText(", ".join(f"{x:g}" for x in e.tp_allocation))
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
        self._update_conseq()

    def _collect(self) -> AppConfig:
        cfg = self.svc.config.model_copy(deep=True)
        r = cfg.risk.model_dump()
        r.update(sizing_mode=self.mode.currentText(), risk_percent=self.pct.value(),
                 fixed_lot=self.fixed_lot.value(), fixed_money=self.fixed_money.value(),
                 symbol_whitelist=[s.strip().upper() for s in self.whitelist.text().split(",") if s.strip()],
                 symbol_blacklist=[s.strip().upper() for s in self.blacklist.text().split(",") if s.strip()],
                 trading_hours_utc=(self.h_from.value(), self.h_to.value()) if self.hours.isChecked() else None)
        for k, w in self.w.items():
            r[k] = w.value()
        e = cfg.execution.model_dump()
        e.update(zone_policy=self.zone.currentText(), tp_strategy=self.tp_strategy.currentText(),
                 tp_allocation=[float(x) for x in self.alloc.text().replace(";", ",").split(",") if x.strip()],
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
        return AppConfig.model_validate(data)

    def _update_conseq(self) -> None:
        eq = self._equity or self.svc.config.paper.starting_balance
        basis = "current equity" if self._equity else "example equity"
        r = self.w
        pct = self.pct.value()
        mode = self.mode.currentText()
        per = {"PCT_EQUITY": eq * pct / 100, "PCT_BALANCE": eq * pct / 100,
               "FIXED_MONEY": self.fixed_money.value()}.get(mode)
        cap = eq * r["max_risk_per_signal_pct"].value() / 100
        lines = [f"Based on {basis} <b>{money(eq)}</b>:"]
        if per is not None:
            eff = min(per, cap)
            lines.append(f"• Each signal risks up to <b>{money(eff)}</b> if its stop loss is hit"
                         + (" (capped by max risk per signal)" if per > cap else ""))
        else:
            lines.append(f"• Fixed {self.fixed_lot.value():g} lots; signals whose SL loss exceeds "
                         f"<b>{money(cap)}</b> are rejected")
        n = r["max_concurrent_trades"].value()
        lines.append(f"• At most <b>{n}</b> trades open, with total open risk ≤ "
                     f"<b>{money(eq * r['max_total_open_risk_pct'].value() / 100)}</b>")
        lines.append(f"• Trading locks for the day after losing <b>{money(eq * r['max_daily_loss_pct'].value() / 100)}</b>")
        lines.append(f"• Trading locks until manual reset after a drawdown of "
                     f"<b>{money(eq * r['max_drawdown_pct'].value() / 100)}</b> from peak")
        if per is not None and per > 0:
            losses = int(eq * r["max_daily_loss_pct"].value() / 100 / min(per, cap))
            lines.append(f"• ≈ <b>{losses}</b> full stop-outs reach the daily lockout")
        try:
            self._collect()
        except (ValidationError, ValueError) as e:
            msg = str(e).splitlines()
            lines.append(f"<span style='color:#F05252'>⚠ {msg[-1] if msg else e}</span>")
        self.c_lines.setText("<br>".join(lines))

    def save(self) -> None:
        try:
            cfg = self._collect()
        except (ValidationError, ValueError) as e:
            Toast.show_error(f"Invalid settings: {e}")
            return
        run_async(self.svc.save_config(cfg), lambda _: Toast.show_message("Risk settings saved", "profit"))

    def _reset_dd(self) -> None:
        a = self.svc.engine.account
        if a is None:
            Toast.show_error("Connect MT5 first")
            return
        if QMessageBox.question(self, "Reset drawdown lockout",
                                f"Reset the drawdown lockout and set the equity peak to {a.equity:,.2f}?") \
                == QMessageBox.Yes:
            self.svc.risk.reset_drawdown_lockout(a.equity)

    def refresh(self, s: Snapshot) -> None:
        if s.account and abs(s.account.equity - self._equity) > 0.5:
            self._equity = s.account.equity
            self._update_conseq()
        st = self.svc.risk.state
        if s.blocking_reason:
            self.lock_pill.set("LOCKED", "loss")
            self.lock_text.setText(s.blocking_reason)
        else:
            self.lock_pill.set("OK", "profit")
            self.lock_text.setText(f"Consecutive losses: {st.consecutive_losses} · day start equity "
                                   f"{money(st.day_start_equity)} · equity peak {money(st.equity_peak)}")
        self.reset_dd.setEnabled(st.drawdown_lockout)
        self.reset_br.setEnabled(st.breaker_tripped)
        self.clear_em.setEnabled(st.emergency_stop)
