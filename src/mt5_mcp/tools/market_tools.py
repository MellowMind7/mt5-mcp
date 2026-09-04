"""Market data: bars_get, ticks_get, quote_get, data_availability.

The paging layer exists because ``copy_rates_*`` is capped by the terminal's
``MaxBars`` chart setting. That ceiling is a *user preference*: it defaults to
100000 but can be set to 5000, or to unlimited. Guessing a constant is wrong in
both directions — too high fails the whole request with "Invalid params", too low
silently truncates the user's history. So the ceiling is read or probed once, and
longer ranges are stitched together from several calls.

Provenance: bar/tick retrieval shape follows
https://github.com/FXDavid-OffbeatForex/MBT (MIT, (c) 2026 davidcyberdash) and
https://github.com/ariadng/metatrader-mcp-server (MIT, (c) 2025 Aria Dhanang).
MBT caps every request at 2000 bars; the paging and the dual-clock output here
are own work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..core.models import (AvailabilityResult, Bar, BarsResult, QuoteResult,
                           Tick, TicksResult)
from ..core.timeutil import utc_to_broker_naive
from ._common import (ToolFailure, conn, ensure_selected, last_error_text,
                      resolve_symbol, timeframe_or_fail, triple)

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

#: Used when the terminal reports no ceiling at all. Large enough not to page
#: needlessly, small enough to stay well inside any sane limit.
_UNLIMITED_CHUNK = 100_000

_MAX_BARS_RETURNED = 20_000
_MAX_TICKS_RETURNED = 50_000


def _parse_when(value: str | None, *, default: datetime | None = None) -> datetime | None:
    """Parse a user date/time as UTC. Accepts 'YYYY-MM-DD' and ISO forms."""
    if value in (None, ""):
        return default
    text = str(value).strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ToolFailure(f"Could not read the date {value!r}.",
                      "Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")


def _chunk_size() -> int:
    ceiling = conn().max_bars()
    if ceiling is None:
        return _UNLIMITED_CHUNK
    # Stay one bar under: the terminal rejects exactly-at-the-limit requests.
    return max(1000, int(ceiling) - 1)


def register(mcp) -> None:
    @mcp.tool(
        name="bars_get",
        description=(
            "Historical candles for one symbol and timeframe. Either the last "
            "N bars, or a date range. Longer ranges are fetched in several "
            "requests automatically and stitched together, so you do not have "
            "to care about the terminal's per-request limit. Every timestamp "
            "comes back in broker time, your display timezone and UTC."),
        annotations=READ_ONLY,
    )
    def bars_get(symbol: str, timeframe: str = "H1", count: int = 200,
                 start: str | None = None, end: str | None = None) -> BarsResult:
        """Fetch candles.

        Args:
            symbol: Symbol name; resolved automatically.
            timeframe: M1, M5, M15, M30, H1, H4, D1, W1, MN1 and the rest.
            count: How many bars, counting back from the most recent. Ignored
                when both start and end are given.
            start: Optional range start, e.g. "2026-01-01".
            end: Optional range end. Defaults to now when only start is given.
        """
        name = resolve_symbol(symbol)
        ensure_selected(name)
        tf_name, tf = timeframe_or_fail(timeframe)
        offset = conn().clock.offset
        chunk = _chunk_size()
        requests = 0
        rows: list = []
        note = None

        if start:
            frm = _parse_when(start)
            to = _parse_when(end, default=datetime.now(timezone.utc))
            if frm >= to:
                raise ToolFailure("The start date is not before the end date.",
                                  "Swap them, or leave 'end' empty for 'now'.")
            # Walk forward in windows so a multi-year M1 range still works.
            cursor = frm
            while cursor < to:
                requests += 1
                batch = conn().call(
                    mt5.copy_rates_range, name, tf,
                    utc_to_broker_naive(cursor, offset),
                    utc_to_broker_naive(to, offset))
                if batch is None or len(batch) == 0:
                    break
                rows.extend(batch if not rows else
                            [b for b in batch if b["time"] > rows[-1]["time"]])
                if len(batch) < chunk:
                    break
                last = rows[-1]["time"]
                cursor = conn().clock.to_utc(last) + timedelta(seconds=1)
                if len(rows) >= _MAX_BARS_RETURNED:
                    note = (f"Stopped at {_MAX_BARS_RETURNED} bars. Narrow the "
                            f"range or use a larger timeframe.")
                    break
        else:
            if count <= 0:
                raise ToolFailure("count must be greater than zero.",
                                  "Ask for at least one bar.")
            wanted = min(int(count), _MAX_BARS_RETURNED)
            position = 0
            while len(rows) < wanted:
                take = min(chunk, wanted - len(rows))
                requests += 1
                batch = conn().call(mt5.copy_rates_from_pos, name, tf,
                                    position, take)
                if batch is None or len(batch) == 0:
                    break
                rows = list(batch) + rows
                if len(batch) < take:
                    break
                position += take
            if count > _MAX_BARS_RETURNED:
                note = (f"Capped at {_MAX_BARS_RETURNED} bars per call to keep "
                        f"the response readable.")

        if not rows:
            raise ToolFailure(
                f"No candles for {name} on {tf_name}.",
                f"{last_error_text()} The broker may not keep history that far "
                f"back for this timeframe.")

        rows = sorted({int(r["time"]): r for r in rows}.values(),
                      key=lambda r: r["time"])
        bars = [Bar(time=triple(int(r["time"])), open=float(r["open"]),
                    high=float(r["high"]), low=float(r["low"]),
                    close=float(r["close"]), tick_volume=int(r["tick_volume"]),
                    spread=int(r["spread"]) if "spread" in r.dtype.names else None,
                    real_volume=int(r["real_volume"])
                    if "real_volume" in r.dtype.names else None)
                for r in rows]
        return BarsResult(symbol=name, timeframe=tf_name, count=len(bars),
                          bars=bars, truncated=bool(note), note=note,
                          requests_used=requests)

    @mcp.tool(
        name="ticks_get",
        description=(
            "Raw ticks for one symbol over a time range. Use this for spread "
            "analysis or to see exactly what happened around one moment; for "
            "anything longer than a few hours prefer bars_get."),
        annotations=READ_ONLY,
    )
    def ticks_get(symbol: str, start: str, end: str | None = None,
                  count: int = 1000, info_only: bool = False) -> TicksResult:
        """Fetch ticks in a range.

        Args:
            symbol: Symbol name; resolved automatically.
            start: Range start, e.g. "2026-09-04 10:00:00".
            end: Range end. Defaults to now.
            count: Maximum ticks to return.
            info_only: Only ticks that changed bid/ask, skipping trade ticks.
        """
        name = resolve_symbol(symbol)
        ensure_selected(name)
        offset = conn().clock.offset
        frm = _parse_when(start)
        to = _parse_when(end, default=datetime.now(timezone.utc))
        if frm >= to:
            raise ToolFailure("The start time is not before the end time.",
                              "Swap them, or leave 'end' empty for 'now'.")
        flags = mt5.COPY_TICKS_INFO if info_only else mt5.COPY_TICKS_ALL
        rows = conn().call(mt5.copy_ticks_range, name,
                           utc_to_broker_naive(frm, offset),
                           utc_to_broker_naive(to, offset), flags)
        if rows is None or len(rows) == 0:
            raise ToolFailure(
                f"No ticks for {name} in that range.",
                f"{last_error_text()} Tick history is usually much shorter than "
                f"bar history.")
        limit = min(int(count), _MAX_TICKS_RETURNED)
        truncated = len(rows) > limit
        rows = rows[-limit:]
        ticks = [Tick(time=triple(int(r["time"])), bid=float(r["bid"]),
                      ask=float(r["ask"]),
                      last=float(r["last"]) if "last" in r.dtype.names else None,
                      volume=int(r["volume"]) if "volume" in r.dtype.names else None)
                 for r in rows]
        return TicksResult(symbol=name, count=len(ticks), ticks=ticks,
                           truncated=truncated)

    @mcp.tool(
        name="quote_get",
        description=(
            "The current bid, ask and spread for one symbol, with the age of "
            "the quote. A quote that is minutes old usually means the market "
            "is closed rather than that something is broken."),
        annotations=READ_ONLY,
    )
    def quote_get(symbol: str) -> QuoteResult:
        name = resolve_symbol(symbol)
        ensure_selected(name)
        tick = conn().call(mt5.symbol_info_tick, name)
        if tick is None or not tick.time:
            raise ToolFailure(
                f"No quote available for {name}.",
                "The market is probably closed. symbol_is_tradable_now says "
                "which of the two it is.")
        info = conn().call(mt5.symbol_info, name)
        point = float(getattr(info, "point", 0.0) or 0.0)
        spread = ((tick.ask - tick.bid) / point) if point else 0.0
        age = (datetime.now(timezone.utc)
               - conn().clock.to_utc(tick.time)).total_seconds()
        return QuoteResult(symbol=name, bid=float(tick.bid), ask=float(tick.ask),
                           spread_points=round(spread, 1), time=triple(tick.time),
                           seconds_since_tick=round(age, 1))

    @mcp.tool(
        name="data_availability",
        description=(
            "How much history this broker actually has for one symbol and "
            "timeframe, and the terminal's per-request bar ceiling. Check this "
            "before designing a backtest — brokers differ enormously in how far "
            "back they go."),
        annotations=READ_ONLY,
    )
    def data_availability(symbol: str, timeframe: str = "D1") -> AvailabilityResult:
        name = resolve_symbol(symbol)
        ensure_selected(name)
        tf_name, tf = timeframe_or_fail(timeframe)
        ceiling = conn().max_bars()
        probe = ceiling - 1 if ceiling else _UNLIMITED_CHUNK
        rows = conn().call(mt5.copy_rates_from_pos, name, tf, 0, probe)
        if rows is None or len(rows) == 0:
            return AvailabilityResult(symbol=name, timeframe=tf_name,
                                      bars_available=0,
                                      max_bars_per_request=ceiling)
        return AvailabilityResult(
            symbol=name, timeframe=tf_name,
            first_bar=triple(int(rows[0]["time"])),
            last_bar=triple(int(rows[-1]["time"])),
            bars_available=len(rows), max_bars_per_request=ceiling)
