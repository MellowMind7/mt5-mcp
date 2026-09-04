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

That is one broker. Prop firms and brokers with session limits may evict the
first session instead, which would knock the user's terminal offline mid-backtest.
`mt5_doctor` should gain a pre-flight check for this, and it needs testing
against a real broker demo before anyone relies on it.

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
small relative to their contract size. `risk_position_size` handles this — it
takes the figure from `order_calc_profit` and warns when the two disagree — but
the same broker data feeds `symbol_info`, which still reports the raw field.

Consider annotating `symbol_info` with the measured value alongside the reported
one, so the discrepancy is visible wherever a user looks rather than only where
it is corrected.

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
