"""DEMO-only enforcement wrapper.

:class:`DemoOnlyGateway` wraps any gateway and re-validates, *immediately
before every trade request*, that the connected account is a DEMO account and
that it is the account the user configured.  This is the backend-level block
on live trading; the GUI block is cosmetic on top of it.
"""
from __future__ import annotations

import logging

from tgmt5.mt5.interface import (
    AccountInfo, ConnectParams, LiveAccountBlocked, MT5Gateway, TradeRequest, TradeResult,
)

log = logging.getLogger(__name__)


def assert_demo(acct: AccountInfo | None, expected_login: int | None, expected_server: str) -> None:
    if acct is None:
        raise LiveAccountBlocked("No account information available - refusing to trade")
    if not acct.is_demo:
        raise LiveAccountBlocked(
            f"Account {acct.login} on {acct.server} is NOT a demo account "
            f"(trade_mode={acct.trade_mode}). Live trading is disabled in this application.")
    if expected_login is not None and acct.login != expected_login:
        raise LiveAccountBlocked(
            f"Connected account {acct.login} does not match configured account {expected_login}")
    if expected_server and acct.server.lower() != expected_server.lower():
        raise LiveAccountBlocked(
            f"Connected server {acct.server!r} does not match configured server {expected_server!r}")


class DemoOnlyGateway:
    def __init__(self, inner: MT5Gateway, expected_login: int | None, expected_server: str) -> None:
        self._inner = inner
        self.expected_login = expected_login
        self.expected_server = expected_server

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    @property
    def inner(self) -> MT5Gateway:
        return self._inner

    def connect(self, params: ConnectParams) -> AccountInfo:
        acct = self._inner.connect(params)
        try:
            assert_demo(acct, self.expected_login, self.expected_server)
        except LiveAccountBlocked:
            self._inner.disconnect()
            raise
        return acct

    def send(self, request: TradeRequest) -> TradeResult:
        assert_demo(self._inner.account_info(), self.expected_login, self.expected_server)
        return self._inner.send(request)
