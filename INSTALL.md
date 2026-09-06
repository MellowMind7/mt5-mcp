# Installing mt5-mcp

Follow these in order. Steps 1 to 4 happen inside MetaTrader; step 5 is the
installer; step 6 is the check that everything works.

If anything goes wrong, jump to **Troubleshooting** at the end — or just ask
Claude *"Is my MetaTrader set up correctly?"*, which runs the full diagnosis and
explains each problem.

---

## 1. Install MetaTrader 5

Download it **from MetaQuotes**, not from your broker:

<https://www.metatrader5.com/en/download>

Brokers ship their own builds and often lag months behind. The build used to
develop this was 6163; a broker terminal on the same machine was on 5833 — 330
builds and about six months older.

Run the installer and accept the defaults. It lands in
`C:\Program Files\MetaTrader 5`.

> Already have a broker's MetaTrader installed? Keep it. Installing the
> MetaQuotes one alongside is fine, they do not interfere. mt5-mcp will find
> both and prefer the newer one.

---

## 2. Open a demo account

On first launch MetaTrader offers to open an account. If it does not, use
**File → Open an Account**.

1. Choose **MetaQuotes Ltd.** and click *Next*.
2. Choose **Open a demo account** and click *Next*.
3. Fill in a name, date of birth, email and phone. None of it is verified.
4. Leave **Use hedge in trading** ticked — it lets you hold opposing positions,
   which makes learning easier.
5. Server **MetaQuotes-Demo**, account type **Forex Hedged USD**, deposit
   100 000, leverage 1:100.
6. Tick the terms box and click *Next*.

MetaTrader shows your account number, password and investor password. **Write
them down now** — the password cannot be read back later, only replaced, and
replacing it needs the old one.

> **Use a demo account.** Everything in this guide assumes one. mt5-mcp blocks
> all trading tools on a live account until you deliberately change
> `MT5_ALLOW_LIVE` in `.env`.

---

## 3. Switch on algorithmic trading

In the toolbar, find the **Algo Trading** button.

- **Red square** = off. Click it.
- **Green triangle** = on. That is what you want.

Same setting under **Tools → Options → Expert Advisors**.

Without this, reading works but no order can be placed, and you get error 10027.

---

## 4. Keep the terminal running

The Python interface only talks to a **running, logged-in** terminal. Close
MetaTrader and mt5-mcp goes quiet.

On a normal desktop, just leave it open.

**On a VPS or remote desktop, this matters:** disconnect your remote session,
never sign out. Signing out ends the Windows session and closes the terminal.
Click the **X** on the remote desktop window rather than using *Sign out*.

If you want MetaTrader to start automatically after a reboot, set up a scheduled
task that runs **at logon** as **your own user**. Do not use "at system startup"
as SYSTEM — that starts the terminal in a session with no desktop, where the
Python interface cannot reach it. This is a real trap: on the development
machine a terminal had been started that way and looked fine in Task Manager
while being completely unreachable.

---

## 5. Run the installer

Double-click **`install.bat`**.

It will:

1. Check for `uv` and install it if missing
2. Install the Python dependencies
3. Find your terminal — and if you have more than one, ask which
4. Ask a few questions and write `.env`
5. Build the portable backtest copy at `C:\mt5tester` (about 160 MB)
6. Register the server with Claude Code
7. Run the diagnosis and show you the result

The questions have sensible defaults — press Enter to accept each one.

### Before your first backtest on a prop-firm account

A backtest opens a **second login with the same account**, because the portable
copy signs in on its own. Two brokers were tested and both allow it — the main
terminal stayed connected the whole time. Prop firms often enforce a single
session, though, and one of those may drop your main terminal instead.

Run one short backtest and watch the terminal before you rely on this during an
evaluation. If the main terminal disconnects, do backtests on a separate demo
account rather than the funded one.

### If you have more than one MetaTrader installed

Plenty of people do: a broker's terminal beside the MetaQuotes one. The
installer lists them all rather than guessing, with the build number and the
account each was last used with, because that is what tells them apart:

```
      2 MetaTrader installations found:
        [1] MetaTrader 5 <- suggested
            build 6182, running, last used AdmiralsGroup-Demo (***4321)
        [2] FTMO Global Markets MT5 Terminal
            build 5833, running, last used FTMO-Demo (***2109)
```

The suggestion is the newest build that is already running. Pick the one
holding the account you actually want to trade — a wrong pick here is not
obviously wrong later, it simply reports a different account than you expect.

You can change it afterwards in `.env` under `MT5_TERMINAL_PATH`.

### About the optional MCP key

The installer asks for an "MCP API key" and you can leave it empty.

MetaTrader build 6060 and newer runs its own MCP server locally. If you give
mt5-mcp its key, backtests and compiling go through that instead, which is
faster. Everything works without it.

To find the key: **Tools → Options → MCP**. Copy the *API Key* field.

> **What this means for your privacy.** When mt5-mcp uses that interface, your
> terminal is the *server* and Claude is the client — the traffic stays on
> `127.0.0.1` and nothing reaches MetaQuotes. This was verified by measurement:
> across 90 calls, the terminal opened no connection other than to its own quote
> server, and made no DNS lookup at all.
>
> **This does not apply to the chat window built into MetaTrader.** There the
> terminal is the client and your input goes to MetaQuotes' provider at
> `api.inferdeck.net`. Different direction, different consequence.

### About the second trading path

On the same options page, under **AI Assistant**, there is a **Trading**
dropdown. Set it to **Disabled**.

If it is on, the terminal's own assistant can place orders — without dry runs,
without your lot ceiling, without your daily loss limit, and without anything
being written to `trades.log`. `mt5_doctor` warns you when it is not disabled.

---

## 6. Check it worked

The installer finishes by running the diagnosis. Every line should read `OK`:

```
[  OK  ] terminal_found         MetaTrader 5 (build 6163, running)
[  OK  ] terminal_running       1 terminal process(es) running.
[  OK  ] connected              Connected to the terminal.
[  OK  ] logged_in              Logged in to DEMO account on MetaQuotes-Demo.
[  OK  ] broker_connection      Connected to the broker, ping 87 ms.
[  OK  ] algo_trading           Algorithmic trading is enabled.
[  OK  ] package_version        Terminal build 6163, package 5.0.6162.
[  OK  ] symbols                12455 symbols loaded; EURUSD resolves to EURUSD.
[  OK  ] live_quote             EURUSD bid 1.16171 ask 1.16172, 2 s old.
[  OK  ] broker_clock           Broker time is UTC+3 h, +1 h against Europe/Berlin.
[  OK  ] files_writable         Write access to ...\MQL5\Files.
[  OK  ] backtest_cli           Portable tester ready at C:\mt5tester.
[  OK  ] second_trading_path    The terminal's built-in assistant cannot trade.

Overall: OK - Everything checks out.
```

Then restart Claude Code and ask:

> **Is my MetaTrader set up correctly?**

---

## Troubleshooting

Every check that fails prints one sentence telling you what to do. These are the
common ones.

### "No MetaTrader 5 installation found"

Either it is not installed, or it is somewhere unusual. Open `.env` and set the
full path by hand:

```
MT5_TERMINAL_PATH=D:\Trading\MetaTrader 5\terminal64.exe
```

### "No terminal process is running"

Start MetaTrader and log in. This is the single most common cause of everything
else failing.

### "Algorithmic trading is switched off"

Click **Algo Trading** in the toolbar until it turns green.

### "This is a LIVE account"

mt5-mcp refuses to trade a live account by default. Reading works normally. To
allow trading, set `MT5_ALLOW_LIVE=true` in `.env` and restart — deliberately,
not by accident.

### Backtests do not run

Check the `backtest_cli` line in the diagnosis.

- *"No portable tester terminal is configured"* — re-run `install.bat`.
- *"The tester directory contains a space"* — MetaTrader silently ignores a
  configuration path containing a space, so the tester must live somewhere like
  `C:\mt5tester`. Do not move it under `C:\Program Files`.

### The tester folder is getting large

Normal. It caches downloaded price history — roughly 100 MB after a handful of
runs, growing with each new symbol and timeframe. Ask Claude to *"clean up the
backtest cache"*, or call `backtest_cleanup`. Nothing of yours is lost.

### A symbol is not found

Ask *"search for symbols matching gold"*. Brokers name things very differently,
and `symbol_search` shows you what yours actually offers.

### Trades are rejected with error 10016

Your stop is too close to the current price. Every broker sets a minimum
distance; `symbol_info` reports it as `stops_level_points`.

### Trades are rejected with error 10018

The market is closed. Ask *"is EURUSD tradable right now?"* — it distinguishes a
closed market from a real problem.

---

## Where things live

| | |
|---|---|
| Configuration | `.env` — holds credentials, never commit it |
| Server log | `logs/mt5-mcp.log` |
| Trade log | `logs/trades.log` — every attempt, including dry runs |
| Backtest terminal | `C:\mt5tester` |
| Backtest reports | `C:\mt5tester\mt5mcp_*.htm` plus four charts each |
