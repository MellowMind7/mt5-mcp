"""Command-line helpers used by install.bat.

Kept separate from ``server.py`` because this one is allowed to print. The
server writes to stdout only for the MCP protocol; this is a plain script.

Usage:
    python -m mt5_mcp.setup_helper find-terminal
    python -m mt5_mcp.setup_helper make-tester <terminal64.exe> <target dir>
    python -m mt5_mcp.setup_helper doctor
"""

from __future__ import annotations

import sys
from pathlib import Path


def _find_terminal() -> int:
    """Print the most plausible terminal path, or nothing."""
    from .core.connection import find_terminals
    found = find_terminals()
    if not found:
        return 1
    print(found[0].exe)
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
