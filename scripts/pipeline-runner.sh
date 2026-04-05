#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# AADS Pipeline Runner v2.1 — 호스트 독립 실행기
#
# 핵심 원칙: "코드 수정만 → 승인 → 커밋 → 푸시 → 빌드 → 배포"
# Claude Code는 코드 수정만 수행. 커밋/푸시/빌드/배포는 승인 후 Runner가 처리.
#
# DB(pipeline_jobs)에서 pending 작업을 감지하여 Claude Code CLI로 실행.
# aads-server 재시작과 완전히 독립. systemd로 관리.
#
# 보안: C1(SQL인젝션방지), C3(크래시복구), C4(원자적Job클레임),
#       H3(임시파일정리), H4(승인타임아웃), H5(재시도)
# ═══════════════════════════════════════════════════════════════════════
set -eo pipefail

# P1: 중복 실행 방지 — 이미 실행 중이면 즉시 종료
exec 9>/tmp/pipeline-runner.lock
if ! flock -n 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 이미 실행 중인 러너가 있습니다. 종료." >&2
    exit 0
fi

# ── 설정 ──────────────────────────────────────────────────────────────
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
# 비밀번호는 EnvironmentFile에서 로드 (systemd)
PGPASSWORD="${PGPASSWORD:-}"
export PGPASSWORD

POLL_INTERVAL="${POLL_INTERVAL:-5}"
AADS_API_URL="${AADS_API_URL:-http://127.0.0.1:8100}"
MAX_RUNTIME="${MAX_RUNTIME:-7200}"
MAX_RETRIES="${MAX_RETRIES:-2}"               # H5: Claude 실패 시 재시도 횟수
MAX_CONCURRENT_PER_PROJECT="${MAX_CONCURRENT_PER_PROJECT:-3}"  # 프로젝트당 동시 실행 수
APPROVAL_TIMEOUT_HOURS="${APPROVAL_TIMEOUT_HOURS:-24}"  # H4: 승인 대기 타임아웃
ARTIFACT_MAX_AGE_HOURS="${ARTIFACT_MAX_AGE_HOURS:-24}"  # H3: 임시파일 보존 시간
LOG_DIR="/var/log/aads-pipeline"
ARTIFACT_DIR="/tmp/aads_pipeline_artifacts"
RUNNER_HOSTNAME=$(hostname -s)

# Claude Code 인증: current.env (oat 키) 사용 — API 키(api03) 사용 금지
source ~/.claude/current.env 2>/dev/null || true
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LANGUAGE=en_US:en MANPATH=

# 프로젝트별 workdir 매핑
declare -A PROJECT_WORKDIR=(
    ["AADS"]="/root/aads/aads-server"
    ["KIS"]="/root/webapp"
    ["GO100"]="/root/kis-autotrade-v4"
    ["SF"]="/data/shortflow"
    ["NTV2"]="/srv/newtalk-v2"
)

# 프로젝트별 허용 목록 (M4: 화이트리스트 검증)
VALID_PROJECTS="AADS"

MAX_JOB_RUNTIME="${MAX_JOB_RUNTIME:-3600}"      # 단일 작업 최대 60분 (stale 방지)
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-300}"    # 5분마다 프로세스 생존 확인
STUCK_CHECK_INTERVAL="${STUCK_CHECK_INTERVAL:-300}"  # 좀비/stuck 감지 주기 (초, 기본 5분)
MIN_DISK_GB="${MIN_DISK_GB:-1}"                  # 최소 디스크 공간 (GB)

mkdir -p "$LOG_DIR" "$ARTIFACT_DIR"

# ── 유틸리티 ──────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/runner.log"; }

# Redis 잠금 해제 헬퍼 (graceful — 실패해도 진행)
_release_work_lock() {
    local project="$1" job_id="$2"
    curl -sf -X POST "${AADS_API_URL}/api/v1/ops/locks/work/release?project=${project}&session_id=${job_id}" 2>/dev/null || true
}
_release_deploy_lock() {
    local project="$1" job_id="$2"
    curl -sf -X POST "${AADS_API_URL}/api/v1/ops/locks/deploy/release?project=${project}&session_id=${job_id}" 2>/dev/null || true
}

# DB 접속 방식
DB_MODE="${DB_MODE:-auto}"
PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"

_init_db_mode() {
    if [[ "$DB_MODE" == "auto" ]]; then
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "$PG_CONTAINER"; then
            DB_MODE="docker"
        else
            DB_MODE="psql"
        fi
    fi
    log "DB_MODE=$DB_MODE host=$RUNNER_HOSTNAME"
}

_psql_cmd() {
    if [[ "$DB_MODE" == "docker" ]]; then
        docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" "$@"
    else
        PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" "$@"
    fi
}

db_exec() {
    # FIX: ASCII Record Separator(0x1E)를 필드 구분자로 사용
    # instruction에 | 문자가 포함되면 IFS='|' 파싱이 깨지는 버그 수정
    local out
    out=$(_psql_cmd -t -A -F $'\x1e' -c "$1" 2>&1) || {
        _notify_db_failure "$1"
        return 1
    }
    echo "$out"
}

db_update() {
    _psql_cmd -c "$1" >/dev/null 2>&1
}

# P1: DB 연결 실패 감지 및 텔레그램 알림
_notify_db_failure() {
    local err_msg="$1"
    local bot="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
    [[ -z "$bot" || -z "$chat" ]] && return 0
    local COOLDOWN="/tmp/pipeline-db-fail.lock" now last=0
    now=$(date +%s)
    [[ -f "$COOLDOWN" ]] && last=$(cat "$COOLDOWN" 2>/dev/null || echo 0)
    if (( now - last > 300 )); then
        echo "$now" > "$COOLDOWN"
        log "❌ DB 연결 실패: $err_msg"
        curl -sf -X POST "https://api.telegram.org/bot${bot}/sendMessage" \
            -d chat_id="$chat" \
            -d text="🚨 [Pipeline Runner] DB 연결 실패 ($(hostname)): $err_msg" \
            -d parse_mode=HTML >/dev/null 2>&1 || true
    fi
}

# C1: SQL 안전 — dollar-quoting (내부에 $esc$가 없는 한 안전)
sql_escape() {
    local val="$1"
    # $esc$ 토큰이 포함되면 제거 (인젝션 방지)
    val="${val//\$esc\$/}"
    echo "\$esc\$${val}\$esc\$"
}

# ── 에러 분류 ─────────────────────────────────────────────────────────
classify_error() {
    local exit_code="$1" stderr_file="$2" stdout_file="$3"
    local err_content=""
    [[ -f "$stderr_file" ]] && err_content=$(tail -c 4000 "$stderr_file" 2>/dev/null)
    local out_tail=""
    [[ -f "$stdout_file" ]] && out_tail=$(tail -100 "$stdout_file" 2>/dev/null)
    local combined="${err_content}${out_tail}"

    if [[ $exit_code -eq 124 ]] || echo "$combined" | grep -qi "timed out\|operation timed out"; then
        echo "timeout"
    elif echo "$combined" | grep -qi "merge conflict\|CONFLICT\|git conflict"; then
        echo "git_conflict"
    elif echo "$combined" | grep -qi "SIGKILL\|kill -9\|Killed"; then
        echo "oom_killed"
    elif echo "$combined" | grep -qi "authentication\|unauthorized\| 401 "; then
        echo "auth_error"
    elif echo "$combined" | grep -qi "rate limit\|429\|quota exceeded"; then
        echo "rate_limit"
    elif echo "$combined" | grep -qi "No space left\|ENOSPC\|disk full"; then
        echo "disk_full"
    elif echo "$combined" | grep -qi "SyntaxError\|syntax error"; then
        echo "code_syntax_error"
    elif echo "$combined" | grep -qi "build fail\|compilation error\|ModuleNotFoundError"; then
        echo "build_fail"
    elif echo "$combined" | grep -qi "permission denied\|EACCES"; then
        echo "permission_denied"
    elif echo "$combined" | grep -qi "network\|connection refused\|ETIMEDOUT\|ECONNRESET"; then
        echo "network_error"
    elif [[ $exit_code -eq 137 || $exit_code -eq 139 ]]; then
        echo "oom_killed"
    else
        echo "unknown"
    fi
}

# ── 사전 검증 (Pre-validation) ─────────────────────────────────────────
pre_validate() {
    local job_id="$1" project="$2" session_id="$3"
    local workdir="${PROJECT_WORKDIR[$project]:-}"

    # 1) WORKDIR 존재 여부
    if [[ -z "$workdir" || ! -d "$workdir" ]]; then
        _fail_job "$job_id" "$session_id" "workdir_missing" "WORKDIR 없음: ${workdir:-undefined}"
        return 1
    fi

    # 2) 디스크 공간 확인 (최소 MIN_DISK_GB)
    local avail_kb
    avail_kb=$(df -k "$workdir" 2>/dev/null | tail -1 | awk '{print $4}')
    local min_kb=$((MIN_DISK_GB * 1024 * 1024))
    if [[ -n "$avail_kb" && "$avail_kb" -lt "$min_kb" ]]; then
        _fail_job "$job_id" "$session_id" "disk_full" "디스크 부족: ${avail_kb}KB < ${min_kb}KB (최소 ${MIN_DISK_GB}GB)"
        return 1
    fi

    # 3) git dirty 상태 → stash 후 진행
    cd "$workdir"
    if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
        log "  PRE_VALIDATE: git dirty → stash (job=$job_id)"
        git stash push -m "pipeline-runner-auto-stash-${job_id}" 2>/dev/null || true
    fi

    return 0
}

# 빠른 실패 헬퍼 — 에러 상태 전환 + error_detail 기록
_fail_job() {
    local job_id="$1" session_id="$2" error_type="$3" detail="$4"
    log "  FAIL_FAST job=$job_id type=$error_type: $detail"
    local safe_detail
    safe_detail=$(sql_escape "$detail")
    db_update "UPDATE pipeline_jobs SET status='error', phase='error',
               error_detail='${error_type}',
               result_output=${safe_detail},
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "❌ [Pipeline Runner] 사전 검증 실패 (${error_type}): ${detail:0:500}"
    _notify_ai "$job_id"
}

# ── 프로젝트 Lock 체크 (동시실행 방지) ──────────────────────────────────
# 같은 프로젝트에서 running/claimed 작업이 있으면 1(locked) 반환
check_project_lock() {
    local project="$1" exclude_job_id="$2"
    local running_count
    running_count=$(db_exec "SELECT count(*) FROM pipeline_jobs
                             WHERE project='${project}' AND status IN ('running','claimed')
                             AND job_id != '${exclude_job_id}';" 2>/dev/null)
    running_count="${running_count// /}"
    if [[ -n "$running_count" && "$running_count" -ge "${MAX_CONCURRENT_PER_PROJECT}" ]]; then
        echo "$running_count"
        return 1
    fi
    return 0
}

# 작업 완료/에러 후 같은 프로젝트의 다음 queued 작업을 자동 시작 대기열로 승격
promote_next_queued() {
    local project="$1"
    # running/claimed 작업이 아직 있으면 승격하지 않음
    local still_running
    still_running=$(db_exec "SELECT count(*) FROM pipeline_jobs
                             WHERE project='${project}' AND status IN ('running','claimed');" 2>/dev/null)
    still_running="${still_running// /}"
    if [[ -n "$still_running" && "$still_running" -ge "${MAX_CONCURRENT_PER_PROJECT}" ]]; then
        return 0
    fi

    # AADS-211: depends_on 체크 — 의존 작업이 done이 아닌 queued 작업은 스킵
    local next_job
    next_job=$(db_exec "SELECT job_id FROM pipeline_jobs
                        WHERE project='${project}' AND status='queued' AND phase='queued'
                          AND (depends_on IS NULL OR EXISTS (
                               SELECT 1 FROM pipeline_jobs dep
                               WHERE dep.job_id = pipeline_jobs.depends_on AND dep.status = 'done'))
                        ORDER BY COALESCE(priority, 0) DESC, created_at ASC LIMIT 1;" 2>/dev/null) || true
    next_job="${next_job// /}"
    if [[ -n "$next_job" ]]; then
        log "  PROMOTE_READY: 프로젝트 $project 의 다음 대기 작업 $next_job — 메인루프에서 곧 클레임"
    fi
}

# ── 중복 작업 확인 ─────────────────────────────────────────────────────
compute_instruction_hash() {
    echo -n "$1" | sha256sum | cut -d' ' -f1 | head -c 16
}

check_duplicate() {
    local job_id="$1" project="$2" instruction="$3"
    local inst_hash
    inst_hash=$(compute_instruction_hash "$instruction")

    # instruction_hash 저장
    db_update "UPDATE pipeline_jobs SET instruction_hash='${inst_hash}' WHERE job_id='${job_id}';"

    # 같은 프로젝트에서 running 상태 작업이 이미 있으면 → queued로 되돌림 (동시 실행 방지)
    local running_count
    if running_count=$(check_project_lock "$project" "$job_id"); then
        : # lock 없음 — 계속 진행
    else
        log "  LOCK: 프로젝트 $project 에 running 작업 ${running_count}개 — $job_id 를 queued로 되돌림"
        db_update "UPDATE pipeline_jobs SET status='queued', phase='queued', updated_at=NOW() WHERE job_id='${job_id}';"
        return 1
    fi

    # 중복 제출 방지: 10분 내 done이면 차단, 30분 내면 경고
    local dup_job
    dup_job=$(db_exec "SELECT job_id FROM pipeline_jobs
                       WHERE project='${project}'
                         AND instruction_hash='${inst_hash}'
                         AND job_id != '${job_id}'
                         AND (
                           status NOT IN ('done','error','rejected_done')
                           OR (status = 'done' AND updated_at > NOW() - INTERVAL '10 minutes')
                         )
                       LIMIT 1;" 2>/dev/null) || true
    if [[ -n "$dup_job" ]]; then
        dup_job="${dup_job// /}"
        # 10분 내 done이거나 아직 진행 중이면 차단
        local dup_status
        dup_status=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${dup_job}';" 2>/dev/null) || true
        dup_status="${dup_status// /}"
        if [[ "$dup_status" != "done" ]]; then
            log "  DEDUP_BLOCK: 동일 작업 진행 중: $dup_job ($dup_status) — $job_id 차단"
            db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='duplicate_blocked',
                       review_feedback=E'[중복 차단] 동일 작업 진행 중: ${dup_job} (${dup_status})',
                       updated_at=NOW() WHERE job_id='${job_id}';"
            return 1
        fi
        log "  DEDUP_WARN: 10분 내 동일 작업 완료: $dup_job (계속 실행하되 경고)"
        db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[DEDUP 경고] 유사 작업: ${dup_job}',
                   updated_at=NOW() WHERE job_id='${job_id}';"
    fi

    return 0
}

# ── 프로세스 생존 확인 (watchdog) ──────────────────────────────────────
_watchdog_check() {
    local filter="$1"
    # running 상태이면서 started_at이 MAX_JOB_RUNTIME 초과인 작업 → 타임아웃
    local timed_out
    timed_out=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                         error_detail='timeout_max_runtime',
                         review_feedback=COALESCE(review_feedback,'') || E'\n[Watchdog] 최대 실행시간 ${MAX_JOB_RUNTIME}s 초과 타임아웃',
                         updated_at=NOW()
                         WHERE status='running'
                           AND started_at IS NOT NULL
                           AND started_at < NOW() - INTERVAL '${MAX_JOB_RUNTIME} seconds'
                           $filter
                         RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$timed_out" ]]; then
        log "  WATCHDOG_TIMEOUT: $timed_out"
        # 타임아웃된 작업의 session_id 조회 → 채팅 알림 + AI 자동 반응
        for t_job in $timed_out; do
            t_job="${t_job// /}"
            [[ -z "$t_job" ]] && continue
            local t_session
            t_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${t_job}';" 2>/dev/null) || true
            t_session="${t_session// /}"
            post_to_chat "$t_session" "⏰ [Pipeline Runner] 작업 타임아웃 (${MAX_JOB_RUNTIME}초 초과): $t_job — 자동 종료됨"
            _notify_ai "$t_job"
            local t_project
            t_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${t_job}';" 2>/dev/null) || true
            promote_next_queued "${t_project// /}"
        done
    fi

    # running 상태이면서 runner_pid가 설정된 작업 — 프로세스 생존 확인
    local stale_rows
    stale_rows=$(db_exec "SELECT job_id, runner_pid FROM pipeline_jobs
                          WHERE status='running' AND runner_pid IS NOT NULL
                          $filter;" 2>/dev/null) || true

    if [[ -n "$stale_rows" ]]; then
        while IFS=$'\x1e' read -r s_job_id s_pid; do
            s_pid="${s_pid// /}"
            s_job_id="${s_job_id// /}"
            [[ -z "$s_job_id" || -z "$s_pid" ]] && continue
            # 프로세스가 죽었는지 확인
            if ! kill -0 "$s_pid" 2>/dev/null; then
                log "  WATCHDOG_DEAD_PROCESS: job=$s_job_id pid=$s_pid — error로 전환"
                db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                           error_detail='process_died',
                           review_feedback=COALESCE(review_feedback,'') || E'\n[Watchdog] Claude Code 프로세스(PID=${s_pid}) 죽음 감지',
                           updated_at=NOW() WHERE job_id='${s_job_id}' AND status='running';"
                # 채팅 알림 + AI 자동 반응 트리거
                local d_session
                d_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${s_job_id}';" 2>/dev/null) || true
                d_session="${d_session// /}"
                post_to_chat "$d_session" "💀 [Pipeline Runner] 프로세스 사망 감지 (PID=${s_pid}): $s_job_id — 자동 에러 처리됨"
                _notify_ai "$s_job_id"
                local d_project
                d_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${s_job_id}';" 2>/dev/null) || true
                promote_next_queued "${d_project// /}"
            fi
        done <<< "$stale_rows"
    fi
}

# C1: 채팅방 메시지 — session_id는 UUID 포맷 검증
post_to_chat() {
    local session_id="$1" content="$2"
    # UUID 포맷 검증 (C1: SQL 인젝션 방지)
    if [[ ! "$session_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
        log "  WARN: invalid session_id, skip chat post"
        return 0
    fi
    local safe_content
    safe_content=$(sql_escape "$content")
    db_update "INSERT INTO chat_messages (id, session_id, role, content, created_at)
               VALUES (gen_random_uuid(), '${session_id}'::uuid, 'assistant',
                       ${safe_content}, NOW());" || true
}

# C4: 원자적 Job 클레임 — UPDATE ... RETURNING으로 동시 실행 방지
# 프로젝트별 동시실행 Lock: 같은 프로젝트에 running/claimed 작업이 있으면 claim하지 않음
claim_queued_job() {
    local filter="$1"
    # instruction의 줄바꿈을 \\n으로 치환하여 단일행 RETURNING 보장
    # AADS-211: depends_on 체크 — 의존 작업이 done이 아니면 스킵
    db_exec "UPDATE pipeline_jobs SET status='claimed', updated_at=NOW()
             WHERE job_id = (
                SELECT p.job_id FROM pipeline_jobs p
                WHERE p.status='queued' AND p.phase='queued' $filter
                  AND (p.depends_on IS NULL OR EXISTS (
                       SELECT 1 FROM pipeline_jobs dep
                       WHERE dep.job_id = p.depends_on AND dep.status = 'done'))
                  AND (SELECT COUNT(*) FROM pipeline_jobs r
                       WHERE r.project = p.project
                         AND r.status IN ('running', 'claimed')
                         AND r.job_id != p.job_id) < ${MAX_CONCURRENT_PER_PROJECT:-3}
                ORDER BY COALESCE(p.priority, 0) DESC, p.created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, replace(replace(instruction, E'\\n', ' '), '|', ' '), chat_session_id, max_cycles, COALESCE(model, 'claude-sonnet-4-6');"
}

claim_approved_job() {
    local filter="$1"
    db_exec "UPDATE pipeline_jobs SET status='deploying', phase='deploying', updated_at=NOW()
             WHERE job_id = (
                SELECT job_id FROM pipeline_jobs
                WHERE status='approved' $filter
                ORDER BY updated_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, chat_session_id;"
}

claim_rejected_job() {
    local filter="$1"
    db_exec "UPDATE pipeline_jobs SET status='rolling_back', phase='rolling_back', updated_at=NOW()
             WHERE job_id = (
                SELECT job_id FROM pipeline_jobs
                WHERE status='rejected' $filter
                ORDER BY updated_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, chat_session_id;"
}

# ── 작업 실행 ─────────────────────────────────────────────────────────
run_job() {
    local job_id="$1" project="$2" instruction="$3" session_id="$4" max_cycles="$5" job_model="${6:-claude-sonnet-4-6}"
    local output_file="$ARTIFACT_DIR/${job_id}.out" err_file="$ARTIFACT_DIR/${job_id}.err"

    # 전역 변수 설정 — cleanup()에서 러너 종료 시 현재 작업을 에러로 마킹하기 위함
    _current_job_id="$job_id"
    _current_session_id="$session_id"
    # 서브셸 전파용 파일 기록 — 부모 셸 또는 재시작된 러너가 읽어 잔여 작업 정리
    echo "$job_id" > /tmp/.pipeline_current_job
    # 서브셸 전파용 파일 기록 — 부모 셸 또는 재시작된 러너가 읽어 잔여 작업 정리
    echo "$job_id" > /tmp/.pipeline_current_job

    # M4: 프로젝트 화이트리스트 검증
    if [[ ! " $VALID_PROJECTS " =~ " $project " ]]; then
        _fail_job "$job_id" "$session_id" "invalid_project" "허용되지 않은 프로젝트: $project"
        return 1
    fi

    # ── Redis 잠금 (1단계: 작업 잠금) ──
    local lock_result
    lock_result=$(curl -sf -X POST "${AADS_API_URL}/api/v1/ops/locks/work/acquire?project=${project}&session_id=${job_id}" 2>/dev/null) || true
    if echo "$lock_result" | grep -q '"acquired":false'; then
        local holder
        holder=$(echo "$lock_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('holder','unknown'))" 2>/dev/null) || holder="unknown"
        log "  REDIS_LOCK: $project 작업 중 (holder=$holder) — $job_id queued로 되돌림"
        db_update "UPDATE pipeline_jobs SET status='queued', phase='queued', updated_at=NOW() WHERE job_id='${job_id}';"
        return 0
    fi

    # ── 사전 검증 (Pre-validation) ──
    pre_validate "$job_id" "$project" "$session_id" || { _release_work_lock "$project" "$job_id"; return 1; }

    # ── 중복 작업 확인 ──
    check_duplicate "$job_id" "$project" "$instruction" || { _release_work_lock "$project" "$job_id"; return 0; }

    local workdir="${PROJECT_WORKDIR[$project]:-}"
    local use_worktree=false
    local worktree_dir=""

    # MAX_CONCURRENT_PER_PROJECT > 1이면 worktree 사용
    if [[ "${MAX_CONCURRENT_PER_PROJECT}" -gt 1 ]]; then
        worktree_dir="/tmp/aads-wt-${job_id}"
        local avail_gb
        avail_gb=$(df --output=avail -BG /tmp 2>/dev/null | tail -1 | tr -d ' G') || avail_gb=999
        if [[ "$avail_gb" -ge 5 ]]; then
            cd "$workdir"
            if git worktree add "$worktree_dir" HEAD 2>/dev/null; then
                workdir="$worktree_dir"
                use_worktree=true
                log "  WORKTREE: $worktree_dir 생성 (avail=${avail_gb}GB)"
            else
                log "  WORKTREE_FAIL: fallback to main workdir"
            fi
        else
            log "  WORKTREE_SKIP: 디스크 부족 ${avail_gb}GB < 5GB"
        fi
    fi

    log "▶ START job=$job_id project=$project workdir=$workdir"
    db_update "UPDATE pipeline_jobs SET status='running', phase='claude_code_work',
               started_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "🔧 [Pipeline Runner] 작업 시작: ${instruction:0:200}"

    # H5: 모델+계정 폴백 (같은 모델 2계정 시도 후 다음 모델)
    # AADS-206: job_model 기준 MODEL_CYCLE 구성 (지정 모델 우선, 폴백 유지)
    local MODEL_CYCLE
    if [[ "$job_model" == litellm:* ]]; then
        # LiteLLM Runner: 1회만 시도 (모델 폴백 없음)
        MODEL_CYCLE=("$job_model")
    elif [[ "$job_model" == "claude-haiku-4-5-20251001" ]]; then
        MODEL_CYCLE=("claude-haiku-4-5-20251001" "claude-haiku-4-5-20251001" "claude-sonnet-4-6" "claude-sonnet-4-6" "claude-opus-4-6" "claude-opus-4-6")
    elif [[ "$job_model" == "claude-opus-4-6" ]]; then
        MODEL_CYCLE=("claude-opus-4-6" "claude-opus-4-6" "claude-sonnet-4-6" "claude-sonnet-4-6" "claude-haiku-4-5-20251001" "claude-haiku-4-5-20251001")
    else
        MODEL_CYCLE=("claude-sonnet-4-6" "claude-sonnet-4-6" "claude-opus-4-6" "claude-opus-4-6" "claude-haiku-4-5-20251001" "claude-haiku-4-5-20251001")
    fi
    local TOKEN_CYCLE=("1" "2" "1" "2" "1" "2")  # 1=Naver, 2=Gmail
    local TOKEN_1="${ANTHROPIC_AUTH_TOKEN:-}"
    local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_2:-}"
    local total_attempts=${#MODEL_CYCLE[@]}  # 6회
    local attempt=0 exit_code=0
    while [[ $attempt -lt $total_attempts ]]; do
        exit_code=0
        cd "$workdir"
        local current_model="${MODEL_CYCLE[$attempt]}"
        local token_slot="${TOKEN_CYCLE[$attempt]}"
        local cycle_num=$(( attempt / 2 + 1 ))

        # 계정 스위치: 토큰 교체 (R-AUTH)
        # OAuth 토큰을 ANTHROPIC_API_KEY로 전달 → CLI가 x-api-key 헤더로 전송
        # CLAUDE_CODE_OAUTH_TOKEN 사용 금지 → OAuth Bearer flow 트리거 → 401
        if [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then
            export ANTHROPIC_API_KEY="$TOKEN_2"
            unset CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true
            log "  TOKEN_SWITCH job=$job_id → 계정2(Naver) via API_KEY"
        else
            export ANTHROPIC_API_KEY="$TOKEN_1"
            unset CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true
            [[ "$token_slot" == "2" ]] && log "  TOKEN_SWITCH job=$job_id → 계정2 없음, 계정1 유지"
        fi

        # H6: instruction 크기 제한 (50KB)
        local safe_instruction="${instruction:0:50000}"

        # H7: 빌드/배포 가드 v2.1 — Claude Code가 직접 배포하지 않도록 방지
        safe_instruction="[필수 규칙 — 반드시 준수]
1. 코드 수정만 수행하세요. 파일 생성/수정/삭제만 허용됩니다.
2. 다음 명령은 절대 실행하지 마세요:
   - docker build, docker compose, docker restart
   - npm run build, npm start, next build
   - supervisorctl, systemctl, service restart
   - kill, pkill (프로세스 종료)
3. 빌드와 배포는 CEO 승인 후 Runner가 자동으로 수행합니다.
4. 작업 완료 시 '빌드 필요' 또는 '배포 필요' 등을 언급하지 마세요. Runner가 알아서 합니다.
5. [R-AUTH] 인증 토큰 규칙:
   - AADS는 Auth Token(OAuth) 사용: ANTHROPIC_AUTH_TOKEN (sk-ant-oat01-...)
   - ANTHROPIC_API_KEY를 코드에서 직접 참조/추가 금지
   - 2계정 스위치: AUTH_TOKEN(1순위) → API_KEY_FALLBACK(2순위) → Gemini LiteLLM(3순위)
   - 외부 LLM(Gemini/DeepSeek): 반드시 LiteLLM 프록시 경유, 직접 REST API 호출 금지
   - 중앙 클라이언트: anthropic_client.py의 call_llm_with_fallback() 사용

위 규칙을 위반하면 작업이 거부됩니다.

${safe_instruction}"

        log "  MODEL_FALLBACK job=$job_id model=$current_model cycle=$cycle_num attempt=$((attempt+1))/$total_attempts"
        # LiteLLM Runner 분기 (litellm: 접두사)
        if [[ "$current_model" == litellm:* ]]; then
            local llm_model_name="${current_model#litellm:}"
            log "  LITELLM_RUNNER job=$job_id model=$llm_model_name"
            timeout "$MAX_RUNTIME" python3 /app/scripts/litellm_runner.py \
                --model "$llm_model_name" \
                --instruction "$safe_instruction" \
                --workdir "$workdir" \
                > "$output_file" 2> "$err_file" &
            local claude_pid=$!
        else
            timeout "$MAX_RUNTIME" claude --model "$current_model" -p --output-format text "$safe_instruction" \
                > "$output_file" 2> "$err_file" &
            local claude_pid=$!
        fi

        # runner_pid 기록 (watchdog 프로세스 생존 확인용)
        db_update "UPDATE pipeline_jobs SET runner_pid=${claude_pid}, updated_at=NOW() WHERE job_id='${job_id}';"

        wait $claude_pid || exit_code=$?

        if [[ $exit_code -eq 0 ]]; then
            break
        fi

        attempt=$((attempt + 1))
        if [[ $attempt -lt $total_attempts ]]; then
            local next_model="${MODEL_CYCLE[$attempt]}"
            local next_token="${TOKEN_CYCLE[$attempt]}"
            local acct_label="계정1(Naver)"
            [[ "$next_token" == "2" ]] && acct_label="계정2(Gmail)"
            local wait_sec=$(( 3 + attempt * 2 ))  # 5초~15초 점진 증가
            log "  RETRY job=$job_id attempt=$((attempt+1))/$total_attempts next=$next_model($acct_label) wait=${wait_sec}s exit=$exit_code"
            sleep "$wait_sec"
        fi
    done

    # runner_pid 클리어
    db_update "UPDATE pipeline_jobs SET runner_pid=NULL WHERE job_id='${job_id}';"

    local output=""
    [[ -f "$output_file" ]] && output=$(head -c 50000 "$output_file")

    if [[ $exit_code -ne 0 ]]; then
        # 에러 분류 (classify_error)
        local error_type
        error_type=$(classify_error "$exit_code" "$err_file" "$output_file")
        log "  FAIL job=$job_id exit=$exit_code type=$error_type attempts=$((attempt))"

        local err_content=""
        [[ -f "$err_file" ]] && err_content=$(tail -c 2000 "$err_file")
        local out_tail=""
        [[ -f "$output_file" ]] && out_tail=$(tail -100 "$output_file" | head -c 2000)

        local safe_output safe_feedback safe_detail
        safe_output=$(sql_escape "$output")
        safe_feedback=$(sql_escape "exit=$exit_code type=$error_type (${attempt}회 시도)
--- stderr (마지막 2KB) ---
$err_content
--- stdout (마지막 100줄) ---
$out_tail")

        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   error_detail='${error_type}',
                   result_output=${safe_output},
                   review_feedback=COALESCE(review_feedback,'') || E'\n' || ${safe_feedback},
                   updated_at=NOW() WHERE job_id='${job_id}';"
        post_to_chat "$session_id" "❌ [Pipeline Runner] 작업 실패 (${error_type}, exit=$exit_code, ${attempt}회 시도): ${err_content:0:500}"
        _release_work_lock "$project" "$job_id"
        _cleanup_artifacts "$job_id"
        # worktree 정리
        if [[ -d "/tmp/aads-wt-${job_id}" ]]; then
            cd "${PROJECT_WORKDIR[$project]:-/tmp}"
            git worktree remove "/tmp/aads-wt-${job_id}" --force 2>/dev/null || rm -rf "/tmp/aads-wt-${job_id}" 2>/dev/null || true
            log "  WORKTREE_CLEANUP: /tmp/aads-wt-${job_id}"
        fi
        _notify_ai "$job_id"
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    fi

    log "  DONE Phase1 job=$job_id"

    # v2.1: 커밋하지 않음 — uncommitted diff 캡처 (승인 후 커밋)
    cd "$workdir"
    local git_diff=""
    git_diff=$(git diff HEAD 2>/dev/null | head -c 50000) || true

    # ═══ AI Reviewer 단계 — CEO 승인 전 독립 AI 리뷰 ═══
    local review_verdict="APPROVE"
    local review_score="1.0"
    if [[ -n "$git_diff" && ${#git_diff} -gt 10 ]]; then
        log "  AI_REVIEW job=$job_id"
        local review_response=""
        # diff에서 변경 파일 목록 추출
        local changed_files=""
        changed_files=$(echo "$git_diff" | grep '^diff --git' | sed 's/diff --git a\///' | sed 's/ b\/.*//' | tr '\n' ',' | sed 's/,$//')

        # JSON body 생성 (jq 사용)
        local review_body=""
        review_body=$(jq -n \
            --arg jid "$job_id" \
            --arg proj "$project" \
            --arg diff "$git_diff" \
            --arg inst "$instruction" \
            --arg files "$changed_files" \
            '{job_id: $jid, project: $proj, diff: $diff, instruction: $inst, files_changed: ($files | split(","))}')

        local review_http_code=""
        review_response=$(curl -4 -s -w "\n%{http_code}" -X POST "${AADS_API_URL}/api/v1/review/code-diff" \
            -H "Content-Type: application/json" \
            -d "$review_body" \
            --max-time 30 2>/dev/null) || true

        review_http_code=$(echo "$review_response" | tail -1)
        review_response=$(echo "$review_response" | sed '$d')

        if [[ "$review_http_code" == "200" ]] && [[ -n "$review_response" ]]; then
            review_verdict=$(echo "$review_response" | jq -r '.verdict // "APPROVE"')
            review_score=$(echo "$review_response" | jq -r '.score // "1.0"')
            log "  AI_REVIEW_RESULT job=$job_id verdict=$review_verdict score=$review_score"

            if [[ "$review_verdict" == "REQUEST_CHANGES" ]]; then
                local review_issues=""
                review_issues=$(echo "$review_response" | jq -r '.issues | join("; ")' 2>/dev/null || echo "")
                log "  AI_REVIEW_REQUEST_CHANGES job=$job_id issues=$review_issues"
                post_to_chat "$session_id" "🔍 [AI Reviewer] 코드 수정 요청 (score=${review_score}): ${review_issues:0:500}"
            fi
        else
            log "  AI_REVIEW_SKIP job=$job_id (HTTP ${review_http_code:-timeout})"
        fi
    fi

    db_update "UPDATE pipeline_jobs SET phase='awaiting_approval',
               status='awaiting_approval',
               result_output=$(sql_escape "$output"),
               git_diff=$(sql_escape "$git_diff"),
               updated_at=NOW() WHERE job_id='${job_id}';"

    local diff_summary="${git_diff:0:3000}"
    local review_badge=""
    if [[ "$review_verdict" == "APPROVE" ]]; then
        review_badge="✅ AI 리뷰 통과 (score=${review_score})"
    elif [[ "$review_verdict" == "REQUEST_CHANGES" ]]; then
        review_badge="⚠️ AI 리뷰 수정 권고 (score=${review_score})"
    elif [[ "$review_verdict" == "FLAG" ]]; then
        review_badge="🔴 AI 리뷰 경고 (score=${review_score})"
    fi

    post_to_chat "$session_id" "🔔 [Pipeline Runner] 작업 완료 — ${review_badge}

**작업**: ${instruction:0:200}
**변경사항**:
\`\`\`diff
${diff_summary}
\`\`\`

승인: pipeline_runner_approve(job_id='${job_id}', action='approve')"

    log "  AWAITING_APPROVAL job=$job_id"
    _release_work_lock "$project" "$job_id"
    _cleanup_artifacts "$job_id"

    # 채팅AI 자동 반응 트리거 — AI가 결과 확인 후 CEO에게 보고
    _notify_ai "$job_id"

    # awaiting_approval은 running이 아니므로 다음 queued 작업 승격 가능
    promote_next_queued "$project"

    # 전역 변수 클리어 — 작업 완료/대기 전환
    _current_job_id=""
    _current_session_id=""
    rm -f /tmp/.pipeline_current_job
}

# 채팅AI 자동 반응 트리거 — 작업 완료/실패 시 AI가 결과를 확인·검수·조치
_notify_ai() {
    local job_id="$1"
    # job_id 유효성 검사 — runner-{hash} 패턴만 허용 (db_exec UPDATE 태그 오염 방어)
    [[ ! "$job_id" =~ ^runner-[0-9a-zA-Z_-]+$ ]] && return 0
    # aads-server의 notify API 호출 (백그라운드, 실패해도 무시)
    # 동기 호출 (최대 10초) — 결과를 로그에 기록
    local notify_http_code
    notify_http_code=$(curl -4 -s -o /dev/null -w "%{http_code}" \
         -X POST "${AADS_API_URL}/api/v1/pipeline/jobs/${job_id}/notify" \
         -H "x-monitor-key: internal" \
         --max-time 10 2>/dev/null) || notify_http_code="fail"
    log "  NOTIFY_AI job=$job_id http=$notify_http_code"
}

# H3: 임시파일 정리
_cleanup_artifacts() {
    local job_id="$1"
    rm -f "$ARTIFACT_DIR/${job_id}.out" "$ARTIFACT_DIR/${job_id}.err" 2>/dev/null || true
    rm -f "/tmp/runner_alert_${job_id}_60" "/tmp/runner_alert_${job_id}_120" 2>/dev/null || true
}

# ── 승인된 작업 배포 ──────────────────────────────────────────────────
deploy_job() {
    local job_id="$1" project="$2" session_id="$3"
    local workdir="${PROJECT_WORKDIR[$project]:-}"
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "▶ DEPLOY job=$job_id project=$project"

    # Redis deploy lock 획득 (동시 배포 방지)
    local deploy_lock_result=""
    deploy_lock_result=$(curl -sf -X POST "${AADS_API_URL}/api/v1/ops/locks/deploy/acquire?project=${project}&session_id=${job_id}" 2>/dev/null) || true
    if echo "$deploy_lock_result" | grep -q '"acquired":false'; then
        log "  DEPLOY_LOCK_WAIT job=$job_id project=$project — 다른 배포 진행 중, 30초 후 재시도"
        sleep 30
        deploy_lock_result=$(curl -sf -X POST "${AADS_API_URL}/api/v1/ops/locks/deploy/acquire?project=${project}&session_id=${job_id}" 2>/dev/null) || true
        if echo "$deploy_lock_result" | grep -q '"acquired":false'; then
            log "  DEPLOY_LOCK_FAIL job=$job_id — 배포 스킵"
            post_to_chat "$session_id" "⚠️ [Pipeline Runner] 배포 락 획득 실패 (다른 배포 진행 중): $job_id"
            return 1
        fi
    fi

    post_to_chat "$session_id" "🚀 [Pipeline Runner] 배포 시작: $job_id"

    local main_workdir="${PROJECT_WORKDIR[$project]:-}"
    local worktree_dir="/tmp/aads-wt-${job_id}"

    # flock으로 같은 프로젝트 동시 배포 방지
    local lock_file="/tmp/pipeline-deploy-${project}.lock"
    (
        flock -w 300 200 || { log "  DEPLOY_LOCK_TIMEOUT: $project"; return 1; }

        if [[ -d "$worktree_dir" ]]; then
            cd "$worktree_dir"
            git add -A 2>/dev/null || true
            local diff_content
            diff_content=$(git diff --cached HEAD 2>/dev/null) || true
            if [[ -n "$diff_content" ]]; then
                cd "$main_workdir"
                echo "$diff_content" | git apply --3way 2>/dev/null || {
                    log "  WORKTREE_MERGE_CONFLICT: $job_id"
                    cd "$worktree_dir"
                    git diff --cached --name-only HEAD 2>/dev/null | while read -r f; do
                        [[ -f "$worktree_dir/$f" ]] && cp "$worktree_dir/$f" "$main_workdir/$f" 2>/dev/null || true
                    done
                    cd "$main_workdir"
                }
                git add -A 2>/dev/null || true
            fi
            cd "$main_workdir"
        else
            cd "$main_workdir"
            git add -A 2>/dev/null || true
        fi

        # .py 파일 변경 여부 감지 (커밋 전 staged 변경 기준)
        local _py_changed="false"
        if git diff --cached --name-only HEAD 2>/dev/null | grep -q '\.py$'; then
            _py_changed="true"
        fi

        git commit -m "Pipeline-Runner: ${job_id}" 2>/dev/null || log "  WARN: git commit skipped (no changes or hook failure)"
        git push 2>/dev/null || true

        # _py_changed를 서브셸 밖으로 전달 (파일 경유)
        echo "$_py_changed" > "/tmp/pipeline-py-changed-${job_id}"

    ) 200>"$lock_file"

    cd "$main_workdir"

    # 서브셸에서 감지한 .py 변경 여부 읽기
    local _py_changed="false"
    if [[ -f "/tmp/pipeline-py-changed-${job_id}" ]]; then
        _py_changed=$(cat "/tmp/pipeline-py-changed-${job_id}" 2>/dev/null) || _py_changed="false"
        rm -f "/tmp/pipeline-py-changed-${job_id}" 2>/dev/null || true
    fi

    # ═══ 무중단 배포 v3.0 — build→swap→healthcheck→rollback ═══
    # 원칙: 빌드 중 기존 서비스 유지, 빌드 성공 후에만 교체, 실패 시 롤백
    case "$project" in
        AADS)
            # 1) aads-server: Blue-Green 무중단 배포 (deploy.sh bluegreen)
            log "  BLUEGREEN aads-server 무중단 배포 시작"
            if bash /root/aads/aads-server/deploy.sh bluegreen 2>&1 | tail -20; then
                log "  BLUEGREEN aads-server 완료"

                # Hot Module Reload — .py 파일 변경이 있을 때만 실행 (재시작 없이 즉시 반영)
                if [[ "$_py_changed" == "true" ]]; then
                    log "  HOT-RELOAD: .py 변경 감지 — 서비스 모듈 자동 리로드 시작"
                    local _hr_resp=""
                    _hr_resp=$(curl -s -m 10 -X POST \
                        -H "Content-Type: application/json" \
                        "http://127.0.0.1:8100/api/v1/ops/hot-reload" 2>/dev/null) || true
                    if [[ -n "$_hr_resp" ]]; then
                        local _hr_ok=""
                        _hr_ok=$(echo "$_hr_resp" | jq -r '.success // 0' 2>/dev/null) || true
                        log "  HOT-RELOAD: ${_hr_ok:-0}개 모듈 리로드 완료"
                    else
                        log "  HOT-RELOAD: WARN — 호출 실패 (서비스 정상 운영, 다음 요청 시 반영)"
                    fi
                else
                    log "  HOT-RELOAD: SKIP — .py 변경 없음 (yml/md 등 비Python 변경)"
                fi
            else
                log "  WARN: bluegreen 실패 — 기존 서비스 유지 (SSE 스트림 보호)"
                # supervisorctl restart 제거: 채팅 중 SSE 스트림 끊김 방지
            fi

            # 2) aads-dashboard: Docker 이미지 빌드 서비스 → build→swap
            if [ -n "$(git -C /root/aads/aads-dashboard status --porcelain 2>/dev/null)" ]; then
                log "  COMMIT aads-dashboard changes"
                git -C /root/aads/aads-dashboard add -A 2>/dev/null || true
                git -C /root/aads/aads-dashboard commit -m "Pipeline-Runner: ${job_id} (dashboard)" 2>/dev/null || true
                git -C /root/aads/aads-dashboard push 2>/dev/null || true
                DASHBOARD_CHANGED=true
            else
                DASHBOARD_CHANGED=false
            fi

            DASHBOARD_LAST_COMMIT=$(git -C /root/aads/aads-dashboard log -1 --format=%ct 2>/dev/null || echo 0)
            CURRENT_TIME=$(date +%s)
            DIFF_SECONDS=$((CURRENT_TIME - DASHBOARD_LAST_COMMIT))
            if [ "$DASHBOARD_CHANGED" = true ] || [ "$DIFF_SECONDS" -lt 600 ]; then
                log "  ZERO-DOWNTIME aads-dashboard (${DIFF_SECONDS}s ago)"
                local _compose_file="/root/aads/aads-server/docker-compose.prod.yml"
                # Step 1: 이미지만 빌드 (기존 컨테이너 유지)
                if docker compose -f "$_compose_file" build aads-dashboard 2>&1 | tail -5; then
                    # Step 2: 빌드 성공 → 컨테이너 교체
                    docker compose -f "$_compose_file" up -d --no-build aads-dashboard 2>/dev/null || true
                    log "  aads-dashboard zero-downtime swap complete"

                    # ── QA 자동 실행: 대시보드 배포 후 프론트엔드 검증 ──
                    log "  QA: 30초 대기 후 Visual QA 실행..."
                    sleep 30
                    local _qa_response=""
                    _qa_response=$(curl -s -m 60 -X POST \
                        -H "Content-Type: application/json" \
                        -d '{"pages": ["/", "/chat", "/ops"]}' \
                        "http://127.0.0.1:8100/api/v1/visual-qa/full-qa" 2>/dev/null) || true

                    if [ -z "$_qa_response" ]; then
                        log "  QA: WARN — QA API 호출 실패 (응답 없음), 배포는 계속 진행"
                        post_to_chat "$session_id" "⚠️ [Runner] QA API 호출 실패 — 배포는 정상 완료, QA 수동 확인 필요"
                    else
                        local _qa_verdict=""
                        _qa_verdict=$(echo "$_qa_response" | jq -r '.verdict // empty' 2>/dev/null) || true

                        if echo "$_qa_verdict" | grep -qi "FAIL"; then
                            local _qa_summary=""
                            _qa_summary=$(echo "$_qa_response" | jq -r '.summary // "상세 정보 없음"' 2>/dev/null) || true
                            log "  QA: FAIL — $_qa_verdict: $_qa_summary"
                            post_to_chat "$session_id" "🔴 [Runner] 프론트엔드 QA FAIL [$_qa_verdict]: $_qa_summary (롤백 없음, 수동 확인 필요)"

                            # 텔레그램 긴급 알림
                            if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
                                curl -s -m 10 -X POST \
                                    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                                    -d "chat_id=${TELEGRAM_CHAT_ID}" \
                                    -d "text=🔴 [AADS Runner] 프론트엔드 QA FAIL [$_qa_verdict]: ${_qa_summary}" \
                                    -d "parse_mode=HTML" 2>/dev/null || true
                            fi
                        elif echo "$_qa_verdict" | grep -qi "PASS"; then
                            log "  QA: PASS ✅ [$_qa_verdict]"
                            post_to_chat "$session_id" "✅ [Runner] 프론트엔드 QA PASS [$_qa_verdict] — 대시보드 배포 검증 완료"
                        elif echo "$_qa_verdict" | grep -qi "CEO\|CONDITIONAL"; then
                            local _qa_summary=""
                            _qa_summary=$(echo "$_qa_response" | jq -r '.summary // "상세 정보 없음"' 2>/dev/null) || true
                            log "  QA: CONDITIONAL — $_qa_verdict: $_qa_summary"
                            post_to_chat "$session_id" "⚠️ [Runner] 프론트엔드 QA 조건부 [$_qa_verdict]: $_qa_summary — CEO 확인 필요"
                        else
                            log "  QA: WARN — verdict 파싱 불가 ($_qa_verdict), 배포는 계속 진행"
                            post_to_chat "$session_id" "⚠️ [Runner] QA 결과 불명확 [$_qa_verdict] — 배포는 정상 완료, 수동 확인 필요"
                        fi
                    fi
                else
                    log "  WARN: aads-dashboard build failed — 기존 서비스 유지"
                    post_to_chat "$session_id" "⚠️ [Runner] 대시보드 빌드 실패 — 기존 버전 유지"
                fi
            else
                log "  SKIP aads-dashboard rebuild (no recent commits, last: ${DIFF_SECONDS}s ago)"
            fi
            ;;
        KIS)
            # KIS: systemd 서비스 → graceful restart (~2초)
            # kis-v41-api (port 8003), kis-webapp-api (port 8001)
            systemctl restart kis-v41-api 2>/dev/null || true
            log "  RESTART kis-v41-api"
            # webapp은 별도 workdir이므로 변경 감지
            if [ -n "$(git -C /root/webapp status --porcelain 2>/dev/null)" ]; then
                git -C /root/webapp add -A 2>/dev/null || true
                git -C /root/webapp commit -m "Pipeline-Runner: ${job_id} (webapp)" 2>/dev/null || true
                git -C /root/webapp push 2>/dev/null || true
                systemctl restart kis-webapp-api 2>/dev/null || true
                log "  RESTART kis-webapp-api"
            fi
            ;;
        GO100)
            # GO100 API: systemd → restart (~2초)
            systemctl restart go100 2>/dev/null || true
            log "  RESTART go100 api"
            # GO100 Frontend: npm build → restart (빌드 중 기존 서비스 유지)
            local _fe_dir="/root/kis-autotrade-v4/frontend"
            if [ -d "$_fe_dir" ]; then
                local _fe_changed=""
                _fe_changed=$(git -C /root/kis-autotrade-v4 diff HEAD --name-only -- frontend/ 2>/dev/null) || true
                if [ -n "$_fe_changed" ]; then
                    log "  ZERO-DOWNTIME go100-frontend build start"
                    cd "$_fe_dir"
                    # Step 1: 빌드 (기존 next start 프로세스 유지)
                    if npx next build 2>&1 | tail -5; then
                        # Step 2: 빌드 성공 → restart (새 .next/ 반영)
                        systemctl restart go100-frontend 2>/dev/null || true
                        log "  go100-frontend zero-downtime restart complete"
                    else
                        log "  WARN: go100-frontend build failed — 기존 서비스 유지"
                        post_to_chat "$session_id" "⚠️ [Runner] GO100 프론트엔드 빌드 실패 — 기존 버전 유지"
                    fi
                else
                    log "  SKIP go100-frontend (no frontend changes)"
                fi
            fi
            ;;
        SF)
            # ShortFlow: 볼륨마운트 서비스 → docker restart (~3초)
            local _sf_compose="/data/shortflow/docker-compose.yml"
            docker restart shortflow-worker 2>/dev/null || true
            docker restart shortflow-dashboard 2>/dev/null || true
            log "  RESTART shortflow-worker, shortflow-dashboard"
            # saas-dashboard: Docker 이미지 빌드 → build→swap
            local _saas_changed=""
            _saas_changed=$(git -C /data/shortflow diff HEAD --name-only -- saas-dashboard/ 2>/dev/null) || true
            if [ -n "$_saas_changed" ]; then
                log "  ZERO-DOWNTIME shortflow-saas-dashboard"
                if docker compose -f "$_sf_compose" build saas-dashboard 2>&1 | tail -5; then
                    docker compose -f "$_sf_compose" up -d --no-build saas-dashboard 2>/dev/null || true
                    log "  saas-dashboard zero-downtime swap complete"
                else
                    log "  WARN: saas-dashboard build failed — 기존 서비스 유지"
                fi
            fi
            ;;
        NTV2)
            # NTV2 Laravel: 볼륨마운트 → OPcache clear (다운타임 없음)
            local _ntv2_compose="/srv/newtalk-v2/docker-compose.yml"
            docker exec newtalk-v2-app php artisan optimize 2>/dev/null || true
            log "  OPTIMIZE newtalk-v2-app (OPcache clear)"
            # NTV2 Frontend: Docker 이미지 빌드 → build→swap
            local _ntv2_fe_changed=""
            _ntv2_fe_changed=$(git -C /srv/newtalk-v2 diff HEAD --name-only -- frontend/ 2>/dev/null) || true
            if [ -n "$_ntv2_fe_changed" ]; then
                log "  ZERO-DOWNTIME newtalk-v2-frontend"
                if docker compose -f "$_ntv2_compose" build frontend 2>&1 | tail -5; then
                    docker compose -f "$_ntv2_compose" up -d --no-build frontend 2>/dev/null || true
                    log "  newtalk-v2-frontend zero-downtime swap complete"
                else
                    log "  WARN: newtalk-v2-frontend build failed — 기존 서비스 유지"
                fi
            fi
            # Reverb: 볼륨마운트 → restart
            docker restart newtalk-v2-reverb 2>/dev/null || true
            log "  RESTART newtalk-v2-reverb"
            ;;
    esac

    # ═══ 헬스체크 (retry 루프 — 최대 60초, 5초 간격) ═══
    local health_ok="unknown"
    local health_url=""
    case "$project" in
        AADS)   health_url="http://localhost:8100/api/v1/health" ;;
        KIS)    health_url="http://localhost:8003/health" ;;
        GO100)  health_url="http://localhost:8002/health" ;;
        SF)     health_url="http://localhost:8000/health" ;;
        NTV2)   health_url="http://localhost:8080" ;;
    esac

    if [[ -n "$health_url" ]]; then
        health_ok="FAIL"
        for _retry in 1 2 3; do
            sleep 10
            if curl -sf -m 10 -o /dev/null "$health_url"; then
                health_ok="OK"
                break
            fi
            log "  헬스체크 재시도 ${_retry}/3 job=$job_id"
        done
    fi

    # ═══ 자동 롤백: health-check FAIL 시 이전 커밋으로 복구 ═══
    if [[ "$health_ok" == "FAIL" ]]; then
        log "  ROLLBACK_START job=$job_id project=$project — health-check 실패"
        post_to_chat "$session_id" "🔴 [Pipeline Runner] health-check 실패 — 자동 롤백 시작: $job_id"

        cd "$main_workdir" 2>/dev/null || cd "${PROJECT_WORKDIR[$project]:-}"
        if git revert --no-edit HEAD 2>/dev/null; then
            git push 2>/dev/null || true
            log "  ROLLBACK_REVERT: git revert HEAD 성공"

            case "$project" in
                AADS)
                    # 롤백도 무중단 배포 사용 (SSE 스트림 보호)
                    if bash /root/aads/aads-server/deploy.sh bluegreen 2>&1 | tail -10; then
                        log "  ROLLBACK_DEPLOY: bluegreen 성공"
                    else
                        log "  ROLLBACK_DEPLOY: bluegreen 실패 — 기존 서비스 유지"
                    fi
                    ;;
                KIS)
                    systemctl restart kis-v41-api 2>/dev/null || true
                    ;;
                GO100)
                    systemctl restart go100 2>/dev/null || true
                    ;;
                SF)
                    docker restart shortflow-worker 2>/dev/null || true
                    ;;
                NTV2)
                    docker exec newtalk-v2-app php artisan optimize 2>/dev/null || true
                    ;;
            esac

            sleep 10
            local rollback_health="FAIL"
            if [[ -n "$health_url" ]]; then
                if curl -sf -o /dev/null "$health_url" 2>/dev/null; then
                    rollback_health="OK"
                fi
            fi

            post_to_chat "$session_id" "↩️ [Pipeline Runner] 자동 롤백 완료 (롤백 후 health=${rollback_health}): $job_id"
            log "  ROLLBACK_DONE job=$job_id rollback_health=$rollback_health"

            if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
                curl -s -m 10 -X POST \
                    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                    -d "chat_id=${TELEGRAM_CHAT_ID}" \
                    -d "text=🔴 [Runner] 자동 롤백 실행: ${job_id} (${project}) — health=${rollback_health}" \
                    -d "parse_mode=HTML" 2>/dev/null || true
            fi

            db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='health_check_fail_rollback',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[자동롤백] health-check 실패 → git revert → rollback_health=${rollback_health}',
                       updated_at=NOW() WHERE job_id='${job_id}';"
            post_to_chat "$session_id" "🔴 [Pipeline Runner] 자동 롤백으로 에러 처리: $job_id"
            _release_deploy_lock "$project" "$job_id"
            _notify_ai "$job_id"
            promote_next_queued "$project"
            return 1
        else
            log "  ROLLBACK_REVERT_FAIL: git revert 실패 — 수동 복구 필요"
            _release_deploy_lock "$project" "$job_id"
            post_to_chat "$session_id" "🔴 [Pipeline Runner] 자동 롤백 실패 (git revert 불가) — 수동 복구 필요: $job_id"
            _notify_ai "$job_id"
            promote_next_queued "$project"
        fi
    fi

    db_update "UPDATE pipeline_jobs SET status='done', phase='done',
               review_feedback=COALESCE(review_feedback,'') || E'\n[v2.1][배포완료] health=${health_ok} by=${RUNNER_HOSTNAME}',
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "✅ [Pipeline Runner] 배포 완료 (health=${health_ok})"
    log "  DEPLOYED job=$job_id health=$health_ok"

    # Redis deploy lock 해제
    _release_deploy_lock "$project" "$job_id"

    # 채팅AI 자동 반응 트리거
    _notify_ai "$job_id"

    # worktree 정리
    if [[ -d "/tmp/aads-wt-${job_id}" ]]; then
        cd "${PROJECT_WORKDIR[$project]:-/tmp}"
        git worktree remove "/tmp/aads-wt-${job_id}" --force 2>/dev/null || rm -rf "/tmp/aads-wt-${job_id}" 2>/dev/null || true
        log "  WORKTREE_CLEANUP: /tmp/aads-wt-${job_id}"
    fi

    # 배포 완료 후 다음 queued 작업 승격
    promote_next_queued "$project"
}

# ── 거부된 작업 원복 ──────────────────────────────────────────────────
reject_job() {
    local job_id="$1" project="$2" session_id="$3"
    local workdir="${PROJECT_WORKDIR[$project]:-}"
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "▶ REJECT job=$job_id project=$project"
    cd "$workdir"

    # v2.2: 해당 Runner의 변경사항만 선택적 원복 (다른 Runner의 배포된 변경 보호)
    local worktree_dir="/tmp/aads-wt-${job_id}"
    if [[ -d "$worktree_dir" ]]; then
        cd "${PROJECT_WORKDIR[$project]:-/tmp}"
        git worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir" 2>/dev/null || true
        log "  REJECT_WORKTREE_CLEANUP: $worktree_dir"
    else
        local stash_msg="reject-${job_id}-$(date +%s)"
        git stash push -m "$stash_msg" 2>/dev/null || {
            log "  REJECT_STASH_FAIL: $job_id — git checkout fallback"
            git checkout -- . 2>/dev/null || true
            git clean -fd 2>/dev/null || true
        }
        log "  REJECT_STASH: $stash_msg (git stash list로 복구 가능)"
    fi

    db_update "UPDATE pipeline_jobs SET status='rejected_done', phase='rejected_done', updated_at=NOW() WHERE job_id='${job_id}';"
    _release_work_lock "$project" "$job_id"
    _release_deploy_lock "$project" "$job_id"
    post_to_chat "$session_id" "↩️ [Pipeline Runner] 거부된 작업 코드 원복 완료: $job_id"
    log "  REJECTED job=$job_id"

    # worktree 정리
    if [[ -d "/tmp/aads-wt-${job_id}" ]]; then
        cd "${PROJECT_WORKDIR[$project]:-/tmp}"
        git worktree remove "/tmp/aads-wt-${job_id}" --force 2>/dev/null || rm -rf "/tmp/aads-wt-${job_id}" 2>/dev/null || true
        log "  WORKTREE_CLEANUP: /tmp/aads-wt-${job_id}"
    fi

    # 거부 후 다음 queued 작업 승격
    promote_next_queued "$project"
}

# C3: 크래시 복구 — 시작 시 stuck 작업 정리
_recover_stuck_jobs() {
    local filter="$1"

    # BUG-7: 좀비 작업 강제 kill — running 상태 + MAX_RUNTIME(7200초) 초과 + runner_pid 존재
    local zombie_rows
    zombie_rows=$(db_exec "SELECT job_id, runner_pid, chat_session_id, project
                           FROM pipeline_jobs
                           WHERE status='running'
                             AND runner_pid IS NOT NULL
                             AND started_at IS NOT NULL
                             AND started_at < NOW() - INTERVAL '${MAX_RUNTIME} seconds'
                             $filter;" 2>/dev/null) || true
    if [[ -n "$zombie_rows" ]]; then
        while IFS=$'\x1e' read -r z_job z_pid z_session z_project; do
            z_job="${z_job// /}"
            z_pid="${z_pid// /}"
            z_session="${z_session// /}"
            z_project="${z_project// /}"
            [[ -z "$z_job" || -z "$z_pid" ]] && continue
            log "  ZOMBIE_KILL: job=$z_job pid=$z_pid — SIGTERM 전송"
            kill -15 "$z_pid" 2>/dev/null || true
            sleep 5
            if kill -0 "$z_pid" 2>/dev/null; then
                log "  ZOMBIE_KILL: job=$z_job pid=$z_pid — SIGTERM 무시, SIGKILL 전송"
                kill -9 "$z_pid" 2>/dev/null || true
            fi
            db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='zombie_killed',
                       runner_pid=NULL,
                       review_feedback=COALESCE(review_feedback,'') || E'\n[Zombie Kill] PID=${z_pid} SIGTERM→SIGKILL, MAX_RUNTIME=${MAX_RUNTIME}s 초과',
                       updated_at=NOW() WHERE job_id='${z_job}';"
            post_to_chat "$z_session" "💀 [Pipeline Runner] 좀비 작업 강제 종료 (PID=${z_pid}, ${MAX_RUNTIME}s 초과): $z_job"
            _notify_ai "$z_job"
            if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
                curl -s -m 10 -X POST \
                    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                    -d "chat_id=${TELEGRAM_CHAT_ID}" \
                    -d "text=💀 [Runner] 좀비 작업 강제 종료: ${z_job} (PID=${z_pid}, ${MAX_RUNTIME}s 초과)" \
                    -d "parse_mode=HTML" 2>/dev/null || true
            fi
            [[ -n "$z_project" ]] && promote_next_queued "$z_project"
        done <<< "$zombie_rows"
    fi

    # BUG-7: deploying 상태 10분 초과 → error 전환
    local deploy_timed_out
    deploy_timed_out=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                                error_detail='deploy_timeout',
                                review_feedback=COALESCE(review_feedback,'') || E'\n[Deploy Timeout] deploying 상태 10분 초과',
                                updated_at=NOW()
                                WHERE status='deploying'
                                  AND updated_at < NOW() - INTERVAL '10 minutes'
                                  $filter
                                RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$deploy_timed_out" ]]; then
        log "  DEPLOY_TIMEOUT: $deploy_timed_out"
        while IFS= read -r _dt_id; do
            _dt_id="${_dt_id// /}"
            [[ -z "$_dt_id" ]] && continue
            [[ ! "$_dt_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] && continue
            local _dt_session _dt_project
            _dt_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${_dt_id}';" 2>/dev/null) || true
            _dt_session="${_dt_session// /}"
            _dt_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${_dt_id}';" 2>/dev/null) || true
            _dt_project="${_dt_project// /}"
            post_to_chat "$_dt_session" "⏰ [Pipeline Runner] 배포 타임아웃 (10분 초과): $_dt_id — 자동 에러 처리됨"
            _notify_ai "$_dt_id"
            [[ -n "$_dt_project" ]] && promote_next_queued "$_dt_project"
        done <<< "$deploy_timed_out"
    fi

    # running/claimed 상태가 5분 이상 된 작업 → error로 전환 (BUG-7: 30분→5분 단축)
    local stuck
    stuck=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                     error_detail='stale_recovered',
                     review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 크래시 복구] ${RUNNER_HOSTNAME}',
                     updated_at=NOW()
                     WHERE status IN ('running','claimed')
                       AND updated_at < NOW() - INTERVAL '5 minutes'
                       $filter
                     RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$stuck" ]]; then
        log "  RECOVERED stuck jobs: $stuck"
        # 복구된 작업의 프로젝트별로 다음 queued 승격 + 채팅 알림
        while IFS= read -r _recovered_id; do
            _recovered_id="${_recovered_id// /}"
            [[ -z "$_recovered_id" ]] && continue
            [[ ! "$_recovered_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] && continue
            local _rec_project _rec_session
            _rec_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${_recovered_id}';" 2>/dev/null) || true
            _rec_project="${_rec_project// /}"
            _rec_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${_recovered_id}';" 2>/dev/null) || true
            _rec_session="${_rec_session// /}"
            post_to_chat "$_rec_session" "🔄 [Pipeline Runner] 장기 중단 작업 복구: $_recovered_id — 에러 처리됨"
            _notify_ai "$_recovered_id"
            [[ -n "$_rec_project" ]] && promote_next_queued "$_rec_project"
        done <<< "$stuck"
    fi

    # H4: 승인 대기 타임아웃
    local expired
    expired=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='approval_timeout',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[승인 타임아웃 ${APPROVAL_TIMEOUT_HOURS}h]',
                       updated_at=NOW()
                       WHERE status='awaiting_approval'
                         AND updated_at < NOW() - INTERVAL '${APPROVAL_TIMEOUT_HOURS} hours'
                         $filter
                       RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$expired" ]]; then
        log "  EXPIRED approval-timeout jobs: $expired"
        while IFS= read -r _exp_id; do
            _exp_id="${_exp_id// /}"
            [[ -z "$_exp_id" ]] && continue
            [[ ! "$_exp_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] && continue
            local _exp_session _exp_project
            _exp_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${_exp_id}';" 2>/dev/null) || true
            _exp_session="${_exp_session// /}"
            _exp_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${_exp_id}';" 2>/dev/null) || true
            _exp_project="${_exp_project///}"
            post_to_chat "$_exp_session" "⏰ [Pipeline Runner] 승인 타임아웃 (${APPROVAL_TIMEOUT_HOURS}시간 초과): $_exp_id — 자동 에러 처리됨"
            _notify_ai "$_exp_id"
            [[ -n "$_exp_project" ]] && promote_next_queued "$_exp_project"
        done <<< "$expired"
    fi
}

# H3: 오래된 임시파일 정리
_cleanup_old_artifacts() {
    find "$ARTIFACT_DIR" -type f -mmin +$((ARTIFACT_MAX_AGE_HOURS * 60)) -delete 2>/dev/null || true
}

# BUG-5: 소요시간 이상치 알림 — running 작업 60분/120분 초과 시 텔레그램 알림 (중복 방지 플래그)
_check_runtime_alerts() {
    local filter="$1"
    local running_rows
    running_rows=$(db_exec "SELECT job_id, chat_session_id, project,
                            FLOOR(EXTRACT(EPOCH FROM (NOW() - started_at))/60)::int
                            FROM pipeline_jobs
                            WHERE status='running'
                              AND started_at IS NOT NULL
                              $filter;" 2>/dev/null) || true
    [[ -z "$running_rows" ]] && return 0

    while IFS=$'\x1e' read -r r_job_id r_session r_project r_elapsed; do
        r_job_id="${r_job_id// /}"
        r_elapsed="${r_elapsed// /}"
        [[ -z "$r_job_id" || -z "$r_elapsed" ]] && continue
        [[ ! "$r_job_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] && continue
        [[ ! "$r_elapsed" =~ ^[0-9]+$ ]] && continue

        if [[ "$r_elapsed" -ge 120 ]]; then
            # 2차 경고 (120분 초과)
            local flag_120="/tmp/runner_alert_${r_job_id}_120"
            if [[ ! -f "$flag_120" ]]; then
                touch "$flag_120"
                log "  RUNTIME_ALERT_120 job=$r_job_id elapsed=${r_elapsed}m"
                post_to_chat "$r_session" "🚨 [Pipeline Runner] 2차 경고 — 작업 120분 초과 (${r_elapsed}분 경과): $r_job_id"
                if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
                    curl -s -m 10 -X POST \
                        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                        -d "chat_id=${TELEGRAM_CHAT_ID}" \
                        -d "text=🚨 [Runner] 2차 경고 — 작업 120분 초과: ${r_job_id} (${r_project}, ${r_elapsed}분 경과)" \
                        -d "parse_mode=HTML" 2>/dev/null || true
                fi
            fi
        elif [[ "$r_elapsed" -ge 60 ]]; then
            # 1차 알림 (60분 초과)
            local flag_60="/tmp/runner_alert_${r_job_id}_60"
            if [[ ! -f "$flag_60" ]]; then
                touch "$flag_60"
                log "  RUNTIME_ALERT_60 job=$r_job_id elapsed=${r_elapsed}m"
                post_to_chat "$r_session" "⚠️ [Pipeline Runner] 소요시간 이상 — 작업 60분 초과 (${r_elapsed}분 경과): $r_job_id"
                if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
                    curl -s -m 10 -X POST \
                        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                        -d "chat_id=${TELEGRAM_CHAT_ID}" \
                        -d "text=⚠️ [Runner] 작업 60분 초과: ${r_job_id} (${r_project}, ${r_elapsed}분 경과)" \
                        -d "parse_mode=HTML" 2>/dev/null || true
                fi
            fi
        fi
    done <<< "$running_rows"
}

# ── 메인 루프 ─────────────────────────────────────────────────────────
main() {
    _init_db_mode
    log "═══ Pipeline Runner v2.1 시작 (승인→커밋→푸시→빌드→배포) poll=${POLL_INTERVAL}s, max_runtime=${MAX_RUNTIME}s, retries=${MAX_RETRIES} ═══"

    # 프로젝트 필터 구성
    local project_filter=""
    if [[ -n "${RUNNER_PROJECTS:-}" ]]; then
        local _pf=""
        IFS=',' read -ra _projects <<< "$RUNNER_PROJECTS"
        for _p in "${_projects[@]}"; do
            [[ -n "$_pf" ]] && _pf="$_pf,"
            _pf="$_pf'$_p'"
        done
        project_filter="AND project IN ($_pf)"
        log "프로젝트 필터: $RUNNER_PROJECTS"
    fi

    # 파일 기반 잔여 job 정리 — 서브셸 전파 불가 문제 보완
    # 러너가 재시작될 때, 이전 실행에서 running 상태로 남은 작업을 즉시 error로 마킹
    if [ -f /tmp/.pipeline_current_job ]; then
        prev_job=$(cat /tmp/.pipeline_current_job)
        if [ -n "$prev_job" ]; then
            db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='runner_restarted',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 재시작으로 중단]',
                       updated_at=NOW() WHERE job_id='${prev_job}' AND status='running';" || true
            log "WARN: 이전 running 작업 $prev_job 을 error로 정리 (러너 재시작)"
        fi
        rm -f /tmp/.pipeline_current_job
    fi

    # 파일 기반 잔여 job 정리 — 서브셸 전파 불가 문제 보완
    # 러너가 재시작될 때, 이전 실행에서 running 상태로 남은 작업을 즉시 error로 마킹
    if [ -f /tmp/.pipeline_current_job ]; then
        prev_job=$(cat /tmp/.pipeline_current_job)
        if [ -n "$prev_job" ]; then
            db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='runner_restarted',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 재시작으로 중단]',
                       updated_at=NOW() WHERE job_id='${prev_job}' AND status='running';" || true
            log "WARN: 이전 running 작업 $prev_job 을 error로 정리 (러너 재시작)"
        fi
        rm -f /tmp/.pipeline_current_job
    fi

    # C3: 시작 시 stuck 작업 복구
    _recover_stuck_jobs "$project_filter"

    local _cycle=0
    # BUG-7: STUCK_CHECK_INTERVAL(기본 300초/5분) 기반 동적 cycle 계산
    local _stuck_check_cycles
    _stuck_check_cycles=$(( STUCK_CHECK_INTERVAL / POLL_INTERVAL ))
    [[ "$_stuck_check_cycles" -lt 1 ]] && _stuck_check_cycles=1
    log "STUCK_CHECK_INTERVAL=${STUCK_CHECK_INTERVAL}s → 매 ${_stuck_check_cycles} cycle마다 감지"
    while true; do
        # 글로벌 동시 작업 상한 체크 (전 서버 합산, rate limit 예방)
        local _running_count
        _running_count=$(db_exec "SELECT count(*) FROM pipeline_jobs WHERE status IN ('running','claimed');" 2>/dev/null) || _running_count="0"
        _running_count="${_running_count// /}"

        if [[ "$_running_count" -ge "${MAX_CONCURRENT_GLOBAL:-10}" ]]; then
            # 상한 도달 — 이번 사이클 대기
            if (( _cycle % 12 == 0 )); then
                log "  THROTTLE: ${_running_count}/${MAX_CONCURRENT_GLOBAL:-10} 동시 작업 — 대기"
            fi
            sleep "$POLL_INTERVAL"
            _cycle=$((_cycle + 1))
            continue
        fi

        # 방안A: 완료된 백그라운드 작업 정리
        _reap_bg_jobs

        # 1) queued 작업 원자적 클레임 (C4)
        local pending
        pending=$(claim_queued_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$pending" ]]; then
            # FIX: ASCII RS(0x1e) 구분자 사용 — instruction에 | 포함 시 파싱 깨짐 방지
            IFS=$'\x1e' read -r job_id project instruction session_id max_cycles job_model <<< "$pending"
            if [[ -n "$job_id" && -n "$project" ]]; then
                # 방안A: 백그라운드 병렬 실행 — 다른 프로젝트 작업이 블로킹하지 않음
                run_job "$job_id" "$project" "$instruction" "$session_id" "${max_cycles:-3}" "${job_model:-claude-sonnet-4-6}" &
                _bg_jobs[$!]="${job_id}|${session_id}"
                log "  BG_START: job=$job_id pid=$! (parallel)"
            fi
        fi

        # 2) approved 작업 원자적 클레임 (C4)
        local approved
        approved=$(claim_approved_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$approved" ]]; then
            # FIX: ASCII RS(0x1e) 구분자 사용
            IFS=$'\x1e' read -r job_id project session_id <<< "$approved"
            if [[ -n "$job_id" && -n "$project" ]]; then
                # 방안A: 백그라운드 병렬 실행
                deploy_job "$job_id" "$project" "$session_id" &
                _bg_jobs[$!]="${job_id}|${session_id}"
                log "  BG_DEPLOY: job=$job_id pid=$! (parallel)"
            fi
        fi

        # 3) rejected 작업 코드 원복
        local rejected
        rejected=$(claim_rejected_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$rejected" ]]; then
            IFS=$'\x1e' read -r job_id project session_id <<< "$rejected"
            if [[ -n "$job_id" && -n "$project" ]]; then
                reject_job "$job_id" "$project" "$session_id" &
                _bg_jobs[$!]="${job_id}|${session_id}"
                log "  BG_REJECT: job=$job_id pid=$! (parallel)"
            fi
        fi

        # 주기적 정리 (STUCK_CHECK_INTERVAL 초마다 — BUG-7: 동적 주기)
        _cycle=$((_cycle + 1))
        if (( _cycle % _stuck_check_cycles == 0 )); then
            _recover_stuck_jobs "$project_filter"
            _watchdog_check "$project_filter"
            _cleanup_old_artifacts
            _check_runtime_alerts "$project_filter"
        fi

        sleep "$POLL_INTERVAL"
    done
}

# ── 백그라운드 작업 추적 (방안A: 병렬 실행) ───────────────────────────
declare -A _bg_jobs   # PID -> "job_id|session_id"

_reap_bg_jobs() {
    for _pid in "${!_bg_jobs[@]}"; do
        if ! kill -0 "$_pid" 2>/dev/null; then
            wait "$_pid" 2>/dev/null || true
            unset '_bg_jobs[$_pid]'
        fi
    done
}

# ── 시그널 핸들링 ────────────────────────────────────────────────────
_current_job_id=""
_current_session_id=""
cleanup() {
    log "═══ Pipeline Runner v2.1 종료 ═══"
    # 방안A: 모든 백그라운드 작업 정리
    for _pid in "${!_bg_jobs[@]}"; do
        IFS='|' read -r _jid _sid <<< "${_bg_jobs[$_pid]}"
        kill "$_pid" 2>/dev/null || true
        wait "$_pid" 2>/dev/null || true
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   error_detail='runner_shutdown',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 종료로 중단]',
                   updated_at=NOW() WHERE job_id='${_jid}' AND status='running';" || true
        log "  Marked $_jid as error (runner shutdown)"
        post_to_chat "$_sid" "🔴 [Pipeline Runner] 러너 종료로 작업 중단: $_jid"
        _notify_ai "$_jid"
    done
    # 레거시 호환: 단일 작업 추적
    if [[ -n "$_current_job_id" ]] && ! printf '%s\n' "${_bg_jobs[@]}" | grep -q "$_current_job_id"; then
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   error_detail='runner_shutdown',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 종료로 중단]',
                   updated_at=NOW() WHERE job_id='${_current_job_id}' AND status='running';" || true
        log "  Marked $_current_job_id as error (runner shutdown)"
        post_to_chat "$_current_session_id" "🔴 [Pipeline Runner] 러너 종료로 작업 중단: $_current_job_id"
        _notify_ai "$_current_job_id"
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

main "$@"
