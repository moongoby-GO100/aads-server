#!/usr/bin/env python3
"""Deploy aads-dashboard (build + up -d)."""
import subprocess, sys

compose = '/root/aads/aads-dashboard/docker-compose.yml'

print('=== Building aads-dashboard ===')
r = subprocess.run(
    ['docker', 'compose', '-f', compose, 'build', 'aads-dashboard'],
    capture_output=True, text=True, timeout=300,
)
print(r.stdout[-2000:] if r.stdout else '')
if r.stderr:
    print(r.stderr[-1000:])
if r.returncode != 0:
    print(f'BUILD FAILED (exit={r.returncode})')
    sys.exit(1)

print('=== Starting aads-dashboard ===')
r2 = subprocess.run(
    ['docker', 'compose', '-f', compose, 'up', '-d', 'aads-dashboard'],
    capture_output=True, text=True, timeout=60,
)
print(r2.stdout[-500:] if r2.stdout else '')
if r2.stderr:
    print(r2.stderr[-500:])
if r2.returncode != 0:
    print(f'UP FAILED (exit={r2.returncode})')
    sys.exit(1)

print('DEPLOY SUCCESS')
