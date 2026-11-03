"""Telethon adapter event handling (Telethon client itself is not contacted)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tgmt5.domain.models import MessageEventType
from tgmt5.secrets import MemoryKeyring, SecretStore
from tgmt5.telegram.client import LoginRequired, TelethonSource


def ev(chat_id, mid, text, reply=None):
    msg = SimpleNamespace(id=mid, raw_text=text, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                          reply_to_msg_id=reply, edit_date=None)
    return SimpleNamespace(chat_id=chat_id, message=msg, chat=SimpleNamespace(title="VIP"))


@pytest.fixture
def src():
    s = TelethonSource(SecretStore(MemoryKeyring()))
    s.got = []

    async def on(m):
        s.got.append(m)
    s._on_message = on
    s._chat_ids = {-100}
    return s


async def test_only_selected_chats_are_forwarded(src):
    await src._on_new(ev(-100, 1, "BUY XAUUSD NOW SL 1 TP 2", reply=None))
    await src._on_new(ev(-999, 2, "BUY XAUUSD NOW SL 1 TP 2"))
    assert [m.message_id for m in src.got] == [1]
    m = src.got[0]
    assert m.source_title == "VIP" and m.event is MessageEventType.NEW and m.received_at


async def test_edit_and_reply_preserved(src):
    await src._on_edit(ev(-100, 5, "Move SL to BE", reply=3))
    m = src.got[0]
    assert m.event is MessageEventType.EDIT and m.reply_to_message_id == 3


async def test_deletions(src):
    await src._on_deleted(SimpleNamespace(chat_id=-100, deleted_ids=[7, 8]))
    await src._on_deleted(SimpleNamespace(chat_id=-555, deleted_ids=[9]))
    await src._on_deleted(SimpleNamespace(chat_id=None, deleted_ids=[10]))   # basic group
    assert [(m.message_id, m.event) for m in src.got] == [
        (7, MessageEventType.DELETE), (8, MessageEventType.DELETE), (10, MessageEventType.DELETE)]


async def test_handler_exception_does_not_kill_listener(src):
    async def boom(m):
        raise RuntimeError("x")
    src._on_message = boom
    await src._on_new(ev(-100, 1, "hi"))      # must not raise
    assert src.status.messages_received == 1


async def test_missing_credentials_requires_login(src):
    with pytest.raises(LoginRequired):
        src._build_client()


def test_client_built_with_safe_options():
    sec = SecretStore(MemoryKeyring())
    sec.set(SecretStore.TG_API_ID, "12345")
    sec.set(SecretStore.TG_API_HASH, "0123456789abcdef0123456789abcdef")
    c = TelethonSource(sec)._build_client()
    assert c._catch_up is False and c._auto_reconnect is True
