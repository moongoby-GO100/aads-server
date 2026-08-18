#!/usr/bin/env python3
"""스테일 next build 락 해제 후, 대시보드 빌드를 detached 백그라운드로 실행한다."""
import os
import shutil
import subprocess
from pathlib import Path

LOCK = Path("/root/aads/aads-dashboard/.next/lock")
LOG = "/tmp/aads_dash_build_p0.log"

# 실행 중 next build 프로세스가 없을 때만 락 제거
running = subprocess.run(["pgrep", "-f", "next build"], capture_output=True, text=True).stdout.strip()
if running:
    print("BUILD_ALREADY_RUNNING", running)
    raise SystemExit(1)

if LOCK.exists():
    if LOCK.is_dir():
        shutil.rmtree(LOCK)
    else:
        os.unlink(LOCK)
    print("stale lock removed:", LOCK)

with open(LOG, "w", encoding="utf-8") as fh:
    p = subprocess.Popen(
        ["npm", "run", "build"],
        cwd="/root/aads/aads-dashboard",
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print("started pid", p.pid, "log", LOG)
