#!/usr/bin/env bash
# Run on Jinah in an exact `git archive <sha>` export. Stage only; never cut over.
set -euo pipefail
release_sha=${1:?usage: stage-release.sh FULL_COMMITTED_SHA}
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid release SHA' >&2; exit 2; }
[[ "$EUID" == 0 ]] || { echo 'Run as root on the target host' >&2; exit 2; }
release_dir="/opt/obys/releases/$release_sha"
[[ "$(pwd -P)" == "$release_dir" ]] || { echo 'Wrong release directory' >&2; exit 2; }
exec 9>/run/lock/obys-stage-release.lock
flock -w 10 9 || { echo 'Another release is staging' >&2; exit 3; }
test -f app/obys_standalone.py
test -f deploy/obys/requirements.obys.lock
if ! id obys >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/obys --shell /usr/sbin/nologin obys
fi
install -d -m 0750 -o obys -g obys /var/lib/obys
install -d -m 0700 -o obys -g obys /var/lib/obys/finance /var/lib/obys/ledgers
install -d -m 0755 /opt/obys/slots
if [[ ! -f .runtime-ready ]]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install --disable-pip-version-check -r deploy/obys/requirements.obys.lock
    .venv/bin/python -m pip check
    printf '%s\n' "$release_sha" > .runtime-ready
fi
[[ "$(cat .runtime-ready)" == "$release_sha" ]] || { echo 'Release marker mismatch' >&2; exit 4; }
install -m 0644 deploy/obys/obys-api@.service /etc/systemd/system/obys-api@.service
systemctl daemon-reload
printf 'Staged %s. No slot started, enabled, relinked or routed.\n' "$release_sha"
