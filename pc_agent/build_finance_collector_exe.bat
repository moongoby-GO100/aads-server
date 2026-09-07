@echo off
chcp 65001 >nul
echo ===== AADS Finance Collector EXE Build =====
echo Shinhan + IBK unified bank transaction collector
echo.

python --version 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ first.
    pause
    exit /b 1
)

echo [1/3] Installing build dependencies...
pip install pyinstaller pystray pillow pyautogui pyperclip psutil pygetwindow websockets >nul 2>&1

echo [2/3] Building AADS-Finance-Collector-Setup.exe...
python "%~dp0build_finance_collector_exe.py"

if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo [3/3] Done!
echo Output: %~dp0dist\AADS-Finance-Collector-Setup.exe
pause
