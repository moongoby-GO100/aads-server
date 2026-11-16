#!/bin/bash
# scripts/run_migration_down.sh
# 되돌림(down) 마이그레이션 실행기: 지문 함수 선주입 및 실행 가드
#
# Usage: run_migration_down.sh [--print|--execute] <migration_file>
#   --print    (기본) 실행 없이 지문 함수와 down SQL을 합쳐 표준출력으로 출력
#   --execute  AADS_DOWN_APPROVED=<승인 id> 환경변수가 설정된 경우에만 docker exec로 psql 실행

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"

mode="print"
migration_arg=""

usage() {
    echo "Usage: $0 [--print|--execute] <migration_file>" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --print)
            mode="print"
            shift
            ;;
        --execute)
            mode="execute"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Error: Unknown option $1" >&2
            usage
            exit 2
            ;;
        *)
            if [[ -n "$migration_arg" ]]; then
                echo "Error: Only one migration file may be specified" >&2
                usage
                exit 2
            fi
            migration_arg="$1"
            shift
            ;;
    esac
done

if [[ -z "$migration_arg" ]]; then
    echo "Error: Migration file argument is required" >&2
    usage
    exit 2
fi

base="$(basename "$migration_arg")"
stem="$base"
stem="${stem%.down.sql}"
stem="${stem%_rollback.sql}"
stem="${stem%.rollback.sql}"
stem="${stem%.verify.sql}"
stem="${stem%.sql}"
stem="${stem%.down}"

down_file="$REPO_ROOT/migrations/rollback/${stem}.down.sql"
if [[ ! -f "$down_file" ]]; then
    if [[ -f "$migration_arg" ]]; then
        down_file="$migration_arg"
    elif [[ -f "$REPO_ROOT/$migration_arg" ]]; then
        down_file="$REPO_ROOT/$migration_arg"
    else
        echo "Error: 되돌림(down) 파일을 찾을 수 없습니다: $down_file" >&2
        exit 1
    fi
fi

helper_file="$REPO_ROOT/migrations/release_object_fingerprint.verify.sql"
if [[ ! -f "$helper_file" ]]; then
    echo "Error: 지문 함수 선주입 파일을 찾을 수 없습니다: $helper_file" >&2
    exit 1
fi

build_down_sql() {
    # 1. 지문 함수 선주입 (down 본문보다 항상 앞에 위치)
    cat "$helper_file"
    printf '\n'

    # 2. 실행 전 BEGIN, 끝에 COMMIT 이 down 파일에 있는지 확인하고 없으면 감싼다 (중복 BEGIN 금지)
    local has_begin=false
    local has_commit=false
    if grep -qiE '^[[:space:]]*BEGIN([[:space:]]*;)?([[:space:]]*)$' "$down_file"; then
        has_begin=true
    fi
    if grep -qiE '^[[:space:]]*COMMIT([[:space:]]*;)?([[:space:]]*)$' "$down_file"; then
        has_commit=true
    fi

    if [[ "$has_begin" != "true" ]]; then
        printf 'BEGIN;\n'
    fi

    cat "$down_file"
    printf '\n'

    if [[ "$has_commit" != "true" ]]; then
        printf 'COMMIT;\n'
    fi
}

if [[ "$mode" == "print" ]]; then
    build_down_sql
    exit 0
fi

# mode == execute
if [[ -z "${AADS_DOWN_APPROVED:-}" ]]; then
    echo "Error: 되돌림 실행을 위해서는 --execute 옵션과 AADS_DOWN_APPROVED=<승인 id> 환경변수가 모두 필요합니다." >&2
    exit 3
fi

build_down_sql | docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDATABASE"
