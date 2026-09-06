"""Pydantic return types.

One schema for the whole server. The source projects each invented their own
field names and error shapes; a user must not be able to tell that this was
merged from five of them. Rules applied everywhere:

* every timestamp is a :class:`TimeTriple` — broker, display and UTC together,
  never a bare number whose clock you have to guess;
* every failure is a :class:`ToolError` with a ``fix`` sentence;
* nothing that could carry a credential is ever a field.

Own work. Field naming unified across MBT, MTX, ariadng, amirkhonov and PHUICMT.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    """Common config: reject unknown fields so a typo fails loudly in tests."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


class TimeTriple(Base):
    """One instant in the three clocks that matter."""

    broker: str = Field(description="Broker server time, naive, 'YYYY-MM-DD HH:MM:SS'.")
    display: str = Field(description="Configured display timezone, default Europe/Berlin.")
    utc: str = Field(description="Real UTC.")


class ToolError(Base):
    """A failure a tool chose to report rather than raise."""

    ok: Literal[False] = False
    error: str = Field(description="What went wrong, one line.")
    fix: str | None = Field(default=None, description="What to do about it.")
    code: int | None = Field(default=None, description="MT5 code, if there was one.")
    code_name: str | None = None
    retryable: bool = False


# ---------------------------------------------------------------------------
# Connection and diagnostics
# ---------------------------------------------------------------------------


class TerminalInfo(Base):
    path: str | None = None
    build: int | None = None
    data_dir: str | None = None
    broker_connected: bool | None = None
    algo_trading_enabled: bool | None = None
    trade_api_disabled: bool | None = None
    ping_ms: int | None = None


class AccountInfo(Base):
    login: str = Field(description="Masked. The full number is never returned.")
    server: str | None = None
    currency: str | None = None
    type: str | None = Field(default=None, description="DEMO, CONTEST or REAL.")
    margin_mode: str | None = Field(default=None, description="NETTING, EXCHANGE or HEDGING.")
    leverage: int | None = None
    balance: float | None = None
    equity: float | None = None
    free_margin: float | None = None
    margin: float | None = None
    margin_level: float | None = None
    profit: float | None = None
    trade_allowed: bool | None = None
    experts_allowed: bool | None = None


class ClockInfo(Base):
    measured: bool
    broker_minus_utc_hours: float | None = None
    broker_minus_display_hours: float | None = None
    measured_at_utc: str | None = None
    source: str | None = None
    stale: bool | None = None
    note: str | None = None


class StatusResult(Base):
    connected: bool
    terminal: TerminalInfo | None = None
    account: AccountInfo | None = None
    time: ClockInfo | None = None
    note: str | None = None


class DoctorCheck(Base):
    """One diagnostic line. ``fix`` is filled whenever status is not ``ok``."""

    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str
    fix: str | None = None


class DoctorResult(Base):
    overall: Literal["ok", "warn", "fail"]
    summary: str
    checks: list[DoctorCheck]
    backtest_path: str | None = Field(
        default=None,
        description="Which backtest route is active: 'cli', 'native' or 'none'.")


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


class SymbolBrief(Base):
    name: str
    description: str = ""
    group: str = ""
    kind: str = ""
    expires: bool = Field(
        default=False,
        description="True for a dated contract such as an index future. It "
                    "expires, rolls to a new ticker, and does not track the "
                    "cash price exactly.")
    in_market_watch: bool = False


class SymbolCandidate(SymbolBrief):
    confidence: float
    reasons: list[str] = Field(default_factory=list)


class ResolveResult(Base):
    query: str
    resolved: str | None
    unambiguous: bool
    candidates: list[SymbolCandidate] = Field(default_factory=list)
    note: str | None = None


class SymbolSpec(Base):
    """Everything needed to size and send an order for one symbol."""

    name: str
    description: str = ""
    group: str = ""
    kind: str = ""
    digits: int
    point: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int
    tick_size: float
    tick_value: float
    tick_value_profit: float | None = None
    tick_value_loss: float | None = None
    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""
    filling_modes: list[str] = Field(default_factory=list)
    execution_mode: str = ""
    trade_mode: str = ""
    expires: bool = False
    expiration: str | None = Field(
        default=None, description="Expiry date for a dated contract.")
    in_market_watch: bool = False
    spread_points: int | None = None


class TradabilityResult(Base):
    symbol: str
    tradable: bool
    reason: str
    session_open: bool | None = None
    last_tick: TimeTriple | None = None
    seconds_since_tick: float | None = None


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class Bar(Base):
    time: TimeTriple
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int | None = None
    real_volume: int | None = None


class BarsResult(Base):
    symbol: str
    timeframe: str
    count: int
    bars: list[Bar]
    truncated: bool = False
    note: str | None = None
    requests_used: int = Field(
        default=1,
        description="How many SDK calls the paging layer needed.")


class Tick(Base):
    time: TimeTriple
    bid: float
    ask: float
    last: float | None = None
    volume: int | None = None


class TicksResult(Base):
    symbol: str
    count: int
    ticks: list[Tick]
    truncated: bool = False


class QuoteResult(Base):
    symbol: str
    bid: float
    ask: float
    spread_points: float
    time: TimeTriple
    seconds_since_tick: float


class AvailabilityResult(Base):
    symbol: str
    timeframe: str
    first_bar: TimeTriple | None = None
    last_bar: TimeTriple | None = None
    bars_available: int
    max_bars_per_request: int | None = Field(
        default=None,
        description="Terminal's MaxBars ceiling. None means unlimited.")


# ---------------------------------------------------------------------------
# Positions, orders, history
# ---------------------------------------------------------------------------


class Position(Base):
    ticket: int
    symbol: str
    type: str
    volume: float
    price_open: float
    price_current: float
    sl: float = 0.0
    tp: float = 0.0
    profit: float
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""
    opened: TimeTriple
    ours: bool = Field(
        default=False,
        description="True when the magic number matches this server's.")


class PositionsResult(Base):
    count: int
    positions: list[Position]
    total_profit: float = 0.0


class Order(Base):
    ticket: int
    symbol: str
    type: str
    volume_current: float
    volume_initial: float
    price_open: float
    price_current: float | None = None
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""
    placed: TimeTriple
    expiration: TimeTriple | None = None
    ours: bool = False


class OrdersResult(Base):
    count: int
    orders: list[Order]


class Deal(Base):
    ticket: int
    order: int
    position_id: int
    symbol: str
    type: str
    entry: str
    volume: float
    price: float
    profit: float
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    magic: int = 0
    comment: str = ""
    time: TimeTriple


class HistoryResult(Base):
    count: int
    deals: list[Deal]
    realised_profit: float = 0.0
    total_commission: float = 0.0
    total_swap: float = 0.0
    frm: TimeTriple | None = Field(default=None, alias="from")
    to: TimeTriple | None = None


class PositionDetail(Base):
    position: Position
    deals: list[Deal] = Field(default_factory=list)
    risk_money: float | None = None
    reward_money: float | None = None
    r_multiple: float | None = Field(
        default=None,
        description="Current profit divided by the money at risk at the stop.")
    distance_to_sl_points: float | None = None
    distance_to_tp_points: float | None = None


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class PositionSizeResult(Base):
    symbol: str
    volume: float = Field(description="Lot size, already rounded to volume_step.")
    volume_unrounded: float
    risk_money: float
    risk_percent: float | None = None
    account_currency: str
    stop_distance_points: float
    stop_distance_price: float
    value_per_point_per_lot: float
    money_at_risk_at_stop: float = Field(
        description="What the rounded volume actually risks — the number that matters.")
    capped_by: str | None = Field(
        default=None,
        description="Which limit reduced the size: 'max_lot', 'volume_max', "
                    "'max_risk_percent', 'free_margin' or null.")
    warnings: list[str] = Field(default_factory=list)


class MarginResult(Base):
    symbol: str
    volume: float
    order_type: str
    price: float
    margin: float
    account_currency: str
    free_margin_after: float | None = None


class ProfitResult(Base):
    symbol: str
    volume: float
    order_type: str
    price_open: float
    price_close: float
    profit: float
    account_currency: str
    points: float


class ExposureLeg(Base):
    key: str
    volume_net: float
    volume_long: float
    volume_short: float
    profit: float
    positions: int


class ExposureResult(Base):
    account_currency: str
    equity: float
    by_symbol: list[ExposureLeg]
    by_currency: list[ExposureLeg]
    total_profit: float
    margin_used: float
    margin_level: float | None = None


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------


class GuardVerdict(Base):
    """What the safety rail decided, always returned with a trade attempt."""

    allowed: bool
    dry_run: bool
    checks_passed: list[str] = Field(default_factory=list)
    blocked_by: str | None = None
    reason: str | None = None
    fix: str | None = None


class OrderPreview(Base):
    """The ``order_check`` result, folded into every trade response."""

    retcode: int
    retcode_name: str
    comment: str
    margin_required: float | None = None
    free_margin_after: float | None = None
    balance: float | None = None
    equity: float | None = None
    would_succeed: bool


class TradeResult(Base):
    ok: bool
    dry_run: bool
    action: str
    symbol: str
    order_type: str
    volume: float
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    ticket: int | None = None
    order: int | None = None
    deal: int | None = None
    retcode: int | None = None
    retcode_name: str | None = None
    message: str = ""
    fix: str | None = None
    guard: GuardVerdict
    preview: OrderPreview | None = None
    filling_mode: str | None = None
    time: TimeTriple | None = None


class BatchCloseResult(Base):
    ok: bool
    dry_run: bool
    requested: int
    closed: int
    failed: int
    results: list[TradeResult] = Field(default_factory=list)
    guard: GuardVerdict
    message: str = ""


# ---------------------------------------------------------------------------
# Compile and backtest
# ---------------------------------------------------------------------------


class CompileDiagnostic(Base):
    severity: Literal["error", "warning", "info"]
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None


class CompileResult(Base):
    ok: bool = Field(
        description="True only when there were zero errors AND a fresh .ex5 exists.")
    source: str
    output: str | None = None
    errors: int = 0
    warnings: int = 0
    diagnostics: list[CompileDiagnostic] = Field(default_factory=list)
    ex5_written: bool = False
    ex5_age_seconds: float | None = None
    route: str = Field(default="cli", description="'cli' or 'native'.")
    message: str = ""
    raw_tail: str | None = None


class BacktestMetrics(Base):
    """The subset of tester metrics that survives across MT5 builds."""

    net_profit: float | None = None
    gross_profit: float | None = None
    gross_loss: float | None = None
    profit_factor: float | None = None
    expected_payoff: float | None = None
    recovery_factor: float | None = None
    sharpe_ratio: float | None = None
    balance_drawdown_max: float | None = None
    balance_drawdown_percent: float | None = None
    equity_drawdown_max: float | None = None
    equity_drawdown_percent: float | None = None
    total_trades: int | None = None
    profit_trades: int | None = None
    loss_trades: int | None = None
    short_trades: int | None = None
    long_trades: int | None = None
    largest_profit_trade: float | None = None
    largest_loss_trade: float | None = None
    initial_deposit: float | None = None
    history_quality_percent: float | None = None


class BacktestResult(Base):
    ok: bool
    expert: str
    symbol: str
    timeframe: str
    model: str
    frm: str = Field(alias="from")
    to: str
    deposit: float
    leverage: int
    route: str = Field(description="'cli' or 'native'.")
    duration_seconds: float
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)
    report_html: str | None = None
    report_images: list[str] = Field(default_factory=list)
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    fix: str | None = None
    log_tail: list[str] = Field(default_factory=list)


class BacktestComparison(Base):
    runs: list[BacktestResult]
    ranking: list[dict[str, Any]]
    criterion: str
    note: str = ""
