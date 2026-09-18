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
SWEEP_MAX_RETRY="${SWEEP_MAX_RETRY:-10}"             # 잡당 자동 재검수 상한 (CEO 지시 2026-09-17: 6→10)
SWEEP_BACKOFF_BASE_MIN="${SWEEP_BACKOFF_BASE_MIN:-10}"
SWEEP_BACKOFF_MAX_MIN="${SWEEP_BACKOFF_MAX_MIN:-360}"
# 연속으로 이만큼 인프라 사유 실패가 나오면 그때 배치를 멈춘다(진짜 회로 개방).
# 첫 실패에서 멈추면 "검수 인프라가 죽었다" 와 "이 작업 하나가 무겁다" 를
# 구분하지 못한다.
SWEEP_INFRA_CIRCUIT="${SWEEP_INFRA_CIRCUIT:-3}"
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

# NULL commit_hash 를 가진 review_hold 산출물에 승인용 commit_hash 를 채운다.
# 값의 출처는 오직 보존된 워크트리의 기존 HEAD 다 — 스위퍼는 새 커밋을 만들지
# 않는다. AI 검수가 승인한 diff 는 review_hold 진입 시점의 git_diff 컬럼이므로,
# 그 뒤 워크트리에 쌓인 미커밋 변경을 스위퍼가 임의로 커밋하면 검수받지 않은
# 내용이 승인 큐로 올라갈 수 있다(AADS-SWEEPER-COMMITHASH-P0). 워크트리가
# 없거나 dirty 하면 승격하지 않고 사유를 review_feedback 에 남긴다.
RECOVERED_COMMIT_SHA=""
ensure_review_hold_commit() {
    local job_id="$1"
    local worktree_dir="/tmp/aads-wt-${job_id}"
    local current_sha persisted_sha reason note
    RECOVERED_COMMIT_SHA=""

    persisted_sha=$(db_query "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || persisted_sha=""
    if [[ "$persisted_sha" =~ ^[0-9a-f]{40}$ ]]; then
        RECOVERED_COMMIT_SHA="$persisted_sha"
        return 0
    fi

    if [[ ! -d "$worktree_dir" ]] || [[ "$(git -C "$worktree_dir" rev-parse --is-inside-work-tree 2>/dev/null || true)" != "true" ]]; then
        reason="워크트리 없음(${worktree_dir}) — commit_hash 를 채울 수 없어 승격하지 않음"
        log "  REVIEW_HOLD_NO_ARTIFACT ${job_id} worktree=${worktree_dir} — 승격하지 않음"
    elif [[ -n "$(git -C "$worktree_dir" status --porcelain 2>/dev/null)" ]]; then
        reason="워크트리 dirty(미커밋 변경 있음) — 검수받은 diff 와 다를 수 있어 승격하지 않음"
        log "  REVIEW_HOLD_DIRTY ${job_id} worktree=${worktree_dir} — 승격하지 않음"
    else
        current_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
        if [[ "$current_sha" =~ ^[0-9a-f]{40}$ ]]; then
            db_exec "UPDATE pipeline_jobs SET commit_hash='${current_sha}', updated_at=NOW()
                     WHERE job_id='${job_id}' AND status IN ('review_hold','awaiting_approval')
                       AND commit_hash IS NULL;"
            persisted_sha=$(db_query "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || persisted_sha=""
            if [[ "$persisted_sha" == "$current_sha" ]]; then
                RECOVERED_COMMIT_SHA="$current_sha"
                log "  REVIEW_HOLD_COMMIT_RECOVERED ${job_id} sha=${current_sha}"
                return 0
            fi
            reason="commit_hash DB 저장 확인 실패"
            log "  REVIEW_HOLD_COMMIT_FAILED ${job_id} — ${reason}"
        else
            reason="워크트리에 HEAD 커밋 없음"
            log "  REVIEW_HOLD_COMMIT_FAILED ${job_id} — ${reason}"
        fi
    fi

    note=$(sql_escape "[자동재검수] 승격 보류 — ${reason}")
    db_exec "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note}, updated_at=NOW()
             WHERE job_id='${job_id}' AND status='review_hold';"
    return 1
}

# 재시도 추적 컬럼 — 멱등 생성
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_retry_count INTEGER NOT NULL DEFAULT 0;"
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_retry_last_at TIMESTAMPTZ;"
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_request_id UUID;"

select_sql="SELECT job_id, project, COALESCE(review_retry_count,0), COALESCE(chat_session_id,''), COALESCE(review_request_id::text,'')
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

total=0; promoted=0; rejected=0; retried=0; consec_infra=0; consec_unreachable=0

# 인프라 사유 실패 처리 — 재시도 예산을 실제로 소비하고 다음 작업으로 넘어간다.
#
# 예전에는 카운터를 올리지 않고(retry budget preserved) batch 를 break 했다.
# 그 결과 백오프는 영원히 기본값 10분에 머물고 SWEEP_MAX_RETRY 에도 영영 닿지
# 않았으며, 매 주기 같은 첫 작업만 다시 시도하고 나머지는 스캔조차 되지 않았다.
# 2026-09-15 06:32~07:32 실측 — 21회 연속 `scanned=1 promoted=0 retried=0`,
# 그동안 GO100 4건이 손도 닿지 않은 채 review_hold 에 있었다.
infra_retry() {
    local jid="$1" proj="$2" prev="$3" reason="$4"
    local nxt=$((prev + 1))
    local nte
    nte=$(sql_escape "[자동재검수] 인프라 사유 재시도 ${nxt}/${SWEEP_MAX_RETRY} — ${reason}")
    db_exec "UPDATE pipeline_jobs
             SET review_retry_count=${nxt}, review_retry_last_at=NOW(),
                 review_request_id=NULL,
                 review_feedback=COALESCE(review_feedback,'') || E'\n' || ${nte}
             WHERE job_id='${jid}' AND status='review_hold';"
    retried=$((retried + 1))
    consec_infra=$((consec_infra + 1))
    log "  INFRA_RETRY ${jid} project=${proj} retry=${nxt}/${SWEEP_MAX_RETRY} ${reason}"
    if [[ "$nxt" -ge "$SWEEP_MAX_RETRY" ]]; then
        log "  EXHAUSTED ${jid} — 자동 재검수 상한 도달, 수동 확인 필요"
    fi
}

while IFS=$'\x1e' read -r job_id project retry_count session_id request_id; do
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

    if [[ ! "$request_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
        request_id=$(cat /proc/sys/kernel/random/uuid)
        db_exec "UPDATE pipeline_jobs SET review_request_id='${request_id}'::uuid WHERE job_id='${job_id}' AND status='review_hold';"
    fi

    jq -n --rawfile d "$diff_file" --rawfile i "$ins_file" \
          --arg r "$request_id" --arg j "$job_id" --arg p "$project" \
          '{request_id:$r, job_id:$j, project:$p, diff:$d, instruction:$i}' > "$payload_file"

    # 요청은 먼저 DB에 저장되고 202로 즉시 반환된다. 이후에는 HTTP 응답 본문이
    # 아니라 request_id의 DB 상태만 폴링하므로 verdict 응답 유실이 없다.
    http_code=$(curl -4 -s --http1.1 -o "$resp_file" -w '%{http_code}' \
        -X POST "${AADS_API_URL}/api/v1/review/code-diff/requests" \
        -H 'Content-Type: application/json' \
        -d @"$payload_file" \
        --connect-timeout 10 --max-time 20 2>/dev/null) || http_code="000"

    verdict=""; score="0.0"; category=""; issues=""
    request_status=""
    if [[ "$http_code" == "202" ]]; then
        deadline=$((SECONDS + REVIEW_MAX_TIME))
        while (( SECONDS < deadline )); do
            poll_code=$(curl -4 -s --http1.1 -o "$resp_file" -w '%{http_code}' \
                "${AADS_API_URL}/api/v1/review/code-diff/requests/${request_id}" \
                --connect-timeout 5 --max-time 15 2>/dev/null) || poll_code="000"
            if [[ "$poll_code" == "200" ]]; then
                request_status=$(jq -r '.status // empty' "$resp_file" 2>/dev/null || echo "")
                [[ "$request_status" == "completed" || "$request_status" == "failed" ]] && break
            fi
            sleep "${REVIEW_POLL_INTERVAL:-3}"
        done
    fi
    if [[ "$request_status" == "completed" ]]; then
        verdict=$(jq -r '.verdict // empty' "$resp_file" 2>/dev/null || echo "")
        score=$(jq -r '.score // 0.0' "$resp_file" 2>/dev/null || echo "0.0")
        category=$(jq -r '.flag_category // empty' "$resp_file" 2>/dev/null || echo "")
        # issues 는 jsonb 컬럼인데 asyncpg 가 파싱하지 않아 API 가 "문자열" 로 돌려준다.
        # 예전 `join("; ")` 는 문자열에 대해 jq 오류를 내고 2>/dev/null 로 삼켜져 항상 빈 값이 됐다.
        # 그 결과 REQUEST_CHANGES 확정 반려에 사유가 한 줄도 남지 않아 재제출 지시서를 쓸 수 없었다
        # (2026-09-17 runner-65233eb7 실측). 문자열이면 한 번 더 파싱한다.
        issues=$(jq -r '(.issues // []) | (if type=="string" then (fromjson? // []) else . end) | if type=="array" then join("; ") else tostring end' "$resp_file" 2>/dev/null || echo "")
    fi
    [[ "$score" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || score="0.0"

    # 인프라 사유 실패는 재시도 예산을 소비하고 다음 작업으로 넘어간다. 연속
    # 실패가 SWEEP_INFRA_CIRCUIT 회 쌓였을 때만 "인프라가 죽었다" 로 보고 배치를
    # 멈춘다.
    infra_reason=""
    unreachable=0
    if [[ "$http_code" == "000" || -z "$http_code" ]]; then
        # enqueue 자체가 서버에 닿지 못한 경우(연결 실패) — 검수 모델이 답을
        # 못 준 것과 달리 "시도"가 아니었다. review_retry_count 를 올리면
        # 배포 컷오버 창처럼 연결이 잠깐 막히는 구간에서 정상 잡까지 재시도
        # 예산을 다 태우고 review_hold 에 영구 방치된다.
        unreachable=1
        infra_reason="enqueue_http=${http_code:-000} request_status=${request_status:-unknown} verdict=${verdict:-none}"
    elif [[ "$request_status" == "failed" ]]; then
        infra_reason="request_status=failed"
    elif [[ "$http_code" != "202" || -z "$verdict" ]]; then
        infra_reason="enqueue_http=${http_code} request_status=${request_status:-unknown} verdict=${verdict:-none}"
    elif [[ "$verdict" == "FLAG" && ",REVIEW_API_UNAVAILABLE,REVIEW_MODEL_NO_RESPONSE,REVIEW_PARSER_FAILURE,REVIEW_TIMEOUT," == *",${category},"* ]]; then
        infra_reason="category=${category}"
    fi

    if [[ "$unreachable" == "1" ]]; then
        db_exec "UPDATE pipeline_jobs SET review_retry_last_at=NOW() WHERE job_id='${job_id}' AND status='review_hold';"
        consec_unreachable=$((consec_unreachable + 1))
        log "  ENQUEUE_UNREACHABLE ${job_id} project=${project} ${infra_reason} (retry 미차감, consec=${consec_unreachable}/3)"
        rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
        if [[ "$consec_unreachable" -ge 3 ]]; then
            log "  API 도달 불가 — 이번 스위프 중단 (retry budget preserved; batch stopped)"
            break
        fi
        continue
    fi
    consec_unreachable=0

    if [[ -n "$infra_reason" ]]; then
        infra_retry "$job_id" "$project" "$retry_count" "$infra_reason"
        rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
        if [[ "$consec_infra" -ge "$SWEEP_INFRA_CIRCUIT" ]]; then
            log "  CIRCUIT_OPEN 연속 인프라 실패 ${consec_infra}회 — 이번 배치 중단, 다음 타이머 주기에 재확인"
            break
        fi
        continue
    fi

    # 판정을 받아냈다 — 인프라는 살아 있다.
    consec_infra=0
    next_retry=$((retry_count + 1))
    log "  REVIEW $job_id project=$project http=$http_code verdict=${verdict:-none} score=$score category=${category:-none} retry=${next_retry}/${SWEEP_MAX_RETRY}"

    if [[ "$verdict" == "APPROVE" ]]; then
        if ! ensure_review_hold_commit "$job_id"; then
            rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
            continue
        fi
        # 호스트 TZ 가 CEST 라 date 를 그대로 쓰면 "KST" 라벨이 7시간 어긋난다 (실측 2026-09-14).
        note=$(sql_escape "[자동재검수] PASS score=${score} attempt=${next_retry} ($(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M KST'))")
        db_exec "UPDATE pipeline_jobs
                 SET status='awaiting_approval', phase='awaiting_approval',
                     review_verdict='APPROVE', review_score=${score},
                     review_flag_category=NULL, review_needs_retry=FALSE,
                     review_request_id=NULL,
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
                     review_request_id=NULL,
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
                     review_request_id=NULL,
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
