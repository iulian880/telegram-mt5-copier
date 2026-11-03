# Test report

Run on 2026-11-03 in a Linux CI-style container (Python 3.13.18, PySide6 6.12.0, Telethon 1.45.0, SQLAlchemy 2.1.6, Alembic 1.20.1, Pydantic 2.14.2), with Qt in offscreen mode.

```
$ pytest -q
174 passed, 1 deselected in 5.56s
```

| File | Tests | Covers |
|---|---|---|
| `test_parser.py` | 57 | All seven example formats from the spec, aliases, limit/stop, multiple TPs, "TP open", thousands separators, timestamps and lot/risk noise, ambiguous cases (missing SL, wrong-side levels, typos, two symbols, conflicting directions, comma decimals), false positives (promo, results, analysis, past tense, code-like text), management variants |
| `test_sizing.py` | 14 | Never rounding up, EURUSD and XAUUSD sizing, non-standard contract size, JPY → USD conversion, tick-value fallback, min volume above budget, max volume and step, fixed lot, multi-TP split never exceeding the total, three TPs never tripling risk |
| `test_engine.py` | 46 | Full pipeline: duplicates, monitor mode, stale signals, unauthorised or disabled sources, review queue, price moved, wait-for-zone and expiry, zone REJECT/PENDING, explicit limits, spread, TP already reached, broker rejection, partial leg failure, **timeout-but-executed reconciled without a duplicate**, timeout-not-executed single retry, transport exceptions, broker unreachable (stays UNCERTAIN, no retry), filling-mode fallback, **live account blocked plus emergency**, first-execution confirmation, emergency stop, concurrent/total/margin/blacklist/frequency limits, cross-source dedup, broker suffix resolution, edits (fix SL → execute; never re-execute), deletes, daily-loss and drawdown lockouts persisting across restart, consecutive-loss breaker, MT5 instability or disconnect |
| `test_management.py` | 31 | Reply and context matching, ambiguity → review, BE (+offset, invalid), partial close, close all, modify SL/TP, cancel pending/waiting, TP-hit handling, TP1 → BE automation, trailing stop, netting account managed partials, FIRST_TP_ONLY, small-volume fallback, pending expiry, manual close in the terminal, **recovery** (uncertain found, absent → failed and not resent, crash before send, closed while offline, orphans, waiting expiry, no replay after restart) |
| `test_service.py` | 19 | Async service with worker thread and a real SQLite file plus Alembic migration: monitor default, DEMO gating and confirmation, live/wrong account refused, account switched to live after connecting, emergency persistence, flatten-all leaves manual trades alone, paper mode isolation and cost model, **6 concurrent signals with max 2 trades → exactly 2 executed**, bounded queue, disconnect → pause → reconnect → resume, review approve/dismiss, Telegram notifications, snapshot and analytics, config never persists execution enabled, secrets redacted from logs and DB |
| `test_telegram.py` | 6 | Telethon event conversion, chat filtering, edits/replies, deletions (incl. basic groups), handler exceptions, safe client options (`catch_up=False`, auto-reconnect) |
| `test_ui_smoke.py` | 1 | Builds the main window, renders all 8 pages against live service data |
| `test_demo_e2e.py` | 1 (opt-in) | Real DEMO round trip. **Not run here** (needs Windows, an MT5 terminal and explicit approval) |

## What was NOT verified in this environment

- **The real MetaTrader5 package and terminal.** It is Windows-only. `RealMT5Gateway` is a thin mapping written against the documented API (`initialize`, `account_info`, `symbol_info`, `order_calc_profit`, `order_send`, …) but it was not executed. Run `pytest -m demo_e2e` on Windows to verify it.
- **The real Telegram network.** The login flow, `iter_dialogs`, event delivery and reconnect behaviour of the live Telethon client were not exercised; only the adapter's event handling was tested, with stub events.
- **IC Markets-specific behaviour.** Symbol names, filling modes, stops levels and whether order comments are preserved all come from the terminal at runtime. Reconciliation depends on the broker keeping the order comment (client tag). If a broker overwrote comments, an uncertain order would be marked FAILED and the position would appear as an "orphan" (magic number match) in the recovery report, so it would be flagged rather than duplicated.
- **The PyInstaller build** was not run (it needs Windows).
- **Visual review** used offscreen renders on Linux with the Inter font. On Windows the UI uses Segoe UI Variable.

## Known limitations

- Paper-trading positions are rebuilt from the database after a restart, but SL/TP hits that happen *while the app is closed* are not simulated for paper trades.
- Deletion events from basic (non-super) groups carry no chat ID. They are matched by message ID across sources.
- The parser is English-oriented. Providers with unusual formats need aliases or the `implicit_market` template, or their messages end up in manual review.
- Correlation groups are user-defined lists, not computed correlations.
- The daily loss lockout uses UTC days.
