# User guide

## Modes
| Mode | What happens |
|---|---|
| **Monitor only** (always at start-up) | Messages are parsed and validated against live prices. Lot size and risk are shown. No orders. |
| **Paper trading** | Same pipeline, with fills simulated on live prices (configurable spread, slippage, commission). Stored separately. |
| **DEMO auto-execution** | Orders are sent to the verified demo account. Needs a connected DEMO account, an enabled source, Algo Trading on, no emergency stop, and a one-time confirmation per account. |

## Safety controls
- **EMERGENCY STOP** (top right): switches to Monitor only, cancels waiting signals and persists across restarts. Open positions are not touched. Clear it in *Risk → Equity protection*.
- **Flatten all…**: closes every position and pending order *opened by this app* (tracked legs and magic number), after a confirmation. Your manual trades are not affected.
- **Lockouts**: the daily loss lockout resets the next day. The drawdown lockout and the consecutive-loss breaker need a manual reset.
- If MT5 is unreachable 3 times in a row, execution pauses automatically and resumes after 5 healthy checks. Reconnection uses backoff.

## Signal monitor
Each card shows the source, the time, the original text, the parsed fields, lots, risk, MT5 tickets, the reason for a rejection and a status timeline. **Manual review** cards can be dismissed, or *re-validated & executed*. Re-validation runs every check again against the current market and only executes in DEMO or PAPER mode.

## Follow-up messages
`Move SL to BE`, `SL to 2655`, `New TP 2700`, `Close 50%`, `Close all`, `Cancel pending` and `TP1 hit` are matched to the original signal:
1. by **reply** (most reliable);
2. otherwise to the *only* active trade from that source (filtered by symbol if one is named);
3. if several trades match, the message goes to **manual review**. Nothing is guessed.

`TP1 hit` messages are informational by default. They can be set to move the SL to breakeven.

## Recommended defaults
0.5 % equity per signal, 3 % max open risk, 3 % daily loss, 10 % drawdown, 120 s max signal age, entry tolerance 0.3 × SL distance, spread ≤ 0.15 × SL distance.
