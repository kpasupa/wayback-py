@echo off
title Wayback Download

pushd "%~dp0"

echo =====================================
echo  Wayback Download (resume)
echo  DO NOT CLOSE THIS WINDOW
echo  Open status.bat to monitor progress
echo =====================================
echo.

python -m wayback download --config config.yaml

echo.
echo =====================================
echo Download finished
echo =====================================
pause