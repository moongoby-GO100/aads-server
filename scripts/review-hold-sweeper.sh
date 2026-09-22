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
ORIGIN_ADJUDICATION_RETRY_THRESHOLD="${REVIEW_ORIGIN_ADJUDICATION_RETRY_THRESHOLD:-3}"
ORIGIN_ADJUDICATION_TIMEOUT_MIN="${REVIEW_ORIGIN_ADJUDICATION_TIMEOUT_MIN:-120}"
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

# ─── SHARED-BLOCK BEGIN: job_diff_contract ────────────────────────────
# 이 블록은 pipeline-runner.sh 와 review-hold-sweeper.sh 에 **같은 본문으로**
# 복제된다. 공유 파일을 source 하지 않는 이유는 순환 의존 때문이다 —
# 러너 스크립트는 최상단에서 flock 을 잡고 말미에서 main 을 호출하므로,
# 스위퍼가 그것을 source 하면 스위퍼가 러너를 기동시킨다. 반대로 러너가
# 스위퍼를 source 하면 스위퍼의 flock/배치가 러너 안에서 돈다.
# 그래서 복제하되 갈라지지 못하게 막는다: 두 사본이 한 바이트라도 달라지면
# tests/unit/test_review_hold_commit_gap.py 가 즉시 실패한다.
#
# capture_job_diff_text
#   러너가 pipeline_jobs.git_diff 에 저장하는 값을 그대로 만든다
#   (base..HEAD 45000B + 미커밋 5000B, base 가 없거나 HEAD 와 같으면 50000B).
#   스위퍼는 이 함수로 워크트리를 다시 읽어 "검수받은 diff 와 같은가" 를 본다.
#   `|| true` 는 게으름이 아니다 — head 가 상한에서 읽기를 닫으면 git 은
#   SIGPIPE(141)로 죽고 pipefail 이 그것을 그대로 돌려준다. 여기서 값을 비우면
#   45KB 를 넘는 diff 가 통째로 사라진다. 잘린 앞부분은 이미 캡처돼 있다.
# normalize_job_diff
#   두 diff 가 같은 변경인지 비교하기 위한 정규화. blob 해시(index 줄)와
#   CR·줄끝 공백·빈 줄만 지운다. 내용 줄은 건드리지 않는다 — 여기서 과하게
#   지우면 "다른 변경" 이 "같다" 로 통과해 검수받지 않은 코드가 승인 큐로 샌다.
#   마지막 awk 는 끝줄 개행을 한 벌로 맞춘다. DB 에서 읽은 값(psql 이 개행을
#   덧붙인다)과 워크트리에서 읽은 값(명령치환이 개행을 지운다)을 그대로 해시하면
#   내용이 같아도 늘 달라진다.
# resolve_job_base_sha
#   러너의 pre_exec_sha(워크트리 생성 시점 HEAD)는 DB 에 남지 않는다.
#   워크트리는 origin/main 에서 detach 로 만들어지므로 분기점(merge-base)이
#   그 값과 같다.
capture_job_diff_text() {
    local repo="$1" base_sha="${2:-}"
    local head_sha="" diff_text="" uncommitted=""
    head_sha=$(git -C "$repo" rev-parse HEAD 2>/dev/null) || head_sha=""
    if [[ -n "$base_sha" && -n "$head_sha" && "$base_sha" != "$head_sha" ]]; then
        diff_text=$(git -C "$repo" diff "${base_sha}..${head_sha}" 2>/dev/null | head -c 45000) || true
        uncommitted=$(git -C "$repo" diff HEAD 2>/dev/null | head -c 5000) || true
        if [[ -n "${uncommitted//[[:space:]]/}" ]]; then
            diff_text="${diff_text}
${uncommitted}"
        fi
    else
        diff_text=$(git -C "$repo" diff HEAD 2>/dev/null | head -c 50000) || true
    fi
    printf '%s' "$diff_text"
    return 0
}

normalize_job_diff() {
    sed -e 's/\r$//' \
        -e 's/[[:space:]]*$//' \
        -e '/^index [0-9a-f]\{4,\}\.\./d' \
        -e '/^similarity index /d' \
        -e '/^dissimilarity index /d' \
      | sed -e '/^$/d' \
      | awk '{ print }'
    return 0
}

resolve_job_base_sha() {
    local repo="$1" base=""
    base=$(git -C "$repo" merge-base HEAD origin/main 2>/dev/null) || base=""
    [[ -n "$base" ]] || base=$(git -C "$repo" merge-base HEAD main 2>/dev/null) || base=""
    [[ "$base" =~ ^[0-9a-f]{40}$ ]] || base=""
    printf '%s' "$base"
    return 0
}
# ─── SHARED-BLOCK END: job_diff_contract ──────────────────────────────

# review_hold 산출물의 승인용 commit_hash 를 확정한다. 값의 출처는 오직 보존된
# 워크트리의 기존 HEAD 다 — 스위퍼는 새 리비전을 만들지 않는다.
# AI 검수가 승인한 것은 review_hold 진입 시점의 git_diff 이므로, 그 뒤 워크트리에
# 쌓인 변경을 스위퍼가 임의로 확정하면 검수받지 않은 내용이 승인 큐로 올라간다
# (AADS-SWEEPER-COMMITHASH-P0).
#
# dirty 워크트리는 예전에 여기서 끝났다. 승격도 종결도 하지 않고 review_hold 에
# 남겨 두었으므로 다음 주기에 같은 잡이 다시 뽑혀 같은 판정을 받았다 — 검수 LLM
# 비용만 쓰고 상태는 그대로인 무한 재검수다(AADS-REVIEWHOLD-DIRTY-STRAND-P0).
# 이제는 셋 중 하나로 반드시 끝낸다:
#   0  … 승격 가능(persisted SHA 또는 clean HEAD)
#   10 … 워크트리 변경 == 검수받은 diff → 러너에게 넘김(재검수 대상에서 빠짐)
#   11 … 구조적 terminal(산출물 없음 / diff drift) → 재검수 제외
#   1  … 판정 불가(일시적) → 다음 주기 재시도
RECOVERED_COMMIT_SHA=""
ensure_review_hold_commit() {
    local job_id="$1" score="${2:-0.0}" attempt="${3:-0}"
    local worktree_dir="/tmp/aads-wt-${job_id}"
    local current_sha persisted_sha reason note
    RECOVERED_COMMIT_SHA=""

    persisted_sha=$(db_query "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || persisted_sha=""
    if [[ "$persisted_sha" =~ ^[0-9a-f]{40}$ ]]; then
        RECOVERED_COMMIT_SHA="$persisted_sha"
        return 0
    fi

    if [[ ! -d "$worktree_dir" ]] || [[ "$(git -C "$worktree_dir" rev-parse --is-inside-work-tree 2>/dev/null || true)" != "true" ]]; then
        # 산출물이 없으면 재검수를 몇 번 더 돌려도 결과가 같다. 구조적 terminal.
        log "  REVIEW_HOLD_NO_ARTIFACT ${job_id} worktree=${worktree_dir} — 산출물 없음, 재검수 종결"
        terminate_review_hold "$job_id" "review_hold_no_artifact" \
            "산출물 워크트리 없음(${worktree_dir}) — 복구 경로가 없어 재검수 종결"
        return 11
    fi

    if [[ -n "$(git -C "$worktree_dir" status --porcelain 2>/dev/null)" ]]; then
        log "  REVIEW_HOLD_DIRTY ${job_id} worktree=${worktree_dir} — 검수받은 diff 와 대조 후 판정"
        # `|| dirty_rc=$?` 없이 그냥 부르면 errexit 가 비-0 반환에 스위퍼를 통째로
        # 끝낸다. 10/11 은 정상 판정이지 오류가 아니다.
        local dirty_rc=0
        review_hold_dirty_recovery "$job_id" "$worktree_dir" "$score" "$attempt" || dirty_rc=$?
        return "$dirty_rc"
    fi

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
    else
        reason="워크트리에 HEAD 리비전 없음"
    fi
    log "  REVIEW_HOLD_COMMIT_FAILED ${job_id} — ${reason}"

    note=$(sql_escape "[자동재검수] 승격 보류 — ${reason}")
    db_exec "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note}, updated_at=NOW()
             WHERE job_id='${job_id}' AND status='review_hold';"
    return 1
}

# 재검수에서 영구히 제외되는 종결. 상태를 error 로 굳히고 review_flag_category 를
# 비워 select 대상에서 빠지게 한다(둘 중 하나만으로도 빠지지만, 나중에 상태 조건이
# 바뀌어도 재검수로 되돌아오지 않도록 둘 다 건다).
terminate_review_hold() {
    local job_id="$1" detail="$2" reason="$3"
    local note
    note=$(sql_escape "[자동재검수] 종결(${detail}) — ${reason}")
    db_exec "UPDATE pipeline_jobs
             SET status='error', phase='${detail}',
                 error_detail='${detail}',
                 review_flag_category=NULL,
                 review_needs_retry=FALSE,
                 review_request_id=NULL,
                 review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note},
                 completed_at=NOW(), updated_at=NOW()
             WHERE job_id='${job_id}' AND status='review_hold';"
    # 종결된 작업의 원 세션 판정 요청이 뒤늦게 실행되면 같은 작업을 다시
    # 검토하는 잡음이 생긴다. 아직 소비되지 않은 durable reaction도 함께
    # fail-closed 처리한다. 완료된 reaction 감사 이력은 보존한다.
    db_exec "UPDATE chat_deferred_reactions
             SET status='failed',
                 error_message=COALESCE(error_message, 'review job terminated: ${detail}'),
                 claimed_by=NULL, lease_expires_at=NULL,
                 completed_at=COALESCE(completed_at, NOW()), updated_at=NOW()
             WHERE ohvis_task_id LIKE 'review-adjudication:${job_id}:%'
               AND status IN ('pending','claimed');"
    return 0
}

# dirty 워크트리 판정 — 계약 1~3.
#
# 검수받은 것은 DB 의 git_diff 다. 워크트리를 러너와 **같은 규칙으로** 다시 읽어
# (capture_job_diff_text) 같은 정규화를 건 뒤(normalize_job_diff) 해시를 비교한다.
#   같다  → 워크트리에는 검수받은 변경만 있다. 표시만 남기고 러너에게 넘긴다.
#           확정은 러너의 기존 커밋 경로가 한다 — 그 경로만 pre-commit 훅을 거친다.
#   다르다 → 검수 뒤에 누가 손댔거나 다른 작업이 섞였다. 승인 큐로 올릴 수 없고,
#           재검수를 반복해도 같은 결론이므로 terminal 로 끝낸다.
review_hold_dirty_recovery() {
    local job_id="$1" worktree_dir="$2" score="${3:-0.0}" attempt="${4:-0}"
    local base_sha stored_file live_file stored_hash live_hash note now_flag

    base_sha=$(resolve_job_base_sha "$worktree_dir")
    stored_file=$(mktemp /tmp/review-sweep-stored.XXXXXX)
    live_file=$(mktemp /tmp/review-sweep-live.XXXXXX)
    db_query "SELECT COALESCE(git_diff,'') FROM pipeline_jobs WHERE job_id='${job_id}';" > "$stored_file" 2>/dev/null || true
    capture_job_diff_text "$worktree_dir" "$base_sha" > "$live_file" 2>/dev/null || true

    stored_hash=$(normalize_job_diff < "$stored_file" | sha256sum | cut -d' ' -f1)
    live_hash=$(normalize_job_diff < "$live_file" | sha256sum | cut -d' ' -f1)
    local stored_bytes live_bytes
    stored_bytes=$(wc -c < "$stored_file" | tr -d '[:space:]')
    live_bytes=$(wc -c < "$live_file" | tr -d '[:space:]')
    rm -f "$stored_file" "$live_file"

    # 한쪽이 비어 있으면 "같다" 가 아니라 "비교하지 못했다" 다. 빈 diff 두 개의
    # 해시가 우연히 같다고 승격시키면 아무 변경 없는 잡이 승인 큐로 올라간다.
    if [[ "$stored_bytes" == "0" || "$live_bytes" == "0" ]]; then
        log "  REVIEW_HOLD_DIFF_UNCOMPARABLE ${job_id} stored=${stored_bytes}B live=${live_bytes}B — 다음 주기로 보류"
        note=$(sql_escape "[자동재검수] 복구 보류 — diff 대조 불가(stored=${stored_bytes}B live=${live_bytes}B)")
        db_exec "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note}, updated_at=NOW()
                 WHERE job_id='${job_id}' AND status='review_hold';"
        return 1
    fi

    if [[ "$stored_hash" != "$live_hash" ]]; then
        log "  REVIEW_HOLD_DIFF_DRIFT ${job_id} stored=${stored_hash:0:12} live=${live_hash:0:12} — 재검수 종결"
        terminate_review_hold "$job_id" "review_hold_diff_drift" \
            "워크트리 변경이 검수받은 git_diff 와 다름(stored=${stored_hash:0:12}/${stored_bytes}B live=${live_hash:0:12}/${live_bytes}B)"
        return 11
    fi

    # 동일 — 러너에게 넘긴다. review_flag_category 를 비워 재검수 대상에서 빼고,
    # phase 는 'review_hold' 그대로 둔다(대시보드 보드 상태가 phase 로 판정한다).
    note=$(sql_escape "[자동재검수] PASS score=${score} attempt=${attempt} — dirty 산출물 diff 일치(${live_hash:0:12}), 러너 커밋 경로 대기 ($(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M KST'))")
    db_exec "UPDATE pipeline_jobs
             SET review_verdict='APPROVE', review_score=${score},
                 review_flag_category=NULL, review_needs_retry=FALSE,
                 review_request_id=NULL,
                 error_detail='review_hold_recovery_pending',
                 review_retry_count=${attempt}, review_retry_last_at=NOW(),
                 review_feedback=COALESCE(review_feedback,'') || E'\n' || ${note},
                 updated_at=NOW()
             WHERE job_id='${job_id}' AND status='review_hold';"
    now_flag=$(db_query "SELECT COALESCE(error_detail,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || now_flag=""
    if [[ "$now_flag" != "review_hold_recovery_pending" ]]; then
        log "  REVIEW_HOLD_RECOVERY_WRITE_MISSED ${job_id} error_detail='${now_flag}' — 다음 주기 재시도"
        return 1
    fi
    log "  REVIEW_HOLD_RECOVERY_HANDOFF ${job_id} sha_base=${base_sha:0:12} — 러너 커밋 경로로 재진입 대기"
    return 10
}

# 재시도 추적 컬럼 — 멱등 생성
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_retry_count INTEGER NOT NULL DEFAULT 0;"
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_retry_last_at TIMESTAMPTZ;"
db_exec "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS review_request_id UUID;"

select_sql="SELECT job_id, project, COALESCE(review_retry_count,0), COALESCE(chat_session_id,''), COALESCE(review_request_id::text,''),
       COALESCE(error_detail,''),
       CASE WHEN error_detail='review_origin_adjudication_pending'
            THEN FLOOR(EXTRACT(EPOCH FROM (NOW() - updated_at)) / 60)::bigint
       END
FROM pipeline_jobs
WHERE status='review_hold'
  AND review_flag_category IN (${INFRA_CATEGORIES})
  AND COALESCE(git_diff,'') <> ''
  AND COALESCE(review_retry_count,0) < ${SWEEP_MAX_RETRY}
  AND (COALESCE(error_detail,'') <> 'review_origin_adjudication_pending'
       OR updated_at < NOW() - (${ORIGIN_ADJUDICATION_TIMEOUT_MIN}::text || ' minutes')::interval)
  AND (review_retry_last_at IS NULL
       OR review_retry_last_at < NOW() - ((LEAST(${SWEEP_BACKOFF_MAX_MIN},
            (${SWEEP_BACKOFF_BASE_MIN} * POWER(2, COALESCE(review_retry_count,0)))::int))::text || ' minutes')::interval)
-- 큰 diff 한 건이 복구 창을 독점하지 않도록 작은 것부터 처리한다.
ORDER BY length(COALESCE(git_diff,'')) ASC, updated_at ASC
LIMIT ${SWEEP_BATCH};"

total=0; promoted=0; rejected=0; retried=0; handed=0; terminated=0; consec_infra=0; consec_unreachable=0

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
        log "  REVIEW_RETRY_EXHAUSTED ${jid} project=${proj} retry=${nxt}/${SWEEP_MAX_RETRY}"
        terminate_review_hold "$jid" "review_failed" \
            "자동 재검수 상한(${SWEEP_MAX_RETRY}) 도달 — ${reason}"
        terminated=$((terminated + 1))
        return
    fi
    if [[ "$nxt" -ge "$ORIGIN_ADJUDICATION_RETRY_THRESHOLD" ]]; then
        log "  ORIGIN_THRESHOLD ${jid} — 연속 무응답 판정 상한 도달, 원 세션 판정 이관"
        enqueue_origin_adjudication "$jid" "$proj"
    fi
}

# 모델 재검수가 상한에 닿으면 작업을 영구 review_hold 로 방치하지 않는다.
# 원 세션과 job 의 tenant, commit SHA, 저장 diff SHA-256을 DB 안에서 한 번 더
# 결선하고 durable reaction 을 만든다. 같은 증거에 대한 pending/claimed 요청은
# ohvis_task_id 로 멱등 차단한다. 원 세션은 코드를 수정하거나 자동 승인하지 않고
# 검수 결과만 pipeline_review_adjudicate 로 중앙 상태 머신에 제출한다.
enqueue_origin_adjudication() {
    local jid="$1" proj="$2" deferred_id=""
    deferred_id=$(db_query "
WITH target AS (
    SELECT j.job_id, j.project, j.chat_session_id::uuid AS session_id,
           lower(j.commit_hash) AS commit_sha,
           encode(digest(j.git_diff, 'sha256'), 'hex') AS diff_sha256,
           length(j.git_diff) AS diff_bytes,
           'review-adjudication:' || j.job_id || ':' || lower(j.commit_hash) || ':' ||
             encode(digest(j.git_diff, 'sha256'), 'hex') AS dedupe_key
      FROM pipeline_jobs j
      JOIN chat_sessions s
        ON s.id::text = j.chat_session_id
       AND s.tenant_id = j.tenant_id
     WHERE j.job_id='${jid}'
       AND j.project='${proj}'
       AND j.status='review_hold'
       AND j.review_flag_category IN (${INFRA_CATEGORIES})
       AND COALESCE(j.review_retry_count,0) >= ${ORIGIN_ADJUDICATION_RETRY_THRESHOLD}
       AND COALESCE(j.error_detail,'') <> 'review_origin_adjudication_expired'
       AND COALESCE(j.commit_hash,'') ~ '^[0-9a-fA-F]{7,64}$'
       AND COALESCE(j.git_diff,'') <> ''
), marked AS (
    UPDATE pipeline_jobs j
       SET error_detail='review_origin_adjudication_pending', updated_at=NOW()
      FROM target t
     WHERE j.job_id=t.job_id AND j.status='review_hold'
    RETURNING t.*
)
INSERT INTO chat_deferred_reactions
       (session_id, system_message, ohvis_task_id, status, attempts, created_at, updated_at)
SELECT m.session_id,
       format('[시스템] Pipeline review 인프라가 자동 재검수 상한에 도달했습니다. 이 요청은 원 세션 read-only 판정입니다. 코드·Git·DB·배포를 변경하지 말고 저장된 instruction, git_diff, 테스트 증거만 검토하십시오. job_id=%s project=%s commit_sha=%s diff_sha256=%s diff_bytes=%s. 판정 후 pipeline_review_adjudicate(job_id, expected_commit_sha, expected_diff_sha256, verdict=APPROVE|REJECT|UNKNOWN, findings)를 정확히 1회 호출하십시오. APPROVE는 awaiting_approval까지만 이동하며 push·배포를 수행하지 않습니다.',
              m.job_id, m.project, m.commit_sha, m.diff_sha256, m.diff_bytes),
       m.dedupe_key, 'pending', 0, NOW(), NOW()
  FROM marked m
 WHERE NOT EXISTS (
       SELECT 1 FROM chat_deferred_reactions q
        WHERE q.ohvis_task_id=m.dedupe_key
          AND q.status IN ('pending','claimed')
 )
RETURNING id::text;" 2>/dev/null | tr -d '[:space:]') || deferred_id=""
    if [[ -n "$deferred_id" ]]; then
        handed=$((handed + 1))
        log "  ORIGIN_ADJUDICATION_ENQUEUED ${jid} project=${proj} deferred=${deferred_id:0:8}"
    else
        log "  ORIGIN_ADJUDICATION_DEDUPED_OR_BLOCKED ${jid} project=${proj}"
    fi
}

# 원 세션 판정에도 명시적인 시간 상한이 있다. 상한이 끝난 작업을 다시 검수
# 모델 백오프로 돌리면 원 세션과 모델 사이를 영구 순환하며 review_hold 건수가
# 줄지 않는다. 이미 만료 표식이 남은 기존 적체를 스위프 시작 시 먼저 종결한다.
expired_origin_rows=$(db_query "
SELECT job_id, project
  FROM pipeline_jobs
 WHERE status='review_hold'
   AND error_detail='review_origin_adjudication_expired'
 ORDER BY updated_at ASC
 LIMIT ${SWEEP_BATCH};" 2>/dev/null) || expired_origin_rows=""
if [[ "$DRY_RUN" == "1" ]]; then
    [[ -z "${expired_origin_rows//[[:space:]]/}" ]] || log "DRY_RUN expired origin adjudications present"
else
    while IFS=$'\x1e' read -r expired_job expired_project; do
        [[ -z "$expired_job" ]] && continue
        [[ "$expired_job" =~ ^runner-[0-9a-f]{6,32}$ ]] || continue
        terminate_review_hold "$expired_job" "review_origin_adjudication_expired" \
            "원 세션 판정 제한시간(${ORIGIN_ADJUDICATION_TIMEOUT_MIN}분) 만료 — 승인 없이 fail-closed 종결"
        terminated=$((terminated + 1))
        log "ORIGIN_ADJUDICATION_TERMINATED ${expired_job} project=${expired_project}"
    done <<< "$expired_origin_rows"
fi

# 이전 주기에 이미 임계치를 넘긴 작업도 백오프 만료를 기다리지 않고 먼저
# 원 세션으로 이관한다. 이렇게 해야 배포 직후 정책이 적용되면 기존 적체도 즉시
# 줄고, select_sql 의 일반 모델 재시도와 동시에 같은 작업을 잡지 않는다.
handoff_rows=$(db_query "
SELECT job_id, project
  FROM pipeline_jobs
 WHERE status='review_hold'
   AND review_flag_category IN (${INFRA_CATEGORIES})
   AND COALESCE(review_retry_count,0) >= ${ORIGIN_ADJUDICATION_RETRY_THRESHOLD}
   AND COALESCE(error_detail,'') NOT IN ('review_origin_adjudication_pending', 'review_origin_adjudication_expired')
 ORDER BY updated_at ASC
 LIMIT ${SWEEP_BATCH};" 2>/dev/null) || handoff_rows=""
if [[ "$DRY_RUN" == "1" ]]; then
    [[ -z "${handoff_rows//[[:space:]]/}" ]] || log "DRY_RUN origin adjudication candidates present"
else
    while IFS=$'\x1e' read -r handoff_job handoff_project; do
        [[ -z "$handoff_job" ]] && continue
        [[ "$handoff_job" =~ ^runner-[0-9a-f]{6,32}$ ]] || continue
        enqueue_origin_adjudication "$handoff_job" "$handoff_project"
    done <<< "$handoff_rows"
fi

if ! rows=$(db_query "$select_sql"); then
    log "review_hold 대상 조회 실패 — 대상 없음으로 오인하지 않고 중단합니다"
    exit 1
fi

if [[ -z "${rows//[[:space:]]/}" ]]; then
    log "no eligible review_hold job (infra category, backoff 만족); origin_handoff=${handed}"
    exit 0
fi

while IFS=$'\x1e' read -r job_id project retry_count session_id request_id error_detail origin_pending_age_min; do
    [[ -z "$job_id" ]] && continue
    # C1: job_id 형식 검증 — DB 값이라도 그대로 SQL/URL에 넣지 않는다.
    if [[ ! "$job_id" =~ ^runner-[0-9a-f]{6,32}$ ]]; then
        log "  SKIP invalid job_id format: ${job_id:0:40}"
        continue
    fi

    if [[ "$error_detail" == "review_origin_adjudication_pending" ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            log "ORIGIN_ADJUDICATION_EXPIRED ${job_id} project=${project} age_min=${origin_pending_age_min}"
        else
            expired_age_min=$(db_query "
WITH expired AS (
    SELECT job_id,
           FLOOR(EXTRACT(EPOCH FROM (NOW() - updated_at)) / 60)::bigint AS age_min
      FROM pipeline_jobs
     WHERE job_id='${job_id}'
       AND status='review_hold'
       AND error_detail='review_origin_adjudication_pending'
       AND updated_at < NOW() - (${ORIGIN_ADJUDICATION_TIMEOUT_MIN}::text || ' minutes')::interval
), marked AS (
    UPDATE pipeline_jobs j
       SET error_detail='review_origin_adjudication_expired', updated_at=NOW()
      FROM expired e
     WHERE j.job_id=e.job_id AND j.status='review_hold'
    RETURNING e.age_min
)
SELECT age_min FROM marked;" 2>/dev/null | tr -d '[:space:]') || expired_age_min=""
            if [[ -z "$expired_age_min" ]]; then
                log "  SKIP ${job_id} — origin adjudication 만료 상태 변경 누락"
                continue
            fi
            terminate_review_hold "$job_id" "review_origin_adjudication_expired" \
                "원 세션 판정 제한시간(${ORIGIN_ADJUDICATION_TIMEOUT_MIN}분) 만료(age=${expired_age_min}분) — 승인 없이 fail-closed 종결"
            terminated=$((terminated + 1))
            log "ORIGIN_ADJUDICATION_TERMINATED ${job_id} project=${project} age_min=${expired_age_min}"
            continue
        fi
    fi
    total=$((total + 1))

    diff_file=$(mktemp /tmp/review-sweep-diff.XXXXXX)
    ins_file=$(mktemp /tmp/review-sweep-ins.XXXXXX)
    payload_file=$(mktemp /tmp/review-sweep-payload.XXXXXX)
    resp_file=$(mktemp /tmp/review-sweep-resp.XXXXXX)

    db_query "SELECT COALESCE(git_diff,'') FROM pipeline_jobs WHERE job_id='${job_id}';" > "$diff_file" 2>/dev/null || true
    db_query "SELECT COALESCE(instruction,'') FROM pipeline_jobs WHERE job_id='${job_id}';" > "$ins_file" 2>/dev/null || true

    # 동일 모델이 2회 연속 NO_RESPONSE/PARSER_FAILURE면 다음 재검수에서 제외
    same_model_twice=$(db_query "SELECT CASE WHEN COUNT(*) = 2 AND MIN(model_used) = MAX(model_used) THEN 'yes' ELSE 'no' END FROM (SELECT model_used FROM code_reviews WHERE job_id='${job_id}' AND flag_category IN ('REVIEW_MODEL_NO_RESPONSE','REVIEW_PARSER_FAILURE') ORDER BY created_at DESC LIMIT 2) t" 2>/dev/null | tr -d '[:space:]' || echo "no")
    excluded_model=$(db_query "SELECT COALESCE(model_used,'') FROM code_reviews WHERE job_id='${job_id}' AND flag_category IN ('REVIEW_MODEL_NO_RESPONSE','REVIEW_PARSER_FAILURE') ORDER BY created_at DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [[ "$same_model_twice" == "yes" && -n "$excluded_model" ]]; then
        printf '\n[REVIEW_EXCLUDE_MODELS: %s]\n' "$excluded_model" >> "$ins_file"
        log "  MODEL_EXCLUDED ${job_id} model=${excluded_model}"
    fi

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
        -H 'X-Monitor-Key: internal-review-hold-sweeper' \
        -d @"$payload_file" \
        --connect-timeout 10 --max-time 20 2>/dev/null) || http_code="000"
    response_detail=$(jq -r '.detail // .error // empty' "$resp_file" 2>/dev/null | tr '\n' ' ' | head -c 300)

    verdict=""; score="0.0"; category=""; issues=""
    request_status=""
    if [[ "$http_code" == "202" ]]; then
        deadline=$((SECONDS + REVIEW_MAX_TIME))
        while (( SECONDS < deadline )); do
            poll_code=$(curl -4 -s --http1.1 -o "$resp_file" -w '%{http_code}' \
                "${AADS_API_URL}/api/v1/review/code-diff/requests/${request_id}" \
                -H 'X-Monitor-Key: internal-review-hold-sweeper' \
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
        infra_reason="enqueue_http=${http_code} request_status=${request_status:-unknown} verdict=${verdict:-none} detail=${response_detail:-none}"
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
        # AADS-STALE-TRIGGER-SUPPRESS-P1: the job may have reached a terminal
        # state while its asynchronous review request was in flight.  Do not
        # promote it or create an approval notification in that case.
        current_status=$(db_query "SELECT COALESCE(status,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || current_status=""
        if [[ "$current_status" == "done" || "$current_status" == "error" ]]; then
            log "  SWEEPER_SKIP_DONE job=${job_id} status=${current_status}"
            rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
            continue
        fi
        # 승격 전 산출물 확정. 0 이 아니면 이 잡은 이번 주기에 승격하지 않는다 —
        # 10(러너 인계) / 11(terminal 종결) 은 이미 헬퍼가 DB 에 기록했다.
        commit_gate_rc=0
        ensure_review_hold_commit "$job_id" "$score" "$next_retry" || commit_gate_rc=$?
        if [[ "$commit_gate_rc" -ne 0 ]]; then
            case "$commit_gate_rc" in
                10) handed=$((handed + 1)) ;;
                11) terminated=$((terminated + 1)) ;;
            esac
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
        if [[ "$next_retry" -ge "$ORIGIN_ADJUDICATION_RETRY_THRESHOLD" ]]; then
            log "  ORIGIN_THRESHOLD $job_id — 연속 무응답 판정 상한 도달, 원 세션 판정 이관"
            enqueue_origin_adjudication "$job_id" "$project"
        fi
    fi

    rm -f "$diff_file" "$ins_file" "$payload_file" "$resp_file"
done <<< "$rows"

log "sweep done: scanned=${total} promoted=${promoted} rejected=${rejected} retried=${retried} handoff=${handed} terminal=${terminated}"
