"""Account-type enforcement wrapper.

:class:`AccountGuard` wraps any gateway and re-validates, *immediately before every
trade request*, that the connected account is the account the user configured and that
its type is allowed:

* DEMO accounts are always allowed.
* Real-money accounts are refused unless the policy callbacks allow them. The service
  allows connecting to one only when the user switched on "Allow real-money accounts",
  and allows NEW orders on it only in REAL ACCOUNT mode after the user confirmed that
  exact account. Risk-reducing requests (close a position, remove a pending order,
  change SL/TP) stay possible on a confirmed real account in any mode, so positions can
  always be managed or closed.

This is the backend-level block; the GUI controls are on top of it.
"""
from __future__ import annotations

import logging
from typing import Callable

from tgmt5.mt5.interface import (
    TRADE_ACTION_DEAL, TRADE_ACTION_REMOVE, TRADE_ACTION_SLTP, AccountInfo, ConnectParams,
    LiveAccountBlocked, MT5Gateway, TradeRequest, TradeResult,
)

log = logging.getLogger(__name__)


def is_risk_reducing(req: TradeRequest) -> bool:
    """Close an existing position, remove a pending order, or change SL/TP."""
    if req.action in (TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE):
        return True
    return req.action == TRADE_ACTION_DEAL and bool(req.position)


def check_account(acct: AccountInfo | None, expected_login: int | None, expected_server: str,
                  *, allow_real: bool = False) -> None:
    if acct is None:
        raise LiveAccountBlocked("No account information available - refusing to trade")
    if not acct.is_demo and not allow_real:
        raise LiveAccountBlocked(
            f"Account {acct.login} on {acct.server} is NOT a demo account "
            f"(trade_mode={acct.trade_mode}). Real-money trading is off: switch on 'Allow real-money "
            f"accounts' in Connection and use REAL ACCOUNT mode to trade it.")
    if expected_login is not None and acct.login != expected_login:
        raise LiveAccountBlocked(
            f"Connected account {acct.login} does not match configured account {expected_login}")
    if expected_server and acct.server.lower() != expected_server.lower():
        raise LiveAccountBlocked(
            f"Connected server {acct.server!r} does not match configured server {expected_server!r}")


def assert_demo(acct: AccountInfo | None, expected_login: int | None, expected_server: str) -> None:
    check_account(acct, expected_login, expected_server, allow_real=False)


class AccountGuard:
    def __init__(self, inner: MT5Gateway, expected_login: int | None, expected_server: str, *,
                 allow_real_connect: Callable[[], bool] = lambda: False,
                 allow_real_send: Callable[[AccountInfo, TradeRequest], bool] = lambda a, r: False) -> None:
        self._inner = inner
        self.expected_login = expected_login
        self.expected_server = expected_server
        self._allow_real_connect = allow_real_connect
        self._allow_real_send = allow_real_send

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    @property
    def inner(self) -> MT5Gateway:
        return self._inner

    def connect(self, params: ConnectParams) -> AccountInfo:
        acct = self._inner.connect(params)
        try:
            check_account(acct, self.expected_login, self.expected_server,
                          allow_real=self._allow_real_connect())
        except LiveAccountBlocked:
            self._inner.disconnect()
            raise
        return acct

    def send(self, request: TradeRequest) -> TradeResult:
        acct = self._inner.account_info()
        real_ok = acct is not None and not acct.is_demo and self._allow_real_send(acct, request)
        check_account(acct, self.expected_login, self.expected_server, allow_real=real_ok)
        return self._inner.send(request)


#: backwards-compatible name: with the default policies it only ever allows DEMO accounts
DemoOnlyGateway = AccountGuard
