"""Configuration, read once from ``.env`` and the environment.

Credentials live here and nowhere else. They are never logged, never returned
from a tool, and the account number leaves this module only masked.

Own work. No third-party code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

__all__ = ["Settings", "get_settings", "mask_login", "reload_settings"]


def _project_root() -> Path:
    """Repository root — three levels up from ``src/mt5_mcp/config.py``."""
    return Path(__file__).resolve().parents[2]


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "ja", "wahr")


def _as_float(raw: str | None, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def mask_login(login: int | str | None) -> str:
    """``123456789`` -> ``***6789``. Used everywhere a login would be shown.

    Short logins are masked entirely rather than partially, because revealing
    four of five digits is not masking.
    """
    if login in (None, "", 0):
        return "(not connected)"
    s = str(login).strip()
    if len(s) <= 4:
        return "*" * len(s)
    return "***" + s[-4:]


@dataclass(frozen=True)
class Settings:
    """Everything the server reads from the environment."""

    # --- connection -------------------------------------------------------
    terminal_path: str | None = None
    login: int | None = None
    password: str | None = field(default=None, repr=False)
    server: str | None = None

    # --- safety rail (Etappe 5) -------------------------------------------
    allow_live: bool = False
    max_lot: float = 1.0
    max_risk_percent: float = 2.0
    max_open_positions: int = 10
    daily_loss_limit_percent: float = 5.0
    magic: int = 770001

    # --- strategy tester --------------------------------------------------
    tester_terminal_path: str | None = None
    """Dedicated *portable* copy of terminal64.exe used for backtests.

    The tester cannot share an install with the interactive terminal: a second
    non-portable instance of the same data folder exits immediately without
    running anything. ``install.bat`` creates this copy.
    """
    tester_timeout_sec: int = 900

    # --- native MCP accelerator (optional, build >= 6060) -----------------
    native_mcp_key: str | None = field(default=None, repr=False)
    native_mcp_terminal_url: str = "http://127.0.0.1:22346/mcp"
    native_mcp_editor_url: str = "http://127.0.0.1:22345/mcp"
    use_native_mcp: bool = True
    """When False the server always takes the CLI path, even if native is up.

    Useful for reproducing a course participant's problem on an older build.
    """

    # --- housekeeping -----------------------------------------------------
    log_dir: str | None = None
    log_level: str = "INFO"
    timezone_display: str = "Europe/Berlin"

    @property
    def masked_login(self) -> str:
        return mask_login(self.login)

    def has_credentials(self) -> bool:
        return bool(self.login and self.password and self.server)

    def safe_summary(self) -> dict:
        """A dict that is safe to log or return from a tool."""
        return {
            "terminal_path": self.terminal_path,
            "account": self.masked_login,
            "server": self.server,
            "allow_live": self.allow_live,
            "max_lot": self.max_lot,
            "max_risk_percent": self.max_risk_percent,
            "max_open_positions": self.max_open_positions,
            "daily_loss_limit_percent": self.daily_loss_limit_percent,
            "magic": self.magic,
            "native_mcp_enabled": self.use_native_mcp,
            "native_mcp_key_set": bool(self.native_mcp_key),
            "display_timezone": self.timezone_display,
        }


def _build() -> Settings:
    root = _project_root()
    # override=False: a real environment variable beats the file, which is what
    # you want when install.bat or a test harness sets one deliberately.
    load_dotenv(root / ".env", override=False)

    g = os.environ.get
    log_dir = g("MT5_LOG_DIR") or str(root / "logs")

    return Settings(
        terminal_path=g("MT5_TERMINAL_PATH") or None,
        login=_as_int(g("MT5_LOGIN"), 0) or None,
        password=g("MT5_PASSWORD") or None,
        server=g("MT5_SERVER") or None,
        allow_live=_as_bool(g("MT5_ALLOW_LIVE"), False),
        max_lot=_as_float(g("MT5_MAX_LOT"), 1.0),
        max_risk_percent=_as_float(g("MT5_MAX_RISK_PERCENT"), 2.0),
        max_open_positions=_as_int(g("MT5_MAX_OPEN_POSITIONS"), 10),
        daily_loss_limit_percent=_as_float(g("MT5_DAILY_LOSS_LIMIT_PERCENT"), 5.0),
        magic=_as_int(g("MT5_MAGIC"), 770001),
        tester_terminal_path=g("MT5_TESTER_TERMINAL_PATH") or None,
        tester_timeout_sec=_as_int(g("MT5_TESTER_TIMEOUT_SEC"), 900),
        native_mcp_key=g("MT5_NATIVE_MCP_KEY") or None,
        native_mcp_terminal_url=g("MT5_NATIVE_MCP_TERMINAL_URL")
        or "http://127.0.0.1:22346/mcp",
        native_mcp_editor_url=g("MT5_NATIVE_MCP_EDITOR_URL")
        or "http://127.0.0.1:22345/mcp",
        use_native_mcp=_as_bool(g("MT5_USE_NATIVE_MCP"), True),
        log_dir=log_dir,
        log_level=(g("MT5_LOG_LEVEL") or "INFO").upper(),
        timezone_display=g("MT5_TIMEZONE_DISPLAY") or "Europe/Berlin",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _build()


def reload_settings() -> Settings:
    """Drop the cache and re-read. Only for tests and ``install.bat`` checks."""
    get_settings.cache_clear()
    return get_settings()
