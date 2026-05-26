#!/usr/bin/env python3
"""Build and deploy aads-dashboard in background."""
import subprocess
import sys

COMPOSE = "/root/aads/aads-dashboard/docker-compose.yml"

print("Starting dashboard build+deploy in background...")
proc = subprocess.Popen(
    ["docker", "compose", "-f", COMPOSE, "up", "-d", "--build", "aads-dashboard"],
    stdout=open("/tmp/dashboard_deploy.log", "w"),
    stderr=subprocess.STDOUT,
)
print(f"PID: {proc.pid}")
print("Log: /tmp/dashboard_deploy.log")
print("Check: ps aux | grep {pid} or cat /tmp/dashboard_deploy.log")
