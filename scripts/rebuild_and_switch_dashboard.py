#!/usr/bin/env python3
"""
aads-dashboard-green --no-cache 재빌드 후 nginx upstream 수동 전환
B1-B4 패치가 반영된 새 이미지로 green을 재빌드하고 서비스에 반영한다.
"""
import subprocess
import os
import re
import sys

LOG = "/tmp/aads_dashboard_rebuild.log"
COMPOSE_DIR = "/root/aads/aads-server"
NGINX_UPSTREAM = "/etc/nginx/conf.d/aads-upstream.conf"

log_fd = open(LOG, "w", buffering=1)

def run(cmd, **kwargs):
    log_fd.write(f"\n$ {' '.join(cmd)}\n")
    log_fd.flush()
    r = subprocess.run(cmd, stdout=log_fd, stderr=log_fd, **kwargs)
    log_fd.write(f"[exit {r.returncode}]\n")
    log_fd.flush()
    return r

def log(msg):
    log_fd.write(f"[INFO] {msg}\n")
    log_fd.flush()
    print(msg)

log("Step 1: docker build --no-cache aads-dashboard-green")
r = run([
    "docker", "compose",
    "-f", f"{COMPOSE_DIR}/docker-compose.prod.yml",
    "--profile", "green",
    "build", "--no-cache", "aads-dashboard-green"
], cwd=COMPOSE_DIR)
if r.returncode != 0:
    log("빌드 실패!")
    sys.exit(1)
log("빌드 완료")

log("Step 2: green 컨테이너 재시작")
run(["docker", "rm", "-f", "aads-dashboard-green"], cwd=COMPOSE_DIR)
r = run([
    "docker", "compose",
    "-f", f"{COMPOSE_DIR}/docker-compose.prod.yml",
    "--profile", "green",
    "up", "-d", "--no-deps", "aads-dashboard-green"
], cwd=COMPOSE_DIR)
if r.returncode != 0:
    log("컨테이너 시작 실패!")
    sys.exit(1)

# Step 3: green 헬스체크
import time
log("Step 3: green 헬스체크 대기 (최대 60초)")
for i in range(30):
    time.sleep(2)
    r2 = subprocess.run(
        ["wget", "-q", "--spider", "http://127.0.0.1:3101/login"],
        capture_output=True
    )
    if r2.returncode == 0:
        log(f"헬스체크 통과 ({i*2}초)")
        break
else:
    log("헬스체크 실패!")
    sys.exit(1)

# Step 4: nginx upstream → green
log("Step 4: nginx upstream → green 전환")
text = open(NGINX_UPSTREAM).read()
# 백업
open(NGINX_UPSTREAM + ".bak_manual", "w").write(text)

text2, count = re.subn(
    r"upstream\s+aads_dashboard\s*\{.*?\n\}",
    ("upstream aads_dashboard {\n"
     "    zone aads_dashboard 64k;\n"
     "    least_conn;\n"
     "    # Active slot is the non-backup line. dashboard deploy.sh rewrites 3100/3101.\n"
     "    server 127.0.0.1:3100 max_fails=3 fail_timeout=30s backup;\n"
     "    server 127.0.0.1:3101 max_fails=3 fail_timeout=30s;\n"
     "    keepalive 16;\n}"),
    text, count=1, flags=re.S
)
if count != 1:
    log("upstream block 찾기 실패!")
    sys.exit(1)

open(NGINX_UPSTREAM, "w").write(text2)
log("nginx 설정 변경 완료")

# Step 5: nginx -t 검증
r = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
log_fd.write(r.stderr + "\n")
if r.returncode != 0:
    log("nginx -t 실패! 롤백")
    import shutil
    shutil.copy(NGINX_UPSTREAM + ".bak_manual", NGINX_UPSTREAM)
    sys.exit(1)
log("nginx -t 통과")

# Step 6: nginx reload
r = subprocess.run(["nginx", "-s", "reload"], capture_output=True, text=True)
if r.returncode != 0:
    log(f"nginx reload 실패: {r.stderr}")
    sys.exit(1)
log("nginx reload 완료")

# Step 7: 상태 기록
open("/root/aads/aads-dashboard/.active_container", "w").write("aads-dashboard-green\n")
open("/root/aads/aads-dashboard/.active_port", "w").write("3101\n")

log("✅ 배포 완료 — green 슬롯(3101) 서비스 중")
log(f"로그: {LOG}")
