from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFormLayout, QHeaderView, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem,
)

from tgmt5.analytics.stats import compute
from tgmt5.config import SourceConfig
from tgmt5.secrets import SecretStore
from tgmt5.services.app import Snapshot
from tgmt5.ui.pages.base import Page
from tgmt5.ui.widgets import Card, Dot, Toast, hbox, label, run_async

SRC_COLS = ["Enabled", "Chat ID", "Name", "Risk ×", "Symbol aliases (GOLD=XAUUSD, …)", "Parser template"]


class SourcesPage(Page):
    title = "Telegram Sources"
    subtitle = ("Log in with your own Telegram account (MTProto user session). Only groups/channels you "
                "already belong to can be selected. Credentials are stored in the Windows Credential "
                "Manager, never in files or logs.")

    def build(self) -> None:
        auth = Card("Telegram login")
        self.api_id = QLineEdit()
        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.Password)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+40…")
        self.code = QLineEdit()
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("only if 2FA is enabled")
        f = QFormLayout()
        f.addRow("API ID", self.api_id)
        f.addRow("API hash", self.api_hash)
        f.addRow("Phone number", self.phone)
        f.addRow("Login code", self.code)
        f.addRow("2FA password", self.pw)
        auth.add(layout=f)
        save_api = QPushButton("Save API credentials")
        send = QPushButton("Send code")
        sign = QPushButton("Sign in")
        sign.setProperty("kind", "primary")
        save_api.clicked.connect(self._save_api)
        send.clicked.connect(self._send_code)
        sign.clicked.connect(self._sign_in)
        auth.add(layout=hbox(save_api, "stretch", send, sign))
        auth.add(label("Get your API ID and hash at my.telegram.org → API development tools.", "muted",
                       wrap=True))

        diag = Card("Connection diagnostics")
        self.dot = Dot()
        self.diag_title = label("—", "h2")
        self.diag = label("", "muted", wrap=True)
        diag.add(layout=hbox(self.dot, self.diag_title, "stretch"))
        diag.add(self.diag)
        reconnect = QPushButton("Restart listener")
        reconnect.clicked.connect(lambda: run_async(self.svc.start_telegram(),
                                                    lambda ok: Toast.show_message(
                                                        "Listener started" if ok else "Listener not started — see log",
                                                        "profit" if ok else "warn")))
        diag.add(layout=hbox("stretch", reconnect))
        self.body.addLayout(hbox(auth, diag, spacing=16))

        dl = Card("My groups & channels")
        self.dialogs = QTableWidget(0, 3)
        self.dialogs.setHorizontalHeaderLabels(["Name", "Type", "Chat ID"])
        self.dialogs.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.dialogs.verticalHeader().setVisible(False)
        self.dialogs.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.dialogs.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.dialogs.setMinimumHeight(180)
        load = QPushButton("Load my chats")
        add = QPushButton("Add selected as source")
        load.clicked.connect(self._load_dialogs)
        add.clicked.connect(self._add_selected)
        dl.header.addWidget(load)
        dl.header.addWidget(add)
        dl.add(self.dialogs)
        self.body.addWidget(dl)

        sc = Card("Signal sources")
        self.src = QTableWidget(0, len(SRC_COLS))
        self.src.setHorizontalHeaderLabels(SRC_COLS)
        self.src.verticalHeader().setVisible(False)
        self.src.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.src.setMinimumHeight(180)
        save = QPushButton("Save sources")
        save.setProperty("kind", "primary")
        rm = QPushButton("Remove selected")
        save.clicked.connect(self._save_sources)
        rm.clicked.connect(lambda: [self.src.removeRow(i.row()) for i in
                                    sorted(self.src.selectionModel().selectedRows(), reverse=True)])
        sc.header.addWidget(rm)
        sc.header.addWidget(save)
        sc.add(self.src)
        sc.add(label("Parser templates: 'default' requires NOW/MARKET or a price for an entry; "
                     "'implicit_market' treats 'BUY GOLD + SL/TP' without a price as a market order.",
                     "muted", wrap=True))
        self.body.addWidget(sc)

        perf = Card("Per-source performance")
        self.perf = QTableWidget(0, 4)
        self.perf.setHorizontalHeaderLabels(["Source", "Closed trades", "Win rate", "Net P&L"])
        self.perf.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.perf.verticalHeader().setVisible(False)
        self.perf.setMinimumHeight(140)
        perf.add(self.perf)
        self.body.addWidget(perf)
        self.body.addStretch()
        self._load_sources()
        self._perf_t = 0.0
        has_id = self.svc.secrets.get(SecretStore.TG_API_ID)
        if has_id:
            self.api_id.setText(has_id)
            self.api_hash.setPlaceholderText("stored in keyring")

    # ------------------------------------------------------------------
    def _save_api(self) -> None:
        if not self.api_id.text().strip().isdigit() or len(self.api_hash.text().strip()) < 16:
            Toast.show_error("Enter a numeric API ID and the 32-character API hash")
            return
        self.svc.secrets.set(SecretStore.TG_API_ID, self.api_id.text().strip())
        self.svc.secrets.set(SecretStore.TG_API_HASH, self.api_hash.text().strip())
        self.api_hash.clear()
        self.api_hash.setPlaceholderText("stored in keyring")
        Toast.show_message("API credentials saved to the OS keyring", "profit")

    def _send_code(self) -> None:
        tg = self.svc.telegram
        if not hasattr(tg, "send_code"):
            Toast.show_message("Sandbox mode uses a simulated Telegram feed", "warn")
            return
        run_async(tg.send_code(self.phone.text().strip()),
                  lambda _: Toast.show_message("Code sent via Telegram", "info"))

    def _sign_in(self) -> None:
        tg = self.svc.telegram
        if not hasattr(tg, "sign_in"):
            return

        def done(r):
            if r == "password_required":
                Toast.show_message("2FA enabled: enter your password and press Sign in again", "warn")
                return
            self.code.clear()
            self.pw.clear()
            Toast.show_message("Signed in to Telegram", "profit")
            run_async(self.svc.start_telegram())
        run_async(tg.sign_in(self.phone.text().strip(), self.code.text().strip(),
                             self.pw.text() or None), done)

    def _load_dialogs(self) -> None:
        def show(ds):
            self.dialogs.setRowCount(len(ds))
            for r, d in enumerate(ds):
                for c, v in enumerate([d.title, d.kind, str(d.id)]):
                    self.dialogs.setItem(r, c, QTableWidgetItem(v))
        run_async(self.svc.telegram.list_dialogs(), show)

    def _add_selected(self) -> None:
        existing = {int(self.src.item(r, 1).text()) for r in range(self.src.rowCount())}
        for idx in self.dialogs.selectionModel().selectedRows():
            cid = int(self.dialogs.item(idx.row(), 2).text())
            if cid not in existing:
                self._add_row(SourceConfig(chat_id=cid, title=self.dialogs.item(idx.row(), 0).text()))
        Toast.show_message("Added - press 'Save sources' to apply", "info")

    def _add_row(self, s: SourceConfig) -> None:
        r = self.src.rowCount()
        self.src.insertRow(r)
        en = QTableWidgetItem()
        en.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        en.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
        self.src.setItem(r, 0, en)
        cid = QTableWidgetItem(str(s.chat_id))
        cid.setFlags(cid.flags() & ~Qt.ItemIsEditable)
        self.src.setItem(r, 1, cid)
        self.src.setItem(r, 2, QTableWidgetItem(s.title))
        self.src.setItem(r, 3, QTableWidgetItem(f"{s.risk_multiplier:g}"))
        self.src.setItem(r, 4, QTableWidgetItem(", ".join(f"{k}={v}" for k, v in s.symbol_aliases.items())))
        self.src.setItem(r, 5, QTableWidgetItem(s.template))

    def _load_sources(self) -> None:
        self.src.setRowCount(0)
        for s in self.svc.config.sources:
            self._add_row(s)

    def _save_sources(self) -> None:
        try:
            out = []
            for r in range(self.src.rowCount()):
                aliases = {}
                for part in self.src.item(r, 4).text().split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        aliases[k.strip().upper()] = v.strip().upper()
                tmpl = self.src.item(r, 5).text().strip() or "default"
                if tmpl not in ("default", "implicit_market"):
                    raise ValueError(f"unknown parser template {tmpl!r}")
                out.append(SourceConfig(
                    chat_id=int(self.src.item(r, 1).text()), title=self.src.item(r, 2).text(),
                    enabled=self.src.item(r, 0).checkState() == Qt.Checked,
                    risk_multiplier=float(self.src.item(r, 3).text()), symbol_aliases=aliases,
                    template=tmpl))
        except Exception as e:  # noqa: BLE001
            Toast.show_error(f"Invalid source settings: {e}")
            return
        cfg = self.svc.config.model_copy(deep=True)
        cfg.sources = out
        run_async(self.svc.save_config(cfg), lambda _: Toast.show_message("Sources saved", "profit"))

    def refresh(self, s: Snapshot) -> None:
        tg = s.telegram
        self.dot.set_tone("profit" if tg["connected"] and tg["authorized"] else
                          "warn" if tg["connected"] else "loss")
        self.diag_title.setText("Listening" if tg["connected"] and tg["authorized"] else
                                "Not authorised" if tg["connected"] else "Disconnected")
        self.diag.setText(
            f"Account: {('@' + tg['user']) if tg['user'] else '—'}\n"
            f"Ping: {tg['latency_ms']:.0f} ms\n" if tg["latency_ms"] else
            f"Account: {('@' + tg['user']) if tg['user'] else '—'}\nPing: —\n")
        self.diag.setText(self.diag.text() +
                          f"Messages received: {tg['messages']} · reconnects: {tg['reconnects']}\n"
                          f"Average delivery delay: "
                          f"{(tg['avg_delivery_ms'] or 0) / 1000:.2f}s · queue depth {s.queue_depth}")
        if time.monotonic() - self._perf_t > 20:
            self._perf_t = time.monotonic()
            st = compute(self.svc.db, simulated=s.mode == "PAPER")
            names = {c.chat_id: c.title or str(c.chat_id) for c in self.svc.config.sources}
            self.perf.setRowCount(len(st.per_source))
            for r, (sid, v) in enumerate(st.per_source.items()):
                for c, val in enumerate([names.get(sid, str(sid)), str(v["trades"]),
                                         f"{v['win_rate']:.0f}%", f"{v['net']:+,.2f}"]):
                    self.perf.setItem(r, c, QTableWidgetItem(val))
