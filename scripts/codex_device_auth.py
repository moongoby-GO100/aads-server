#!/usr/bin/env python3
"""Codex CLI device-auth helper — captures device code from PTY output."""
import subprocess, sys, os, re, time, signal

LOG = "/tmp/codex_device_auth.log"
CODE_FILE = "/tmp/codex_device_code.txt"

# Clean previous
for f in [LOG, CODE_FILE]:
    if os.path.exists(f):
        os.remove(f)

proc = subprocess.Popen(
    ["codex", "login", "--device-auth"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    stdin=subprocess.PIPE,
    text=True,
    bufsize=1,
)

collected = []
start = time.time()
code_found = False

while time.time() - start < 120:
    line = proc.stdout.readline()
    if not line:
        if proc.poll() is not None:
            break
        continue
    collected.append(line)
    with open(LOG, "a") as f:
        f.write(line)

    # Look for device code pattern (e.g., XXXX-XXXXX)
    m = re.search(r'([A-Z0-9]{4}-[A-Z0-9]{5,6})', line)
    if m and not code_found:
        code_found = True
        with open(CODE_FILE, "w") as f:
            f.write(m.group(1))
        print(f"DEVICE_CODE={m.group(1)}")
        sys.stdout.flush()

    # Check for success
    if "logged in" in line.lower() or "success" in line.lower():
        print(f"AUTH_SUCCESS")
        break

if proc.poll() is None:
    # Still running — waiting for browser auth
    print(f"WAITING_FOR_BROWSER (pid={proc.pid})")
    # Keep running in background
    sys.exit(0)
else:
    rc = proc.returncode
    print(f"PROCESS_EXIT={rc}")
    if not code_found:
        print("NO_CODE_FOUND")
        print("LOG:")
        print("".join(collected[-20:]))
