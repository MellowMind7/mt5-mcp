"""Broker time, UTC and Berlin time — measured, never assumed.

MT5 hands out every timestamp in **broker server time** as a naive Unix-looking
integer. It is not UTC. On the machine this was developed against the broker ran
at UTC+3 while the host clock sat at UTC-7, so trusting either the raw value or
the host's local time would have been wrong by three and ten hours respectively.

The offset is therefore *measured* against a live tick, and re-measured on a
timer, because it moves twice a year and a session can straddle the switch.

Own work. No third-party code.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = [
    "BrokerClock",
    "broker_naive_to_utc",
    "utc_to_broker_naive",
    "format_both",
    "BERLIN",
]

BERLIN = ZoneInfo("Europe/Berlin")

# How long a measured offset is trusted. Short enough that a DST switch is
# picked up within the hour, long enough that we are not measuring constantly.
_TTL = timedelta(minutes=30)

# Broker offsets are whole or half hours in practice; rounding to 15 minutes
# removes tick latency and clock drift without hiding a real offset.
_QUANTUM_SECONDS = 900

# Real time zones run from UTC-12 to UTC+14. Anything outside that is not an
# offset, it is a stale quote being mistaken for one: over a closed weekend the
# newest tick is a day or two old, and measuring against it yields an
# "offset" of minus thirty-odd hours. Believing that would shift every
# timestamp the server returns by the same amount, silently.
MAX_PLAUSIBLE_OFFSET = timedelta(hours=14)


@dataclass(frozen=True)
class ClockReading:
    """One measurement of the broker's clock against real UTC."""

    offset: timedelta
    """broker server time minus UTC."""
    measured_at: datetime
    """UTC instant of the measurement."""
    source: str
    """Which symbol's tick produced it, for diagnostics."""

    @property
    def offset_hours(self) -> float:
        return self.offset.total_seconds() / 3600.0


class BrokerClock:
    """Measures and caches the broker's UTC offset.

    Thread-safe: the connection layer funnels every SDK call through one worker
    thread, but tool handlers read the cached offset from arbitrary threads.
    """

    def __init__(self, ttl: timedelta = _TTL) -> None:
        self._ttl = ttl
        self._reading: ClockReading | None = None
        self._lock = threading.Lock()
        self._rejected_gap: timedelta | None = None
        """Raw gap from the last implausible measurement, kept to explain why."""

    # -- measurement ------------------------------------------------------

    def measure(self, tick_time: int | float,
                symbol: str = "?") -> ClockReading | None:
        """Record an offset from one tick timestamp.

        ``tick_time`` is ``symbol_info_tick().time`` — seconds since epoch, but
        expressed in broker wall-clock time rather than UTC.

        Returns ``None`` and keeps the previous reading when the result is not a
        possible time zone. That happens whenever the newest quote is stale,
        which over a closed weekend it always is: the measurement cannot tell a
        broker three hours ahead from a quote thirty-eight hours old, so an
        implausible answer is refused rather than believed.
        """
        now_utc = datetime.now(timezone.utc)
        # Read the broker stamp *as if* it were UTC to recover its wall clock.
        broker_wall = datetime.fromtimestamp(float(tick_time), tz=timezone.utc)
        raw = (broker_wall - now_utc).total_seconds()
        if abs(raw) > MAX_PLAUSIBLE_OFFSET.total_seconds():
            with self._lock:
                self._rejected_gap = timedelta(seconds=raw)
            return None
        rounded = round(raw / _QUANTUM_SECONDS) * _QUANTUM_SECONDS
        reading = ClockReading(timedelta(seconds=rounded), now_utc, symbol)
        with self._lock:
            self._reading = reading
            self._rejected_gap = None
        return reading

    def invalidate(self) -> None:
        """Forget the cached offset — called on every (re)connect."""
        with self._lock:
            self._reading = None
            self._rejected_gap = None

    @property
    def reading(self) -> ClockReading | None:
        with self._lock:
            return self._reading

    def is_stale(self) -> bool:
        with self._lock:
            if self._reading is None:
                return True
            return datetime.now(timezone.utc) - self._reading.measured_at > self._ttl

    @property
    def offset(self) -> timedelta:
        """Cached offset, or zero when nothing has been measured yet.

        Callers that must not guess should check ``is_stale()`` first. Falling
        back to zero is deliberate: a wrong-by-offset timestamp is obvious to a
        trader, whereas raising here would break every read tool over a
        cosmetic detail.
        """
        with self._lock:
            return self._reading.offset if self._reading else timedelta(0)

    # -- conversion -------------------------------------------------------

    def to_utc(self, broker_naive: datetime | int | float) -> datetime:
        return broker_naive_to_utc(broker_naive, self.offset)

    def to_berlin(self, broker_naive: datetime | int | float) -> datetime:
        return self.to_utc(broker_naive).astimezone(BERLIN)

    def from_utc(self, moment: datetime) -> datetime:
        return utc_to_broker_naive(moment, self.offset)

    def describe(self) -> dict:
        """Diagnostic block for ``mt5_status`` and ``mt5_doctor``."""
        r = self.reading
        if r is None:
            with self._lock:
                rejected = self._rejected_gap
            if rejected is not None:
                hours = abs(rejected.total_seconds()) / 3600.0
                return {
                    "measured": False,
                    "note": (f"Broker offset cannot be measured: the newest "
                             f"quote is roughly {hours:.0f} hours old, so the "
                             f"market is closed. Times are shown as the broker "
                             f"sent them, without conversion."),
                }
            return {
                "measured": False,
                "note": "Broker offset not measured yet — no tick has arrived.",
            }
        berlin_offset = (
            datetime.now(BERLIN).utcoffset() or timedelta(0)
        ).total_seconds() / 3600.0
        return {
            "measured": True,
            "broker_minus_utc_hours": round(r.offset_hours, 2),
            "broker_minus_display_hours": round(r.offset_hours - berlin_offset, 2),
            "measured_at_utc": r.measured_at.replace(microsecond=0).isoformat(),
            "source": r.source,
            "stale": self.is_stale(),
        }


def broker_naive_to_utc(value: datetime | int | float,
                        offset: timedelta) -> datetime:
    """Turn a broker timestamp into a real, aware UTC datetime."""
    if isinstance(value, (int, float)):
        wall = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif value.tzinfo is None:
        wall = value.replace(tzinfo=timezone.utc)
    else:
        wall = value.astimezone(timezone.utc)
    return wall - offset


def utc_to_broker_naive(moment: datetime, offset: timedelta) -> datetime:
    """Inverse of :func:`broker_naive_to_utc`, for building request ranges.

    The result is *naive* on purpose — that is what the MT5 SDK expects, and
    attaching a tzinfo here would make it silently convert again.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) + offset).replace(tzinfo=None)


def format_both(value: datetime | int | float, offset: timedelta) -> dict:
    """Render one timestamp the three ways a user might need it.

    Every timestamp the server returns goes through here, so a trader never has
    to wonder which clock a number is in.
    """
    utc = broker_naive_to_utc(value, offset)
    broker = utc + offset
    return {
        "broker": broker.replace(tzinfo=None).isoformat(sep=" "),
        "berlin": utc.astimezone(BERLIN).isoformat(sep=" "),
        "utc": utc.replace(tzinfo=None).isoformat(sep=" "),
    }
