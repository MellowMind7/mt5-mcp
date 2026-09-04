"""Broker symbol resolution — turn what a trader says into what the broker calls it.

Every source project assumes the caller already knows the exact symbol name.
That assumption breaks on the first participant whose broker writes ``EURUSD.raw``
instead of ``EURUSD``, and it breaks *silently* in a worse way than expected.

The measured case that drove this design: on MetaQuotes-Demo the name ``GOLD``
exists — as *Barrick Gold Corporation*, a Nasdaq share. ``DAX`` exists as the
*Global X DAX Germany ETF*. ``DOW`` is *Dow Inc*. A plain "exact match first"
rule therefore does not fail loudly on this broker, it hands back a mining share
when the user asked for gold. So exact matches are *scored*, not trusted: a name
hit whose instrument class contradicts the request loses to a description hit
that agrees with it.

Nothing here hardcodes a broker's folder names. Instruments are classified by
``trade_calc_mode``, which the SDK reports the same way everywhere, so the
resolver learns each broker's universe at runtime instead of shipping a table
that is wrong for the next participant.

Own work. The need was identified from a measurement, not from a source project.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "Kind",
    "SymbolRecord",
    "Candidate",
    "Resolution",
    "SymbolResolver",
]


class Kind(str, Enum):
    """Instrument class, derived from ``trade_calc_mode``, not from folder names."""

    FOREX = "forex"
    METAL = "metal"
    INDEX = "index"
    CRYPTO = "crypto"
    STOCK = "stock"
    COMMODITY = "commodity"
    OTHER = "other"


# SYMBOL_CALC_MODE values that matter for classification. Names kept in English
# because they are MQL5 constants.
_CALC_FOREX = {0, 5}          # FOREX, FOREX_NO_LEVERAGE
_CALC_CFD = {2, 4}            # CFD, CFD_LEVERAGE
_CALC_CFDINDEX = {3}          # CFD_INDEX
_CALC_FUTURES = {1, 6}        # FUTURES, EXCH_FUTURES
_CALC_STOCKS = {32, 33, 34, 36, 37}  # EXCH_STOCKS and friends

# Metal base currencies are ISO 4217 and broker-independent.
_METAL_CODES = {"XAU": "Gold", "XAG": "Silver", "XPT": "Platinum", "XPD": "Palladium"}

_CRYPTO_CODES = {
    "BTC", "XBT", "ETH", "LTC", "XRP", "BCH", "ADA", "DOT", "SOL", "DOGE",
    "BNB", "AVAX", "MATIC", "LINK", "TRX", "XLM", "UNI", "ATOM",
}

_ISO_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "DKK",
    "PLN", "CZK", "HUF", "TRY", "ZAR", "MXN", "SGD", "HKD", "CNH", "CNY", "RUB",
    "THB", "ILS", "INR", "KRW", "BRL", "CLP", "COP", "PHP", "IDR", "MYR", "TWD",
    "AED", "SAR", "QAR", "RON", "BGN", "HRK", "ISK", "NGN", "KES", "EGP",
}

# Affixes brokers bolt onto an otherwise standard name. Order matters: longer
# first, so ``.cash`` is stripped before ``.c``.
_SUFFIXES = (
    ".cash", ".spot", ".raw", ".ecn", ".pro", ".stp", ".std", ".micro", ".mini",
    "_raw", "_ecn", "_pro", "_stp", "_i", "_m", "-raw", "-ecn", "-pro", "-cash",
    ".r", ".m", ".i", ".c", ".p", ".a", ".s", ".z", ".x",
    "micro", "mini", "cash", "ecn", "pro", "raw",
    "#", "+", "-", "_", ".", "m", "c", "i", "r", "z", "x", "e", "s",
)
_PREFIXES = ("#", "_", ".", "fx", "cfd", "spot")


@dataclass(frozen=True)
class SymbolRecord:
    """One broker symbol, reduced to what resolution needs."""

    name: str
    description: str
    path: str
    base: str
    profit: str
    calc_mode: int
    digits: int
    visible: bool
    kind: Kind
    stem: str
    """Name with broker affixes removed, uppercased. ``EURUSD.raw`` -> ``EURUSD``."""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.path.rsplit("\\", 1)[0] if "\\" in self.path else self.path,
            "kind": self.kind.value,
            "in_market_watch": self.visible,
        }


@dataclass
class Candidate:
    """A possible answer with the reason it scored the way it did."""

    record: SymbolRecord
    score: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {**self.record.as_dict(),
                "confidence": round(min(self.score, 1.0), 3),
                "reasons": self.reasons}


@dataclass
class Resolution:
    """Outcome of one lookup."""

    query: str
    best: Candidate | None
    candidates: list[Candidate]
    ambiguous: bool
    note: str = ""

    def as_dict(self) -> dict:
        out: dict[str, Any] = {
            "query": self.query,
            "resolved": self.best.record.name if self.best else None,
            "unambiguous": bool(self.best) and not self.ambiguous,
            "candidates": [c.as_dict() for c in self.candidates],
        }
        if self.note:
            out["note"] = self.note
        return out


# ---------------------------------------------------------------------------
# What the user might say
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Concept:
    """A broker-independent idea of an instrument, e.g. "the German index"."""

    key: str
    kind: Kind
    words: tuple[str, ...]
    """Description words that identify it, all lowercase."""
    codes: tuple[str, ...] = ()
    """Common ticker stems, tried as name hits."""
    require_all: tuple[str, ...] = ()
    """Words that must *all* appear in the description for a description hit."""


# Index concepts are matched on their description ("Germany 40 Index"), because
# the ticker differs per broker (DE40 / GER40 / DE30 / GER30 / DAX40) while the
# description is remarkably stable.
_CONCEPTS: tuple[Concept, ...] = (
    Concept("dax", Kind.INDEX, ("dax", "ger40", "ger30", "de40", "de30", "germany40"),
            ("DE40", "GER40", "DE30", "GER30", "DAX40", "GERMANY40"),
            require_all=("germany",)),
    Concept("dow", Kind.INDEX, ("dow", "dowjones", "us30", "wallstreet", "wall street"),
            ("US30", "DJ30", "WS30", "DOW30"), require_all=("30",)),
    Concept("sp500", Kind.INDEX, ("sp500", "s&p", "spx", "us500", "standard poor"),
            ("US500", "SPX500", "SP500", "SPX"), require_all=("500",)),
    Concept("nasdaq", Kind.INDEX, ("nas100", "nasdaq", "ustec", "ustech", "ndx"),
            ("USTEC", "NAS100", "NDX100", "USTECH100", "NASDAQ100"),
            require_all=("100",)),
    Concept("ftse", Kind.INDEX, ("ftse", "uk100", "footsie"), ("UK100", "FTSE100")),
    Concept("nikkei", Kind.INDEX, ("nikkei", "jpn225", "jp225"), ("JPN225", "JP225")),
    Concept("eurostoxx", Kind.INDEX, ("stoxx", "eustx", "euro stoxx"),
            ("EUSTX50", "STOXX50", "EU50")),
    Concept("cac", Kind.INDEX, ("cac", "fra40", "france40"), ("FRA40", "CAC40")),
    Concept("asx", Kind.INDEX, ("asx", "aus200"), ("AUS200", "ASX200")),
    Concept("swiss", Kind.INDEX, ("smi", "swi20", "switzerland20"), ("SWI20", "SMI20")),
    Concept("gold", Kind.METAL, ("gold", "xau"), ("XAUUSD", "GOLD")),
    Concept("silber", Kind.METAL, ("silber", "silver", "xag"), ("XAGUSD", "SILVER")),
    Concept("platin", Kind.METAL, ("platin", "platinum", "xpt"), ("XPTUSD",)),
    Concept("palladium", Kind.METAL, ("palladium", "xpd"), ("XPDUSD",)),
    Concept("oel", Kind.COMMODITY, ("oel", "öl", "oil", "wti", "crude", "brent"),
            ("WTI", "USOIL", "UKOIL", "BRENT", "XTIUSD", "XBRUSD", "CRUDOIL")),
    Concept("erdgas", Kind.COMMODITY, ("gas", "natgas", "erdgas", "natural gas"),
            ("NATGAS", "XNGUSD", "NGAS")),
    Concept("bitcoin", Kind.CRYPTO, ("bitcoin", "btc", "xbt"), ("BTCUSD", "XBTUSD")),
    Concept("ethereum", Kind.CRYPTO, ("ethereum", "eth", "ether"), ("ETHUSD",)),
)

_CONCEPT_BY_WORD: dict[str, Concept] = {}
for _c in _CONCEPTS:
    for _w in _c.words:
        _CONCEPT_BY_WORD.setdefault(_w, _c)


def _classify(calc_mode: int, base: str, profit: str, path: str) -> Kind:
    """Instrument class from SDK facts, with folder name as a last resort only."""
    base, profit = (base or "").upper(), (profit or "").upper()
    if base in _METAL_CODES:
        return Kind.METAL
    if base in _CRYPTO_CODES or profit in _CRYPTO_CODES:
        return Kind.CRYPTO
    if calc_mode in _CALC_STOCKS:
        return Kind.STOCK
    if calc_mode in _CALC_FOREX:
        # A "forex" calc mode with a non-currency base is a broker quirk, not a pair.
        if base in _ISO_CURRENCIES and profit in _ISO_CURRENCIES:
            return Kind.FOREX
        return Kind.OTHER
    if calc_mode in _CALC_CFDINDEX:
        return Kind.INDEX
    if calc_mode in _CALC_CFD or calc_mode in _CALC_FUTURES:
        head = path.split("\\", 1)[0].lower() if path else ""
        if any(w in head for w in ("index", "indices", "indizes", "cash")):
            return Kind.INDEX
        if any(w in head for w in ("metal", "metalle")):
            return Kind.METAL
        if any(w in head for w in ("energ", "commodit", "rohstoff", "oil")):
            return Kind.COMMODITY
        if any(w in head for w in ("crypto", "krypto")):
            return Kind.CRYPTO
        if any(w in head for w in ("stock", "aktien", "share", "equit")):
            return Kind.STOCK
        return Kind.OTHER
    return Kind.OTHER


# A bare trailing letter is only an affix when enough name is left to still be a
# symbol. Without this floor ``USTEC`` erodes to ``UST`` one letter at a time and
# stops matching anything, which is how the Nasdaq index went missing.
_MIN_STEM_AFTER_SINGLE_CHAR = 6
_MIN_STEM_AFTER_AFFIX = 3


def _stem(name: str) -> str:
    """Strip broker affixes so ``EURUSDm``/``EURUSD.raw`` both reduce to ``EURUSD``."""
    s = name.strip().upper()
    for pre in _PREFIXES:
        up = pre.upper()
        if len(s) > len(up) + 2 and s.startswith(up):
            s = s[len(up):]
            break
    changed = True
    while changed and len(s) > _MIN_STEM_AFTER_AFFIX:
        changed = False
        for suf in _SUFFIXES:
            up = suf.upper()
            floor = (_MIN_STEM_AFTER_SINGLE_CHAR if len(up) == 1 and up.isalpha()
                     else _MIN_STEM_AFTER_AFFIX)
            if len(s) - len(up) >= floor and s.endswith(up):
                s = s[: -len(up)]
                changed = True
                break
    return s


_MINI_WORDS = ("mini", "micro", "mikro")


def _is_mini(rec: SymbolRecord) -> bool:
    """Spot a reduced-size contract, e.g. ``US30M`` / ``S&P 500mini``.

    The description is the reliable signal; a bare trailing ``M`` on the name is
    only trusted when the plain stem also exists, because plenty of ordinary
    symbols end in M.
    """
    desc = rec.description.lower()
    if any(w in desc for w in _MINI_WORDS):
        return True
    return bool(re.search(r"(?:mini|micro)$", rec.name, re.IGNORECASE))


def _asks_for_mini(query_lower: str) -> bool:
    return any(w in query_lower for w in _MINI_WORDS)


def _split_pair(text: str) -> tuple[str, str] | None:
    """``EURUSD`` / ``EUR/USD`` / ``eur-usd`` -> ``("EUR", "USD")``."""
    cleaned = re.sub(r"[^A-Za-z]", "", text).upper()
    if len(cleaned) != 6:
        return None
    left, right = cleaned[:3], cleaned[3:]
    known = _ISO_CURRENCIES | set(_METAL_CODES) | _CRYPTO_CODES
    if left in known and right in known:
        return left, right
    return None


class SymbolResolver:
    """Learns the broker's symbol universe, then answers questions about it."""

    def __init__(self, connection) -> None:
        self._conn = connection
        self._lock = threading.RLock()
        self._records: list[SymbolRecord] = []
        self._by_name: dict[str, SymbolRecord] = {}
        self._by_stem: dict[str, list[SymbolRecord]] = {}
        self._loaded = False
        self._cache: dict[str, Resolution] = {}

    # -- learning ---------------------------------------------------------

    def load(self, *, force: bool = False) -> int:
        """Read every symbol the broker offers and classify it."""
        with self._lock:
            if self._loaded and not force:
                return len(self._records)
            import MetaTrader5 as mt5

            raw = self._conn.call(mt5.symbols_get) or []
            records: list[SymbolRecord] = []
            for s in raw:
                base = getattr(s, "currency_base", "") or ""
                profit = getattr(s, "currency_profit", "") or ""
                path = getattr(s, "path", "") or ""
                # Not `or -1`: SYMBOL_CALC_MODE_FOREX is 0, and a falsy-zero
                # fallback here silently reclassifies every currency pair.
                raw_calc = getattr(s, "trade_calc_mode", None)
                calc = int(raw_calc) if raw_calc is not None else -1
                records.append(SymbolRecord(
                    name=s.name,
                    description=getattr(s, "description", "") or "",
                    path=path,
                    base=base.upper(),
                    profit=profit.upper(),
                    calc_mode=calc,
                    digits=int(getattr(s, "digits", 0) or 0),
                    visible=bool(getattr(s, "visible", False)),
                    kind=_classify(calc, base, profit, path),
                    stem=_stem(s.name),
                ))
            self._records = records
            self._by_name = {r.name.upper(): r for r in records}
            by_stem: dict[str, list[SymbolRecord]] = {}
            for r in records:
                by_stem.setdefault(r.stem, []).append(r)
            self._by_stem = by_stem
            self._cache.clear()
            self._loaded = True
            log.info("loaded %d symbols in %d groups", len(records),
                     len({r.path.split("\\", 1)[0] for r in records}))
            return len(records)

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    @property
    def records(self) -> list[SymbolRecord]:
        self._ensure()
        return self._records

    # -- resolution -------------------------------------------------------

    def resolve(self, query: str, *, kind_hint: Kind | None = None,
                limit: int = 5) -> Resolution:
        """Translate a user's word into this broker's symbol name."""
        self._ensure()
        key = f"{query.strip().upper()}|{kind_hint.value if kind_hint else ''}"
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        result = self._resolve_uncached(query, kind_hint, limit)
        with self._lock:
            self._cache[key] = result
        return result

    def _resolve_uncached(self, query: str, kind_hint: Kind | None,
                          limit: int) -> Resolution:
        q = query.strip()
        if not q:
            return Resolution(query, None, [], False, "Empty query.")

        upper = q.upper()
        flat = re.sub(r"[^A-Z0-9]", "", upper)
        lower = q.lower()
        concept = _CONCEPT_BY_WORD.get(lower) or _CONCEPT_BY_WORD.get(
            re.sub(r"[^a-z0-9]", "", lower))
        pair = _split_pair(q)
        wanted = kind_hint or (concept.kind if concept else None)
        if pair and wanted is None:
            wanted = Kind.METAL if pair[0] in _METAL_CODES else (
                Kind.CRYPTO if pair[0] in _CRYPTO_CODES or pair[1] in _CRYPTO_CODES
                else Kind.FOREX)

        scored: dict[str, Candidate] = {}

        def bump(rec: SymbolRecord, points: float, why: str) -> None:
            slot = scored.get(rec.name)
            if slot is None:
                slot = Candidate(rec, 0.0, [])
                scored[rec.name] = slot
            slot.score += points
            slot.reasons.append(why)

        # 1. exact name — strong, but only a starting point
        exact = self._by_name.get(upper)
        if exact is not None:
            bump(exact, 0.70, "exact name match")

        # 2. same name after stripping the broker's affixes.
        #    Guarded by length: "Öl" flattens to "L" once the umlaut is dropped,
        #    and a single letter matches whatever share happens to be named that.
        for rec in (self._by_stem.get(flat, ()) if len(flat) >= 3 else ()):
            if rec.name.upper() != upper:
                bump(rec, 0.62, f"name matches once broker affixes are stripped: {rec.name} -> {rec.stem}")

        # 3. currency pair, in either notation
        if pair:
            target = pair[0] + pair[1]
            for rec in self._records:
                if rec.stem == target or (rec.base == pair[0] and rec.profit == pair[1]):
                    bump(rec, 0.60, f"currency pair {pair[0]}/{pair[1]}")

        # 4. concept: known tickers, then description.
        #    A record must score a code hit at most once — the stem index and the
        #    name index overlap, and double counting let a share ride two bonuses
        #    past the penalty meant to hold it down.
        if concept is not None:
            hit_by_code: set[str] = set()
            for code in concept.codes:
                matches = list(self._by_stem.get(code, ()))
                direct = self._by_name.get(code)
                if direct is not None:
                    matches.append(direct)
                for rec in matches:
                    if rec.name in hit_by_code:
                        continue
                    hit_by_code.add(rec.name)
                    bump(rec, 0.55, f"known ticker spelling for {concept.key}")
            for rec in self._records:
                desc = rec.description.lower()
                if not desc:
                    continue
                # Brokers write "US Tech 100 Index" and "Germany 40 Index";
                # matching the de-spaced form too lets one concept word cover
                # both "ustech" and "us tech" without listing every variant.
                squashed = re.sub(r"[^a-z0-9]", "", desc)
                hit = any(w in desc or w in squashed for w in concept.words)
                ok = (not concept.require_all
                      or all(w in desc or w in squashed
                             for w in concept.require_all))
                if hit and ok:
                    bump(rec, 0.45, f"description matches: {rec.description!r}")

        # 5. description contains the query. Leading position counts for more:
        #    "Apple Inc" is the thing itself, "T-Rex 2X Long Apple" is about it.
        if len(flat) >= 3:
            for rec in self._records:
                desc = rec.description.lower()
                if not desc or lower not in desc:
                    continue
                if desc.startswith(lower):
                    bump(rec, 0.35, f"description starts with {q!r}")
                else:
                    bump(rec, 0.20, f"description contains {q!r}")

        # 6. class agreement — this is what defeats the GOLD/Barrick trap
        if wanted is not None:
            for cand in scored.values():
                if cand.record.kind is wanted:
                    cand.score += 0.30
                    cand.reasons.append(f"instrument class matches: {wanted.value}")
                elif cand.record.kind is Kind.STOCK:
                    # A share when a metal or index was meant is the classic
                    # silent mistake. Push it below anything that agrees.
                    cand.score -= 0.80
                    cand.reasons.append(
                        f"penalised: this is a share ({cand.record.description!r}) "
                        f"but a {wanted.value} was asked for")
                else:
                    cand.score -= 0.15
                    cand.reasons.append(
                        f"instrument class differs: {cand.record.kind.value}")

        # 7. small nudges: Market Watch, plain contract over the mini one
        for cand in scored.values():
            if cand.record.visible:
                cand.score += 0.05
                cand.reasons.append("already in Market Watch")
            if _is_mini(cand.record) and not _asks_for_mini(lower):
                # US30 and US30M are both "the Dow". Unless the user said mini,
                # they mean the full contract — a tenth-size position is a
                # quiet way to make every later risk figure wrong.
                cand.score -= 0.25
                cand.reasons.append("penalised: mini/micro contract")
            cand.score -= 0.01 * max(0, len(cand.record.name) - 6)

        # 8. fuzzy, only when nothing else spoke up
        if not scored:
            names = list(self._by_name)
            for hit in difflib.get_close_matches(upper, names, n=limit, cutoff=0.75):
                rec = self._by_name[hit]
                ratio = difflib.SequenceMatcher(None, upper, hit).ratio()
                bump(rec, 0.30 * ratio, f"similar name ({ratio:.0%})")

        ranked = sorted(scored.values(), key=lambda c: c.score, reverse=True)
        ranked = [c for c in ranked if c.score > 0][:limit]
        if not ranked:
            return Resolution(
                query, None, [], False,
                f"No symbol found for {q!r}. Try symbol_search with part of the "
                f"name, or look in the terminal's Market Watch.")

        best = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None
        ambiguous = runner is not None and (best.score - runner.score) < 0.15
        note = ""
        if ambiguous:
            note = (f"Ambiguous: {best.record.name!r} and "
                    f"{runner.record.name!r} score almost the same. "
                    f"Be more specific, or pick one from the candidate list.")
        return Resolution(query, best, ranked, ambiguous, note)

    def resolve_name(self, query: str, *, kind_hint: Kind | None = None) -> str:
        """Resolve to a single name or raise with a German explanation."""
        res = self.resolve(query, kind_hint=kind_hint)
        if res.best is None:
            raise ValueError(res.note or f"Symbol {query!r} not found.")
        if res.ambiguous:
            raise ValueError(res.note)
        return res.best.record.name

    # -- search -----------------------------------------------------------

    def search(self, text: str, *, kind: Kind | None = None,
               limit: int = 25) -> list[SymbolRecord]:
        """Free-text search over names and descriptions."""
        self._ensure()
        needle = text.strip().lower()
        if not needle:
            return []
        hits: list[tuple[float, SymbolRecord]] = []
        for rec in self._records:
            if kind is not None and rec.kind is not kind:
                continue
            name = rec.name.lower()
            desc = rec.description.lower()
            if needle == name:
                rank = 3.0
            elif name.startswith(needle):
                rank = 2.0
            elif needle in name:
                rank = 1.5
            elif needle in desc:
                rank = 1.0
            else:
                continue
            if rec.visible:
                rank += 0.2
            hits.append((rank, rec))
        hits.sort(key=lambda p: (-p[0], len(p[1].name), p[1].name))
        return [r for _, r in hits[:limit]]

    def groups(self) -> dict[str, dict]:
        """How this broker names things — feeds the ``mt5://symbole`` resource."""
        self._ensure()
        out: dict[str, dict] = {}
        for rec in self._records:
            head = rec.path.split("\\", 1)[0] if rec.path else "(no group)"
            slot = out.setdefault(head, {"count": 0, "kinds": {}, "examples": []})
            slot["count"] += 1
            slot["kinds"][rec.kind.value] = slot["kinds"].get(rec.kind.value, 0) + 1
            if len(slot["examples"]) < 5:
                slot["examples"].append(rec.name)
        return out

    def naming_scheme(self) -> dict:
        """Detect the broker's affix habit, e.g. every pair ends in ``.raw``."""
        self._ensure()
        pairs = [r for r in self._records if r.kind is Kind.FOREX]
        affixes: dict[str, int] = {}
        for rec in pairs:
            extra = rec.name.upper().replace(rec.stem, "", 1)
            affixes[extra or "(no affix)"] = affixes.get(extra or "(no affix)", 0) + 1
        ranked = sorted(affixes.items(), key=lambda kv: kv[1], reverse=True)
        examples = {}
        for concept_key, hint in (("EURUSD", Kind.FOREX), ("gold", Kind.METAL),
                                  ("dax", Kind.INDEX), ("dow", Kind.INDEX),
                                  ("sp500", Kind.INDEX), ("nasdaq", Kind.INDEX)):
            res = self.resolve(concept_key, kind_hint=hint)
            examples[concept_key] = res.best.record.name if res.best else None
        return {
            "currency_pairs_total": len(pairs),
            "name_affixes": dict(ranked[:6]),
            "how_they_are_named_here": examples,
        }
