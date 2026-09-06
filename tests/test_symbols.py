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


class TestCashVersusDerivatives:
    """Found against AdmiralsGroup-Demo, 2026-09-06.

    That broker offers the same index three ways: a cash CFD, an expiring
    futures CFD, and a 15-minute-delayed ETF CFD. The resolver used to hand back
    the futures or the ETF. Both are silently wrong instruments — one expires
    this quarter, the other is a different security on a delayed feed.
    """

    @pytest.mark.parametrize("query,expected", [
        ("DOW", "[DJI30]"),
        ("US30", "[DJI30]"),
        ("SP500", "[SP500]"),
        ("NAS100", "US100"),
        ("CAC", "[CAC40]"),
        ("DAX", "GERMANY40"),
        ("FTSE", "[FTSE100]"),
        ("Nikkei", "[JP225]"),
    ])
    def test_index_queries_reach_the_cash_contract(self, admirals, query, expected):
        result = admirals.resolve(query)
        assert result.best is not None, f"{query} did not resolve"
        assert result.best.record.name == expected

    def test_a_dated_contract_loses_to_its_cash_equivalent(self, admirals):
        result = admirals.resolve("DOW")
        cash = next(c for c in result.candidates if c.record.name == "[DJI30]")
        futures = next(c for c in result.candidates if c.record.name == "_YM_U6")
        assert cash.score > futures.score

    def test_the_futures_contract_is_still_offered_as_an_alternative(self, admirals):
        """Demoted, not hidden. Whoever wants the contract should find it."""
        names = [c.record.name for c in admirals.resolve("DOW").candidates]
        assert "_YM_U6" in names

    def test_the_futures_contract_is_reachable_by_name(self, admirals):
        assert admirals.resolve("_YM_U6").best.record.name == "_YM_U6"

    def test_an_etf_loses_to_the_index_it_tracks(self, admirals):
        result = admirals.resolve("SP500")
        cash = next(c for c in result.candidates if c.record.name == "[SP500]")
        etf = [c for c in result.candidates if c.record.name in ("#VUSA", "#SPY")]
        assert all(cash.score > e.score for e in etf)

    def test_etfs_are_classified_as_shares(self, admirals):
        """Regression: an "ETF CFDs" group fell through to `other` and so
        escaped the penalty that keeps shares away from index queries."""
        etf = next(r for r in admirals.records if r.name == "#VUSA")
        assert etf.kind is Kind.STOCK

    def test_expiry_is_detected_generically_not_by_name(self, admirals):
        """_YM_U6 becomes _YM_Z6 next quarter. Only expiration_time is stable."""
        dated = [r for r in admirals.records if r.expires]
        assert {r.name for r in dated} == {
            "_YM_U6", "_ES_U6", "_US100_U6", "_GER40_U6", "_FRANCE40_U6"}
        assert all(not r.expires for r in admirals.records
                   if r.name.startswith("["))

    def test_asking_for_futures_lifts_the_demotion(self, admirals):
        result = admirals.resolve("DAX futures")
        if result.best is not None:
            assert not any("dated contract" in reason
                           for reason in result.best.reasons)

    def test_bracket_and_hash_affixes_are_stripped(self, admirals):
        by_name = {r.name: r for r in admirals.records}
        assert by_name["[SP500]"].stem == "SP500"
        assert by_name["[FTSE100]"].stem == "FTSE100"
        assert by_name["#AAPL"].stem == "AAPL"

    def test_the_other_instruments_still_resolve(self, admirals):
        for query, expected in [("GOLD", "GOLD"), ("Silber", "SILVER"),
                                ("EURUSD", "EURUSD"), ("Bitcoin", "BTCUSD"),
                                ("Platin", "PLATINUM" if any(
                                    r.name == "PLATINUM" for r in admirals.records)
                                 else None)]:
            if expected is None:
                continue
            result = admirals.resolve(query)
            assert result.best is not None, f"{query} did not resolve"
            assert result.best.record.name == expected


class TestNoRegressionOnTheFirstBroker:
    """MetaQuotes-Demo has no futures tier at all, so it cannot show a
    regression by itself. These pin the earlier behaviour in place."""

    @pytest.mark.parametrize("query,expected", [
        ("GOLD", "XAUUSD"), ("DAX", "DE40"), ("DOW", "US30"),
        ("SP500", "US500"), ("NAS100", "USTEC"), ("EURUSD", "EURUSD"),
        ("Apple", "AAPL"), ("US30M", "US30M"),
    ])
    def test_still_correct(self, resolver, query, expected):
        result = resolver.resolve(query)
        assert result.best is not None, f"{query} did not resolve"
        assert result.best.record.name == expected

    def test_nothing_is_marked_as_expiring(self, resolver):
        assert not any(r.expires for r in resolver.records)
