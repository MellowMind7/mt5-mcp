"""Risk tools: risk_position_size, risk_calc_margin, risk_calc_profit, risk_exposure.

``risk_position_size`` is the number that decides how much money a trader loses
when a stop is hit. It is worth being explicit about how it is derived.

    value_per_point_per_lot = tick_value_loss / (tick_size / point)
    volume                  = risk_money / (stop_points * value_per_point_per_lot)

Two details that are easy to get wrong and expensive when you do:

* **The loss side is not the profit side.** ``trade_tick_value_profit`` and
  ``trade_tick_value_loss`` differ on symbols whose profit currency is not the
  account currency — measured on USDJPY, they differ in the sixth digit, and on
  a cross-currency index they differ more. A stop loss is a loss, so
  ``trade_tick_value_loss`` is the one used.
* **Rounding goes down, never up.** ``volume_step`` forces a rounded lot size;
  rounding up would quietly risk more than the caller asked for. After rounding
  the function reports what the rounded size *actually* risks, so nobody has to
  redo the arithmetic.

Provenance: the sizing formula follows
https://github.com/FXDavid-OffbeatForex/MTX (MIT, (c) 2026 davidcyberdash),
commit 908ca9b, core/mm.py. MTX uses the symmetric ``tick_value``; using the
loss side, the cap reporting and the realised-risk readback are own work.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..config import get_settings
from ..core.models import (ExposureLeg, ExposureResult, MarginResult,
                           PositionSizeResult, ProfitResult)
from ._common import (ORDER_TYPE_NAMES, ToolFailure, conn, ensure_selected,
                      normalise_volume, resolve_symbol, round_to_step,
                      symbol_or_fail)

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

_ORDER_TYPES = {
    "buy": mt5.ORDER_TYPE_BUY, "sell": mt5.ORDER_TYPE_SELL,
    "buy_limit": mt5.ORDER_TYPE_BUY_LIMIT, "sell_limit": mt5.ORDER_TYPE_SELL_LIMIT,
    "buy_stop": mt5.ORDER_TYPE_BUY_STOP, "sell_stop": mt5.ORDER_TYPE_SELL_STOP,
}


#: Probe distance for the pricing-engine measurement. Large enough that the
#: result clears MT5's two-decimal rounding on cheap instruments, small enough
#: to stay inside a sane price range.
_PROBE_POINTS = 1000.0

#: Report a disagreement between the two methods above this relative gap.
_DISAGREE_TOLERANCE = 0.02


def _tick_value_formula(info, side: str) -> float:
    """The documented arithmetic: tick value scaled from ticks to points."""
    point = float(getattr(info, "point", 0.0) or 0.0)
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    field = ("trade_tick_value_loss" if side == "loss"
             else "trade_tick_value_profit")
    tick_value = float(getattr(info, field, 0.0) or 0.0)
    if not tick_value:
        tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    if tick_value and tick_size and point:
        return tick_value / (tick_size / point)
    return 0.0


def _measured_value_per_point(info, side: str, entry: float | None) -> float:
    """Ask the terminal's own pricing engine what one point is worth.

    ``order_calc_profit`` is what MT5 uses for real profit and loss, so it is
    the authority. It is measured over a long move and divided down, because the
    result is rounded to account-currency cents: a single-point probe on a cheap
    instrument rounds to 0.01 and loses most of its precision.
    """
    point = float(getattr(info, "point", 0.0) or 0.0)
    if not point or not entry:
        return 0.0
    volume = max(1.0, float(getattr(info, "volume_min", 1.0) or 1.0))
    distance = _PROBE_POINTS * point
    # A buy that moves down is a loss; measuring the side we actually need
    # keeps asymmetric instruments honest.
    close = entry - distance if side == "loss" else entry + distance
    profit = conn().call(mt5.order_calc_profit, mt5.ORDER_TYPE_BUY,
                         info.name, volume, float(entry), float(close))
    if profit is None:
        return 0.0
    return abs(float(profit)) / (_PROBE_POINTS * volume)


def value_per_point(info, *, side: str = "loss",
                    entry: float | None = None) -> tuple[float, str, str | None]:
    """Money per point per lot, in the account currency.

    Returns ``(value, source, warning)``.

    Two sources exist and they do **not** always agree. Measured on a live
    MetaQuotes demo: XAUUSD reports ``trade_tick_value = 0.1`` while a contract
    of 100 ounces moving one point is worth 1.0 — a factor of ten. XAGUSD is
    wrong by the same factor. Sizing gold off the reported field would build a
    position ten times too large, which is the one mistake in this file that
    costs somebody real money.

    So the terminal's pricing engine wins, and the documented arithmetic is the
    fallback for when it cannot answer.
    """
    measured = _measured_value_per_point(info, side, entry)
    stated = _tick_value_formula(info, side)

    if measured > 0 and stated > 0:
        gap = abs(measured - stated) / max(measured, stated)
        if gap > _DISAGREE_TOLERANCE:
            return measured, "pricing_engine", (
                f"This broker reports a tick value for {info.name} that "
                f"disagrees with its own pricing engine "
                f"({stated:.6f} vs {measured:.6f} per point per lot, "
                f"{gap:.0%} apart). The pricing-engine figure is used, because "
                f"that is what profit and loss are actually settled on.")
        return measured, "pricing_engine", None
    if measured > 0:
        return measured, "pricing_engine", None
    if stated > 0:
        return stated, "tick_value", (
            "The terminal could not price a test move, so the figure comes from "
            "the symbol's reported tick value. Check the size before trading it.")
    contract = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
    point = float(getattr(info, "point", 0.0) or 0.0)
    return contract * point, "contract_size", (
        "Neither a tick value nor a test price was available, so the figure "
        "falls back to contract size times point. That is only correct when the "
        "profit currency matches the account currency — verify it by hand.")


def register(mcp) -> None:
    @mcp.tool(
        name="risk_position_size",
        description=(
            "Work out the lot size for a trade from the risk you are willing "
            "to take. Give either risk_percent of equity or risk_money, and "
            "either a stop price or a stop distance in points. Returns the lot "
            "size rounded down to the symbol's step, plus what that rounded "
            "size actually risks — the number that matters. Respects "
            "MT5_MAX_LOT and MT5_MAX_RISK_PERCENT and says when a cap applied."),
        annotations=READ_ONLY,
    )
    def risk_position_size(symbol: str, stop_price: float | None = None,
                           stop_points: float | None = None,
                           risk_percent: float | None = None,
                           risk_money: float | None = None,
                           entry_price: float | None = None,
                           side: str = "buy") -> PositionSizeResult:
        """Size a position from a risk budget.

        Args:
            symbol: Symbol name; resolved automatically.
            stop_price: Absolute stop-loss price. Use this or stop_points.
            stop_points: Stop distance in points. Use this or stop_price.
            risk_percent: Percent of account equity to risk. Use this or risk_money.
            risk_money: Amount in account currency to risk.
            entry_price: Assumed entry. Defaults to the current ask/bid.
            side: "buy" or "sell"; only affects which side of the spread is used.
        """
        if risk_percent is None and risk_money is None:
            raise ToolFailure("No risk budget given.",
                              "Pass risk_percent (e.g. 1.0) or risk_money.")
        if stop_price is None and stop_points is None:
            raise ToolFailure("No stop given.",
                              "Pass stop_price or stop_points — a position size "
                              "cannot be derived without a stop.")

        name, info = symbol_or_fail(symbol)
        account = conn().call(mt5.account_info)
        if account is None:
            raise ToolFailure("Could not read the account.",
                              "Run mt5_doctor to check the connection.")

        settings = get_settings()
        warnings: list[str] = []
        capped_by: str | None = None

        equity = float(account.equity)
        if risk_money is None:
            if risk_percent > settings.max_risk_percent:
                warnings.append(
                    f"risk_percent {risk_percent}% exceeds MT5_MAX_RISK_PERCENT "
                    f"({settings.max_risk_percent}%); using the limit instead.")
                risk_percent = settings.max_risk_percent
                capped_by = "max_risk_percent"
            budget = equity * float(risk_percent) / 100.0
        else:
            budget = float(risk_money)
            implied = (budget / equity * 100.0) if equity else 0.0
            if implied > settings.max_risk_percent:
                budget = equity * settings.max_risk_percent / 100.0
                warnings.append(
                    f"risk_money would be {implied:.2f}% of equity, above "
                    f"MT5_MAX_RISK_PERCENT ({settings.max_risk_percent}%); "
                    f"reduced to {budget:.2f}.")
                capped_by = "max_risk_percent"

        point = float(info.point)
        tick = conn().call(mt5.symbol_info_tick, name)
        if entry_price is None:
            if tick is None or not tick.time:
                raise ToolFailure(
                    f"No current price for {name}, and no entry_price given.",
                    "Pass entry_price explicitly, or wait for the market to open.")
            entry = float(tick.ask if side.lower().startswith("b") else tick.bid)
        else:
            entry = float(entry_price)

        if stop_points is not None:
            distance_points = abs(float(stop_points))
        else:
            distance_points = abs(entry - float(stop_price)) / point
        if distance_points <= 0:
            raise ToolFailure("The stop distance is zero.",
                              "The stop must sit away from the entry price.")

        stops_level = int(getattr(info, "trade_stops_level", 0) or 0)
        if stops_level and distance_points < stops_level:
            warnings.append(
                f"The stop is {distance_points:.0f} points away but this broker "
                f"requires at least {stops_level}. The order would be rejected "
                f"with retcode 10016.")

        per_point, source, caveat = value_per_point(info, side="loss",
                                                    entry=entry)
        if caveat:
            warnings.append(caveat)
        if per_point <= 0:
            raise ToolFailure(
                f"Cannot determine the point value for {name}.",
                "This symbol reports neither a tick value nor a contract size. "
                "Size the position by hand.")

        raw_volume = budget / (distance_points * per_point)
        step = float(info.volume_step)
        volume = round_to_step(raw_volume, step)

        vmin = float(info.volume_min)
        vmax = float(info.volume_max)
        if volume < vmin:
            volume = vmin
            warnings.append(
                f"The calculated size was below the minimum of {vmin}; raised to "
                f"it. This risks more than requested.")
            capped_by = capped_by or "volume_min"
        if vmax and volume > vmax:
            volume = round_to_step(vmax, step)
            capped_by = "volume_max"
            warnings.append(f"Capped at the symbol maximum of {vmax}.")
        if volume > settings.max_lot:
            volume = round_to_step(settings.max_lot, step)
            capped_by = "max_lot"
            warnings.append(
                f"Capped at MT5_MAX_LOT ({settings.max_lot}). Raise it in .env "
                f"if that is intended.")

        actual_risk = distance_points * per_point * volume

        margin = conn().call(
            mt5.order_calc_margin,
            mt5.ORDER_TYPE_BUY if side.lower().startswith("b") else mt5.ORDER_TYPE_SELL,
            name, volume, entry)
        if margin is not None and margin > float(account.margin_free):
            warnings.append(
                f"This size needs {margin:.2f} margin but only "
                f"{account.margin_free:.2f} is free. The order would fail with "
                f"retcode 10019.")
            capped_by = capped_by or "free_margin"

        return PositionSizeResult(
            symbol=name, volume=volume, volume_unrounded=round(raw_volume, 6),
            risk_money=round(budget, 2),
            risk_percent=round(budget / equity * 100.0, 4) if equity else None,
            account_currency=account.currency,
            stop_distance_points=round(distance_points, 1),
            stop_distance_price=round(distance_points * point, int(info.digits)),
            value_per_point_per_lot=round(per_point, 6),
            money_at_risk_at_stop=round(actual_risk, 2),
            capped_by=capped_by, warnings=warnings)

    @mcp.tool(
        name="risk_calc_margin",
        description=(
            "How much margin a position would tie up, and what free margin "
            "would remain. Ask this before opening rather than discovering it "
            "through retcode 10019."),
        annotations=READ_ONLY,
    )
    def risk_calc_margin(symbol: str, volume: float, order_type: str = "buy",
                         price: float | None = None) -> MarginResult:
        name, info = symbol_or_fail(symbol)
        kind = _ORDER_TYPES.get(order_type.strip().lower())
        if kind is None:
            raise ToolFailure(f"Unknown order type {order_type!r}.",
                              "Use one of: " + ", ".join(_ORDER_TYPES))
        if price is None:
            tick = conn().call(mt5.symbol_info_tick, name)
            if tick is None:
                raise ToolFailure(f"No current price for {name}.",
                                  "Pass price explicitly.")
            price = float(tick.ask if "buy" in order_type else tick.bid)
        volume, notes = normalise_volume(info, volume)
        margin = conn().call(mt5.order_calc_margin, kind, name, volume, float(price))
        if margin is None:
            raise ToolFailure(f"The terminal could not calculate margin for {name}.",
                              "Check the volume against volume_min and volume_max.")
        account = conn().call(mt5.account_info)
        return MarginResult(
            symbol=name, volume=volume, order_type=order_type.lower(),
            price=float(price), margin=round(float(margin), 2),
            account_currency=account.currency if account else "",
            free_margin_after=(round(float(account.margin_free) - float(margin), 2)
                               if account else None))

    @mcp.tool(
        name="risk_calc_profit",
        description=(
            "What a move from one price to another would be worth, in account "
            "currency, for a given lot size. Use it to sanity-check a target "
            "before placing the trade."),
        annotations=READ_ONLY,
    )
    def risk_calc_profit(symbol: str, volume: float, price_open: float,
                         price_close: float, order_type: str = "buy") -> ProfitResult:
        name, info = symbol_or_fail(symbol)
        kind = _ORDER_TYPES.get(order_type.strip().lower())
        if kind is None or kind not in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL):
            raise ToolFailure(f"Unknown or unsupported order type {order_type!r}.",
                              "Use 'buy' or 'sell'.")
        volume, _notes = normalise_volume(info, volume)
        profit = conn().call(mt5.order_calc_profit, kind, name, volume,
                             float(price_open), float(price_close))
        if profit is None:
            raise ToolFailure(f"The terminal could not calculate profit for {name}.",
                              "Check that both prices are plausible for this symbol.")
        account = conn().call(mt5.account_info)
        point = float(info.point) or 1.0
        return ProfitResult(
            symbol=name, volume=volume, order_type=order_type.lower(),
            price_open=float(price_open), price_close=float(price_close),
            profit=round(float(profit), 2),
            account_currency=account.currency if account else "",
            points=round(abs(float(price_close) - float(price_open)) / point, 1))

    @mcp.tool(
        name="risk_exposure",
        description=(
            "Total exposure across all open positions, grouped by symbol and "
            "by currency. The currency view is the useful one: three separate "
            "long EUR trades are one big EUR bet, and this is where that "
            "becomes visible."),
        annotations=READ_ONLY,
    )
    def risk_exposure() -> ExposureResult:
        account = conn().call(mt5.account_info)
        if account is None:
            raise ToolFailure("Could not read the account.",
                              "Run mt5_doctor to check the connection.")
        rows = list(conn().call(mt5.positions_get) or [])

        by_symbol: dict[str, dict] = defaultdict(
            lambda: {"long": 0.0, "short": 0.0, "profit": 0.0, "n": 0})
        by_currency: dict[str, dict] = defaultdict(
            lambda: {"long": 0.0, "short": 0.0, "profit": 0.0, "n": 0})

        for row in rows:
            is_long = int(row.type) == mt5.POSITION_TYPE_BUY
            volume = float(row.volume)
            slot = by_symbol[row.symbol]
            slot["long" if is_long else "short"] += volume
            slot["profit"] += float(row.profit)
            slot["n"] += 1

            info = conn().call(mt5.symbol_info, row.symbol)
            if info is None:
                continue
            contract = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
            notional = volume * contract
            base = getattr(info, "currency_base", "") or ""
            quote = getattr(info, "currency_profit", "") or ""
            if base:
                leg = by_currency[base]
                leg["long" if is_long else "short"] += notional
                leg["n"] += 1
            if quote and quote != base:
                leg = by_currency[quote]
                # Long EURUSD is long EUR and short USD.
                leg["short" if is_long else "long"] += notional
                leg["n"] += 1

        def legs(source: dict) -> list[ExposureLeg]:
            out = []
            for key, slot in sorted(source.items()):
                out.append(ExposureLeg(
                    key=key,
                    volume_net=round(slot["long"] - slot["short"], 2),
                    volume_long=round(slot["long"], 2),
                    volume_short=round(slot["short"], 2),
                    profit=round(slot["profit"], 2), positions=slot["n"]))
            return out

        return ExposureResult(
            account_currency=account.currency, equity=float(account.equity),
            by_symbol=legs(by_symbol), by_currency=legs(by_currency),
            total_profit=round(sum(float(r.profit) for r in rows), 2),
            margin_used=float(account.margin),
            margin_level=float(account.margin_level) or None)
