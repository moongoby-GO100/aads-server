@echo off
chcp 65001 >nul
title AADS PC Agent - 설치
echo ========================================
echo   AADS PC Agent 설치 프로그램
echo ========================================
echo.

cd /d %~dp0

:: 1. Python 확인
echo [1/4] Python 확인 중...
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo https://python.org 에서 Python 3.10 이상을 설치하세요.
    echo 설치 시 "Add Python to PATH" 체크 필수!
    pause
    exit /b 1
)
echo [OK] Python 발견
echo.

:: 2. venv 생성
echo [2/4] 가상환경 생성 중...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo [오류] 가상환경 생성 실패
        pause
        exit /b 1
    )
    echo [OK] 가상환경 생성 완료
) else (
    echo [OK] 기존 가상환경 사용
)
echo.

:: 3. 패키지 설치
echo [3/4] 필수 패키지 설치 중...
.venv\Scripts\pip install -r requirements.txt -q
if errorlevel 1 (
    echo [오류] 패키지 설치 실패
    pause
    exit /b 1
)
echo [OK] 패키지 설치 완료
echo.

:: 4. 바탕화면 바로가기
echo [4/4] 바탕화면 바로가기 생성 중...
set "SCRIPT_DIR=%~dp0"
set "DESKTOP=%USERPROFILE%\Desktop"
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%DESKTOP%\AADS PC Agent.lnk'); $sc.TargetPath = '%SCRIPT_DIR%run.bat'; $sc.WorkingDirectory = '%SCRIPT_DIR%'; $sc.Description = 'AADS PC Agent'; $sc.Save()"
if errorlevel 1 (
    echo [경고] 바로가기 생성 실패 - run.bat을 직접 실행하세요
) else (
    echo [OK] 바탕화면에 "AADS PC Agent" 바로가기 생성
)
echo.

echo ========================================
echo   설치 완료!
echo   바탕화면의 "AADS PC Agent"를 실행하거나
echo   run.bat을 직접 실행하세요.
echo ========================================
pause
