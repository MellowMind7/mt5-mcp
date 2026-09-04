"""Plain-English translations for every MT5 error a user can hit.

Two distinct code spaces exist and are routinely confused:

* **Trade server retcodes** (``TRADE_RETCODE_*``, 10004..10046) arrive in
  ``OrderSendResult.retcode`` / ``OrderCheckResult.retcode``. They describe what
  the *broker* did with an order.
* **Terminal error codes** (``RES_*``, -10000..1) come from ``mt5.last_error()``.
  They describe what the *Python package* did locally and never reach the broker.

``1`` means success in one space and nothing at all in the other, so the two
tables must never be merged. Every entry carries a ``fix``: one sentence telling
the user what to actually do. That sentence is the product.

Own work. No third-party code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Mt5Error",
    "describe_retcode",
    "describe_last_error",
    "is_success_retcode",
    "RETCODES",
    "LAST_ERRORS",
    "SUCCESS_RETCODES",
    "retcode_table_markdown",
]


@dataclass(frozen=True)
class Mt5Error:
    """One decoded MT5 error, ready to hand to a non-technical user."""

    code: int
    space: Literal["retcode", "last_error"]
    name: str
    """The official MQL5 / Python constant."""
    meaning: str
    """What happened."""
    fix: str
    """What to do about it. One sentence."""
    retryable: bool = False
    """True when simply trying again has a real chance of succeeding."""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "space": self.space,
            "name": self.name,
            "meaning": self.meaning,
            "fix": self.fix,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:
        return f"[{self.code} {self.name}] {self.meaning} — {self.fix}"


def _r(code, name, meaning, fix, retryable=False):
    return Mt5Error(code, "retcode", name, meaning, fix, retryable)


def _e(code, name, meaning, fix, retryable=False):
    return Mt5Error(code, "last_error", name, meaning, fix, retryable)


# --------------------------------------------------------------------------
# Trade server return codes (MQL5: ENUM_TRADE_RETURN_CODES)
# --------------------------------------------------------------------------

RETCODES: dict[int, Mt5Error] = {e.code: e for e in [
    _r(10004, "REQUOTE",
       "Requote — the price moved while the order was in flight.",
       "Send it again. If this keeps happening, raise the allowed deviation to "
       "20-30 points.",
       retryable=True),
    _r(10006, "REJECT",
       "The broker rejected the request without giving a specific reason.",
       "Check that the symbol is currently tradable and that your account allows "
       "trading. On demo accounts over the weekend this is normal."),
    _r(10007, "CANCEL",
       "The request was cancelled by hand.",
       "Not an error — someone cancelled in the terminal. Send it again if you "
       "still want it."),
    _r(10008, "PLACED", "The order was accepted and is now working.",
       "Nothing to do, this is a success."),
    _r(10009, "DONE", "The request was carried out in full.",
       "Nothing to do, this is a success."),
    _r(10010, "DONE_PARTIAL",
       "Only part of the requested volume was filled.",
       "Check the open position: less is in the market than you asked for. The "
       "remainder was not filled because there was not enough liquidity."),
    _r(10011, "ERROR", "Internal error while processing the request.",
       "Try again. If it persists, restart the terminal.", retryable=True),
    _r(10012, "TIMEOUT", "The request expired before it could be processed.",
       "Send it again. Check your connection to the broker if it repeats.",
       retryable=True),
    _r(10013, "INVALID",
       "The request is invalid — some field does not fit the rest.",
       "Most common cause: a misspelled symbol name, or an order type that does "
       "not match the price. Resolve the symbol with symbol_resolve first."),
    _r(10014, "INVALID_VOLUME",
       "The volume is invalid.",
       "Check the symbol's volume_min, volume_max and volume_step. The lot size "
       "must be a multiple of volume_step — with a step of 0.01, a size of 0.015 "
       "is not allowed."),
    _r(10015, "INVALID_PRICE",
       "The price is invalid.",
       "The price must be rounded to the symbol's digits, and for pending orders "
       "it must sit on the correct side of the market."),
    _r(10016, "INVALID_STOPS",
       "Stop loss or take profit is too close to the current price — or on the "
       "wrong side of it.",
       "Keep at least trade_stops_level points away from the market price. For a "
       "buy, the stop goes below and the target above."),
    _r(10017, "TRADE_DISABLED", "Trading is disabled for this account.",
       "This is an account setting at the broker, not in the terminal. Ask your "
       "broker — for expired demo accounts this is normal."),
    _r(10018, "MARKET_CLOSED", "The market is closed.",
       "Wait for the symbol's trading hours. Forex rests at the weekend; indices "
       "and shares also have daily breaks."),
    _r(10019, "NO_MONEY", "Not enough free margin for this trade.",
       "Reduce the lot size or close other positions. risk_calc_margin tells you "
       "in advance what a position would cost."),
    _r(10020, "PRICE_CHANGED", "The price changed.",
       "Send it again — the second attempt usually goes through.", retryable=True),
    _r(10021, "PRICE_OFF", "There are no quotes to trade against.",
       "The symbol is not producing a price right now. Use quote_get to check "
       "whether ticks are arriving at all, and that the symbol is in Market Watch."),
    _r(10022, "INVALID_EXPIRATION",
       "The order's expiration date is invalid.",
       "The date must be in the future. Some brokers allow no expiry at all — "
       "use GTC in that case."),
    _r(10023, "ORDER_CHANGED", "The order's state changed in the meantime.",
       "Re-read the order and repeat the operation.", retryable=True),
    _r(10024, "TOO_MANY_REQUESTS", "Too many requests in too short a time.",
       "Wait a moment and slow down. The broker is throttling on purpose.",
       retryable=True),
    _r(10025, "NO_CHANGES", "The request changes nothing about the current state.",
       "The new values are identical to the old ones. Not an error, just a no-op."),
    _r(10026, "SERVER_DISABLES_AT",
       "Automated trading is switched off on the server side.",
       "That is the broker's decision, not yours. Ask them about it."),
    _r(10027, "CLIENT_DISABLES_AT",
       "Algorithmic trading is switched off in the terminal.",
       "Click the «Algo Trading» button in the terminal's toolbar until the icon "
       "turns green. Or use Tools -> Options -> Expert Advisors."),
    _r(10028, "LOCKED", "The request is locked for processing.",
       "Wait a moment and try again.", retryable=True),
    _r(10029, "FROZEN",
       "The order or position is frozen and cannot be modified right now.",
       "This happens close to the execution price. Check trade_freeze_level and "
       "wait until the price has moved away."),
    _r(10030, "INVALID_FILL",
       "The filling mode is not supported for this symbol.",
       "Do not guess it: read the symbol's filling_mode. Depending on the symbol "
       "the server expects FOK, IOC or RETURN — mt5-mcp picks this automatically."),
    _r(10031, "CONNECTION", "No connection to the trade server.",
       "Check the connection indicator at the bottom right of the terminal. Either "
       "the network or the broker's server is unreachable.", retryable=True),
    _r(10032, "ONLY_REAL", "The operation is allowed on live accounts only.",
       "Not possible on a demo account. Nothing is wrong with your request."),
    _r(10033, "LIMIT_ORDERS", "The maximum number of pending orders is reached.",
       "Cancel some existing pending orders first."),
    _r(10034, "LIMIT_VOLUME",
       "The maximum volume for this symbol is reached.",
       "Open positions plus orders exceed the broker's limit. Reduce the size or "
       "close something first."),
    _r(10035, "INVALID_ORDER", "Invalid or forbidden order type for this symbol.",
       "Some symbols do not allow stop-limit orders. Use a plain limit or stop "
       "order instead."),
    _r(10036, "POSITION_CLOSED",
       "The position you referenced is already closed.",
       "Reload the position list — that ticket no longer exists."),
    _r(10038, "INVALID_CLOSE_VOLUME",
       "The closing volume is larger than the open position.",
       "You cannot close more than is open. Check the volume."),
    _r(10039, "CLOSE_ORDER_EXIST",
       "A close order already exists for this position.",
       "Wait for the running close to finish instead of sending a second one."),
    _r(10040, "LIMIT_POSITIONS",
       "The maximum number of simultaneous positions is reached.",
       "On netting accounts one position per symbol counts; on hedging accounts "
       "each one does. Close something before opening more."),
    _r(10041, "REJECT_CANCEL",
       "Activating the pending order was rejected and the order was cancelled.",
       "The order is gone. Place it again if you still want it."),
    _r(10042, "LONG_ONLY",
       "Only long positions are allowed for this symbol.",
       "The broker forbids short selling here. Only «buy» is possible."),
    _r(10043, "SHORT_ONLY",
       "Only short positions are allowed for this symbol.",
       "Only «sell» is possible."),
    _r(10044, "CLOSE_ONLY",
       "Only closing is allowed for this symbol.",
       "New positions are blocked, usually near contract expiry. You can still "
       "close what you hold."),
    _r(10045, "FIFO_CLOSE",
       "The account enforces FIFO — the oldest position must be closed first.",
       "Close the oldest position in the same symbol first. This mostly affects "
       "US brokers."),
    _r(10046, "HEDGE_PROHIBITED",
       "Opposite positions in the same symbol are forbidden on this account.",
       "The account is set to netting. Close the opposing position instead of "
       "opening a second one in the other direction."),
]}

#: Retcodes that mean the request succeeded. Anything else is a failure.
SUCCESS_RETCODES = frozenset({10008, 10009, 10010, 10025})


# --------------------------------------------------------------------------
# Terminal / Python package error codes (mt5.last_error())
# --------------------------------------------------------------------------

LAST_ERRORS: dict[int, Mt5Error] = {e.code: e for e in [
    _e(1, "S_OK", "Success.", "Nothing to do."),
    _e(-1, "E_FAIL", "Generic failure in the call.",
       "Usually the terminal is busy, or the call asked for more data than exists. "
       "Request a smaller amount and try again.",
       retryable=True),
    _e(-2, "E_INVALID_PARAMS", "Invalid parameters.",
       "A value is out of range. A common cause is asking for more bars than the "
       "terminal allows (Tools -> Options -> Charts -> «Max bars in chart»)."),
    _e(-3, "E_NO_MEMORY", "Not enough memory for the result.",
       "Ask for less data at once — a shorter period or a larger timeframe."),
    _e(-4, "E_NOT_FOUND", "The requested thing does not exist.",
       "Usually an unknown symbol name. Translate it with symbol_resolve first."),
    _e(-5, "E_INVALID_VERSION", "Version mismatch.",
       "The terminal and the MetaTrader5 Python package are too far apart. Update "
       "both to current versions."),
    _e(-6, "E_AUTH_FAILED", "Authorisation failed.",
       "Account number, password or server name is wrong. Make sure the server "
       "name is spelled exactly as in the terminal, hyphen included."),
    _e(-7, "E_UNSUPPORTED", "This function is not supported.",
       "Your terminal build cannot do this. Update the terminal."),
    _e(-8, "E_AUTO_TRADING_DISABLED", "Automated trading is disabled.",
       "Click «Algo Trading» in the terminal's toolbar until the icon turns green."),
    _e(-9, "E_INTERNAL_FAIL", "Internal terminal error.",
       "Restart the terminal. If it persists, the installation is damaged."),
    _e(-10000, "E_INTERNAL_FAIL_SEND", "The request could not be sent.",
       "The connection to the terminal dropped. mt5-mcp reconnects automatically — "
       "is the terminal still running?", retryable=True),
    _e(-10001, "E_INTERNAL_FAIL_RECEIVE", "No answer received from the terminal.",
       "The terminal stopped responding. Is it still running and logged in?",
       retryable=True),
    _e(-10002, "E_INTERNAL_FAIL_INIT", "The terminal could not be started.",
       "Check the path to terminal64.exe in your .env. mt5_doctor will find it "
       "for you."),
    _e(-10003, "E_INTERNAL_FAIL_CONNECT", "Cannot connect to the terminal.",
       "Start MetaTrader 5 and log in. The Python interface only talks to a "
       "running, logged-in terminal."),
    _e(-10004, "E_INTERNAL_FAIL_TIMEOUT", "Timed out.",
       "The terminal took too long. Try again.", retryable=True),
]}


_UNKNOWN_RETCODE_FIX = (
    "This code is not in our table. Look it up in the MQL5 documentation under "
    "«Trade Server Return Codes» and report it to us so we can add it."
)
_UNKNOWN_ERROR_FIX = (
    "This code is not in our table. Restart the terminal and try again."
)


def describe_retcode(code: int) -> Mt5Error:
    """Translate an ``OrderSendResult.retcode``. Never raises."""
    known = RETCODES.get(code)
    if known is not None:
        return known
    return Mt5Error(code, "retcode", "UNKNOWN",
                    f"Unknown return code {code} from the trade server.",
                    _UNKNOWN_RETCODE_FIX)


def describe_last_error(err) -> Mt5Error:
    """Translate ``mt5.last_error()``.

    Accepts either the ``(code, text)`` tuple the package returns or a bare int,
    because callers reach for both and getting it wrong silently produces a
    useless message.
    """
    code, text = (err if isinstance(err, (tuple, list)) and len(err) >= 2
                  else (err, ""))
    try:
        code = int(code)
    except (TypeError, ValueError):
        return Mt5Error(0, "last_error", "UNKNOWN",
                        f"Unreadable error value from the terminal: {err!r}",
                        _UNKNOWN_ERROR_FIX)
    known = LAST_ERRORS.get(code)
    if known is not None:
        return known
    detail = f" ({text})" if text else ""
    return Mt5Error(code, "last_error", "UNKNOWN",
                    f"Unknown terminal error {code}{detail}.",
                    _UNKNOWN_ERROR_FIX)


def is_success_retcode(code: int | None) -> bool:
    return code in SUCCESS_RETCODES


def retcode_table_markdown() -> str:
    """Render both tables as Markdown for the docs."""
    out = ["# MT5 error codes", "",
           "There are **two separate number spaces**. Do not mix them up: `1` "
           "means success in one table and does not appear at all in the other.",
           "", "## Trade server return codes", "",
           "These arrive in the `retcode` of a trade response and describe what "
           "the **broker** did with the order.", "",
           "| Code | Name | Meaning | Fix |", "|---|---|---|---|"]
    for code in sorted(RETCODES):
        e = RETCODES[code]
        out.append(f"| {e.code} | `{e.name}` | {e.meaning} | {e.fix} |")
    out += ["", "## Terminal error codes", "",
            "These come from `last_error()` and describe what went wrong "
            "**locally**. They never reach the broker.", "",
            "| Code | Name | Meaning | Fix |", "|---|---|---|---|"]
    for code in sorted(LAST_ERRORS, reverse=True):
        e = LAST_ERRORS[code]
        out.append(f"| {e.code} | `{e.name}` | {e.meaning} | {e.fix} |")
    return "\n".join(out) + "\n"
