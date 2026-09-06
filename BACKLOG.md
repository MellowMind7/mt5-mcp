# Backlog

Everything found during the v1 build that was deliberately **not** built.
Recorded so the decisions are auditable and nothing is silently lost.

v1 shipped under a feature freeze: the only things added after it were fixes to
defects that stopped v1 working.

---

## Deferred to v2 by decision

### `MCP_Bridge.mq5` and `chart_screenshot`

An MQL5 expert advisor running on a chart, talking to the server through files
in `MQL5/Files`, so `ChartScreenShot()` can return the real chart with the
user's own indicators and drawings on it.

Why it is worth doing: the terminal's built-in MCP server has `chart_open`,
`chart_add_indicator`, `chart_apply_template` and `chart_close` — but **no
screenshot tool of any kind**, in any of its 73. It is the one chart capability
that has to be built rather than borrowed.

Once the expert exists for the screenshot, adding `ChartOpen`,
`ChartSetSymbolPeriod` and `ChartIndicatorAdd` to it costs about thirty more
lines and makes chart control work on **every** build rather than only 6060 and
newer. That is cheaper than depending on the terminal's version.

### MCP resources and prompts

Planned: `mt5://start`, `mt5://symbols` (this broker's naming, generated from
the live symbol list), `mt5://errors` (the retcode table), `mt5://ea-bridge`,
plus prompts for checking an account, fetching data, building and testing an
expert, and sizing a position.

The retcode table is already renderable — `errors.retcode_table_markdown()`
exists and is tested. The broker naming resource has its data source ready too,
in `SymbolResolver.naming_scheme()`. Only the wiring is missing.

Note that the terminal's own MCP server exposes **neither** resources nor
prompts — both lists come back empty — so there is nothing to borrow here.

---

## Found during the build, not acted on

### Auto-update on the portable tester

The portable backtest copy contacted MetaQuotes' update server on first run and
downloaded a build update unprompted. It did not break the run once the real bug
beside it was fixed, but it costs a minute and could in principle replace the
executable underneath a running test.

Worth investigating whether the update channel can be pinned or disabled for the
tester copy specifically.

### Concurrent login was only verified against one broker

A portable tester signing in with the **same account** as the interactive
terminal was measured on MetaQuotes-Demo: 23 samples over 46 seconds, zero
dropouts, the main terminal stayed connected throughout, and the server logged
the concurrent authorisation rather than replacing it.

**Re-tested on 2026-09-06 against AdmiralsGroup-Demo**, a real broker, on a
netting account: 20 samples, zero dropouts, the main terminal stayed connected
and the backtest produced its report. Two brokers now agree.

Still open for prop firms specifically. Those often enforce a single session and
may evict the first one, which would knock the user's terminal offline
mid-backtest. `mt5_doctor` should gain a pre-flight check before anyone relies
on this with a funded evaluation account.

### `send_web_request` on the native server

Returns `code 1003`, which traces to the terminal's own **Web: GET requests
only** policy rather than to anything in this project. Not used by mt5-mcp.

### `symbol_resolve` and multi-word queries

"Dow mini" resolves to nothing, because concept lookup matches whole words
against a fixed table. `US30M` resolves correctly when named directly. A phrase
parser would fix it; the current behaviour fails safe by returning nothing
rather than guessing.

### Broker tick values that contradict the pricing engine

XAUUSD and XAGUSD on MetaQuotes-Demo report `trade_tick_value` ten times too
small relative to their own contract size. `risk_position_size` handles this —
it takes the figure from `order_calc_profit` and warns when the two disagree —
but the same broker data feeds `symbol_info`, which still reports the raw field.

Consider annotating `symbol_info` with the measured value alongside the reported
one, so the discrepancy is visible wherever a user looks rather than only where
it is corrected.

#### Checked: is this a tick-versus-point confusion?

The common sizing formula divides by `tick_size / point`, which silently
conflates the two whenever a broker sets them differently. If that were the
cause here, the fix would be a wording change rather than a data problem: no
fault of the broker's, just an arithmetic assumption that does not hold.

Measured on 2026-09-06 against MetaQuotes-Demo. **It is not the cause.**

| Symbol | digits | `point` | `trade_tick_size` | `tick_size / point` | `trade_tick_value_loss` | `contract_size` | `contract x tick_size` | pricing engine, per point per lot |
|---|---|---|---|---|---|---|---|---|
| XAUUSD | 2 | 0.01    | 0.01    | 1.000000 | **0.1**  | 100     | **1.0**  | **1.0**  |
| XAGUSD | 3 | 0.001   | 0.001   | 1.000000 | **0.5**  | 5000    | **5.0**  | **5.0**  |
| DE40   | 2 | 0.01    | 0.01    | 1.000000 | 0.011614 | 1       | 0.01 EUR | 0.011610 |
| EURUSD | 5 | 0.00001 | 0.00001 | 1.000000 | 1.0      | 100000  | 1.0      | 1.0      |

`trade_tick_size` equals `point` on every symbol checked, so the divisor is
exactly 1 and the formula passes `trade_tick_value_loss` through untouched. The
formula cannot have produced the discrepancy.

What remains is an internal contradiction in the broker's own symbol record: for
the two metals, `contract_size x tick_size` and `trade_tick_value_loss` disagree
by a factor of ten, the same factor on both. The pricing engine agrees with the
contract-size figure. DE40 is consistent once EUR-to-USD conversion is applied
(0.01 EUR becomes 0.011614 USD), which is what a correct cross-currency tick
value should look like.

The warning text therefore stays as it is: it reports a measurable disagreement
between two of the broker's own numbers, not an assumption of ours.

#### Answered: it is the MetaQuotes demo feed, not MT5

Same table, taken on 2026-09-06 against **AdmiralsGroup-Demo**:

| Symbol | `point` | `tick_size` | `ts/point` | `tv_loss` | `contract` | `contract x tick_size` |
|---|---|---|---|---|---|---|
| GOLD      | 0.01    | 0.01    | 1.0 | **1.000000** | 100    | **1.00** |
| SILVER    | 0.001   | 0.001   | 1.0 | **5.000000** | 5000   | **5.00** |
| GERMANY40 | 0.01    | 0.01    | 1.0 | 0.011614     | 1      | 0.01 EUR |
| EURUSD    | 0.00001 | 0.00001 | 1.0 | 1.000000     | 100000 | 1.00     |

The metals are **consistent here**: gold reports 1.0 against a contract-implied
1.0, silver 5.0 against 5.0. `tick_size` again equals `point` throughout.

So the factor of ten is specific to the MetaQuotes demo feed, not to MetaTrader
and not to the formula. The pricing-engine-first approach in
`risk_position_size` costs nothing where the data is already right and is the
only thing that saves the position size where it is not.

#### The weekend gap, and how it was closed

`order_calc_profit` returns nothing while a market is closed, so over a weekend
the sizing used to fall back to the reported tick value — with its warning
attached, but still the wrong number on that one feed. A gold position sized on
a Sunday against MetaQuotes-Demo would have been ten times too large, and a
weekend is exactly when people sit down to plan trades.

**Fixed.** `contract_size * point` is now a third source. It is static, needs no
live price, and is compared against the reported tick value whenever the engine
is silent:

* both agree, within 2% — the figure is used, no warning needed;
* they disagree — `risk_position_size` **raises** instead of answering, naming
  both numbers and the factor between them;
* profit currency is not the account currency — the static figure is in the
  wrong money and cannot be compared, so the tick value is used with the caveat
  attached rather than refusing a perfectly good instrument for the wrong reason.

Verified live against AdmiralsGroup-Demo with the market shut: GOLD, SILVER,
EURUSD, GERMANY40 and `[SP500]` all sized correctly, because that broker's two
static sources agree. The refusal path is covered by unit tests carrying the
real MetaQuotes numbers.

### ~~Index resolution picks futures and ETFs over the cash index~~ — FIXED

Found 2026-09-06 on AdmiralsGroup-Demo, fixed the same day under the feature
freeze exception. Kept here because the reasoning is worth having.

That broker separates three tiers that trade very differently:

| Group | n | Naming | What it is |
|---|---|---|---|
| `Cash Indices CFDs` | 19 | `[DJI30]`, `[SP500]`, `[FTSE100]`, `GERMANY40`, `US100` | what a trader means |
| `Index Futures CFDs` | 22 | `_YM_U6`, `_ES_U6`, `_US100_U6` | **expiring contracts** |
| `ETF CFDs` | 208 | `#VUSA`, `#SPY`, `#CAC` | **15-minute delayed** |

The resolver currently returns:

| Query | Returned | Should be |
|---|---|---|
| `DOW` | `_YM_U6` — Dow Jones Futures, **Exp: Sep 2026** | `[DJI30]` |
| `SP500` | `#VUSA` — Vanguard S&P 500 UCITS ETF, **15min delay** | `[SP500]` |
| `NAS100` | `_US100_U6` — US Tech 100 Futures, **Exp: Sep 2026** | `US100` |
| `CAC` | `#CAC` — Amundi CAC 40 UCITS ETF | `[CAC40]` |
| `US30` | not found | `[DJI30]` |

Everything else resolved correctly on this broker — `GOLD`, `SILVER`,
`PLATINUM`, `GERMANY40`, `[FTSE100]`, `[JP225]`, `BTCUSD`, `BRENT`, `EURUSD`,
`USDJPY`. The bracket and `#` prefixes are handled fine. It is specifically the
cash-versus-derivative distinction that is missing.

This is the same failure the module exists to prevent, one layer down. Asking
for the S&P 500 and receiving a delayed ETF, or the Dow and receiving a contract
that expires this month, is a silently wrong instrument — not an error the user
will see until the position behaves oddly or the backtest is worthless.

Two gaps cause it:

1. **No notion of expiry.** `symbol_info` exposes `expiration_time`; a symbol
   that has one is a dated contract and should lose to an equivalent that does
   not, unless the user asked for futures.
2. **ETFs classify as `other`, not `stock`.** `_classify` looks for `stock`,
   `share`, `equit` in the group name but not `etf` or `fund`, so `#VUSA`
   escaped the penalty that keeps shares away from index queries.

The fix is small and sits entirely inside `_classify` and the scoring in
`symbols.py`: add expiry demotion, add ETF to the share keywords, and prefer the
cash tier for index concepts. It needs testing against both brokers, because
MetaQuotes-Demo has no futures tier at all and would not catch a regression.

**Fixed.** Dated contracts are demoted by 0.35 whenever `expiration_time` is
set — generic, never a name pattern, because `_YM_U6` becomes `_YM_Z6` next
quarter. `etf`, `fund`, `fonds` and `trust` joined the share keywords in
`_classify`. Bracket affixes are stripped like `#` and `_`, so `[SP500]` stems
to `SP500`. Concept matching now sees the ticker as well as the description,
because the cash Dow is named `[DJI30]` while its description never says "30".

Futures and ETFs are demoted, not hidden: `_YM_U6` still appears among the
candidates for "DOW" and still resolves by name. Asking for "DAX futures" lifts
the demotion.

Regression covers both brokers as separate fixtures — `broker_universe` for
MetaQuotes-Demo, `admirals_universe` for the three-tier layout. The second one
exists because MetaQuotes has no futures tier and could never have caught this.

---

## Explicitly out of scope, permanently

### MetaTrader 4

No official Python binding exists; everything goes through expert-advisor
bridges. That is a different project with a different architecture, not a
feature of this one.

### Server-side indicator calculation

Deliberately dropped. Indicator values must agree with what the user sees on
their chart, and the only way to guarantee that is to take them from the
terminal — through the chart bridge or through an indicator that logs its own
signals. A second implementation in Python would drift from the chart and be
believed anyway.

This is also why `psyb0t/mt5-httpapi` contributed nothing: server-side analysis
was the only thing wanted from it.

### Automated strategies, bots, signal services

Not built and not wanted. Nothing in this server runs on a timer, and every
order requires a person in the loop. The unattended enforcement mode present in
one of the source projects was deliberately left behind for the same reason.

### Cloud or public endpoints

The server runs locally, beside the terminal, on stdio. There is no listener and
nothing to expose.
