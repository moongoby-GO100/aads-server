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
# applied), 5 a migration failed (later files not attempted), 6 ledger unreadable,
# 7 CEO approval unavailable/mismatched, 8 schema inspection failed.

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

# There is no migration-specific, human-CEO-authenticated approval source yet.
# Generic agent_permission_requests.decided_by is a tenant identifier, not proof
# of a human CEO decision. Never accept a file or environment approval override.
# Replace this function only after that trusted approval contract is established.
verify_ceo_approval() {
    echo "APPROVAL-SOURCE-UNRESOLVED 승인 원천 미확정" >&2
    return 1
}

# Bind the review manifest to immutable file bytes and the release identity.
# An extracted deploy archive has no .git; deploy.sh exports AADS_RELEASE_SHA.
release_sha="${AADS_RELEASE_SHA:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)}"
manifest=""
for path in "${pending[@]}"; do
    name="migrations/$(basename "$path")"
    digest="$(sha256sum "$path" | awk '{print $1}')"
    manifest+="${name}|${digest}"$'\n'
done

# Read-only best-effort SQL syntax recognition: ALTER TABLE ... ADD COLUMN /
# ADD CONSTRAINT, including repeated ADD clauses and quoted identifiers.
# Only information_schema SELECTs reach psql; migration text is never executed.
inspect_pending_objects() {
    python3 - "${pending[@]}" <<'PYDRIFT'
import os
import re
import subprocess
import sys

ident = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z_0-9$]*)'
qualified = rf'{ident}(?:\s*\.\s*{ident})?'
def unquote(value):
    return value[1:-1].replace('""', '"') if value.startswith('"') else value.lower()
def literal(value):
    return "'" + value.replace("'", "''") + "'"
for filename in sys.argv[1:]:
    sql = open(filename).read()
    sql = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', sql, flags=re.S)
    for match in re.finditer(
        rf'\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({qualified})(.*?);',
        sql, re.I | re.S,
    ):
        parts = re.findall(ident, match[1])
        schema, table = ('public', unquote(parts[0])) if len(parts) == 1 else map(unquote, parts)
        for addition in re.finditer(
            rf'\bADD\s+(COLUMN|CONSTRAINT)\s+(?:IF\s+NOT\s+EXISTS\s+)?({ident})', match[2], re.I,
        ):
            kind, name = addition[1].upper(), unquote(addition[2])
            view, field = ('columns', 'column_name') if kind == 'COLUMN' else ('table_constraints', 'constraint_name')
            query = (f"SELECT count(*) FROM information_schema.{view} WHERE table_schema={literal(schema)} "
                     f"AND table_name={literal(table)} AND {field}={literal(name)};")
            result = subprocess.run(
                ['timeout', '60', 'docker', 'exec', os.environ.get('PG_CONTAINER', 'aads-postgres'),
                 'psql', '-U', os.environ.get('PGUSER', 'aads'), '-d', os.environ.get('PGDATABASE', 'aads'),
                 '-qAt', '-v', 'ON_ERROR_STOP=1', '-c', query],
                capture_output=True, text=True,
            )
            if result.returncode or not result.stdout.strip().isdigit():
                print(f'DRIFT-ERROR cannot inspect {schema}.{table}.{name}', file=sys.stderr)
                sys.exit(8)
            if int(result.stdout.strip()):
                print(f'DRIFT migrations/{os.path.basename(filename)} (unledgered {kind} {schema}.{table}.{name} already exists)')
PYDRIFT
}

if $plan_only; then
    if ((${#pending[@]})); then
        inspect_pending_objects || exit 8
    fi
elif ((${#pending[@]})); then
    printf 'RELEASE %s\n%s' "${release_sha:-unknown}" "$manifest"
    if ! verify_ceo_approval "$release_sha" "$manifest"; then
        echo "BLOCK-CEO-APPROVAL: PENDING migrations require a human CEO approval bound to RELEASE and every filename/sha256 above."
        echo "승인 방법: CEO 사람 로그인으로 해당 명세를 검토. 승인 원천 미확정: 안전한 전용 승인 경로 확정 전에는 적용할 수 없습니다."
        exit 7
    fi
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
