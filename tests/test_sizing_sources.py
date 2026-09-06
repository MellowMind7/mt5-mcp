"""Where the money-per-point figure comes from, and when it refuses.

Three sources, in order of authority:

1. ``order_calc_profit`` — the terminal's own pricing engine, and what real
   profit and loss are settled on.
2. ``trade_tick_value_loss`` scaled from ticks to points — the documented
   arithmetic, and wrong by a factor of ten for the metals on one measured feed.
3. ``contract_size * point`` — static, needs no live price, but only comparable
   when the profit currency is the account currency.

The interesting case is the weekend: the engine answers nothing while a market
is closed, which is exactly when people plan trades. Then 2 and 3 are checked
against each other, and a disagreement must refuse rather than guess.
"""

from __future__ import annotations

import pytest

from mt5_mcp.tools._common import ToolFailure
from mt5_mcp.tools.risk_tools import _contract_value_per_point, value_per_point

from conftest import make_symbol


@pytest.fixture
def no_engine(monkeypatch):
    """Simulate a closed market: order_calc_profit answers nothing."""
    monkeypatch.setattr("mt5_mcp.tools.risk_tools._measured_value_per_point",
                        lambda info, side, entry: 0.0)


@pytest.fixture
def engine(monkeypatch):
    """Simulate a live market with a pricing engine that returns `value`."""
    def install(value: float) -> None:
        monkeypatch.setattr("mt5_mcp.tools.risk_tools._measured_value_per_point",
                            lambda info, side, entry: value)
    return install


class TestContractSource:
    def test_contract_times_point(self):
        gold = make_symbol(name="GOLD", point=0.01, trade_contract_size=100.0,
                           currency_profit="USD")
        assert _contract_value_per_point(gold, "USD") == pytest.approx(1.0)

    def test_forex_pair(self):
        assert _contract_value_per_point(make_symbol(), "USD") == pytest.approx(1.0)

    def test_none_when_the_profit_currency_differs(self):
        """A figure in EUR cannot be compared against one in USD.

        Returning it anyway would flag every cross-currency instrument as
        broken, so the cross-check simply does not apply there.
        """
        dax = make_symbol(name="DE40", point=0.01, trade_contract_size=1.0,
                          currency_profit="EUR")
        assert _contract_value_per_point(dax, "USD") is None

    def test_none_without_an_account_currency(self):
        assert _contract_value_per_point(make_symbol(), None) is None

    def test_none_when_the_contract_size_is_missing(self):
        broken = make_symbol(trade_contract_size=0.0, currency_profit="USD")
        assert _contract_value_per_point(broken, "USD") is None


class TestEngineWins:
    def test_engine_result_is_used(self, engine):
        engine(1.0)
        value, source, warning = value_per_point(
            make_symbol(), entry=1.16, account_currency="USD")
        assert value == pytest.approx(1.0)
        assert source == "pricing_engine"
        assert warning is None

    def test_engine_overrides_a_wrong_tick_value_and_says_so(self, engine):
        """The measured XAUUSD case: field says 0.1, reality is 1.0."""
        engine(1.0)
        gold = make_symbol(
            name="XAUUSD", point=0.01, trade_tick_size=0.01,
            trade_tick_value_loss=0.1, trade_contract_size=100.0,
            currency_profit="USD")
        value, source, warning = value_per_point(
            gold, entry=3000.0, account_currency="USD")
        assert value == pytest.approx(1.0)
        assert source == "pricing_engine"
        assert warning and "disagrees" in warning
        assert "0.100000" in warning and "1.000000" in warning


class TestClosedMarket:
    """No live price. This is the weekend, when trades get planned."""

    def test_agreeing_static_sources_are_trusted(self, no_engine):
        value, source, warning = value_per_point(
            make_symbol(), entry=1.16, account_currency="USD")
        assert value == pytest.approx(1.0)
        assert source == "tick_value_confirmed"
        assert warning is None

    def test_disagreeing_static_sources_refuse(self, no_engine):
        """XAUUSD on the MetaQuotes demo feed, sized on a Sunday.

        The tick value says 0.1, the contract says 1.0. Handing back a lot size
        built on the wrong one would be ten times too large.
        """
        gold = make_symbol(
            name="XAUUSD", point=0.01, trade_tick_size=0.01,
            trade_tick_value_loss=0.1, trade_contract_size=100.0,
            currency_profit="USD")
        with pytest.raises(ToolFailure) as caught:
            value_per_point(gold, entry=3000.0, account_currency="USD")
        assert "XAUUSD" in str(caught.value)
        assert "10 times wrong" in caught.value.fix
        assert "market is closed" in caught.value.fix

    def test_a_correct_feed_is_not_refused(self, no_engine):
        """The same gold contract at a broker that reports it correctly."""
        gold = make_symbol(
            name="GOLD", point=0.01, trade_tick_size=0.01,
            trade_tick_value_loss=1.0, trade_contract_size=100.0,
            currency_profit="USD")
        value, source, warning = value_per_point(
            gold, entry=3000.0, account_currency="USD")
        assert value == pytest.approx(1.0)
        assert source == "tick_value_confirmed"
        assert warning is None

    def test_cross_currency_cannot_be_checked_but_still_answers(self, no_engine):
        """DE40 pays in EUR on a USD account, so the static check does not apply.

        Refusing here would block a correct instrument for a reason that is not
        about it, so the tick value is used with the caveat attached.
        """
        dax = make_symbol(
            name="DE40", point=0.01, trade_tick_size=0.01,
            trade_tick_value_loss=0.011614, trade_contract_size=1.0,
            currency_profit="EUR")
        value, source, warning = value_per_point(
            dax, entry=24000.0, account_currency="USD")
        assert value == pytest.approx(0.011614)
        assert source == "tick_value"
        assert warning and "not the account currency" in warning

    def test_tolerance_allows_rounding_noise(self, no_engine):
        """A one percent gap is conversion noise, not a factor-of-ten error."""
        near = make_symbol(
            name="NEAR", point=0.01, trade_tick_size=0.01,
            trade_tick_value_loss=1.01, trade_contract_size=100.0,
            currency_profit="USD")
        _value, source, _warning = value_per_point(
            near, entry=100.0, account_currency="USD")
        assert source == "tick_value_confirmed"

    def test_contract_only_when_no_tick_value_is_reported(self, no_engine):
        bare = make_symbol(
            name="BARE", point=0.01, trade_tick_size=0.01,
            trade_tick_value=0.0, trade_tick_value_loss=0.0,
            trade_tick_value_profit=0.0, trade_contract_size=100.0,
            currency_profit="USD")
        value, source, warning = value_per_point(
            bare, entry=100.0, account_currency="USD")
        assert value == pytest.approx(1.0)
        assert source == "contract_size"
        assert warning is not None

    def test_nothing_usable_refuses(self, no_engine):
        useless = make_symbol(
            name="VOID", trade_tick_value=0.0, trade_tick_value_loss=0.0,
            trade_tick_value_profit=0.0, trade_contract_size=0.0,
            currency_profit="USD")
        with pytest.raises(ToolFailure) as caught:
            value_per_point(useless, entry=1.0, account_currency="USD")
        assert "one point" in str(caught.value)
