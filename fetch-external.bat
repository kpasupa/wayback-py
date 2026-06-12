@echo off
title Wayback Fetch External

pushd "%~dp0"

echo =====================================
echo  Wayback Fetch External assets/iframes
echo  DO NOT CLOSE THIS WINDOW
echo  Open status.bat to monitor progress
echo =====================================
echo.

python -m wayback fetch-external --config config.yaml

echo.
echo =====================================
echo Fetch external finished
echo =====================================
pause
