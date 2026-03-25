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
VALID_PROJECTS="AADS KIS GO100 SF NTV2"

MAX_JOB_RUNTIME="${MAX_JOB_RUNTIME:-3600}"      # 단일 작업 최대 60분 (stale 방지)
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-300}"    # 5분마다 프로세스 생존 확인
MIN_DISK_GB="${MIN_DISK_GB:-1}"                  # 최소 디스크 공간 (GB)

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
    # FIX: ASCII Record Separator(0x1E)를 필드 구분자로 사용
    # instruction에 | 문자가 포함되면 IFS='|' 파싱이 깨지는 버그 수정
    _psql_cmd -t -A -F $'\x1e' -c "$1" 2>/dev/null
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

# ── 에러 분류 ─────────────────────────────────────────────────────────
classify_error() {
    local exit_code="$1" stderr_file="$2" stdout_file="$3"
    local err_content=""
    [[ -f "$stderr_file" ]] && err_content=$(tail -c 4000 "$stderr_file" 2>/dev/null)
    local out_tail=""
    [[ -f "$stdout_file" ]] && out_tail=$(tail -100 "$stdout_file" 2>/dev/null)

    if [[ $exit_code -eq 124 ]]; then
        echo "timeout"
    elif echo "$err_content" | grep -qi "merge conflict\|CONFLICT"; then
        echo "git_conflict"
    elif echo "$err_content" | grep -qi "build fail\|compilation error\|SyntaxError\|ModuleNotFoundError"; then
        echo "build_fail"
    elif echo "$err_content" | grep -qi "permission denied\|EACCES"; then
        echo "permission_denied"
    elif echo "$err_content" | grep -qi "No space left\|ENOSPC"; then
        echo "disk_full"
    elif echo "$err_content" | grep -qi "rate limit\|429\|quota exceeded"; then
        echo "rate_limit"
    elif echo "$err_content" | grep -qi "network\|connection refused\|ETIMEDOUT\|ECONNRESET"; then
        echo "network_error"
    elif [[ $exit_code -eq 137 || $exit_code -eq 139 ]]; then
        echo "claude_code_crash"
    elif [[ $exit_code -ne 0 ]]; then
        echo "claude_code_error_${exit_code}"
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
    running_count=$(db_exec "SELECT count(*) FROM pipeline_jobs
                             WHERE project='${project}' AND status IN ('running','claimed')
                             AND job_id != '${job_id}';" 2>/dev/null)
    running_count="${running_count// /}"
    if [[ -n "$running_count" && "$running_count" -gt 0 ]]; then
        log "  DEDUP: 프로젝트 $project 에 running 작업 ${running_count}개 — $job_id 를 queued로 되돌림"
        db_update "UPDATE pipeline_jobs SET status='queued', phase='queued', updated_at=NOW() WHERE job_id='${job_id}';"
        return 1
    fi

    # 최근 30분 내 동일 instruction_hash의 done 작업이 있으면 경고 (실행은 계속)
    local dup_job
    dup_job=$(db_exec "SELECT job_id FROM pipeline_jobs
                       WHERE project='${project}'
                         AND instruction_hash='${inst_hash}'
                         AND status='done'
                         AND updated_at > NOW() - INTERVAL '30 minutes'
                         AND job_id != '${job_id}'
                       LIMIT 1;" 2>/dev/null) || true
    if [[ -n "$dup_job" ]]; then
        dup_job="${dup_job// /}"
        log "  DEDUP_WARN: 30분 내 동일 작업 존재: $dup_job (계속 실행)"
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
             RETURNING job_id, project, replace(replace(instruction, E'\\n', ' '), '|', ' '), chat_session_id, max_cycles;"
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
    local job_id="$1" project="$2" instruction="$3" session_id="$4" max_cycles="$5"
    local output_file="$ARTIFACT_DIR/${job_id}.out" err_file="$ARTIFACT_DIR/${job_id}.err"

    # M4: 프로젝트 화이트리스트 검증
    if [[ ! " $VALID_PROJECTS " =~ " $project " ]]; then
        _fail_job "$job_id" "$session_id" "invalid_project" "허용되지 않은 프로젝트: $project"
        return 1
    fi

    # ── 사전 검증 (Pre-validation) ──
    pre_validate "$job_id" "$project" "$session_id" || return 1

    # ── 중복 작업 확인 ──
    check_duplicate "$job_id" "$project" "$instruction" || return 0

    local workdir="${PROJECT_WORKDIR[$project]:-}"
    log "▶ START job=$job_id project=$project workdir=$workdir"
    db_update "UPDATE pipeline_jobs SET status='running', phase='claude_code_work',
               started_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "🔧 [Pipeline Runner] 작업 시작: ${instruction:0:200}"

    # H5: 모델 폴백 순환 재시도 (소넷→오퍼스→하이쿠→소넷, 2~3회)
    local MODEL_CYCLE=("claude-sonnet" "claude-opus" "claude-haiku")
    local MAX_MODEL_CYCLES=3  # 전체 순환 횟수
    local total_attempts=$(( ${#MODEL_CYCLE[@]} * MAX_MODEL_CYCLES ))  # 9회
    local attempt=0 exit_code=0
    while [[ $attempt -lt $total_attempts ]]; do
        exit_code=0
        cd "$workdir"
        local model_idx=$(( attempt % ${#MODEL_CYCLE[@]} ))
        local current_model="${MODEL_CYCLE[$model_idx]}"
        local cycle_num=$(( attempt / ${#MODEL_CYCLE[@]} + 1 ))

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
        timeout "$MAX_RUNTIME" claude --model "$current_model" -p --output-format text "$safe_instruction" \
            > "$output_file" 2> "$err_file" &
        local claude_pid=$!

        # runner_pid 기록 (watchdog 프로세스 생존 확인용)
        db_update "UPDATE pipeline_jobs SET runner_pid=${claude_pid}, updated_at=NOW() WHERE job_id='${job_id}';"

        wait $claude_pid || exit_code=$?

        if [[ $exit_code -eq 0 ]]; then
            break
        fi

        attempt=$((attempt + 1))
        if [[ $attempt -lt $total_attempts ]]; then
            local next_model_idx=$(( attempt % ${#MODEL_CYCLE[@]} ))
            local next_model="${MODEL_CYCLE[$next_model_idx]}"
            local wait_sec=$(( 3 + attempt ))  # 3초~12초 점진 증가
            log "  RETRY job=$job_id attempt=$((attempt+1))/$total_attempts next_model=$next_model wait=${wait_sec}s exit=$exit_code"
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
        _cleanup_artifacts "$job_id"
        _notify_ai "$job_id"
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
    _cleanup_artifacts "$job_id"

    # 채팅AI 자동 반응 트리거 — AI가 결과 확인 후 CEO에게 보고
    _notify_ai "$job_id"
}

# 채팅AI 자동 반응 트리거 — 작업 완료/실패 시 AI가 결과를 확인·검수·조치
_notify_ai() {
    local job_id="$1"
    # aads-server의 notify API 호출 (백그라운드, 실패해도 무시)
    curl -4 -sf -X POST "${AADS_API_URL}/api/v1/pipeline/jobs/${job_id}/notify" \
         -H "x-monitor-key: internal" \
         --max-time 10 >/dev/null 2>&1 &
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

    # v2.1: 승인 후 커밋 → 푸시
    git add -A 2>/dev/null || true
    git commit -m "Pipeline-Runner: ${job_id}" 2>/dev/null || log "  WARN: git commit skipped (no changes or hook failure)"
    git push 2>/dev/null || true

    # ═══ 무중단 배포 v3.0 — build→swap→healthcheck→rollback ═══
    # 원칙: 빌드 중 기존 서비스 유지, 빌드 성공 후에만 교체, 실패 시 롤백
    case "$project" in
        AADS)
            # 1) aads-server: 볼륨마운트 → supervisorctl restart (~2초)
            docker exec aads-server supervisorctl restart aads-api 2>/dev/null || true

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

                        if [ "$_qa_verdict" = "FAIL" ]; then
                            local _qa_summary=""
                            _qa_summary=$(echo "$_qa_response" | jq -r '.summary // "상세 정보 없음"' 2>/dev/null) || true
                            log "  QA: FAIL — $_qa_summary"
                            post_to_chat "$session_id" "🔴 [Runner] 프론트엔드 QA FAIL: $_qa_summary (롤백 없음, 수동 확인 필요)"

                            # 텔레그램 긴급 알림
                            if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
                                curl -s -m 10 -X POST \
                                    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                                    -d "chat_id=${TELEGRAM_CHAT_ID}" \
                                    -d "text=🔴 [AADS Runner] 프론트엔드 QA FAIL: ${_qa_summary}" \
                                    -d "parse_mode=HTML" 2>/dev/null || true
                            fi
                        elif [ "$_qa_verdict" = "PASS" ]; then
                            log "  QA: PASS ✅"
                            post_to_chat "$session_id" "✅ [Runner] 프론트엔드 QA PASS — 대시보드 배포 검증 완료"
                        else
                            log "  QA: WARN — verdict 파싱 불가 ($_qa_verdict), 배포는 계속 진행"
                            post_to_chat "$session_id" "⚠️ [Runner] QA 결과 파싱 실패 — 배포는 정상 완료, QA 수동 확인 필요"
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
        for _retry in 1 2 3 4 5 6 7 8 9 10 11 12; do
            sleep 5
            if curl -sf -o /dev/null "$health_url"; then
                health_ok="OK"
                break
            fi
            log "  HEALTH_RETRY job=$job_id attempt=$_retry"
        done
    fi

    db_update "UPDATE pipeline_jobs SET status='done', phase='done',
               review_feedback=COALESCE(review_feedback,'') || E'\n[v2.1][배포완료] health=${health_ok} by=${RUNNER_HOSTNAME}',
               updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "✅ [Pipeline Runner] 배포 완료 (health=${health_ok})"
    log "  DEPLOYED job=$job_id health=$health_ok"

    # 채팅AI 자동 반응 트리거
    _notify_ai "$job_id"
}

# ── 거부된 작업 원복 ──────────────────────────────────────────────────
reject_job() {
    local job_id="$1" project="$2" session_id="$3"
    local workdir="${PROJECT_WORKDIR[$project]:-}"
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "▶ REJECT job=$job_id project=$project"
    cd "$workdir"

    # v2.1: uncommitted 변경사항 제거 (커밋 전이므로 reset 불필요)
    git checkout -- . 2>/dev/null || true
    git clean -fd 2>/dev/null || true

    db_update "UPDATE pipeline_jobs SET status='rejected_done', phase='rejected_done', updated_at=NOW() WHERE job_id='${job_id}';"
    post_to_chat "$session_id" "↩️ [Pipeline Runner] 거부된 작업 코드 원복 완료: $job_id"
    log "  REJECTED job=$job_id"
}

# C3: 크래시 복구 — 시작 시 stuck 작업 정리
_recover_stuck_jobs() {
    local filter="$1"
    # running/claimed 상태가 30분 이상 된 작업 → error로 전환
    local stuck
    stuck=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                     error_detail='stale_recovered',
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
                       error_detail='approval_timeout',
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
            # FIX: ASCII RS(0x1e) 구분자 사용 — instruction에 | 포함 시 파싱 깨짐 방지
            IFS=$'\x1e' read -r job_id project instruction session_id max_cycles <<< "$pending"
            if [[ -n "$job_id" && -n "$project" ]]; then
                run_job "$job_id" "$project" "$instruction" "$session_id" "${max_cycles:-3}" || true
            fi
        fi

        # 2) approved 작업 원자적 클레임 (C4)
        local approved
        approved=$(claim_approved_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$approved" ]]; then
            # FIX: ASCII RS(0x1e) 구분자 사용
            IFS=$'\x1e' read -r job_id project session_id <<< "$approved"
            if [[ -n "$job_id" && -n "$project" ]]; then
                deploy_job "$job_id" "$project" "$session_id" || true
            fi
        fi

        # 3) rejected 작업 코드 원복
        local rejected
        rejected=$(claim_rejected_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$rejected" ]]; then
            IFS=$'\x1e' read -r job_id project session_id <<< "$rejected"
            if [[ -n "$job_id" && -n "$project" ]]; then
                reject_job "$job_id" "$project" "$session_id" || true
            fi
        fi

        # 주기적 정리 (60 cycle = ~5분마다)
        _cycle=$((_cycle + 1))
        if (( _cycle % 60 == 0 )); then
            _recover_stuck_jobs "$project_filter"
            _watchdog_check "$project_filter"
            _cleanup_old_artifacts
        fi

        sleep "$POLL_INTERVAL"
    done
}

# ── 시그널 핸들링 ────────────────────────────────────────────────────
_current_job_id=""
cleanup() {
    log "═══ Pipeline Runner v2.1 종료 ═══"
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