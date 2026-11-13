"""REAL ACCOUNT mode: opt-in, typed confirmation, backend guard on every order."""
import pytest

from tgmt5.config import AppConfig, MT5Settings, SourceConfig
from tgmt5.domain.models import ExecutionMode
from tgmt5.mt5.guard import AccountGuard, is_risk_reducing
from tgmt5.mt5.interface import (
    ACCOUNT_TRADE_MODE_REAL, TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, LiveAccountBlocked, TradeRequest,
)
from tgmt5.persistence.db import Database
from tgmt5.persistence.models import SignalRow
from tgmt5.secrets import MemoryKeyring, SecretStore
from tgmt5.services.app import AppService
from tgmt5.telegram.client import FakeTelegramSource
from tests.conftest import SOURCE, make_broker

SIGNAL = "BUY XAUUSD NOW SL 2650 TP 2690"


@pytest.fixture
async def real(tmp_path):
    broker = make_broker()
    broker.trade_mode = ACCOUNT_TRADE_MODE_REAL
    tg = FakeTelegramSource()
    s = AppService(tmp_path, secrets=SecretStore(MemoryKeyring()), telegram=tg,
                   gateway_factory=lambda: broker, db=Database.at_path(tmp_path / "t.db"))
    s.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                         mt5=MT5Settings(login=broker.login, server=broker.server))
    s.config.mark_all_reviewed()
    await s.start()
    s.broker, s.tg = broker, tg
    yield s
    await s.stop()


async def allow(svc):
    cfg = svc.config.model_copy(deep=True)
    cfg.mt5.allow_real_account = True
    await svc.save_config(cfg)


def test_guard_blocks_real_accounts_by_default():
    b = make_broker()
    b.trade_mode = ACCOUNT_TRADE_MODE_REAL
    g = AccountGuard(b, b.login, b.server)
    with pytest.raises(LiveAccountBlocked, match="NOT a demo account"):
        g.send(TradeRequest(action=TRADE_ACTION_DEAL, symbol="EURUSD", volume=0.01))


def test_risk_reducing_requests():
    assert is_risk_reducing(TradeRequest(action=TRADE_ACTION_SLTP, position=5))
    assert is_risk_reducing(TradeRequest(action=TRADE_ACTION_DEAL, position=5))      # close
    assert not is_risk_reducing(TradeRequest(action=TRADE_ACTION_DEAL))               # new order


async def test_real_account_refused_unless_allowed(real):
    with pytest.raises(LiveAccountBlocked, match="Allow real-money"):
        await real.connect_mt5("pw")
    await allow(real)
    acct = await real.connect_mt5("pw")
    assert not acct.is_demo and real.engine.mode is ExecutionMode.MONITOR


async def test_real_mode_needs_typed_confirmation_then_trades(real):
    await allow(real)
    await real.connect_mt5("pw")
    with pytest.raises(LiveAccountBlocked):
        await real.set_mode(ExecutionMode.DEMO)                 # DEMO mode never trades a real account
    with pytest.raises(ValueError, match="not been confirmed"):
        await real.set_mode(ExecutionMode.LIVE)
    with pytest.raises(ValueError, match="does not match"):
        await real.confirm_real_account("12345")
    await real.confirm_real_account(str(real.broker.login))
    await real.set_mode(ExecutionMode.LIVE)
    await real.tg.push(SOURCE, SIGNAL)
    await real.queue.join()
    with real.db.session() as s:
        assert [r.status for r in s.query(SignalRow)] == ["EXECUTED"]
    assert len(real.broker.positions()) == 1


async def test_real_mode_still_requires_the_pre_trade_review(real):
    await allow(real)
    await real.connect_mt5("pw")
    await real.confirm_real_account(str(real.broker.login))
    real.config.risk.max_concurrent_trades = 9
    with pytest.raises(ValueError, match="Review the red risk settings"):
        await real.set_mode(ExecutionMode.LIVE)


async def test_switching_real_off_stops_trading_but_positions_can_be_closed(real):
    await allow(real)
    await real.connect_mt5("pw")
    await real.confirm_real_account(str(real.broker.login))
    await real.set_mode(ExecutionMode.LIVE)
    await real.tg.push(SOURCE, SIGNAL)
    await real.queue.join()
    await real.set_mode(ExecutionMode.MONITOR)
    # outside REAL mode: closing the open position is still possible (risk-reducing)
    from tgmt5.persistence.models import ExecutionRow
    with real.db.session() as s:
        leg_id = s.query(ExecutionRow).first().id
    ok, msg = await real.close_position(leg_id)
    assert ok, msg
    assert real.broker.positions() == []
    # turning the option off clears the confirmation and blocks REAL mode
    cfg = real.config.model_copy(deep=True)
    cfg.mt5.allow_real_account = False
    await real.save_config(cfg)
    assert real.risk.state.confirmed_real_account == ""
    with pytest.raises(LiveAccountBlocked):
        await real.set_mode(ExecutionMode.LIVE)


async def test_new_orders_blocked_outside_real_mode(real):
    await allow(real)
    await real.connect_mt5("pw")
    await real.confirm_real_account(str(real.broker.login))
    with pytest.raises(LiveAccountBlocked):
        real.real_gw.send(TradeRequest(action=TRADE_ACTION_DEAL, symbol="EURUSD", volume=0.01))


def test_real_trading_is_never_imported():
    cfg = AppConfig()
    cfg.mt5.allow_real_account = True
    assert AppConfig.import_json(cfg.export_json()).mt5.allow_real_account is False


async def test_real_mode_not_available_in_sandbox(tmp_path):
    from tgmt5.__main__ import build_service
    svc = build_service(tmp_path, sandbox=True)
    await svc.start()
    try:
        await svc.connect_mt5("x")
        with pytest.raises(ValueError, match="DEMO account|sandbox"):
            await svc.set_mode(ExecutionMode.LIVE)
    finally:
        await svc.stop()
