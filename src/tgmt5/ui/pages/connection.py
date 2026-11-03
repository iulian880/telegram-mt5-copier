from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QLineEdit, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
)

from tgmt5.config import AppConfig
from tgmt5.domain.models import ExecutionMode
from tgmt5.mt5.real import detect_terminals
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import Card, Dot, Pill, Toast, hbox, label, money, run_async


class ConfirmExecutionDialog(QDialog):
    def __init__(self, parent, acct) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enable automated DEMO execution")
        lay = QVBoxLayout(self)
        lay.addWidget(label("Enable automated execution on this DEMO account?", "h2"))
        lay.addWidget(label(
            f"Account {acct.login} · {acct.server} · {acct.company}\nBalance {money(acct.balance, acct.currency)}"
            f" · leverage 1:{acct.leverage}\n\nSignals from your enabled Telegram sources will be sent to MT5 "
            "as real orders on this demo account after passing every risk check. You can stop at any time "
            "with the EMERGENCY STOP button.", wrap=True))
        self.ack = QCheckBox("I understand that orders will be placed automatically on this DEMO account")
        lay.addWidget(self.ack)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setEnabled(False)
        bb.button(QDialogButtonBox.Ok).setText("Enable")
        self.ack.toggled.connect(bb.button(QDialogButtonBox.Ok).setEnabled)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ConnectionPage(Page):
    title = "Connection & Execution"
    subtitle = ("Connect the local MetaTrader 5 terminal to an IC Markets SC DEMO account. Accounts that "
                "are not DEMO are refused, and the check is repeated before every order.")

    def build(self) -> None:
        c = self.svc.config.mt5
        mt5 = Card("MetaTrader 5")
        self.path = QComboBox()
        self.path.setEditable(True)
        self.path.addItems([c.terminal_path] if c.terminal_path else [])
        detect = QPushButton("Detect")
        browse = QPushButton("Browse…")
        detect.clicked.connect(self._detect)
        browse.clicked.connect(self._browse)
        self.login = QLineEdit(str(c.login or ""))
        self.login.setPlaceholderText("DEMO account number")
        self.server = QLineEdit(c.server or "ICMarketsSC-Demo")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("leave empty to use the saved password")
        self.save_pw = QCheckBox("Save password in Windows Credential Manager")
        self.suffix = QLineEdit(c.symbol_suffix)
        self.suffix.setPlaceholderText("auto-detect")
        f = QFormLayout()
        f.addRow("Terminal", hbox(self.path, detect, browse))
        f.addRow("Login", self.login)
        f.addRow("Server", self.server)
        f.addRow("Password", self.password)
        f.addRow("", self.save_pw)
        f.addRow("Symbol suffix", self.suffix)
        mt5.add(layout=f)
        self.connect_btn = QPushButton("Connect && verify")
        self.connect_btn.setProperty("kind", "primary")
        self.disc_btn = QPushButton("Disconnect")
        self.connect_btn.clicked.connect(self._connect)
        self.disc_btn.clicked.connect(lambda: run_async(self.svc.disconnect_mt5()))
        mt5.add(layout=hbox("stretch", self.disc_btn, self.connect_btn))

        ver = Card("Account verification")
        self.v_dot = Dot()
        self.v_title = label("Not connected", "h2")
        self.v_pill = Pill("—")
        self.v_lines = label("", "muted", wrap=True)
        ver.add(layout=hbox(self.v_dot, self.v_title, "stretch", self.v_pill))
        ver.add(self.v_lines)
        self.body.addLayout(hbox(mt5, ver, spacing=16))

        ex = Card("Execution mode")
        self.group = QButtonGroup(self)
        self.r_mon = QRadioButton("Monitor only — parse and validate, never send orders (default)")
        self.r_pap = QRadioButton("Paper trading — simulate fills with live MT5 prices, no orders sent")
        self.r_demo = QRadioButton("DEMO auto-execution — send orders to the verified DEMO account")
        for i, r in enumerate((self.r_mon, self.r_pap, self.r_demo)):
            self.group.addButton(r, i)
            ex.add(r)
        self.r_mon.setChecked(True)
        apply = QPushButton("Apply mode")
        apply.setProperty("kind", "primary")
        apply.clicked.connect(self._apply_mode)
        self.mode_note = label("", "muted", wrap=True)
        ex.add(layout=hbox(self.mode_note, "stretch", apply))

        paper = Card("Paper trading model")
        p = self.svc.config.paper
        self.p_bal = QDoubleSpinBox()
        self.p_bal.setRange(100, 1e8)
        self.p_bal.setValue(p.starting_balance)
        self.p_spread = QDoubleSpinBox()
        self.p_spread.setRange(0, 1e4)
        self.p_spread.setValue(p.extra_spread_points)
        self.p_slip = QDoubleSpinBox()
        self.p_slip.setRange(0, 1e4)
        self.p_slip.setValue(p.slippage_points)
        self.p_comm = QDoubleSpinBox()
        self.p_comm.setRange(0, 1000)
        self.p_comm.setValue(p.commission_per_lot)
        pf = QFormLayout()
        pf.addRow("Starting balance", self.p_bal)
        pf.addRow("Extra spread (points)", self.p_spread)
        pf.addRow("Slippage (points)", self.p_slip)
        pf.addRow("Commission per lot (round turn)", self.p_comm)
        paper.add(layout=pf)
        self.body.addLayout(hbox(ex, paper, spacing=16))

        other = Card("Notifications, appearance & configuration")
        n = self.svc.config.notifications
        self.n_desk = QCheckBox("Desktop notifications")
        self.n_desk.setChecked(n.desktop)
        self.n_tg = QLineEdit(n.telegram_destination)
        self.n_tg.setPlaceholderText("e.g. me (Saved Messages) - empty disables")
        self.theme = QComboBox()
        self.theme.addItems(["dark", "light"])
        self.theme.setCurrentText(self.svc.config.theme)
        of = QFormLayout()
        of.addRow(self.n_desk)
        of.addRow("Telegram notifications to", self.n_tg)
        of.addRow("Theme (applies on restart)", self.theme)
        other.add(layout=of)
        save = QPushButton("Save settings")
        save.setProperty("kind", "primary")
        save.clicked.connect(self._save)
        exp = QPushButton("Export config…")
        imp = QPushButton("Import config…")
        exp.clicked.connect(self._export)
        imp.clicked.connect(self._import)
        other.add(layout=hbox(exp, imp, "stretch", save))

        health = Card("Health diagnostics")
        self.h_lines = label("", "muted", wrap=True)
        health.add(self.h_lines)
        self.body.addLayout(hbox(other, health, spacing=16))
        self.body.addStretch()

    # ------------------------------------------------------------------
    def _detect(self) -> None:
        found = detect_terminals()
        self.path.clear()
        self.path.addItems(found)
        Toast.show_message(f"{len(found)} MT5 terminal(s) found" if found else
                           "No terminal64.exe found - use Browse", "info" if found else "warn")

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Select terminal64.exe", "", "terminal64.exe (terminal64.exe)")
        if p:
            self.path.setCurrentText(p)

    def _mt5_config(self) -> AppConfig:
        cfg = self.svc.config.model_copy(deep=True)
        cfg.mt5.terminal_path = self.path.currentText().strip()
        cfg.mt5.login = int(self.login.text()) if self.login.text().strip().isdigit() else None
        cfg.mt5.server = self.server.text().strip()
        cfg.mt5.symbol_suffix = self.suffix.text().strip()
        return cfg

    def _connect(self) -> None:
        cfg = self._mt5_config()
        if cfg.mt5.login is None or not cfg.mt5.server:
            Toast.show_error("Enter the DEMO account number and server")
            return
        pw = self.password.text() or None
        save = self.save_pw.isChecked()

        async def go():
            await self.svc.save_config(cfg)
            return await self.svc.connect_mt5(pw, save_password=save)
        self.connect_btn.setEnabled(False)

        def ok(acct):
            self.connect_btn.setEnabled(True)
            self.password.clear()
            Toast.show_message(f"Connected to DEMO account {acct.login}", "profit")

        def err(e):
            self.connect_btn.setEnabled(True)
            Toast.show_error(str(e))
        run_async(go(), ok, err)

    def _apply_mode(self) -> None:
        mode = [ExecutionMode.MONITOR, ExecutionMode.PAPER, ExecutionMode.DEMO][self.group.checkedId()]
        if mode is ExecutionMode.DEMO:
            snap_acct = self.svc.real_gw.account_info() if self.svc.real_gw else None
            if snap_acct is None:
                Toast.show_error("Connect and verify a DEMO account first")
                return
            key = f"{snap_acct.login}@{snap_acct.server}"
            if self.svc.risk.state.confirmed_account != key:
                if ConfirmExecutionDialog(self, snap_acct).exec() != QDialog.Accepted:
                    return

                async def confirm_then():
                    await self.svc.confirm_automated_execution()
                    await self.svc.set_mode(mode)
                run_async(confirm_then(), lambda _: Toast.show_message("DEMO auto-execution enabled", "profit"))
                return
        run_async(self.svc.set_mode(mode), lambda _: Toast.show_message(f"Mode: {mode.value}", "info"))

    def _save(self) -> None:
        cfg = self._mt5_config()
        cfg.paper.starting_balance = self.p_bal.value()
        cfg.paper.extra_spread_points = self.p_spread.value()
        cfg.paper.slippage_points = self.p_slip.value()
        cfg.paper.commission_per_lot = self.p_comm.value()
        cfg.notifications.desktop = self.n_desk.isChecked()
        cfg.notifications.telegram_destination = self.n_tg.text().strip()
        cfg.theme = self.theme.currentText()
        run_async(self.svc.save_config(cfg), lambda _: Toast.show_message("Settings saved", "profit"))

    def _export(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Export configuration", str(Path.home() / "tgmt5-config.json"),
                                           "JSON (*.json)")
        if p:
            Path(p).write_text(self.svc.config.export_json(), encoding="utf-8")
            Toast.show_message("Configuration exported (contains no credentials)", "profit")

    def _import(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Import configuration", "", "JSON (*.json)")
        if not p:
            return
        try:
            cfg = AppConfig.import_json(Path(p).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            Toast.show_error(f"Invalid configuration: {e}")
            return
        run_async(self.svc.save_config(cfg), lambda _: Toast.show_message("Configuration imported", "profit"))

    def refresh(self, s: Snapshot) -> None:
        a = self.svc.real_gw.account_info() if (self.svc.real_gw and s.mt5_connected) else None
        if a:
            self.v_dot.set_tone("profit" if a.is_demo else "loss")
            self.v_title.setText(f"{a.login} @ {a.server}")
            self.v_pill.set("SANDBOX DEMO" if s.sandbox else "DEMO VERIFIED" if a.is_demo else "LIVE - BLOCKED",
                            "warn" if s.sandbox else "profit" if a.is_demo else "loss")
            self.v_lines.setText(
                f"Broker: {a.company}\nCurrency: {a.currency} · Leverage 1:{a.leverage} · "
                f"{'Hedging' if a.is_hedging else 'Netting'} account\n"
                f"Balance {money(a.balance)} · Equity {money(a.equity)} · Margin {money(a.margin)} · "
                f"Free {money(a.margin_free)}\nAutomated execution confirmed: {'yes' if s.confirmed else 'no'}")
        else:
            self.v_dot.set_tone("loss")
            self.v_title.setText("Not connected")
            self.v_pill.set("OFFLINE", "muted")
            self.v_lines.setText("Requirements: MT5 terminal installed and logged in once, 'Algo Trading' "
                                 "enabled, account type DEMO.")
        notes = {"MONITOR": "Currently monitoring only.", "PAPER": "Currently paper trading.",
                 "DEMO": "Currently executing on the DEMO account."}
        self.mode_note.setText(notes[s.mode] + (f"  ⚠ {s.blocking_reason}" if s.blocking_reason else ""))
        ti = self.svc.real_gw.terminal_info() if (self.svc.real_gw and s.mt5_connected) else None
        rec = self.svc.last_recovery.summary() if self.svc.last_recovery else "no recovery run yet"
        tg = s.telegram
        self.h_lines.setText(
            f"MT5 terminal: {'connected' if s.mt5_connected else 'disconnected'}"
            + (f", algo trading {'ON' if ti.trade_allowed else 'OFF'}" if ti else "") + "\n"
            f"Telegram: {'connected' if tg['connected'] else 'disconnected'}, "
            f"{'authorised' if tg['authorized'] else 'not authorised'}\n"
            f"Signal queue: {s.queue_depth} waiting\n"
            f"Last recovery: {rec}\n"
            f"Database: {self.svc.db.url.replace('sqlite:///', '')}")
