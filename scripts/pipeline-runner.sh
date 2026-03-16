#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# AADS Pipeline Runner v2 — 호스트 독립 실행기
#
# DB(pipeline_jobs)에서 pending 작업을 감지하여 Claude Code CLI로 실행.
# aads-server 재시작과 완전히 독립. systemd로 관리.
#
# 보안: C1(SQL인젝션방지), C3(크래시복구), C4(원자적Job클레임),
#       H3(임시파일정리), H4(승인타임아웃), H5(재시도)
# ═══════════════════════════════════════════════════════════════════════
set -eo pipefail

# ── 설정 ──────────────────────────────────────────────────────────────
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
# 비밀번호는 EnvironmentFile에서 로드 (systemd)
PGPASSWORD="${PGPASSWORD:-}"
export PGPASSWORD

POLL_INTERVAL="${POLL_INTERVAL:-5}"
MAX_RUNTIME="${MAX_RUNTIME:-7200}"
MAX_RETRIES="${MAX_RETRIES:-2}"               # H5: Claude 실패 시 재시도 횟수
APPROVAL_TIMEOUT_HOURS="${APPROVAL_TIMEOUT_HOURS:-24}"  # H4: 승인 대기 타임아웃
ARTIFACT_MAX_AGE_HOURS="${ARTIFACT_MAX_AGE_HOURS:-24}"  # H3: 임시파일 보존 시간
LOG_DIR="/var/log/aads-pipeline"
ARTIFACT_DIR="/tmp/aads_pipeline_artifacts"
RUNNER_HOSTNAME=$(hostname -s)

# Claude Code 인증: current.env (oat 키) 사용 — API 키(api03) 사용 금지
source ~/.claude/current.env 2>/dev/null || true
if false; then
fi
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LANGUAGE=en_US:en MANPATH=

# 프로젝트별 workdir 매핑
declare -A PROJECT_WORKDIR=(
    ["AADS"]="/root/aads/aads-server"
    ["KIS"]="/root/webapp"
    ["GO100"]="/root/go100"
    ["SF"]="/data/shortflow"
    ["NTV2"]="/srv/newtalk-v2"
)

# 프로젝트별 허용 목록 (M4: 화이트리스트 검증)
VALID_PROJECTS="AADS KIS GO100 SF NTV2"

mkdir -p "$LOG_DIR" "$ARTIFACT_DIR"

# ── 유틸리티 ──────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/runner.log"; }

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
    _psql_cmd -t -A -c "$1" 2>/dev/null
}

db_update() {
    _psql_cmd -c "$1" >/dev/null 2>&1
}

# C1: SQL 안전 — dollar-quoting (내부에 $esc$가 없는 한 안전)
sql_escape() {
    local val="$1"
    # $esc$ 토큰이 포함되면 제거 (인젝션 방지)
    val="${val//\$esc\$/}"
    echo "\$esc\$${val}\$esc\$"
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
claim_queued_job() {
    local filter="$1"
    # instruction의 줄바꿈을 \\n으로 치환하여 단일행 RETURNING 보장
    db_exec "UPDATE pipeline_jobs SET status='claimed', updated_at=NOW()
             WHERE job_id = (
                SELECT job_id FROM pipeline_jobs
                WHERE status='queued' AND phase='queued' $filter
                ORDER BY created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, replace(instruction, E'\\n', ' '), chat_session_id, max_cycles;"
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

# ── 작업 실행 ─────────────────────────────────────────────────────────
run_job() {
    local job_id="$1" project="$2" instruction="$3" session_id="$4" max_cycles="$5"
    local output_file="$ARTIFACT_DIR/${job_id}.out" err_file="$ARTIFACT_DIR/${job_id}.err"

    # M4: 프로젝트 화이트리스트 검증
    if [[ ! " $VALID_PROJECTS " =~ " $project " ]]; then
        log "  ERROR: invalid project '$project'"
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   result_output='허용되지 않은 프로젝트', updated_at=NOW()
                   WHERE job_id='${job_id}';"
        return 1
    fi

    local workdir="${PROJECT_WORKDIR[$project]:-}"
    if [[ -z "$workdir" || ! -d "$workdir" ]]; then
        log "  ERROR: workdir not found for $project"
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   result_output=$(sql_escape "workdir 없음: ${workdir:-unknown}"),
                   updated_at=NOW() WHERE job_id='${job_id}';"
        return 1
    fi

    log "▶ START job=$job_id project=$project workdir=$workdir"
    db_update "UPDATE pipeline_jobs SET status='running', phase='claude_code_work',
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "🔧 [Pipeline Runner] 작업 시작: ${instruction:0:200}"

    # H5: 재시도 루프
    local attempt=0 exit_code=0
    while [[ $attempt -le $MAX_RETRIES ]]; do
        exit_code=0
        cd "$workdir"

        # H6: instruction 크기 제한 (50KB)
        local safe_instruction="${instruction:0:50000}"

        timeout "$MAX_RUNTIME" claude -p --output-format text "$safe_instruction" \
            > "$output_file" 2> "$err_file" || exit_code=$?

        if [[ $exit_code -eq 0 ]]; then
            break
        fi

        attempt=$((attempt + 1))
        if [[ $attempt -le $MAX_RETRIES ]]; then
            local wait_sec=$((2 ** attempt))
            log "  RETRY job=$job_id attempt=$attempt/$MAX_RETRIES wait=${wait_sec}s exit=$exit_code"
            sleep "$wait_sec"
        fi
    done

    local output=""
    [[ -f "$output_file" ]] && output=$(head -c 50000 "$output_file")

    if [[ $exit_code -ne 0 ]]; then
        log "  FAIL job=$job_id exit=$exit_code attempts=$((attempt))"
        local err_content=""
        [[ -f "$err_file" ]] && err_content=$(tail -c 2000 "$err_file")
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   result_output=$(sql_escape "$output"),
                   review_feedback=$(sql_escape "exit=$exit_code (${attempt}회 시도): $err_content"),
                   updated_at=NOW() WHERE job_id='${job_id}';"
        post_to_chat "$session_id" "❌ [Pipeline Runner] 작업 실패 (exit=$exit_code, ${attempt}회 시도): ${err_content:0:500}"
        _cleanup_artifacts "$job_id"
        _notify_ai "$job_id"
        return 1
    fi

    log "  DONE Phase1 job=$job_id"

    # git diff 캡처
    local git_diff=""
    git_diff=$(cd "$workdir" && git diff HEAD 2>/dev/null | head -c 50000) || true

    db_update "UPDATE pipeline_jobs SET phase='awaiting_approval',
               status='awaiting_approval',
               result_output=$(sql_escape "$output"),
               git_diff=$(sql_escape "$git_diff"),
               updated_at=NOW() WHERE job_id='${job_id}';"

    local diff_summary="${git_diff:0:3000}"
    post_to_chat "$session_id" "🔔 [Pipeline Runner] 작업 완료 — CEO 승인 대기

**작업**: ${instruction:0:200}
**변경사항**:
\`\`\`diff
${diff_summary}
\`\`\`

승인: pipeline_runner_approve(job_id='${job_id}', action='approve')"

    log "  AWAITING_APPROVAL job=$job_id"
    _cleanup_artifacts "$job_id"

    # 채팅AI 자동 반응 트리거 — AI가 결과 확인 후 CEO에게 보고
    _notify_ai "$job_id"
}

# 채팅AI 자동 반응 트리거 — 작업 완료/실패 시 AI가 결과를 확인·검수·조치
_notify_ai() {
    local job_id="$1"
    # aads-server의 notify API 호출 (백그라운드, 실패해도 무시)
    curl -4 -sf -X POST "http://127.0.0.1:8100/api/v1/pipeline/jobs/${job_id}/notify" \
         -H "x-monitor-key: internal" \
         --max-time 5 >/dev/null 2>&1 &
    log "  NOTIFY_AI job=$job_id"
}

# H3: 임시파일 정리
_cleanup_artifacts() {
    local job_id="$1"
    rm -f "$ARTIFACT_DIR/${job_id}.out" "$ARTIFACT_DIR/${job_id}.err" 2>/dev/null || true
}

# ── 승인된 작업 배포 ──────────────────────────────────────────────────
deploy_job() {
    local job_id="$1" project="$2" session_id="$3"
    local workdir="${PROJECT_WORKDIR[$project]:-}"
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "▶ DEPLOY job=$job_id project=$project"
    post_to_chat "$session_id" "🚀 [Pipeline Runner] 배포 시작: $job_id"

    cd "$workdir"

    # git commit + push
    git add -u 2>/dev/null || true
    git commit -m "Pipeline-Runner: ${job_id}" 2>/dev/null || true
    git push 2>/dev/null || true

    # 서비스 재시작 (프로젝트별)
    case "$project" in
        AADS)
            docker exec aads-server supervisorctl restart aads-api 2>/dev/null || true
            sleep 5
            ;;
        KIS)
            # uvicorn --reload
            ;;
        GO100)
            # uvicorn --reload
            ;;
    esac

    # 검증
    local health_ok="unknown"
    case "$project" in
        AADS)
            curl -sf -o /dev/null http://localhost:8100/api/v1/health && health_ok="OK" || health_ok="FAIL"
            ;;
        KIS)
            curl -sf -o /dev/null http://211.188.51.113:8080/health && health_ok="OK" || health_ok="FAIL"
            ;;
    esac

    db_update "UPDATE pipeline_jobs SET status='done', phase='done',
               review_feedback=COALESCE(review_feedback,'') || E'\n[배포완료] health=${health_ok} by=${RUNNER_HOSTNAME}',
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "✅ [Pipeline Runner] 배포 완료 (health=${health_ok})"
    log "  DEPLOYED job=$job_id health=$health_ok"

    # 채팅AI 자동 반응 트리거
    _notify_ai "$job_id"
}

# C3: 크래시 복구 — 시작 시 stuck 작업 정리
_recover_stuck_jobs() {
    local filter="$1"
    # running/claimed 상태가 30분 이상 된 작업 → error로 전환
    local stuck
    stuck=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                     review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 크래시 복구] ${RUNNER_HOSTNAME}',
                     updated_at=NOW()
                     WHERE status IN ('running','claimed')
                       AND updated_at < NOW() - INTERVAL '30 minutes'
                       $filter
                     RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$stuck" ]]; then
        log "  RECOVERED stuck jobs: $stuck"
    fi

    # H4: 승인 대기 타임아웃
    local expired
    expired=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[승인 타임아웃 ${APPROVAL_TIMEOUT_HOURS}h]',
                       updated_at=NOW()
                       WHERE status='awaiting_approval'
                         AND updated_at < NOW() - INTERVAL '${APPROVAL_TIMEOUT_HOURS} hours'
                         $filter
                       RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$expired" ]]; then
        log "  EXPIRED approval-timeout jobs: $expired"
    fi
}

# H3: 오래된 임시파일 정리
_cleanup_old_artifacts() {
    find "$ARTIFACT_DIR" -type f -mmin +$((ARTIFACT_MAX_AGE_HOURS * 60)) -delete 2>/dev/null || true
}

# ── 메인 루프 ─────────────────────────────────────────────────────────
main() {
    _init_db_mode
    log "═══ Pipeline Runner v2 시작 (poll=${POLL_INTERVAL}s, max_runtime=${MAX_RUNTIME}s, retries=${MAX_RETRIES}) ═══"

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

    # C3: 시작 시 stuck 작업 복구
    _recover_stuck_jobs "$project_filter"

    local _cycle=0
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

        # 1) queued 작업 원자적 클레임 (C4)
        local pending
        pending=$(claim_queued_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$pending" ]]; then
            IFS='|' read -r job_id project instruction session_id max_cycles <<< "$pending"
            if [[ -n "$job_id" && -n "$project" ]]; then
                run_job "$job_id" "$project" "$instruction" "$session_id" "${max_cycles:-3}" || true
            fi
        fi

        # 2) approved 작업 원자적 클레임 (C4)
        local approved
        approved=$(claim_approved_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$approved" ]]; then
            IFS='|' read -r job_id project session_id <<< "$approved"
            if [[ -n "$job_id" && -n "$project" ]]; then
                deploy_job "$job_id" "$project" "$session_id" || true
            fi
        fi

        # 주기적 정리 (60 cycle = ~5분마다)
        _cycle=$((_cycle + 1))
        if (( _cycle % 60 == 0 )); then
            _recover_stuck_jobs "$project_filter"
            _cleanup_old_artifacts
        fi

        sleep "$POLL_INTERVAL"
    done
}

# ── 시그널 핸들링 ────────────────────────────────────────────────────
_current_job_id=""
cleanup() {
    log "═══ Pipeline Runner v2 종료 ═══"
    # 현재 실행 중인 작업이 있으면 error로 마킹
    if [[ -n "$_current_job_id" ]]; then
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 종료로 중단]',
                   updated_at=NOW() WHERE job_id='${_current_job_id}' AND status='running';" || true
        log "  Marked $_current_job_id as error (runner shutdown)"
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

main "$@"
