from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QLineEdit, QMessageBox, QPushButton, QRadioButton, QSizePolicy, QVBoxLayout,
)

from tgmt5.config import AppConfig
from tgmt5.domain.models import ExecutionMode
from tgmt5.mt5.real import detect_terminals
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import Card, Dot, Pill, ResponsiveRow, Toast, color, hbox, label, money, run_async

MODES = [ExecutionMode.MONITOR, ExecutionMode.PAPER, ExecutionMode.DEMO, ExecutionMode.LIVE]


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


class ConfirmRealDialog(QDialog):
    """REAL ACCOUNT confirmation: tick the risk statement AND type the account number."""

    def __init__(self, parent, acct) -> None:
        super().__init__(parent)
        self.acct = acct
        self.setWindowTitle("Enable REAL ACCOUNT execution")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        title = label("Trade with REAL MONEY on this account?", "h2")
        title.setStyleSheet(f"color: {color('loss')};")
        lay.addWidget(title)
        lay.addWidget(label(
            f"Account {acct.login} · {acct.server} · {acct.company}\n"
            f"Balance {money(acct.balance, acct.currency)} · leverage 1:{acct.leverage}\n\n"
            "Every signal that passes your risk settings will be sent to this REAL-money account "
            "automatically. Losses are real and can exceed what the preview shows (gaps, slippage, "
            "broker problems, provider mistakes). The EMERGENCY STOP button stops new trades at any time; "
            "open positions stay open until you close them.", wrap=True))
        self.ack = QCheckBox("I understand that I can lose real money and that I am responsible for it")
        lay.addWidget(self.ack)
        lay.addWidget(label(f"Type the account number ({acct.login}) to confirm:", "muted"))
        self.typed = QLineEdit()
        self.typed.setPlaceholderText(str(acct.login))
        lay.addWidget(self.typed)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = self.bb.button(QDialogButtonBox.Ok)
        ok.setText("Enable REAL ACCOUNT")
        ok.setProperty("kind", "danger")
        ok.setEnabled(False)
        self.ack.toggled.connect(self._check)
        self.typed.textChanged.connect(self._check)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

    def _check(self, *_a) -> None:
        self.bb.button(QDialogButtonBox.Ok).setEnabled(
            self.ack.isChecked() and self.typed.text().strip() == str(self.acct.login))


class ConnectionPage(Page):
    title = "Connection & Execution"
    subtitle = ("Connect the local MetaTrader 5 terminal. DEMO accounts work out of the box. Real-money accounts "
                "are refused unless you allow them below and confirm the account by typing its number; the "
                "account type is checked again before every order.")

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
        self.login.setPlaceholderText("account number")
        self.server = QLineEdit(c.server or "ICMarketsSC-Demo")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("leave empty to use the saved password")
        self.save_pw = QCheckBox("Save password in Windows Credential Manager")
        self.suffix = QLineEdit(c.symbol_suffix)
        self.suffix.setPlaceholderText("auto-detect")
        self.allow_real = QCheckBox("Allow real-money accounts (enables REAL ACCOUNT mode)")
        self.allow_real.setChecked(c.allow_real_account)
        self.allow_real.setToolTip("Off: only DEMO accounts can be connected. On: a real account can be "
                                   "connected; trading it still needs REAL ACCOUNT mode and a typed confirmation.")
        self.allow_real.clicked.connect(self._toggle_real)
        f = QFormLayout()
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        f.addRow("Terminal", hbox(self.path, detect, browse))
        f.addRow("Login", self.login)
        f.addRow("Server", self.server)
        f.addRow("Password", self.password)
        f.addRow("", self.save_pw)
        f.addRow("Symbol suffix", self.suffix)
        f.addRow("", self.allow_real)
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
        self.v_title.setWordWrap(True)
        self.v_pill = Pill("—")
        self.v_lines = label("", "muted", wrap=True)
        ver.add(layout=hbox(self.v_dot, self.v_title, "stretch", self.v_pill))
        ver.add(self.v_lines)
        ver.lay.addStretch(1)
        self.body.addWidget(ResponsiveRow(mt5, ver, breakpoint=900, stretches=[3, 2]))

        ex = Card("Execution mode")
        self.group = QButtonGroup(self)
        self.r_mon = QRadioButton("Monitor only: parse and validate, never send orders (default)")
        self.r_pap = QRadioButton("Paper trading: simulate fills with live MT5 prices, no orders sent")
        self.r_demo = QRadioButton("DEMO auto-execution: send orders to the verified DEMO account")
        self.r_live = QRadioButton("REAL ACCOUNT auto-execution: send orders to your REAL-money account")
        self.r_live.setStyleSheet(f"QRadioButton {{ color: {color('loss')}; font-weight: 650; }}"
                                  f"QRadioButton:disabled {{ color: {color('muted')}; font-weight: 400; }}")
        for i, r in enumerate((self.r_mon, self.r_pap, self.r_demo, self.r_live)):
            self.group.addButton(r, i)
            ex.add(r)
        self.r_mon.setChecked(True)
        apply = QPushButton("Apply mode")
        apply.setProperty("kind", "primary")
        apply.clicked.connect(self._apply_mode)
        apply.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.mode_note = label("", "muted", wrap=True)
        ex.add(layout=hbox(self.mode_note, apply))

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
        pf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        pf.addRow("Starting balance", self.p_bal)
        pf.addRow("Extra spread (points)", self.p_spread)
        pf.addRow("Slippage (points)", self.p_slip)
        pf.addRow("Commission per lot", self.p_comm)
        paper.add(layout=pf)
        paper.lay.addStretch(1)
        self.body.addWidget(ResponsiveRow(ex, paper, breakpoint=900, stretches=[3, 2]))

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
        of.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
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
        health.lay.addStretch(1)
        self.body.addWidget(ResponsiveRow(other, health, breakpoint=900))
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
        cfg.mt5.allow_real_account = self.allow_real.isChecked()
        return cfg

    def _toggle_real(self, checked: bool) -> None:
        if checked and QMessageBox.warning(
                self, "Allow real-money accounts",
                "With this on, a REAL-money MT5 account can be connected and, after a typed confirmation, "
                "traded automatically in REAL ACCOUNT mode.\n\nLosses on a real account are real. Test your "
                "settings on a DEMO account first.\n\nAllow real-money accounts?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            self.allow_real.setChecked(False)
            return
        cfg = self.svc.config.model_copy(deep=True)
        cfg.mt5.allow_real_account = checked
        run_async(self.svc.save_config(cfg), lambda _: Toast.show_message(
            "Real-money accounts allowed" if checked else "Real-money accounts blocked (REAL mode off)",
            "warn" if checked else "info"))

    def _connect(self) -> None:
        cfg = self._mt5_config()
        if cfg.mt5.login is None or not cfg.mt5.server:
            Toast.show_error("Enter the account number and server")
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
            Toast.show_message(f"Connected to {'DEMO' if acct.is_demo else 'REAL'} account {acct.login}",
                               "profit" if acct.is_demo else "warn")

        def err(e):
            self.connect_btn.setEnabled(True)
            Toast.show_error(str(e))
        run_async(go(), ok, err)

    def _apply_mode(self) -> None:
        mode = MODES[self.group.checkedId()]
        acct = self.svc.real_gw.account_info() if self.svc.real_gw else None
        if mode in (ExecutionMode.DEMO, ExecutionMode.LIVE) and acct is None:
            Toast.show_error("Connect and verify the account first")
            return
        key = f"{acct.login}@{acct.server}" if acct else ""
        if mode is ExecutionMode.DEMO and self.svc.risk.state.confirmed_account != key:
            if ConfirmExecutionDialog(self, acct).exec() != QDialog.Accepted:
                return

            async def confirm_demo():
                await self.svc.confirm_automated_execution()
                await self.svc.set_mode(mode)
            run_async(confirm_demo(), lambda _: Toast.show_message("DEMO auto-execution enabled", "profit"))
            return
        if mode is ExecutionMode.LIVE and self.svc.risk.state.confirmed_real_account != key:
            if acct.is_demo:
                Toast.show_error("This is a DEMO account: use DEMO auto-execution")
                return
            dlg = ConfirmRealDialog(self, acct)
            if dlg.exec() != QDialog.Accepted:
                return
            typed = dlg.typed.text()

            async def confirm_real():
                await self.svc.confirm_real_account(typed)
                await self.svc.set_mode(mode)
            run_async(confirm_real(), lambda _: Toast.show_message("REAL ACCOUNT auto-execution enabled", "warn"))
            return
        run_async(self.svc.set_mode(mode), lambda _: Toast.show_message(
            "REAL ACCOUNT auto-execution enabled" if mode is ExecutionMode.LIVE else f"Mode: {mode.value}",
            "warn" if mode is ExecutionMode.LIVE else "info"))

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
        allowed = self.svc.config.mt5.allow_real_account
        if self.allow_real.isChecked() != allowed:
            self.allow_real.setChecked(allowed)
        self.r_live.setEnabled(allowed and not s.sandbox)
        a = self.svc.real_gw.account_info() if (self.svc.real_gw and s.mt5_connected) else None
        if a:
            real = not a.is_demo
            self.v_dot.set_tone("loss" if real else "profit")
            self.v_title.setText(f"{a.login} @ {a.server}")
            if s.sandbox:
                self.v_pill.set("SANDBOX DEMO", "warn")
            elif real:
                self.v_pill.set("REAL ACCOUNT" if allowed else "LIVE - BLOCKED", "loss")
            else:
                self.v_pill.set("DEMO VERIFIED", "profit")
            self.v_lines.setText(
                f"Broker: {a.company}\n"
                f"Currency: {a.currency} · Leverage 1:{a.leverage} · {'Hedging' if a.is_hedging else 'Netting'}\n"
                f"Balance {money(a.balance)} · Equity {money(a.equity)}\n"
                f"Margin {money(a.margin)} · Free {money(a.margin_free)}\n"
                f"{'REAL-money' if real else 'DEMO'} execution confirmed: {'yes' if s.confirmed else 'no'}")
        else:
            self.v_dot.set_tone("loss")
            self.v_title.setText("Not connected")
            self.v_pill.set("OFFLINE", "muted")
            self.v_lines.setText("Requirements: MT5 terminal installed and logged in once, 'Algo Trading' "
                                 "enabled. DEMO accounts always; real-money accounts only when allowed above.")
        notes = {"MONITOR": "Currently monitoring only.", "PAPER": "Currently paper trading.",
                 "DEMO": "Currently executing on the DEMO account.",
                 "LIVE": "Currently executing on the REAL-money account."}
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
            f"Database: {_breakable(self.svc.db.url.replace('sqlite:///', ''))}")


def _breakable(path: str) -> str:
    """Let long paths wrap after each separator (zero-width space) instead of widening the page."""
    return path.replace("\\", "\\​").replace("/", "/​")
