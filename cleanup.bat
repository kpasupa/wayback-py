@echo off
title Wayback Cleanup

pushd "%~dp0"

echo =====================================
echo Cleaning pages into data\clean\ ...
echo =====================================
echo.

REM Pass --force to re-clean everything (rebuilds style.css and indexes).
python -m wayback clean --config config.yaml %*

echo.
echo =====================================
echo Cleanup finished. Open data\clean\_index.html
echo =====================================
pause
