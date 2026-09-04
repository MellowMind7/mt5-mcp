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
def resolver(broker_universe):
    """A SymbolResolver primed with the miniature broker, no terminal needed."""
    from mt5_mcp.core.symbols import SymbolResolver
    instance = SymbolResolver(FakeConnection(symbols=broker_universe))
    instance.load()
    return instance
