"""Headless Strategy Tester over ``terminal64.exe /config:``.

This is the **default** backtest route, not a fallback. It works on every MT5
build, needs no API key and no GUI, which matters because course participants sit
on broker terminals that lag the MetaQuotes release by months. The native MCP
route (build 6060+) is an accelerator layered on top, not a replacement.

Three things were measured on a live terminal and are easy to get wrong:

1. **A second non-portable instance of the same data folder exits in ~6 seconds
   without running anything.** The interactive terminal the Python API depends on
   therefore blocks the tester. The fix is a dedicated ``/portable`` copy of
   ``terminal64.exe``; ``install.bat`` creates it from the user's own install.
2. **``/config:`` cannot cope with spaces in the path.** ``subprocess`` quotes the
   whole argument and MT5 reads the closing quote as part of the filename, then
   logs ``cannot load config "..."" at start`` and carries on with defaults. The
   tester directory must live somewhere without spaces.
3. **``Report=`` must be a RELATIVE path.** An absolute one is discarded in
   silence: no file, no warning, exit code 0. The report lands next to the
   portable data folder.

The portable copy inherits the interactive terminal's encrypted ``accounts.dat``,
so it logs in on its own and no plaintext password is written anywhere.

Provenance: the ``/config:`` + ini + report-parsing approach is adapted from
https://github.com/FXDavid-OffbeatForex/MBT (MIT, (c) 2026 davidcyberdash),
commit 186eb6b, core/tester.py, and from
https://github.com/PHUICMT/mcp-mt5 (MIT, (c) 2026 PHUICMT), commit 9a859e1.
The three findings above and the portable-copy design are own work.
"""

from __future__ import annotations

import glob
import html
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "TesterError", "TesterSetup", "build_tester_ini", "run_backtest_cli",
    "parse_report_html", "prepare_tester_copy", "tester_dir_size_mb",
    "clean_tester_caches", "MODEL_CODES", "MODEL_NAMES",
]

MODEL_CODES = {
    "every_tick": 0, "every tick": 0,
    "1min_ohlc": 1, "m1_ohlc": 1, "m1 ohlc": 1, "ohlc": 1,
    "open_prices": 2, "open prices": 2, "open": 2,
    "math": 3, "math_calculations": 3, "math calculations": 3,
    "real_ticks": 4, "real ticks": 4,
}
MODEL_NAMES = {0: "every_tick", 1: "1min_ohlc", 2: "open_prices",
               3: "math", 4: "real_ticks"}

_PERIODS = {"M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20",
            "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1"}

# Files MT5 leaves beside the report; all are useful output for the user.
_REPORT_IMAGE_SUFFIXES = ("", "-hst", "-mfemae", "-holding")


class TesterError(RuntimeError):
    """Backtest could not run. Carries a fix sentence."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


@dataclass(frozen=True)
class TesterSetup:
    """Where the portable tester lives and whether it is usable."""

    exe: Path | None
    data_dir: Path | None
    ready: bool
    problem: str | None = None
    fix: str | None = None


def _has_space(path: str | Path) -> bool:
    return " " in str(path)


def check_setup(tester_exe: str | None) -> TesterSetup:
    """Validate the configured tester copy without running anything."""
    if not tester_exe:
        return TesterSetup(
            None, None, False,
            "No portable tester terminal is configured.",
            "Run install.bat, which copies terminal64.exe to a path without "
            "spaces and sets MT5_TESTER_TERMINAL_PATH in your .env.")
    exe = Path(tester_exe)
    if not exe.is_file():
        return TesterSetup(
            exe, None, False,
            f"MT5_TESTER_TERMINAL_PATH points at {exe}, which does not exist.",
            "Re-run install.bat to recreate the portable tester copy.")
    if _has_space(exe.parent):
        return TesterSetup(
            exe, exe.parent, False,
            f"The tester directory {exe.parent} contains a space.",
            "MT5 cannot load a /config: path containing spaces — it fails "
            "silently. Move the tester copy somewhere like C:\\mt5tester.")
    return TesterSetup(exe, exe.parent, True)


# ---------------------------------------------------------------------------
# Building the portable copy
# ---------------------------------------------------------------------------


def prepare_tester_copy(source_exe: str, source_data_dir: str,
                        target_dir: str) -> dict:
    """Create the portable tester from the user's own installation.

    Copies only what the tester needs: the two executables, the encrypted
    credential store so it can log in unattended, and the compiled MQL5 tree so
    it does not spend four minutes recompiling the examples on first start.

    No MetaQuotes binary is ever shipped with this project — everything here is
    copied from what the user already has installed.
    """
    src_exe = Path(source_exe)
    src_data = Path(source_data_dir)
    target = Path(target_dir)
    if _has_space(target):
        raise TesterError(
            f"The tester directory {target} contains a space.",
            "Choose a path without spaces, for example C:\\mt5tester.")
    if not src_exe.is_file():
        raise TesterError(f"{src_exe} does not exist.",
                          "Point MT5_TERMINAL_PATH at a real terminal64.exe.")

    target.mkdir(parents=True, exist_ok=True)
    (target / "config").mkdir(exist_ok=True)
    copied: list[str] = []

    for name in ("terminal64.exe", "metatester64.exe"):
        source = src_exe.parent / name
        if source.is_file():
            shutil.copy2(source, target / name)
            copied.append(name)

    for name in ("accounts.dat", "common.ini", "servers.dat"):
        source = src_data / "config" / name
        if source.is_file():
            shutil.copy2(source, target / "config" / name)
            copied.append(f"config/{name}")

    mql5_src = src_data / "MQL5"
    if mql5_src.is_dir():
        shutil.copytree(mql5_src, target / "MQL5", dirs_exist_ok=True)
        copied.append("MQL5/")

    return {
        "target": str(target),
        "copied": copied,
        "exe": str(target / "terminal64.exe"),
        "size_mb": tester_dir_size_mb(str(target)),
    }


def tester_dir_size_mb(target_dir: str | None) -> float:
    if not target_dir or not Path(target_dir).is_dir():
        return 0.0
    total = 0
    for root, _dirs, files in os.walk(target_dir):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return round(total / (1024 * 1024), 1)


def clean_tester_caches(target_dir: str) -> dict:
    """Delete the tester's regenerable caches.

    ``bases`` (downloaded history) reached 98 MB after six short backtests and
    keeps growing with every new symbol and timeframe. It is a cache: removing
    it costs one re-download, not any user data.
    """
    target = Path(target_dir)
    freed = 0.0
    removed: list[str] = []
    for name in ("bases", "temp", "Tester/cache", "Tester/bases", "logs"):
        path = target / name
        if not path.is_dir():
            continue
        size = tester_dir_size_mb(str(path))
        try:
            shutil.rmtree(path)
        except OSError:
            log.warning("could not remove %s", path, exc_info=True)
            continue
        freed += size
        removed.append(name)
    return {"removed": removed, "freed_mb": round(freed, 1),
            "size_after_mb": tester_dir_size_mb(target_dir)}


# ---------------------------------------------------------------------------
# The ini
# ---------------------------------------------------------------------------


def _fmt_date(value: Any) -> str:
    """Accept 'YYYY-MM-DD', 'YYYY.MM.DD' or a datetime; emit MT5's 'YYYY.MM.DD'."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y.%m.%d")
    text = str(value).strip()
    text = text.split("T")[0].split(" ")[0]
    return text.replace("-", ".").replace("/", ".")


def build_tester_ini(*, expert: str, symbol: str, period: str = "H1",
                     from_date: Any = None, to_date: Any = None,
                     model: str = "open_prices", deposit: float = 10000.0,
                     leverage: int = 100, currency: str = "USD",
                     report_name: str = "", set_file: str = "",
                     optimization: int = 0,
                     optimization_criterion: int = 0) -> str:
    """Return the text of a ``[Tester]`` ini.

    ``report_name`` must be a bare relative name — see the module docstring.
    ``optimization`` is always written explicitly: the terminal remembers the
    previous run's mode, and omitting the key makes a plain backtest fail with
    ``tester is configured for optimization``.
    """
    period_key = str(period).strip().upper()
    if period_key not in _PERIODS:
        raise TesterError(f"Unknown tester period {period!r}.",
                          "Use one of: " + ", ".join(sorted(_PERIODS)))
    model_code = MODEL_CODES.get(str(model).strip().lower())
    if model_code is None:
        raise TesterError(
            f"Unknown tick model {model!r}.",
            "Use every_tick, 1min_ohlc, open_prices, math or real_ticks.")
    if report_name and (os.path.isabs(report_name) or _has_space(report_name)):
        raise TesterError(
            f"The report name {report_name!r} must be relative and space-free.",
            "MT5 silently discards an absolute Report= path. Pass a bare file "
            "name such as 'mt5mcp_run'.")

    lines = [
        "[Tester]",
        f"Expert={expert}",
        f"Symbol={symbol}",
        f"Period={period_key}",
        f"Model={model_code}",
        f"Optimization={int(optimization)}",
        f"OptimizationCriterion={int(optimization_criterion)}",
        f"Deposit={deposit:.2f}",
        f"Currency={currency}",
        f"Leverage={int(leverage)}",
        "ProfitInPips=0",
        "ExecutionMode=0",
        "Visual=0",
        "ShutdownTerminal=1",
        "ReplaceReport=1",
    ]
    if from_date:
        lines.append(f"FromDate={_fmt_date(from_date)}")
    if to_date:
        lines.append(f"ToDate={_fmt_date(to_date)}")
    if report_name:
        lines.append(f"Report={report_name}")
    if set_file:
        lines.append(f"ExpertParameters={set_file}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def _read_text_any(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            pass
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("cp1252", errors="ignore")


def _tail_log(data_dir: Path, limit: int = 40) -> list[str]:
    """Last lines of the tester agent log — the ground truth when a run fails."""
    out: list[str] = []
    for pattern in ("Tester/logs/*.log", "logs/*.log"):
        for path in sorted(glob.glob(str(data_dir / pattern)))[-2:]:
            text = _read_text_any(Path(path))
            rows = [r.strip() for r in text.splitlines() if r.strip()]
            out.extend(rows[-limit:])
    return out[-limit:]


def run_backtest_cli(*, tester_exe: str, expert: str, symbol: str,
                     period: str = "H1", from_date: Any = None,
                     to_date: Any = None, model: str = "open_prices",
                     deposit: float = 10000.0, leverage: int = 100,
                     currency: str = "USD", set_file: str = "",
                     timeout_sec: int = 900) -> dict:
    """Run one backtest and return the parsed report plus diagnostics."""
    setup = check_setup(tester_exe)
    if not setup.ready:
        raise TesterError(setup.problem or "tester not ready", setup.fix)

    assert setup.exe is not None and setup.data_dir is not None
    data_dir = setup.data_dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_name = f"mt5mcp_{stamp}"
    # The ini deliberately does NOT share the report's prefix. It used to, and
    # the cleanup glob below then matched the config file itself and deleted it
    # microseconds after it was written. MT5 logged "cannot load config ... at
    # start", carried on as a plain interactive terminal, and never exited — so
    # the run looked like a hang rather than a mistake.
    ini_path = data_dir / f"cfg_{stamp}.ini"

    for stale in glob.glob(str(data_dir / (report_name + "*"))):
        try:
            os.remove(stale)
        except OSError:
            pass

    ini_path.write_text(
        build_tester_ini(expert=expert, symbol=symbol, period=period,
                         from_date=from_date, to_date=to_date, model=model,
                         deposit=deposit, leverage=leverage, currency=currency,
                         report_name=report_name, set_file=set_file,
                         optimization=0),
        encoding="utf-8")

    started = time.time()
    try:
        process = subprocess.Popen(
            [str(setup.exe), "/portable", f"/config:{ini_path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise TesterError(f"Could not start the tester terminal: {exc}",
                          "Check MT5_TESTER_TERMINAL_PATH in your .env.") from exc

    timed_out = False
    while process.poll() is None:
        if time.time() - started > timeout_sec:
            process.kill()
            timed_out = True
            break
        time.sleep(1.0)
    duration = time.time() - started

    try:
        ini_path.unlink()
    except OSError:
        pass

    report = data_dir / f"{report_name}.htm"
    if not report.is_file():
        matches = sorted(glob.glob(str(data_dir / (report_name + "*.htm"))))
        report = Path(matches[0]) if matches else report

    tail = _tail_log(data_dir)
    if timed_out:
        raise TesterError(
            f"The backtest did not finish within {timeout_sec} seconds.",
            "Shorten the date range, use the open_prices model, or raise "
            "MT5_TESTER_TIMEOUT_SEC.")
    if not report.is_file():
        hint = "; ".join(tail[-3:]) if tail else "no tester log was produced"
        raise TesterError(
            f"The tester finished but wrote no report. Last log lines: {hint}",
            "Most often the expert path is wrong or the .ex5 is missing. Check "
            "that the compiled expert exists under the tester's MQL5/Experts.")

    images = []
    for suffix in _REPORT_IMAGE_SUFFIXES:
        candidate = data_dir / f"{report_name}{suffix}.png"
        if candidate.is_file():
            images.append(str(candidate))

    metrics = parse_report_html(report)
    return {
        "report_html": str(report),
        "report_images": images,
        "raw_metrics": metrics,
        "duration_seconds": round(duration, 1),
        "log_tail": tail[-15:],
        "model": model,
        "data_dir": str(data_dir),
    }


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

_NUM = re.compile(r"-?[\d\u00a0 ]+(?:\.\d+)?")


def _to_number(text: str) -> float | None:
    """MT5 writes '10 000.00' with a thin space as the thousands separator."""
    if text is None:
        return None
    match = _NUM.search(str(text).replace("\u00a0", " "))
    if not match:
        return None
    try:
        return float(match.group(0).replace(" ", ""))
    except ValueError:
        return None


def _percent_in(text: str) -> float | None:
    match = re.search(r"\(([-\d.]+)%\)", str(text))
    return float(match.group(1)) if match else None


def parse_report_html(path: str | Path) -> dict[str, Any]:
    """Pull the label/value pairs out of MT5's HTML report.

    The file is UTF-16 and its layout is a table of ``Label:`` cells followed by
    value cells. Parsed positionally rather than by row, because MT5 varies the
    number of columns between builds.
    """
    text = _read_text_any(Path(path))
    if not text:
        return {}
    cells = [html.unescape(re.sub(r"<[^>]+>", "", cell)).replace("\u00a0", " ").strip()
             for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", text, re.S | re.I)]
    cells = [c for c in cells if c]
    pairs: dict[str, str] = {}
    for index, cell in enumerate(cells):
        if cell.endswith(":") and index + 1 < len(cells):
            pairs.setdefault(cell.rstrip(":").strip(), cells[index + 1])
    return pairs


#: Report label -> our metric name. Labels are stable across recent builds.
REPORT_FIELDS = {
    "Total Net Profit": "net_profit",
    "Gross Profit": "gross_profit",
    "Gross Loss": "gross_loss",
    "Profit Factor": "profit_factor",
    "Expected Payoff": "expected_payoff",
    "Recovery Factor": "recovery_factor",
    "Sharpe Ratio": "sharpe_ratio",
    "Total Trades": "total_trades",
    "Initial Deposit": "initial_deposit",
}


def metrics_from_report(pairs: dict[str, Any]) -> dict[str, Any]:
    """Normalise the raw label/value pairs into our stable metric names."""
    out: dict[str, Any] = {}
    for label, key in REPORT_FIELDS.items():
        if label in pairs:
            out[key] = _to_number(pairs[label])
    for label, key, pct_key in (
        ("Balance Drawdown Maximal", "balance_drawdown_max",
         "balance_drawdown_percent"),
        ("Equity Drawdown Maximal", "equity_drawdown_max",
         "equity_drawdown_percent"),
    ):
        if label in pairs:
            out[key] = _to_number(pairs[label])
            out[pct_key] = _percent_in(pairs[label])
    for label, key in (("Short Trades (won %)", "short_trades"),
                       ("Long Trades (won %)", "long_trades"),
                       ("Profit Trades (% of total)", "profit_trades"),
                       ("Loss Trades (% of total)", "loss_trades"),
                       ("Largest profit trade", "largest_profit_trade"),
                       ("Largest loss trade", "largest_loss_trade")):
        if label in pairs:
            out[key] = _to_number(pairs[label])
    if "History Quality" in pairs:
        out["history_quality_percent"] = _to_number(pairs["History Quality"])
    for key in ("total_trades", "short_trades", "long_trades",
                "profit_trades", "loss_trades"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out
