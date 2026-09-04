"""Symbol resolution — the part that decides which market you actually trade."""

from __future__ import annotations

import pytest

from mt5_mcp.core.symbols import Kind, _classify, _stem


class TestStem:
    """Broker affixes come off; real names stay intact."""

    @pytest.mark.parametrize("name,expected", [
        ("EURUSD", "EURUSD"),
        ("EURUSD.raw", "EURUSD"),
        ("EURUSD.ecn", "EURUSD"),
        ("EURUSDm", "EURUSD"),
        ("EURUSD_i", "EURUSD"),
        ("EURUSD#", "EURUSD"),
        ("EURUSD.cash", "EURUSD"),
    ])
    def test_affixes_are_stripped(self, name, expected):
        assert _stem(name) == expected

    @pytest.mark.parametrize("name", ["USTEC", "US30", "WTI", "DE40", "UK100"])
    def test_short_names_survive(self, name):
        """Regression: single-letter stripping ate USTEC down to UST.

        A trailing letter is only an affix when enough name is left to still be
        a symbol; without that floor the Nasdaq index became unfindable.
        """
        assert _stem(name) == name


class TestClassify:
    """Instrument class comes from trade_calc_mode, not from folder names."""

    def test_forex_calc_mode_zero_is_not_falsy(self):
        """Regression: `getattr(s, "trade_calc_mode", -1) or -1` turned the
        FOREX mode (0) into -1 and made every currency pair "other"."""
        assert _classify(0, "EUR", "USD", "Forex\\EURUSD") is Kind.FOREX

    def test_metal_by_base_currency(self):
        assert _classify(4, "XAU", "USD", "Metals\\XAUUSD") is Kind.METAL

    def test_share_is_a_share_even_when_named_gold(self):
        assert _classify(32, "USD", "USD", "Nasdaq\\Stock\\GOLD") is Kind.STOCK

    def test_index_by_folder_when_calc_mode_is_generic_cfd(self):
        assert _classify(2, "USD", "EUR", "Indexes\\DE40") is Kind.INDEX

    def test_crypto_by_base_currency(self):
        assert _classify(4, "BTC", "USD", "Crypto\\BTCUSD") is Kind.CRYPTO


class TestResolveTraps:
    """The reason this module exists."""

    def test_gold_resolves_to_the_metal_not_the_mining_share(self, resolver):
        result = resolver.resolve("GOLD")
        assert result.best.record.name == "XAUUSD"
        assert result.best.record.kind is Kind.METAL

    def test_gold_wins_against_an_exact_name_match(self, resolver):
        """An exact hit on a share must lose to a description hit on the metal."""
        result = resolver.resolve("GOLD")
        share = next(c for c in result.candidates if c.record.name == "GOLD")
        metal = next(c for c in result.candidates if c.record.name == "XAUUSD")
        assert metal.score > share.score

    def test_dax_resolves_to_the_index_not_the_etf(self, resolver):
        assert resolver.resolve("DAX").best.record.name == "DE40"

    def test_dow_resolves_to_the_index_not_dow_inc(self, resolver):
        assert resolver.resolve("DOW").best.record.name == "US30"

    def test_apple_still_resolves_to_the_share(self, resolver):
        """Class awareness must not break the case where a share is correct."""
        assert resolver.resolve("Apple").best.record.name == "AAPL"


class TestResolveEveryday:
    @pytest.mark.parametrize("query,expected", [
        ("EURUSD", "EURUSD"),
        ("eur/usd", "EURUSD"),
        ("EUR-USD", "EURUSD"),
        ("eurusd", "EURUSD"),
        ("USDJPY", "USDJPY"),
        ("Silber", "XAGUSD"),
        ("silver", "XAGUSD"),
        ("SP500", "US500"),
        ("NAS100", "USTEC"),
        ("nasdaq", "USTEC"),
    ])
    def test_common_names(self, resolver, query, expected):
        result = resolver.resolve(query)
        assert result.best is not None, f"{query} did not resolve"
        assert result.best.record.name == expected

    def test_full_contract_beats_the_mini(self, resolver):
        """US30 and US30M are both "the Dow"; the full one is meant."""
        assert resolver.resolve("DOW").best.record.name == "US30"

    def test_asking_for_the_mini_gets_the_mini(self, resolver):
        result = resolver.resolve("US30M")
        assert result.best.record.name == "US30M"

    def test_unknown_query_returns_nothing_rather_than_a_bad_guess(self, resolver):
        result = resolver.resolve("Erdgas")
        assert result.best is None
        assert "No symbol found" in result.note

    def test_short_query_does_not_match_a_random_share(self, resolver):
        """Regression: "Öl" flattens to "L" and matched a one-letter ticker."""
        result = resolver.resolve("Öl")
        assert result.best is None or result.best.record.kind is not Kind.STOCK


class TestNamingScheme:
    def test_reports_affixes_and_examples(self, resolver):
        scheme = resolver.naming_scheme()
        assert scheme["currency_pairs_total"] == 3
        assert scheme["how_they_are_named_here"]["gold"] == "XAUUSD"
        assert scheme["how_they_are_named_here"]["dax"] == "DE40"


class TestSearch:
    def test_finds_by_description(self, resolver):
        hits = resolver.search("germany")
        assert any(h.name == "DE40" for h in hits)

    def test_kind_filter(self, resolver):
        hits = resolver.search("us", kind=Kind.INDEX)
        assert hits and all(h.kind is Kind.INDEX for h in hits)
