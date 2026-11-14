"""Regression tests for the bugs fixed in v0.4.0 (found in the November 2026 review)."""
from __future__ import annotations

from tgmt5.config import AppConfig, SourceConfig
from tgmt5.domain.models import MessageEventType, SignalStatus

from tests.conftest import SOURCE, Harness

AMBIGUOUS_GOLD = "BUY XAUUSD SL 2650 TP 2670"       # no entry condition -> manual review
GOLD3 = "BUY XAUUSD NOW\nSL: 2650\nTP1: 2670\nTP2: 2680\nTP3: 2690"


# ---- chat text must not close trades ------------------------------------------------------
def test_chat_message_does_not_close_open_trade():
    h = Harness()
    sid = h.send(GOLD3, msg_id=10)
    assert h.signal(sid).status == SignalStatus.EXECUTED.value
    n = len(h.broker.positions())
    h.send("Gold is getting close to TP1 2670 🔥")
    h.send("Keep your SL, be patient guys")
    h.send("Take only 1% risk on this one")
    assert len(h.broker.positions()) == n
    assert all(p.volume > 0 for p in h.broker.positions())


# ---- delete events without a chat id -------------------------------------------------------
def test_delete_without_chat_id_ignores_channel_sources():
    h = Harness()
    sid = h.send(AMBIGUOUS_GOLD, msg_id=500)
    assert h.signal(sid).status == SignalStatus.MANUAL_REVIEW.value
    # Telegram sends no chat id for deletions in private chats / basic groups; message ids there
    # are a separate sequence from channel ids, so this must not touch the channel's signal
    h.send("", msg_id=500, source=0, event=MessageEventType.DELETE)
    assert h.signal(sid).status == SignalStatus.MANUAL_REVIEW.value


def test_delete_without_chat_id_still_matches_basic_group_source():
    group = -4512345                                      # basic group: shares the account id space
    cfg = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP"),
                             SourceConfig(chat_id=group, title="Small group")])
    h = Harness(cfg=cfg)
    sid = h.send(AMBIGUOUS_GOLD, msg_id=777, source=group)
    assert h.signal(sid).status == SignalStatus.MANUAL_REVIEW.value
    h.send("", msg_id=777, source=0, event=MessageEventType.DELETE)
    assert h.signal(sid).status == SignalStatus.CANCELLED.value


# ---- PAPER mode after disconnect + reconnect --------------------------------------------
async def test_paper_mode_works_after_reconnect(tmp_path):
    from tests.conftest import make_broker
    from tgmt5.config import MT5Settings
    from tgmt5.domain.models import ExecutionMode
    from tgmt5.persistence.db import Database
    from tgmt5.persistence.models import SignalRow
    from tgmt5.secrets import MemoryKeyring, SecretStore
    from tgmt5.services.app import AppService
    from tgmt5.telegram.client import FakeTelegramSource

    tg = FakeTelegramSource()
    svc = AppService(tmp_path, secrets=SecretStore(MemoryKeyring()), telegram=tg,
                     gateway_factory=make_broker,          # a new adapter per connect, like the real one
                     db=Database.at_path(tmp_path / "t.db"))
    svc.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                           mt5=MT5Settings(login=50001234, server="ICMarketsSC-Demo", auto_connect=False))
    svc.config.mark_all_reviewed()
    svc.config.execution.cross_source_dedup_seconds = 0
    await svc.start()
    try:
        await svc.connect_mt5("pw")
        await svc.set_mode(ExecutionMode.PAPER)
        await svc.disconnect_mt5()
        await svc.connect_mt5("pw")
        await svc.set_mode(ExecutionMode.PAPER)
        await tg.push(SOURCE, "BUY XAUUSD NOW SL 2648 TP 2690")
        await svc.queue.join()
        with svc.db.session() as s:
            sig = s.query(SignalRow).order_by(SignalRow.id.desc()).first()
            assert sig.status == SignalStatus.SIMULATED.value, (sig.status, sig.reject_reason)
    finally:
        await svc.stop()


# ---- large accounts: legs are capped at "max lot per position", not rejected ------------
def test_large_account_caps_each_leg_instead_of_rejecting():
    from tests.conftest import make_broker
    h = Harness(broker=make_broker(balance=1_000_000))
    h.engine.refresh_account()
    sid = h.send(GOLD3)
    sig = h.signal(sid)
    assert sig.status == SignalStatus.EXECUTED.value, sig.reject_reason
    cap = h.cfg.risk.max_lot_per_order
    vols = [l.volume for l in sig.executions]
    assert vols and all(v <= cap + 1e-9 for v in vols), vols
    assert vols[0] == cap                               # the biggest (front-loaded) leg hits the cap
