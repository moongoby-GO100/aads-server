#!/usr/bin/env python3
"""Fix TypeScript error at line 2012: wrap removed in braces"""
import subprocess, sys

HOST = "root@host.docker.internal"
FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

r = subprocess.run(
    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", HOST, f"cat {FILE}"],
    capture_output=True, text=True, timeout=30
)
content = r.stdout

old = '      if (removed) setInput(removed); chatInputRef.current?.setValue(removed);'
new = '      if (removed) { setInput(removed); chatInputRef.current?.setValue(removed); }'

if old in content:
    content = content.replace(old, new, 1)
    p = subprocess.Popen(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", HOST, f"cat > {FILE}"],
        stdin=subprocess.PIPE, text=True
    )
    p.communicate(input=content, timeout=30)
    print(f"FIX OK (exit={p.returncode})")
else:
    print("Pattern not found - maybe already fixed?")
