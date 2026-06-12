@echo off
title Wayback Cleanup

pushd "%~dp0"

echo =====================================
echo  Wayback Cleanup (re-clean)
echo  DO NOT CLOSE THIS WINDOW
echo  Open status.bat to monitor progress
echo =====================================
echo.

REM Pass --force to re-clean everything (rebuilds style.css and indexes).
python -m wayback clean --config config.yaml %*

echo.
echo =====================================
echo Cleanup finished.
echo =====================================
pause
