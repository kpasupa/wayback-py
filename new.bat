@echo off
title Wayback Scraper

REM Run from this script's folder
pushd "%~dp0"

echo =====================================
echo Starting Wayback scraper
echo =====================================
echo.

python -m wayback run --config config.yaml

echo.
echo =====================================
echo Finished
echo =====================================
pause