#!/usr/bin/env python3
"""Patch deploy.sh: auto-stash dirty worktree instead of blocking."""
import sys

path = '/root/aads/aads-server/deploy.sh'
with open(path) as f:
    content = f.read()

if 'AADS_DEPLOY_STASHED' in content:
    print('ALREADY_PATCHED')
    sys.exit(0)

# 1. Replace the blocking path in enforce_release_worktree_gate()
# Old: block and exit
old_block = '''    echo "[deploy.sh] ❌ dirty worktree detected; release blocked before build."
    echo "[deploy.sh]    Commit/stash/split unrelated files, or explicitly set AADS_DEPLOY_ALLOW_DIRTY_ARCHIVE=true after confirming dirty files must be excluded."
    audit_control "release-context" "$COMPOSE_DIR" "blocked" "dirty worktree count=${dirty_count}"
    return 1'''

# New: auto-stash and continue
new_block = '''    echo "[deploy.sh] ⚠️ dirty worktree detected (${dirty_count} files); auto-stashing for clean release."
    if git -C "$COMPOSE_DIR" stash -u -m "deploy-autostash-$(date +%Y%m%d-%H%M%S)" 2>/dev/null; then
        export AADS_DEPLOY_STASHED=1
        echo "[deploy.sh] ✅ auto-stash succeeded; will restore after deploy."
        audit_control "release-context" "$COMPOSE_DIR" "auto-stashed" "dirty worktree count=${dirty_count}"
        return 0
    fi
    echo "[deploy.sh] ❌ auto-stash failed; release blocked."
    audit_control "release-context" "$COMPOSE_DIR" "blocked" "dirty worktree count=${dirty_count}; stash failed"
    return 1'''

if old_block not in content:
    print('MARKER_NOT_FOUND: blocking path')
    sys.exit(1)

content = content.replace(old_block, new_block)

# 2. Add stash pop to cleanup_release_context()
old_cleanup = 'cleanup_release_context() {'
new_cleanup = '''cleanup_release_context() {
    if [[ "${AADS_DEPLOY_STASHED:-0}" == "1" ]]; then
        echo "[deploy.sh] restoring auto-stashed worktree changes..."
        git -C "$COMPOSE_DIR" stash pop 2>/dev/null || echo "[deploy.sh] ⚠️ stash pop failed; changes remain in git stash list"
        export AADS_DEPLOY_STASHED=0
    fi'''

if old_cleanup not in content:
    print('MARKER_NOT_FOUND: cleanup_release_context')
    sys.exit(1)

content = content.replace(old_cleanup, new_cleanup, 1)

with open(path, 'w') as f:
    f.write(content)
print('PATCHED')
