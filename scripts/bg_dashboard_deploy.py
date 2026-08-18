#!/usr/bin/env python3
"""대시보드 컨테이너만 재빌드/재기동 (R-DOCKER 준수: --no-deps, 단일 서비스)."""
import subprocess

LOG = "/tmp/aads_dash_deploy_p0.log"
COMPOSE = "/root/aads/aads-dashboard/docker-compose.yml"

script = (
    f"docker compose -f {COMPOSE} build aads-dashboard && "
    f"docker compose -f {COMPOSE} up -d --no-deps aads-dashboard && "
    "echo DEPLOY_OK"
)

with open(LOG, "w", encoding="utf-8") as fh:
    p = subprocess.Popen(
        ["/bin/bash", "-lc", script],
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print("started pid", p.pid, "log", LOG)
