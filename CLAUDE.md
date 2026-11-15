# TGMT5: Telegram → MetaTrader 5 copy trading

Python 3.12+ desktop app (PySide6 + qasync) that parses Telegram signals and places MT5 orders. It runs on Windows (the `MetaTrader5` package is Windows-only); development happens on macOS. It can move real money, so safety rules beat convenience. Public repo: every push to `main` publishes a GitHub Release.

## Commands
- Local setup on this Mac (Homebrew Python 3.15 has no PySide6 wheels): `uv venv --python 3.13 .venv && uv pip install --python .venv -e ".[dev]"`
- Tests (local): `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` (257 tests, no credentials or broker)
- CI `tests.yml` (push/PR, ubuntu + windows, Python 3.12–3.14): `pip install -e ".[dev]"` then `pytest -q`
- CI `build.yml` (PR, push to main; windows-latest): `packaging\build_windows.ps1` (pytest, PyInstaller, signing), `build_portable.ps1`, `build_installer.ps1`, then a `--sandbox` smoke test of the portable exe. The Windows exes cannot be built on macOS.
- Run without a broker or Telegram (any OS): `python -m tgmt5 --sandbox`
- Screenshots: `.venv/bin/python scripts/screenshots.py <dir> <seconds>` (`TGMT5_THEME=light` for the light theme)

## Verification policy
- macOS runs the test suite and the sandbox. Real MT5, Telethon login and the packaged exe only run on Windows: check them through the PR's CI (`gh run watch`, push only after asking) or the user tests on Windows.
- Never report live trading behaviour as tested unless it ran against MT5 on Windows; say what stayed unverified.

## Structure (`src/tgmt5/`)
- `domain` (dataclasses, no I/O) · `parsing` (deterministic, pure) · `mt5` (`RealMT5Gateway`, `SimBroker`, `AccountGuard`, `SymbolResolver`) · `risk` · `execution` (`ExecutionEngine`, management, recovery) · `telegram` · `persistence` (SQLAlchemy + Alembic, migrations run at startup) · `services/app.py` (`AppService`) · `analytics` · `ui` (PySide6).
- `secrets.py` (OS keyring), `config.py`, `sandbox.py`, `__main__.py` (data dir: `TGMT5_PORTABLE=1` → `<exe dir>/TGMT5-data`, else `%LOCALAPPDATA%\TGMT5` or `~/.tgmt5`).
- Docs: `docs/ARCHITECTURE.md` (state machine, idempotency, threading, schema), `docs/TEST_REPORT.md`.

## Architecture decisions (keep them)
- The app always starts in MONITOR mode (`config.py` forces it on load). Real accounts need the three opt-in steps, and `AccountGuard` re-checks the account on every trade request. Never weaken or bypass these.
- Parsing stays deterministic; anything ambiguous goes to MANUAL_REVIEW. No AI parser.
- Every MetaTrader5 call and pipeline step runs on the single `mt5` worker thread; the package is not thread-safe.
- The UI only calls `AppService`; it never touches order sending.
- Secrets live only in the OS keyring (service `tgmt5`), never in config, the database or logs.
- No new dependencies without the user's approval.

## Gotchas
- Release: bump the version in `pyproject.toml`, `src/tgmt5/__init__.py` and `packaging/installer.iss`, and add `docs/release-notes/v<version>.md`; otherwise the next push to `main` overwrites the current release.
- `pytest -m demo_e2e` places real orders on a DEMO account (needs `TGMT5_DEMO_E2E=I_UNDERSTAND_THIS_PLACES_DEMO_ORDERS` and MT5 credentials). Never run it unless the user asks in this session.
- Commits use the GitHub noreply identity set in the repo's local git config; don't change it.

## Safety
- Never read, print or commit Telegram sessions (`*.session`), keyring values, MT5 passwords, signing certs (`*.pfx`, `*.p12`) or `config.local.json`.
- Don't push to `main`, merge PRs or delete releases without the user's explicit approval.

## UI and design
- Tokens: `src/tgmt5/ui/theme.py` (`Palette`, `DARK`/`LIGHT`, the QSS built in `stylesheet()`).
- Design system, once generated: `design-system/tgmt5/MASTER.md`, the single source for visual decisions. `ui-ux-pro-max` has no PySide6/Qt stack: use only its `--design-system` and `--domain` searches (no `--stack`), describe the app as a PySide6 desktop trading tool in the query text, and save with `--design-system -p tgmt5 --persist --output-dir <repo root>` (without `-p` the folder is named after the query).

## Local third-party skills (not in git, excluded via `.git/info/exclude`)
- `.claude/skills/ui-ux-pro-max`: nextlevelbuilder/ui-ux-pro-max-skill, tag v2.15.0, commit a38d04c. Local patch on 11 lines of SKILL.md: `python "${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max` → `python3 "${CLAUDE_SKILL_DIR}`. Uninstall: delete the folder.

## Compaction
Always keep: the files changed in this session, the test command and its last result, the current branch and version, any safety rule touched, and any approval still pending (push, merge, release, demo run).
