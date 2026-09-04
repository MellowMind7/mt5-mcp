# mt5-mcp

One MCP server for MetaTrader 5. Ask questions about your account and the
markets in plain language, size positions from a risk budget, write and backtest
expert advisors — all against your own terminal, on your own machine.

Nothing here trades on its own. Every order needs you to say yes.

---

## Try these first

Once `install.bat` has finished, open Claude Code and type any of these.

1. **Is my MetaTrader set up correctly?**
2. **Show me the last 200 daily candles for the DAX.**
3. **What is my account balance, and how much margin do I have free?**
4. **If I risk 1% on EURUSD with a 30-pip stop, how many lots is that?**
5. **What would gold cost me in margin for half a lot?**
6. **List my open positions and tell me which one is furthest from its stop.**
7. **Is the Nasdaq tradable right now, or is the market closed?**
8. **What is EURUSD called at my broker, and what about gold and the Dow?**
9. **Write me an expert advisor that buys when RSI drops below 30, and backtest
   it over the last two years.**
10. **Show me my closed trades from the past month with the running total.**

Number 4 is the one worth learning. It reads your broker's real contract
specification and tells you what the rounded lot size *actually* risks — which
is rarely exactly what you asked for.

---

## What it does

| Area | Tools |
|---|---|
| Connection and diagnosis | `mt5_connect`, `mt5_status`, `mt5_account`, `mt5_disconnect`, `mt5_doctor` |
| Symbols | `symbol_resolve`, `symbol_search`, `symbol_info`, `symbol_select`, `symbol_is_tradable_now` |
| Market data | `bars_get`, `ticks_get`, `quote_get`, `data_availability` |
| Account | `positions_get`, `orders_get`, `history_get`, `position_detail` |
| Risk | `risk_position_size`, `risk_calc_margin`, `risk_calc_profit`, `risk_exposure` |
| Trading | `trade_market`, `trade_pending`, `trade_modify`, `trade_close`, `trade_close_batch`, `trade_cancel` |
| Code and backtesting | `mql_compile`, `backtest`, `backtest_report`, `backtest_compare`, `backtest_cleanup` |

### Symbol resolution, and why it matters

Say "gold" and you get gold. That sounds trivial until you look at what brokers
actually call things.

On the MetaQuotes demo this was built against, a symbol literally named `GOLD`
exists — it is **Barrick Gold Corporation**, a mining share. `DAX` is a
**Global X ETF**. `DOW` is **Dow Inc**. A tool that matched names exactly would
not fail loudly on any of these; it would hand back the wrong instrument and say
nothing.

So `symbol_resolve` classifies instruments by what they *are* — using the
contract specification, not folder names — and a name match that contradicts the
request loses to a description match that agrees with it. It learns each
broker's naming at runtime, so `EURUSD.raw`, `EURUSDm` and `EURUSD` all work
wherever you are.

```
"gold"   ->  XAUUSD   (not Barrick Gold)
"DAX"    ->  DE40     (not the Global X ETF)
"Dow"    ->  US30     (the full contract, not US30M)
"SP500"  ->  US500
```

### Position sizing

`risk_position_size` derives the lot size from what you are willing to lose, and
it takes the number from the terminal's own pricing engine rather than from the
symbol's advertised tick value.

That distinction is not academic. On the demo used for development, XAUUSD
reports a tick value ten times too small; sizing gold from that field would have
built a position **ten times larger** than intended. When the two sources
disagree, the tool uses the pricing engine and says so in a warning.

Lot sizes are always rounded **down** to the broker's step, and the response
reports what the rounded size really risks.

### The safety rail

Configured in `.env`, applied to every write tool without exception:

- **Live accounts are blocked** unless you set `MT5_ALLOW_LIVE=true`. Reading
  always works.
- **Every trade tool defaults to `dry_run=true`.** You get the broker's own
  pre-flight check — margin, free margin afterwards, whether it would be
  accepted — and nothing is sent.
- **Lot ceiling, risk ceiling, position count, daily loss limit.** Any of them
  can refuse, and the refusal says which one and what to change.
- **Closing every position** additionally requires `confirm="CLOSE ALL"`.
- **Positions this server did not open are left alone** unless you pass
  `include_foreign=true`.
- **Every attempt is written to `trades.log`** — dry runs and refusals included.
- **Nothing runs on a timer.** No scheduler, no background trading, no
  auto-close.

If a limit cannot be evaluated — the account read fails, the history query
errors — the trade is **blocked**, not allowed. A rail that opens when it is
confused is not a rail.

### Times

Every timestamp comes back three ways:

```json
{"broker":  "2026-09-04 16:00:00",
 "display": "2026-09-04 15:00:00",
 "utc":     "2026-09-04 13:00:00"}
```

MetaTrader's raw timestamps are in broker server time, which is neither UTC nor
your local clock. The offset is *measured* against a live tick and re-measured
every half hour, so it survives a daylight-saving change mid-session.

---

## Requirements

- Windows
- MetaTrader 5, installed and logged in to an account
- Python 3.12 or newer — `install.bat` handles the rest

Start with **`INSTALL.md`**. It covers the steps inside MetaTrader too: opening a
demo account, switching algorithmic trading on, and keeping the terminal
running.

---

## Backtesting

Backtests run through a separate portable copy of your terminal, created by
`install.bat` at `C:\mt5tester`. This is not optional cleverness: a second
MetaTrader instance sharing a data folder with the one you have open exits after
six seconds without running anything, so the tester needs its own.

The copy inherits your terminal's encrypted credential store, so it signs in by
itself and no password is written anywhere.

This route works on **every** MetaTrader build. If your terminal is build 6060
or newer and you put its MCP key in `.env`, backtests and compiling use the
terminal's own faster interface instead. `mt5_doctor` tells you which route is
active.

The tester's downloaded price history grows — roughly 100 MB after a handful of
runs. `backtest_cleanup` clears it; it is a cache and comes back on demand.

---

## Checking it works

```
uv run python smoke_test.py     # every read tool, once, against your terminal
uv run pytest                   # the calculations, no terminal needed
```

Or just ask Claude: *"Is my MetaTrader set up correctly?"*

---

## Safety, plainly stated

This is a tool for **studying** markets and **testing** ideas. It is not a
trading system, it has no strategy, and it will not make money for you.

Trading carries real financial risk. Backtest results describe the past and
predict nothing. The defaults here point at a demo account on purpose — leave
them that way until you have a reason not to.

Nothing in this software trades without a person deciding to.

---

## Licence

MIT — see `LICENSE`.

Built by merging five open-source projects, each credited with its copyright
holder and licence text in `THIRD-PARTY-NOTICES.md`. `BEFUND.md` records which
projects were examined, what licence each actually carries, and why three of
them contributed ideas but no code.
