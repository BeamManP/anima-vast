@echo off
chcp 65001 >nul 2>&1
title Anima WebUI - Quick Launch
cd /d "%~dp0"
python -u run_anima.py
echo.
echo   Bye!
timeout /t 3 >nul
