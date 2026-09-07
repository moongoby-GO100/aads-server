@echo off
chcp 65001 >nul
title AADS Shinhan Collector - EXE Build
echo ========================================
echo   AADS Shinhan Collector EXE Build
echo ========================================
echo.

cd /d %~dp0

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    pause
    exit /b 1
)

python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Failed to install build requirements.
    pause
    exit /b 1
)

python build_shinhan_collector_exe.py
if errorlevel 1 (
    echo [ERROR] EXE build failed.
    pause
    exit /b 1
)

echo.
echo [OK] dist\AADS-Shinhan-Collector-Setup.exe created.
pause
