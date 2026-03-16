#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# AADS Pipeline Runner — 호스트 독립 실행기
#
# DB(pipeline_jobs)에서 pending 작업을 감지하여 Claude Code CLI로 실행.
# aads-server 재시작과 완전히 독립. systemd로 관리.
#
# Usage: systemctl start aads-pipeline-runner
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── 설정 ──────────────────────────────────────────────────────────────
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
PGPASSWORD="${PGPASSWORD:-aads_dev_local}"
export PGPASSWORD

POLL_INTERVAL="${POLL_INTERVAL:-5}"          # DB 폴링 간격 (초)
MAX_RUNTIME="${MAX_RUNTIME:-7200}"           # 작업 최대 실행 시간 (초, 2시간)
LOG_DIR="/var/log/aads-pipeline"
ARTIFACT_DIR="/tmp/aads_pipeline_artifacts"

# 프로젝트별 workdir 매핑
declare -A PROJECT_WORKDIR=(
    ["AADS"]="/root/aads/aads-server"
    ["KIS"]="/root/webapp"
)

# SSH 접속이 필요한 원격 프로젝트
declare -A PROJECT_SSH=(
    ["GO100"]="211.188.51.113:/root/go100"
    ["SF"]="114.207.244.86:7916:/data/shortflow"
    ["NTV2"]="114.207.244.86:7916:/srv/newtalk-v2"
    ["KIS_211"]="211.188.51.113:/root/webapp"
)

mkdir -p "$LOG_DIR" "$ARTIFACT_DIR"

# ── 유틸리티 ──────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/runner.log"; }

PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"

db_exec() {
    docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" \
         -t -A -c "$1" 2>/dev/null
}

db_update() {
    docker exec "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" \
         -c "$1" >/dev/null 2>&1
}

# 채팅방에 메시지 전송 (aads-server가 표시)
post_to_chat() {
    local session_id="$1" content="$2"
    # content에서 작은따옴표 이스케이프
    content="${content//\'/\'\'}"
    db_update "INSERT INTO chat_messages (id, session_id, role, content, created_at)
               VALUES (gen_random_uuid(), '${session_id}'::uuid, 'assistant',
                       '${content}', NOW());"
}

# ── 작업 실행 ─────────────────────────────────────────────────────────
run_job() {
    local job_id="$1" project="$2" instruction="$3" session_id="$4" max_cycles="$5"
    local workdir="" output_file="$ARTIFACT_DIR/${job_id}.out" err_file="$ARTIFACT_DIR/${job_id}.err"

    log "▶ START job=$job_id project=$project"

    # 상태 업데이트: running
    db_update "UPDATE pipeline_jobs SET status='running', phase='claude_code_work',
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "🔧 [Pipeline Runner] 작업 시작: ${instruction:0:200}"

    # workdir 결정
    workdir="${PROJECT_WORKDIR[$project]:-}"
    if [[ -z "$workdir" ]]; then
        log "  ERROR: unknown project $project"
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   result_output='알 수 없는 프로젝트: ${project}', updated_at=NOW()
                   WHERE job_id='${job_id}';"
        post_to_chat "$session_id" "❌ 알 수 없는 프로젝트: ${project}"
        return 1
    fi

    if [[ ! -d "$workdir" ]]; then
        log "  ERROR: workdir not found: $workdir"
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   result_output='workdir 없음: ${workdir}', updated_at=NOW()
                   WHERE job_id='${job_id}';"
        return 1
    fi

    # ── Phase 1: Claude Code 실행 ────────────────────────────────────
    local exit_code=0
    cd "$workdir"

    # Claude Code 실행 (타임아웃 적용)
    timeout "$MAX_RUNTIME" claude -p --output-format text "$instruction" \
        > "$output_file" 2> "$err_file" || exit_code=$?

    local output=""
    [[ -f "$output_file" ]] && output=$(head -c 50000 "$output_file")

    if [[ $exit_code -ne 0 ]]; then
        log "  FAIL job=$job_id exit=$exit_code"
        local err_content=""
        [[ -f "$err_file" ]] && err_content=$(tail -c 2000 "$err_file")
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   result_output=$(psql_escape "$output"),
                   review_feedback=$(psql_escape "exit=$exit_code: $err_content"),
                   updated_at=NOW() WHERE job_id='${job_id}';"
        post_to_chat "$session_id" "❌ [Pipeline Runner] 작업 실패 (exit=$exit_code): ${err_content:0:500}"
        return 1
    fi

    log "  DONE Phase1 job=$job_id"

    # ── Phase 2: git diff 캡처 ───────────────────────────────────────
    local git_diff=""
    git_diff=$(cd "$workdir" && git diff HEAD 2>/dev/null | head -c 50000) || true

    db_update "UPDATE pipeline_jobs SET phase='awaiting_approval',
               status='awaiting_approval',
               result_output=$(psql_escape "$output"),
               git_diff=$(psql_escape "$git_diff"),
               updated_at=NOW() WHERE job_id='${job_id}';"

    # 승인 요청
    local diff_summary="${git_diff:0:3000}"
    post_to_chat "$session_id" "🔔 [Pipeline Runner] 작업 완료 — CEO 승인 대기

**작업**: ${instruction:0:200}
**변경사항**:
\`\`\`diff
${diff_summary}
\`\`\`

승인하려면 채팅에서 'approve ${job_id}' 또는 API 호출하세요."

    log "  AWAITING_APPROVAL job=$job_id"
}

# SQL 안전 이스케이프 (psql dollar-quote)
psql_escape() {
    local val="$1"
    # dollar-quoting으로 안전하게 감싸기
    echo "\$esc\$${val}\$esc\$"
}

# ── 승인된 작업 배포 ──────────────────────────────────────────────────
deploy_job() {
    local job_id="$1" project="$2" session_id="$3"
    local workdir="${PROJECT_WORKDIR[$project]:-}"

    [[ -z "$workdir" ]] && return 1

    log "▶ DEPLOY job=$job_id project=$project"
    db_update "UPDATE pipeline_jobs SET phase='deploying', updated_at=NOW()
               WHERE job_id='${job_id}';"

    cd "$workdir"

    # git commit + push
    local commit_msg="Pipeline-Runner: ${job_id}"
    git add -u 2>/dev/null || true
    git commit -m "$commit_msg" --no-verify 2>/dev/null || true
    git push 2>/dev/null || true

    # 서비스 재시작 (프로젝트별)
    case "$project" in
        AADS)
            docker exec aads-server supervisorctl restart aads-api 2>/dev/null || true
            sleep 5
            ;;
        KIS)
            # KIS는 별도 재시작 불필요 (uvicorn --reload)
            ;;
    esac

    # 검증
    local health_ok="unknown"
    case "$project" in
        AADS)
            if curl -s -o /dev/null -w "%{http_code}" http://localhost:8100/api/v1/health | grep -q 200; then
                health_ok="OK"
            else
                health_ok="FAIL"
            fi
            ;;
    esac

    db_update "UPDATE pipeline_jobs SET status='done', phase='done',
               review_feedback=COALESCE(review_feedback,'') || E'\n[배포완료] health=${health_ok}',
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "✅ [Pipeline Runner] 배포 완료 (health=${health_ok})"
    log "  DEPLOYED job=$job_id health=$health_ok"
}

# ── 메인 루프 ─────────────────────────────────────────────────────────
main() {
    log "═══ Pipeline Runner 시작 (poll=${POLL_INTERVAL}s, max_runtime=${MAX_RUNTIME}s) ═══"

    while true; do
        # 1) pending(queued) 작업 감지
        local pending
        pending=$(db_exec "SELECT job_id, project, instruction, chat_session_id, max_cycles
                           FROM pipeline_jobs
                           WHERE status='queued' AND phase='queued'
                           ORDER BY created_at ASC LIMIT 1;" 2>/dev/null) || true

        if [[ -n "$pending" ]]; then
            IFS='|' read -r job_id project instruction session_id max_cycles <<< "$pending"
            if [[ -n "$job_id" ]]; then
                run_job "$job_id" "$project" "$instruction" "$session_id" "$max_cycles" || true
            fi
        fi

        # 2) approved 작업 감지 (CEO가 승인한 것)
        local approved
        approved=$(db_exec "SELECT job_id, project, chat_session_id
                            FROM pipeline_jobs
                            WHERE status='approved' AND phase='awaiting_approval'
                            ORDER BY updated_at ASC LIMIT 1;" 2>/dev/null) || true

        if [[ -n "$approved" ]]; then
            IFS='|' read -r job_id project session_id <<< "$approved"
            if [[ -n "$job_id" ]]; then
                deploy_job "$job_id" "$project" "$session_id" || true
            fi
        fi

        sleep "$POLL_INTERVAL"
    done
}

# ── 시그널 핸들링 ────────────────────────────────────────────────────
cleanup() {
    log "═══ Pipeline Runner 종료 ═══"
    exit 0
}
trap cleanup SIGTERM SIGINT

main "$@"
