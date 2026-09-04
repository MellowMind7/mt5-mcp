"""The one place that talks to the MetaTrader5 SDK.

Two hard constraints shape this module.

**The SDK is not thread-safe.** It keeps process-global state and a single IPC
channel to the terminal. Calling it from two threads corrupts that channel in
ways that surface much later as nonsense data. Every call therefore goes through
one dedicated worker thread. The approach is taken from ariadng's
metatrader-mcp-server (MIT); the queue plumbing here is rewritten.

**stdout belongs to the MCP protocol.** On stdio transport a single stray
``print`` desynchronises the JSON-RPC stream and the client disconnects with an
unhelpful parse error. Logging goes to a file, and the SDK is never allowed to
inherit our stdout.

Provenance: single-worker-thread approach adapted from
https://github.com/ariadng/metatrader-mcp-server (MIT, (c) 2025 Aria Dhanang),
commit bd2ff59. Terminal discovery adapted from
https://github.com/FXDavid-OffbeatForex/MBT (MIT, (c) 2026 davidcyberdash),
commit 186eb6b, core/tester.py. Everything else is own work.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import MetaTrader5 as mt5

from ..config import Settings, get_settings
from .errors import Mt5Error, describe_last_error
from .timeutil import BrokerClock

log = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "Mt5ConnectionError",
    "Mt5Connection",
    "get_connection",
    "find_terminals",
    "TerminalCandidate",
]

# A call that outlives this is a hung terminal, not a slow one. The tester runs
# out-of-process and is not bound by it.
_CALL_TIMEOUT = 120.0

# MT5 refuses a second initialize() too soon after shutdown().
_RECONNECT_COOLDOWN = 2.0


class Mt5ConnectionError(RuntimeError):
    """Raised when the terminal cannot be reached. Carries an actionable sentence."""

    def __init__(self, message: str, detail: Mt5Error | None = None) -> None:
        super().__init__(message)
        self.detail = detail

    def as_dict(self) -> dict:
        out = {"error": str(self)}
        if self.detail is not None:
            out["cause"] = self.detail.as_dict()
        return out


# ---------------------------------------------------------------------------
# Terminal discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalCandidate:
    """One MT5 installation found on this machine."""

    exe: str
    data_dir: str | None
    build: int | None
    company: str | None
    is_running: bool = False

    def as_dict(self) -> dict:
        return {
            "exe": self.exe,
            "data_dir": self.data_dir,
            "build": self.build,
            "vendor": self.company,
            "running": self.is_running,
        }


def _read_text_any(path: Path) -> str:
    """Decode a small MT5 text file that may be UTF-16, UTF-8 or cp1252.

    UTF-16 is trusted only with a BOM: Python's BOM-less utf-16 codec happily
    decodes any even-length byte stream, so a UTF-8 file would come back as
    plausible-looking garbage instead of failing over to the next codec.
    """
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


def _appdata_terminal_base() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "MetaQuotes" / "Terminal" if appdata else None


def _data_dir_for(install_dir: Path) -> str | None:
    """Find the ``%APPDATA%`` data folder belonging to one installation.

    A portable install keeps its data beside the exe; everything else gets a
    hashed folder whose ``origin.txt`` points back at the install.
    """
    if (install_dir / "MQL5").is_dir():
        return str(install_dir)  # portable
    base = _appdata_terminal_base()
    if base is None or not base.is_dir():
        return None
    want = os.path.normcase(str(install_dir).replace("/", "\\"))
    newest: tuple[float, str] | None = None
    for child in base.iterdir():
        if child.name.lower() in ("common", "community") or not child.is_dir():
            continue
        if not (child / "MQL5").is_dir():
            continue
        origin = child / "origin.txt"
        if origin.is_file():
            text = os.path.normcase(_read_text_any(origin).replace("/", "\\"))
            if want in text:
                return str(child)
        stamp = child.stat().st_mtime
        if newest is None or stamp > newest[0]:
            newest = (stamp, str(child))
    return newest[1] if newest else None


def _file_build(exe: Path) -> tuple[int | None, str | None]:
    """Read build number and vendor out of the exe's version resource."""
    try:
        import win32api  # provided by pywin32, a transitive dependency of mcp
    except ImportError:
        return None, None
    try:
        info = win32api.GetFileVersionInfo(str(exe), "\\")
        build = info["FileVersionLS"] & 0xFFFF
        lang, codepage = win32api.GetFileVersionInfo(
            str(exe), "\\VarFileInfo\\Translation")[0]
        key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\CompanyName"
        company = win32api.GetFileVersionInfo(str(exe), key)
        return int(build), company
    except Exception:  # noqa: BLE001 - version resources vary wildly
        return None, None


def _running_terminal_exes() -> set[str]:
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        pass
    running: set[str] = set()
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where", "name='terminal64.exe'",
             "get", "ExecutablePath", "/format:csv"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        for line in out.splitlines():
            part = line.strip().rsplit(",", 1)[-1]
            if part.lower().endswith("terminal64.exe"):
                running.add(os.path.normcase(part))
    except Exception:  # noqa: BLE001 - wmic is deprecated and may be absent
        pass
    return running


def find_terminals(extra: str | None = None) -> list[TerminalCandidate]:
    """Locate every MT5 installation on this machine.

    Ordered so the most plausible choice comes first: a running terminal beats a
    stopped one, and a higher build beats a lower one. That ordering is what
    makes ``install.bat`` able to guess correctly without asking.
    """
    seen: dict[str, Path] = {}
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    ]
    if extra:
        candidate = Path(extra)
        if candidate.is_file():
            seen[os.path.normcase(str(candidate))] = candidate
        elif (candidate / "terminal64.exe").is_file():
            seen[os.path.normcase(str(candidate / "terminal64.exe"))] = \
                candidate / "terminal64.exe"

    for root in roots:
        if not root or not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            exe = child / "terminal64.exe"
            if exe.is_file():
                seen.setdefault(os.path.normcase(str(exe)), exe)

    running = _running_terminal_exes()
    out: list[TerminalCandidate] = []
    for key, exe in seen.items():
        build, company = _file_build(exe)
        out.append(TerminalCandidate(
            exe=str(exe),
            data_dir=_data_dir_for(exe.parent),
            build=build,
            company=company,
            is_running=key in running,
        ))
    out.sort(key=lambda c: (c.is_running, c.build or 0), reverse=True)
    return out


def read_max_bars(data_dir: str | None) -> int | None:
    """Read ``[Charts] MaxBars`` from a terminal's ``common.ini``.

    Returns ``None`` when the setting is absent **or set to unlimited**. Callers
    must treat ``None`` as "no known ceiling" and fall back to probing, not as
    "zero bars allowed" — the value is a user setting and genuinely can be
    unlimited, which is why this does not default to a number.
    """
    if not data_dir:
        return None
    ini = Path(data_dir) / "config" / "common.ini"
    if not ini.is_file():
        return None
    for line in _read_text_any(ini).splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("maxbars"):
            continue
        _, _, raw = stripped.partition("=")
        raw = raw.strip()
        if raw.lower() in ("unlimited", "0", "-1", ""):
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# The single-threaded executor
# ---------------------------------------------------------------------------


class _Worker(threading.Thread):
    """One thread; every SDK call runs here and nowhere else."""

    def __init__(self) -> None:
        super().__init__(name="mt5-sdk", daemon=True)
        self._jobs: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._stopping = threading.Event()

    def run(self) -> None:
        while not self._stopping.is_set():
            job = self._jobs.get()
            if job is None:
                break
            fn, future = job
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - relayed to caller
                    future.set_exception(exc)

    def submit(self, fn: Callable[[], T], timeout: float = _CALL_TIMEOUT) -> T:
        future: Future = Future()
        self._jobs.put((fn, future))
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._stopping.set()
        self._jobs.put(None)


class Mt5Connection:
    """Owns the terminal connection and serialises access to it."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._worker = _Worker()
        self._worker.start()
        self._connected = False
        self._guard = threading.RLock()
        self.clock = BrokerClock()
        self._terminal_path: str | None = None
        self._data_dir: str | None = None
        self._max_bars: int | None = None
        self._max_bars_probed = False

    # -- lifecycle --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def terminal_path(self) -> str | None:
        return self._terminal_path

    @property
    def data_dir(self) -> str | None:
        return self._data_dir

    def connect(self, *, force: bool = False) -> dict:
        """Attach to a running terminal, or start one and log in.

        Passing credentials is optional: when the terminal is already logged in,
        a bare ``initialize(path=...)`` attaches to that session, which is the
        normal case for a course participant who left the terminal open.
        """
        with self._guard:
            if self._connected and not force:
                return self.status()
            if self._connected:
                self._shutdown_locked()
                time.sleep(_RECONNECT_COOLDOWN)

            path = self._settings.terminal_path
            if not path:
                found = find_terminals()
                if not found:
                    raise Mt5ConnectionError(
                        "No MetaTrader 5 installation found on this machine. Install the "
                        "terminal, or put the path to terminal64.exe into your "
                        ".env as MT5_TERMINAL_PATH.")
                path = found[0].exe

            kwargs: dict[str, Any] = {"path": path}
            if self._settings.has_credentials():
                kwargs.update(login=self._settings.login,
                              password=self._settings.password,
                              server=self._settings.server)

            ok = self._worker.submit(lambda: mt5.initialize(**kwargs))
            if not ok:
                detail = describe_last_error(
                    self._worker.submit(mt5.last_error))
                raise Mt5ConnectionError(
                    f"Could not connect to the terminal. {detail.fix}", detail)

            self._connected = True
            self._terminal_path = path
            self.clock.invalidate()
            self._max_bars_probed = False

            info = self._worker.submit(mt5.terminal_info)
            self._data_dir = getattr(info, "data_path", None) if info else None
            self._refresh_clock_locked()
            log.info("connected to %s (build %s), account %s",
                     path, getattr(info, "build", "?"),
                     self._settings.masked_login)
            return self.status()

    def _shutdown_locked(self) -> None:
        try:
            self._worker.submit(mt5.shutdown, timeout=15)
        except Exception:  # noqa: BLE001 - shutting down must never raise
            log.warning("shutdown() complained; ignoring", exc_info=True)
        self._connected = False

    def disconnect(self) -> None:
        with self._guard:
            if self._connected:
                self._shutdown_locked()
                self.clock.invalidate()

    def close(self) -> None:
        self.disconnect()
        self._worker.stop()

    # -- the call path ----------------------------------------------------

    def call(self, fn: Callable[..., T], *args: Any,
             timeout: float = _CALL_TIMEOUT, **kwargs: Any) -> T:
        """Run one SDK function on the worker thread.

        Reconnects once and retries if the terminal dropped out underneath us,
        which happens whenever the user restarts it.
        """
        self.ensure_connected()
        # Build the job without empty keyword arguments. `fn(*args, **{})`
        # compiles to CALL_FUNCTION_EX and hands the C extension an empty kwargs
        # dict rather than NULL; MetaTrader5.order_check rejects that outright
        # with "Unnamed arguments not allowed" and returns None, which looks
        # exactly like a failed pre-flight check. Some SDK functions tolerate it,
        # so the bug only shows up on a few tools.
        job = (lambda: fn(*args, **kwargs)) if kwargs else (lambda: fn(*args))
        try:
            return self._worker.submit(job, timeout=timeout)
        except Mt5ConnectionError:
            raise
        except Exception:
            if not self._looks_alive():
                log.warning("lost the terminal connection, reconnecting")
                self.connect(force=True)
                return self._worker.submit(job, timeout=timeout)
            raise

    def ensure_connected(self) -> None:
        if not self._connected:
            self.connect()

    def _looks_alive(self) -> bool:
        try:
            return self._worker.submit(mt5.terminal_info, timeout=15) is not None
        except Exception:  # noqa: BLE001
            return False

    # -- derived state ----------------------------------------------------

    def _refresh_clock_locked(self, symbol: str = "EURUSD") -> None:
        """Measure the broker offset from any symbol that is ticking."""
        try:
            tick = self._worker.submit(lambda: mt5.symbol_info_tick(symbol),
                                       timeout=20)
            if tick is None or not getattr(tick, "time", 0):
                for fallback in self._worker.submit(
                        lambda: [s.name for s in (mt5.symbols_get() or [])[:40]],
                        timeout=30):
                    tick = self._worker.submit(
                        lambda n=fallback: mt5.symbol_info_tick(n), timeout=20)
                    if tick is not None and getattr(tick, "time", 0):
                        symbol = fallback
                        break
            if tick is not None and getattr(tick, "time", 0):
                self.clock.measure(tick.time, symbol)
        except Exception:  # noqa: BLE001 - a missing offset is not fatal
            log.debug("could not measure the broker offset", exc_info=True)

    def refresh_clock_if_stale(self) -> None:
        if self.clock.is_stale():
            with self._guard:
                self._refresh_clock_locked()

    def max_bars(self) -> int | None:
        """Largest bar count one ``copy_rates`` call will accept.

        Read from ``common.ini`` when it names a number. When the setting is
        absent or unlimited we probe once, because guessing wrong in either
        direction is worse than one extra call: too high fails the whole
        request, too low silently truncates the user's history.
        """
        if self._max_bars_probed:
            return self._max_bars
        configured = read_max_bars(self._data_dir)
        if configured:
            self._max_bars = configured
            self._max_bars_probed = True
            return self._max_bars
        self._max_bars = self._probe_max_bars()
        self._max_bars_probed = True
        return self._max_bars

    def _probe_max_bars(self) -> int | None:
        """Binary-search the ceiling using a symbol with deep history."""
        symbol = "EURUSD"
        try:
            info = self.call(mt5.symbol_info, symbol)
            if info is None:
                symbols = self.call(mt5.symbols_get) or []
                if not symbols:
                    return None
                symbol = symbols[0].name
            self.call(mt5.symbol_select, symbol, True)

            def works(n: int) -> bool:
                rates = self.call(mt5.copy_rates_from_pos,
                                  symbol, mt5.TIMEFRAME_M1, 0, n)
                return rates is not None

            if works(200_000):
                return None  # effectively unlimited
            low, high = 1_000, 200_000
            if not works(low):
                return low
            while low + 1 < high:
                mid = (low + high) // 2
                if works(mid):
                    low = mid
                else:
                    high = mid
            return low
        except Exception:  # noqa: BLE001 - probing must never break a session
            log.debug("MaxBars probe failed", exc_info=True)
            return None

    # -- reporting --------------------------------------------------------

    def status(self) -> dict:
        """Connection state, safe to return from a tool. No credentials."""
        if not self._connected:
            return {"connected": False,
                    "note": "Not connected to the terminal. Call mt5_connect first."}
        info = self.call(mt5.terminal_info)
        account = self.call(mt5.account_info)
        self.refresh_clock_if_stale()
        trade_modes = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
        margin_modes = {0: "NETTING", 1: "EXCHANGE", 2: "HEDGING"}
        out: dict[str, Any] = {
            "connected": True,
            "terminal": {
                "path": self._terminal_path,
                "build": getattr(info, "build", None),
                "data_dir": self._data_dir,
                "broker_connected": getattr(info, "connected", None),
                "algo_trading_enabled": getattr(info, "trade_allowed", None),
                "trade_api_disabled": getattr(info, "tradeapi_disabled", None),
                "ping_ms": (getattr(info, "ping_last", 0) or 0) // 1000,
            },
            "time": self.clock.describe(),
        }
        if account is not None:
            out["account"] = {
                "login": self._settings.masked_login
                if self._settings.login else "***%s" % str(account.login)[-4:],
                "server": account.server,
                "currency": account.currency,
                "type": trade_modes.get(account.trade_mode, "?"),
                "margin_mode": margin_modes.get(account.margin_mode, "?"),
                "leverage": account.leverage,
                "balance": account.balance,
                "equity": account.equity,
                "free_margin": account.margin_free,
                "trade_allowed": account.trade_allowed,
                "experts_allowed": account.trade_expert,
            }
        return out


_singleton: Mt5Connection | None = None
_singleton_lock = threading.Lock()


def get_connection() -> Mt5Connection:
    """The process-wide connection. There is deliberately only ever one."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Mt5Connection()
        return _singleton
