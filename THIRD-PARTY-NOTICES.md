# Third-party notices

`mt5-mcp` merges work from several open-source projects. This file satisfies the
only condition the MIT licence imposes: the copyright notice and the licence
text travel with the software.

Five projects were adopted as code. Three were **not** — their licence status
did not permit it — and are listed at the end for completeness, because ideas
were taken from them even though no code was.

Every file under `src/mt5_mcp/` that builds on adopted code names its origin and
commit in the module docstring. Files without such a note are original work.

---

## Projects whose code is included

### MBT — MetaTrader Backtesting Toolkit

* Repository: <https://github.com/FXDavid-OffbeatForex/MBT>
* Licence: MIT
* Copyright: **Copyright (c) 2026 davidcyberdash**
* Commit referenced: `186eb6b`

Used for: the headless Strategy Tester approach — writing a `[Tester]` ini and
driving `terminal64.exe /config:` as a subprocess — together with its careful
handling of MT5 data-folder discovery (portable versus `%APPDATA%`, resolving
`origin.txt`) and its BOM-aware decoding of MT5's UTF-16 text files. See
`src/mt5_mcp/core/tester.py` and `src/mt5_mcp/core/connection.py`.

### MTX — MetaTrader Execution Toolkit

* Repository: <https://github.com/FXDavid-OffbeatForex/MTX>
* Licence: MIT
* Copyright: **Copyright (c) 2026 davidcyberdash**
* Commit referenced: `908ca9b`

Used for: the position-sizing formula (`value_per_point_per_lot = tick_value /
(tick_size / point)`), the guardrail set — maximum open positions, daily loss
limit, and reconstructing the day's starting balance from the day's closing
deals — and the order-execution flow. See `src/mt5_mcp/tools/risk_tools.py`,
`src/mt5_mcp/core/guard.py` and `src/mt5_mcp/tools/trade_tools.py`.

MTX's unattended enforcement mode, which closes positions on its own when a
limit is breached, is deliberately **not** carried over. `mt5-mcp` never trades
without a human in the loop.

### metatrader-mcp-server

* Repository: <https://github.com/ariadng/metatrader-mcp-server>
* Licence: MIT
* Copyright: **Copyright (c) 2025 Aria Dhanang**
* Commit referenced: `bd2ff59`

Used for: routing every MetaTrader5 SDK call through a single dedicated worker
thread, because the SDK is not thread-safe and concurrent use corrupts its IPC
channel in ways that surface later as wrong data. Also the tool shape for
positions and orders. See `src/mt5_mcp/core/connection.py` and
`src/mt5_mcp/tools/account_tools.py`.

### metatrader5-mcp

* Repository: <https://github.com/amirkhonov/metatrader5-mcp>
* Licence: MIT
* Copyright: **Copyright (c) 2024-2026 MetaTrader5-MCP Contributors**
* Commit referenced: `18cd5d3`

Used for: the margin and profit preview tools built on `order_calc_margin` and
`order_calc_profit`, and the Market Watch management approach. See
`src/mt5_mcp/tools/risk_tools.py` and `src/mt5_mcp/tools/symbol_tools.py`.

### mcp-mt5

* Repository: <https://github.com/PHUICMT/mcp-mt5>
* Licence: MIT
* Copyright: **Copyright (c) 2026 PHUICMT**
* Commit referenced: `9a859e1`

Used for: the Strategy Tester ini layout and the parsing of MT5's HTML report
into named metrics. See `src/mt5_mcp/core/tester.py`.

---

## The MIT licence

All five projects above ship the identical standard MIT text, verified by
checksum after removing the copyright line. It is reproduced once here and
applies to each of them under its own copyright holder named above.

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Projects examined but NOT used as code

These were reviewed. No code from them appears in `mt5-mcp`. They are recorded
here so the distinction is auditable rather than a matter of trust. The full
reasoning is in `BEFUND.md`.

### mcp-metatrader5-server

* Repository: <https://github.com/Qoyyuum/mcp-metatrader5-server>
* Licence status: **no LICENCE file in the repository.** MIT is asserted only by
  a `pyproject.toml` classifier and the single word "MIT" in the README.

Without a licence text there is no named copyright holder and no warranty
disclaimer, so the MIT condition could not be satisfied even in principle. Only
the *idea* — shipping MCP resources and prompts as built-in guidance — was
noted, and that is out of scope for v1 anyway.

### metatrader-mcp

* Repository: <https://github.com/ali-rajabpour/metatrader-mcp>
* Licence status: **no LICENCE file.** The README points at a `LICENSE` that
  does not exist, and `authors` is still the unedited template
  (`Your Name <your.email@example.com>`), so there is no identifiable rights
  holder.

Only the idea of an EA-based chart bridge was noted. The technique it rests on,
`ChartScreenShot()`, is documented by MetaQuotes.

### mt5-httpapi

* Repository: <https://github.com/psyb0t/mt5-httpapi>
* Licence: **WTFPL version 2**, `Copyright (C) 2026 Ciprian Mandache`

The WTFPL is in fact more permissive than MIT and would have allowed reuse. It
is excluded because this project restricts itself to MIT, Apache-2.0 and BSD,
and because what was wanted from it — server-side indicator calculation — was
deliberately dropped from the design. Indicator values come from the terminal
itself so that they agree with what the user sees on the chart.

---

## Runtime dependencies

`mt5-mcp` installs these at run time. They are not redistributed here; each
carries its own licence.

| Package | Licence |
|---|---|
| `MetaTrader5` | proprietary, published by MetaQuotes Ltd. on PyPI |
| `mcp` (Model Context Protocol SDK) | MIT |
| `pydantic` | MIT |
| `python-dotenv` | BSD-3-Clause |
| `pandas` | BSD-3-Clause |
| `tzdata` | Apache-2.0 |

**No MetaQuotes binary is redistributed with this project.** `install.bat`
copies `terminal64.exe` and `metatester64.exe` from the user's own existing
installation to create the portable backtest instance.
