"""Build the AADS Shinhan Collector Windows setup executable.

Run this on Windows:
    python build_shinhan_collector_exe.py

Output:
    pc_agent/dist/AADS-Shinhan-Collector-Setup.exe
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ENTRYPOINT = SCRIPT_DIR / "shinhan_collector_launcher.py"
DIST_DIR = SCRIPT_DIR / "dist"
BUILD_DIR = SCRIPT_DIR / "build_tmp" / "shinhan_collector"
SPEC_DIR = SCRIPT_DIR / "build_tmp"
ICON_FILE = SCRIPT_DIR / "icon.ico"
EXE_NAME = "AADS-Shinhan-Collector-Setup"


def build_command() -> list[str]:
    """Return the PyInstaller command without executing it."""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--name",
        EXE_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(SPEC_DIR),
        "--add-data",
        f"{SCRIPT_DIR / 'launcher.py'}{os.pathsep}.",
        "--add-data",
        f"{SCRIPT_DIR / 'agent.py'}{os.pathsep}.",
        "--add-data",
        f"{SCRIPT_DIR / 'updater.py'}{os.pathsep}.",
        "--add-data",
        f"{SCRIPT_DIR / 'tray.py'}{os.pathsep}.",
        "--add-data",
        f"{SCRIPT_DIR / 'VERSION'}{os.pathsep}.",
        "--add-data",
        f"{SCRIPT_DIR / 'commands'}{os.pathsep}commands",
        "--hidden-import",
        "tkinter",
        "--hidden-import",
        "pystray",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "PIL.Image",
        "--hidden-import",
        "PIL.ImageDraw",
        "--hidden-import",
        "websockets",
        "--hidden-import",
        "websockets.legacy",
        "--hidden-import",
        "websockets.legacy.client",
        "--hidden-import",
        "asyncio",
        "--hidden-import",
        "json",
        "--hidden-import",
        "hashlib",
        "--hidden-import",
        "logging",
        "--hidden-import",
        "pyautogui",
        "--hidden-import",
        "pyperclip",
        "--hidden-import",
        "psutil",
        "--hidden-import",
        "PIL.ImageGrab",
        "--hidden-import",
        "pyscreeze",
        "--hidden-import",
        "pygetwindow",
        "--collect-all",
        "pyautogui",
        "--collect-all",
        "psutil",
    ]
    if ICON_FILE.exists():
        cmd.extend(["--icon", str(ICON_FILE)])
    cmd.append(str(ENTRYPOINT))
    return cmd


def build() -> None:
    """Build the Windows setup executable."""
    if sys.platform != "win32":
        raise SystemExit("Windows EXE packaging must run on Windows with PyInstaller.")
    print(f"[BUILD] Starting: {EXE_NAME}.exe")
    cmd = build_command()
    print(f"[BUILD] Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    exe_path = DIST_DIR / f"{EXE_NAME}.exe"
    print(f"[BUILD] Success: {exe_path}")
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"[BUILD] Size: {size_mb:.1f} MB")


if __name__ == "__main__":
    build()
