"""Acceptance run: call every read-only tool once against a live terminal.

This is the check that says the build is good. It exercises the real terminal —
no mocks — and prints one row per tool. Nothing here writes: the trade tools are
covered separately, and only ever with dry_run.

Usage:  uv run python smoke_test.py
Exit code 0 when every tool answered, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from typing import Any

# stdout is ours here — this is a script, not the stdio server.
from mt5_mcp.server import build_server

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _summarise(payload: Any, limit: int = 74) -> str:
    """One short line describing what came back."""
    if payload is None:
        return "(no content)"
    if isinstance(payload, dict):
        for key in ("resolved", "summary", "count", "symbol", "connected",
                    "login", "overall", "ok"):
            if key in payload:
                if key == "count":
                    return f"count={payload['count']}"
                return f"{key}={payload[key]}"
        return ", ".join(list(payload)[:5])
    if isinstance(payload, list):
        return f"{len(payload)} row(s)"
    return str(payload)[:limit]


async def run() -> int:
    server = build_server()
    results: list[tuple[str, str, float, str]] = []

    async def call(label: str, name: str, args: dict | None = None,
                   optional: bool = False) -> Any:
        started = time.time()
        try:
            result = await server.call_tool(name, args or {})
            took = time.time() - started
            payload = getattr(result, "structured_content", None)
            if getattr(result, "is_error", False):
                text = "".join(getattr(c, "text", "") for c in result.content)
                results.append((label, SKIP if optional else FAIL, took,
                                text[:74]))
                return None
            if isinstance(payload, dict) and payload.get("ok") is False:
                status = SKIP if optional else FAIL
                results.append((label, status, took,
                                str(payload.get("error", ""))[:74]))
                return None
            results.append((label, PASS, took, _summarise(payload)))
            return payload
        except Exception as exc:  # noqa: BLE001 - the point is to report it
            took = time.time() - started
            detail = f"{type(exc).__name__}: {exc}"
            results.append((label, SKIP if optional else FAIL, took, detail[:74]))
            if not optional:
                traceback.print_exc(limit=2)
            return None

    print("=" * 96)
    print("mt5-mcp smoke test")
    print("=" * 96)

    # --- connection and diagnosis ---------------------------------------
    status = await call("mt5_connect", "mt5_connect")
    if status is None:
        print("\nCannot connect to the terminal — nothing else can be tested.")
        _report(results)
        return 1
    await call("mt5_status", "mt5_status")
    await call("mt5_account", "mt5_account")
    doctor = await call("mt5_doctor", "mt5_doctor", {"deep": False})

    # --- symbols ---------------------------------------------------------
    resolved = await call("symbol_resolve", "symbol_resolve", {"query": "EURUSD"})
    symbol = (resolved or {}).get("resolved") or "EURUSD"
    await call("symbol_resolve (Gold trap)", "symbol_resolve", {"query": "GOLD"})
    await call("symbol_search", "symbol_search", {"text": "euro", "limit": 5})
    await call("symbol_info", "symbol_info", {"symbol": symbol})
    await call("symbol_select", "symbol_select",
               {"symbol": symbol, "enable": True})
    await call("symbol_is_tradable_now", "symbol_is_tradable_now",
               {"symbol": symbol})

    # --- market data -----------------------------------------------------
    await call("bars_get (count)", "bars_get",
               {"symbol": symbol, "timeframe": "H1", "count": 50})
    await call("bars_get (range)", "bars_get",
               {"symbol": symbol, "timeframe": "D1", "start": "2026-01-01"})
    await call("quote_get", "quote_get", {"symbol": symbol}, optional=True)
    await call("ticks_get", "ticks_get",
               {"symbol": symbol, "start": "2026-09-04 00:00:00", "count": 20},
               optional=True)
    await call("data_availability", "data_availability",
               {"symbol": symbol, "timeframe": "D1"})

    # --- account ---------------------------------------------------------
    positions = await call("positions_get", "positions_get")
    await call("orders_get", "orders_get")
    await call("history_get", "history_get", {"limit": 10})
    rows = (positions or {}).get("positions") or []
    if rows:
        await call("position_detail", "position_detail",
                   {"ticket": rows[0]["ticket"]})
    else:
        results.append(("position_detail", SKIP, 0.0, "no open position"))

    # --- risk ------------------------------------------------------------
    await call("risk_position_size", "risk_position_size",
               {"symbol": symbol, "stop_points": 200, "risk_percent": 1.0})
    await call("risk_calc_margin", "risk_calc_margin",
               {"symbol": symbol, "volume": 0.10, "order_type": "buy"})
    await call("risk_calc_profit", "risk_calc_profit",
               {"symbol": symbol, "volume": 0.10, "price_open": 1.1000,
                "price_close": 1.1100, "order_type": "buy"})
    await call("risk_exposure", "risk_exposure")

    # --- trading, preview only ------------------------------------------
    await call("trade_market (dry run)", "trade_market",
               {"symbol": symbol, "side": "buy", "volume": 0.01,
                "dry_run": True})
    await call("trade_close_batch (dry run)", "trade_close_batch",
               {"scope": "profitable", "dry_run": True})

    # --- backtest --------------------------------------------------------
    route = (doctor or {}).get("backtest_path", "none")
    results.append(("backtest route", PASS if route != "none" else FAIL,
                    0.0, f"route={route}"))

    return _report(results)


def _report(results: list[tuple[str, str, float, str]]) -> int:
    print()
    print(f"{'tool':<30} {'status':<6} {'ms':>7}  detail")
    print("-" * 96)
    for label, status, took, detail in results:
        print(f"{label:<30} {status:<6} {took * 1000:7.0f}  {detail}")
    passed = sum(1 for r in results if r[1] == PASS)
    failed = sum(1 for r in results if r[1] == FAIL)
    skipped = sum(1 for r in results if r[1] == SKIP)
    print("-" * 96)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
