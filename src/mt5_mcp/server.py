"""mt5-mcp — one MCP server for MetaTrader 5.

Transport is stdio, which means **stdout belongs to the JSON-RPC stream**. A
single stray ``print`` anywhere in this process corrupts the protocol and the
client disconnects with an unhelpful parse error. Logging is therefore wired to
a file before anything else is imported that might log, and the root logger gets
no stream handler at all.

Run with:  ``uv run mt5-mcp``
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .config import get_settings

log = logging.getLogger("mt5_mcp")

INSTRUCTIONS = """\
mt5-mcp talks to a local MetaTrader 5 terminal.

Start here:
1. Call mt5_connect once. If anything fails, call mt5_doctor — it checks every
   prerequisite in order and each problem comes with one sentence saying what to
   do about it.
2. Never pass a raw symbol name from the user straight into a tool. Run it
   through symbol_resolve first. Brokers rename everything, and worse, a plain
   name like GOLD or DAX is an unrelated *share* at many brokers, so an exact
   match can silently return the wrong instrument.
3. Size positions with risk_position_size rather than by hand. It reads the
   symbol's real tick value and reports what the rounded lot actually risks.

Trading:
- Every trade tool defaults to dry_run=true and returns a preview from the
  broker's own pre-flight check. Show that preview to the user and get explicit
  agreement before calling again with dry_run=false.
- Nothing here runs on a timer. There is no background trading, no scheduler and
  no auto-close. Every order needs a human decision in the loop.
- Live accounts are blocked unless MT5_ALLOW_LIVE is set. Reading always works.

Timestamps come back in three clocks — broker, display and UTC — because MT5's
raw values are broker-local and are not UTC.
"""


def _setup_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))

    directory = Path(settings.log_dir or ".")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            directory / "mt5-mcp.log", maxBytes=2_000_000, backupCount=3,
            encoding="utf-8")
    except OSError:
        # Never fall back to stdout: it would break the protocol. stderr is safe.
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)


def build_server() -> MCPServer:
    """Create the server and register every tool. Used by tests too."""
    _setup_logging()
    mcp = MCPServer(
        name="mt5-mcp",
        title="MetaTrader 5",
        version="1.0.0",
        instructions=INSTRUCTIONS,
    )
    from .tools import (account_tools, backtest_tools, connection_tools,
                        market_tools, risk_tools, symbol_tools, trade_tools)
    for module in (connection_tools, symbol_tools, market_tools, account_tools,
                   risk_tools, trade_tools, backtest_tools):
        module.register(mcp)
    log.info("mt5-mcp ready")
    return mcp


def main() -> None:
    server = build_server()
    try:
        server.run("stdio")
    finally:
        from .core.connection import get_connection
        try:
            get_connection().close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("error while closing the connection", exc_info=True)


if __name__ == "__main__":
    main()
