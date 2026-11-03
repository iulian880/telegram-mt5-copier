"""OPTIONAL end-to-end test against a real IC Markets SC DEMO account.

Never runs by default.  Requirements:
  * Windows with the MT5 terminal installed and logged in to a DEMO account, Algo Trading enabled
  * pip install MetaTrader5
  * explicit approval:  set TGMT5_DEMO_E2E=I_UNDERSTAND_THIS_PLACES_DEMO_ORDERS
  * TGMT5_MT5_LOGIN, TGMT5_MT5_SERVER (e.g. ICMarketsSC-Demo), TGMT5_MT5_PASSWORD
    (password only via environment for this test; the app itself uses the keyring)

Run:  pytest -m demo_e2e -s tests/test_demo_e2e.py

It places ONE minimum-volume EURUSD market order through the full pipeline (parser ->
risk engine -> DEMO guard -> MT5), verifies the position and its SL/TP, then closes it.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.demo_e2e

APPROVAL = "I_UNDERSTAND_THIS_PLACES_DEMO_ORDERS"


@pytest.fixture(scope="module")
def demo_env():
    if sys.platform != "win32":
        pytest.skip("MetaTrader5 package is Windows-only")
    if os.environ.get("TGMT5_DEMO_E2E") != APPROVAL:
        pytest.skip(f"set TGMT5_DEMO_E2E={APPROVAL} to approve placing demo orders")
    try:
        return int(os.environ["TGMT5_MT5_LOGIN"]), os.environ["TGMT5_MT5_SERVER"], \
            os.environ["TGMT5_MT5_PASSWORD"]
    except KeyError as e:
        pytest.skip(f"missing {e}")


def test_demo_round_trip(demo_env, tmp_path):
    from tgmt5.config import AppConfig, SizingMode, SourceConfig
    from tgmt5.domain.models import ExecutionMode, SignalStatus, TelegramMessage
    from tgmt5.execution.engine import ExecutionEngine
    from tgmt5.execution.management import close_leg
    from tgmt5.mt5.guard import DemoOnlyGateway
    from tgmt5.mt5.interface import ConnectParams
    from tgmt5.mt5.real import RealMT5Gateway
    from tgmt5.persistence.db import Database
    from tgmt5.persistence.models import SignalRow
    from tgmt5.risk.engine import RiskEngine

    login, server, password = demo_env
    gw = DemoOnlyGateway(RealMT5Gateway(), login, server)
    acct = gw.connect(ConnectParams(login=login, server=server, password=password))
    assert acct.is_demo, "refusing: not a demo account"
    db = Database.at_path(tmp_path / "e2e.db")
    cfg = AppConfig(sources=[SourceConfig(chat_id=1, title="e2e")])
    cfg.risk.sizing_mode = SizingMode.FIXED_LOT
    cfg.risk.fixed_lot = 0.01
    cfg.execution.cross_source_dedup_seconds = 0
    risk = RiskEngine(lambda: cfg.risk, lambda: None, lambda v: None)
    risk.state.confirmed_account = f"{acct.login}@{acct.server}"
    eng = ExecutionEngine(db, gw, risk, lambda: cfg, mode=ExecutionMode.DEMO)
    sym = eng.resolver.resolve("EURUSD")
    t = gw.tick(sym)
    text = f"BUY EURUSD NOW\nSL {t.bid - 0.0050:.5f}\nTP {t.ask + 0.0050:.5f}"
    now = datetime.now(timezone.utc)
    sid = eng.handle_message(TelegramMessage(1, 1, text, now, received_at=now))
    with db.session() as s:
        sig = s.get(SignalRow, sid)
        assert sig.status == SignalStatus.EXECUTED.value, sig.reject_reason
        leg = sig.executions[0]
        pos = [p for p in gw.positions(sym) if p.ticket == leg.position_ticket]
        assert pos and pos[0].sl > 0 and pos[0].tp > 0 and pos[0].volume == 0.01
        ok, msg = close_leg(eng, s, leg, leg.volume, "e2e cleanup")
        assert ok, msg
    gw.disconnect()
