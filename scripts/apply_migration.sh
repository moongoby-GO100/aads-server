#!/bin/bash
# Apply SQL files once and record their checksums in schema_migrations.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
force=false
dry_run=false
files=()

usage() {
    echo "Usage: $0 [--force] [--dry-run] <sql-file>..." >&2
}

sql_literal() {
    local value="$1"
    printf "'%s'" "${value//\'/\'\'}"
}

migration_name() {
    local file="$1"
    if [[ "$file" == "$REPO_ROOT/"* ]]; then
        printf '%s' "${file#"$REPO_ROOT/"}"
    else
        basename "$file"
    fi
}

while (($#)); do
    case "$1" in
        --force) force=true ;;
        --dry-run) dry_run=true ;;
        -h|--help) usage; exit 0 ;;
        --) shift; files+=("$@"); break ;;
        -*) echo "Unknown option: $1" >&2; usage; exit 2 ;;
        *) files+=("$1") ;;
    esac
    shift
done

if ((${#files[@]} == 0)); then
    usage
    exit 2
fi

# Validate the entire invocation before the first DB call (also with --force).
# Ordered absolute filename:content-sha256 lines bind the marker to these files.
gate_manifest() {
    local input absolute
    for input in "$@"; do
        [[ -f "$input" ]] || { echo "SQL file not found: $input" >&2; return 2; }
        absolute="$(cd "$(dirname "$input")" && pwd)/$(basename "$input")"
        printf '%s:%s\n' "$absolute" "$(sha256sum "$absolute" | awk '{print $1}')"
    done
}

if ! $dry_run; then
    manifest_rows="$(gate_manifest "${files[@]}")" || exit 2
    expected_gate="$(printf '%s\n' "$manifest_rows" | sha256sum | awk '{print $1}')"
    if [[ "${AADS_MIGRATION_GATE_MANIFEST:-}" != "$expected_gate" ]]; then
        if ! python3 - "$REPO_ROOT/scripts" "${files[@]}" <<'PYGATE'
import sys
from pathlib import Path

message = "BLOCK-CEO-APPROVAL: direct apply_migration.sh 는 자동 검증 통과 또는 apply_release_migrations.sh 경유가 필요합니다"
try:
    sys.path.insert(0, sys.argv[1])
    from release_migration_autoverify import classify_files
    reasons = classify_files([Path(name) for name in sys.argv[2:]])
except Exception as exc:
    reasons = [f"자동 검증 판정 실패: {exc}"]
if reasons:
    print(message, file=sys.stderr)
    for reason in reasons:
        print(reason, file=sys.stderr)
    sys.exit(7)
PYGATE
        then
            exit 7
        fi
    fi
fi

for input_file in "${files[@]}"; do
    if [[ ! -f "$input_file" ]]; then
        echo "SQL file not found: $input_file" >&2
        exit 2
    fi

    file="$(cd "$(dirname "$input_file")" && pwd)/$(basename "$input_file")"
    filename="$(migration_name "$file")"
    sha256="$(sha256sum "$file" | awk '{print $1}')"
    filename_sql="$(sql_literal "$filename")"
    sha256_sql="$(sql_literal "$sha256")"

    existing_sha=""
    table_exists="$(docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" -qAtc \
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='schema_migrations');")"
    if [[ "$table_exists" == "t" ]]; then
        existing_sha="$(docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" -qAtc \
            "SELECT sha256 FROM schema_migrations WHERE filename=${filename_sql};")"
    fi

    if [[ -n "$existing_sha" && "$existing_sha" == "$sha256" ]]; then
        echo "SKIP $filename (same sha256)"
        continue
    fi
    if [[ -n "$existing_sha" && "$existing_sha" != "$sha256" ]] && ! $force; then
        echo "BLOCK $filename: sha256 mismatch (recorded=$existing_sha current=$sha256); use --force to apply" >&2
        exit 3
    fi

    if $dry_run; then
        if [[ -n "$existing_sha" && "$existing_sha" != "$sha256" ]]; then
            echo "DRY-RUN APPLY $filename (forced sha256 update)"
        else
            echo "DRY-RUN APPLY $filename"
        fi
        continue
    fi

    echo "APPLY $filename"
    {
        printf '%s\n' 'BEGIN;'
        # Some legacy migrations contain their own COMMIT. Keep the timer at
        # session scope so the checksum ledger can still be written afterward.
        printf "%s\n" "SELECT set_config('aads.migration_started_at', clock_timestamp()::text, false);"
        sed -e '$a\' "$file"
        printf '%s\n' \
            "INSERT INTO schema_migrations (filename, sha256, applied_by, source, duration_ms)" \
            "VALUES (${filename_sql}, ${sha256_sql}, current_user, 'runtime'," \
            "        GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - current_setting('aads.migration_started_at')::timestamptz)) * 1000)::integer)" \
            "ON CONFLICT (filename) DO UPDATE" \
            "SET sha256 = EXCLUDED.sha256," \
            "    applied_at = now()," \
            "    applied_by = EXCLUDED.applied_by," \
            "    source = EXCLUDED.source," \
            "    duration_ms = EXCLUDED.duration_ms," \
            "    note = NULL;" \
            'COMMIT;'
    } | docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDATABASE"
    echo "APPLIED $filename"
done
