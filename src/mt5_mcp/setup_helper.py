"""Command-line helpers used by install.bat.

Kept separate from ``server.py`` because this one is allowed to print. The
server writes to stdout only for the MCP protocol; this is a plain script.

Usage:
    python -m mt5_mcp.setup_helper find-terminal
    python -m mt5_mcp.setup_helper make-tester <terminal64.exe> <target dir>
    python -m mt5_mcp.setup_helper doctor
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _account_of(data_dir: str | None) -> str:
    """Which account a terminal last used — the thing that tells them apart.

    Two MetaTrader installations look identical in a list of paths. What a
    trader actually recognises is the broker name, so it is read out of the
    terminal's own ``common.ini``. The login is shown masked.
    """
    if not data_dir:
        return ""
    from .core.connection import _read_text_any
    ini = Path(data_dir) / "config" / "common.ini"
    if not ini.is_file():
        return ""
    login = server = ""
    for line in _read_text_any(ini).splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("login="):
            login = stripped.partition("=")[2].strip()
        elif stripped.lower().startswith("server="):
            server = stripped.partition("=")[2].strip()
    if not server:
        return ""
    from .config import mask_login
    return f"{server} ({mask_login(login)})" if login else server


def _find_terminal() -> int:
    """Choose a terminal. Prints the menu to stderr, the chosen path to stdout.

    Silently taking the first hit is wrong once a machine has more than one
    installation, and machines do: a broker's terminal alongside the MetaQuotes
    one is the normal case, not the exotic one. The wrong choice is not
    obviously wrong either — it connects, it reads, and it reports somebody
    else's account.

    stdout carries nothing but the path so ``install.bat`` can still capture it
    with ``for /f``; everything a human reads goes to stderr.
    """
    from .config import get_settings
    from .core.connection import find_terminals

    found = find_terminals()

    # The portable backtest copy is ours, not an installation the user chose.
    tester = get_settings().tester_terminal_path
    if tester:
        tester_dir = os.path.normcase(str(Path(tester).parent))
        found = [t for t in found
                 if os.path.normcase(str(Path(t.exe).parent)) != tester_dir]

    if not found:
        return 1
    if len(found) == 1:
        print(found[0].exe)
        return 0

    def describe(candidate) -> str:
        bits = [f"build {candidate.build or '?'}"]
        if candidate.is_running:
            bits.append("running")
        account = _account_of(candidate.data_dir)
        if account:
            bits.append(f"last used {account}")
        return ", ".join(bits)

    print(f"\n      {len(found)} MetaTrader installations found:", file=sys.stderr)
    for index, candidate in enumerate(found, 1):
        marker = " <- suggested" if index == 1 else ""
        print(f"        [{index}] {Path(candidate.exe).parent.name}{marker}",
              file=sys.stderr)
        print(f"            {describe(candidate)}", file=sys.stderr)
        print(f"            {candidate.exe}", file=sys.stderr)
    print("      The suggestion is the newest build that is already running.",
          file=sys.stderr)
    print("      Pick the one holding the account you want to trade.",
          file=sys.stderr)

    # The prompt is written to stderr by hand. input(prompt) puts it on stdout,
    # where install.bat's `for /f` reads it as part of the path and then reports
    # that "Which one? [1-2, default 1]: C:\... does not exist".
    print(f"      Which one? [1-{len(found)}, default 1]: ",
          end="", file=sys.stderr, flush=True)
    try:
        # Strip a UTF-8 BOM as well: piped input on Windows often carries one,
        # and "\ufeff1" is not a number.
        answer = input().strip().lstrip("\ufeff").strip()
    except (EOFError, KeyboardInterrupt):
        # Non-interactive run: say what was chosen rather than choosing quietly.
        print(f"      No input available; using [1] {found[0].exe}",
              file=sys.stderr)
        print(found[0].exe)
        return 0

    choice = found[0]
    if answer:
        try:
            index = int(answer)
            if 1 <= index <= len(found):
                choice = found[index - 1]
            else:
                print(f"      {answer} is not in range; using [1].",
                      file=sys.stderr)
        except ValueError:
            print(f"      {answer!r} is not a number; using [1].", file=sys.stderr)
    print(choice.exe)
    return 0


def _make_tester(source_exe: str, target: str) -> int:
    from .core.connection import find_terminals
    from .core.tester import TesterError, prepare_tester_copy

    data_dir = None
    for candidate in find_terminals(source_exe):
        if Path(candidate.exe).samefile(source_exe) if Path(candidate.exe).exists() \
                else False:
            data_dir = candidate.data_dir
            break
    if data_dir is None:
        found = find_terminals(source_exe)
        data_dir = found[0].data_dir if found else None
    if not data_dir:
        print("      Could not locate the terminal's data folder.")
        print("      Start MetaTrader 5 once, then run this installer again.")
        return 1

    try:
        result = prepare_tester_copy(source_exe, data_dir, target)
    except TesterError as exc:
        print(f"      {exc}")
        if exc.fix:
            print(f"      {exc.fix}")
        return 1
    except OSError as exc:
        print(f"      Could not copy the tester: {exc}")
        return 1

    print(f"      copied {len(result['copied'])} item(s), "
          f"{result['size_mb']} MB at {result['target']}")
    print("      The first backtest downloads price history and takes longer "
          "than later ones.")
    return 0


def _doctor() -> int:
    """Run mt5_doctor and print it as a table."""
    import asyncio

    from .server import build_server

    async def run() -> int:
        server = build_server()
        try:
            await server.call_tool("mt5_connect", {})
        except Exception:  # noqa: BLE001 - the doctor explains it better
            pass
        result = await server.call_tool("mt5_doctor", {"deep": False})
        if result.is_error:
            text = "".join(getattr(c, "text", "") for c in result.content)
            print(f"  The diagnosis itself failed: {text[:400]}")
            return 1
        report = result.structured_content
        symbols = {"ok": "  OK  ", "warn": " WARN ", "fail": " FAIL "}
        for check in report["checks"]:
            print(f"  [{symbols.get(check['status'], '  ?   ')}] "
                  f"{check['name']:<22} {check['detail']}")
            if check.get("fix"):
                print(f"            -> {check['fix']}")
        print()
        print(f"  Overall: {report['overall'].upper()} - {report['summary']}")
        print(f"  Backtest route: {report['backtest_path']}")
        return 0 if report["overall"] != "fail" else 1

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print(__doc__)
        return 2
    command, rest = args[0], args[1:]
    if command == "find-terminal":
        return _find_terminal()
    if command == "make-tester":
        if len(rest) < 2:
            print("usage: make-tester <terminal64.exe> <target dir>")
            return 2
        return _make_tester(rest[0], rest[1])
    if command == "doctor":
        return _doctor()
    print(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
