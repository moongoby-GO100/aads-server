"""KakaoBot SaaS — PyInstaller 빌드 스크립트.

launcher.py → kakaobot-setup.exe (onefile, windowed).
실행: python build_exe.py → dist/kakaobot-setup.exe
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
    """PyInstaller로 런처 EXE 빌드."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", EXE_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(SCRIPT_DIR / "build_tmp"),
        "--specpath", str(SCRIPT_DIR / "build_tmp"),
        # 런처에 필요한 모듈 포함
        "--add-data", f"{SCRIPT_DIR / 'updater.py'}{os.pathsep}.",
        "--add-data", f"{SCRIPT_DIR / 'tray.py'}{os.pathsep}.",
        "--add-data", f"{SCRIPT_DIR / 'VERSION'}{os.pathsep}.",
        # Hidden imports
        "--hidden-import", "tkinter",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
    ]

    # 아이콘 파일이 있으면 적용
    if ICON_FILE.exists():
        cmd.extend(["--icon", str(ICON_FILE)])

    # 런처 스크립트
    cmd.append(str(LAUNCHER))

    print(f"빌드 시작: {EXE_NAME}.exe")
    print(f"명령: {' '.join(cmd)}")

    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    if result.returncode == 0:
        exe_path = DIST_DIR / f"{EXE_NAME}.exe"
        print(f"\n빌드 성공: {exe_path}")
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print(f"파일 크기: {size_mb:.1f} MB")
    else:
        print(f"\n빌드 실패 (코드 {result.returncode})")
        sys.exit(1)


if __name__ == "__main__":
    build()
