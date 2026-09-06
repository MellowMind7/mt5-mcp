"""Fakes that let the calculating parts be tested without a terminal.

Everything here is deliberately dumb: the point is to pin down our arithmetic
and our decisions, not to re-simulate MetaTrader.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest


@dataclass
class FakeSymbol:
    """Enough of ``SymbolInfo`` for the code under test."""

    name: str = "EURUSD"
    description: str = "Euro vs US Dollar"
    path: str = "Forex\\EURUSD"
    digits: int = 5
    point: float = 1e-05
    trade_contract_size: float = 100_000.0
    volume_min: float = 0.01
    volume_max: float = 500.0
    volume_step: float = 0.01
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    trade_tick_size: float = 1e-05
    trade_tick_value: float = 1.0
    trade_tick_value_profit: float = 1.0
    trade_tick_value_loss: float = 1.0
    currency_base: str = "EUR"
    currency_profit: str = "USD"
    currency_margin: str = "EUR"
    filling_mode: int = 1
    trade_exemode: int = 2
    trade_mode: int = 4
    trade_calc_mode: int = 0
    visible: bool = True
    spread: int = 1
    expiration_time: int = 0
    """Non-zero marks a dated contract. Futures set it; cash instruments do not."""


@dataclass
class FakeConnection:
    """Stands in for Mt5Connection where only ``call`` and ``clock`` matter."""

    symbols: list = field(default_factory=list)
    responses: dict = field(default_factory=dict)

    def call(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", str(fn))
        if name in self.responses:
            value = self.responses[name]
            return value(*args, **kwargs) if callable(value) else value
        if name == "symbols_get":
            return self.symbols
        return None

    def ensure_connected(self):
        return None


def make_symbol(**overrides) -> FakeSymbol:
    return FakeSymbol(**overrides)


@pytest.fixture
def eurusd() -> FakeSymbol:
    return make_symbol()


@pytest.fixture
def xauusd() -> FakeSymbol:
    """Gold as this broker really reports it — with the wrong tick value."""
    return make_symbol(
        name="XAUUSD", description="Gold vs US Dollar", path="Metals\\XAUUSD",
        digits=2, point=0.01, trade_contract_size=100.0, volume_max=100.0,
        trade_tick_size=0.01, trade_tick_value=0.1,
        trade_tick_value_profit=0.1, trade_tick_value_loss=0.1,
        currency_base="XAU", currency_profit="USD", currency_margin="USD",
        filling_mode=3, trade_calc_mode=4)


@pytest.fixture
def de40() -> FakeSymbol:
    return make_symbol(
        name="DE40", description="Germany 40 Index", path="Indexes\\DE40",
        digits=2, point=0.01, trade_contract_size=1.0, volume_min=0.1,
        volume_step=0.1, volume_max=250.0, trade_tick_size=0.01,
        trade_tick_value=0.0116209, trade_tick_value_profit=0.0116209,
        trade_tick_value_loss=0.011621, currency_base="USD",
        currency_profit="EUR", currency_margin="EUR", filling_mode=2,
        trade_calc_mode=2)


@pytest.fixture
def broker_universe() -> list[FakeSymbol]:
    """A miniature broker that contains the traps we care about."""
    return [
        make_symbol(),
        make_symbol(name="GBPUSD", description="Great Britain Pound vs US Dollar",
                    path="Forex\\GBPUSD", currency_base="GBP"),
        make_symbol(name="USDJPY", description="US Dollar vs Yen",
                    path="Forex\\USDJPY", digits=3, point=0.001,
                    currency_base="USD", currency_profit="JPY"),
        make_symbol(name="XAUUSD", description="Gold vs US Dollar",
                    path="Metals\\XAUUSD", currency_base="XAU",
                    currency_profit="USD", trade_calc_mode=4),
        make_symbol(name="XAGUSD", description="Silver vs US Dollar",
                    path="Metals\\XAGUSD", currency_base="XAG",
                    currency_profit="USD", trade_calc_mode=4),
        make_symbol(name="DE40", description="Germany 40 Index",
                    path="Indexes\\DE40", currency_base="USD",
                    currency_profit="EUR", trade_calc_mode=2),
        make_symbol(name="US30", description="US Wall Street 30 Index",
                    path="Indexes\\US30", currency_base="USD",
                    currency_profit="USD", trade_calc_mode=2),
        make_symbol(name="US30M", description="US30m", path="Indexes\\US30M",
                    currency_base="USD", currency_profit="USD",
                    trade_calc_mode=2),
        make_symbol(name="USTEC", description="US Tech 100 Index",
                    path="Indexes\\USTEC", currency_base="USD",
                    currency_profit="USD", trade_calc_mode=2),
        make_symbol(name="US500", description="US SPX 500 Index",
                    path="Indexes\\US500", currency_base="USD",
                    currency_profit="USD", trade_calc_mode=2),
        # The traps: shares whose ticker is what a trader calls something else.
        make_symbol(name="GOLD", description="Barrick Gold Corporation (BC)",
                    path="Nasdaq\\Stock\\GOLD", currency_base="USD",
                    currency_profit="USD", trade_calc_mode=32),
        make_symbol(name="DAX", description="Global X DAX Germany ETF",
                    path="Nasdaq\\ETF\\DAX", currency_base="USD",
                    currency_profit="USD", trade_calc_mode=32),
        make_symbol(name="DOW", description="Dow Inc", path="Nasdaq\\Stock\\DOW",
                    currency_base="USD", currency_profit="USD",
                    trade_calc_mode=32),
        make_symbol(name="AAPL", description="Apple Inc",
                    path="Nasdaq\\Stock\\AAPL", currency_base="USD",
                    currency_profit="USD", trade_calc_mode=32),
    ]


@pytest.fixture
def admirals_universe() -> list[FakeSymbol]:
    """A second, structurally different broker.

    Modelled on AdmiralsGroup-Demo as measured on 2026-09-06. What makes it
    worth testing is the three-tier index layout the first broker does not
    have at all: a cash CFD, an expiring futures CFD and a delayed ETF CFD, all
    describing the same underlying index. MetaQuotes-Demo has no futures tier,
    so a regression here would be invisible there.
    """
    cash = "Cash Indices CFDs"
    fut = "Index Futures CFDs"
    etf = "ETF CFDs"
    return [
        # --- cash indices, the ones a trader means -----------------------
        make_symbol(name="[DJI30]", description="Dow Jones Index CFD, cash (USD)",
                    path=rf"{cash}\[DJI30]", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="[SP500]",
                    description="Standard and Poor's Index CFD, cash (USD)",
                    path=rf"{cash}\[SP500]", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="US100", description="US100 Index CFD, cash (USD)",
                    path=rf"{cash}\US100", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="[CAC40]", description="CAC40 Index CFD, cash (EUR)",
                    path=rf"{cash}\[CAC40]", trade_calc_mode=4,
                    currency_base="EUR", currency_profit="EUR"),
        make_symbol(name="GERMANY40",
                    description="Germany 40 Index CFD, cash (EUR)",
                    path=rf"{cash}\GERMANY40", trade_calc_mode=4,
                    currency_base="EUR", currency_profit="EUR"),
        make_symbol(name="[FTSE100]", description="FTSE100 Index CFD, cash (GBP)",
                    path=rf"{cash}\[FTSE100]", trade_calc_mode=4,
                    currency_base="GBP", currency_profit="GBP"),
        make_symbol(name="[JP225]", description="Nikkei 225 Index CFD, cash (JPY)",
                    path=rf"{cash}\[JP225]", trade_calc_mode=4,
                    currency_base="JPY", currency_profit="JPY"),
        # --- the same indices as dated futures ---------------------------
        make_symbol(name="_YM_U6", expiration_time=1789000000,
                    description="Dow Jones Index Futures CFD, USD [Exp: Sep 2026]",
                    path=rf"{fut}\_YM_U6", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="_ES_U6", expiration_time=1789000000,
                    description="Standard and Poor's Index Futures CFD, USD "
                                "[Exp: Sep 2026]",
                    path=rf"{fut}\_ES_U6", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="_US100_U6", expiration_time=1789000000,
                    description="US Tech 100 Index Futures CFD, USD [Exp: Sep 2026]",
                    path=rf"{fut}\_US100_U6", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="_GER40_U6", expiration_time=1789000000,
                    description="DAX40 Index Futures CFD, EUR [Exp: Sep 2026]",
                    path=rf"{fut}\_GER40_U6", trade_calc_mode=4,
                    currency_base="EUR", currency_profit="EUR"),
        make_symbol(name="_FRANCE40_U6", expiration_time=1789000000,
                    description="France 40 Index Futures CFD, EUR [Exp: Sep 2026]",
                    path=rf"{fut}\_FRANCE40_U6", trade_calc_mode=4,
                    currency_base="EUR", currency_profit="EUR"),
        # --- and as delayed ETFs -----------------------------------------
        make_symbol(name="#VUSA",
                    description="Vanguard S&P 500 UCITS ETF CFD(15min delay)",
                    path=rf"{etf}\#VUSA", trade_calc_mode=2,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="#SPY",
                    description="State Street SPDR S&P 500 ETF Trust CFD(15min delay)",
                    path=rf"{etf}\#SPY", trade_calc_mode=2,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="#CAC",
                    description="Amundi CAC 40 UCITS ETF - Dist CFD(15min delay)",
                    path=rf"{etf}\#CAC", trade_calc_mode=2,
                    currency_base="EUR", currency_profit="EUR"),
        make_symbol(name="#DOW", description="Dow Inc. CFD(15min delay)",
                    path=r"Stock CFDs\#DOW", trade_calc_mode=2,
                    currency_base="USD", currency_profit="USD"),
        # --- the rest, which already worked ------------------------------
        make_symbol(name="GOLD", description="Gold (100 oz) vs US Dollar CFD",
                    path=r"Metals\GOLD", trade_calc_mode=4,
                    currency_base="XAU", currency_profit="USD",
                    trade_contract_size=100.0),
        make_symbol(name="SILVER", description="Silver (5000 oz) vs US Dollar CFD",
                    path=r"Metals\SILVER", trade_calc_mode=4,
                    currency_base="XAG", currency_profit="USD",
                    trade_contract_size=5000.0),
        make_symbol(name="EURUSD", description="Euro vs US Dollar",
                    path=r"Forex\EURUSD", trade_calc_mode=0),
        make_symbol(name="BTCUSD", description="Bitcoin vs US Dollar CFD",
                    path=r"Cryptocurrencies\BTCUSD", trade_calc_mode=4,
                    currency_base="BTC", currency_profit="USD"),
        make_symbol(name="BRENT",
                    description="BRENT Crude Oil (100 barrels) CFD, USD",
                    path=r"Energies\BRENT", trade_calc_mode=4,
                    currency_base="USD", currency_profit="USD"),
        make_symbol(name="#AAPL", description="Apple Inc. CFD(15min delay)",
                    path=r"Stock CFDs\#AAPL", trade_calc_mode=2,
                    currency_base="USD", currency_profit="USD"),
    ]


@pytest.fixture
def admirals(admirals_universe):
    from mt5_mcp.core.symbols import SymbolResolver
    instance = SymbolResolver(FakeConnection(symbols=admirals_universe))
    instance.load()
    return instance


@pytest.fixture
def resolver(broker_universe):
    """A SymbolResolver primed with the miniature broker, no terminal needed."""
    from mt5_mcp.core.symbols import SymbolResolver
    instance = SymbolResolver(FakeConnection(symbols=broker_universe))
    instance.load()
    return instance
