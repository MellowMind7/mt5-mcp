"""Shared helpers for every tool module.

The filling-mode logic here is the part worth reading. Getting it wrong is how
you earn retcode 10030, and the mapping is not the obvious one.

Own work, informed by the MQL5 documentation and measurements against a live
terminal (see BEFUND.md).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

import MetaTrader5 as mt5

from ..config import get_settings
from ..core.connection import Mt5Connection, get_connection
from ..core.errors import describe_last_error
from ..core.models import TimeTriple
from ..core.symbols import Kind, SymbolResolver
from ..core.timeutil import broker_naive_to_utc

log = logging.getLogger(__name__)

__all__ = [
    "conn", "resolver", "triple", "resolve_symbol", "ensure_selected",
    "symbol_or_fail", "pick_filling_mode", "FILLING_NAMES", "TIMEFRAMES",
    "timeframe_or_fail", "ORDER_TYPE_NAMES", "POSITION_TYPE_NAMES",
    "DEAL_ENTRY_NAMES", "TRADE_MODE_NAMES", "EXECUTION_MODE_NAMES",
    "last_error_text", "ToolFailure", "round_to_step", "normalise_volume",
]

_resolver: SymbolResolver | None = None


class ToolFailure(RuntimeError):
    """A user-facing failure carrying a fix sentence."""

    def __init__(self, message: str, fix: str | None = None,
                 code: int | None = None, code_name: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code
        self.code_name = code_name

    def as_dict(self) -> dict:
        return {"ok": False, "error": str(self), "fix": self.fix,
                "code": self.code, "code_name": self.code_name}


def conn() -> Mt5Connection:
    connection = get_connection()
    connection.ensure_connected()
    return connection


def resolver() -> SymbolResolver:
    global _resolver
    if _resolver is None:
        _resolver = SymbolResolver(get_connection())
    return _resolver


def last_error_text() -> str:
    err = describe_last_error(mt5.last_error())
    return f"{err.meaning} {err.fix}"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def triple(value: datetime | int | float | None) -> TimeTriple | None:
    """Render a broker timestamp in all three clocks."""
    if value is None:
        return None
    connection = get_connection()
    offset = connection.clock.offset
    utc = broker_naive_to_utc(value, offset)
    broker = utc + offset
    from ..core.timeutil import BERLIN
    display_tz = BERLIN
    name = get_settings().timezone_display
    if name and name != "Europe/Berlin":
        try:
            from zoneinfo import ZoneInfo
            display_tz = ZoneInfo(name)
        except Exception:  # noqa: BLE001 - a bad tz name must not break a read
            log.warning("unknown MT5_TIMEZONE_DISPLAY %r, using Europe/Berlin", name)
    return TimeTriple(
        broker=broker.replace(tzinfo=None).isoformat(sep=" "),
        display=utc.astimezone(display_tz).replace(tzinfo=None).isoformat(sep=" "),
        utc=utc.replace(tzinfo=None).isoformat(sep=" "),
    )


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


def resolve_symbol(name: str, *, kind_hint: Kind | None = None) -> str:
    """Translate a user's word into this broker's symbol name, or fail loudly."""
    res = resolver().resolve(name, kind_hint=kind_hint)
    if res.best is None:
        raise ToolFailure(
            f"No symbol matches {name!r} at this broker.",
            res.note or "Use symbol_search with part of the name to see what "
                        "this broker offers.")
    if res.ambiguous:
        options = ", ".join(c.record.name for c in res.candidates[:4])
        raise ToolFailure(
            f"{name!r} is ambiguous at this broker.",
            f"Candidates: {options}. Call symbol_resolve to see the reasoning, "
            f"then pass the exact name.")
    return res.best.record.name


def ensure_selected(name: str) -> None:
    """Put a symbol into Market Watch.

    Everything else — quotes, ticks, order sends — silently returns nothing for
    a symbol that is not selected, so this runs before every read and write.
    """
    info = conn().call(mt5.symbol_info, name)
    if info is not None and getattr(info, "visible", False):
        return
    if not conn().call(mt5.symbol_select, name, True):
        raise ToolFailure(
            f"Could not add {name!r} to Market Watch.",
            "The symbol may not exist at this broker or may be disabled for "
            "your account. Try symbol_search to find the right name.")


def symbol_or_fail(name: str):
    """Resolve, select, and return the SDK's symbol info in one step."""
    resolved = resolve_symbol(name)
    ensure_selected(resolved)
    info = conn().call(mt5.symbol_info, resolved)
    if info is None:
        raise ToolFailure(f"symbol_info({resolved!r}) returned nothing.",
                          last_error_text())
    return resolved, info


# ---------------------------------------------------------------------------
# Filling mode
# ---------------------------------------------------------------------------

# The symbol's `filling_mode` is a bitmask, and it does NOT use the same numbers
# as the order's `type_filling`. Mapping, straight from the MQL5 docs:
#
#     SYMBOL_FILLING_FOK = 1  ->  ORDER_FILLING_FOK = 0
#     SYMBOL_FILLING_IOC = 2  ->  ORDER_FILLING_IOC = 1
#     SYMBOL_FILLING_BOC = 4  ->  ORDER_FILLING_BOC = 3
#
# ORDER_FILLING_RETURN = 2 never appears in the mask. That is not an omission:
# RETURN is always permitted *except* under Market execution. So a symbol
# reporting filling_mode == 0 is not broken and has not "no filling mode" — it
# simply supports RETURN only, which is why the execution mode has to be
# consulted rather than the mask alone.
_MASK_TO_ORDER = (
    (1, mt5.ORDER_FILLING_FOK, "FOK"),
    (2, mt5.ORDER_FILLING_IOC, "IOC"),
    (4, mt5.ORDER_FILLING_BOC, "BOC"),
)

FILLING_NAMES = {
    mt5.ORDER_FILLING_FOK: "FOK",
    mt5.ORDER_FILLING_IOC: "IOC",
    mt5.ORDER_FILLING_RETURN: "RETURN",
    mt5.ORDER_FILLING_BOC: "BOC",
}


def supported_fillings(info) -> list[str]:
    """Human-readable list of what a symbol accepts."""
    mask = int(getattr(info, "filling_mode", 0) or 0)
    names = [label for bit, _order, label in _MASK_TO_ORDER if mask & bit]
    if int(getattr(info, "trade_exemode", 0)) != mt5.SYMBOL_TRADE_EXECUTION_MARKET:
        names.append("RETURN")
    return names


def pick_filling_mode(info, *, pending: bool = False) -> tuple[int, str]:
    """Choose a filling mode this symbol will actually accept.

    Returns ``(ORDER_FILLING_* value, label)``.

    Under Market execution the mask is binding and one of its bits must be used.
    Everywhere else RETURN is the safe fallback, which is what makes a symbol
    with an empty mask tradable at all.
    """
    mask = int(getattr(info, "filling_mode", 0) or 0)
    exemode = int(getattr(info, "trade_exemode", 0) or 0)

    # BOC ("book or cancel") only makes sense for resting orders; never pick it
    # for a market order, where it would be rejected outright.
    order = [(bit, value, label) for bit, value, label in _MASK_TO_ORDER
             if label != "BOC" or pending]

    if exemode == mt5.SYMBOL_TRADE_EXECUTION_MARKET:
        for bit, value, label in order:
            if mask & bit:
                return value, label
        # Market execution with an empty mask should not happen; FOK is the
        # least surprising guess and the retcode will say so if it is wrong.
        return mt5.ORDER_FILLING_FOK, "FOK"

    if pending:
        return mt5.ORDER_FILLING_RETURN, "RETURN"
    for bit, value, label in order:
        if mask & bit:
            return value, label
    return mt5.ORDER_FILLING_RETURN, "RETURN"


# ---------------------------------------------------------------------------
# Enum names
# ---------------------------------------------------------------------------

TIMEFRAMES: dict[str, int] = {
    "M1": mt5.TIMEFRAME_M1, "M2": mt5.TIMEFRAME_M2, "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4, "M5": mt5.TIMEFRAME_M5, "M6": mt5.TIMEFRAME_M6,
    "M10": mt5.TIMEFRAME_M10, "M12": mt5.TIMEFRAME_M12, "M15": mt5.TIMEFRAME_M15,
    "M20": mt5.TIMEFRAME_M20, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2, "H3": mt5.TIMEFRAME_H3, "H4": mt5.TIMEFRAME_H4,
    "H6": mt5.TIMEFRAME_H6, "H8": mt5.TIMEFRAME_H8, "H12": mt5.TIMEFRAME_H12,
    "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1,
}

_TF_ALIASES = {"1M": "M1", "5M": "M5", "15M": "M15", "30M": "M30",
               "1H": "H1", "4H": "H4", "1D": "D1", "DAILY": "D1",
               "1W": "W1", "WEEKLY": "W1", "MONTHLY": "MN1", "MN": "MN1"}


def timeframe_or_fail(name: str) -> tuple[str, int]:
    key = str(name).strip().upper()
    key = _TF_ALIASES.get(key, key)
    if key not in TIMEFRAMES:
        raise ToolFailure(
            f"Unknown timeframe {name!r}.",
            "Use one of: " + ", ".join(TIMEFRAMES))
    return key, TIMEFRAMES[key]


ORDER_TYPE_NAMES = {
    mt5.ORDER_TYPE_BUY: "buy", mt5.ORDER_TYPE_SELL: "sell",
    mt5.ORDER_TYPE_BUY_LIMIT: "buy_limit", mt5.ORDER_TYPE_SELL_LIMIT: "sell_limit",
    mt5.ORDER_TYPE_BUY_STOP: "buy_stop", mt5.ORDER_TYPE_SELL_STOP: "sell_stop",
    mt5.ORDER_TYPE_BUY_STOP_LIMIT: "buy_stop_limit",
    mt5.ORDER_TYPE_SELL_STOP_LIMIT: "sell_stop_limit",
}
POSITION_TYPE_NAMES = {mt5.POSITION_TYPE_BUY: "buy", mt5.POSITION_TYPE_SELL: "sell"}
DEAL_ENTRY_NAMES = {mt5.DEAL_ENTRY_IN: "in", mt5.DEAL_ENTRY_OUT: "out",
                    2: "inout", 3: "out_by"}
TRADE_MODE_NAMES = {
    mt5.SYMBOL_TRADE_MODE_DISABLED: "disabled",
    mt5.SYMBOL_TRADE_MODE_LONGONLY: "long_only",
    mt5.SYMBOL_TRADE_MODE_SHORTONLY: "short_only",
    mt5.SYMBOL_TRADE_MODE_CLOSEONLY: "close_only",
    mt5.SYMBOL_TRADE_MODE_FULL: "full",
}
EXECUTION_MODE_NAMES = {
    mt5.SYMBOL_TRADE_EXECUTION_REQUEST: "request",
    mt5.SYMBOL_TRADE_EXECUTION_INSTANT: "instant",
    mt5.SYMBOL_TRADE_EXECUTION_MARKET: "market",
    mt5.SYMBOL_TRADE_EXECUTION_EXCHANGE: "exchange",
}


# ---------------------------------------------------------------------------
# Volume arithmetic
# ---------------------------------------------------------------------------


def round_to_step(value: float, step: float) -> float:
    """Round down to a whole number of steps.

    Rounding *down* is deliberate: rounding a position size up would quietly
    put more money at risk than the caller asked for.
    """
    if step <= 0:
        return float(value)
    steps = int(round(float(value) / step + 1e-9))
    if steps * step > value + 1e-12:
        steps -= 1
    # volume_step is decimal (0.01, 0.1, 1.0); derive the precision from it so
    # 0.1 * 3 does not come back as 0.30000000000000004.
    decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))
    return round(max(steps, 0) * step, decimals)


def normalise_volume(info, volume: float) -> tuple[float, list[str]]:
    """Clamp a lot size into what the symbol allows. Returns notes explaining any change."""
    notes: list[str] = []
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    vmin = float(getattr(info, "volume_min", step) or step)
    vmax = float(getattr(info, "volume_max", 0) or 0)
    out = round_to_step(float(volume), step)
    if out < vmin:
        notes.append(f"raised to the symbol minimum of {vmin}")
        out = vmin
    if vmax and out > vmax:
        notes.append(f"capped at the symbol maximum of {vmax}")
        out = round_to_step(vmax, step)
    return out, notes
