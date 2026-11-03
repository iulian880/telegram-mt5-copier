# Architecture

## Layers

| Package | Responsibility |
|---|---|
| `tgmt5.domain` | Plain dataclasses and enums (signals, statuses, decisions). No I/O. |
| `tgmt5.parsing` | Deterministic parser plus symbol aliases. Pure function: text in, `ParseResult` out. |
| `tgmt5.mt5` | `MT5Gateway` protocol; `RealMT5Gateway` (official package), `SimBroker` (fake and paper), `DemoOnlyGateway` guard, `SymbolResolver`. |
| `tgmt5.risk` | `sizing` (broker-spec lot sizing) and `RiskEngine` (limits and persisted lockouts). |
| `tgmt5.execution` | `ExecutionEngine` (pipeline and state machine), `management` (follow-ups, BE/TP/trailing, sync), `recovery`. |
| `tgmt5.telegram` | Telethon adapter and a scriptable fake source. |
| `tgmt5.persistence` | SQLAlchemy models, `Database` (sessions, settings, audit log) and Alembic migrations. |
| `tgmt5.services` | `AppService`: threading, queue, modes, health checks, notifications, UI snapshot. |
| `tgmt5.analytics` | Performance statistics and CSV export (DEMO and PAPER kept separate). |
| `tgmt5.ui` | PySide6 window, pages and custom widgets. The UI only calls `AppService`. |

## Signal state machine

```
RECEIVED → PARSED → VALIDATED → QUEUED → SUBMITTING → EXECUTED | PENDING_PLACED | PARTIAL | SIMULATED
   │          │          │                     └──────→ FAILED | UNCERTAIN (→ reconciled)
   │          │          └→ REJECTED | WAITING (→ re-validated each tick until expiry → EXPIRED)
   │          └→ MANUAL_REVIEW (ambiguous / incomplete)          MONITORED (monitor-only mode)
   └→ (noise is logged, not stored as a signal)                  EXECUTED/… → CLOSED | CANCELLED
```

Each broker order is an `ExecutionRow` ("leg") with its own state: `SUBMITTING → OPEN | PENDING | REJECTED | UNCERTAIN → (reconcile) → OPEN | FAILED`, then `CLOSED` or `CANCELLED`.

## Idempotency and duplicate prevention

1. `(source_id, message_id)` is unique, so a repeated Telegram event does nothing.
2. Edits of an executed signal never re-execute. They are recorded and need manual review (SL edits can optionally be applied).
3. Cross-source deduplication uses a fingerprint of symbol, direction, SL and TPs within a time window.
4. Each leg gets a unique client tag (`TG<signal>L<leg>`) used as the MT5 order comment. The leg is committed to SQLite *before* `order_send`.
5. On `TIMEOUT`, `CONNECTION`, `ERROR`, an exception or a missing result, the leg becomes `UNCERTAIN`. The engine searches positions, pending orders and the last 7 days of deals for the tag and magic number. A single retry is allowed only once the order is **proven absent**, and only if the price is still within the slippage limit. If the broker cannot be queried, the leg stays `UNCERTAIN` and is never retried.

## Threading

- asyncio loop, shared with Qt through qasync: Telegram, timers and UI coroutines.
- One `mt5` worker thread runs every MetaTrader5 call and every pipeline step (`ThreadPoolExecutor(max_workers=1)`).
- Telegram events go into a bounded `asyncio.Queue` (1000). When it is full, the message is dropped and logged as an error, which means it is *not executed*.
- `Database.session()` reuses the outer session when it is nested on the same thread, so one pipeline step is one atomic SQLite transaction. This also avoids the SQLite single-writer lock.

## Database schema (SQLite, WAL, `synchronous=FULL`)

- `settings`: validated `AppConfig` JSON and `risk_state` (lockouts, equity peak, day-start equity, confirmed account).
- `telegram_messages`: original text, IDs, reply-to, date, edit count, deleted flag.
- `signals`: parsed fields, status, reason, lots, risk, fingerprint, parent signal (for management messages), latency.
- `signal_events`: the audit trail of every transition and decision, including all risk checks.
- `executions`: legs with request JSON, retcode, tickets, fill, slippage, spread, latency, P/L.
- `app_events`: the searchable activity log (credentials redacted).
- `equity_snapshots`: the equity curve.

Migrations are in `src/tgmt5/persistence/migrations` and are applied automatically on start-up (`alembic upgrade head`).

## Key decisions

- **Unit-free price filters.** Spread and entry tolerance are expressed as a fraction of the stop-loss distance, so the same defaults work for EURUSD and XAUUSD. Point-based overrides per symbol are optional.
- **Multi-TP.** On hedging accounts the default is one position per TP (`SPLIT`) with weighted allocation. Netting accounts automatically use `SINGLE_MANAGED`: one position whose TP is the final target, with partial closes at the earlier targets. If the volume is too small to split, the trade falls back to one position with TP1, and the fallback is logged.
- **Entry zones.** `WAIT` (default) re-validates on every tick until expiry, `REJECT` rejects, and `PENDING` places a limit order at the zone edge. The engine never switches strategy on its own.
- **No AI parser.** The deterministic parser covers the required formats. An AI extractor would add cost, latency and a new failure mode without being allowed to authorise trades anyway.
