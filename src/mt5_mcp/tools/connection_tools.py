"""Connection and diagnostics: mt5_connect, mt5_status, mt5_account,
mt5_disconnect, mt5_doctor.

``mt5_doctor`` is the one that decides whether somebody gives up after five
minutes. Every check that can fail carries a sentence saying what to do, and the
checks run in dependency order so the first failure is the real cause rather
than a symptom of it.

Own work.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..config import get_settings
from ..core import tester as tester_mod
from ..core.connection import (Mt5ConnectionError, find_terminals,
                               get_connection, read_max_bars)
from ..core.models import (AccountInfo, ClockInfo, DoctorCheck, DoctorResult,
                           StatusResult, TerminalInfo)
from ..core.native import native_editor, native_terminal
from ._common import ToolFailure, conn, resolver

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITE_SAFE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

#: Warn above this. A single short backtest already pulled 98 MB of history.
_TESTER_SIZE_WARN_MB = 2048.0


def _status_model() -> StatusResult:
    raw = get_connection().status()
    if not raw.get("connected"):
        return StatusResult(connected=False, note=raw.get("note"))
    return StatusResult(
        connected=True,
        terminal=TerminalInfo(**raw.get("terminal", {})),
        account=AccountInfo(**raw["account"]) if raw.get("account") else None,
        time=ClockInfo(**raw.get("time", {})),
    )


def register(mcp) -> None:
    @mcp.tool(
        name="mt5_connect",
        description=(
            "Connect to the MetaTrader 5 terminal. Call this once before "
            "anything else. If the terminal is already running and logged in, "
            "no credentials are needed — it attaches to that session. "
            "Credentials, when used, come from .env and are never returned."),
        annotations=WRITE_SAFE,
    )
    def mt5_connect(force: bool = False) -> StatusResult:
        """Attach to the terminal, or start it and log in.

        Args:
            force: Reconnect even if a connection already exists.
        """
        try:
            get_connection().connect(force=force)
        except Mt5ConnectionError as exc:
            raise ToolFailure(str(exc),
                              exc.detail.fix if exc.detail else
                              "Run mt5_doctor for a step-by-step check.") from exc
        resolver().load(force=force)
        return _status_model()

    @mcp.tool(
        name="mt5_status",
        description=(
            "Current connection state: terminal build, broker connection, "
            "whether algorithmic trading is on, the account summary, and the "
            "measured broker/UTC time offset."),
        annotations=READ_ONLY,
    )
    def mt5_status() -> StatusResult:
        return _status_model()

    @mcp.tool(
        name="mt5_account",
        description=(
            "Account figures: balance, equity, free margin, margin level, "
            "leverage, currency and account type. The account number is "
            "returned masked."),
        annotations=READ_ONLY,
    )
    def mt5_account() -> AccountInfo:
        info = conn().call(mt5.account_info)
        if info is None:
            raise ToolFailure("The terminal returned no account information.",
                              "Check that the terminal is logged in, then run "
                              "mt5_doctor.")
        settings = get_settings()
        trade_modes = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
        margin_modes = {0: "NETTING", 1: "EXCHANGE", 2: "HEDGING"}
        masked = (settings.masked_login if settings.login
                  else "***" + str(info.login)[-4:])
        return AccountInfo(
            login=masked, server=info.server, currency=info.currency,
            type=trade_modes.get(info.trade_mode, "?"),
            margin_mode=margin_modes.get(info.margin_mode, "?"),
            leverage=info.leverage, balance=info.balance, equity=info.equity,
            free_margin=info.margin_free, margin=info.margin,
            margin_level=info.margin_level, profit=info.profit,
            trade_allowed=info.trade_allowed, experts_allowed=info.trade_expert,
        )

    @mcp.tool(
        name="mt5_disconnect",
        description="Close the connection to the terminal. The terminal itself keeps running.",
        annotations=WRITE_SAFE,
    )
    def mt5_disconnect() -> dict:
        get_connection().disconnect()
        return {"ok": True, "message": "Disconnected from the terminal."}

    @mcp.tool(
        name="mt5_doctor",
        description=(
            "Full diagnosis, in dependency order: terminal found, running, "
            "logged in, algorithmic trading on, Python package version, a live "
            "symbol fetch, write access to the Files folder, the backtest "
            "route, and whether a second unguarded trading path is open. Every "
            "problem comes with one sentence saying what to do. Run this first "
            "whenever something does not work."),
        annotations=READ_ONLY,
    )
    def mt5_doctor(deep: bool = False) -> DoctorResult:
        """Check everything that has to be true for this server to work.

        Args:
            deep: Also probe the terminal's bar-count ceiling, which costs a
                few extra calls.
        """
        checks: list[DoctorCheck] = []
        settings = get_settings()

        def add(name: str, status: str, detail: str, fix: str | None = None) -> None:
            checks.append(DoctorCheck(name=name, status=status, detail=detail,
                                      fix=fix))

        # 1. terminal on disk ------------------------------------------------
        terminals = find_terminals(settings.terminal_path)
        if not terminals:
            add("terminal_found", "fail",
                "No MetaTrader 5 installation found on this machine.",
                "Install MetaTrader 5 from metaquotes.net, or set "
                "MT5_TERMINAL_PATH in your .env to the full path of "
                "terminal64.exe.")
            return DoctorResult(overall="fail",
                                summary="No terminal — nothing else can work.",
                                checks=checks, backtest_path="none")
        listing = ", ".join(
            f"{Path(t.exe).parent.name} (build {t.build or '?'}"
            f"{', running' if t.is_running else ''})" for t in terminals[:4])
        add("terminal_found", "ok",
            f"{len(terminals)} installation(s): {listing}")

        running = [t for t in terminals if t.is_running]
        if not running:
            add("terminal_running", "warn",
                "No terminal process is running.",
                "Start MetaTrader 5 and log in. The Python interface only talks "
                "to a running, logged-in terminal.")
        else:
            add("terminal_running", "ok",
                f"{len(running)} terminal process(es) running.")

        # 2. our connection --------------------------------------------------
        connection = get_connection()
        try:
            connection.connect()
            add("connected", "ok", "Connected to the terminal.")
        except Mt5ConnectionError as exc:
            add("connected", "fail", str(exc),
                exc.detail.fix if exc.detail else
                "Start the terminal, log in, and try again.")
            return DoctorResult(overall="fail",
                                summary="Cannot reach the terminal.",
                                checks=checks, backtest_path="none")

        terminal_info = connection.call(mt5.terminal_info)
        account = connection.call(mt5.account_info)

        if account is None:
            add("logged_in", "fail", "The terminal is not logged in to an account.",
                "Log in via File -> Login to Trade Account, then try again.")
        else:
            kind = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(account.trade_mode, "?")
            status = "ok"
            fix = None
            if kind == "REAL" and not settings.allow_live:
                status = "warn"
                fix = ("This is a LIVE account. All write tools are blocked "
                       "because MT5_ALLOW_LIVE is false. Reading works normally.")
            add("logged_in", status,
                f"Logged in to {kind} account on {account.server}.", fix)

        if terminal_info is not None and not terminal_info.connected:
            add("broker_connection", "fail",
                "The terminal is not connected to the broker.",
                "Check the connection indicator at the bottom right of the "
                "terminal, and your network.")
        else:
            ping = (getattr(terminal_info, "ping_last", 0) or 0) // 1000
            add("broker_connection", "ok", f"Connected to the broker, ping {ping} ms.")

        # 3. algo trading ----------------------------------------------------
        if terminal_info is not None and not terminal_info.trade_allowed:
            add("algo_trading", "warn",
                "Algorithmic trading is switched off in the terminal.",
                "Click «Algo Trading» in the toolbar until the icon turns "
                "green. Reading works without it; trading does not.")
        else:
            add("algo_trading", "ok", "Algorithmic trading is enabled.")

        if terminal_info is not None and getattr(terminal_info, "tradeapi_disabled", False):
            add("trade_api", "fail",
                "The terminal has the trading API disabled.",
                "Tools -> Options -> Expert Advisors: allow algorithmic trading.")

        # 4. package version -------------------------------------------------
        version = connection.call(mt5.version)
        build = version[1] if version else None
        package = getattr(mt5, "__version__", "?")
        if build and str(package).split(".")[-1].isdigit():
            package_build = int(str(package).split(".")[-1])
            gap = abs(int(build) - package_build)
            if gap > 200:
                add("package_version", "warn",
                    f"Terminal build {build}, MetaTrader5 package {package} — "
                    f"{gap} builds apart.",
                    "Update the Python package with: uv pip install -U MetaTrader5")
            else:
                add("package_version", "ok",
                    f"Terminal build {build}, package {package}.")
        else:
            add("package_version", "ok", f"MetaTrader5 package {package}.")

        # 5. symbols ---------------------------------------------------------
        try:
            total = resolver().load()
            probe = resolver().resolve("EURUSD")
            if probe.best is None:
                add("symbols", "warn",
                    f"{total} symbols loaded, but EURUSD could not be resolved.",
                    "Unusual broker naming. Use symbol_search to find the right "
                    "names; everything else still works.")
            else:
                add("symbols", "ok",
                    f"{total} symbols loaded; EURUSD resolves to "
                    f"{probe.best.record.name}.")
        except Exception as exc:  # noqa: BLE001
            add("symbols", "fail", f"Could not read the symbol list: {exc}",
                "Restart the terminal and try again.")

        # 6. live tick -------------------------------------------------------
        try:
            name = (resolver().resolve("EURUSD").best.record.name
                    if resolver().resolve("EURUSD").best else "EURUSD")
            connection.call(mt5.symbol_select, name, True)
            tick = connection.call(mt5.symbol_info_tick, name)
            if tick is None or not tick.time:
                add("live_quote", "warn", f"No tick data for {name}.",
                    "Outside market hours this is normal. During the week it "
                    "means the symbol is not subscribed at your broker.")
            else:
                age = (datetime.now(timezone.utc)
                       - connection.clock.to_utc(tick.time)).total_seconds()
                add("live_quote", "ok",
                    f"{name} bid {tick.bid} ask {tick.ask}, "
                    f"{age:.0f} s old.")
        except Exception as exc:  # noqa: BLE001
            add("live_quote", "warn", f"Test quote failed: {exc}",
                "Check that the market is open for this symbol.")

        # 7. clock -----------------------------------------------------------
        clock = connection.clock.describe()
        if clock.get("measured"):
            add("broker_clock", "ok",
                f"Broker time is UTC{clock['broker_minus_utc_hours']:+g} h, "
                f"{clock['broker_minus_display_hours']:+g} h against "
                f"{settings.timezone_display}.")
        else:
            add("broker_clock", "warn", "The broker time offset is not measured yet.",
                "It is measured from the first tick that arrives. Outside market "
                "hours it may stay unknown.")

        # 8. files folder ----------------------------------------------------
        data_dir = getattr(terminal_info, "data_path", None)
        if data_dir:
            files_dir = Path(data_dir) / "MQL5" / "Files"
            try:
                files_dir.mkdir(parents=True, exist_ok=True)
                probe_file = files_dir / ".mt5mcp_write_test"
                probe_file.write_text("ok", encoding="utf-8")
                probe_file.unlink()
                add("files_writable", "ok", f"Write access to {files_dir}.")
            except OSError as exc:
                add("files_writable", "fail",
                    f"Cannot write to {files_dir}: {exc}",
                    "Run the terminal and this server as the same Windows user, "
                    "or check the folder permissions.")

        # 9. backtest route --------------------------------------------------
        route = "none"
        setup = tester_mod.check_setup(settings.tester_terminal_path)
        if setup.ready:
            route = "cli"
            size = tester_mod.tester_dir_size_mb(str(setup.data_dir))
            if size > _TESTER_SIZE_WARN_MB:
                add("backtest_cli", "warn",
                    f"The tester copy at {setup.data_dir} has grown to {size} MB.",
                    "Most of that is downloaded history under 'bases'. Call "
                    "backtest_cleanup to reclaim it; it is a cache and will be "
                    "re-downloaded on demand.")
            else:
                add("backtest_cli", "ok",
                    f"Portable tester ready at {setup.data_dir} ({size} MB).")
        else:
            add("backtest_cli", "warn", setup.problem or "Tester copy not set up.",
                setup.fix)

        native_ok, native_reason = native_terminal().available()
        if native_ok:
            if route == "none":
                route = "native"
            add("backtest_native", "ok",
                "Terminal MCP reachable — backtests take the fast route.")
        elif route == "cli":
            add("backtest_native", "ok",
                "Backtests use the command-line tester, which works on every "
                "MetaTrader build."
                + ("" if settings.native_mcp_key
                   else " The optional fast route is not configured."))
        else:
            add("backtest_native", "fail",
                f"No backtest route available. {native_reason}",
                "Run install.bat to build the portable tester copy — that is "
                "the route that works without any key.")

        editor_ok, editor_reason = native_editor().available()
        if editor_ok:
            add("compile_route", "ok",
                "MetaEditor MCP reachable — compiling uses the fast route.")
        else:
            # Distinguish "you have not configured the accelerator" from "the
            # accelerator is configured but broken". Only the second is worth
            # a nudge; the first is the normal, fully supported setup.
            if not settings.native_mcp_key:
                add("compile_route", "ok",
                    "Compiling uses metaeditor64.exe on the command line, which "
                    "works on every build.",
                    "Optional: put the key from Tools -> Options -> MCP into "
                    "MT5_NATIVE_MCP_KEY to use the terminal's faster route.")
            else:
                add("compile_route", "warn",
                    f"MetaEditor MCP not reachable: {editor_reason}",
                    "Compiling falls back to metaeditor64.exe, which works but "
                    "is slower. Port 22345 only listens while MetaEditor is "
                    "open — start it, or clear MT5_NATIVE_MCP_KEY to stop "
                    "trying.")

        # 10. second, unguarded trading path ---------------------------------
        checks.append(_assistant_check(data_dir))

        # 11. optional deep probe -------------------------------------------
        if deep:
            ceiling = connection.max_bars()
            configured = read_max_bars(data_dir)
            add("max_bars", "ok",
                f"Bars per request: {'unlimited' if ceiling is None else ceiling}"
                + (f" (common.ini says {configured})" if configured else ""))

        fails = [c for c in checks if c.status == "fail"]
        warns = [c for c in checks if c.status == "warn"]
        if fails:
            overall = "fail"
            summary = (f"{len(fails)} blocking problem(s): "
                       + "; ".join(c.name for c in fails))
        elif warns:
            overall = "warn"
            summary = (f"Usable, with {len(warns)} warning(s): "
                       + "; ".join(c.name for c in warns))
        else:
            overall = "ok"
            summary = "Everything checks out."
        return DoctorResult(overall=overall, summary=summary, checks=checks,
                            backtest_path=route)


def _assistant_check(data_dir: str | None) -> DoctorCheck:
    """Is the terminal's own AI assistant allowed to trade?

    That path bypasses this server completely: no dry_run, no lot ceiling, no
    daily loss limit, no trades.log. It is a GUI toggle any user can flip, so it
    is worth checking every time rather than assuming it is still off.
    """
    if not data_dir:
        return DoctorCheck(name="second_trading_path", status="ok",
                           detail="Terminal data folder unknown; nothing to check.")
    ini = Path(data_dir) / "config" / "assistant.ini"
    if not ini.is_file():
        return DoctorCheck(
            name="second_trading_path", status="ok",
            detail="No assistant.ini — this build has no built-in AI assistant.")
    try:
        raw = ini.read_bytes()
        text = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                else raw.decode("utf-8", "replace"))
    except OSError as exc:
        return DoctorCheck(name="second_trading_path", status="warn",
                           detail=f"Could not read assistant.ini: {exc}",
                           fix="Check the file permissions.")
    value = None
    for line in text.splitlines():
        if line.strip().lower().startswith("permissionstrade"):
            _, _, raw_value = line.partition("=")
            value = raw_value.strip()
            break
    if value is None:
        return DoctorCheck(
            name="second_trading_path", status="ok",
            detail="assistant.ini has no trading permission set.")
    labels = {"0": "Enabled", "1": "Manual confirmation", "2": "Disabled"}
    label = labels.get(value, value)
    if value == "2":
        return DoctorCheck(
            name="second_trading_path", status="ok",
            detail=f"The terminal's built-in assistant cannot trade ({label}).")
    return DoctorCheck(
        name="second_trading_path", status="warn",
        detail=(f"The terminal's built-in AI assistant is allowed to trade "
                f"({label}). That is a second route to your account which does "
                f"not pass through this server."),
        fix=("Set Tools -> Options -> AI Assistant -> Trading to «Disabled» "
             "unless you want it. Orders sent that way have no dry-run, no lot "
             "ceiling, no daily loss limit and are not written to trades.log."))
