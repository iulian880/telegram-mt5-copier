"""Application service: wires Telegram, the execution engine, MT5 and the UI together.

Threading model
---------------
* asyncio event loop (shared with Qt through qasync): Telegram I/O, timers, UI calls.
* one dedicated "mt5" worker thread: every MetaTrader5 call, every execution /
  risk / DB write of the trading pipeline.  The official MT5 package is not
  thread-safe, and serialising all trading work also removes races between
  concurrent signals.
* Messages flow Telegram -> bounded asyncio.Queue -> worker thread.

The UI only calls methods on this service; it never touches the gateway, so
it cannot place an order that bypasses the risk engine.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from tgmt5.config import AppConfig
from tgmt5.domain.models import ExecutionMode, MessageKind, SignalStatus, TelegramMessage
from tgmt5.execution.engine import ExecutionEngine
from tgmt5.execution.legs import LegState
from tgmt5.execution.management import cancel_pending_leg, close_leg
from tgmt5.execution.recovery import RecoveryReport, recover
from tgmt5.mt5.guard import AccountGuard, is_risk_reducing
from tgmt5.mt5.interface import (
    AccountInfo, ConnectParams, GatewayError, LiveAccountBlocked, MT5Gateway, TradeRequest,
    TradeResult,
)
from tgmt5.mt5.sim import PaperGateway, _Pos
from tgmt5.persistence.db import Database
from tgmt5.persistence.models import EquitySnapshotRow, ExecutionRow, SignalRow
from tgmt5.domain.models import Direction
from tgmt5.review import REVIEW_LABELS, pending_reviews
from tgmt5.risk.engine import RiskEngine
from tgmt5.risk.preview import DEFAULT_SYMBOLS, ExampleSignal, SizingPreview, build_preview
from tgmt5.secrets import SecretStore
from tgmt5.telegram.client import TelegramSource

log = logging.getLogger(__name__)
Listener = Callable[[str, dict], None]


class ReadOnlyGateway:
    """Market-data-only view used in MONITOR mode: any trade request raises."""

    def __init__(self, inner: MT5Gateway) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def send(self, request: TradeRequest) -> TradeResult:
        raise PermissionError("monitor-only mode: trade requests are disabled")


@dataclass
class Snapshot:
    mode: str
    sandbox: bool
    account: AccountInfo | None
    mt5_connected: bool
    telegram: dict
    blocking_reason: str | None
    emergency: bool
    confirmed: bool
    positions: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    signals_today: int = 0
    executed_today: int = 0
    rejected_today: int = 0
    review_queue: int = 0
    daily_pnl: float = 0.0
    floating_pnl: float = 0.0
    drawdown_pct: float = 0.0
    open_risk: float = 0.0
    open_risk_cap: float = 0.0
    queue_depth: int = 0


class AppService:
    def __init__(self, data_dir: Path, *, secrets: SecretStore, telegram: TelegramSource,
                 gateway_factory: Callable[[], MT5Gateway], sandbox: bool = False,
                 db: Database | None = None) -> None:
        self.data_dir = data_dir
        self.secrets = secrets
        self.telegram = telegram
        self.gateway_factory = gateway_factory
        self.sandbox = sandbox
        self.db = db or Database.at_path(data_dir / "tgmt5.db")
        self.config: AppConfig = self.db.load_config()
        self.risk = RiskEngine(lambda: self.config.risk, lambda: self.db.get_setting("risk_state"),
                               lambda v: self.db.set_setting("risk_state", v))
        self._mt5 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self.engine = ExecutionEngine(self.db, None, self.risk, lambda: self.config,
                                      mode=ExecutionMode.MONITOR, emit=self._emit)
        self.queue: asyncio.Queue[TelegramMessage] = asyncio.Queue(maxsize=1000)
        self.listeners: list[Listener] = []
        self.real_gw: AccountGuard | None = None
        self.paper_gw: PaperGateway | None = None
        self._connect_params: ConnectParams | None = None
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fail_count = 0
        self._ok_count = 0
        self._last_snapshot_eq = datetime.min.replace(tzinfo=timezone.utc)
        self.last_recovery: RecoveryReport | None = None
        self.equity_snapshot_seconds = 60

    # ------------------------------------------------------------- plumbing
    def subscribe(self, fn: Listener) -> None:
        self.listeners.append(fn)

    def _emit(self, kind: str, data: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(kind, data)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")
        if kind == "notify":
            self._maybe_telegram_notify(data)

    def _maybe_telegram_notify(self, data: dict) -> None:
        n = self.config.notifications
        ev = data.get("event")
        wanted = {"executed": n.on_executed, "rejected": n.on_rejected, "closed": n.on_closed}
        if not n.telegram_destination or not wanted.get(ev, False) or self._loop is None:
            return
        text = f"[TGMT5 {self.engine.mode.value}] {data.get('title', '')}\n{data.get('body', '')}"
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._safe_notify(n.telegram_destination, text)))

    async def _safe_notify(self, dest: str, text: str) -> None:
        try:
            await self.telegram.send_notification(dest, text)
        except Exception as e:  # noqa: BLE001
            log.warning("telegram notification failed: %s", type(e).__name__)

    async def run_mt5(self, fn: Callable, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._mt5, fn, *args)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.engine.audit_app("INFO", "app", "Application started in MONITOR mode"
                              + (" (SANDBOX)" if self.sandbox else ""))
        reason = self.risk.blocking_reason()
        if reason:
            self.engine.audit_app("WARNING", "risk", f"Restored lockout state: {reason}")
        self._tasks = [asyncio.create_task(self._consume(), name="consumer"),
                       asyncio.create_task(self._ticker(), name="ticker")]
        await self.start_telegram()

    async def start_telegram(self) -> bool:
        try:
            await self.telegram.start(self.enabled_chat_ids(), self.on_telegram_message)
            self.engine.audit_app("INFO", "telegram", "Telegram listener started")
            return True
        except Exception as e:  # noqa: BLE001
            self.engine.audit_app("WARNING", "telegram", f"Telegram not started: {e}")
            return False

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        try:
            await self.telegram.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.real_gw:
            await self.run_mt5(self.real_gw.disconnect)
        self._mt5.shutdown(wait=True)

    def enabled_chat_ids(self) -> list[int]:
        return [s.chat_id for s in self.config.sources if s.enabled]

    async def on_telegram_message(self, msg: TelegramMessage) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.engine.audit_app("ERROR", "telegram",
                                  f"Signal queue full - message {getattr(msg, 'message_id', '?')} dropped (not executed)")

    async def _consume(self) -> None:
        while True:
            msg = await self.queue.get()
            try:
                await self.run_mt5(self.engine.handle_message, msg)
            except Exception as e:  # noqa: BLE001
                log.exception("pipeline failure")
                self.engine.audit_app("ERROR", "pipeline", f"processing failed: {e}")
            finally:
                self.queue.task_done()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.run_mt5(self._tick)
            except Exception as e:  # noqa: BLE001
                log.exception("tick failed")
                self.engine.audit_app("ERROR", "app", f"periodic task failed: {e}")

    def _tick(self) -> None:
        self._health_check()
        if self.engine.gw is not None:
            self.engine.on_tick()
        now = datetime.now(timezone.utc)
        if now - self._last_snapshot_eq > timedelta(seconds=self.equity_snapshot_seconds):
            self._last_snapshot_eq = now
            self._equity_snapshot()
        self._emit("tick", {})

    def _health_check(self) -> None:
        if self.real_gw is None:
            return
        ok = False
        try:
            ok = self.real_gw.is_connected()
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._ok_count += 1
            self._fail_count = 0
            if self.risk.mt5_unstable and self._ok_count >= 5:
                self.risk.mt5_unstable = False
                self.engine.audit_app("INFO", "mt5", "MT5 connection stable again - execution resumed")
            return
        self._ok_count = 0
        self._fail_count += 1
        if self._fail_count == 3:
            self.risk.mt5_unstable = True
            self.engine.audit_app("ERROR", "mt5", "MT5 connection lost - execution paused")
        if self._fail_count >= 3 and self._fail_count in (3, 6, 12, 24, 48) or self._fail_count % 120 == 0:
            self._reconnect()

    def _reconnect(self) -> None:
        if self.real_gw is None or self._connect_params is None:
            return
        self.engine.audit_app("INFO", "mt5", "attempting MT5 reconnect")
        try:
            self.real_gw.connect(self._connect_params)
            self.last_recovery = recover(self.engine)
        except LiveAccountBlocked as e:
            self.risk.emergency(str(e))
            self.engine.audit_app("CRITICAL", "safety", str(e))
        except Exception as e:  # noqa: BLE001
            self.engine.audit_app("WARNING", "mt5", f"reconnect failed: {e}")

    def _equity_snapshot(self) -> None:
        gw = self.engine.gw
        if gw is None:
            return
        acct = gw.account_info()
        if acct:
            with self.db.session() as s:
                s.add(EquitySnapshotRow(balance=acct.balance, equity=acct.equity,
                                        is_simulated=self.engine.simulated))

    # ------------------------------------------------------------- MT5
    async def connect_mt5(self, password: str | None = None, *, save_password: bool = False) -> AccountInfo:
        cfg = self.config.mt5
        if cfg.login is None or not cfg.server:
            raise ValueError("Configure the account number and server first")
        if password is None:
            password = self.secrets.get(SecretStore.mt5_password_key(cfg.login))
        elif save_password:
            self.secrets.set(SecretStore.mt5_password_key(cfg.login), password)
        params = ConnectParams(terminal_path=cfg.terminal_path, login=cfg.login, password=password,
                               server=cfg.server)

        def _do() -> AccountInfo:
            inner = self.gateway_factory()
            guard = AccountGuard(inner, cfg.login, cfg.server,
                                 allow_real_connect=lambda: self.config.mt5.allow_real_account,
                                 allow_real_send=self._real_send_allowed)
            acct = guard.connect(params)          # raises LiveAccountBlocked for a refused real account
            if cfg.expected_currency and acct.currency != cfg.expected_currency:
                guard.disconnect()
                raise GatewayError(f"account currency {acct.currency} != expected {cfg.expected_currency}")
            self.real_gw = guard
            self._connect_params = params
            self._fail_count = 0
            self.risk.mt5_unstable = False
            self.engine.set_mode(ExecutionMode.MONITOR, ReadOnlyGateway(guard))
            self.engine.resolver.suffix = cfg.symbol_suffix
            self.last_recovery = recover(self._recovery_engine(self._broker_mode(acct), guard))
            kind = "DEMO" if acct.is_demo else "REAL-MONEY"
            self.engine.audit_app("INFO" if acct.is_demo else "WARNING", "mt5",
                                  f"Connected to {kind} account {acct.login} on {acct.server} "
                                  f"({acct.currency}, 1:{acct.leverage}, "
                                  f"{'hedging' if acct.is_hedging else 'netting'})")
            return acct

        try:
            return await self.run_mt5(_do)
        except LiveAccountBlocked as e:
            self.engine.audit_app("CRITICAL", "safety", str(e))
            raise

    def _real_send_allowed(self, acct: AccountInfo, req: TradeRequest) -> bool:
        """Guard policy for real-money accounts (called before every order)."""
        if not self.config.mt5.allow_real_account:
            return False
        if self.risk.state.confirmed_real_account != f"{acct.login}@{acct.server}":
            return False
        # new orders only in REAL ACCOUNT mode; closing / protecting positions always
        return self.engine.mode is ExecutionMode.LIVE or is_risk_reducing(req)

    def _broker_mode(self, acct: AccountInfo | None = None) -> ExecutionMode:
        """Mode to use for helper engines that act on the connected broker account."""
        if acct is None and self.real_gw is not None:
            acct = self.real_gw.account_info()
        return ExecutionMode.LIVE if acct is not None and not acct.is_demo else ExecutionMode.DEMO

    async def confirm_real_account(self, typed_login: str) -> None:
        """Typed confirmation, required once per real-money account before REAL ACCOUNT mode."""
        def _do() -> None:
            acct = self.real_gw.account_info() if self.real_gw else None
            if acct is None:
                raise ValueError("Connect the account first")
            if acct.is_demo:
                raise ValueError("This is a DEMO account: use DEMO auto-execution")
            if not self.config.mt5.allow_real_account:
                raise ValueError("Real-money accounts are not allowed (Connection settings)")
            if typed_login.strip() != str(acct.login):
                raise ValueError("The account number you typed does not match the connected account")
            self.risk.state.confirmed_real_account = f"{acct.login}@{acct.server}"
            self.risk.persist()
            self.engine.audit_app("CRITICAL", "safety",
                                  f"User confirmed REAL-MONEY execution for {acct.login}@{acct.server}")
        await self.run_mt5(_do)

    def _recovery_engine(self, mode: ExecutionMode, gw: MT5Gateway) -> ExecutionEngine:
        return ExecutionEngine(self.db, gw, self.risk, lambda: self.config, mode=mode, emit=self._emit,
                               symbol_suffix=self.config.mt5.symbol_suffix)

    async def disconnect_mt5(self) -> None:
        await self.set_mode(ExecutionMode.MONITOR)

        def _do() -> None:
            if self.real_gw:
                self.real_gw.disconnect()
            self.real_gw = None
            self.engine.set_gateway(None)
        await self.run_mt5(_do)

    # ------------------------------------------------------------- modes
    def account_key(self) -> str:
        acct = self.real_gw.account_info() if self.real_gw else None
        return f"{acct.login}@{acct.server}" if acct else ""

    async def confirm_automated_execution(self) -> None:
        """Explicit user confirmation, required once per DEMO account before any order."""
        def _do() -> None:
            acct = self.real_gw.account_info() if self.real_gw else None
            if acct is None or not acct.is_demo:
                raise LiveAccountBlocked("Connect and verify a DEMO account first")
            self.risk.state.confirmed_account = f"{acct.login}@{acct.server}"
            self.risk.persist()
            self.engine.audit_app("WARNING", "safety",
                                  f"User confirmed automated DEMO execution for {acct.login}@{acct.server}")
        await self.run_mt5(_do)

    async def set_mode(self, mode: ExecutionMode) -> None:
        def _do() -> None:
            if mode is ExecutionMode.MONITOR:
                self.engine.set_mode(mode, ReadOnlyGateway(self.real_gw) if self.real_gw else None)
                if self.real_gw is None:
                    self.engine.set_gateway(None)
                return
            if self.real_gw is None or not self.real_gw.is_connected():
                raise ValueError("Connect MT5 first (market data is required)")
            if mode is ExecutionMode.PAPER:
                p = self.config.paper
                if self.paper_gw is None:
                    self.paper_gw = PaperGateway(self.real_gw.inner, balance=p.starting_balance,
                                                 extra_spread_points=p.extra_spread_points,
                                                 slippage_points=p.slippage_points,
                                                 commission_per_lot=p.commission_per_lot)
                    self._restore_paper_positions(self.paper_gw)
                self.engine.set_mode(mode, self.paper_gw)
                return
            # DEMO or LIVE (REAL ACCOUNT)
            if self.risk.state.emergency_stop:
                raise ValueError("Emergency stop is active - clear it first")
            acct = self.real_gw.account_info()
            if acct is None:
                raise ValueError("No account information - reconnect MT5")
            key = f"{acct.login}@{acct.server}"
            if mode is ExecutionMode.DEMO:
                if not acct.is_demo:
                    raise LiveAccountBlocked("Account is not a DEMO account - use REAL ACCOUNT mode for it")
                if self.risk.state.confirmed_account != key:
                    raise ValueError("Automated execution has not been confirmed for this DEMO account")
            else:
                if self.sandbox:
                    raise ValueError("REAL ACCOUNT mode is not available in sandbox mode")
                if acct.is_demo:
                    raise ValueError("This is a DEMO account: use DEMO auto-execution")
                if not self.config.mt5.allow_real_account:
                    raise LiveAccountBlocked("Real-money accounts are not allowed: switch on 'Allow "
                                             "real-money accounts' in Connection first")
                if self.risk.state.confirmed_real_account != key:
                    raise ValueError("REAL ACCOUNT execution has not been confirmed for this account")
            if not self.config.sources or not any(s.enabled for s in self.config.sources):
                raise ValueError("No enabled Telegram source")
            missing = [] if self.sandbox else pending_reviews(self.config)
            if missing:
                raise ValueError("Review the red risk settings first (Risk page, tick each one): "
                                 + ", ".join(REVIEW_LABELS[k] for k in missing))
            ti = self.real_gw.terminal_info()
            if ti is not None and not ti.trade_allowed:
                raise ValueError("Algo Trading is disabled in the MT5 terminal")
            self.engine.set_mode(mode, self.real_gw)
            if mode is ExecutionMode.DEMO:
                self.engine.audit_app("WARNING", "safety", "DEMO auto-execution ENABLED")
            else:
                self.engine.audit_app("CRITICAL", "safety",
                                      f"REAL ACCOUNT auto-execution ENABLED on {key} (real money)")
        await self.run_mt5(_do)
        self._emit("mode", {"mode": mode.value})

    def _restore_paper_positions(self, gw: PaperGateway) -> None:
        """Paper positions live in memory; rebuild them from the DB after a restart."""
        with self.db.session() as s:
            legs = list(s.scalars(select(ExecutionRow).where(
                ExecutionRow.is_simulated.is_(True), ExecutionRow.state == LegState.OPEN.value)))
            for l in legs:
                if l.position_ticket and l.fill_price:
                    gw._positions[l.position_ticket] = _Pos(
                        l.position_ticket, l.symbol, 0 if l.direction == "BUY" else 1,
                        l.volume - l.closed_volume, l.fill_price, l.sl or 0.0, l.tp or 0.0,
                        self.config.execution.magic_number, l.client_tag, 0.0)
            mx = max([l.position_ticket or 0 for l in legs] + [0])
            import itertools
            gw._ids = itertools.count(max(1000, mx + 1))

    # ------------------------------------------------------------- safety
    async def emergency_stop(self, reason: str = "manual STOP button") -> None:
        self.risk.emergency(reason)
        await self.set_mode(ExecutionMode.MONITOR)

        def _cancel_waiting() -> None:
            with self.db.session() as s:
                for sig in s.scalars(select(SignalRow).where(
                        SignalRow.status.in_([SignalStatus.WAITING.value, SignalStatus.QUEUED.value]))):
                    self.engine._transition(s, sig, SignalStatus.CANCELLED, "emergency stop")
        await self.run_mt5(_cancel_waiting)
        self.engine.audit_app("CRITICAL", "safety", f"EMERGENCY STOP: {reason}")
        self._emit("mode", {"mode": "MONITOR"})

    async def clear_emergency(self) -> None:
        self.risk.clear_emergency()
        self.engine.audit_app("WARNING", "safety", "Emergency stop cleared by user (mode stays MONITOR)")

    async def flatten_all(self) -> list[str]:
        """Close every position / pending order OWNED BY THIS APP (by magic + tracked legs)."""
        def _do() -> list[str]:
            gw = self.paper_gw if self.engine.mode is ExecutionMode.PAPER else self.real_gw
            if gw is None:
                raise ValueError("Not connected")
            eng = self._recovery_engine(
                ExecutionMode.PAPER if gw is self.paper_gw else self._broker_mode(), gw)
            results = []
            with self.db.session() as s:
                legs = list(s.scalars(select(ExecutionRow).where(
                    ExecutionRow.is_simulated == eng.simulated,
                    ExecutionRow.state.in_([LegState.OPEN.value, LegState.PENDING.value]))))
                for l in legs:
                    if l.state == LegState.OPEN.value:
                        ok, m = close_leg(eng, s, l, l.volume - l.closed_volume, "flatten all")
                    else:
                        ok, m = cancel_pending_leg(eng, s, l, "flatten all")
                    results.append(f"{l.client_tag}: {m}")
            eng.audit_app("WARNING", "safety", f"Flatten all: {len(results)} order(s) processed")
            return results
        return await self.run_mt5(_do)

    async def close_position(self, leg_id: int) -> tuple[bool, str]:
        """Manual close of one app-managed leg (UI, after confirmation)."""
        def _do():
            with self.db.session() as s:
                leg = s.get(ExecutionRow, leg_id)
                if leg is None or leg.state != LegState.OPEN.value:
                    return False, "not open"
                gw = self.engine.gw
                if isinstance(gw, ReadOnlyGateway):
                    gw = self.real_gw
                eng = self._recovery_engine(self.engine.mode if self.engine.mode is not
                                            ExecutionMode.MONITOR else self._broker_mode(), gw)
                return close_leg(eng, s, leg, leg.volume - leg.closed_volume, "manual close")
        return await self.run_mt5(_do)

    async def modify_position(self, leg_id: int, sl: float, tp: float | None) -> tuple[bool, str]:
        from tgmt5.execution.management import modify_leg_sl

        def _do():
            with self.db.session() as s:
                leg = s.get(ExecutionRow, leg_id)
                if leg is None or leg.state not in (LegState.OPEN.value, LegState.PENDING.value):
                    return False, "not open"
                gw = self.engine.gw
                if isinstance(gw, ReadOnlyGateway):
                    gw = self.real_gw
                eng = self._recovery_engine(self.engine.mode if self.engine.mode is not
                                            ExecutionMode.MONITOR else self._broker_mode(), gw)
                return modify_leg_sl(eng, s, leg, sl, "manual modify", new_tp=tp)
        return await self.run_mt5(_do)

    async def resolve_review(self, signal_id: int, approve: bool) -> SignalStatus:
        """Manual review queue: dismiss, or re-run the FULL validation for a reviewed signal."""
        def _do() -> SignalStatus:
            with self.db.session() as s:
                sig = s.get(SignalRow, signal_id)
                if sig is None or sig.status != SignalStatus.MANUAL_REVIEW.value:
                    raise ValueError("signal is not awaiting review")
                if not approve or sig.kind != MessageKind.NEW_SIGNAL.value or sig.stop_loss is None:
                    self.engine._transition(s, sig, SignalStatus.CANCELLED, "dismissed by user")
                    return SignalStatus.CANCELLED
            return self.engine.process_signal(signal_id, None, from_waiting=False)
        return await self.run_mt5(_do)

    # ------------------------------------------------------------- config
    async def save_config(self, cfg: AppConfig) -> None:
        cfg = AppConfig.model_validate(cfg.model_dump())   # full validation
        if not cfg.mt5.allow_real_account:
            # switching real-money trading off takes effect immediately
            if self.engine.mode is ExecutionMode.LIVE:
                await self.set_mode(ExecutionMode.MONITOR)
            if self.risk.state.confirmed_real_account:
                self.risk.state.confirmed_real_account = ""
                self.risk.persist()
        cfg.mode = self.config.mode
        self.config = cfg
        self.db.save_config(cfg)
        await self.telegram.update_chats(self.enabled_chat_ids())
        self.engine.audit_app("INFO", "config", "Configuration saved")
        self._emit("config", {})

    async def sizing_preview(self, cfg: AppConfig, *, fallback_balance: float, sl_pips: float,
                             n_tps: int, recent: bool) -> SizingPreview:
        """Lots / risk / margin the engine would use for example signals (Risk page preview)."""
        examples = (self._recent_examples() if recent else []) or [
            ExampleSignal(sym, Direction.BUY, n_tps, sl_pips=sl_pips, label="typical")
            for sym in DEFAULT_SYMBOLS]
        gw, resolver = self.engine.gw, self.engine.resolver
        if gw is None:
            return build_preview(None, None, cfg, examples, fallback_balance=fallback_balance)
        return await self.run_mt5(lambda: build_preview(gw, resolver, cfg, examples,
                                                        fallback_balance=fallback_balance))

    def _recent_examples(self, limit: int = 8) -> list[ExampleSignal]:
        """Most recent parsed signal per symbol, as preview examples."""
        out: dict[str, ExampleSignal] = {}
        with self.db.session() as s:
            rows = s.scalars(select(SignalRow).where(
                SignalRow.kind == MessageKind.NEW_SIGNAL.value, SignalRow.stop_loss.is_not(None),
                SignalRow.symbol.is_not(None)).order_by(SignalRow.id.desc()).limit(300))
            for r in rows:
                if r.symbol in out:
                    continue
                entry = r.entry_price if r.entry_price is not None else (
                    (r.entry_low + r.entry_high) / 2 if r.entry_low and r.entry_high else None)
                if entry is None:
                    continue
                out[r.symbol] = ExampleSignal(
                    r.symbol, Direction(r.direction), len(r.take_profits or []), entry=entry,
                    stop_loss=r.stop_loss, label=f"signal {r.created_at:%d.%m %H:%M}")
                if len(out) >= limit:
                    break
        return list(out.values())

    # ------------------------------------------------------------- snapshot
    async def snapshot(self) -> Snapshot:
        return await self.run_mt5(self._snapshot)

    def _snapshot(self) -> Snapshot:
        gw = self.engine.gw
        acct = gw.account_info() if gw else None
        connected = bool(self.real_gw and self.real_gw.is_connected())
        tg = self.telegram.status
        snap = Snapshot(
            mode=self.engine.mode.value, sandbox=self.sandbox, account=acct, mt5_connected=connected,
            telegram={"connected": tg.connected, "authorized": tg.authorized, "user": tg.user,
                      "latency_ms": tg.latency_ms, "reconnects": tg.reconnects,
                      "messages": tg.messages_received, "avg_delivery_ms": tg.avg_delivery_ms},
            blocking_reason=self.risk.blocking_reason(), emergency=self.risk.state.emergency_stop,
            confirmed=bool(acct and (self.risk.state.confirmed_real_account if not acct.is_demo else
                                     self.risk.state.confirmed_account) == f"{acct.login}@{acct.server}"),
            queue_depth=self.queue.qsize())
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        sim = self.engine.simulated
        with self.db.session() as s:
            base = select(func.count(SignalRow.id)).where(SignalRow.created_at >= start,
                                                          SignalRow.kind == MessageKind.NEW_SIGNAL.value)
            snap.signals_today = s.scalar(base) or 0
            snap.executed_today = s.scalar(base.where(SignalRow.is_simulated == sim, SignalRow.status.in_(
                ["EXECUTED", "PARTIAL", "CLOSED", "SIMULATED", "PENDING_PLACED"]))) or 0
            snap.rejected_today = s.scalar(base.where(SignalRow.status.in_(
                ["REJECTED", "EXPIRED", "FAILED"]))) or 0
            snap.review_queue = s.scalar(select(func.count(SignalRow.id)).where(
                SignalRow.status == SignalStatus.MANUAL_REVIEW.value)) or 0
            legs = {l.position_ticket: l for l in s.scalars(select(ExecutionRow).where(
                ExecutionRow.is_simulated == sim, ExecutionRow.state == LegState.OPEN.value))}
            pend = {l.order_ticket: l for l in s.scalars(select(ExecutionRow).where(
                ExecutionRow.is_simulated == sim, ExecutionRow.state == LegState.PENDING.value))}
            if gw is not None and acct is not None:
                from tgmt5.risk.engine import position_open_risk
                magic = self.config.execution.magic_number
                for p in gw.positions():
                    if p.magic != magic:
                        continue
                    leg = legs.get(p.ticket)
                    r = position_open_risk(gw, p)
                    snap.open_risk += r
                    snap.floating_pnl += p.profit
                    snap.positions.append({
                        "ticket": p.ticket, "symbol": p.symbol, "type": "BUY" if p.type == 0 else "SELL",
                        "volume": p.volume, "price_open": p.price_open, "price_current": p.price_current,
                        "sl": p.sl, "tp": p.tp, "profit": p.profit, "risk": r, "opened": p.time,
                        "signal_id": leg.signal_id if leg else None, "leg_id": leg.id if leg else None})
                for o in gw.orders():
                    if o.magic != magic:
                        continue
                    leg = pend.get(o.ticket)
                    snap.orders.append({"ticket": o.ticket, "symbol": o.symbol, "type": o.type,
                                        "volume": o.volume_current, "price": o.price_open, "sl": o.sl,
                                        "tp": o.tp, "signal_id": leg.signal_id if leg else None})
                snap.open_risk_cap = acct.equity * self.config.risk.max_total_open_risk_pct / 100
                if not sim:
                    snap.daily_pnl = self.risk.daily_pnl(acct)
                    snap.drawdown_pct = self.risk.drawdown_pct(acct)
                else:
                    snap.daily_pnl = acct.equity - self.config.paper.starting_balance
        return snap
