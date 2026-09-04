@echo off
setlocal EnableDelayedExpansion
title mt5-mcp installer

rem ---------------------------------------------------------------------------
rem  mt5-mcp installer
rem
rem  Does everything end to end: checks for uv and installs it if missing,
rem  fetches dependencies, finds your MetaTrader 5 terminal, builds the portable
rem  backtest copy, writes .env, registers the server with Claude Code, and
rem  finishes by running mt5_doctor so you see the result immediately.
rem
rem  Safe to run again. It never overwrites an existing .env without asking.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

echo.
echo ===========================================================================
echo   mt5-mcp installer
echo ===========================================================================
echo.

rem --- 1. uv ------------------------------------------------------------------
echo [1/7] Checking for uv...
set "UV=uv"
where uv >nul 2>&1
if errorlevel 1 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "UV=%USERPROFILE%\.local\bin\uv.exe"
        echo       found at %USERPROFILE%\.local\bin\uv.exe
    ) else (
        echo       uv is not installed. Installing it now...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 ^| iex"
        if exist "%USERPROFILE%\.local\bin\uv.exe" (
            set "UV=%USERPROFILE%\.local\bin\uv.exe"
        ) else (
            echo.
            echo   ERROR: uv could not be installed automatically.
            echo   Install it by hand from https://docs.astral.sh/uv/ and run this again.
            goto :fail
        )
    )
) else (
    echo       already installed
)

rem --- 2. dependencies --------------------------------------------------------
echo.
echo [2/7] Installing dependencies (this may take a minute)...
"%UV%" sync --quiet
if errorlevel 1 (
    echo   ERROR: dependency installation failed.
    echo   Check your internet connection and run this again.
    goto :fail
)
echo       done

rem --- 3. find the terminal ---------------------------------------------------
echo.
echo [3/7] Looking for your MetaTrader 5 terminal...
for /f "usebackq delims=" %%T in (`"%UV%" run python -m mt5_mcp.setup_helper find-terminal 2^>nul`) do set "TERMINAL=%%T"
if "%TERMINAL%"=="" (
    echo       No terminal found automatically.
    set /p TERMINAL="      Full path to terminal64.exe: "
)
if not exist "%TERMINAL%" (
    echo.
    echo   ERROR: %TERMINAL% does not exist.
    echo   Install MetaTrader 5 from https://www.metatrader5.com and run this again.
    goto :fail
)
echo       %TERMINAL%

rem --- 4. .env ----------------------------------------------------------------
echo.
echo [4/7] Configuration...
if exist ".env" (
    echo       .env already exists.
    set /p OVERWRITE="      Overwrite it? (y/N): "
    if /i not "!OVERWRITE!"=="y" (
        echo       keeping the existing .env
        goto :tester
    )
)

echo.
echo       Leave a field empty to accept the default in brackets.
echo.
set "ALLOW_LIVE=false"
set /p MAXLOT="      Largest lot size a single trade may use [1.0]: "
if "!MAXLOT!"=="" set "MAXLOT=1.0"
set /p MAXRISK="      Most percent of equity one trade may risk [2.0]: "
if "!MAXRISK!"=="" set "MAXRISK=2.0"
set /p MAXPOS="      Most positions open at once [10]: "
if "!MAXPOS!"=="" set "MAXPOS=10"
set /p DAYLOSS="      Daily loss limit, percent of equity [5.0]: "
if "!DAYLOSS!"=="" set "DAYLOSS=5.0"
echo.
echo       The terminal's own MCP server can speed up backtests and compiling.
echo       The key is in the terminal under Tools ^> Options ^> MCP.
echo       Leave empty to skip it - everything still works without it.
set /p NATIVEKEY="      MCP API key (optional): "

> ".env" echo # mt5-mcp configuration. Written by install.bat.
>> ".env" echo # This file holds credentials - it is in .gitignore and must stay there.
>> ".env" echo.
>> ".env" echo MT5_TERMINAL_PATH=%TERMINAL%
>> ".env" echo.
>> ".env" echo # Safety rail. Live trading is off by default on purpose.
>> ".env" echo MT5_ALLOW_LIVE=%ALLOW_LIVE%
>> ".env" echo MT5_MAX_LOT=%MAXLOT%
>> ".env" echo MT5_MAX_RISK_PERCENT=%MAXRISK%
>> ".env" echo MT5_MAX_OPEN_POSITIONS=%MAXPOS%
>> ".env" echo MT5_DAILY_LOSS_LIMIT_PERCENT=%DAYLOSS%
>> ".env" echo MT5_MAGIC=770001
>> ".env" echo.
>> ".env" echo # Portable copy used for backtests. Path must contain no spaces.
>> ".env" echo MT5_TESTER_TERMINAL_PATH=C:\mt5tester\terminal64.exe
>> ".env" echo MT5_TESTER_TIMEOUT_SEC=900
>> ".env" echo.
>> ".env" echo # Optional accelerator, MetaTrader build 6060 and above.
>> ".env" echo MT5_NATIVE_MCP_KEY=%NATIVEKEY%
>> ".env" echo MT5_USE_NATIVE_MCP=true
>> ".env" echo.
>> ".env" echo MT5_TIMEZONE_DISPLAY=Europe/Berlin
>> ".env" echo MT5_LOG_LEVEL=INFO
echo       .env written

:tester
rem --- 5. portable tester copy ------------------------------------------------
echo.
echo [5/7] Building the portable backtest terminal...
echo       A backtest cannot share an installation with the terminal you have
echo       open, so a copy is made at C:\mt5tester (about 160 MB).
"%UV%" run python -m mt5_mcp.setup_helper make-tester "%TERMINAL%" "C:\mt5tester"
if errorlevel 1 (
    echo       WARNING: the tester copy could not be created.
    echo       Everything except backtesting will still work.
)

rem --- 6. register with Claude Code -------------------------------------------
echo.
echo [6/7] Registering with Claude Code...
where claude >nul 2>&1
if errorlevel 1 (
    echo       The 'claude' command was not found, so this step is skipped.
    echo       Add the server by hand later with:
    echo         claude mcp add mt5 -- "%CD%\.venv\Scripts\python.exe" -m mt5_mcp.server
) else (
    call claude mcp remove mt5 >nul 2>&1
    call claude mcp add mt5 -- "%CD%\.venv\Scripts\python.exe" -m mt5_mcp.server
    if errorlevel 1 (
        echo       WARNING: registration failed. Add it by hand:
        echo         claude mcp add mt5 -- "%CD%\.venv\Scripts\python.exe" -m mt5_mcp.server
    ) else (
        echo       registered as "mt5"
    )
)

rem --- 7. diagnosis -----------------------------------------------------------
echo.
echo [7/7] Running the diagnosis...
echo.
"%UV%" run python -m mt5_mcp.setup_helper doctor
echo.
echo ===========================================================================
echo   Finished.
echo.
echo   If the diagnosis above shows any FAIL, follow the sentence next to it -
echo   each one says exactly what to do.
echo.
echo   Read INSTALL.md for the steps inside MetaTrader itself, and README.md
echo   for ten sentences you can try straight away.
echo ===========================================================================
echo.
pause
exit /b 0

:fail
echo.
echo Installation stopped.
pause
exit /b 1
