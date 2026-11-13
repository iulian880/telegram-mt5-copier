# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `MetaTrader5 package is not available` | Windows only. Run `pip install MetaTrader5` in the venv and use 64-bit Python. |
| `MT5 initialize failed: [-6] Authorization failed` | Wrong login, password or server. Log in once by hand in the terminal and copy the exact server name. |
| `MT5 initialize failed: [-10005] IPC timeout` | The terminal is starting or busy. Start MT5 first and select the right `terminal64.exe` if you have several. |
| `Account … is NOT a demo account` | Working as designed: live accounts are refused. Log the terminal in to your demo account. |
| `Algo Trading is disabled in the terminal` (retcode 10027) | Turn on the **Algo Trading** button in MT5. |
| `symbol XAUUSD not found (or ambiguous)` | The broker uses a suffix or several variants. Set **Symbol suffix** in Connection, or add an alias on the source. |
| `Unsupported filling mode` (10030) | The app retries with the next mode the symbol allows (FOK → IOC → RETURN). If it still fails, see the log. |
| `Invalid stops` (10016) | SL/TP is too close (stops level) or already passed. The signal is rejected; check the provider's levels. |
| Signals `EXPIRED` | The message arrived later than *max signal age* (e.g. after a reconnect). Working as designed. |
| `minimum volume … above the budget` | The SL is too wide for your risk %. Raise the risk or accept that the signal is skipped. |
| Many `MANUAL REVIEW` | Use per-source aliases, or the `implicit_market` template for providers who post `BUY GOLD` + SL/TP without "now". |
| Telegram `not authorised` | Sign in again in Sources. If you revoked the session in Telegram, the stored one is invalid. |
| `database is locked` | Another TGMT5 instance is running on the same data folder. Close it. |
| "Windows protected your PC" (SmartScreen) | The release files are signed with a self-signed test certificate, so Windows doesn't recognise the publisher. Click **More info → Run anyway**. |
| Portable version starts slowly | It unpacks itself to a temporary folder on every start. That is normal. Use the installer for faster start-up. |
| Logs | `%LOCALAPPDATA%\TGMT5\logs\tgmt5.jsonl` (portable version: `TGMT5-data\logs\` next to the `.exe`), plus the in-app **Log** page. |
