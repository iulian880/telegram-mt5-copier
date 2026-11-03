# IC Markets SC DEMO and MT5 setup

1. Open a **demo account** with IC Markets (Seychelles / "IC Markets SC"), MetaTrader 5 platform. Write down the **login**, **password** and **server**. Demo servers are usually named like `ICMarketsSC-Demo`, but use the exact name from your account email.
2. Install the MetaTrader 5 terminal and log in to the demo account once by hand.
3. In MT5, enable **Algo Trading** (toolbar button). Under *Tools → Options → Expert Advisors*, make sure algorithmic trading is allowed.
4. In TGMT5, open **Connection**:
   - **Detect** the terminal (or Browse to `terminal64.exe`);
   - enter the login and server, then the password (optionally saved to Credential Manager);
   - click **Connect & verify**. The app checks that the account is DEMO (`trade_mode`) and that the login and server match. Any other account is disconnected immediately.
5. Check the **Account verification** card: currency, leverage, hedging/netting, balance and margin.
6. Stay in **Monitor only** for a while and watch how signals are interpreted. Then try **Paper trading**. Switch to **DEMO auto-execution** only when you are satisfied. The first time, you must confirm the account explicitly.

What is discovered from the broker, not assumed
- Symbol names and suffixes (e.g. `XAUUSD` or `XAUUSD.a`; NAS100 → `USTEC`). If more than one variant matches, the signal goes to review, or you can set the suffix by hand.
- Contract size, tick size and value, volume min/max/step, stops and freeze levels, filling modes, profit and margin currency.
- Profit and loss for sizing via `order_calc_profit`, and margin via `order_calc_margin`.
- Hedging or netting account mode.

Pending-order expiry is handled by the app (it cancels after N minutes) instead of relying on broker-specific expiration modes.
