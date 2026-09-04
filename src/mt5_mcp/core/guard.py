"""The safety rail. Every write tool goes through here, without exception.

Design rules, in order of importance:

1. **Fail closed.** If a check cannot be evaluated — the account read fails, the
   history query errors — the trade is blocked and the reason says so. A rail
   that opens when it is confused is not a rail.
2. **Live accounts are blocked by default.** ``MT5_ALLOW_LIVE=false`` is the
   shipped default and reads stay allowed, so a participant who points this at a
   funded account gets a refusal rather than a fill.
3. **Nothing runs on a timer.** There is no scheduler, no background thread that
   trades, no auto-flatten. The rail only ever answers a question a human asked.
   MTX has an ``enforce`` mode that closes positions unattended; that is
   deliberately not carried over.
4. **Other people's positions are untouched.** Anything whose magic number is
   not ours needs ``include_foreign=True`` spelled out.

Provenance: the guardrail *set* (max positions, daily loss, day-start balance
derived from today's closing deals) follows
https://github.com/FXDavid-OffbeatForex/MTX (MIT, (c) 2026 davidcyberdash),
commit 908ca9b, core/mm.py. The unattended-enforcement half of that module is
intentionally not adopted. Structure and wording are own work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5

from ..config import Settings, get_settings
from .models import GuardVerdict

log = logging.getLogger(__name__)

__all__ = ["Guard", "GuardBlocked", "TradeIntent", "get_guard"]

_CONFIRM_PHRASE = "CLOSE ALL"


class GuardBlocked(RuntimeError):
    """Raised when the rail refuses. Carries the verdict for the response."""

    def __init__(self, verdict: GuardVerdict) -> None:
        super().__init__(verdict.reason or "blocked by the safety rail")
        self.verdict = verdict


@dataclass
class TradeIntent:
    """What a write tool is about to do, in the rail's own vocabulary."""

    action: str
    """'open', 'modify', 'close', 'cancel' or 'close_batch'."""
    symbol: str | None = None
    volume: float | None = None
    risk_percent: float | None = None
    scope: str | None = None
    confirm: str | None = None
    include_foreign: bool = False
    target_magics: list[int] = field(default_factory=list)
    dry_run: bool = True


class Guard:
    """Evaluates one :class:`TradeIntent` against the configured limits."""

    def __init__(self, connection, settings: Settings | None = None) -> None:
        self._conn = connection
        self._settings = settings or get_settings()
        self._trade_log = self._open_trade_log()

    # -- logging ----------------------------------------------------------

    def _open_trade_log(self) -> Path | None:
        try:
            directory = Path(self._settings.log_dir or ".")
            directory.mkdir(parents=True, exist_ok=True)
            return directory / "trades.log"
        except OSError:
            log.warning("cannot create the trades.log directory", exc_info=True)
            return None

    def record(self, intent: TradeIntent, request: dict[str, Any] | None,
               response: Any, retcode: int | None) -> None:
        """Append one attempt to ``trades.log``.

        Every attempt is written, including dry runs and refusals, because the
        question this file answers later is "what did the assistant try to do",
        not "what got filled".
        """
        if self._trade_log is None:
            return
        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        safe_request = dict(request or {})
        for secret in ("password", "login"):
            safe_request.pop(secret, None)
        line = (f"{stamp}\t{intent.action}\t{intent.symbol or '-'}\t"
                f"dry_run={intent.dry_run}\tretcode={retcode}\t"
                f"request={safe_request}\tresponse={response}")
        try:
            with self._trade_log.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            log.warning("could not write to trades.log", exc_info=True)

    # -- individual checks ------------------------------------------------

    def _account(self):
        info = self._conn.call(mt5.account_info)
        if info is None:
            raise RuntimeError("account_info() returned nothing")
        return info

    def _positions(self):
        rows = self._conn.call(mt5.positions_get)
        return list(rows) if rows else []

    def _day_start_balance(self, account) -> float:
        """Balance at midnight, derived from today's realised closing deals.

        MT5 exposes no "balance at midnight" figure, so it is reconstructed:
        current balance minus everything realised since local midnight. Broker
        midnight would be more correct in principle, but a daily loss limit is a
        rule the *trader* keeps, and the trader's day is their own.
        """
        midnight = datetime.combine(datetime.now().date(), dtime.min)
        deals = self._conn.call(mt5.history_deals_get, midnight, datetime.now())
        if deals is None:
            raise RuntimeError(f"history_deals_get() failed: {mt5.last_error()}")
        realised = sum(float(d.profit) + float(d.commission) + float(d.swap)
                       for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)
        return float(account.balance) - realised

    # -- the entry point --------------------------------------------------

    def evaluate(self, intent: TradeIntent) -> GuardVerdict:
        """Decide whether ``intent`` may proceed. Never raises for a rule miss."""
        passed: list[str] = []

        def block(what: str, reason: str, fix: str) -> GuardVerdict:
            verdict = GuardVerdict(allowed=False, dry_run=intent.dry_run,
                                   checks_passed=passed, blocked_by=what,
                                   reason=reason, fix=fix)
            self.record(intent, None, f"BLOCKED {what}", None)
            return verdict

        # 1. account type -------------------------------------------------
        try:
            account = self._account()
        except Exception as exc:  # noqa: BLE001 - fail closed on purpose
            return block("account_unreadable",
                         f"Could not read the account: {exc}",
                         "Check that the terminal is running and logged in, then "
                         "run mt5_doctor.")

        is_live = int(account.trade_mode) == 2
        if is_live and not self._settings.allow_live:
            return block("live_account",
                         "This is a LIVE account and MT5_ALLOW_LIVE is false.",
                         "Reading stays available. To trade a live account, set "
                         "MT5_ALLOW_LIVE=true in your .env and restart the "
                         "server — do that deliberately, not by accident.")
        passed.append("account_type")

        # 2. terminal permits trading at all ------------------------------
        terminal = self._conn.call(mt5.terminal_info)
        if terminal is not None and not getattr(terminal, "trade_allowed", False):
            return block("algo_trading_off",
                         "Algorithmic trading is switched off in the terminal.",
                         "Click «Algo Trading» in the terminal toolbar until the "
                         "icon turns green, then try again.")
        if not getattr(account, "trade_allowed", True):
            return block("account_trade_disabled",
                         "The broker has trading disabled for this account.",
                         "Contact your broker. Expired demo accounts show this too.")
        passed.append("trading_enabled")

        # 3. batch close needs the phrase ---------------------------------
        if intent.action == "close_batch" and (intent.scope or "").lower() == "all":
            if (intent.confirm or "").strip().upper() != _CONFIRM_PHRASE:
                return block("confirmation_missing",
                             "Closing every position needs an explicit confirmation.",
                             f"Pass confirm=\"{_CONFIRM_PHRASE}\" to prove this is "
                             f"intended.")
            passed.append("confirmation")

        # 4. lot ceiling --------------------------------------------------
        if intent.volume is not None and intent.action == "open":
            if intent.volume > self._settings.max_lot:
                return block("max_lot",
                             f"Volume {intent.volume} exceeds MT5_MAX_LOT "
                             f"({self._settings.max_lot}).",
                             f"Reduce the size to {self._settings.max_lot} or less, "
                             f"or raise MT5_MAX_LOT in your .env.")
            passed.append("max_lot")

        # 5. risk ceiling -------------------------------------------------
        if intent.risk_percent is not None:
            if intent.risk_percent > self._settings.max_risk_percent:
                return block("max_risk_percent",
                             f"Risk {intent.risk_percent}% exceeds "
                             f"MT5_MAX_RISK_PERCENT "
                             f"({self._settings.max_risk_percent}%).",
                             "Lower the risk, or raise the limit in your .env.")
            passed.append("max_risk_percent")

        # 6. position count and daily loss --------------------------------
        if intent.action == "open":
            try:
                positions = self._positions()
            except Exception as exc:  # noqa: BLE001 - fail closed
                return block("positions_unreadable",
                             f"Could not read open positions: {exc}",
                             "Run mt5_doctor to check the terminal connection.")
            if len(positions) >= self._settings.max_open_positions:
                return block("max_open_positions",
                             f"{len(positions)} positions are open, the limit is "
                             f"{self._settings.max_open_positions}.",
                             "Close something first, or raise "
                             "MT5_MAX_OPEN_POSITIONS in your .env.")
            passed.append("max_open_positions")

            try:
                day_start = self._day_start_balance(account)
                floating = sum(float(p.profit) for p in positions)
                change = (float(account.balance) - day_start) + floating
                loss = -change if change < 0 else 0.0
                limit = self._settings.daily_loss_limit_percent
                if day_start > 0 and limit > 0:
                    pct = loss / day_start * 100.0
                    if pct >= limit:
                        return block(
                            "daily_loss_limit",
                            f"Today's loss is {loss:.2f} ({pct:.2f}% of "
                            f"{day_start:.2f}), the limit is {limit}%.",
                            "No new positions today. This resets at midnight, or "
                            "you can raise MT5_DAILY_LOSS_LIMIT_PERCENT.")
                passed.append("daily_loss_limit")
            except Exception as exc:  # noqa: BLE001 - fail closed
                return block("daily_loss_unreadable",
                             f"Could not evaluate the daily loss limit: {exc}",
                             "Run mt5_doctor to check the terminal connection.")

        return GuardVerdict(allowed=True, dry_run=intent.dry_run,
                            checks_passed=passed)

    def require(self, intent: TradeIntent) -> GuardVerdict:
        """Like :meth:`evaluate` but raises :class:`GuardBlocked` on refusal."""
        verdict = self.evaluate(intent)
        if not verdict.allowed:
            raise GuardBlocked(verdict)
        return verdict

    # -- ownership --------------------------------------------------------

    def owns(self, magic: int | None) -> bool:
        return int(magic or 0) == int(self._settings.magic)

    def filter_ours(self, rows: list, include_foreign: bool) -> tuple[list, int]:
        """Split rows into "may be touched" and a count of what was withheld."""
        if include_foreign:
            return list(rows), 0
        keep = [r for r in rows if self.owns(getattr(r, "magic", 0))]
        return keep, len(rows) - len(keep)

    @property
    def magic(self) -> int:
        return int(self._settings.magic)

    @property
    def settings(self) -> Settings:
        return self._settings


_singleton: Guard | None = None


def get_guard(connection=None) -> Guard:
    global _singleton
    if _singleton is None:
        from .connection import get_connection
        _singleton = Guard(connection or get_connection())
    return _singleton
