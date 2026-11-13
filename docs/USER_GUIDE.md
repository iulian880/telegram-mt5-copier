# User guide

## Modes
| Mode | What happens |
|---|---|
| **Monitor only** (always at start-up) | Messages are parsed and validated against live prices. Lot size and risk are shown. No orders. |
| **Paper trading** | Same pipeline, with fills simulated on live prices (configurable spread, slippage, commission). Stored separately. |
| **DEMO auto-execution** | Orders are sent to the verified demo account. Needs a connected DEMO account, an enabled source, Algo Trading on, no emergency stop, all red risk settings ticked, and a one-time confirmation per account. |
| **REAL ACCOUNT auto-execution** | Orders are sent to a **real-money** account. Off by default. Needs everything DEMO needs, plus *Allow real-money accounts* switched on in Connection and a confirmation where you type the account number. Shown in red everywhere. |

### REAL ACCOUNT mode
1. Test your settings on a DEMO account first.
2. In **Connection**, switch on *Allow real-money accounts* (you are asked to confirm), then connect the real account.
3. In **Risk**, check the preview with the detected balance and tick every red setting.
4. In **Connection**, choose *REAL ACCOUNT auto-execution* → **Apply mode**, tick the risk statement and type the account number.

The account type is checked again before every order. Outside REAL ACCOUNT mode a real account only accepts closing / protecting orders (close, remove pending, change SL/TP), so you can always manage open positions. Switching *Allow real-money accounts* off stops REAL ACCOUNT mode immediately and forgets the confirmation. The setting is never imported from a config file.

**At start-up** the app is always in Monitor only. If the MT5 password is saved in Credential Manager, it reconnects to MT5 by itself, still in Monitor only, so positions and balance are visible straight away. Switch execution back on in Connection.

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

## Position sizing (Risk page)
Pick one **sizing method**. Only the input that method needs is shown.

| Method | You set | Example with a 3-TP signal |
|---|---|---|
| *Simple:* Fixed lot on every TP | lot per TP | 0.01 → TP1 0.01 + TP2 0.01 + TP3 0.01 |
| *Simple:* Fixed total lot per signal | total lot | 0.06 → split with the TP allocation |
| *Simple:* Fixed money at risk | amount | €10 → lot calculated so the stop loss loses at most €10 |
| *Advanced:* % of equity / % of balance | percent | 1 % of €1,000 → €10 at risk |
| *Advanced:* Lot per TP that grows with balance | lot + balance step | 0.01 per 300 → 300–599: 0.01 per TP, 600–899: 0.02 per TP |

**Advanced sizing rules:**
- **Multiple TPs:** one position per TP (hedging accounts), one position closed in parts, or TP1 only.
- **TP volume allocation:** how a total is shared between the TPs. *Equal 1 : 1 : 1* is recommended for signals whose TPs sit at about 0.6R / 1R / 2R. With the SL moved to breakeven after TP1, the TP2 and TP3 positions then run without risk.
- **If the volume is too small:** use fewer TPs (default, never risks more than planned), open the minimum lot on every TP (may risk more), or skip the signal.

**What will be opened** shows, for each symbol, the exact lots per TP, the loss if the SL is hit, the % of equity and the margin. When MT5 is connected it uses the detected balance and the broker's own calculations, through the same sizing code the engine uses. Without a connection it uses an example balance and approximate pip values. It can use a typical signal (SL in pips, number of TPs) or your most recent real signals. Every blocked symbol shows the reason.

## Small accounts (from about 200 EUR)
With 200–500 EUR, one minimum lot (0.01) on three TPs is already about 5–8 % of the account per signal at a ~55-pip stop. The percentage caps and the drawdown lockouts would block every trade. Use the **Small account** preset, or set it up yourself:
- Fixed lot on every TP = 0.01
- Percentage risk caps **off**, drawdown protection **off** (both are switches)
- A low *Min free margin to keep*, for example 20

Before enabling execution, look at the preview's worst case ("if N signals hit their SL together"). The broker's margin check and the account guard always stay active.

## Pre-trade review (red fields)
Critical risk settings are shown in **red with a ✓ button**. Look at each one and click ✓: it returns to the normal colours. If you change a value later, it turns red again. **DEMO auto-execution cannot be switched on while any red field remains.** Imported settings must be reviewed again. Ticks are kept when you click Save, or right away when nothing else is unsaved.

## Recommended defaults
0.5 % equity per signal, 3 % max open risk, 3 % daily loss, 10 % drawdown, 120 s max signal age, entry tolerance 0.3 × SL distance, spread ≤ 0.15 × SL distance (the **Conservative** preset). For small accounts, see above.
