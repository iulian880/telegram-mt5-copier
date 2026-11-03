"""Canonical instrument names and their common Telegram aliases.

A canonical symbol is a broker-independent name (``XAUUSD``, ``NAS100``).  The
broker-specific name (``XAUUSD.a``, ``USTEC`` ...) is resolved later against the
symbols actually offered by the connected MT5 terminal; see
:mod:`tgmt5.mt5.symbols`.
"""
from __future__ import annotations

import re

CURRENCIES = frozenset({
    "EUR", "USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD",
    "NOK", "SEK", "DKK", "PLN", "MXN", "ZAR", "TRY", "CNH", "HUF", "CZK",
})

# canonical -> aliases that may appear in messages
CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("XAUUSD", "XAU/USD", "XAU", "GOLD"),
    "XAGUSD": ("XAGUSD", "XAG/USD", "XAG", "SILVER"),
    "US30": ("US30", "DJ30", "DOW", "DOWJONES", "DJI", "WALLSTREET"),
    "NAS100": ("NAS100", "NASDAQ", "NAS", "USTEC", "US100", "NDX", "NQ"),
    "SPX500": ("SPX500", "SPX", "US500", "SP500", "S&P500"),
    "GER40": ("GER40", "DAX", "DE40", "GER30", "DE30"),
    "UK100": ("UK100", "FTSE", "FTSE100"),
    "USOIL": ("USOIL", "WTI", "XTIUSD", "CRUDE", "OIL"),
    "UKOIL": ("UKOIL", "BRENT", "XBRUSD"),
    "BTCUSD": ("BTCUSD", "BTC/USD", "BTC", "BITCOIN"),
    "ETHUSD": ("ETHUSD", "ETH/USD", "ETH", "ETHEREUM"),
}

# canonical -> broker names to try, in order of preference (suffixes handled separately)
BROKER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("XAUUSD", "GOLD"),
    "XAGUSD": ("XAGUSD", "SILVER"),
    "US30": ("US30", "DJ30", "WS30"),
    "NAS100": ("USTEC", "NAS100", "US100", "NDX100"),
    "SPX500": ("US500", "SPX500", "SP500"),
    "GER40": ("DE40", "GER40", "DE30", "GER30"),
    "UK100": ("UK100", "FTSE100"),
    "USOIL": ("XTIUSD", "USOIL", "WTI"),
    "UKOIL": ("XBRUSD", "UKOIL", "BRENT"),
    "BTCUSD": ("BTCUSD",),
    "ETHUSD": ("ETHUSD",),
}

_ALIAS_INDEX: dict[str, str] = {}
for _canon, _aliases in CANONICAL_ALIASES.items():
    for _a in _aliases:
        _ALIAS_INDEX[_a.replace("/", "")] = _canon

_FX_RE = re.compile(r"^([A-Z]{3})/?([A-Z]{3})$")


def canonicalize(token: str, extra_aliases: dict[str, str] | None = None) -> str | None:
    """Map a message token to a canonical symbol, or ``None`` if it is not an instrument."""
    t = token.upper().strip().strip("#$")
    if extra_aliases:
        for k, v in extra_aliases.items():
            if k.upper() == t:
                return v.upper()
    compact = t.replace("/", "")
    if compact in _ALIAS_INDEX:
        return _ALIAS_INDEX[compact]
    m = _FX_RE.match(t)
    if m and m.group(1) in CURRENCIES and m.group(2) in CURRENCIES and m.group(1) != m.group(2):
        return m.group(1) + m.group(2)
    return None


def broker_candidates(canonical: str) -> tuple[str, ...]:
    return BROKER_CANDIDATES.get(canonical, (canonical,))
