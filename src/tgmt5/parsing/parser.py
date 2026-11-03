"""Deterministic signal parser.

Design rules (see spec section 3):

* The parser never invents information.  Anything it cannot determine reliably
  produces :data:`MessageKind.AMBIGUOUS`, which routes the message to manual review.
* It is pure: text in, :class:`ParseResult` out.  No I/O, no market data.
  Market-dependent validation (entry deviation, spread...) belongs to the
  execution/risk layers.
* Message text is only ever matched against regular expressions; nothing in a
  message is evaluated or executed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from tgmt5.domain.models import (
    Direction, EntryType, ManagementInstruction, ManagementType, MessageKind,
    ParsedSignal, ParseResult,
)
from tgmt5.parsing.symbols import canonicalize

NUM = r"\d+(?:\.\d+)?"
_SEP = r"\s*[:=@\-–]?\s*"

_SL_RE = re.compile(
    rf"(?:\bS\s*/\s*L\b|\bSL\b|\bSTOP\s*-?\s*LOSS\b|\bSTOPLOSS\b|(?<!BUY )(?<!SELL )\bSTOP\b(?!\s*(?:LIMIT|ORDER)))"
    rf"{_SEP}({NUM})")
_TP_RE = re.compile(
    rf"(?:\bT\s*/\s*P|\bTP|\bTAKE\s*-?\s*PROFIT|\bTARGET)S?\s*(?:(\d)(?![\d.]))?\s*[:=@\-–.)]*\s*"
    rf"({NUM}(?:\s*(?:/|\||,|AND|&)\s*{NUM})*)")
_TP_OPEN_RE = re.compile(r"\bTP\s*\d?\s*[:=\-]?\s*OPEN\b")
_DIR_RE = re.compile(r"\b(BUY|SELL|LONG|SHORT)\b")
_ORDER_TYPE_RE = re.compile(r"\b(BUY|SELL)\s*(LIMIT|STOP)\b")
_MARKET_RE = re.compile(r"\b(NOW|MARKET|CMP|INSTANT|MKT|IMMEDIATELY)\b")
_ZONE_RE = re.compile(rf"(?<![\w.])({NUM})\s*(?:-|–|—|~|TO|/)\s*({NUM})(?![\w.])")
_NUM_RE = re.compile(rf"(?<![\w.#$])({NUM})(?![\w.%])")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_LOT_RISK_RE = re.compile(rf"\b(?:LOTS?|LOT\s*SIZE|RISK|VOLUME)\s*[:=]?\s*{NUM}\s*%?")
_PCT_RE = re.compile(rf"{NUM}\s*%")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_BAD_COMMA_RE = re.compile(r"\d,\d")
_TOKEN_RE = re.compile(r"[A-Z0-9&]{2,12}(?:/[A-Z]{3})?")

_PROMO_RE = re.compile(
    r"\b(JOIN|VIP|SUBSCRIBE|PROMO|DISCOUNT|OFFER|COURSE|MENTORSHIP|GIVEAWAY|REFERRAL|"
    r"RESULTS?|RECAP|SUMMARY|WEEKLY|MONTHLY|REPORT|YESTERDAY|LAST\s+WEEK|PIPS\s+PROFIT|"
    r"PIPS\s+SECURED|ANALYSIS|OUTLOOK|PREDICTION)\b")

# --- management ----------------------------------------------------------
_BE_RE = re.compile(
    r"(\b(SL|STOP\s*LOSS|STOPS?)\b.*\b(BE|B/E|BREAK\s*-?\s*EVEN|ENTRY|ENTRY\s*PRICE)\b)|\bBREAK\s*-?\s*EVEN\b|\bRISK\s*FREE\b")
_MOD_SL_RE = re.compile(
    rf"\b(MOVE|CHANGE|NEW|UPDATE|ADJUST|SET|MODIFY|TRAIL)\b.*?\b(SL|STOP\s*LOSS)\b\s*(?:TO|AT|@|:|=)?\s*({NUM})")
_MOD_TP_RE = re.compile(
    rf"\b(MOVE|CHANGE|NEW|UPDATE|ADJUST|SET|MODIFY)\b.*?\b(TP|TAKE\s*PROFIT)\b\s*(?:TO|AT|@|:|=)?\s*({NUM})")
_PARTIAL_RE = re.compile(rf"\b(CLOSE|SECURE|TAKE|BOOK)\b[^%\n]*?({NUM})\s*%")
_HALF_RE = re.compile(r"\b(CLOSE|SECURE|TAKE|BOOK)\s+(HALF)\b")
_CLOSE_RE = re.compile(r"\b(CLOSE|EXIT)\b(\s+(ALL|NOW|TRADES?|POSITIONS?|IT|EVERYTHING|FULL))?")
_CANCEL_RE = re.compile(r"\b(CANCEL|CANCELLED|DELETE|REMOVE)\b")
_TP_HIT_RE = re.compile(r"\bTP\s*(\d)?\s*(HIT|REACHED|DONE|SMASHED|✅)")
_SL_HIT_RE = re.compile(r"\b(SL|STOP\s*LOSS)\s*(HIT|REACHED|TRIGGERED)\b")


@dataclass(frozen=True)
class ParserConfig:
    """Per-source parser template."""
    symbol_aliases: dict[str, str] = field(default_factory=dict)
    implicit_market: bool = False          # treat "BUY GOLD + SL/TP" without price as market
    max_level_distance: float = 0.20       # SL/TP must be within 20% of entry (typo guard)
    max_zone_width: float = 0.02


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("—", "-").replace("–", "-").replace("−", "-")
    t = t.upper()
    t = _THOUSANDS_RE.sub("", t)
    # drop emojis / pictographs but keep ✅ which some providers use for "hit"
    t = "".join(ch if (ch == "✅" or ch == "\n" or not unicodedata.category(ch).startswith("S")
                       or ch in "@$&%/|~=:+-") else " " for ch in t)
    return t


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _find_symbols(text: str, aliases: dict[str, str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        canon = canonicalize(tok, aliases)
        if canon:
            found.add((canon, tok))
    # also catch "S&P 500" / "EUR / USD" style
    for m in re.finditer(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b", text):
        canon = canonicalize(m.group(1) + m.group(2), aliases)
        if canon:
            found.add((canon, m.group(0)))
    return found


class SignalParser:
    def __init__(self, config: ParserConfig | None = None) -> None:
        self.config = config or ParserConfig()

    # ------------------------------------------------------------------
    def parse(self, text: str) -> ParseResult:
        if not text or not text.strip():
            return ParseResult(MessageKind.NOISE, reasons=("empty",))
        norm = _normalize(text)
        if len(norm) > 4000:
            return ParseResult(MessageKind.NOISE, reasons=("too long",))

        directions = {_canon_dir(d) for d in _DIR_RE.findall(norm)}
        has_sl = bool(_SL_RE.search(norm))
        has_tp = bool(_TP_RE.search(norm))

        if directions and (has_sl or has_tp):
            if _PROMO_RE.search(norm):
                return ParseResult(MessageKind.NOISE, reasons=("promotional/historical wording",))
            return self._parse_signal(norm, directions)

        mgmt = self._parse_management(norm)
        if mgmt is not None:
            return mgmt

        if directions:
            symbols = _find_symbols(norm, self.config.symbol_aliases)
            if symbols and not _PROMO_RE.search(norm):
                return ParseResult(MessageKind.AMBIGUOUS,
                                   reasons=("direction and symbol present but no stop loss",))
        return ParseResult(MessageKind.NOISE, reasons=("no trading instruction recognised",))

    # ------------------------------------------------------------------
    def _parse_signal(self, norm: str, directions: set[Direction]) -> ParseResult:
        def ambiguous(*why: str) -> ParseResult:
            return ParseResult(MessageKind.AMBIGUOUS, reasons=why, confidence=0.0)

        if len(directions) != 1:
            return ambiguous("conflicting directions (both BUY and SELL)")
        direction = next(iter(directions))
        if _BAD_COMMA_RE.search(norm):
            return ambiguous("ambiguous comma in number")

        work = _TIME_RE.sub(" ", norm)
        work = _DATE_RE.sub(" ", work)
        work = _LOT_RISK_RE.sub(" ", work)

        # ---- SL
        spans: list[tuple[int, int]] = []
        sls: set[float] = set()
        for m in _SL_RE.finditer(work):
            sls.add(float(m.group(1)))
            spans.append(m.span())
        if not sls:
            return ambiguous("no stop loss")
        if len(sls) > 1:
            return ambiguous(f"multiple stop loss values {sorted(sls)}")
        sl = sls.pop()

        # ---- TPs ("TP3 open" = no fixed target; blank it before value extraction)
        open_spans = [m.span() for m in _TP_OPEN_RE.finditer(work)]
        work = _blank(work, open_spans)
        tp_by_index: dict[int, float] = {}
        tps_unindexed: list[float] = []
        for m in _TP_RE.finditer(work):
            spans.append(m.span())
            values = [float(x) for x in re.findall(NUM, m.group(2))]
            if m.group(1) is not None and len(values) == 1:
                idx = int(m.group(1))
                if idx in tp_by_index and tp_by_index[idx] != values[0]:
                    return ambiguous(f"TP{idx} given twice with different values")
                tp_by_index[idx] = values[0]
            else:
                tps_unindexed.extend(values)
        tps = [tp_by_index[k] for k in sorted(tp_by_index)] + tps_unindexed
        # dedupe preserving order
        seen: set[float] = set()
        tps = [x for x in tps if not (x in seen or seen.add(x))]

        rest = _blank(work, spans)

        # ---- symbol
        symbols = _find_symbols(rest, self.config.symbol_aliases)
        canon = {c for c, _ in symbols}
        if not canon:
            return ambiguous("no recognised symbol")
        if len(canon) > 1:
            return ambiguous(f"multiple symbols {sorted(canon)}")
        symbol = next(iter(canon))
        raw_symbol = next(iter(symbols))[1]
        for _, tok in symbols:
            rest = rest.replace(tok, " " * len(tok))

        # ---- entry
        explicit = _ORDER_TYPE_RE.search(rest)
        is_market = bool(_MARKET_RE.search(rest))
        rest_no_pct = _PCT_RE.sub(" ", rest)

        anchors = [sl, *tps]

        def plausible(x: float) -> bool:
            return any(abs(x - a) / max(a, 1e-9) <= self.config.max_level_distance for a in anchors)

        zones = [(float(a), float(b)) for a, b in _ZONE_RE.findall(rest_no_pct)
                 if plausible(float(a)) and plausible(float(b))]
        zone_spans = [m.span() for m in _ZONE_RE.finditer(rest_no_pct)]
        singles_text = _blank(rest_no_pct, zone_spans)
        singles = sorted({float(x) for x in _NUM_RE.findall(singles_text) if plausible(float(x))})

        entry_price = entry_low = entry_high = None
        if zones:
            if len(set(zones)) > 1 or singles:
                return ambiguous("multiple entry prices/zones")
            lo, hi = sorted(zones[0])
            if lo == hi:
                entry_price = lo
            else:
                if (hi - lo) / lo > self.config.max_zone_width:
                    return ambiguous("entry zone implausibly wide")
                entry_low, entry_high = lo, hi
        elif len(singles) == 1:
            entry_price = singles[0]
        elif len(singles) > 1:
            return ambiguous(f"multiple candidate entry prices {singles}")

        if explicit:
            if entry_price is None:
                return ambiguous("pending order without a single entry price")
            entry_type = EntryType.LIMIT if explicit.group(2) == "LIMIT" else EntryType.STOP
        elif entry_low is not None:
            entry_type = EntryType.ZONE
        elif entry_price is not None:
            entry_type = EntryType.MARKET if is_market else EntryType.PRICE
        elif is_market or self.config.implicit_market:
            entry_type = EntryType.MARKET
        else:
            return ambiguous("entry condition not stated (no price and no NOW/MARKET)")

        # ---- TPs required? (decided by execution settings; parser just reports)
        signal = ParsedSignal(
            direction=direction, symbol=symbol, entry_type=entry_type, stop_loss=sl,
            take_profits=tuple(tps), entry_price=entry_price, entry_low=entry_low,
            entry_high=entry_high, raw_symbol=raw_symbol,
        )
        problems = validate_geometry(signal, self.config.max_level_distance)
        if problems:
            return ambiguous(*problems)
        # order TPs from nearest to farthest
        ordered = tuple(sorted(signal.take_profits, key=lambda t: direction.sign * t))
        if ordered != signal.take_profits:
            signal = ParsedSignal(**{**_asdict(signal), "take_profits": ordered})
        return ParseResult(MessageKind.NEW_SIGNAL, signal=signal)

    # ------------------------------------------------------------------
    def _parse_management(self, norm: str) -> ParseResult | None:
        symbols = _find_symbols(norm, self.config.symbol_aliases)
        canon = {c for c, _ in symbols}
        if len(canon) > 1:
            sym = None
            multi = True
        else:
            sym = next(iter(canon)) if canon else None
            multi = False

        def mk(t: ManagementType, kind: MessageKind = MessageKind.MANAGEMENT, **kw) -> ParseResult:
            if multi:
                return ParseResult(MessageKind.AMBIGUOUS,
                                   reasons=("management message mentions several symbols",))
            return ParseResult(kind, management=ManagementInstruction(type=t, symbol=sym, **kw))

        if m := _TP_HIT_RE.search(norm):
            return mk(ManagementType.TP_HIT, MessageKind.INFO,
                      tp_index=int(m.group(1)) if m.group(1) else None)
        if _SL_HIT_RE.search(norm):
            return mk(ManagementType.SL_HIT, MessageKind.INFO)
        if _CANCEL_RE.search(norm):
            return mk(ManagementType.CANCEL_PENDING)
        if m := _PARTIAL_RE.search(norm):
            pct = float(m.group(2))
            if not 0 < pct <= 100:
                return ParseResult(MessageKind.AMBIGUOUS, reasons=("invalid close percentage",))
            if pct == 100:
                return mk(ManagementType.CLOSE_ALL)
            return mk(ManagementType.CLOSE_PARTIAL, fraction=pct / 100)
        if _HALF_RE.search(norm):
            return mk(ManagementType.CLOSE_PARTIAL, fraction=0.5)
        if m := _MOD_SL_RE.search(norm):
            return mk(ManagementType.MODIFY_SL, price=float(m.group(3)))
        if _BE_RE.search(norm):
            return mk(ManagementType.MOVE_SL_BE)
        if m := _MOD_TP_RE.search(norm):
            return mk(ManagementType.MODIFY_TP, price=float(m.group(3)))
        if _CLOSE_RE.search(norm) and not _PROMO_RE.search(norm):
            return mk(ManagementType.CLOSE_ALL)
        return None


def _canon_dir(word: str) -> Direction:
    return Direction.BUY if word in ("BUY", "LONG") else Direction.SELL


def _asdict(s: ParsedSignal) -> dict:
    return {f: getattr(s, f) for f in s.__slots__}


def validate_geometry(sig: ParsedSignal, max_dist: float = 0.20) -> list[str]:
    """Static consistency checks between direction, entry, SL and TPs."""
    problems: list[str] = []
    d = sig.direction.sign
    ref = sig.reference_entry
    if sig.entry_low is not None:
        # SL must be outside the whole zone
        worst = sig.entry_low if d > 0 else sig.entry_high
        if d * (worst - sig.stop_loss) <= 0:
            problems.append("stop loss inside or on the wrong side of the entry zone")
    elif ref is not None and d * (ref - sig.stop_loss) <= 0:
        problems.append("stop loss on the wrong side of entry")
    anchor = ref if ref is not None else sig.stop_loss
    for tp in sig.take_profits:
        if ref is not None and d * (tp - ref) <= 0:
            problems.append(f"take profit {tp} on the wrong side of entry")
        if d * (tp - sig.stop_loss) <= 0:
            problems.append(f"take profit {tp} not beyond stop loss")
        if abs(tp - anchor) / anchor > max_dist:
            problems.append(f"take profit {tp} implausibly far from entry")
    if ref is not None and abs(ref - sig.stop_loss) / ref > max_dist:
        problems.append("stop loss implausibly far from entry")
    if sig.stop_loss <= 0:
        problems.append("stop loss must be positive")
    return problems
