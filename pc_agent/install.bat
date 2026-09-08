@echo off
setlocal EnableExtensions
chcp 65001 >nul
title AADS PC Agent - 설치

set "SOURCE_DIR=%~dp0"
set "INSTALL_DIR=%LOCALAPPDATA%\AADS\PC-Agent"
set "LOG_FILE=%TEMP%\AADS-PC-Agent-install.log"

echo ========================================
echo   AADS PC Agent 설치 프로그램
echo ========================================
echo 설치 로그: %LOG_FILE%
echo.

echo [%date% %time%] install start source=%SOURCE_DIR% > "%LOG_FILE%"

:: 압축을 푼 임시/다운로드 폴더가 아니라 사용자별 안정 경로에 설치한다.
echo [1/5] 설치 파일을 안정 경로로 복사 중...
if /I "%SOURCE_DIR:~0,-1%"=="%INSTALL_DIR%" goto files_ready
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%" >> "%LOG_FILE%" 2>&1
robocopy "%SOURCE_DIR%" "%INSTALL_DIR%" /E /R:2 /W:1 /XD ".venv" "__pycache__" "build_tmp" "dist" /XF "*.pyc" "*.pyo" >> "%LOG_FILE%" 2>&1
if errorlevel 8 (
    echo [오류] 설치 파일 복사 실패. 로그를 확인하세요: %LOG_FILE%
    pause
    exit /b 1
)
:files_ready
set "SCRIPT_DIR=%INSTALL_DIR%\"
cd /d "%INSTALL_DIR%"
echo [OK] 설치 경로: %INSTALL_DIR%
echo.

:: py launcher와 python.exe를 모두 지원하고 Python 3.10 이상만 선택한다.
echo [2/5] Python 3.10 이상 확인 중...
set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

:: Python이 없으면 Windows 기본 패키지 관리자로 자동 설치 후 즉시 재탐색한다.
if not defined PYTHON_CMD (
    echo [안내] Python이 없어 Python 3.11 자동 설치를 시도합니다.
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [오류] Python과 winget이 모두 없습니다.
        echo Microsoft Store에서 Python 3.11을 설치한 뒤 install.bat을 다시 실행하세요.
        echo [ERROR] python and winget missing >> "%LOG_FILE%"
        pause
        exit /b 1
    )
    winget install --id Python.Python.3.11 --exact --silent --accept-package-agreements --accept-source-agreements >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [오류] Python 자동 설치에 실패했습니다. 로그를 확인하세요: %LOG_FILE%
        pause
        exit /b 1
    )
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    if not defined PYTHON_CMD (
        where py >nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=py -3"
    )
)
if not defined PYTHON_CMD (
    echo [오류] Python 설치 후 실행 파일을 찾지 못했습니다. Windows 로그아웃 후 다시 실행하세요.
    echo [ERROR] python executable not found after install >> "%LOG_FILE%"
    pause
    exit /b 1
)
echo [OK] Python: %PYTHON_CMD%
echo.

echo [3/5] 전용 가상환경 생성 중...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [오류] 가상환경 생성 실패. 로그를 확인하세요: %LOG_FILE%
        pause
        exit /b 1
    )
)
echo [OK] 가상환경 준비 완료
echo.

echo [4/5] 필수 패키지 설치 중...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [오류] pip 준비 실패. 로그를 확인하세요: %LOG_FILE%
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [오류] 패키지 설치 실패. 인터넷 연결과 로그를 확인하세요: %LOG_FILE%
    pause
    exit /b 1
)
echo [OK] 필수 패키지 설치 완료
echo.

echo [5/5] 바로가기와 자동실행 등록 중...
set "DESKTOP=%USERPROFILE%\Desktop"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%DESKTOP%\AADS PC Agent.lnk'); $sc.TargetPath = '%SCRIPT_DIR%run.bat'; $sc.WorkingDirectory = '%INSTALL_DIR%'; $sc.Description = 'AADS PC Agent'; $sc.Save()" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [경고] 바탕화면 바로가기 생성 실패. 설치는 계속합니다.
) else (
    echo [OK] 바탕화면 바로가기 생성
)

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if not exist "%STARTUP%" mkdir "%STARTUP%" >> "%LOG_FILE%" 2>&1
(
    echo @echo off
    echo cd /d "%INSTALL_DIR%"
    echo start "" "%SCRIPT_DIR%run.bat"
) > "%STARTUP%\AADS-PC-Agent-Autostart.cmd"
if errorlevel 1 (
    echo [경고] 자동실행 등록 실패. 바탕화면 바로가기로 직접 실행할 수 있습니다.
) else (
    echo [OK] Windows 로그온 자동실행 등록
)
echo.

echo [시작] PC Agent 런처 실행 중...
if exist "%SCRIPT_DIR%.venv\Scripts\pythonw.exe" (
    start "" "%SCRIPT_DIR%.venv\Scripts\pythonw.exe" "%SCRIPT_DIR%launcher.py"
) else (
    start "" "%SCRIPT_DIR%.venv\Scripts\python.exe" "%SCRIPT_DIR%launcher.py"
)
echo [%date% %time%] install complete >> "%LOG_FILE%"
echo.

echo ========================================
echo   설치 완료!
echo   설치 경로: %INSTALL_DIR%
echo   PC Agent를 시작했으며 다음 로그인부터 자동 실행됩니다.
echo ========================================
pause
exit /b 0
