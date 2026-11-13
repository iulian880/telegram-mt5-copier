# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `MetaTrader5 package is not available` | Windows only. Run `pip install MetaTrader5` in the venv and use 64-bit Python. |
| `MT5 initialize failed: [-6] Authorization failed` | Wrong login, password or server. Log in once by hand in the terminal and copy the exact server name. |
| `MT5 initialize failed: [-10005] IPC timeout` | The terminal is starting or busy. Start MT5 first and select the right `terminal64.exe` if you have several. |
| `Account … is NOT a demo account` | Real-money accounts are refused by default. Log the terminal in to your demo account, or switch on *Allow real-money accounts* in Connection and use REAL ACCOUNT mode. |
| `REAL ACCOUNT execution has not been confirmed` | Choose REAL ACCOUNT in Connection → Apply mode, tick the risk statement and type the account number. |
| `Review the red risk settings first` | Open Risk and tick every red setting (✓). Changed values must be ticked again. |
| Positions page empty / MT5 disconnected after a restart or update | TGMT5 reconnects by itself only when the MT5 password is saved in Credential Manager. Otherwise use Connection → Connect & verify (tick *Save password* once). Your positions stay at the broker with their SL/TP meanwhile. |
| Setup says it could not close TGMT5 | Versions before 0.2.2 did not close cleanly when Setup asked. Close TGMT5 yourself, then click Retry. From 0.2.2 on, Setup closes it automatically. |
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
