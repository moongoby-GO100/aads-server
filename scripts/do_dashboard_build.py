#!/usr/bin/env python3
"""대시보드 Docker 이미지 재빌드 스크립트 (백그라운드 실행, 로그 파일 기록)"""
import subprocess, sys, os, datetime

LOG = "/tmp/dashboard_build.log"

with open(LOG, "w") as f:
    f.write("[%s] Dashboard build started\n" % datetime.datetime.now().isoformat())

cmds = [
    ["docker", "compose", "-f", "/root/aads/aads-dashboard/docker-compose.yml", "build", "--no-cache"],
    ["docker", "compose", "-f", "/root/aads/aads-dashboard/docker-compose.yml", "up", "-d"],
]

with open(LOG, "a") as f:
    for cmd in cmds:
        f.write("[RUN] %s\n" % " ".join(cmd))
        f.flush()
        r = subprocess.run(cmd, stdout=f, stderr=f, timeout=600)
        if r.returncode != 0:
            f.write("[ERROR] exit=%d\n" % r.returncode)
            sys.exit(1)
    f.write("[DONE] Dashboard rebuild complete at %s\n" % datetime.datetime.now().isoformat())
