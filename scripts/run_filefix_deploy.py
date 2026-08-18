#!/usr/bin/env python3
"""AADS-FILES(2026-08-18): 백엔드 bluegreen + 대시보드 빌드/기동을 백그라운드로 순차 실행.

SSH 도구 타임아웃(빌드 수 분)을 피하기 위해 런처가 즉시 반환하고,
진행 상황은 /root/aads/aads-server/logs/filefix_deploy.log 로 남긴다.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

LOG_DIR = pathlib.Path("/root/aads/aads-server/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG = LOG_DIR / "filefix_deploy.log"
RUNNER = LOG_DIR / "filefix_deploy_steps.sh"

STEPS = """#!/bin/bash
set -x
echo "=== [1/3] backend bluegreen $(date '+%F %T %Z') ==="
bash /root/aads/aads-server/deploy.sh bluegreen
echo "=== [2/3] dashboard build $(date '+%F %T %Z') ==="
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard
echo "=== [3/3] dashboard up $(date '+%F %T %Z') ==="
docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard
echo "=== DONE $(date '+%F %T %Z') ==="
"""

RUNNER.write_text(STEPS, encoding="utf-8")
os.chmod(RUNNER, 0o755)

with open(LOG, "ab", buffering=0) as fh:
    proc = subprocess.Popen(
        ["/bin/bash", str(RUNNER)],
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd="/root/aads/aads-server",
    )

print(f"[OK] launched pid={proc.pid} log={LOG}")
