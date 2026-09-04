"""Trading: trade_market, trade_pending, trade_modify, trade_close,
trade_close_batch, trade_cancel.

Every one of these defaults to ``dry_run=True``. A dry run runs the whole
pipeline — resolve, size, guard, ``order_check`` — and stops one step before
``order_send``. The response looks the same either way, so an assistant that
forgets to flip the flag produces a preview rather than a fill.

Every attempt, including refusals and dry runs, is appended to ``trades.log``.

Provenance: execution and guardrail flow follows
https://github.com/FXDavid-OffbeatForex/MTX (MIT, (c) 2026 davidcyberdash),
commit 908ca9b, core/trade.py; the batch/close shape follows
https://github.com/ariadng/metatrader-mcp-server (MIT, (c) 2025 Aria Dhanang),
commit bd2ff59. dry-run-by-default, the order_check fold-in and the filling
selection are own work.
"""

from __future__ import annotations

import logging

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..config import get_settings
from ..core.errors import describe_retcode, is_success_retcode
from ..core.guard import GuardBlocked, TradeIntent, get_guard
from ..core.models import (BatchCloseResult, GuardVerdict, OrderPreview,
                           TradeResult)
from ._common import (FILLING_NAMES, ORDER_TYPE_NAMES, POSITION_TYPE_NAMES,
                      ToolFailure, conn, ensure_selected, normalise_volume,
                      pick_filling_mode, resolve_symbol, symbol_or_fail, triple)

log = logging.getLogger(__name__)

DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

_PENDING_TYPES = {
    "buy_limit": mt5.ORDER_TYPE_BUY_LIMIT, "sell_limit": mt5.ORDER_TYPE_SELL_LIMIT,
    "buy_stop": mt5.ORDER_TYPE_BUY_STOP, "sell_stop": mt5.ORDER_TYPE_SELL_STOP,
    "buy_stop_limit": mt5.ORDER_TYPE_BUY_STOP_LIMIT,
    "sell_stop_limit": mt5.ORDER_TYPE_SELL_STOP_LIMIT,
}


def _preview(request: dict) -> OrderPreview | None:
    """Run order_check and translate its verdict."""
    checked = conn().call(mt5.order_check, request)
    if checked is None:
        return None
    code = int(checked.retcode)
    # order_check answers 0 for "this would be accepted". Zero is not a trade
    # server return code and is absent from the table, so name it here rather
    # than reporting a clean pre-flight check as UNKNOWN.
    name = "CHECK_OK" if code == 0 else describe_retcode(code).name
    return OrderPreview(
        retcode=code, retcode_name=name,
        comment=checked.comment or "",
        margin_required=float(getattr(checked, "margin", 0.0) or 0.0),
        free_margin_after=float(getattr(checked, "margin_free", 0.0) or 0.0),
        balance=float(getattr(checked, "balance", 0.0) or 0.0),
        equity=float(getattr(checked, "equity", 0.0) or 0.0),
        would_succeed=code == 0)


def _send(request: dict, intent: TradeIntent, *, action: str, symbol: str,
          order_type: str, volume: float, price: float | None,
          sl: float | None, tp: float | None, verdict: GuardVerdict,
          filling_label: str | None) -> TradeResult:
    """Preview, then send unless this is a dry run. Always logs."""
    guard = get_guard()
    preview = _preview(request)

    if intent.dry_run:
        guard.record(intent, request, "DRY_RUN", None)
        message = "Dry run — nothing was sent."
        fix = None
        if preview and not preview.would_succeed:
            error = describe_retcode(preview.retcode)
            message = (f"Dry run — the broker would reject this: "
                       f"{error.meaning}")
            fix = error.fix
        elif preview:
            message = ("Dry run — this order would go through. Call again with "
                       "dry_run=false to send it.")
        return TradeResult(
            ok=bool(preview and preview.would_succeed), dry_run=True,
            action=action, symbol=symbol, order_type=order_type, volume=volume,
            price=price, sl=sl, tp=tp, message=message, fix=fix, guard=verdict,
            preview=preview, filling_mode=filling_label)

    if preview is not None and not preview.would_succeed:
        error = describe_retcode(preview.retcode)
        guard.record(intent, request, f"PRECHECK_FAILED {preview.retcode}",
                     preview.retcode)
        return TradeResult(
            ok=False, dry_run=False, action=action, symbol=symbol,
            order_type=order_type, volume=volume, price=price, sl=sl, tp=tp,
            retcode=preview.retcode, retcode_name=error.name,
            message=f"Not sent — the pre-flight check failed: {error.meaning}",
            fix=error.fix, guard=verdict, preview=preview,
            filling_mode=filling_label)

    result = conn().call(mt5.order_send, request)
    if result is None:
        guard.record(intent, request, "NO_RESULT", None)
        raise ToolFailure("The terminal returned nothing for order_send.",
                          "Check that the terminal is still running, then run "
                          "mt5_doctor.")
    retcode = int(result.retcode)
    error = describe_retcode(retcode)
    ok = is_success_retcode(retcode)
    guard.record(intent, request, f"retcode={retcode} {error.name}", retcode)
    return TradeResult(
        ok=ok, dry_run=False, action=action, symbol=symbol,
        order_type=order_type, volume=float(getattr(result, "volume", volume)),
        price=float(getattr(result, "price", 0.0)) or price, sl=sl, tp=tp,
        ticket=int(getattr(result, "order", 0)) or None,
        order=int(getattr(result, "order", 0)) or None,
        deal=int(getattr(result, "deal", 0)) or None,
        retcode=retcode, retcode_name=error.name,
        message=error.meaning if not ok else "Done.",
        fix=None if ok else error.fix, guard=verdict, preview=preview,
        filling_mode=filling_label, time=triple(None))


def _blocked(exc: GuardBlocked, action: str, symbol: str, order_type: str,
             volume: float) -> TradeResult:
    return TradeResult(
        ok=False, dry_run=exc.verdict.dry_run, action=action, symbol=symbol,
        order_type=order_type, volume=volume,
        message=f"Blocked by the safety rail: {exc.verdict.reason}",
        fix=exc.verdict.fix, guard=exc.verdict)


def register(mcp) -> None:
    settings = get_settings()

    @mcp.tool(
        name="trade_market",
        description=(
            "Open a position at the current market price. Defaults to a DRY "
            "RUN: it resolves the symbol, applies the safety rail and runs the "
            "broker's own pre-flight check, then reports what would happen "
            "without sending anything. Pass dry_run=false to actually trade."),
        annotations=DESTRUCTIVE,
    )
    def trade_market(symbol: str, side: str, volume: float,
                     sl: float | None = None, tp: float | None = None,
                     deviation: int = 20, comment: str = "mt5-mcp",
                     dry_run: bool = True) -> TradeResult:
        """Buy or sell at market.

        Args:
            symbol: Symbol name; resolved automatically.
            side: "buy" or "sell".
            volume: Lot size. Use risk_position_size to derive it.
            sl: Optional stop-loss price.
            tp: Optional take-profit price.
            deviation: Maximum slippage in points.
            comment: Order comment, max 31 characters.
            dry_run: True (default) previews without sending.
        """
        direction = side.strip().lower()
        if direction not in ("buy", "sell"):
            raise ToolFailure(f"Unknown side {side!r}.", "Use 'buy' or 'sell'.")
        name, info = symbol_or_fail(symbol)
        volume, notes = normalise_volume(info, volume)

        intent = TradeIntent(action="open", symbol=name, volume=volume,
                             dry_run=dry_run)
        try:
            verdict = get_guard().require(intent)
        except GuardBlocked as exc:
            return _blocked(exc, "open", name, direction, volume)

        tick = conn().call(mt5.symbol_info_tick, name)
        if tick is None or not tick.time:
            raise ToolFailure(f"No current price for {name}.",
                              "The market is probably closed — check with "
                              "symbol_is_tradable_now.")
        price = float(tick.ask if direction == "buy" else tick.bid)
        kind = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        filling, label = pick_filling_mode(info, pending=False)

        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": volume,
            "type": kind, "price": price, "deviation": int(deviation),
            "magic": settings.magic, "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        if sl:
            request["sl"] = round(float(sl), int(info.digits))
        if tp:
            request["tp"] = round(float(tp), int(info.digits))

        result = _send(request, intent, action="open", symbol=name,
                       order_type=direction, volume=volume, price=price,
                       sl=sl, tp=tp, verdict=verdict, filling_label=label)
        if notes:
            result.message += " (" + "; ".join(notes) + ")"
        return result

    @mcp.tool(
        name="trade_pending",
        description=(
            "Place a pending order: buy_limit, sell_limit, buy_stop, "
            "sell_stop, buy_stop_limit or sell_stop_limit. Defaults to a DRY "
            "RUN. Pass dry_run=false to actually place it."),
        annotations=DESTRUCTIVE,
    )
    def trade_pending(symbol: str, order_type: str, volume: float, price: float,
                      stoplimit: float | None = None, sl: float | None = None,
                      tp: float | None = None, comment: str = "mt5-mcp",
                      dry_run: bool = True) -> TradeResult:
        """Place a resting order.

        Args:
            symbol: Symbol name; resolved automatically.
            order_type: buy_limit, sell_limit, buy_stop, sell_stop,
                buy_stop_limit or sell_stop_limit.
            volume: Lot size.
            price: Trigger price.
            stoplimit: Limit price, required for the stop_limit types.
            sl: Optional stop-loss price.
            tp: Optional take-profit price.
            comment: Order comment.
            dry_run: True (default) previews without sending.
        """
        key = order_type.strip().lower()
        kind = _PENDING_TYPES.get(key)
        if kind is None:
            raise ToolFailure(f"Unknown pending order type {order_type!r}.",
                              "Use one of: " + ", ".join(_PENDING_TYPES))
        name, info = symbol_or_fail(symbol)
        volume, notes = normalise_volume(info, volume)

        intent = TradeIntent(action="open", symbol=name, volume=volume,
                             dry_run=dry_run)
        try:
            verdict = get_guard().require(intent)
        except GuardBlocked as exc:
            return _blocked(exc, "pending", name, key, volume)

        filling, label = pick_filling_mode(info, pending=True)
        digits = int(info.digits)
        request = {
            "action": mt5.TRADE_ACTION_PENDING, "symbol": name, "volume": volume,
            "type": kind, "price": round(float(price), digits),
            "magic": settings.magic, "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        if stoplimit:
            request["stoplimit"] = round(float(stoplimit), digits)
        elif key.endswith("stop_limit"):
            raise ToolFailure(f"{key} needs a stoplimit price.",
                              "Pass stoplimit as well as price.")
        if sl:
            request["sl"] = round(float(sl), digits)
        if tp:
            request["tp"] = round(float(tp), digits)

        result = _send(request, intent, action="pending", symbol=name,
                       order_type=key, volume=volume, price=float(price),
                       sl=sl, tp=tp, verdict=verdict, filling_label=label)
        if notes:
            result.message += " (" + "; ".join(notes) + ")"
        return result

    @mcp.tool(
        name="trade_modify",
        description=(
            "Change the stop loss or take profit of an open position, or of a "
            "pending order. Defaults to a DRY RUN."),
        annotations=DESTRUCTIVE,
    )
    def trade_modify(ticket: int, sl: float | None = None,
                     tp: float | None = None, price: float | None = None,
                     include_foreign: bool = False,
                     dry_run: bool = True) -> TradeResult:
        """Modify a position's or order's levels.

        Args:
            ticket: Position or order ticket.
            sl: New stop-loss price. Pass 0 to remove.
            tp: New take-profit price. Pass 0 to remove.
            price: New trigger price, pending orders only.
            include_foreign: Allow modifying something this server did not open.
            dry_run: True (default) previews without sending.
        """
        if sl is None and tp is None and price is None:
            raise ToolFailure("Nothing to change.", "Pass sl, tp or price.")

        guard = get_guard()
        positions = list(conn().call(mt5.positions_get, ticket=int(ticket)) or [])
        orders = list(conn().call(mt5.orders_get, ticket=int(ticket)) or [])
        row = positions[0] if positions else (orders[0] if orders else None)
        if row is None:
            raise ToolFailure(f"No position or order with ticket {ticket}.",
                              "Check positions_get and orders_get.")
        is_position = bool(positions)

        if not guard.owns(row.magic) and not include_foreign:
            verdict = GuardVerdict(
                allowed=False, dry_run=dry_run, blocked_by="foreign_position",
                reason=f"Ticket {ticket} was not opened by this server "
                       f"(magic {row.magic}).",
                fix="Pass include_foreign=true if you really mean to touch a "
                    "position you opened by hand or with another tool.")
            guard.record(TradeIntent(action="modify", symbol=row.symbol,
                                     dry_run=dry_run),
                         None, "BLOCKED foreign_position", None)
            return TradeResult(ok=False, dry_run=dry_run, action="modify",
                               symbol=row.symbol, order_type="modify",
                               volume=float(getattr(row, "volume", 0.0) or 0.0),
                               message=verdict.reason, fix=verdict.fix,
                               guard=verdict)

        intent = TradeIntent(action="modify", symbol=row.symbol,
                             include_foreign=include_foreign, dry_run=dry_run)
        try:
            verdict = guard.require(intent)
        except GuardBlocked as exc:
            return _blocked(exc, "modify", row.symbol, "modify", 0.0)

        info = conn().call(mt5.symbol_info, row.symbol)
        digits = int(info.digits) if info else 5
        if is_position:
            request = {
                "action": mt5.TRADE_ACTION_SLTP, "position": int(ticket),
                "symbol": row.symbol,
                "sl": round(float(sl), digits) if sl is not None else float(row.sl),
                "tp": round(float(tp), digits) if tp is not None else float(row.tp),
            }
        else:
            request = {
                "action": mt5.TRADE_ACTION_MODIFY, "order": int(ticket),
                "symbol": row.symbol,
                "price": round(float(price), digits) if price is not None
                else float(row.price_open),
                "sl": round(float(sl), digits) if sl is not None else float(row.sl),
                "tp": round(float(tp), digits) if tp is not None else float(row.tp),
                "type_time": mt5.ORDER_TIME_GTC,
            }
        return _send(request, intent, action="modify", symbol=row.symbol,
                     order_type="position" if is_position else "order",
                     volume=float(getattr(row, "volume", 0.0) or 0.0),
                     price=request.get("price"), sl=request.get("sl"),
                     tp=request.get("tp"), verdict=verdict, filling_label=None)

    @mcp.tool(
        name="trade_close",
        description=(
            "Close one open position, fully or partially. Defaults to a DRY "
            "RUN. Positions this server did not open need include_foreign=true."),
        annotations=DESTRUCTIVE,
    )
    def trade_close(ticket: int, volume: float | None = None,
                    deviation: int = 20, include_foreign: bool = False,
                    dry_run: bool = True) -> TradeResult:
        """Close a position.

        Args:
            ticket: Position ticket.
            volume: Partial volume to close. Defaults to the whole position.
            deviation: Maximum slippage in points.
            include_foreign: Allow closing a position this server did not open.
            dry_run: True (default) previews without sending.
        """
        result = _close_one(int(ticket), volume, deviation, include_foreign,
                            dry_run, settings.magic)
        return result

    @mcp.tool(
        name="trade_close_batch",
        description=(
            "Close several positions at once. scope='symbol' closes everything "
            "in one symbol, scope='profitable' or 'losing' filters by result, "
            "scope='all' closes everything and additionally requires "
            "confirm=\"CLOSE ALL\". Defaults to a DRY RUN."),
        annotations=DESTRUCTIVE,
    )
    def trade_close_batch(scope: str = "symbol", symbol: str | None = None,
                          confirm: str | None = None,
                          include_foreign: bool = False,
                          dry_run: bool = True) -> BatchCloseResult:
        """Close a group of positions.

        Args:
            scope: "symbol", "profitable", "losing" or "all".
            symbol: Required when scope is "symbol".
            confirm: Must be "CLOSE ALL" when scope is "all".
            include_foreign: Include positions this server did not open.
            dry_run: True (default) previews without sending.
        """
        guard = get_guard()
        key = scope.strip().lower()
        if key not in ("symbol", "profitable", "losing", "all"):
            raise ToolFailure(f"Unknown scope {scope!r}.",
                              "Use symbol, profitable, losing or all.")

        intent = TradeIntent(action="close_batch", scope=key, confirm=confirm,
                             include_foreign=include_foreign, dry_run=dry_run)
        try:
            verdict = guard.require(intent)
        except GuardBlocked as exc:
            return BatchCloseResult(
                ok=False, dry_run=dry_run, requested=0, closed=0, failed=0,
                guard=exc.verdict,
                message=f"Blocked by the safety rail: {exc.verdict.reason}")

        rows = list(conn().call(mt5.positions_get) or [])
        if key == "symbol":
            if not symbol:
                raise ToolFailure("scope='symbol' needs a symbol.",
                                  "Pass the symbol, or use another scope.")
            name = resolve_symbol(symbol)
            rows = [r for r in rows if r.symbol == name]
        elif key == "profitable":
            rows = [r for r in rows if float(r.profit) > 0]
        elif key == "losing":
            rows = [r for r in rows if float(r.profit) < 0]

        rows, withheld = guard.filter_ours(rows, include_foreign)
        results = [_close_one(int(r.ticket), None, 20, include_foreign, dry_run,
                              settings.magic) for r in rows]
        closed = sum(1 for r in results if r.ok)
        failed = len(results) - closed
        message = (f"{len(results)} position(s) matched, {closed} would close."
                   if dry_run else
                   f"{closed} of {len(results)} position(s) closed.")
        if withheld:
            message += (f" {withheld} position(s) skipped because this server "
                        f"did not open them; pass include_foreign=true to "
                        f"include them.")
        return BatchCloseResult(
            ok=failed == 0, dry_run=dry_run, requested=len(results),
            closed=closed, failed=failed, results=results, guard=verdict,
            message=message)

    @mcp.tool(
        name="trade_cancel",
        description="Cancel a pending order that has not triggered yet. Defaults to a DRY RUN.",
        annotations=DESTRUCTIVE,
    )
    def trade_cancel(ticket: int, include_foreign: bool = False,
                     dry_run: bool = True) -> TradeResult:
        guard = get_guard()
        rows = list(conn().call(mt5.orders_get, ticket=int(ticket)) or [])
        if not rows:
            raise ToolFailure(f"No pending order with ticket {ticket}.",
                              "Use orders_get to see the current tickets.")
        row = rows[0]
        if not guard.owns(row.magic) and not include_foreign:
            verdict = GuardVerdict(
                allowed=False, dry_run=dry_run, blocked_by="foreign_order",
                reason=f"Order {ticket} was not placed by this server.",
                fix="Pass include_foreign=true to cancel it anyway.")
            return TradeResult(ok=False, dry_run=dry_run, action="cancel",
                               symbol=row.symbol, order_type="cancel",
                               volume=float(row.volume_current),
                               message=verdict.reason, fix=verdict.fix,
                               guard=verdict)

        intent = TradeIntent(action="cancel", symbol=row.symbol,
                             include_foreign=include_foreign, dry_run=dry_run)
        try:
            verdict = guard.require(intent)
        except GuardBlocked as exc:
            return _blocked(exc, "cancel", row.symbol, "cancel",
                            float(row.volume_current))
        request = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)}
        return _send(request, intent, action="cancel", symbol=row.symbol,
                     order_type="cancel", volume=float(row.volume_current),
                     price=None, sl=None, tp=None, verdict=verdict,
                     filling_label=None)


def _close_one(ticket: int, volume: float | None, deviation: int,
               include_foreign: bool, dry_run: bool, magic: int) -> TradeResult:
    """Shared close path for trade_close and trade_close_batch."""
    guard = get_guard()
    rows = list(conn().call(mt5.positions_get, ticket=ticket) or [])
    if not rows:
        raise ToolFailure(f"No open position with ticket {ticket}.",
                          "Use positions_get to see the current tickets — it "
                          "may already be closed.")
    row = rows[0]
    side = POSITION_TYPE_NAMES.get(int(row.type), "?")

    if not guard.owns(row.magic) and not include_foreign:
        verdict = GuardVerdict(
            allowed=False, dry_run=dry_run, blocked_by="foreign_position",
            reason=f"Position {ticket} was not opened by this server "
                   f"(magic {row.magic}).",
            fix="Pass include_foreign=true to close a position you opened by "
                "hand or with another tool.")
        guard.record(TradeIntent(action="close", symbol=row.symbol,
                                 dry_run=dry_run),
                     None, "BLOCKED foreign_position", None)
        return TradeResult(ok=False, dry_run=dry_run, action="close",
                           symbol=row.symbol, order_type=side,
                           volume=float(row.volume), message=verdict.reason,
                           fix=verdict.fix, guard=verdict)

    intent = TradeIntent(action="close", symbol=row.symbol,
                         volume=float(row.volume),
                         include_foreign=include_foreign, dry_run=dry_run)
    try:
        verdict = guard.require(intent)
    except GuardBlocked as exc:
        return _blocked(exc, "close", row.symbol, side, float(row.volume))

    ensure_selected(row.symbol)
    info = conn().call(mt5.symbol_info, row.symbol)
    tick = conn().call(mt5.symbol_info_tick, row.symbol)
    if tick is None or not tick.time:
        raise ToolFailure(f"No current price for {row.symbol}.",
                          "The market is probably closed — a position cannot "
                          "be closed while its market is shut.")

    close_volume = float(row.volume) if volume is None else float(volume)
    close_volume, _notes = normalise_volume(info, min(close_volume,
                                                      float(row.volume)))
    is_long = int(row.type) == mt5.POSITION_TYPE_BUY
    kind = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
    price = float(tick.bid if is_long else tick.ask)
    filling, label = pick_filling_mode(info, pending=False)

    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": row.symbol,
        "volume": close_volume, "type": kind, "position": int(ticket),
        "price": price, "deviation": int(deviation), "magic": magic,
        "comment": "mt5-mcp close", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    return _send(request, intent, action="close", symbol=row.symbol,
                 order_type=side, volume=close_volume, price=price,
                 sl=None, tp=None, verdict=verdict, filling_label=label)
