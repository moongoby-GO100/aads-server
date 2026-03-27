@echo off
chcp 65001 >nul
title AADS PC Agent
echo [AADS PC Agent] 시작합니다.
echo 종료하려면 이 창을 닫으세요.
echo.

cd /d %~dp0

:: 가상환경이 있으면 사용
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    echo [OK] 가상환경 사용
) else (
    set "PYTHON=python"
    echo [!] 시스템 Python 사용 (install.bat 실행 권장)
)

:loop
echo [%date% %time%] 에이전트 시작...
%PYTHON% agent.py
echo [%date% %time%] 에이전트 종료됨. 3초 후 재시작...
timeout /t 3 /nobreak >nul
goto loop
