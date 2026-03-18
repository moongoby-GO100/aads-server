#!/usr/bin/env python3
"""KIS 자동매매/수집 크론 비활성화 (CEO 지시 2026-03-18)"""
import subprocess, sys

r = subprocess.run(
    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
     "root@host.docker.internal", "crontab -l"],
    capture_output=True, text=True
)
if r.returncode != 0:
    print(f"ERROR: crontab -l failed: {r.stderr}")
    sys.exit(1)

lines = r.stdout.split("\n")
keywords = [
    "kisautotrade", "webapp/backend", "webapp/scripts",
    "auto_trading", "integrated-trading", "watchdog.service",
    "kis-autotrade", "collection_scheduler", "monitor_system",
    "genspark_ai_developer", "alert_07_50",
]

new_lines = []
disabled_count = 0
for line in lines:
    stripped = line.strip()
    if stripped.startswith("#") or not stripped:
        new_lines.append(line)
        continue
    if any(kw in line for kw in keywords):
        new_lines.append("# [DISABLED 20260318 CEO] " + line)
        disabled_count += 1
    else:
        new_lines.append(line)

new_crontab = "\n".join(new_lines) + "\n"

with open("/tmp/new_crontab", "w") as f:
    f.write(new_crontab)

# scp to host then apply
subprocess.run(
    ["scp", "-o", "StrictHostKeyChecking=no", "/tmp/new_crontab",
     "root@host.docker.internal:/tmp/new_crontab"],
    capture_output=True, text=True
)
p = subprocess.run(
    ["ssh", "-o", "StrictHostKeyChecking=no",
     "root@host.docker.internal", "crontab /tmp/new_crontab"],
    capture_output=True, text=True
)
if p.returncode != 0:
    print(f"ERROR: crontab apply failed: {p.stderr}")
    sys.exit(1)

print(f"OK: {disabled_count}개 크론 비활성화 완료")
