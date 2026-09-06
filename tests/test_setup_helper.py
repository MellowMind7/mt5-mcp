"""Choosing between several MetaTrader installations.

A machine with two terminals is the normal case, not the exotic one: a broker's
build sits next to the MetaQuotes one. Picking the first silently is the wrong
kind of wrong — the server connects, reads happily, and reports somebody else's
account.

The contract these tests pin down: stdout carries the chosen path and nothing
else, so ``install.bat`` can still capture it with ``for /f``; everything a
human reads goes to stderr.
"""

from __future__ import annotations

import pytest

from mt5_mcp.core.connection import TerminalCandidate
from mt5_mcp.setup_helper import _find_terminal


def candidate(name: str, build: int, running: bool = True) -> TerminalCandidate:
    return TerminalCandidate(
        exe=rf"C:\Program Files\{name}\terminal64.exe",
        data_dir=rf"C:\data\{name}", build=build, company="MetaQuotes Ltd.",
        is_running=running)


MQ = candidate("MetaTrader 5", 6182)
FTMO = candidate("FTMO Global Markets MT5 Terminal", 5833)


@pytest.fixture
def install(monkeypatch):
    """Install a fake set of found terminals and a fake account reader."""
    def apply(found, accounts=None, tester_path=None):
        monkeypatch.setattr("mt5_mcp.core.connection.find_terminals",
                            lambda *a, **k: list(found))
        monkeypatch.setattr("mt5_mcp.setup_helper._account_of",
                            lambda data_dir: (accounts or {}).get(data_dir, ""))
        from mt5_mcp.config import Settings
        monkeypatch.setattr("mt5_mcp.config.get_settings",
                            lambda: Settings(tester_terminal_path=tester_path))
    return apply


class TestSingleInstallation:
    def test_one_terminal_is_printed_without_asking(self, install, capsys):
        install([MQ])
        assert _find_terminal() == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == MQ.exe
        assert captured.err == ""

    def test_no_terminal_returns_failure_and_prints_nothing(self, install, capsys):
        install([])
        assert _find_terminal() == 1
        assert capsys.readouterr().out.strip() == ""


class TestSeveralInstallations:
    def test_the_choice_is_offered_not_taken(self, install, capsys, monkeypatch):
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "2")
        assert _find_terminal() == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == FTMO.exe
        assert "2 MetaTrader installations found" in captured.err

    def test_stdout_carries_only_the_path(self, install, capsys, monkeypatch):
        """install.bat reads stdout with `for /f`; a menu there would break it."""
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "1")
        _find_terminal()
        captured = capsys.readouterr()
        assert captured.out.strip().splitlines() == [MQ.exe]

    def test_the_prompt_goes_to_stderr_not_stdout(self, install, capsys,
                                                  monkeypatch):
        """Regression, caught by running the real installer.

        input(prompt) writes its prompt to stdout. install.bat captured it as
        part of the path and reported that
        "Which one? [1-2, default 1]: C:/... does not exist".
        """
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "1")
        _find_terminal()
        captured = capsys.readouterr()
        assert "Which one?" in captured.err
        assert "Which one?" not in captured.out

    def test_a_byte_order_mark_is_tolerated(self, install, capsys, monkeypatch):
        """Piped input on Windows arrives as "﻿1", which is not a number."""
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "﻿2")
        _find_terminal()
        captured = capsys.readouterr()
        assert captured.out.strip() == FTMO.exe
        assert "not a number" not in captured.err

    def test_the_menu_names_the_account_each_one_holds(self, install, capsys,
                                                       monkeypatch):
        """Two paths look alike; the broker name is what a trader recognises."""
        install([MQ, FTMO], accounts={
            MQ.data_dir: "AdmiralsGroup-Demo (***4321)",
            FTMO.data_dir: "FTMO-Demo (***2109)"})
        monkeypatch.setattr("builtins.input", lambda: "1")
        _find_terminal()
        err = capsys.readouterr().err
        assert "AdmiralsGroup-Demo (***4321)" in err
        assert "FTMO-Demo (***2109)" in err
        assert "build 6182" in err and "build 5833" in err

    def test_empty_answer_takes_the_suggestion(self, install, capsys, monkeypatch):
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "")
        _find_terminal()
        assert capsys.readouterr().out.strip() == MQ.exe

    def test_out_of_range_answer_falls_back_and_says_so(self, install, capsys,
                                                        monkeypatch):
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "9")
        _find_terminal()
        captured = capsys.readouterr()
        assert captured.out.strip() == MQ.exe
        assert "not in range" in captured.err

    def test_nonsense_answer_falls_back_and_says_so(self, install, capsys,
                                                    monkeypatch):
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", lambda: "banana")
        _find_terminal()
        captured = capsys.readouterr()
        assert captured.out.strip() == MQ.exe
        assert "not a number" in captured.err

    def test_no_console_still_answers_and_announces_the_default(self, install,
                                                                capsys,
                                                                monkeypatch):
        """Piped or unattended runs must not hang, and must not choose quietly."""
        def no_input():
            raise EOFError
        install([MQ, FTMO])
        monkeypatch.setattr("builtins.input", no_input)
        assert _find_terminal() == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == MQ.exe
        assert "No input available" in captured.err


class TestTesterCopyIsHidden:
    def test_the_portable_backtest_copy_is_not_offered(self, install, capsys):
        """C:\\mt5tester is ours, not an installation the user chose."""
        tester = TerminalCandidate(
            exe=r"C:\mt5tester\terminal64.exe", data_dir=r"C:\mt5tester",
            build=6182, company="MetaQuotes Ltd.", is_running=False)
        install([MQ, tester], tester_path=r"C:\mt5tester\terminal64.exe")
        assert _find_terminal() == 0
        captured = capsys.readouterr()
        # Only one real installation is left, so no menu appears at all.
        assert captured.out.strip() == MQ.exe
        assert captured.err == ""
