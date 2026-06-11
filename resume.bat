@echo off
title Wayback Download

pushd "%~dp0"

echo =====================================
echo Resuming download...
echo =====================================
echo.

python -m wayback download --config config.yaml

echo.
echo =====================================
echo Download finished
echo =====================================
pause