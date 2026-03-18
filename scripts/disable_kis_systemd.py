#!/usr/bin/env python3
"""KIS systemd 타이머/서비스 완전 비활성화"""
import subprocess

timers = [
    "kis-1m-daily.timer",
    "kis-1m-intraday.service",
    "kis-autotrade.service",
    "kis-autotrade-api.service",
    "kis-autotrade-scalping.service",
    "kis-ohlcv-sync.timer",
    "kis-ohlcv-sync.service",
    "kis-chart-refresh.timer",
    "kis-chart-refresh.service",
    "kis-collect-report.timer",
    "kis-collect-report.service",
    "kis-autotrade-top100.timer",
    "kis-autotrade-top100.service",
    "kis-autotrade-backup.timer",
    "kis-autotrade-backup-verify.timer",
    "kis-autotrade-backup-sandbox.timer",
]

host = "root@host.docker.internal"

for unit in timers:
    for action in ["stop", "disable"]:
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
               host, f"systemctl {action} {unit}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        status = "OK" if r.returncode == 0 else f"SKIP({r.stderr.strip()[:50]})"
        print(f"{action:8s} {unit:45s} {status}")

# Verify
r = subprocess.run(
    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
     host, "systemctl list-units --type=service,timer --state=active | grep kis"],
    capture_output=True, text=True, timeout=10
)
print(f"\n=== Active KIS units ===\n{r.stdout if r.stdout.strip() else '(none)'}")
