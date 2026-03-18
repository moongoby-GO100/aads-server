#!/usr/bin/env python3
"""대시보드 빌드를 백그라운드로 실행 (즉시 리턴)"""
import subprocess, os
# 자식 프로세스로 실제 빌드 스크립트 실행 (부모는 즉시 종료)
pid = os.fork()
if pid > 0:
    print("Build launched in background (pid=%d)" % pid)
    print("Check progress: cat /tmp/dashboard_build.log")
else:
    os.setsid()
    with open("/tmp/dashboard_build.log", "w") as log:
        for cmd in [
            "docker compose -f /root/aads/aads-dashboard/docker-compose.yml build --no-cache",
            "docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d",
        ]:
            log.write("[RUN] %s\n" % cmd)
            log.flush()
            subprocess.call(cmd.split(), stdout=log, stderr=log)
        log.write("[DONE]\n")
    os._exit(0)
