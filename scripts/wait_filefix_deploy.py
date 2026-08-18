#!/usr/bin/env python3
"""AADS-FILES: 배포 로그를 최대 100초 폴링하고 마지막 상태를 출력한다."""
import pathlib
import time

LOG = pathlib.Path("/root/aads/aads-server/logs/filefix_deploy.log")
DEADLINE = time.time() + 100

while time.time() < DEADLINE:
    text = LOG.read_text(encoding="utf-8", errors="ignore")
    if "=== DONE" in text:
        print("[DONE] 전체 단계 종료")
        break
    time.sleep(5)

text = LOG.read_text(encoding="utf-8", errors="ignore")
lines = [ln for ln in text.splitlines() if ln.strip()]
marks = [ln for ln in lines if ln.startswith("===") or "deploy.sh]" in ln]
print("---- 단계 마커 (마지막 12) ----")
for ln in marks[-12:]:
    print(ln)
print("---- 로그 마지막 3줄 ----")
for ln in lines[-3:]:
    print(ln)
