@echo off
REM Realtime Wayback scraper status. Double-click or run: status.bat [seconds]
REM Optional arg = refresh interval in seconds (default 3). Ctrl+C to quit.

setlocal
set "INTERVAL=%~1"
if "%INTERVAL%"=="" set "INTERVAL=3"

REM Run from this script's own folder so config.yaml resolves.
pushd "%~dp0"

:loop
cls
echo  Wayback scraper - live status   ^(refresh %INTERVAL%s, Ctrl+C to quit^)
echo  =====================================================================
python -m wayback status --config config.yaml
echo.
echo  Updated: %TIME%
timeout /t %INTERVAL% /nobreak >nul
goto loop
