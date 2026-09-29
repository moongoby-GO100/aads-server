#!/bin/bash
# Apply every pending migrations/*.sql of a release through the checksum ledger.
#
# 2026-09-29 866df31a: deploy.sh applied only three hard-coded SQL files, so
# migrations/20260929_goal_pause_all_paths.sql shipped with the code but never
# reached the database ("column paused_at does not exist", 28 failures in 8 min).
# This script is the one place a release's schema is brought up to date.
#
# Usage: apply_release_migrations.sh [--root DIR] [--plan] [--only FILE]...
#   --root DIR   release tree containing migrations/ and scripts/ (default: repo)
#   --plan       classify and run the destructive-SQL gate, apply nothing
#   --only FILE  limit to these migrations (repo-relative, e.g. migrations/x.sql)
#
# Per file one of these lines is printed, in filename order:
#   SKIP     recorded in schema_migrations with the same sha256
#   DRIFT    recorded with a different sha256 — already applied, never re-run
#   HOLD     listed in the baseline (pre-ledger, applied by hand or unverified)
#   EXCLUDE  *.verify.sql / *rollback* — not a forward migration
#   PENDING  --plan only: would be applied
#   APPLY    applied now via scripts/apply_migration.sh
#
# Exit codes: 0 ok, 2 usage, 4 destructive SQL in a pending file (nothing
# applied), 5 a migration failed (later files not attempted), 6 ledger unreadable.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
plan_only=false
only=()

while (($#)); do
    case "$1" in
        --root) ROOT="$(cd "$2" && pwd)"; shift ;;
        --plan) plan_only=true ;;
        --only) only+=("$2"); shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

APPLY_BIN="${AADS_APPLY_MIGRATION_BIN:-${ROOT}/scripts/apply_migration.sh}"
BASELINE_FILE="${AADS_MIGRATION_BASELINE_FILE:-${ROOT}/scripts/migrations_auto_apply_baseline.txt}"

# filename|sha256 per line. Tests pass a file; production reads the ledger.
read_ledger() {
    if [[ -n "${AADS_MIGRATION_LEDGER_FILE:-}" ]]; then
        cat "$AADS_MIGRATION_LEDGER_FILE"
        return
    fi
    docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" -qAt -v ON_ERROR_STOP=1 \
        -c "SELECT filename || '|' || sha256 FROM schema_migrations;"
}

# Prints the first destructive statement kind found, empty when none.
# Line-by-line awk on purpose (R-BG: no nested-repetition regex on whole files).
destructive_reason() {
    awk '
        BEGIN { stmt = ""; found = 0 }
        function check(s) {
            gsub(/[ \t\r\n]+/, " ", s)
            s = " " toupper(s) " "
            if (s ~ /[^A-Z_]DROP TABLE /) return "DROP TABLE"
            if (s ~ /[^A-Z_]DROP COLUMN /) return "DROP COLUMN"
            if (s ~ /[^A-Z_]TRUNCATE[ ;(]/) return "TRUNCATE"
            if (s ~ /[^A-Z_]DELETE FROM / && s !~ / WHERE /) return "DELETE without WHERE"
            return ""
        }
        {
            line = $0
            sub(/--.*/, "", line)
            n = split(line, parts, ";")
            for (i = 1; i <= n; i++) {
                stmt = stmt " " parts[i]
                if (i < n) {
                    r = check(stmt)
                    if (r != "") { print r; found = 1; exit }
                    stmt = ""
                }
            }
        }
        END { if (!found && stmt != "") { r = check(stmt); if (r != "") print r } }
    ' "$1"
}

if ! ledger="$(read_ledger)"; then
    echo "LEDGER-ERROR cannot read schema_migrations" >&2
    exit 6
fi
declare -A recorded=()
while IFS='|' read -r name sha; do
    [[ -n "$name" ]] && recorded["$name"]="$sha"
done <<< "$ledger"

declare -A baseline=()
if [[ -f "$BASELINE_FILE" ]]; then
    while read -r name _; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        baseline["$name"]=1
    done < "$BASELINE_FILE"
fi

declare -A wanted=()
for name in "${only[@]}"; do
    wanted["$name"]=1
done

pending=()
counts_skip=0 counts_drift=0 counts_hold=0 counts_exclude=0
while IFS= read -r path; do
    name="migrations/$(basename "$path")"
    if ((${#only[@]})) && [[ -z "${wanted[$name]:-}" ]]; then
        continue
    fi
    base="$(basename "$path")"
    case "$base" in
        *.verify.sql|*rollback*)
            echo "EXCLUDE $name (verify/rollback script)"
            counts_exclude=$((counts_exclude + 1))
            continue
            ;;
    esac
    sha="$(sha256sum "$path" | awk '{print $1}')"
    # Manual runs from another checkout recorded the bare basename
    # (20260929_goal_pause_all_paths.sql); both names mean "applied".
    rec="${recorded[$name]:-${recorded[$base]:-}}"
    if [[ -n "$rec" && "$rec" == "$sha" ]]; then
        echo "SKIP $name (already applied, same sha256)"
        counts_skip=$((counts_skip + 1))
    elif [[ -n "$rec" ]]; then
        echo "DRIFT $name (already applied with sha256=${rec:0:12}, file=${sha:0:12}; not re-run)"
        counts_drift=$((counts_drift + 1))
    elif [[ -n "${baseline[$name]:-}" ]]; then
        echo "HOLD $name (pre-ledger baseline; verify and apply with scripts/apply_migration.sh)"
        counts_hold=$((counts_hold + 1))
    else
        pending+=("$path")
    fi
done < <(find "$ROOT/migrations" -maxdepth 1 -type f -name '*.sql' | LC_ALL=C sort)

# Gate every pending file before touching the database, so a destructive file
# late in the list can never leave the earlier ones half-applied.
blocked=0
for path in "${pending[@]}"; do
    reason="$(destructive_reason "$path")"
    if [[ -n "$reason" ]]; then
        echo "BLOCK-DESTRUCTIVE migrations/$(basename "$path"): ${reason}; auto-apply refused, apply manually after review"
        blocked=1
    fi
done
if ((blocked)); then
    exit 4
fi

applied=0
for path in "${pending[@]}"; do
    name="migrations/$(basename "$path")"
    if $plan_only; then
        echo "PENDING $name"
        continue
    fi
    echo "APPLY $name"
    if ! "$APPLY_BIN" "$path"; then
        echo "FAILED $name; later migrations not attempted" >&2
        exit 5
    fi
    applied=$((applied + 1))
done

echo "SUMMARY pending=${#pending[@]} applied=${applied} skipped=${counts_skip} drift=${counts_drift} held=${counts_hold} excluded=${counts_exclude} plan=${plan_only}"
