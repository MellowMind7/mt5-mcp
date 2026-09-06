"""Symbol tools: symbol_resolve, symbol_search, symbol_info, symbol_select,
symbol_is_tradable_now.

Own work — no source project offered symbol resolution.
"""

from __future__ import annotations

from datetime import datetime, timezone

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..core.models import (ResolveResult, SymbolBrief, SymbolCandidate,
                           SymbolSpec, TradabilityResult)
from ..core.symbols import Kind
from ._common import (EXECUTION_MODE_NAMES, TRADE_MODE_NAMES, ToolFailure, conn,
                      ensure_selected, resolve_symbol, resolver,
                      supported_fillings, triple)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITE_SAFE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

_KIND_VALUES = {k.value: k for k in Kind}


def _spec(name: str, info) -> SymbolSpec:
    record = next((r for r in resolver().records if r.name == name), None)
    return SymbolSpec(
        name=info.name,
        description=getattr(info, "description", "") or "",
        group=(info.path.rsplit("\\", 1)[0] if getattr(info, "path", "") else ""),
        kind=record.kind.value if record else "",
        digits=int(info.digits),
        point=float(info.point),
        contract_size=float(info.trade_contract_size),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
        stops_level_points=int(info.trade_stops_level),
        freeze_level_points=int(info.trade_freeze_level),
        tick_size=float(info.trade_tick_size),
        tick_value=float(info.trade_tick_value),
        tick_value_profit=float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0),
        tick_value_loss=float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0),
        currency_base=getattr(info, "currency_base", "") or "",
        currency_profit=getattr(info, "currency_profit", "") or "",
        currency_margin=getattr(info, "currency_margin", "") or "",
        filling_modes=supported_fillings(info),
        execution_mode=EXECUTION_MODE_NAMES.get(int(info.trade_exemode), "?"),
        trade_mode=TRADE_MODE_NAMES.get(int(info.trade_mode), "?"),
        expires=bool(getattr(info, "expiration_time", 0)),
        expiration=(triple(int(info.expiration_time)).broker
                    if getattr(info, "expiration_time", 0) else None),
        in_market_watch=bool(getattr(info, "visible", False)),
        spread_points=int(getattr(info, "spread", 0) or 0),
    )


def register(mcp) -> None:
    @mcp.tool(
        name="symbol_resolve",
        description=(
            "Translate what a trader says into what this broker calls it. "
            "Handles broker suffixes (EURUSD.raw, EURUSDm), common names "
            "(Gold, DAX, Dow, Nasdaq) and currency-pair notation (EUR/USD). "
            "Use this before any other tool that takes a symbol. It also "
            "protects against a trap: on many brokers a plain name like GOLD "
            "or DAX is an unrelated *share*, and an exact-match lookup would "
            "silently return the wrong instrument."),
        annotations=READ_ONLY,
    )
    def symbol_resolve(query: str, kind: str | None = None,
                       limit: int = 5) -> ResolveResult:
        """Resolve one user-supplied name.

        Args:
            query: What the user said, e.g. "Gold", "DAX", "eur/usd".
            kind: Optional class hint: forex, metal, index, crypto, stock,
                commodity. Narrows an ambiguous query.
            limit: How many candidates to return.
        """
        hint = None
        if kind:
            hint = _KIND_VALUES.get(kind.strip().lower())
            if hint is None:
                raise ToolFailure(
                    f"Unknown kind {kind!r}.",
                    "Use one of: " + ", ".join(_KIND_VALUES))
        result = resolver().resolve(query, kind_hint=hint, limit=limit)
        return ResolveResult(
            query=result.query,
            resolved=result.best.record.name if result.best else None,
            unambiguous=bool(result.best) and not result.ambiguous,
            candidates=[SymbolCandidate(**c.as_dict()) for c in result.candidates],
            note=result.note or None,
        )

    @mcp.tool(
        name="symbol_search",
        description=(
            "Free-text search across this broker's symbol names and "
            "descriptions. Use it when symbol_resolve finds nothing, or to "
            "explore what a broker offers."),
        annotations=READ_ONLY,
    )
    def symbol_search(text: str, kind: str | None = None,
                      limit: int = 25) -> list[SymbolBrief]:
        """Search symbols by substring.

        Args:
            text: Part of a name or description.
            kind: Optional class filter: forex, metal, index, crypto, stock,
                commodity.
            limit: Maximum rows.
        """
        wanted = _KIND_VALUES.get(kind.strip().lower()) if kind else None
        if kind and wanted is None:
            raise ToolFailure(f"Unknown kind {kind!r}.",
                              "Use one of: " + ", ".join(_KIND_VALUES))
        hits = resolver().search(text, kind=wanted, limit=limit)
        return [SymbolBrief(**r.as_dict()) for r in hits]

    @mcp.tool(
        name="symbol_info",
        description=(
            "Full trading specification for one symbol: digits, point, "
            "contract size, volume limits and step, stops level, tick value, "
            "supported filling modes and execution mode. These are the numbers "
            "position sizing and order sending depend on."),
        annotations=READ_ONLY,
    )
    def symbol_info(symbol: str) -> SymbolSpec:
        name = resolve_symbol(symbol)
        ensure_selected(name)
        info = conn().call(mt5.symbol_info, name)
        if info is None:
            raise ToolFailure(f"No specification available for {name!r}.",
                              "Check the name with symbol_search.")
        return _spec(name, info)

    @mcp.tool(
        name="symbol_select",
        description=(
            "Add a symbol to Market Watch, or remove it. A symbol that is not "
            "in Market Watch returns no quotes and cannot be traded, so most "
            "tools do this automatically — this is for tidying up."),
        annotations=WRITE_SAFE,
    )
    def symbol_select(symbol: str, enable: bool = True) -> dict:
        """Show or hide a symbol in Market Watch.

        Args:
            symbol: Symbol name, resolved automatically.
            enable: True to add, False to remove.
        """
        name = resolve_symbol(symbol)
        ok = conn().call(mt5.symbol_select, name, enable)
        if not ok:
            raise ToolFailure(
                f"Could not {'add' if enable else 'remove'} {name!r}.",
                "A symbol cannot be removed while a chart, position or order "
                "is using it.")
        return {"ok": True, "symbol": name, "in_market_watch": enable}

    @mcp.tool(
        name="symbol_is_tradable_now",
        description=(
            "Can this symbol be traded at this moment? Checks the broker's "
            "trade mode, the terminal's algo-trading switch, and whether ticks "
            "are actually arriving. Use it before trying to open a position, "
            "to tell 'market closed' apart from a real problem."),
        annotations=READ_ONLY,
    )
    def symbol_is_tradable_now(symbol: str) -> TradabilityResult:
        name = resolve_symbol(symbol)
        ensure_selected(name)
        info = conn().call(mt5.symbol_info, name)
        if info is None:
            raise ToolFailure(f"No specification available for {name!r}.",
                              "Check the name with symbol_search.")

        mode = int(info.trade_mode)
        if mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            return TradabilityResult(
                symbol=name, tradable=False,
                reason="The broker has trading disabled for this symbol.")

        tick = conn().call(mt5.symbol_info_tick, name)
        age = None
        stamp = None
        if tick is not None and tick.time:
            stamp = triple(tick.time)
            age = (datetime.now(timezone.utc)
                   - conn().clock.to_utc(tick.time)).total_seconds()

        terminal = conn().call(mt5.terminal_info)
        if terminal is not None and not terminal.trade_allowed:
            return TradabilityResult(
                symbol=name, tradable=False,
                reason="Algorithmic trading is switched off in the terminal. "
                       "Click «Algo Trading» in the toolbar.",
                last_tick=stamp, seconds_since_tick=age)

        if mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
            return TradabilityResult(
                symbol=name, tradable=False,
                reason="This symbol is close-only right now; no new positions.",
                last_tick=stamp, seconds_since_tick=age)

        # A quote older than a few minutes on a symbol whose session should be
        # open is the usual sign of a closed market, and it is a far more common
        # cause of a failed order than anything else.
        if age is None:
            return TradabilityResult(
                symbol=name, tradable=False,
                reason="No quote has arrived for this symbol. The market is "
                       "probably closed.",
                session_open=False)
        stale = age > 300
        limits = []
        if mode == mt5.SYMBOL_TRADE_MODE_LONGONLY:
            limits.append("long positions only")
        if mode == mt5.SYMBOL_TRADE_MODE_SHORTONLY:
            limits.append("short positions only")
        reason = ("Tradable." if not limits else "Tradable, " + ", ".join(limits))
        if stale:
            reason = (f"The last quote is {age / 60:.0f} minutes old — the market "
                      f"is most likely closed.")
        return TradabilityResult(
            symbol=name, tradable=not stale, reason=reason,
            session_open=not stale, last_tick=stamp, seconds_since_tick=age)
