#!/usr/bin/env python3
"""Deploy aads-dashboard in background (build + rm old + up)."""
import subprocess

log = '/tmp/dashboard_deploy2.log'
compose = '/root/aads/aads-dashboard/docker-compose.yml'

with open(log, 'w') as f:
    f.write('=== Dashboard deploy v2 started ===\n')

logf = open(log, 'a')
proc = subprocess.Popen(
    ['docker', 'compose', '-f', compose, 'up', '-d', '--build', '--force-recreate', 'aads-dashboard'],
    stdout=logf,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(f'Deploy PID={proc.pid}, log={log}')
