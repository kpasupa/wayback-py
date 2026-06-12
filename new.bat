@echo off
title Wayback Scraper

REM Run from this script's folder
pushd "%~dp0"

echo =====================================
echo  Wayback Scraper
echo  DO NOT CLOSE THIS WINDOW
echo  Open status.bat to monitor progress
echo =====================================
echo.

python -m wayback run --config config.yaml

echo.
echo =====================================
echo Finished
echo =====================================
pause