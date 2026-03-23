"""
AADS-195 Phase 3: PyInstaller EXE 빌드 스크립트.
단일 EXE 파일로 PC Agent를 빌드한다.

사용법:
    python build.py

필요 패키지:
    pip install pyinstaller websockets pyautogui pyperclip pywin32 Pillow psutil
"""
from __future__ import annotations

import os
import subprocess
import sys


def build() -> None:
    """PyInstaller로 단일 EXE 빌드."""
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    agent_py = os.path.join(agent_dir, "agent.py")
    commands_dir = os.path.join(agent_dir, "commands")
    icon_path = os.path.join(agent_dir, "icon.ico")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "AADSAgent",
        "--console",  # 콘솔 창 표시 (로그 확인용)
        # 명령 모듈 포함
        "--add-data", f"{commands_dir}{os.pathsep}commands",
    ]

    # 아이콘 파일 있으면 적용
    if os.path.exists(icon_path):
        cmd.extend(["--icon", icon_path])

    # Hidden imports (Windows 전용 모듈)
    hidden_imports = [
        "websockets",
        "pyautogui",
        "pyperclip",
        "win32gui",
        "win32con",
        "PIL",
        "PIL.ImageGrab",
        "psutil",
    ]
    for mod in hidden_imports:
        cmd.extend(["--hidden-import", mod])

    cmd.append(agent_py)

    print(f"빌드 시작: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=agent_dir)

    if result.returncode == 0:
        exe_path = os.path.join(agent_dir, "dist", "AADSAgent.exe")
        print(f"\n빌드 완료: {exe_path}")
        print("실행: AADSAgent.exe")
        print("환경변수 설정 후 실행:")
        print("  set AADS_SERVER_URL=wss://aads.newtalk.kr/api/v1/ws/pc-agent")
        print("  set PC_AGENT_SECRET=<시크릿>")
        print("  AADSAgent.exe")
    else:
        print(f"\n빌드 실패 (exit code: {result.returncode})")
        sys.exit(1)


if __name__ == "__main__":
    build()
