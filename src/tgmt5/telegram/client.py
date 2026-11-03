"""Telegram (MTProto user session) adapter built on Telethon.

* Uses the user's own authorised session, so it only sees chats the user is a
  legitimate member of.  It never tries to join or bypass anything.
* Event driven (``NewMessage`` / ``MessageEdited`` / ``MessageDeleted``), no polling.
* Telethon reconnects automatically; we expose status + latency and never
  replay missed messages blindly (``catch_up`` is off; signal-age validation
  rejects anything stale that does arrive).
* The session string and API hash live in the OS keyring only.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from tgmt5.domain.models import MessageEventType, TelegramMessage
from tgmt5.secrets import SecretStore

log = logging.getLogger(__name__)
OnMessage = Callable[[TelegramMessage], Awaitable[None]]


@dataclass
class DialogInfo:
    id: int
    title: str
    kind: str          # "group" | "channel" | "user"


@dataclass
class TelegramStatus:
    connected: bool = False
    authorized: bool = False
    user: str = ""
    latency_ms: float | None = None
    last_message_at: datetime | None = None
    reconnects: int = 0
    messages_received: int = 0
    avg_delivery_ms: float | None = None
    _delivery: list[float] = field(default_factory=list, repr=False)

    def record_delivery(self, ms: float) -> None:
        self._delivery = (self._delivery + [ms])[-200:]
        self.avg_delivery_ms = sum(self._delivery) / len(self._delivery)


class TelegramSource(Protocol):
    status: TelegramStatus

    async def start(self, chat_ids: list[int], on_message: OnMessage) -> None: ...
    async def stop(self) -> None: ...
    async def list_dialogs(self) -> list[DialogInfo]: ...
    async def send_notification(self, destination: str, text: str) -> None: ...
    async def update_chats(self, chat_ids: list[int]) -> None: ...


class LoginRequired(RuntimeError):
    pass


class TelethonSource:
    def __init__(self, secrets: SecretStore) -> None:
        self.secrets = secrets
        self.status = TelegramStatus()
        self.client = None
        self._chat_ids: set[int] = set()
        self._on_message: OnMessage | None = None
        self._phone_code_hash: str | None = None
        self._watch_task: asyncio.Task | None = None

    # ------------------------------------------------------------- auth
    def _build_client(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        api_id = self.secrets.get(SecretStore.TG_API_ID)
        api_hash = self.secrets.get(SecretStore.TG_API_HASH)
        if not api_id or not api_hash:
            raise LoginRequired("Telegram API ID / API hash not configured")
        session = self.secrets.get(SecretStore.TG_SESSION) or ""
        return TelegramClient(StringSession(session), int(api_id), api_hash,
                              auto_reconnect=True, connection_retries=None, retry_delay=3,
                              request_retries=3, catch_up=False,
                              device_model="TGMT5 Desktop", app_version="0.1")

    async def connect(self) -> bool:
        if self.client is None:
            self.client = self._build_client()
        if not self.client.is_connected():
            await self.client.connect()
        self.status.connected = self.client.is_connected()
        self.status.authorized = await self.client.is_user_authorized()
        if self.status.authorized:
            me = await self.client.get_me()
            self.status.user = (me.username or me.first_name or "") if me else ""
        return self.status.authorized

    async def send_code(self, phone: str) -> None:
        await self.connect()
        sent = await self.client.send_code_request(phone)
        self._phone_code_hash = sent.phone_code_hash

    async def sign_in(self, phone: str, code: str, password: str | None = None) -> str:
        """Returns "ok" or "password_required" (2FA)."""
        from telethon.errors import SessionPasswordNeededError
        await self.connect()
        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=self._phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                return "password_required"
            await self.client.sign_in(password=password)
        self._persist_session()
        self.status.authorized = True
        return "ok"

    async def sign_in_password(self, password: str) -> str:
        await self.client.sign_in(password=password)
        self._persist_session()
        self.status.authorized = True
        return "ok"

    def _persist_session(self) -> None:
        self.secrets.set(SecretStore.TG_SESSION, self.client.session.save())

    async def logout(self) -> None:
        if self.client:
            await self.client.log_out()
        self.secrets.delete(SecretStore.TG_SESSION)
        self.status = TelegramStatus()

    # ------------------------------------------------------------- events
    async def start(self, chat_ids: list[int], on_message: OnMessage) -> None:
        from telethon import events
        if not await self.connect():
            raise LoginRequired("Telegram session not authorised - log in from the Sources screen")
        self._on_message = on_message
        self._chat_ids = set(chat_ids)
        c = self.client
        c.add_event_handler(self._on_new, events.NewMessage())
        c.add_event_handler(self._on_edit, events.MessageEdited())
        c.add_event_handler(self._on_deleted, events.MessageDeleted())
        self._watch_task = asyncio.create_task(self._watchdog())
        log.info("Telegram listening on %d chats", len(self._chat_ids))

    async def update_chats(self, chat_ids: list[int]) -> None:
        self._chat_ids = set(chat_ids)

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
        if self.client:
            await self.client.disconnect()
        self.status.connected = False

    def _convert(self, event, kind: MessageEventType) -> TelegramMessage | None:
        if event.chat_id not in self._chat_ids:     # only explicitly selected sources
            return None
        m = event.message
        now = datetime.now(timezone.utc)
        date = m.date if m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
        title = ""
        try:
            title = getattr(event.chat, "title", "") or ""
        except Exception:  # noqa: BLE001
            pass
        return TelegramMessage(source_id=event.chat_id, message_id=m.id, text=m.raw_text or "",
                               date=date, event=kind, reply_to_message_id=m.reply_to_msg_id,
                               edit_date=m.edit_date, source_title=title, received_at=now)

    async def _dispatch(self, msg: TelegramMessage | None) -> None:
        if msg is None or self._on_message is None:
            return
        self.status.messages_received += 1
        self.status.last_message_at = msg.received_at
        if msg.event is MessageEventType.NEW and msg.received_at:
            self.status.record_delivery((msg.received_at - msg.date).total_seconds() * 1000)
        try:
            await self._on_message(msg)
        except Exception:  # noqa: BLE001 - one bad message must not kill the listener
            log.exception("message handler failed")

    async def _on_new(self, event) -> None:
        await self._dispatch(self._convert(event, MessageEventType.NEW))

    async def _on_edit(self, event) -> None:
        await self._dispatch(self._convert(event, MessageEventType.EDIT))

    async def _on_deleted(self, event) -> None:
        # chat_id is only reliable for channels/supergroups; the engine matches by id otherwise
        cid = event.chat_id
        if cid is not None and cid not in self._chat_ids:
            return
        now = datetime.now(timezone.utc)
        for mid in event.deleted_ids:
            await self._dispatch(TelegramMessage(source_id=cid or 0, message_id=mid, text="",
                                                 date=now, event=MessageEventType.DELETE,
                                                 received_at=now))

    async def _watchdog(self) -> None:
        from telethon import functions
        was_connected = True
        while True:
            await asyncio.sleep(15)
            connected = bool(self.client and self.client.is_connected())
            if connected and not was_connected:
                self.status.reconnects += 1
                log.warning("Telegram reconnected")
            if not connected and was_connected:
                log.warning("Telegram disconnected - Telethon is reconnecting")
            was_connected = self.status.connected = connected
            if connected:
                try:
                    t0 = time.perf_counter()
                    await asyncio.wait_for(
                        self.client(functions.PingRequest(ping_id=random.getrandbits(63))), 10)
                    self.status.latency_ms = (time.perf_counter() - t0) * 1000
                except Exception as e:  # noqa: BLE001
                    log.warning("Telegram ping failed: %s", type(e).__name__)
                    self.status.latency_ms = None

    # ------------------------------------------------------------- misc
    async def list_dialogs(self) -> list[DialogInfo]:
        await self.connect()
        out = []
        async for d in self.client.iter_dialogs():
            if d.is_group or d.is_channel:
                out.append(DialogInfo(id=d.id, title=d.name or str(d.id),
                                      kind="channel" if d.is_channel and not d.is_group else "group"))
        return out

    async def history(self, chat_id: int, limit: int = 200) -> list[TelegramMessage]:
        """Read past messages for parser evaluation / replay (never executed)."""
        await self.connect()
        out = []
        async for m in self.client.iter_messages(chat_id, limit=limit):
            date = m.date if m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
            out.append(TelegramMessage(source_id=chat_id, message_id=m.id, text=m.raw_text or "",
                                       date=date, reply_to_message_id=m.reply_to_msg_id))
        return list(reversed(out))

    async def send_notification(self, destination: str, text: str) -> None:
        if self.client and self.status.authorized and destination:
            await self.client.send_message(destination, text)


class FakeTelegramSource:
    """Scriptable source for tests and the sandbox mode."""

    def __init__(self) -> None:
        self.status = TelegramStatus(connected=True, authorized=True, user="sandbox")
        self._on: OnMessage | None = None
        self.chat_ids: set[int] = set()
        self.sent: list[tuple[str, str]] = []
        self._next_id = 1

    async def start(self, chat_ids: list[int], on_message: OnMessage) -> None:
        self.chat_ids = set(chat_ids)
        self._on = on_message

    async def update_chats(self, chat_ids: list[int]) -> None:
        self.chat_ids = set(chat_ids)

    async def stop(self) -> None:
        self.status.connected = False

    async def list_dialogs(self) -> list[DialogInfo]:
        return [DialogInfo(id=c, title=f"Chat {c}", kind="group") for c in sorted(self.chat_ids)]

    async def send_notification(self, destination: str, text: str) -> None:
        self.sent.append((destination, text))

    async def push(self, chat_id: int, text: str, *, reply_to: int | None = None,
                   event: MessageEventType = MessageEventType.NEW, message_id: int | None = None,
                   title: str = "") -> int:
        mid = message_id if message_id is not None else self._next_id
        self._next_id = max(self._next_id, mid) + 1
        if chat_id not in self.chat_ids or self._on is None:
            return mid
        now = datetime.now(timezone.utc)
        self.status.messages_received += 1
        self.status.last_message_at = now
        await self._on(TelegramMessage(source_id=chat_id, message_id=mid, text=text, date=now,
                                       event=event, reply_to_message_id=reply_to,
                                       source_title=title, received_at=now))
        return mid
