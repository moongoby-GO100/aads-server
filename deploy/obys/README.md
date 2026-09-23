# Jinah M1 runtime foundation

This is candidate infrastructure, not a certified production cutover. Do not
change DNS/upstream or start a collection worker until PRD M2-M4 pass. Existing
AADS deployments continue to use `deploy.sh bluegreen`.

## Layout

- `/opt/obys/releases/<git-sha>/`: immutable source plus per-release Python 3.12 venv.
- `/opt/obys/slots/blue`, `green`: links to tested releases, ports 8110/8111.
- `/var/lib/obys/finance`, `/var/lib/obys/ledgers`: persistent private files.
- `obys-api@blue.service`, `obys-api@green.service`: non-root API only.
- `/etc/obys/runtime.env`, `/etc/obys/slots/{blue,green}.env`: operator-provisioned,
  non-repository configuration. This implementation does not write secret files.

Required runtime settings: `OBYS_AUTH_DATABASE_URL`, `OBYS_DATABASE_URL`,
`ACCT_DATABASE_URL`, `JWT_SECRET_KEY`, `YEOLJEONG_FINANCE_DATA_DIR`,
`OBYS_UPLOAD_ROOT`; slot setting: `OBYS_PORT`. All three DSNs must explicitly
point at distinct local PostgreSQL databases. Runtime DB roles must not be
superusers or bypass RLS. Do not inherit the AADS `DATABASE_URL` or finance DSN.
Use a new signing key; token issuer/audience separation and organization/usage
decoupling are M2 gates, not implemented by this runtime foundation.

Startup fails on missing/remote/mixed DB settings, absent persistent directories,
missing tables/functions/grants or unavailable DBs. `/health/live` means process
alive; `/health/ready` checks all three DBs with bounded read-only queries. Neither
is proof of full business/data equivalence, write grants or file migration.

## Staging and rollback

Build the venv once in a clean committed release, install `requirements.obys.lock`,
run `pip check`, and stage the same release into both slots. Install this template
with `systemd-analyze verify` then `systemctl daemon-reload`. Installation alone
must not enable or start slots. Supply the independently prepared auth/business
DBs, read-only ACCT role and file manifests before starting a candidate.

`stage-release.sh <full-sha>` runs on Jinah inside an exact committed archive at
`/opt/obys/releases/<full-sha>`. It serializes dependency staging, creates the
unprivileged account/private persistent directories, and installs the unit. It
never writes credential files, starts/enables units, changes slot links or proxy
routing. Preserve the original archive checksum with the release evidence; do
not copy a dirty working tree into this directory.

Only the inactive slot may be changed. Check direct readiness and authenticated
menu/button parity before touching the proxy. A future cutover controller must
enforce short routing lock, immediate routed-health rollback, draining, identical
release in standby and at least 300 seconds of P0/P1 monitoring. M1 supplies no
automatic cutover command. Revert candidate setup by stopping the candidate;
the existing public service and its DB remain authoritative.

After cutover, reverting a symlink/DNS is insufficient: preserve and replay new
transactions, permissions and files first, as specified in the PRD. Never start
two collectors. Their cross-host execution fence is a separate required gate.
