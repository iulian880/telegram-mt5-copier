---
paths:
  - "src/tgmt5/execution/**"
  - "src/tgmt5/mt5/**"
  - "src/tgmt5/risk/**"
  - "src/tgmt5/services/**"
---
# Trading safety invariants
- Commit each leg (client tag `TG<signal>L<leg>`) to SQLite before `order_send`. Never reorder this.
- On timeout, connection error, exception or missing result the leg becomes `UNCERTAIN`. Retry once only after the order is proven absent (positions, pending orders, 7 days of deals) and the price is still within slippage; if the broker can't be queried, never retry.
- Edits of an executed signal never re-execute.
- Every MetaTrader5 call goes through the single `mt5` worker thread.
- `AccountGuard`/`DemoOnlyGateway`, MONITOR-on-start and the emergency stop are not to be relaxed, even in tests: add tests instead.
- Any change here needs a test covering the failure path (timeout, rejection, disconnect), not only the happy path.
