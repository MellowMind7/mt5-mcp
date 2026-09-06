"""Arithmetic and decisions that must be right without a terminal."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mt5_mcp.core.errors import (LAST_ERRORS, RETCODES, describe_last_error,
                                 describe_retcode, is_success_retcode)
from mt5_mcp.core.timeutil import (BERLIN, BrokerClock, broker_naive_to_utc,
                                   format_both, utc_to_broker_naive)


class TestRetcodes:
    def test_every_documented_retcode_has_a_fix(self):
        for code, error in RETCODES.items():
            assert error.fix.strip(), f"{code} has no fix sentence"
            assert error.meaning.strip(), f"{code} has no meaning"

    def test_every_last_error_has_a_fix(self):
        for code, error in LAST_ERRORS.items():
            assert error.fix.strip(), f"{code} has no fix sentence"

    @pytest.mark.parametrize("code,name", [
        (10004, "REQUOTE"), (10006, "REJECT"), (10013, "INVALID"),
        (10014, "INVALID_VOLUME"), (10015, "INVALID_PRICE"),
        (10016, "INVALID_STOPS"), (10018, "MARKET_CLOSED"),
        (10019, "NO_MONEY"), (10027, "CLIENT_DISABLES_AT"),
        (10030, "INVALID_FILL"),
    ])
    def test_the_codes_that_were_required(self, code, name):
        assert describe_retcode(code).name == name

    def test_unknown_retcode_is_handled_not_crashed(self):
        error = describe_retcode(99999)
        assert error.name == "UNKNOWN"
        assert "99999" in error.meaning

    def test_success_codes(self):
        assert is_success_retcode(10009)
        assert is_success_retcode(10008)
        assert not is_success_retcode(10019)
        assert not is_success_retcode(None)

    def test_the_two_number_spaces_do_not_collide(self):
        """`1` is success for last_error and absent from the retcode table."""
        assert 1 in LAST_ERRORS
        assert 1 not in RETCODES
        assert describe_last_error((1, "Success")).name == "S_OK"

    def test_last_error_accepts_tuple_or_int(self):
        assert describe_last_error((-6, "auth")).name == "E_AUTH_FAILED"
        assert describe_last_error(-6).name == "E_AUTH_FAILED"

    def test_last_error_survives_nonsense(self):
        assert describe_last_error("boom").name == "UNKNOWN"


class TestBrokerClock:
    def test_offset_is_measured_and_rounded_to_quarter_hours(self):
        clock = BrokerClock()
        now = datetime.now(timezone.utc)
        # A broker three hours ahead of UTC, plus a little tick latency.
        stamp = (now + timedelta(hours=3, seconds=2)).timestamp()
        reading = clock.measure(stamp, "EURUSD")
        assert reading.offset == timedelta(hours=3)

    def test_reading_goes_stale(self):
        clock = BrokerClock(ttl=timedelta(minutes=30))
        clock.measure(datetime.now(timezone.utc).timestamp())
        assert not clock.is_stale()
        # Age the reading rather than sleeping: Windows' wall clock has ~16 ms
        # granularity, so a zero TTL is not reliably exceeded and the test
        # would flake rather than fail.
        aged = replace(clock.reading,
                       measured_at=clock.reading.measured_at - timedelta(hours=1))
        clock._reading = aged
        assert clock.is_stale()

    def test_a_stale_weekend_quote_is_refused(self):
        """Regression: a closed market produced "broker time is UTC-37.75 h".

        Measured against a real broker on a Sunday. The newest quote was from
        Friday's close, and the offset arithmetic cannot tell that apart from a
        time zone — so it reported one, and every timestamp the server returned
        would have been shifted by thirty-eight hours.
        """
        clock = BrokerClock()
        friday_close = datetime.now(timezone.utc) - timedelta(hours=38)
        assert clock.measure(friday_close.timestamp(), "EURUSD") is None
        assert clock.reading is None

    def test_a_refused_measurement_keeps_the_previous_one(self):
        clock = BrokerClock()
        good = datetime.now(timezone.utc) + timedelta(hours=3)
        clock.measure(good.timestamp(), "EURUSD")
        stale = datetime.now(timezone.utc) - timedelta(hours=50)
        assert clock.measure(stale.timestamp(), "XAUUSD") is None
        assert clock.offset == timedelta(hours=3)

    @pytest.mark.parametrize("hours", [-12, -8, 0, 2, 3, 10, 14])
    def test_real_time_zones_are_accepted(self, hours):
        clock = BrokerClock()
        stamp = datetime.now(timezone.utc) + timedelta(hours=hours)
        reading = clock.measure(stamp.timestamp(), "EURUSD")
        assert reading is not None
        assert reading.offset == timedelta(hours=hours)

    @pytest.mark.parametrize("hours", [-38, -25, 15, 48])
    def test_impossible_offsets_are_refused(self, hours):
        clock = BrokerClock()
        stamp = datetime.now(timezone.utc) + timedelta(hours=hours)
        assert clock.measure(stamp.timestamp(), "EURUSD") is None

    def test_refusal_is_explained_as_a_closed_market(self):
        clock = BrokerClock()
        stale = datetime.now(timezone.utc) - timedelta(hours=38)
        clock.measure(stale.timestamp(), "EURUSD")
        described = clock.describe()
        assert described["measured"] is False
        assert "market is closed" in described["note"]
        assert "38 hours" in described["note"]

    def test_unmeasured_clock_is_stale_and_zero(self):
        clock = BrokerClock()
        assert clock.is_stale()
        assert clock.offset == timedelta(0)

    def test_invalidate_forgets(self):
        clock = BrokerClock()
        clock.measure(datetime.now(timezone.utc).timestamp())
        clock.invalidate()
        assert clock.reading is None

    def test_round_trip_broker_utc_broker(self):
        offset = timedelta(hours=3)
        broker = datetime(2026, 9, 4, 16, 0, 0)
        utc = broker_naive_to_utc(broker, offset)
        assert utc.replace(tzinfo=None) == datetime(2026, 9, 4, 13, 0, 0)
        assert utc_to_broker_naive(utc, offset) == broker

    def test_format_both_gives_three_clocks(self):
        offset = timedelta(hours=3)
        out = format_both(datetime(2026, 9, 4, 16, 0, 0), offset)
        assert out["broker"].startswith("2026-09-04 16:00")
        assert out["utc"].startswith("2026-09-04 13:00")
        # Berlin is UTC+2 in September.
        assert out["berlin"].startswith("2026-09-04 15:00")

    def test_winter_offset_differs_from_summer(self):
        """The display conversion must follow DST, not a stored constant."""
        offset = timedelta(hours=2)
        summer = format_both(datetime(2026, 7, 1, 12, 0, 0), offset)
        winter = format_both(datetime(2026, 1, 1, 12, 0, 0), offset)
        assert summer["utc"].startswith("2026-07-01 10:00")
        assert winter["utc"].startswith("2026-01-01 10:00")
        assert summer["berlin"].startswith("2026-07-01 12:00")
        assert winter["berlin"].startswith("2026-01-01 11:00")


class TestVolumeRounding:
    def test_rounds_down_never_up(self):
        from mt5_mcp.tools._common import round_to_step
        # Rounding up would risk more money than the caller asked for.
        assert round_to_step(0.579, 0.01) == 0.57
        assert round_to_step(0.999, 0.1) == 0.9
        assert round_to_step(57.36, 0.1) == 57.3

    def test_no_floating_point_dust(self):
        from mt5_mcp.tools._common import round_to_step
        assert round_to_step(0.3, 0.1) == 0.3
        assert round_to_step(1.2, 0.1) == 1.2

    def test_exact_multiples_are_kept(self):
        from mt5_mcp.tools._common import round_to_step
        assert round_to_step(0.5, 0.01) == 0.5
        assert round_to_step(3.0, 1.0) == 3.0

    def test_zero_step_is_survivable(self):
        from mt5_mcp.tools._common import round_to_step
        assert round_to_step(1.23, 0) == 1.23

    def test_normalise_clamps_to_symbol_limits(self, eurusd):
        from mt5_mcp.tools._common import normalise_volume
        low, notes = normalise_volume(eurusd, 0.001)
        assert low == 0.01 and notes
        high, notes = normalise_volume(eurusd, 9999)
        assert high == 500.0 and notes


class TestFillingMode:
    """The bitmask is not the order constant, and 0 does not mean "none"."""

    def _pick(self, symbol, pending=False):
        from mt5_mcp.tools._common import pick_filling_mode
        return pick_filling_mode(symbol, pending=pending)

    def test_market_execution_uses_a_bit_from_the_mask(self, eurusd):
        eurusd.trade_exemode = 2  # SYMBOL_TRADE_EXECUTION_MARKET
        eurusd.filling_mode = 1   # SYMBOL_FILLING_FOK
        value, label = self._pick(eurusd)
        assert label == "FOK" and value == 0

    def test_ioc_bit_maps_to_the_ioc_order_constant(self, eurusd):
        eurusd.trade_exemode = 2
        eurusd.filling_mode = 2   # SYMBOL_FILLING_IOC
        value, label = self._pick(eurusd)
        assert label == "IOC" and value == 1

    def test_empty_mask_outside_market_execution_falls_back_to_return(self, eurusd):
        """filling_mode == 0 is not a broken symbol.

        RETURN never appears in the mask because it is always allowed except
        under Market execution, so the execution mode has to be consulted.
        """
        eurusd.trade_exemode = 1  # INSTANT
        eurusd.filling_mode = 0
        value, label = self._pick(eurusd)
        assert label == "RETURN" and value == 2

    def test_boc_is_never_chosen_for_a_market_order(self, eurusd):
        eurusd.trade_exemode = 2
        eurusd.filling_mode = 4  # only BOC advertised
        _value, label = self._pick(eurusd, pending=False)
        assert label != "BOC"

    def test_boc_is_allowed_for_a_pending_order(self, eurusd):
        eurusd.trade_exemode = 2
        eurusd.filling_mode = 4
        value, label = self._pick(eurusd, pending=True)
        assert label == "BOC" and value == 3

    def test_supported_list_includes_return_when_applicable(self, eurusd):
        from mt5_mcp.tools._common import supported_fillings
        eurusd.trade_exemode = 1
        eurusd.filling_mode = 3
        assert supported_fillings(eurusd) == ["FOK", "IOC", "RETURN"]

    def test_supported_list_excludes_return_under_market_execution(self, eurusd):
        from mt5_mcp.tools._common import supported_fillings
        eurusd.trade_exemode = 2
        eurusd.filling_mode = 3
        assert supported_fillings(eurusd) == ["FOK", "IOC"]


class TestTimeframes:
    def test_known_names_and_aliases(self):
        from mt5_mcp.tools._common import timeframe_or_fail
        assert timeframe_or_fail("H1")[0] == "H1"
        assert timeframe_or_fail("h1")[0] == "H1"
        assert timeframe_or_fail("1h")[0] == "H1"
        assert timeframe_or_fail("daily")[0] == "D1"

    def test_unknown_name_is_rejected_with_a_list(self):
        from mt5_mcp.tools._common import ToolFailure, timeframe_or_fail
        with pytest.raises(ToolFailure) as caught:
            timeframe_or_fail("H7")
        assert "H1" in caught.value.fix
