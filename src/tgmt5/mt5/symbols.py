"""Resolve canonical symbols to the names actually offered by the broker."""
from __future__ import annotations

import re

from tgmt5.mt5.interface import MT5Gateway
from tgmt5.parsing.symbols import broker_candidates, canonicalize

_SUFFIX_RE = re.compile(r"^[._\-+#]?[A-Za-z0-9]{0,4}$")


class SymbolResolver:
    def __init__(self, gateway: MT5Gateway, suffix: str = "") -> None:
        self.gw = gateway
        self.suffix = suffix
        self._cache: dict[str, str | None] = {}
        self._reverse: dict[str, str] = {}
        self._universe: list[str] | None = None

    def refresh(self) -> None:
        self._universe = self.gw.symbols()
        self._cache.clear()

    @property
    def universe(self) -> list[str]:
        if self._universe is None:
            self.refresh()
        return self._universe or []

    def resolve(self, canonical: str) -> str | None:
        """Return the broker symbol, or None if missing/ambiguous (never guesses)."""
        if canonical in self._cache:
            return self._cache[canonical]
        names = set(self.universe)
        result: str | None = None
        for cand in broker_candidates(canonical):
            if self.suffix and cand + self.suffix in names:
                result = cand + self.suffix
                break
            if cand in names:
                result = cand
                break
            suffixed = [n for n in names if n.upper().startswith(cand.upper())
                        and _SUFFIX_RE.match(n[len(cand):]) and n[len(cand):]]
            if len(suffixed) == 1:
                result = suffixed[0]
                break
            if len(suffixed) > 1:
                result = None    # ambiguous: several variants (e.g. .a and .r) - require config
                break
        if result is not None:
            self.gw.symbol_select(result)
            self._reverse[result] = canonical
        self._cache[canonical] = result
        return result

    def canonical_of(self, broker_symbol: str) -> str:
        if broker_symbol in self._reverse:
            return self._reverse[broker_symbol]
        base = broker_symbol
        if self.suffix and base.endswith(self.suffix):
            base = base[: -len(self.suffix)]
        c = canonicalize(base) or canonicalize(re.sub(r"[._\-+#].*$", "", base))
        return c or broker_symbol
