#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# AADS review_hold 자동 재검수 스위퍼
#
# 배경: AI 검수 인프라 장애(리뷰 모델 무응답/타임아웃/파서 실패/리뷰 API 불가)로
#       review_hold에 묶인 작업은 auto_retryable=false 라서 사람이 손대기 전까지
#       영구 방치된다. 2026-09-13 기준 24시간 내 22건이 그렇게 쌓였고,
#       동일 diff를 수동 재검수하면 APPROVE로 통과했다.
#
# 원칙:
#   - 인프라 사유(REVIEW_API_UNAVAILABLE / REVIEW_MODEL_NO_RESPONSE /
#     REVIEW_PARSER_FAILURE / REVIEW_TIMEOUT)로 보류된 작업만 재검수한다.
#   - 코드 결함 반려(REQUEST_CHANGES)는 재검수 대상이 아니며, 재검수 중 나오면
#     review_failed 로 확정한다. 검수를 우회해 승인 큐로 올리지 않는다.
#   - 재검수 통과(APPROVE)만 awaiting_approval 로 전이한다. 최종 승인은 CEO 몫이다.
#   - 지수 백오프로 재시도하며, 상한을 넘으면 방치가 아니라 "재시도 소진"으로 남긴다.
#
# 실행: systemd timer(aads-review-hold-sweeper.timer) 주기 실행. 수동 1회 실행도 가능.
#       DRY_RUN=1 로 실행하면 DB를 변경하지 않고 대상만 출력한다.
# ═══════════════════════════════════════════════════════════════════════
set -eo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
PGPASSWORD="${PGPASSWORD:-}"
export PGPASSWORD

DB_MODE="${DB_MODE:-auto}"
PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
AADS_API_URL="${AADS_API_URL:-http://127.0.0.1:8100}"

SWEEP_BATCH="${SWEEP_BATCH:-5}"                      # 1회 실행당 재검수 건수
SWEEP_MAX_RETRY="${SWEEP_MAX_RETRY:-6}"              # 잡당 자동 재검수 상한
SWEEP_BACKOFF_BASE_MIN="${SWEEP_BACKOFF_BASE_MIN:-10}"
SWEEP_BACKOFF_MAX_MIN="${SWEEP_BACKOFF_MAX_MIN:-360}"
REVIEW_MAX_TIME="${REVIEW_MAX_TIME:-600}"            # 재검수 1건 최대 대기(초)
DRY_RUN="${DRY_RUN:-0}"
LOG_DIR="${LOG_DIR:-/var/log/aads-pipeline}"
LOCK_FILE="${SWEEP_LOCK_FILE:-/tmp/aads-review-hold-sweeper.lock}"

mkdir -p "$LOG_DIR" 2>/dev/null || true

# 중복 실행 방지 — 타이머 주기보다 재검수가 길어져도 겹치지 않는다.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] sweeper already running — skip" >&2
    exit 0
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/review-hold-sweeper.log"; }

INFRA_CATEGORIES="'REVIEW_API_UNAVAILABLE','REVIEW_MODEL_NO_RESPONSE','REVIEW_PARSER_FAILURE','REVIEW_TIMEOUT'"

if [[ "$DB_MODE" == "auto" ]]; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "$PG_CONTAINER"; then
        DB_MODE="docker"
    else
        DB_MODE="psql"
    fi
fi

_psql() {
    if [[ "$DB_MODE" == "docker" ]]; then
        docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" "$@"
    else
        psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" "$@"
    fi
}

db_query() { printf '%s' "$1" | _psql -q -t -A -P footer=off -F $'\x1e'; }
db_exec()  { printf '%s' "$1" | _psql -q >/dev/null 2>&1; }

# pipeline-runner.sh 와 동일한 달러 인용 방식 (SQL 인젝션 방지)
sql_escape() {
    local val="$1"
    val="${val//\$esc\$/}"
    printf '%s' "\$esc\$${val}\$esc\$"
}

# 재시도 추적 컬럼 — 멱등 생성
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_retry_count INTEGER NOT NULL DEFAULT 0;"
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_retry_last_at TIMESTAMPTZ;"

select_sql="SELECT job_id, project, COALESCE(review_retry_count,0), COALESCE(chat_session_id,'')
FROM pipeline_jobs
WHERE status='review_hold'
  AND review_flag_category IN (${INFRA_CATEGORIES})
  AND COALESCE(git_diff,'') <> ''
  AND COALESCE(review_retry_count,0) < ${SWEEP_MAX_RETRY}
  AND (review_retry_last_at IS NULL
       OR review_retry_last_at < NOW() - ((LEAST(${SWEEP_BACKOFF_MAX_MIN},
            (${SWEEP_BACKOFF_BASE_MIN} * POWER(2, COALESCE(review_retry_count,0)))::int))::text || ' minutes')::interval)
-- 큰 diff 한 건이 복구 창을 독점하지 않도록 작은 것부터 처리한다.
ORDER BY length(COALESCE(git_diff,'')) ASC, updated_at ASC
LIMIT ${SWEEP_BATCH};"

if ! rows=$(db_query "$select_sql"); then
    log "review_hold 대상 조회 실패 — 대상 없음으로 오인하지 않고 중단합니다"
    exit 1
fi

if [[ -z "${rows//[[:space:]]/}" ]]; then
    log "no eligible review_hold job (infra category, backoff 만족)"
    exit 0
fi

total=0; promoted=0; rejected=0; retried=0

while IFS=$'\x1e' read -r job_id project retry_count session_id; do
    [[ -z "$job_id" ]] && continue
    # C1: job_id 형식 검증 — DB 값이라도 그대로 SQL/URL에 넣지 않는다.
    if [[ ! "$job_id" =~ ^runner-[0-9a-f]{6,32}$ ]]; then
        log "  SKIP invalid job_id format: ${job_id:0:40}"
        continue
    fi
    total=$((total + 1))

    diff_file=$(mktemp /tmp/review-sweep-diff.XXXXXX)
    ins_file=$(mktemp /tmp/review-sweep-ins.XXXXXX)
    payload_file=$(mktemp /tmp/review-sweep-payload.XXXXXX)
    resp_file=$(mktemp /tmp/review-sweep-resp.XXXXXX)

    db_query "SELECT COALESCE(git_diff,'') FROM pipeline_jobs WHERE job_id='${job_id}';" > "$diff_file" 2>/dev/null || true
    db_query "SELECT COALESCE(instruction,'') FROM pipeline_jobs WHERE job_id='${job_id}';" > "$ins_file" 2>/dev/null || true

    if [[ ! -s "$diff_file" ]]; then
        log "  SKIP $job_id — git_diff 비어 있음"
        rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
        continue
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
        log "  DRY_RUN $job_id project=$project retry=$retry_count diff_bytes=$(wc -c < "$diff_file")"
        rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
        continue
    fi

    jq -n --rawfile d "$diff_file" --rawfile i "$ins_file" \
          --arg j "$job_id" --arg p "$project" \
          '{job_id:$j, project:$p, diff:$d, instruction:$i}' > "$payload_file"

    http_code=$(curl -4 -s --http1.1 -o "$resp_file" -w '%{http_code}' \
        -X POST "${AADS_API_URL}/api/v1/review/code-diff" \
        -H 'Content-Type: application/json' \
        -d @"$payload_file" \
        --connect-timeout 10 --max-time "$REVIEW_MAX_TIME" 2>/dev/null) || http_code="000"

    verdict=""; score="0.0"; category=""; issues=""
    if [[ "$http_code" == "200" && -s "$resp_file" ]]; then
        verdict=$(jq -r '.verdict // empty' "$resp_file" 2>/dev/null || echo "")
        score=$(jq -r '.score // 0.0' "$resp_file" 2>/dev/null || echo "0.0")
        category=$(jq -r '.flag_category // empty' "$resp_file" 2>/dev/null || echo "")
        issues=$(jq -r '(.issues // []) | join("; ")' "$resp_file" 2>/dev/null || echo "")
    fi
    [[ "$score" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || score="0.0"

    # 검수 인프라 전체가 죽은 경우 잡별 재시도 예산을 태우며 같은 장애를 배치
    # 전체에 반복하지 않는다. 첫 실패에서 회로를 열고 다음 타이머 주기에 재확인한다.
    if [[ "$http_code" != "200" || -z "$verdict" ]]; then
        log "  CIRCUIT_OPEN $job_id project=$project http=$http_code verdict=${verdict:-none} — retry budget preserved; batch stopped"
        break
    fi
    if [[ "$verdict" == "FLAG" && ",REVIEW_API_UNAVAILABLE,REVIEW_MODEL_NO_RESPONSE,REVIEW_PARSER_FAILURE,REVIEW_TIMEOUT," == *",${category},"* ]]; then
        log "  CIRCUIT_OPEN $job_id project=$project category=$category — retry budget preserved; batch stopped"
        break
    fi

    next_retry=$((retry_count + 1))
    log "  REVIEW $job_id project=$project http=$http_code verdict=${verdict:-none} score=$score category=${category:-none} retry=${next_retry}/${SWEEP_MAX_RETRY}"

    if [[ "$verdict" == "APPROVE" ]]; then
        note=$(sql_escape "[자동재검수] PASS score=${score} attempt=${next_retry} ($(date '+%Y-%m-%d %H:%M KST'))")
        db_exec "UPDATE pipeline_jobs
                 SET status='awaiting_approval', phase='awaiting_approval',
                     review_verdict='APPROVE', review_score=${score},
                     review_flag_category=NULL, review_needs_retry=FALSE,
                     error_detail=NULL,
                     review_retry_count=${next_retry}, review_retry_last_at=NOW(),
                     review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note},
                     updated_at=NOW()
                 WHERE job_id='${job_id}' AND status='review_hold';"
        promoted=$((promoted + 1))
        if [[ "$session_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
            msg=$(sql_escape "🔁 [자동재검수] ${job_id} 검수 인프라 장애로 보류됐던 작업이 재검수를 통과했습니다 (score=${score}). 승인 대기로 이동했습니다.")
            db_exec "INSERT INTO chat_messages (id, session_id, role, content, created_at)
                     VALUES (gen_random_uuid(), '${session_id}'::uuid, 'assistant', ${msg}, NOW());"
        fi
    elif [[ "$verdict" == "REQUEST_CHANGES" ]]; then
        # 인프라 장애가 아니라 실제 코드 반려 — 승인 큐로 올리지 않고 확정한다.
        note=$(sql_escape "[자동재검수] REQUEST_CHANGES score=${score} — ${issues:0:400}")
        detail=$(sql_escape "review_failed: verdict=REQUEST_CHANGES score=${score} source=auto_sweeper")
        cat_sql="NULL"
        [[ -n "$category" ]] && cat_sql="$(sql_escape "$category")"
        db_exec "UPDATE pipeline_jobs
                 SET status='error', phase='review_failed',
                     review_verdict='REQUEST_CHANGES', review_score=${score},
                     review_flag_category=${cat_sql},
                     review_needs_retry=FALSE,
                     error_detail=${detail},
                     review_retry_count=${next_retry}, review_retry_last_at=NOW(),
                     review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note},
                     updated_at=NOW()
                 WHERE job_id='${job_id}' AND status='review_hold';"
        rejected=$((rejected + 1))
    else
        # 여전히 인프라/무응답 — 백오프 카운터만 올린다.
        note=$(sql_escape "[자동재검수] 재시도 ${next_retry}/${SWEEP_MAX_RETRY} http=${http_code} verdict=${verdict:-none} category=${category:-none}")
        db_exec "UPDATE pipeline_jobs
                 SET review_retry_count=${next_retry}, review_retry_last_at=NOW(),
                     review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note}
                 WHERE job_id='${job_id}' AND status='review_hold';"
        retried=$((retried + 1))
        if [[ "$next_retry" -ge "$SWEEP_MAX_RETRY" ]]; then
            log "  EXHAUSTED $job_id — 자동 재검수 상한 도달, 수동 확인 필요"
        fi
    fi

    rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
done <<< "$rows"

log "sweep done: scanned=${total} promoted=${promoted} rejected=${rejected} retried=${retried}"
