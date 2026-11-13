"""Application-service integration tests (asyncio + worker thread, mocked MT5 & Telegram)."""
import asyncio
import json

import pytest

from tgmt5.analytics.stats import compute, export_csv
from tgmt5.config import AppConfig, MT5Settings, SourceConfig
from tgmt5.domain.models import ExecutionMode, SignalStatus
from tgmt5.logging_setup import redact, register_secret
from tgmt5.mt5.interface import ACCOUNT_TRADE_MODE_REAL, LiveAccountBlocked
from tgmt5.persistence.db import Database
from tgmt5.persistence.models import SignalRow
from tgmt5.secrets import MemoryKeyring, SecretStore
from tgmt5.services.app import AppService
from tgmt5.telegram.client import FakeTelegramSource
from tests.conftest import SOURCE, make_broker


@pytest.fixture
async def svc(tmp_path):
    broker = make_broker()
    tg = FakeTelegramSource()
    secrets = SecretStore(MemoryKeyring())
    db = Database.at_path(tmp_path / "t.db")       # real file DB + Alembic migrations
    s = AppService(tmp_path, secrets=secrets, telegram=tg, gateway_factory=lambda: broker, db=db)
    s.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                         mt5=MT5Settings(login=broker.login, server=broker.server))
    s.config.mark_all_reviewed()       # pre-trade review done (tested separately)
    await s.start()
    s.broker, s.tg = broker, tg
    yield s
    await s.stop()


async def drain(s):
    await s.queue.join()


def statuses(s):
    with s.db.session() as ss:
        return [r.status for r in ss.query(SignalRow).order_by(SignalRow.id)]


GOLD = "BUY XAUUSD NOW SL 2650 TP 2690"


async def test_starts_in_monitor_and_never_trades(svc):
    await svc.connect_mt5("pw")
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    assert statuses(svc) == ["MONITORED"] and svc.broker.requests == []


async def test_demo_requires_explicit_confirmation(svc):
    await svc.connect_mt5("pw")
    with pytest.raises(ValueError, match="confirmed"):
        await svc.set_mode(ExecutionMode.DEMO)
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    assert statuses(svc) == ["EXECUTED"]


async def test_demo_requires_connection(svc):
    with pytest.raises(ValueError, match="Connect MT5"):
        await svc.set_mode(ExecutionMode.DEMO)


async def test_live_account_connection_refused(svc):
    svc.broker.trade_mode = ACCOUNT_TRADE_MODE_REAL
    with pytest.raises(LiveAccountBlocked):
        await svc.connect_mt5("pw")
    assert svc.real_gw is None
    with pytest.raises(ValueError):
        await svc.set_mode(ExecutionMode.DEMO)


async def test_wrong_account_refused(svc):
    svc.config.mt5.login = 123
    with pytest.raises(LiveAccountBlocked, match="does not match"):
        await svc.connect_mt5("pw")


async def test_account_switched_to_live_after_connect_blocks_orders(svc):
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    svc.broker.trade_mode = ACCOUNT_TRADE_MODE_REAL      # terminal re-logged to a live account
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    assert svc.broker.positions() == [] and svc.risk.state.emergency_stop


async def test_emergency_stop_switches_to_monitor_and_persists(svc, tmp_path):
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await svc.emergency_stop()
    assert svc.engine.mode is ExecutionMode.MONITOR
    with pytest.raises(ValueError, match="Emergency"):
        await svc.set_mode(ExecutionMode.DEMO)
    raw = svc.db.get_setting("risk_state")
    assert raw["emergency_stop"] is True


async def test_flatten_all_only_touches_app_positions(svc):
    from tgmt5.mt5.interface import TRADE_ACTION_DEAL, TradeRequest
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    svc.broker.send(TradeRequest(action=TRADE_ACTION_DEAL, symbol="EURUSD", volume=0.01, type=0,
                                 magic=0, comment="user manual trade"))
    await svc.tg.push(SOURCE, "BUY XAUUSD NOW SL 2650 TP1 2670 TP2 2680")
    await drain(svc)
    assert len(svc.broker.positions()) == 3
    await svc.flatten_all()
    left = svc.broker.positions()
    assert len(left) == 1 and left[0].comment == "user manual trade"


async def test_paper_mode_is_separate_from_demo(svc):
    await svc.connect_mt5("pw")
    await svc.set_mode(ExecutionMode.PAPER)
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    assert statuses(svc) == ["SIMULATED"]
    assert svc.broker.requests == []                  # nothing sent to the real (demo) broker
    assert len(svc.paper_gw.positions()) == 1
    with svc.db.session() as s:
        assert s.query(SignalRow).one().is_simulated


async def test_paper_mode_models_costs(svc):
    svc.config.paper.commission_per_lot = 10
    svc.config.paper.slippage_points = 20
    await svc.connect_mt5("pw")
    await svc.set_mode(ExecutionMode.PAPER)
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    p = svc.paper_gw.positions()[0]
    assert p.price_open == pytest.approx(2660.20 + 0.20)
    assert svc.paper_gw.balance < svc.config.paper.starting_balance


async def test_concurrent_signals_processed_serially_within_limits(svc):
    svc.config.risk.max_concurrent_trades = 2
    svc.config.execution.cross_source_dedup_seconds = 0
    svc.config.mark_all_reviewed()     # a changed critical field must be ticked again
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await asyncio.gather(*[svc.tg.push(SOURCE, f"BUY XAUUSD NOW SL {2640 + i} TP 2690")
                           for i in range(6)])
    await drain(svc)
    st = statuses(svc)
    assert st.count("EXECUTED") == 2 and st.count("REJECTED") == 4


async def test_bounded_queue_drops_rather_than_blocks(svc):
    svc.queue = asyncio.Queue(maxsize=1)
    await svc.on_telegram_message(object())  # type: ignore[arg-type]
    await svc.on_telegram_message(object())  # type: ignore[arg-type]
    assert svc.queue.qsize() == 1


async def test_mt5_disconnect_pauses_execution_and_reconnects(svc):
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    svc.broker.connected = False
    for _ in range(3):
        await svc.run_mt5(svc._health_check)
    # the 3rd failure triggers a reconnect attempt, which succeeds on the fake broker
    assert svc.broker.connected
    assert svc.risk.mt5_unstable              # stays paused until stable for 5 checks
    for _ in range(5):
        await svc.run_mt5(svc._health_check)
    assert not svc.risk.mt5_unstable


async def test_review_queue_approve_runs_full_validation(svc):
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await svc.tg.push(SOURCE, "BUY XAUUSD NOW SL 2650")         # no TP -> review
    await drain(svc)
    assert statuses(svc) == ["MANUAL_REVIEW"]
    svc.config.execution.require_take_profit = False
    with svc.db.session() as s:
        sid = s.query(SignalRow).one().id
    st = await svc.resolve_review(sid, approve=True)
    assert st is SignalStatus.EXECUTED


async def test_review_dismiss(svc):
    await svc.tg.push(SOURCE, "BUY GOLD now")
    await drain(svc)
    with svc.db.session() as s:
        sid = s.query(SignalRow).one().id
    assert await svc.resolve_review(sid, approve=False) is SignalStatus.CANCELLED


async def test_telegram_notifications(svc):
    svc.config.notifications.telegram_destination = "me"
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    await asyncio.sleep(0.05)
    assert svc.tg.sent and svc.tg.sent[0][0] == "me"


async def test_snapshot_and_analytics(svc):
    await svc.connect_mt5("pw")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await svc.tg.push(SOURCE, GOLD)
    await drain(svc)
    snap = await svc.snapshot()
    assert snap.mode == "DEMO" and len(snap.positions) == 1 and snap.positions[0]["signal_id"]
    assert snap.open_risk > 0 and snap.executed_today == 1
    svc.broker.set_price("XAUUSD", 2691, 2691.2)       # TP hit
    await svc.run_mt5(svc._tick)
    st = compute(svc.db, simulated=False)
    assert st.trades == 1 and st.wins == 1 and st.net > 0 and st.avg_r > 0
    assert "DEMO" in export_csv(svc.db, simulated=False)
    assert compute(svc.db, simulated=True).trades == 0


async def test_config_saved_never_persists_execution_enabled(svc):
    svc.config.mode = ExecutionMode.DEMO
    await svc.save_config(svc.config)
    assert svc.db.load_config().mode is ExecutionMode.MONITOR
    exported = json.loads(svc.config.export_json())
    assert "mode" not in exported and "password" not in json.dumps(exported).lower()


async def test_secrets_never_in_logs_or_db(svc):
    secret = "my-super-secret-api-hash-123"
    svc.secrets.set(SecretStore.TG_API_HASH, secret)
    assert redact(f"connecting with {secret}") == "connecting with ***"
    assert "***" in redact("password=hunter2")
    register_secret("pw-xyz-123")
    svc.engine.audit_app("INFO", "test", "login pw-xyz-123 failed")
    assert all("pw-xyz-123" not in e.message for e in svc.db.recent_events())
