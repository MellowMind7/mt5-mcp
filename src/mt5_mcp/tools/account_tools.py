"""Positions, orders, history and one position in detail.

Provenance: tool shape follows
https://github.com/ariadng/metatrader-mcp-server (MIT, (c) 2025 Aria Dhanang),
commit bd2ff59, and
https://github.com/FXDavid-OffbeatForex/MTX (MIT, (c) 2026 davidcyberdash),
commit 908ca9b, core/positions.py. The R-multiple and ownership marking are
own work.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..core.guard import get_guard
from ..core.models import (Deal, HistoryResult, Order, OrdersResult, Position,
                           PositionDetail, PositionsResult)
from ..core.timeutil import utc_to_broker_naive
from ._common import (DEAL_ENTRY_NAMES, ORDER_TYPE_NAMES, POSITION_TYPE_NAMES,
                      ToolFailure, conn, resolve_symbol, triple)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def _position(row) -> Position:
    guard = get_guard()
    return Position(
        ticket=int(row.ticket), symbol=row.symbol,
        type=POSITION_TYPE_NAMES.get(int(row.type), str(row.type)),
        volume=float(row.volume), price_open=float(row.price_open),
        price_current=float(row.price_current), sl=float(row.sl), tp=float(row.tp),
        profit=float(row.profit), swap=float(row.swap),
        commission=float(getattr(row, "commission", 0.0) or 0.0),
        magic=int(row.magic), comment=row.comment or "",
        opened=triple(int(row.time)), ours=guard.owns(row.magic))


def _order(row) -> Order:
    guard = get_guard()
    expiry = int(getattr(row, "time_expiration", 0) or 0)
    return Order(
        ticket=int(row.ticket), symbol=row.symbol,
        type=ORDER_TYPE_NAMES.get(int(row.type), str(row.type)),
        volume_current=float(row.volume_current),
        volume_initial=float(row.volume_initial),
        price_open=float(row.price_open),
        price_current=float(getattr(row, "price_current", 0.0) or 0.0) or None,
        sl=float(row.sl), tp=float(row.tp), magic=int(row.magic),
        comment=row.comment or "", placed=triple(int(row.time_setup)),
        expiration=triple(expiry) if expiry else None,
        ours=guard.owns(row.magic))


def _deal(row) -> Deal:
    return Deal(
        ticket=int(row.ticket), order=int(row.order),
        position_id=int(getattr(row, "position_id", 0) or 0),
        symbol=row.symbol or "",
        type=ORDER_TYPE_NAMES.get(int(row.type), str(row.type)),
        entry=DEAL_ENTRY_NAMES.get(int(row.entry), str(row.entry)),
        volume=float(row.volume), price=float(row.price), profit=float(row.profit),
        commission=float(row.commission), swap=float(row.swap),
        fee=float(getattr(row, "fee", 0.0) or 0.0), magic=int(row.magic),
        comment=row.comment or "", time=triple(int(row.time)))


def _parse_when(value: str | None, default: datetime) -> datetime:
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


def register(mcp) -> None:
    @mcp.tool(
        name="positions_get",
        description=(
            "Open positions, optionally filtered by symbol. Each row is marked "
            "'ours' when its magic number matches this server's, so positions "
            "opened by hand or by another EA are visible but distinguishable."),
        annotations=READ_ONLY,
    )
    def positions_get(symbol: str | None = None,
                      only_ours: bool = False) -> PositionsResult:
        """List open positions.

        Args:
            symbol: Optional symbol filter; resolved automatically.
            only_ours: Show only positions this server opened.
        """
        if symbol:
            name = resolve_symbol(symbol)
            rows = conn().call(mt5.positions_get, symbol=name)
        else:
            rows = conn().call(mt5.positions_get)
        rows = list(rows or [])
        positions = [_position(r) for r in rows]
        if only_ours:
            positions = [p for p in positions if p.ours]
        return PositionsResult(count=len(positions), positions=positions,
                               total_profit=round(sum(p.profit for p in positions), 2))

    @mcp.tool(
        name="orders_get",
        description="Pending orders that have not triggered yet, optionally filtered by symbol.",
        annotations=READ_ONLY,
    )
    def orders_get(symbol: str | None = None,
                   only_ours: bool = False) -> OrdersResult:
        if symbol:
            name = resolve_symbol(symbol)
            rows = conn().call(mt5.orders_get, symbol=name)
        else:
            rows = conn().call(mt5.orders_get)
        rows = list(rows or [])
        orders = [_order(r) for r in rows]
        if only_ours:
            orders = [o for o in orders if o.ours]
        return OrdersResult(count=len(orders), orders=orders)

    @mcp.tool(
        name="history_get",
        description=(
            "Closed deals over a date range, with realised profit, commission "
            "and swap totalled up. Defaults to the last 30 days."),
        annotations=READ_ONLY,
    )
    def history_get(start: str | None = None, end: str | None = None,
                    symbol: str | None = None, limit: int = 200) -> HistoryResult:
        """Fetch trade history.

        Args:
            start: Range start, default 30 days ago.
            end: Range end, default now.
            symbol: Optional symbol filter.
            limit: Maximum deals returned, most recent first.
        """
        now = datetime.now(timezone.utc)
        frm = _parse_when(start, now - timedelta(days=30))
        to = _parse_when(end, now)
        if frm >= to:
            raise ToolFailure("The start date is not before the end date.",
                              "Swap them, or leave 'end' empty for 'now'.")
        offset = conn().clock.offset
        rows = conn().call(mt5.history_deals_get,
                           utc_to_broker_naive(frm, offset),
                           utc_to_broker_naive(to, offset))
        rows = list(rows or [])
        if symbol:
            name = resolve_symbol(symbol)
            rows = [r for r in rows if r.symbol == name]
        rows.sort(key=lambda r: r.time, reverse=True)
        trimmed = rows[:max(1, int(limit))]
        deals = [_deal(r) for r in trimmed]
        return HistoryResult(
            count=len(deals), deals=deals,
            realised_profit=round(sum(float(r.profit) for r in rows), 2),
            total_commission=round(sum(float(r.commission) for r in rows), 2),
            total_swap=round(sum(float(r.swap) for r in rows), 2),
            **{"from": triple(utc_to_broker_naive(frm, offset))},
            to=triple(utc_to_broker_naive(to, offset)))

    @mcp.tool(
        name="position_detail",
        description=(
            "One position in full: its deals, the money at risk if the stop is "
            "hit, the money it makes if the target is hit, the current "
            "R-multiple, and the distance to both levels in points. This is the "
            "tool for 'how is this trade actually doing'."),
        annotations=READ_ONLY,
    )
    def position_detail(ticket: int) -> PositionDetail:
        rows = conn().call(mt5.positions_get, ticket=int(ticket))
        rows = list(rows or [])
        if not rows:
            raise ToolFailure(f"No open position with ticket {ticket}.",
                              "Use positions_get to see the current tickets. It "
                              "may already be closed.")
        row = rows[0]
        position = _position(row)

        info = conn().call(mt5.symbol_info, row.symbol)
        risk = reward = sl_points = tp_points = None
        if info is not None:
            point = float(info.point) or 0.0
            # trade_tick_value_loss is the correct side for a stop: on some
            # symbols the profit and loss tick values genuinely differ.
            tick_loss = float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0)
            tick_profit = float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0)
            tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
            per_point_loss = (tick_loss / (tick_size / point)
                              if tick_size and point else 0.0)
            per_point_profit = (tick_profit / (tick_size / point)
                                if tick_size and point else 0.0)
            if row.sl and point:
                sl_points = abs(float(row.price_open) - float(row.sl)) / point
                risk = round(sl_points * per_point_loss * float(row.volume), 2)
            if row.tp and point:
                tp_points = abs(float(row.tp) - float(row.price_open)) / point
                reward = round(tp_points * per_point_profit * float(row.volume), 2)

        deals: list[Deal] = []
        position_id = int(getattr(row, "identifier", row.ticket))
        history = conn().call(mt5.history_deals_get, position=position_id)
        if history:
            deals = [_deal(d) for d in history]

        r_multiple = None
        if risk and risk > 0:
            r_multiple = round(float(row.profit) / risk, 2)

        return PositionDetail(
            position=position, deals=deals, risk_money=risk, reward_money=reward,
            r_multiple=r_multiple,
            distance_to_sl_points=round(sl_points, 1) if sl_points else None,
            distance_to_tp_points=round(tp_points, 1) if tp_points else None)
