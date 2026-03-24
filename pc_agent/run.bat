@echo off
title AADS PC Agent (Auto-Restart)
echo [AADS PC Agent] 자동 재시작 모드로 실행합니다.
echo 종료하려면 이 창을 닫으세요.
echo.

:loop
echo [%date% %time%] 에이전트 시작...
python agent.py
echo [%date% %time%] 에이전트 종료됨. 3초 후 재시작...
timeout /t 3 /nobreak >nul
cd /d %~dp0
git pull --ff-only 2>nul
goto loop
