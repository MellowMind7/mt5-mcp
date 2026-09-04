"""Compiling and backtesting: mql_compile, backtest, backtest_report,
backtest_compare, backtest_cleanup.

Two routes exist for both jobs, and the order matters:

* **CLI is the default.** ``terminal64.exe /config:`` and ``metaeditor64.exe
  /compile:`` work on every MT5 build, need no API key and no GUI. Course
  participants sit on broker terminals that lag the MetaQuotes release by
  months — one measured here was 330 builds behind — so this has to be the
  ground floor.
* **Native MCP is an accelerator.** Build 6060+ exposes the tester and the
  compiler over HTTP. When it is reachable it is faster and runs inside the
  already-open terminal, so we prefer it, but nothing depends on it.

``mt5_doctor`` reports which route is live.

The compile success rule is the same on both routes and is not the obvious one:
MetaEditor returns exit code 1 even on success, and the native ``compile_file``
reports ``isError: false`` even with six compiler errors. The only reliable
signals are the ``0 errors`` line **and** a freshly written ``.ex5``.

Provenance: CLI tester and report parsing adapted from
https://github.com/FXDavid-OffbeatForex/MBT (MIT, (c) 2026 davidcyberdash),
commit 186eb6b, and https://github.com/PHUICMT/mcp-mt5 (MIT, (c) 2026 PHUICMT),
commit 9a859e1. Route selection and the compile success rule are own work.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

import MetaTrader5 as mt5
from mcp.types import ToolAnnotations

from ..config import get_settings
from ..core import tester as tester_mod
from ..core.models import (BacktestComparison, BacktestMetrics, BacktestResult,
                           CompileDiagnostic, CompileResult)
from ..core.native import NativeUnavailable, native_editor, native_terminal
from ._common import ToolFailure, conn, resolve_symbol

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITE_SAFE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

# "error: undeclared identifier 'x', C:\...\File.mq5(4:4)"
_DIAG = re.compile(
    r"^\s*(error|warning|information|info)\s*:\s*(.*?),\s*(.+?)\((\d+)[:,](\d+)\)\s*$",
    re.IGNORECASE)
# "File.mq5(12,5) : error 123: text"  -- classic metaeditor log layout
_DIAG_ALT = re.compile(
    r"^\s*(.+?)\((\d+)[,:](\d+)\)\s*:\s*(error|warning)\s+\d*\s*:?\s*(.*)$",
    re.IGNORECASE)
_SUMMARY = re.compile(r"(\d+)\s+error", re.IGNORECASE)
_SUMMARY_W = re.compile(r"(\d+)\s+warning", re.IGNORECASE)


def _parse_diagnostics(text: str) -> tuple[list[CompileDiagnostic], int, int]:
    diagnostics: list[CompileDiagnostic] = []
    errors = warnings = 0
    for line in text.splitlines():
        match = _DIAG.match(line)
        if match:
            severity = match.group(1).lower()
            severity = "info" if severity.startswith("info") else severity
            diagnostics.append(CompileDiagnostic(
                severity=severity, message=match.group(2).strip(),
                file=match.group(3).strip(), line=int(match.group(4)),
                column=int(match.group(5))))
            continue
        match = _DIAG_ALT.match(line)
        if match:
            diagnostics.append(CompileDiagnostic(
                severity=match.group(4).lower(), message=match.group(5).strip(),
                file=match.group(1).strip(), line=int(match.group(2)),
                column=int(match.group(3))))
    for line in text.splitlines():
        if "error" in line.lower() and "warning" in line.lower():
            found = _SUMMARY.search(line)
            found_w = _SUMMARY_W.search(line)
            if found:
                errors = int(found.group(1))
            if found_w:
                warnings = int(found_w.group(1))
    if not errors:
        errors = sum(1 for d in diagnostics if d.severity == "error")
    if not warnings:
        warnings = sum(1 for d in diagnostics if d.severity == "warning")
    return diagnostics, errors, warnings


def _metrics_model(raw: dict) -> BacktestMetrics:
    normalised = tester_mod.metrics_from_report(raw)
    return BacktestMetrics(**{k: v for k, v in normalised.items()
                              if k in BacktestMetrics.model_fields})


def _tester_expert_path(source: str) -> str:
    """Normalise an expert path into what the ini's Expert= key expects."""
    text = str(source).replace("/", "\\").strip()
    lowered = text.lower()
    marker = "mql5\\experts\\"
    if marker in lowered:
        text = text[lowered.index(marker) + len(marker):]
    elif lowered.startswith("experts\\"):
        text = text[len("experts\\"):]
    if not text.lower().endswith(".ex5"):
        text = re.sub(r"\.mq5$", "", text, flags=re.IGNORECASE) + ".ex5"
    return text


def register(mcp) -> None:
    settings = get_settings()

    @mcp.tool(
        name="mql_compile",
        description=(
            "Compile an MQL5 source file and return structured diagnostics "
            "with file, line and column. Note that MetaEditor reports success "
            "and failure ambiguously — exit code 1 even when it worked — so "
            "success here means zero errors AND a freshly written .ex5, which "
            "is checked explicitly."),
        annotations=WRITE_SAFE,
    )
    def mql_compile(path: str, syntax_only: bool = False) -> CompileResult:
        """Compile one .mq5 file.

        Args:
            path: Path to the .mq5 file, absolute or relative to MQL5/.
            syntax_only: Check syntax without producing an .ex5.
        """
        source = Path(path)
        if not source.is_absolute():
            data_dir = conn().data_dir
            if data_dir:
                source = Path(data_dir) / "MQL5" / path
        if not source.is_file():
            raise ToolFailure(f"{source} does not exist.",
                              "Give the full path to the .mq5 file, or a path "
                              "relative to the terminal's MQL5 folder.")

        output = source.with_suffix(".ex5")
        before = output.stat().st_mtime if output.is_file() else 0.0
        started = time.time()
        text = ""
        route = "cli"

        editor_ok, _reason = native_editor().available()
        if editor_ok and settings.use_native_mcp:
            try:
                tool = "syntax_check" if syntax_only else "compile_file"
                text = native_editor().call(tool, {"path": str(source)},
                                            timeout=300)
                route = "native"
            except NativeUnavailable as exc:
                log.info("native compile unavailable, using the CLI: %s", exc)

        if route == "cli":
            editor = _find_metaeditor()
            if editor is None:
                raise ToolFailure(
                    "MetaEditor64.exe not found.",
                    "Set MT5_TERMINAL_PATH in your .env so the editor can be "
                    "found next to it.")
            log_file = source.with_suffix(".compile.log")
            flag = "/s:" if syntax_only else "/compile:"
            try:
                subprocess.run(
                    [str(editor), f"{flag}{source}", f"/log:{log_file}"],
                    capture_output=True, timeout=300, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ToolFailure(f"Could not run MetaEditor: {exc}",
                                  "Check the path to MetaEditor64.exe.") from exc
            if log_file.is_file():
                text = tester_mod._read_text_any(log_file)
                try:
                    log_file.unlink()
                except OSError:
                    pass

        diagnostics, errors, warnings = _parse_diagnostics(text)
        after = output.stat().st_mtime if output.is_file() else 0.0
        fresh = bool(after and after >= started - 2)
        age = round(time.time() - after, 1) if after else None

        # Success needs both halves. Either alone lies: the log can say "0
        # errors" for a file that produced nothing, and a stale .ex5 from a
        # previous run sits there looking convincing.
        ok = errors == 0 and (syntax_only or fresh)
        if ok:
            message = ("Syntax is clean." if syntax_only
                       else f"Compiled successfully; {output.name} was written.")
            fix = None
        elif errors:
            message = f"Compilation failed with {errors} error(s)."
            fix = ("Fix the first error and compile again — later ones are "
                   "often knock-on effects of it.")
        else:
            message = ("The compiler reported no errors but no fresh .ex5 was "
                       "produced.")
            fix = ("Check that the file really is an expert, indicator or "
                   "script, and that the output folder is writable.")

        return CompileResult(
            ok=ok, source=str(source),
            output=str(output) if output.is_file() else None,
            errors=errors, warnings=warnings, diagnostics=diagnostics,
            ex5_written=fresh, ex5_age_seconds=age, route=route,
            message=message + ("" if fix is None else " " + fix),
            raw_tail="\n".join(text.splitlines()[-12:]) or None)

    @mcp.tool(
        name="backtest",
        description=(
            "Run a Strategy Tester backtest on a compiled expert and return "
            "the performance metrics. Uses the portable command-line tester, "
            "which works on every MT5 build; when the terminal's built-in MCP "
            "server is available it is used instead because it is faster. "
            "mt5_doctor reports which route is active."),
        annotations=WRITE_SAFE,
    )
    def backtest(expert: str, symbol: str, timeframe: str = "H1",
                 start: str = "", end: str = "", model: str = "open_prices",
                 deposit: float = 10000.0, leverage: int = 100,
                 prefer_native: bool = True) -> BacktestResult:
        """Backtest one expert advisor.

        Args:
            expert: Path to the compiled .ex5, or a path under MQL5/Experts.
            symbol: Symbol to test on; resolved automatically.
            timeframe: Chart timeframe, e.g. H1.
            start: Start date, YYYY-MM-DD.
            end: End date, YYYY-MM-DD.
            model: every_tick, 1min_ohlc, open_prices, math or real_ticks.
            deposit: Starting balance for the test.
            leverage: Leverage for the test account.
            prefer_native: Use the terminal's built-in tester when available.
        """
        name = resolve_symbol(symbol)
        started = time.time()

        if prefer_native and settings.use_native_mcp:
            native_ok, _reason = native_terminal().available()
            if native_ok:
                try:
                    return _backtest_native(expert, name, timeframe, start, end,
                                            model, deposit, leverage, started)
                except NativeUnavailable as exc:
                    log.info("native backtest unavailable, using the CLI: %s", exc)

        setup = tester_mod.check_setup(settings.tester_terminal_path)
        if not setup.ready:
            raise ToolFailure(setup.problem or "The tester is not set up.",
                              setup.fix)
        try:
            raw = tester_mod.run_backtest_cli(
                tester_exe=str(setup.exe),
                expert=_tester_expert_path(expert), symbol=name,
                period=timeframe.upper(), from_date=start, to_date=end,
                model=model, deposit=deposit, leverage=leverage,
                timeout_sec=settings.tester_timeout_sec)
        except tester_mod.TesterError as exc:
            raise ToolFailure(str(exc), exc.fix) from exc

        return BacktestResult(
            ok=True, expert=expert, symbol=name, timeframe=timeframe.upper(),
            model=model, **{"from": start or "(broker start)"},
            to=end or "(now)", deposit=deposit, leverage=leverage, route="cli",
            duration_seconds=raw["duration_seconds"],
            metrics=_metrics_model(raw["raw_metrics"]),
            report_html=raw["report_html"], report_images=raw["report_images"],
            raw_metrics=raw["raw_metrics"],
            message=f"Backtest finished in {raw['duration_seconds']}s.",
            log_tail=raw["log_tail"])

    @mcp.tool(
        name="backtest_report",
        description=(
            "Read the metrics out of a Strategy Tester HTML report that "
            "already exists on disk, without re-running the test."),
        annotations=READ_ONLY,
    )
    def backtest_report(path: str) -> dict:
        report = Path(path)
        if not report.is_file():
            raise ToolFailure(f"{report} does not exist.",
                              "Pass the path returned by the backtest tool.")
        raw = tester_mod.parse_report_html(report)
        if not raw:
            raise ToolFailure(f"{report} is not a readable MT5 report.",
                              "MT5 writes these as UTF-16 HTML. Check that the "
                              "file is the tester's own output.")
        return {"ok": True, "path": str(report),
                "metrics": _metrics_model(raw).model_dump(exclude_none=True),
                "raw_metrics": raw}

    @mcp.tool(
        name="backtest_compare",
        description=(
            "Run the same expert over several symbols or timeframes and rank "
            "the results by a chosen metric. Use this to see whether an edge "
            "holds up outside the one chart it was designed on."),
        annotations=WRITE_SAFE,
    )
    def backtest_compare(expert: str, symbols: list[str],
                         timeframes: list[str] | None = None,
                         start: str = "", end: str = "",
                         model: str = "open_prices", deposit: float = 10000.0,
                         leverage: int = 100,
                         criterion: str = "profit_factor") -> BacktestComparison:
        """Compare one expert across several markets.

        Args:
            expert: Path to the compiled .ex5.
            symbols: Symbols to test.
            timeframes: Timeframes to test. Defaults to ["H1"].
            start: Start date, YYYY-MM-DD.
            end: End date, YYYY-MM-DD.
            model: Tick model.
            deposit: Starting balance.
            leverage: Leverage.
            criterion: Metric to rank by, e.g. profit_factor or net_profit.
        """
        if not symbols:
            raise ToolFailure("No symbols given.", "Pass at least one symbol.")
        frames = timeframes or ["H1"]
        if criterion not in BacktestMetrics.model_fields:
            raise ToolFailure(f"Unknown criterion {criterion!r}.",
                              "Use one of: "
                              + ", ".join(sorted(BacktestMetrics.model_fields)))
        runs: list[BacktestResult] = []
        for one in symbols:
            for frame in frames:
                runs.append(backtest(expert=expert, symbol=one, timeframe=frame,
                                     start=start, end=end, model=model,
                                     deposit=deposit, leverage=leverage))
        ranking = sorted(
            ({"symbol": r.symbol, "timeframe": r.timeframe,
              criterion: getattr(r.metrics, criterion),
              "net_profit": r.metrics.net_profit,
              "total_trades": r.metrics.total_trades} for r in runs),
            key=lambda row: (row[criterion] is None, -(row[criterion] or 0.0)))
        thin = [r for r in ranking if (r["total_trades"] or 0) < 30]
        note = ""
        if thin:
            note = (f"{len(thin)} of {len(ranking)} runs made fewer than 30 "
                    f"trades. Ranking on so few results says more about luck "
                    f"than about the strategy.")
        return BacktestComparison(runs=runs, ranking=ranking,
                                  criterion=criterion, note=note)

    @mcp.tool(
        name="backtest_cleanup",
        description=(
            "Delete the portable tester's caches. The downloaded history under "
            "'bases' grows with every new symbol and timeframe — 98 MB after "
            "six short backtests in testing. It is a cache: removing it costs "
            "one re-download, no user data."),
        annotations=WRITE_SAFE,
    )
    def backtest_cleanup() -> dict:
        setup = tester_mod.check_setup(settings.tester_terminal_path)
        if setup.data_dir is None:
            raise ToolFailure("No portable tester is configured.",
                              "Run install.bat first.")
        before = tester_mod.tester_dir_size_mb(str(setup.data_dir))
        result = tester_mod.clean_tester_caches(str(setup.data_dir))
        return {"ok": True, "size_before_mb": before, **result}


def _backtest_native(expert: str, symbol: str, timeframe: str, start: str,
                     end: str, model: str, deposit: float, leverage: int,
                     started: float) -> BacktestResult:
    """Fast path through the terminal's built-in tester."""
    client = native_terminal()
    workspace = client.call_json("get_workspace_info")
    mql5 = workspace["workspace"]["mql5_folder"]
    expert_path = expert if Path(expert).is_absolute() else str(
        Path(mql5) / "Experts" / _tester_expert_path(expert))
    config = str(Path(mql5) / "Profiles" / "Tester" / "mt5mcp_native.ini")

    def iso(value: str, fallback: str) -> str:
        text = (value or fallback).strip()
        return text if "T" in text or " " in text else f"{text}T00:00:00"

    client.call("tester_prepare_config", {
        "expert_path": expert_path, "symbol": symbol,
        "timeframe": timeframe.upper(), "model": model.replace("_", " "),
        "from_date": iso(start, "2020-01-01"),
        "to_date": iso(end, "2026-01-01"),
        "deposit": deposit, "leverage": leverage,
        # Always explicit: the terminal remembers the previous run's mode, and
        # omitting this makes a plain backtest fail with "tester is configured
        # for optimization".
        "optimization": False,
        "output_path": config})
    run = client.call_json("tester_run_backtest",
                           {"config_path": config, "wait": True,
                            "timeout_sec": 900}, timeout=1000)
    run_id = str(run.get("run_id") or "")
    if not run.get("ok") or run_id in ("", "0"):
        raise NativeUnavailable(f"native tester did not start: {run}")
    raw = client.call_json("tester_get_report", {"run_id": run_id}, timeout=300)

    mapping = {
        "net_profit": "profit", "gross_profit": "gross_profit",
        "gross_loss": "gross_loss", "profit_factor": "profit_factor",
        "expected_payoff": "expected_payoff",
        "recovery_factor": "recovery_factor", "sharpe_ratio": "sharpe_ratio",
        "balance_drawdown_max": "max_drawdown_balance",
        "balance_drawdown_percent": "drawdown_percent_balance",
        "equity_drawdown_max": "max_drawdown_equity",
        "equity_drawdown_percent": "drawdown_percent_equity",
        "total_trades": "trades", "profit_trades": "profit_trades",
        "loss_trades": "loss_trades", "short_trades": "short_trades",
        "long_trades": "long_trades", "initial_deposit": "initial_deposit",
    }
    metrics = BacktestMetrics(**{
        ours: raw.get(theirs) for ours, theirs in mapping.items()
        if raw.get(theirs) is not None})
    return BacktestResult(
        ok=True, expert=expert, symbol=symbol, timeframe=timeframe.upper(),
        model=model, **{"from": start or "(broker start)"}, to=end or "(now)",
        deposit=deposit, leverage=leverage, route="native",
        duration_seconds=round(time.time() - started, 1), metrics=metrics,
        raw_metrics=raw,
        message="Backtest finished via the terminal's built-in tester.")


def _find_metaeditor() -> Path | None:
    settings = get_settings()
    candidates = []
    if settings.terminal_path:
        candidates.append(Path(settings.terminal_path).parent / "MetaEditor64.exe")
    if settings.tester_terminal_path:
        candidates.append(
            Path(settings.tester_terminal_path).parent / "MetaEditor64.exe")
    from ..core.connection import find_terminals
    for found in find_terminals():
        candidates.append(Path(found.exe).parent / "MetaEditor64.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
