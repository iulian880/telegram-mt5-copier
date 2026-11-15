---
paths:
  - "src/tgmt5/ui/**"
---
# UI conventions
- The UI calls only `AppService` and never reaches order sending: no imports from `tgmt5.execution` or of MT5 gateways. The one existing exception is `tgmt5.mt5.real.detect_terminals` (terminal discovery in `pages/connection.py`).
- Colours come from `theme.Palette` (both `DARK` and `LIGHT`); don't add hex literals outside `theme.py` (one legacy literal remains in `widgets.py`). A new colour gets a `Palette` field with values for both themes.
- Check both themes (`TGMT5_THEME=light`) when changing visuals; take screenshots with `scripts/screenshots.py`.
