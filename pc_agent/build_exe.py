"""KakaoBot SaaS - PyInstaller build script.

launcher.py -> kakaobot-setup.exe (onefile, windowed).
Usage: python build_exe.py -> dist/kakaobot-setup.exe
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LAUNCHER = SCRIPT_DIR / "launcher.py"
DIST_DIR = SCRIPT_DIR / "dist"
ICON_FILE = SCRIPT_DIR / "icon.ico"

EXE_NAME = "kakaobot-setup"


def build() -> None:
    """Build launcher EXE with PyInstaller."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", EXE_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(SCRIPT_DIR / "build_tmp"),
        "--specpath", str(SCRIPT_DIR / "build_tmp"),
        "--add-data", f"{SCRIPT_DIR / 'updater.py'}{os.pathsep}.",
        "--add-data", f"{SCRIPT_DIR / 'tray.py'}{os.pathsep}.",
        "--add-data", f"{SCRIPT_DIR / 'VERSION'}{os.pathsep}.",
        "--hidden-import", "tkinter",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "websockets",
        "--hidden-import", "websockets.legacy",
        "--hidden-import", "websockets.legacy.client",
        "--hidden-import", "asyncio",
        "--hidden-import", "json",
        "--hidden-import", "hashlib",
        "--hidden-import", "logging",
        # agent.py + commands 의존성
        "--hidden-import", "pyautogui",
        "--hidden-import", "pyperclip",
        "--hidden-import", "psutil",
        "--hidden-import", "PIL.ImageGrab",
        "--hidden-import", "pyscreeze",
        "--hidden-import", "pygetwindow",
        "--collect-all", "pyautogui",
        "--collect-all", "psutil",
    ]

    if ICON_FILE.exists():
        cmd.extend(["--icon", str(ICON_FILE)])

    cmd.append(str(LAUNCHER))

    print(f"[BUILD] Starting: {EXE_NAME}.exe")
    print(f"[BUILD] Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    if result.returncode == 0:
        exe_path = DIST_DIR / f"{EXE_NAME}.exe"
        print(f"\n[BUILD] Success: {exe_path}")
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print(f"[BUILD] Size: {size_mb:.1f} MB")
    else:
        print(f"\n[BUILD] Failed (code {result.returncode})")
        sys.exit(1)


if __name__ == "__main__":
    build()
