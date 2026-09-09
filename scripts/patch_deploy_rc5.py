#!/usr/bin/env python3
"""Patch deploy.sh with RC5: SSH auto-detach to prevent TERM/HUP kills."""
import sys

path = '/root/aads/aads-server/deploy.sh'
with open(path) as f:
    lines = f.readlines()

# Check if already patched
for line in lines:
    if 'AADS_DEPLOY_DETACHED' in line:
        print('ALREADY_PATCHED')
        sys.exit(0)

# Find insertion point: after mkdir -p "${STATE_DIR}/logs"
insert_after = None
for i, line in enumerate(lines):
    if line.strip() == 'mkdir -p "${STATE_DIR}/logs"':
        insert_after = i
        break

if insert_after is None:
    print('MARKER_NOT_FOUND')
    sys.exit(1)

block = [
    '\n',
    '# RC5: Auto-detach when invoked through SSH — prevents TERM/HUP from killing\n',
    '# a long-running deploy when the SSH session drops or times out.\n',
    'if [[ -n "${SSH_CONNECTION:-}" && -z "${AADS_DEPLOY_DETACHED:-}" ]]; then\n',
    '    export AADS_DEPLOY_DETACHED=1\n',
    '    _detach_log="${STATE_DIR}/logs/deploy-detach-$(date +%Y%m%d-%H%M%S)-$$.log"\n',
    '    if command -v setsid >/dev/null 2>&1; then\n',
    '        setsid bash "$0" "$@" >"$_detach_log" 2>&1 &\n',
    '    else\n',
    '        nohup bash "$0" "$@" >"$_detach_log" 2>&1 &\n',
    '    fi\n',
    '    _detach_pid=$!\n',
    '    echo "[deploy.sh] detached: pid=${_detach_pid}, log=${_detach_log}"\n',
    '    exit 0\n',
    'fi\n',
]

new_lines = lines[:insert_after + 1] + block + lines[insert_after + 1:]
with open(path, 'w') as f:
    f.writelines(new_lines)
print('PATCHED')
