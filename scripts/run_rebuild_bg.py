#!/usr/bin/env python3
"""rebuild_and_switch_dashboard.py 를 백그라운드로 실행"""
import subprocess, os
p = subprocess.Popen(
    ["python3", "/root/aads/aads-server/scripts/rebuild_and_switch_dashboard.py"],
    stdout=open("/dev/null","w"), stderr=open("/dev/null","w"),
    start_new_session=True
)
print(f"PID={p.pid} 시작됨. 로그: /tmp/aads_dashboard_rebuild.log")
