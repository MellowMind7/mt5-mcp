"""The safety rail. These tests describe the promises the README makes."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from mt5_mcp.config import Settings, mask_login
from mt5_mcp.core.guard import Guard, GuardBlocked, TradeIntent


@dataclass
class FakeAccount:
    trade_mode: int = 0          # 0 DEMO, 2 REAL
    balance: float = 10_000.0
    equity: float = 10_000.0
    margin_free: float = 10_000.0
    trade_allowed: bool = True
    currency: str = "USD"
    login: int = 123456789


@dataclass
class FakeTerminal:
    trade_allowed: bool = True


@dataclass
class FakePosition:
    magic: int = 770001
    profit: float = 0.0
    ticket: int = 1


@dataclass
class GuardConnection:
    """Answers only the four calls the rail makes."""

    account: FakeAccount = field(default_factory=FakeAccount)
    terminal: FakeTerminal = field(default_factory=FakeTerminal)
    positions: list = field(default_factory=list)
    deals: list = field(default_factory=list)

    def call(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", str(fn))
        return {
            "account_info": self.account,
            "terminal_info": self.terminal,
            "positions_get": self.positions,
            "history_deals_get": self.deals,
        }.get(name)


def make_guard(tmp_path, connection=None, **settings_overrides) -> Guard:
    defaults = dict(allow_live=False, max_lot=1.0, max_risk_percent=2.0,
                    max_open_positions=10, daily_loss_limit_percent=5.0,
                    magic=770001, log_dir=str(tmp_path))
    defaults.update(settings_overrides)
    return Guard(connection or GuardConnection(), Settings(**defaults))


class TestLiveAccount:
    def test_live_account_is_blocked_by_default(self, tmp_path):
        connection = GuardConnection(account=FakeAccount(trade_mode=2))
        guard = make_guard(tmp_path, connection)
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "live_account"
        assert "MT5_ALLOW_LIVE" in verdict.fix

    def test_live_account_allowed_when_explicitly_enabled(self, tmp_path):
        connection = GuardConnection(account=FakeAccount(trade_mode=2))
        guard = make_guard(tmp_path, connection, allow_live=True)
        assert guard.evaluate(TradeIntent(action="open", volume=0.1)).allowed

    def test_demo_account_is_fine(self, tmp_path):
        guard = make_guard(tmp_path)
        assert guard.evaluate(TradeIntent(action="open", volume=0.1)).allowed


class TestFailClosed:
    """A rail that opens when confused is not a rail."""

    def test_unreadable_account_blocks(self, tmp_path):
        class Broken(GuardConnection):
            def call(self, fn, *args, **kwargs):
                if getattr(fn, "__name__", "") == "account_info":
                    return None
                return super().call(fn, *args, **kwargs)

        guard = make_guard(tmp_path, Broken())
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "account_unreadable"

    def test_unreadable_history_blocks(self, tmp_path):
        connection = GuardConnection()
        connection.deals = None  # history_deals_get failed
        guard = make_guard(tmp_path, connection)
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "daily_loss_unreadable"


class TestLimits:
    def test_lot_ceiling(self, tmp_path):
        guard = make_guard(tmp_path)
        verdict = guard.evaluate(TradeIntent(action="open", volume=5.0))
        assert not verdict.allowed
        assert verdict.blocked_by == "max_lot"

    def test_lot_at_the_ceiling_is_allowed(self, tmp_path):
        guard = make_guard(tmp_path)
        assert guard.evaluate(TradeIntent(action="open", volume=1.0)).allowed

    def test_risk_ceiling(self, tmp_path):
        guard = make_guard(tmp_path)
        verdict = guard.evaluate(
            TradeIntent(action="open", volume=0.1, risk_percent=5.0))
        assert not verdict.allowed
        assert verdict.blocked_by == "max_risk_percent"

    def test_position_count(self, tmp_path):
        connection = GuardConnection(positions=[FakePosition()] * 10)
        guard = make_guard(tmp_path, connection)
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "max_open_positions"

    def test_daily_loss_limit(self, tmp_path):
        # Floating loss of 600 on a 10,000 start is 6%, over the 5% limit.
        connection = GuardConnection(
            positions=[FakePosition(profit=-600.0)])
        guard = make_guard(tmp_path, connection)
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "daily_loss_limit"

    def test_daily_loss_below_the_limit_is_allowed(self, tmp_path):
        connection = GuardConnection(positions=[FakePosition(profit=-100.0)])
        guard = make_guard(tmp_path, connection)
        assert guard.evaluate(TradeIntent(action="open", volume=0.1)).allowed

    def test_limits_do_not_apply_to_closing(self, tmp_path):
        """A trader must always be able to get out, even at the limit."""
        connection = GuardConnection(positions=[FakePosition(profit=-9000.0)] * 20)
        guard = make_guard(tmp_path, connection)
        assert guard.evaluate(TradeIntent(action="close")).allowed


class TestTerminalState:
    def test_algo_trading_off_blocks(self, tmp_path):
        connection = GuardConnection(terminal=FakeTerminal(trade_allowed=False))
        guard = make_guard(tmp_path, connection)
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "algo_trading_off"
        assert "Algo Trading" in verdict.fix

    def test_broker_disabled_account_blocks(self, tmp_path):
        connection = GuardConnection(account=FakeAccount(trade_allowed=False))
        guard = make_guard(tmp_path, connection)
        verdict = guard.evaluate(TradeIntent(action="open", volume=0.1))
        assert not verdict.allowed
        assert verdict.blocked_by == "account_trade_disabled"


class TestBatchConfirmation:
    def test_close_all_needs_the_phrase(self, tmp_path):
        guard = make_guard(tmp_path)
        verdict = guard.evaluate(
            TradeIntent(action="close_batch", scope="all"))
        assert not verdict.allowed
        assert verdict.blocked_by == "confirmation_missing"

    def test_close_all_with_the_phrase_is_allowed(self, tmp_path):
        guard = make_guard(tmp_path)
        verdict = guard.evaluate(
            TradeIntent(action="close_batch", scope="all", confirm="CLOSE ALL"))
        assert verdict.allowed

    def test_the_phrase_is_case_insensitive_but_must_be_right(self, tmp_path):
        guard = make_guard(tmp_path)
        assert guard.evaluate(TradeIntent(action="close_batch", scope="all",
                                          confirm="close all")).allowed
        assert not guard.evaluate(TradeIntent(action="close_batch", scope="all",
                                              confirm="yes")).allowed

    def test_narrower_scopes_need_no_phrase(self, tmp_path):
        guard = make_guard(tmp_path)
        assert guard.evaluate(
            TradeIntent(action="close_batch", scope="profitable")).allowed


class TestOwnership:
    def test_recognises_its_own_magic(self, tmp_path):
        guard = make_guard(tmp_path)
        assert guard.owns(770001)
        assert not guard.owns(12345)
        assert not guard.owns(0)
        assert not guard.owns(None)

    def test_filter_withholds_foreign_positions(self, tmp_path):
        guard = make_guard(tmp_path)
        rows = [FakePosition(magic=770001), FakePosition(magic=999),
                FakePosition(magic=0)]
        kept, withheld = guard.filter_ours(rows, include_foreign=False)
        assert len(kept) == 1
        assert withheld == 2

    def test_include_foreign_keeps_everything(self, tmp_path):
        guard = make_guard(tmp_path)
        rows = [FakePosition(magic=770001), FakePosition(magic=999)]
        kept, withheld = guard.filter_ours(rows, include_foreign=True)
        assert len(kept) == 2 and withheld == 0


class TestTradeLog:
    def test_every_attempt_is_recorded(self, tmp_path):
        guard = make_guard(tmp_path)
        guard.record(TradeIntent(action="open", symbol="EURUSD", dry_run=True),
                     {"volume": 0.1}, "DRY_RUN", None)
        contents = (tmp_path / "trades.log").read_text(encoding="utf-8")
        assert "open" in contents and "EURUSD" in contents
        assert "dry_run=True" in contents

    def test_refusals_are_recorded_too(self, tmp_path):
        guard = make_guard(tmp_path)
        guard.evaluate(TradeIntent(action="open", volume=99.0))
        contents = (tmp_path / "trades.log").read_text(encoding="utf-8")
        assert "BLOCKED max_lot" in contents

    def test_credentials_never_reach_the_log(self, tmp_path):
        guard = make_guard(tmp_path)
        guard.record(TradeIntent(action="open", symbol="EURUSD"),
                     {"volume": 0.1, "password": "hunter2", "login": 123456789},
                     "DRY_RUN", None)
        contents = (tmp_path / "trades.log").read_text(encoding="utf-8")
        assert "hunter2" not in contents
        assert "123456789" not in contents


class TestMasking:
    @pytest.mark.parametrize("value,expected", [
        (123456789, "***6789"),
        ("123456789", "***6789"),
        (None, "(not connected)"),
        (0, "(not connected)"),
        (42, "**"),
        (1234, "****"),
    ])
    def test_login_masking(self, value, expected):
        assert mask_login(value) == expected

    def test_settings_summary_has_no_password(self, tmp_path):
        settings = Settings(login=123456789, password="hunter2",
                            server="Broker-Demo", log_dir=str(tmp_path))
        summary = settings.safe_summary()
        assert "hunter2" not in str(summary)
        assert summary["account"] == "***6789"


class TestRequire:
    def test_require_raises_on_refusal(self, tmp_path):
        guard = make_guard(tmp_path)
        with pytest.raises(GuardBlocked) as caught:
            guard.require(TradeIntent(action="open", volume=99.0))
        assert caught.value.verdict.blocked_by == "max_lot"

    def test_require_returns_the_verdict_when_allowed(self, tmp_path):
        guard = make_guard(tmp_path)
        verdict = guard.require(TradeIntent(action="open", volume=0.1))
        assert verdict.allowed and verdict.checks_passed
