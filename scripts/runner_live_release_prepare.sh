#!/usr/bin/env bash
# Print a runner-live release manifest (JSON) to STDOUT. Installs nothing.
#
# usage: runner_live_release_prepare.sh <release_sha(40 hex)> [--approved-by NAME]
#
# expected_old_sha  = current HEAD of the runner-live checkout
# companion/controller sha256 = scripts/runner_busy_lib.sh and
#                               scripts/runner_live_update.sh at <release_sha>
# release_sha must already be an ancestor of origin/main (this script does not
# fetch; run `git -C <live> fetch origin main` first if needed).
#
# A human reviews the output and installs it as /etc/aads/runner-live-release.json
# (root:root 0644) under a separate CEO approval. See
# docs/design/20261014_AADS_러너라이브_정식반영경로_설계.md.
set -euo pipefail

LIVE_DIR="${AADS_RUNNER_LIVE_DIR:-/root/aads/runner-live}"
release_sha="${1:-}"
approved_by="$(id -un 2>/dev/null || echo unknown)"
if [[ "${2:-}" == --approved-by ]]; then
    approved_by="${3:-}"
elif [[ -n "${2:-}" ]]; then
    echo "usage: $0 <release_sha> [--approved-by NAME]" >&2
    exit 2
fi
if [[ ! "$release_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "usage: $0 <release_sha(40 lowercase hex)> [--approved-by NAME]" >&2
    exit 2
fi
if [[ ! "$approved_by" =~ ^[A-Za-z0-9._@:+\ -]{1,64}$ ]]; then
    echo "ERROR: --approved-by must match [A-Za-z0-9._@:+ -]{1,64}" >&2
    exit 2
fi

git_live() { git -C "$LIVE_DIR" "$@"; }

git_live cat-file -e "${release_sha}^{commit}" 2>/dev/null \
    || { echo "ERROR: $release_sha is not a commit in $LIVE_DIR" >&2; exit 1; }
git_live merge-base --is-ancestor "$release_sha" origin/main 2>/dev/null \
    || { echo "ERROR: $release_sha is not an ancestor of origin/main" >&2; exit 1; }

old_sha=$(git_live rev-parse HEAD)
[[ "$old_sha" != "$release_sha" ]] \
    || { echo "ERROR: runner-live is already at $release_sha" >&2; exit 1; }
companion_sha=$(git_live show "${release_sha}:scripts/runner_busy_lib.sh" | sha256sum | cut -d' ' -f1)
controller_sha=$(git_live show "${release_sha}:scripts/runner_live_update.sh" | sha256sum | cut -d' ' -f1)
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

python3 - "$release_sha" "$old_sha" "$companion_sha" "$controller_sha" "$created_at" "$approved_by" <<'PY'
import json
import sys

keys = ('release_sha', 'expected_old_sha', 'companion_sha256', 'controller_sha256',
        'created_at', 'approved_by')
doc = {'schema': 1, **dict(zip(keys, sys.argv[1:]))}
print(json.dumps(doc, indent=2))
PY
echo "NOTE: nothing was installed. Review, then install as root:root 0644 only after CEO approval." >&2
