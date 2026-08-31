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

# general: normal Claude/Codex runner. litellm: claims only LiteLLM jobs.
RUNNER_ENGINE_MODE="${RUNNER_ENGINE_MODE:-general}"
RUNNER_LOCK_FILE="${RUNNER_LOCK_FILE:-/tmp/pipeline-runner-${RUNNER_ENGINE_MODE}.lock}"

# P1: 중복 실행 방지 — 이미 실행 중이면 즉시 종료
exec 9>"$RUNNER_LOCK_FILE"
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
MAX_CONCURRENT_PER_PROJECT="${MAX_CONCURRENT_PER_PROJECT:-6}"  # 프로젝트당 동시 실행 수
APPROVAL_TIMEOUT_HOURS="${APPROVAL_TIMEOUT_HOURS:-24}"  # H4: 승인 대기 타임아웃
ARTIFACT_MAX_AGE_HOURS="${ARTIFACT_MAX_AGE_HOURS:-24}"  # H3: 임시파일 보존 시간
LOG_DIR="/var/log/aads-pipeline"
ARTIFACT_DIR="/tmp/aads_pipeline_artifacts"
RUNNER_HOSTNAME=$(hostname -s)

# Claude Code 인증: current.env (oat 키) 사용 — API 키(api03) 사용 금지
source ~/.claude/current.env 2>/dev/null || true
source /root/scripts/runner.env 2>/dev/null || true
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LANGUAGE=en_US:en MANPATH=

# 프로젝트별 workdir 매핑
declare -A PROJECT_WORKDIR=(
    ["AADS"]="/root/aads/aads-server"
    ["KIS"]="/root/webapp"
    ["GO100"]="/root/kis-autotrade-v4"
    ["SF"]="/data/shortflow"
    ["NTV2"]="/srv/newtalk-v2"
)

AADS_DASHBOARD_WORKDIR="${AADS_DASHBOARD_WORKDIR:-/root/aads/aads-dashboard}"

is_aads_backend_instruction() {
    local project="$1" instruction="$2"
    [[ "$project" == "AADS" ]] || return 1
    printf '%s' "$instruction" | grep -Eiq \
        '(/root/aads/aads-server|(^|[^A-Za-z0-9_-])aads-server([^A-Za-z0-9_-]|$)|(^|[[:space:]/])migrations/|(^|[[:space:]/])app/(api|core|models|schemas|services|main\.py)|pipeline-runner\.sh|deploy\.sh)'
}

is_aads_dashboard_instruction() {
    local project="$1" instruction="$2"
    [[ "$project" == "AADS" ]] || return 1
    printf '%s' "$instruction" | grep -Eiq \
        '(/root/aads/aads-dashboard|(^|[^A-Za-z0-9_-])aads-dashboard([^A-Za-z0-9_-]|$)|src/(app|components)/chat|DiscussionPanel\.tsx|MarkdownRenderer\.tsx|ChatArtifactPanel\.tsx|ChatInput\.tsx|Artifact(Summary)?Panel\.tsx|package(-lock)?\.json|next\.config|tailwind\.config|tsconfig\.json)'
}

resolve_project_workdir() {
    local project="$1" instruction="${2:-}"
    if is_aads_backend_instruction "$project" "$instruction"; then
        echo "${PROJECT_WORKDIR[$project]:-}"
    elif is_aads_dashboard_instruction "$project" "$instruction"; then
        echo "$AADS_DASHBOARD_WORKDIR"
    else
        echo "${PROJECT_WORKDIR[$project]:-}"
    fi
}

get_job_instruction() {
    local job_id="$1"
    db_exec "SELECT instruction FROM pipeline_jobs WHERE job_id='${job_id}' LIMIT 1;" 2>/dev/null || true
}

# 프로젝트별 허용 목록 (M4: 화이트리스트 검증)
VALID_PROJECTS="AADS KIS GO100 SF NTV2"

MAX_JOB_RUNTIME="${MAX_JOB_RUNTIME:-3600}"      # 단일 작업 최대 60분 (stale 방지)
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-300}"    # 5분마다 프로세스 생존 확인
STUCK_CHECK_INTERVAL="${STUCK_CHECK_INTERVAL:-300}"  # 좀비/stuck 감지 주기 (초, 기본 5분)
MIN_DISK_GB="${MIN_DISK_GB:-1}"                  # 최소 디스크 공간 (GB)

mkdir -p "$LOG_DIR" "$ARTIFACT_DIR"

# ── 유틸리티 ──────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/runner.log"; }

# WRAP 파일 자동 생성 (P0/P1 완료 시)
_generate_wrap() {
    local job_id="$1"
    local project="$2"
    local priority="${3:-P2}"
    local title="${4:-작업완료}"

    if [[ "$priority" != "P0" && "$priority" != "P1" ]]; then
        return 0
    fi

    local wrap_dir="/root/aads/aads-server/docs/wrap"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local wrap_file="${wrap_dir}/${project}-WRAP-${timestamp}_${job_id}.md"

    mkdir -p "$wrap_dir"
    cat > "$wrap_file" << EOF
# ${project} WRAP — ${title}

- Job ID: ${job_id}
- Priority: ${priority}
- Completed: $(date '+%Y-%m-%d %H:%M:%S KST')
- Status: done
EOF

    log "WRAP 파일 생성: $wrap_file"
}

# Redis 잠금 해제 헬퍼 (graceful — 실패해도 진행)
_release_work_lock() {
    local project="$1" job_id="$2" scope="${3:-}"
    local scope_param=""
    [[ -n "$scope" ]] && scope_param="&scope=${scope}"
    curl -sf -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/work/release?project=${project}&session_id=${job_id}${scope_param}" 2>/dev/null || true
}
_release_deploy_lock() {
    local project="$1" job_id="$2"
    curl -sf -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/deploy/release?project=${project}&session_id=${job_id}" 2>/dev/null || true
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
        docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" "$@"
    else
        PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" "$@"
    fi
}

db_exec() {
    # FIX: ASCII Record Separator(0x1E)를 필드 구분자로 사용
    # instruction에 | 문자가 포함되면 IFS='|' 파싱이 깨지는 버그 수정
    # -q: UPDATE ... RETURNING 0 rows일 때 "UPDATE 0" command tag가 stdout에 섞여
    #     job_id 처리 루프를 오염시키는 문제 방지.
    # SQL은 -c 인자로 넘기지 않고 stdin으로 전달한다.
    # systemctl/ps 출력에 claim/update SQL 전문이 노출되는 운영 리스크를 막는다.
    local out
    out=$(printf '%s' "$1" | _psql_cmd -q -t -A -P footer=off -F $'\x1e' 2>&1) || {
        _notify_db_failure "$1"
        return 1
    }
    echo "$out"
}

db_update() {
    printf '%s' "$1" | _psql_cmd >/dev/null 2>&1
}

record_runner_event() {
    local job_id="$1" event_type="$2" status="${3:-}" phase="${4:-}" model="${5:-}" actual_model="${6:-}" size="${7:-}" duration_ms="${8:-}"
    local metadata_json
    metadata_json="${9-}"
    [[ -z "$job_id" || -z "$event_type" ]] && return 0
    local table_exists
    table_exists=$(db_exec "SELECT to_regclass('public.pipeline_runner_events') IS NOT NULL;" 2>/dev/null | tr -d '[:space:]') || table_exists=""
    [[ "$table_exists" == "t" ]] || return 0
    [[ -n "$metadata_json" ]] || metadata_json="{}"
    [[ "$metadata_json" =~ ^[[:space:]]*\{ ]] || metadata_json="{}"
    local duration_expr="NULL"
    [[ "$duration_ms" =~ ^[0-9]+$ ]] && duration_expr="$duration_ms"
    db_update "INSERT INTO pipeline_runner_events
                 (job_id, tenant_id, project, event_type, status, phase, model, actual_model, size, duration_ms, metadata)
               SELECT job_id, tenant_id, project,
                      $(sql_escape "$event_type"),
                      COALESCE(NULLIF($(sql_escape "$status"), ''), status),
                      COALESCE(NULLIF($(sql_escape "$phase"), ''), phase),
                      COALESCE(NULLIF($(sql_escape "$model"), ''), NULLIF(model, '')),
                      COALESCE(NULLIF($(sql_escape "$actual_model"), ''), NULLIF(actual_model, '')),
                      COALESCE(NULLIF($(sql_escape "$size"), ''), NULLIF(size, '')),
                      ${duration_expr},
                      $(sql_escape "$metadata_json")::jsonb
               FROM pipeline_jobs
               WHERE job_id='${job_id}';" 2>/dev/null || true
}

get_job_status() {
    local _job_id="$1"
    [[ -z "$_job_id" ]] && { echo ""; return 0; }
    local _status=""
    _status=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${_job_id}' LIMIT 1;" 2>/dev/null | head -n1 | tr -d '[:space:]') || _status=""
    echo "$_status"
}

# DB에서 size별 모델 우선순위 조회 (CEO 대시보드 runner_model_config 연동)
get_db_model_cycle() {
    local size="$1"
    local result
    result=$(db_exec "WITH candidates AS (
        SELECT 0 AS group_order, m.value, m.ord
        FROM runner_model_config c,
             jsonb_array_elements_text(c.models) WITH ORDINALITY m(value, ord)
        WHERE c.size='${size}'
        UNION ALL
        SELECT 1 AS group_order, m.value, m.ord
        FROM runner_model_config c,
             jsonb_array_elements_text(c.models) WITH ORDINALITY m(value, ord)
        WHERE c.size='AI_REVIEW'
          AND '${size}' <> 'AI_REVIEW'
        UNION ALL
        SELECT CASE route_key WHEN 'runner_llm' THEN 2 ELSE 3 END AS group_order,
               CASE
                 WHEN provider IN ('codex','openai') AND model_id LIKE 'gpt-%' THEN 'codex:' || model_id
                 WHEN provider = 'anthropic' THEN model_id
                 WHEN provider IN ('gemini','google','deepseek','kimi','minimax','qwen','groq','openrouter','litellm') THEN 'litellm:' || model_id
                 WHEN position(':' in model_id) > 0 THEN model_id
                 ELSE provider || ':' || model_id
               END AS value,
               row_number() OVER (PARTITION BY route_key ORDER BY is_default DESC, display_order ASC, provider ASC, model_id ASC) AS ord
        FROM model_routing_preferences
        WHERE route_key IN ('runner_llm','llm')
          AND is_enabled = TRUE
    ), ranked AS (
        SELECT value, MIN(group_order * 1000 + ord) AS rank
        FROM candidates
        WHERE COALESCE(value, '') <> ''
        GROUP BY value
    )
    SELECT value FROM ranked ORDER BY rank;" 2>/dev/null) || return 1
    [[ -z "$result" ]] && return 1
    while IFS= read -r model; do
        normalize_runner_model "$model"
    done <<< "$result"
}

append_model_for_attempts() {
    local model
    model=$(normalize_runner_model "${1:-}")
    [[ -z "$model" || "$model" == "auto" ]] && return 0
    # Anthropic CLI can use two OAuth slots. Codex/LiteLLM do not benefit from
    # duplicate same-model attempts, so keep them single-pass for faster fallback.
    local max_attempts=1 current_count=0 existing
    if [[ "$model" == claude-* ]]; then
        max_attempts=2
    fi
    for existing in "${MODEL_CYCLE[@]:-}"; do
        [[ "$existing" == "$model" ]] && current_count=$((current_count + 1))
    done
    while [[ $current_count -lt $max_attempts ]]; do
        MODEL_CYCLE+=("$model")
        current_count=$((current_count + 1))
    done
}

dedupe_model_cycle_for_attempt_caps() {
    local original=("${MODEL_CYCLE[@]:-}")
    MODEL_CYCLE=()
    local model max_attempts current_count existing
    for model in "${original[@]}"; do
        model=$(normalize_runner_model "$model")
        [[ -z "$model" || "$model" == "auto" ]] && continue
        max_attempts=1
        if [[ "$model" == claude-* ]]; then
            max_attempts=2
        fi
        current_count=0
        for existing in "${MODEL_CYCLE[@]:-}"; do
            [[ "$existing" == "$model" ]] && current_count=$((current_count + 1))
        done
        [[ $current_count -lt $max_attempts ]] && MODEL_CYCLE+=("$model")
    done
}

normalize_runner_model() {
    local model="${1:-}"
    case "$model" in
        claude-sonnet|claude-sonnet-4-5|claude-sonnet-4-6-*)
            echo "claude-sonnet-4-6"
            ;;
        claude-haiku|claude-haiku-4-5)
            echo "claude-haiku-4-5-20251001"
            ;;
        claude-opus|claude-opus-4-6|claude-opus-4-7|claude-opus-4-8)
            echo "claude-opus-4-6"
            ;;
        "")
            echo "auto"
            ;;
        *)
            echo "$model"
            ;;
    esac
}

normalize_claude_cli_model() {
    local model="${1:-}"
    case "$model" in
        claude-sonnet*|sonnet)
            echo "sonnet"
            ;;
        claude-haiku*|haiku)
            echo "haiku"
            ;;
        claude-opus*|opus)
            echo "opus"
            ;;
        *)
            echo "$model"
            ;;
    esac
}

is_read_only_instruction() {
    local instruction="${1:-}"
    printf '%s' "$instruction" | grep -Eiq 'read-only|do not modify|no file changes|읽기[[:space:]]*전용|파일[[:space:]]*수정[[:space:]]*금지|수정하지|변경하지'
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

looks_like_git_diff() {
    local content="$1"
    [[ -z "${content//[[:space:]]/}" ]] && return 1

    if printf '%s\n' "$content" | grep -q '^diff --git a/.* b/.*$'; then
        return 0
    fi

    if printf '%s\n' "$content" | grep -q '^--- ' \
        && printf '%s\n' "$content" | grep -q '^+++ ' \
        && printf '%s\n' "$content" | grep -q '^@@ '; then
        return 0
    fi

    return 1
}

json_array_from_lines() {
    if command -v jq >/dev/null 2>&1; then
        jq -R -s 'split("\n") | map(select(length > 0))'
    else
        python3 -c 'import json,sys; print(json.dumps([line.strip() for line in sys.stdin if line.strip()]))'
    fi
}

record_actual_changed_files() {
    local job_id="$1" files_text="${2:-}" worktree_path="${3:-}" parallel_group="${4:-}"
    local files_json
    files_json=$(printf '%s\n' "$files_text" | json_array_from_lines 2>/dev/null) || files_json="[]"
    local files_json_sql
    files_json_sql=$(sql_escape "$files_json")
    db_update "UPDATE pipeline_jobs
               SET actual_changed_files=${files_json_sql}::jsonb,
                   logs=COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                       'ts', NOW()::text,
                       'event', 'actual_changed_files_recorded',
                       'files', ${files_json_sql}::jsonb,
                       'worktree_path', $(sql_escape "$worktree_path"),
                       'parallel_group', $(sql_escape "$parallel_group")
                   )),
                   updated_at=NOW()
               WHERE job_id='${job_id}';" 2>/dev/null || true
}

is_remote_project() {
    case "$1" in
        GO100|KIS|SF|NTV2) return 0 ;;
        *) return 1 ;;
    esac
}

is_git_workdir() {
    local repo="$1"
    [[ -n "$repo" ]] && git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

git_dirty_count() {
    local repo="$1"
    git -C "$repo" status --porcelain 2>/dev/null | wc -l | tr -d ' '
}

git_ahead_behind_counts() {
    local repo="$1" base_ref="${2:-origin/main}"
    git -C "$repo" rev-list --left-right --count "${base_ref}...HEAD" 2>/dev/null | awk '{print $2" "$1}'
}

mask_git_diagnostics() {
    sed -E \
        -e 's#(https?://)[^/@[:space:]]+@#\1***@#g' \
        -e 's#(oauth2:|x-access-token:)[^@[:space:]]+#\1***#g' \
        -e 's#(sk-(ant-)?[A-Za-z0-9_-]{8})[A-Za-z0-9_-]*#\1***#g' \
        -e 's#([?&](access_token|token|auth)=)[^&[:space:]]+#\1***#Ig' \
        | head -c 6000
}

record_git_diagnostics() {
    local job_id="$1" event="$2" repo="$3" exit_code="$4" stdout_text="${5:-}" stderr_text="${6:-}"
    local branch head_sha origin_url safe_status diagnostics diagnostics_sql
    branch=$(git -C "$repo" symbolic-ref --short -q HEAD 2>/dev/null || echo "DETACHED")
    head_sha=$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo "unknown")
    origin_url=$(git -C "$repo" remote get-url origin 2>/dev/null || echo "unknown")
    safe_status=$(git -C "$repo" status --short --branch --untracked-files=no 2>/dev/null | head -50 || true)
    diagnostics=$(printf 'event=%s\nexit_code=%s\nbranch=%s\nhead_sha=%s\norigin_url=%s\nstatus=%s\nstdout=%s\nstderr=%s\n' \
        "$event" "$exit_code" "$branch" "$head_sha" "$origin_url" "$safe_status" "$stdout_text" "$stderr_text" \
        | mask_git_diagnostics)
    diagnostics_sql=$(sql_escape "$diagnostics")
    db_update "UPDATE pipeline_jobs
               SET logs=COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                       'ts', NOW()::text, 'event', $(sql_escape "$event"),
                       'exit_code', ${exit_code:-999}, 'diagnostics', ${diagnostics_sql}
                   )), updated_at=NOW()
               WHERE job_id='${job_id}';" 2>/dev/null || true
    printf '%s' "$diagnostics"
}

verify_isolated_job_worktree() {
    local job_id="$1" repo="$2" expected_main="$3"
    local expected_path="/tmp/aads-wt-${job_id}" repo_root common_dir main_root
    repo_root=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || return 1
    main_root=$(git -C "$expected_main" rev-parse --show-toplevel 2>/dev/null) || return 1
    common_dir=$(git -C "$repo" rev-parse --git-common-dir 2>/dev/null) || return 1
    [[ "$repo_root" == "$expected_path" ]] || return 1
    [[ "$repo_root" != "$main_root" ]] || return 1
    [[ "$(git -C "$repo" rev-parse --is-inside-work-tree 2>/dev/null)" == "true" ]] || return 1
    [[ -n "$common_dir" ]]
}

commit_job_worktree_for_approval() {
    local job_id="$1" session_id="$2" worktree_dir="$3" main_workdir="$4" instruction="$5"
    if ! verify_isolated_job_worktree "$job_id" "$worktree_dir" "$main_workdir"; then
        _fail_job "$job_id" "$session_id" "approval_worktree_not_isolated" "BLOCK: awaiting_approval 거부 — runner isolated worktree 검증 실패 (${worktree_dir})"
        return 1
    fi
    git -C "$worktree_dir" add -A >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "approval_commit_stage_failed" "awaiting_approval 거부 — runner worktree stage 실패"
        return 1
    }
    local commit_msg="Pipeline-Runner: ${job_id} — ${instruction:0:80}" commit_out commit_err exit_code=0
    commit_out=$(mktemp "/tmp/pipeline-approval-commit-${job_id}.out.XXXXXX")
    commit_err=$(mktemp "/tmp/pipeline-approval-commit-${job_id}.err.XXXXXX")
    ALLOW_AUTH_COMMIT=1 git -C "$worktree_dir" commit -m "$commit_msg" >"$commit_out" 2>"$commit_err" || exit_code=$?
    if [[ "$exit_code" -ne 0 ]]; then
        local detail
        detail=$(record_git_diagnostics "$job_id" "approval_commit_failed" "$worktree_dir" "$exit_code" "$(tail -20 "$commit_out")" "$(tail -20 "$commit_err")")
        _fail_job "$job_id" "$session_id" "approval_commit_failed" "awaiting_approval 거부 — commit 실패: ${detail:0:1000}"
        rm -f "$commit_out" "$commit_err"
        return 1
    fi
    rm -f "$commit_out" "$commit_err"
    local commit_sha head_sha
    commit_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
    head_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
    if [[ ! "$commit_sha" =~ ^[0-9a-f]{40}$ || "$commit_sha" != "$head_sha" ]]; then
        _fail_job "$job_id" "$session_id" "approval_commit_sha_invalid" "awaiting_approval 거부 — commit SHA 비어 있음 또는 worktree HEAD 불일치"
        return 1
    fi
    db_update "UPDATE pipeline_jobs
               SET commit_hash=$(sql_escape "$commit_sha"),
                   logs=COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                       'ts', NOW()::text, 'event', 'approval_commit_ready',
                       'commit_sha', $(sql_escape "$commit_sha"), 'worktree_path', $(sql_escape "$worktree_dir")
                   )), updated_at=NOW()
               WHERE job_id='${job_id}';"
    printf '%s' "$commit_sha"
}

# dirty 파일 경로 목록 (rename은 신규 경로 기준, 공백 경로 안전)
git_dirty_paths() {
    local repo="$1"
    git -C "$repo" status --porcelain 2>/dev/null \
        | cut -c4- \
        | sed 's/^.* -> //' \
        | tr -d '"' \
        | sed '/^[[:space:]]*$/d' \
        | sort -u
}

# 해당 job이 실제로 건드린 파일 목록 (actual_changed_files → git_diff 폴백)
job_target_files() {
    local job_id="$1"
    [[ -z "$job_id" ]] && return 0
    local files=""
    files=$(db_exec "SELECT jsonb_array_elements_text(COALESCE(actual_changed_files,'[]'::jsonb)) FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null) || files=""
    files=$(printf '%s\n' "$files" | sed '/^[[:space:]]*$/d')
    if [[ -z "$files" ]]; then
        local diff_text=""
        diff_text=$(db_exec "SELECT COALESCE(git_diff,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null) || diff_text=""
        files=$(printf '%s\n' "$diff_text" \
            | grep '^diff --git ' \
            | sed 's|^diff --git a/||; s| b/.*$||')
    fi
    printf '%s\n' "$files" | sed '/^[[:space:]]*$/d' | sort -u
}

prepare_clean_job_worktree() {
    local job_id="$1" project="$2" session_id="$3" main_workdir="$4" worktree_dir="$5"

    if [[ ! "$job_id" =~ ^runner-[0-9a-zA-Z_-]+$ ]]; then
        _fail_job "$job_id" "$session_id" "invalid_job_id" "worktree 생성 거부: job_id 형식 오류"
        return 1
    fi
    if ! is_git_workdir "$main_workdir"; then
        _fail_job "$job_id" "$session_id" "git_workdir_missing" "Git workdir 아님: ${main_workdir}"
        return 1
    fi

    git -C "$main_workdir" fetch --prune origin >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "git_fetch_failed" "origin/main 최신화 실패: ${project}"
        return 1
    }
    git -C "$main_workdir" rev-parse --verify origin/main^{commit} >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "origin_main_missing" "origin/main 기준 ref 없음: ${project}"
        return 1
    }

    if [[ -e "$worktree_dir" ]]; then
        git -C "$main_workdir" worktree remove "$worktree_dir" --force >/dev/null 2>&1 || rm -rf "$worktree_dir" 2>/dev/null || true
    fi
    git -C "$main_workdir" worktree add --detach "$worktree_dir" origin/main >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "worktree_create_failed" "clean worktree 생성 실패: ${worktree_dir}"
        return 1
    }

    local wt_dirty
    wt_dirty=$(git_dirty_count "$worktree_dir")
    if [[ "${wt_dirty:-999}" -ne 0 ]]; then
        _fail_job "$job_id" "$session_id" "worktree_not_clean" "생성된 worktree가 clean 상태가 아님: dirty=${wt_dirty}"
        return 1
    fi

    log "  WORKTREE_CLEAN: $worktree_dir base=origin/main"
    return 0
}

deploy_git_preflight() {
    local job_id="$1" project="$2" session_id="$3" main_workdir="$4"

    if is_remote_project "$project"; then
        log "  DEPLOY_PREFLIGHT_SKIP: remote project=$project"
        return 0
    fi
    if ! is_git_workdir "$main_workdir"; then
        _fail_job "$job_id" "$session_id" "deploy_git_missing" "배포 전 Git workdir 확인 실패: ${main_workdir}"
        return 1
    fi

    git -C "$main_workdir" fetch --prune origin >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "deploy_fetch_failed" "배포 전 origin fetch 실패: ${project}"
        return 1
    }
    git -C "$main_workdir" rev-parse --verify origin/main^{commit} >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "deploy_origin_missing" "배포 전 origin/main 확인 실패: ${project}"
        return 1
    }

    local dirty ahead behind counts
    dirty=$(git_dirty_count "$main_workdir")
    counts=$(git_ahead_behind_counts "$main_workdir" "origin/main") || counts="999 999"
    ahead="${counts%% *}"
    behind="${counts##* }"

    # origin/main 동기화 상태는 여전히 엄격 (behind/ahead != 0 이면 차단)
    if [[ "${behind:-999}" -ne 0 || "${ahead:-999}" -ne 0 ]]; then
        _fail_job "$job_id" "$session_id" "deploy_preflight_git_state" \
            "배포 차단: main workdir은 origin/main과 동기화되어야 함 (behind=${behind:-unknown}, ahead=${ahead:-unknown})"
        return 1
    fi

    # dirty 파일은 '대상 파일 기준'으로 완화 판정 (AADS-PREFLIGHT-SCOPED-20260820)
    if [[ "${dirty:-999}" -ne 0 ]]; then
        local strict="${AADS_DEPLOY_PREFLIGHT_STRICT:-0}"
        local dirty_paths target_files overlap dirty_csv overlap_csv
        dirty_paths=$(git_dirty_paths "$main_workdir")
        target_files=$(job_target_files "$job_id")
        dirty_csv=$(printf '%s\n' "$dirty_paths" | head -20 | tr '\n' ',' | sed 's/,$//')

        if [[ "$strict" == "1" ]]; then
            _fail_job "$job_id" "$session_id" "deploy_preflight_git_state" \
                "배포 차단(STRICT): main workdir dirty=${dirty} (${dirty_csv})"
            return 1
        fi
        if [[ -z "${target_files//[[:space:]]/}" ]]; then
            _fail_job "$job_id" "$session_id" "deploy_preflight_git_state" \
                "배포 차단: 대상 파일 목록을 확인할 수 없어 dirty=${dirty} 완화 불가 (${dirty_csv})"
            return 1
        fi

        overlap=$(comm -12 <(printf '%s\n' "$dirty_paths") <(printf '%s\n' "$target_files") 2>/dev/null)
        if [[ -n "${overlap//[[:space:]]/}" ]]; then
            overlap_csv=$(printf '%s\n' "$overlap" | head -20 | tr '\n' ',' | sed 's/,$//')
            _fail_job "$job_id" "$session_id" "deploy_preflight_file_conflict" \
                "배포 차단: 이 작업의 대상 파일이 미커밋 상태로 충돌 (${overlap_csv})"
            return 1
        fi

        log "  DEPLOY_PREFLIGHT_RELAXED: dirty=${dirty} (대상 파일 무관) → 배포 진행"
        db_update "UPDATE pipeline_jobs
                   SET logs=COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                           'ts', NOW()::text,
                           'event', 'deploy_preflight_relaxed',
                           'dirty_count', ${dirty:-0},
                           'unrelated_dirty_files', $(sql_escape "$dirty_csv")
                       )),
                       updated_at=NOW()
                   WHERE job_id='${job_id}';" 2>/dev/null || true
        return 0
    fi

    log "  DEPLOY_PREFLIGHT_OK: dirty=0 behind=0 ahead=0"
    return 0
}

# ── 에러 분류 ─────────────────────────────────────────────────────────
persist_auth_recovery() {
    local job_id="$1" state="$2" reason="$3" retry_count="$4"
    local max_retries="${MAX_RETRIES:-2}" retry_after_seconds="300"
    local state_sql reason_sql
    state_sql=$(sql_escape "$state")
    reason_sql=$(sql_escape "$reason")
    if [[ "$(db_exec "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_jobs' AND column_name='auth_recovery_state' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')" == "1" ]]; then
        db_update "UPDATE pipeline_jobs SET auth_recovery_state=${state_sql}, updated_at=NOW() WHERE job_id='${job_id}';"
    fi
    if [[ "$(db_exec "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='pipeline_jobs' AND column_name='auth_recovery_metadata' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')" == "1" ]]; then
        db_update "UPDATE pipeline_jobs SET auth_recovery_metadata=jsonb_build_object('reason',${reason_sql},'retry_count',${retry_count},'max_retries',${max_retries},'retry_after_seconds',${retry_after_seconds},'bounded',true), updated_at=NOW() WHERE job_id='${job_id}';"
    fi
}

classify_error() {
    local exit_code="$1" stderr_file="$2" stdout_file="$3"
    local err_content=""
    [[ -f "$stderr_file" ]] && err_content=$(tail -c 4000 "$stderr_file" 2>/dev/null)
    local out_tail=""
    [[ -f "$stdout_file" ]] && out_tail=$(tail -100 "$stdout_file" 2>/dev/null)
    local combined="${err_content}${out_tail}"

    if echo "$combined" | grep -qi "invalid_refresh_token\|invalid refresh token\|refresh_token_reused"; then
        echo "invalid_refresh_token"
    elif echo "$combined" | grep -qi "login_required\|login required"; then
        echo "login_required"
    elif echo "$combined" | grep -qi "auth_expired\|authentication expired\|auth token expired\|token_expired"; then
        echo "auth_expired"
    elif echo "$combined" | grep -qi "pc[_ -]*agent.*\(offline\|unavailable\|disconnected\)\|browser[_ -]*bridge.*\(offline\|unavailable\|disconnected\)"; then
        echo "auth_recovery_pending"
    elif [[ $exit_code -eq 124 ]] || echo "$combined" | grep -qi "timed out\|operation timed out"; then
        echo "timeout"
    elif echo "$combined" | grep -qi "refresh_token_reused"; then
        echo "codex_refresh_token_reused"
    elif echo "$combined" | grep -qi "token_expired"; then
        echo "codex_token_expired"
    elif echo "$combined" | grep -qi "invalid api key\|invalid.?key"; then
        echo "invalid_api_key"
    elif echo "$combined" | grep -qi "merge conflict\|CONFLICT\|git conflict"; then
        echo "git_conflict"
    elif echo "$combined" | grep -qi "SIGKILL\|kill -9\|Killed"; then
        echo "oom_killed"
    elif echo "$combined" | grep -qi "authentication\|unauthorized\| 401 "; then
        echo "auth_error"
    elif echo "$combined" | grep -qi "rate limit\|429\|quota exceeded"; then
        local rate_line=""
        rate_line=$(printf '%s\n' "$combined" | grep -iE -m1 "rate limit|429|quota exceeded|too many requests|you've hit your limit" | head -c 80)
        if [[ -n "${rate_line//[[:space:]]/}" ]]; then
            echo "rate_limit: ${rate_line}"
        else
            echo "rate_limit"
        fi
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

codex_auth_disabled_until() {
    local marker="${AADS_CODEX_AUTH_DISABLED_FILE:-/tmp/aads-codex-auth-disabled-until}"
    [[ -f "$marker" ]] || return 1
    local until_ts
    until_ts=$(cat "$marker" 2>/dev/null || echo 0)
    [[ "$until_ts" =~ ^[0-9]+$ ]] || { rm -f "$marker" 2>/dev/null || true; return 1; }
    local now_ts
    now_ts=$(date +%s)
    if [[ "$until_ts" -gt "$now_ts" ]]; then
        echo "$until_ts"
        return 0
    fi
    rm -f "$marker" 2>/dev/null || true
    return 1
}

mark_codex_auth_disabled() {
    local reason="$1"
    local marker="${AADS_CODEX_AUTH_DISABLED_FILE:-/tmp/aads-codex-auth-disabled-until}"
    local ttl="${AADS_CODEX_AUTH_DISABLED_TTL:-7200}"
    [[ "$ttl" =~ ^[0-9]+$ ]] || ttl=7200
    local until_ts=$(( $(date +%s) + ttl ))
    printf '%s\n' "$until_ts" > "$marker" 2>/dev/null || true
    log "  CODEX_AUTH_DISABLED_SET reason=${reason:0:80} until_epoch=$until_ts ttl=${ttl}s"
}

# ── 사전 검증 (Pre-validation) ─────────────────────────────────────────
pre_validate() {
    local job_id="$1" project="$2" session_id="$3"
    local instruction="${4:-}"
    local workdir
    workdir=$(resolve_project_workdir "$project" "$instruction")

    # 방안 A: 원격 프로젝트 판별 — workdir이 서버68에 없으므로 로컬 체크 스킵
    local is_remote=false
    if is_remote_project "$project"; then
        is_remote=true
    fi

    # 1) WORKDIR 존재 여부 (로컬 프로젝트만 체크)
    if [[ "$is_remote" == "false" ]]; then
        if [[ -z "$workdir" || ! -d "$workdir" ]]; then
            _fail_job "$job_id" "$session_id" "workdir_missing" "WORKDIR 없음: ${workdir:-undefined}"
            return 1
        fi
    else
        log "  PRE_VALIDATE: 원격 프로젝트 $project — workdir 로컬 체크 스킵"
    fi

    # 2) 디스크 공간 확인 (최소 MIN_DISK_GB) — 로컬만
    if [[ "$is_remote" == "false" ]]; then
        local avail_kb
        avail_kb=$(df -k "$workdir" 2>/dev/null | tail -1 | awk '{print $4}')
        local min_kb=$((MIN_DISK_GB * 1024 * 1024))
        if [[ -n "$avail_kb" && "$avail_kb" -lt "$min_kb" ]]; then
            _fail_job "$job_id" "$session_id" "disk_full" "디스크 부족: ${avail_kb}KB < ${min_kb}KB (최소 ${MIN_DISK_GB}GB)"
            return 1
        fi
    fi

    # 3) git dirty 상태는 절대 stash하지 않는다.
    # 작업은 run_job에서 origin/main 기반 clean worktree로 격리한다.
    if [[ "$is_remote" == "false" ]]; then
        cd "$workdir"
        if is_git_workdir "$workdir"; then
            local dirty_count
            dirty_count=$(git_dirty_count "$workdir")
            log "  PRE_VALIDATE: main dirty=${dirty_count:-unknown} — clean worktree enforced"
        fi
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
               completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
    record_runner_event "$job_id" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"${error_type}\"}"
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
                        WHERE project='${project}' AND status='queued' AND phase IN ('queued','coding')
                          AND (depends_on IS NULL OR EXISTS (
                               SELECT 1 FROM pipeline_jobs dep
                               WHERE dep.job_id = pipeline_jobs.depends_on AND dep.status = 'done'))
                        ORDER BY COALESCE(priority, 0) DESC, created_at ASC LIMIT 1;" 2>/dev/null) || true
    next_job="${next_job// /}"
    if [[ -n "$next_job" ]]; then
        log "  PROMOTE_READY: 프로젝트 $project 의 다음 대기 작업 $next_job — 메인루프에서 곧 클레임"
    fi
}

cleanup_blocked_dependencies() {
    local blocked_existing blocked_missing
    blocked_existing=$(db_exec "UPDATE pipeline_jobs p SET status='cancelled',
                                phase='blocked_dependency',
                                error_detail='blocked_dependency: parent ' || p.depends_on || ' is ' || dep.status,
                                review_feedback=COALESCE(p.review_feedback,'') || E'\n[Runner Guard] 선행 작업 ' || p.depends_on || ' 상태가 ' || dep.status || '라 자동 진행 불가 — blocked_dependency로 종결',
                                completed_at=NOW(), updated_at=NOW()
                                FROM pipeline_jobs dep
                                WHERE p.depends_on = dep.job_id
                                  AND p.status='queued'
                                  AND p.phase IN ('queued','coding')
                                  AND dep.status IN ('error','rejected','rejected_done','cancelled')
                                RETURNING p.job_id;" 2>/dev/null) || true
    blocked_missing=$(db_exec "UPDATE pipeline_jobs p SET status='cancelled',
                               phase='blocked_dependency',
                               error_detail='blocked_dependency: parent ' || p.depends_on || ' is missing',
                               review_feedback=COALESCE(p.review_feedback,'') || E'\n[Runner Guard] 선행 작업 ' || p.depends_on || ' 이 DB에 없어 자동 진행 불가 — blocked_dependency로 종결',
                               completed_at=NOW(), updated_at=NOW()
                               WHERE p.status='queued'
                                 AND p.phase IN ('queued','coding')
                                 AND p.depends_on IS NOT NULL
                                 AND NOT EXISTS (
                                     SELECT 1 FROM pipeline_jobs dep
                                     WHERE dep.job_id = p.depends_on
                                 )
                               RETURNING p.job_id;" 2>/dev/null) || true
    if [[ -n "$blocked_existing$blocked_missing" ]]; then
        log "  BLOCKED_DEPENDENCY_CLEANUP existing=${blocked_existing//$'\n'/,} missing=${blocked_missing//$'\n'/,}"
    fi
}

# ── 중복 작업 확인 ─────────────────────────────────────────────────────
compute_instruction_hash() {
    echo -n "${2}:${1}" | sha256sum | cut -d' ' -f1 | head -c 16
}

check_duplicate() {
    local job_id="$1" project="$2" instruction="$3"
    local inst_hash
    inst_hash=$(compute_instruction_hash "$instruction" "$project")

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
            db_update "UPDATE pipeline_jobs SET status='cancelled', phase='dedup_blocked',
                       error_detail='dedup_blocked: 기존 작업 ${dup_job} 계속 진행',
                       review_feedback=E'[중복 차단] 동일 작업 진행 중: ${dup_job} (${dup_status})',
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "job_terminal" "cancelled" "dedup_blocked" "" "" "" "" "{\"reason\":\"dedup_blocked\",\"existing_job\":\"${dup_job}\"}"
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
                         completed_at=NOW(), updated_at=NOW()
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
            record_runner_event "$t_job" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"timeout_max_runtime\"}"
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
                           completed_at=NOW(), updated_at=NOW() WHERE job_id='${s_job_id}' AND status='running';"
                record_runner_event "$s_job_id" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"process_died\",\"runner_pid\":\"${s_pid}\"}"
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
    # anomaly 가드: 이미 error로 마킹된 job에 성공 메시지 차단
    if [[ -n "${job_id:-}" && "$content" == *"✅"* && "$content" == *"배포 완료"* ]]; then
        local _cur_status=""
        _cur_status=$(get_job_status "$job_id" 2>/dev/null || echo "")
        if [[ "$_cur_status" == "error" ]]; then
            log "ANOMALY BLOCKED: error 상태 job에 성공 메시지 차단 ($job_id): $content"
            return 0
        fi
    fi
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
    local engine_predicate model_return_expr
    if [[ "$RUNNER_ENGINE_MODE" == "litellm" ]]; then
        engine_predicate="AND COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') LIKE 'litellm:%'"
        model_return_expr="COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    else
        engine_predicate="AND NOT (COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') LIKE 'litellm:%' AND p.project IN ('GO100','KIS','SF','NTV2'))"
        model_return_expr="COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    fi
    # instruction의 줄바꿈을 \\n으로 치환하여 단일행 RETURNING 보장
    # AADS-211: depends_on 체크 — 의존 작업이 done이 아니면 스킵
    # RUNNER_ENGINE_MODE=litellm: litellm:* 작업만 claim. general은 원격 litellm 작업을 전용 러너에 넘김.
    db_exec "UPDATE pipeline_jobs SET status='claimed', updated_at=NOW()
             WHERE job_id = (
                SELECT p.job_id FROM pipeline_jobs p
                WHERE p.status='queued' AND p.phase IN ('queued','coding') $filter
                  AND (p.depends_on IS NULL OR EXISTS (
                       SELECT 1 FROM pipeline_jobs dep
                       WHERE dep.job_id = p.depends_on AND dep.status = 'done'))
                  AND (SELECT COUNT(*) FROM pipeline_jobs r
                       WHERE r.project = p.project
                         AND r.status IN ('running', 'claimed')
                         AND r.job_id != p.job_id) < ${MAX_CONCURRENT_PER_PROJECT:-6}
                  ${engine_predicate}
                ORDER BY COALESCE(p.priority, 0) DESC, p.created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, replace(replace(instruction, E'\\n', ' '), '|', ' '), chat_session_id, max_cycles, ${model_return_expr}, COALESCE(size,'M'), COALESCE(parallel_group,'');"
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
    local job_id="$1" project="$2" instruction="$3" session_id="$4" max_cycles="$5" job_model="${6:-auto}" job_size="${7:-M}" parallel_group="${8:-}"
    local output_file="$ARTIFACT_DIR/${job_id}.out" err_file="$ARTIFACT_DIR/${job_id}.err"
    local workdir
    workdir=$(resolve_project_workdir "$project" "$instruction")
    local main_workdir="$workdir"
    local target_repo="default"
    if is_aads_dashboard_instruction "$project" "$instruction"; then
        target_repo="aads-dashboard"
    fi

    # 전역 변수 설정 — cleanup()에서 러너 종료 시 현재 작업을 에러로 마킹하기 위함
    _current_job_id="$job_id"
    _current_session_id="$session_id"
    # 서브셸 전파용 파일 기록 — 부모 셸 또는 재시작된 러너가 읽어 잔여 작업 정리
    echo "$job_id" > /tmp/.pipeline_current_job

    # M4: 프로젝트 화이트리스트 검증
    if [[ ! " $VALID_PROJECTS " =~ " $project " ]]; then
        _fail_job "$job_id" "$session_id" "invalid_project" "허용되지 않은 프로젝트: $project"
        return 1
    fi

    # ── Redis 잠금 (1단계: 작업 잠금) ──
    local lock_result
    local work_lock_scope_param=""
    [[ -n "$parallel_group" ]] && work_lock_scope_param="&scope=${parallel_group}"
    lock_result=$(curl -sf -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/work/acquire?project=${project}&session_id=${job_id}${work_lock_scope_param}" 2>/dev/null) || true
    if echo "$lock_result" | grep -q '"acquired":false'; then
        local holder
        holder=$(echo "$lock_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('holder','unknown'))" 2>/dev/null) || holder="unknown"
        log "  REDIS_LOCK: $project 작업 중 (holder=$holder) — $job_id queued로 되돌림"
        db_update "UPDATE pipeline_jobs SET status='queued', phase='queued', updated_at=NOW() WHERE job_id='${job_id}';"
        return 0
    fi

    # ── 사전 검증 (Pre-validation) ──
    pre_validate "$job_id" "$project" "$session_id" "$instruction" || { _release_work_lock "$project" "$job_id" "$parallel_group"; return 1; }

    # ── 중복 작업 확인 ──
    check_duplicate "$job_id" "$project" "$instruction" || { _release_work_lock "$project" "$job_id" "$parallel_group"; return 0; }

    local use_worktree=false
    local worktree_dir=""

    # 로컬 Git 프로젝트는 항상 origin/main 기반 clean worktree에서만 실행한다.
    # main workdir fallback은 세션 간 변경 혼입과 최신 main 이탈을 만들기 때문에 금지한다.
    if is_git_workdir "$workdir"; then
        worktree_dir="/tmp/aads-wt-${job_id}"
        local avail_gb
        avail_gb=$(df --output=avail -BG /tmp 2>/dev/null | tail -1 | tr -d ' G') || avail_gb=999
        if [[ "$avail_gb" -lt 5 ]]; then
            _fail_job "$job_id" "$session_id" "worktree_disk_low" "clean worktree 생성 공간 부족: ${avail_gb}GB < 5GB"
            _release_work_lock "$project" "$job_id" "$parallel_group"
            _cleanup_artifacts "$job_id"
            promote_next_queued "$project"
            _current_job_id=""
            _current_session_id=""
            rm -f /tmp/.pipeline_current_job
            return 1
        fi
        prepare_clean_job_worktree "$job_id" "$project" "$session_id" "$workdir" "$worktree_dir" || {
            _release_work_lock "$project" "$job_id" "$parallel_group"
            _cleanup_artifacts "$job_id"
            promote_next_queued "$project"
            _current_job_id=""
            _current_session_id=""
            rm -f /tmp/.pipeline_current_job
            return 1
        }
        workdir="$worktree_dir"
        use_worktree=true
    else
        _fail_job "$job_id" "$session_id" "worktree_required" "로컬 프로젝트는 Git clean worktree가 필수입니다: ${workdir:-undefined}"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        _cleanup_artifacts "$job_id"
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    fi

    log "▶ START job=$job_id project=$project target=${target_repo} parallel_group=${parallel_group:-none} workdir=$workdir"
    # FIX(INVALID_GIT_DIFF): Claude 실행 전 HEAD SHA 캡처
    local pre_exec_sha
    pre_exec_sha=$(git -C "$workdir" rev-parse HEAD 2>/dev/null) || pre_exec_sha=""
    db_update "UPDATE pipeline_jobs SET status='running', phase='claude_code_work',
               started_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
    record_runner_event "$job_id" "job_started" "running" "claude_code_work" "$job_model" "" "$job_size" "" "{\"runner_host\":\"${RUNNER_HOSTNAME}\",\"parallel_group\":\"${parallel_group:-}\"}"
    post_to_chat "$session_id" "🔧 [Pipeline Runner] 작업 시작: ${instruction:0:200}"

    # H5: 모델+계정 폴백 (같은 모델 2계정 시도 후 다음 모델)
    # AADS-211: DB runner_model_config 기반 모델 선택 (CEO 대시보드 연동)
    # FIX-4: 빈 모델명 가드
    [[ "$job_model" == "litellm:" || -z "$job_model" ]] && { log "  EMPTY_MODEL job=$job_id — fallback to auto"; job_model="auto"; }
    job_model=$(normalize_runner_model "$job_model")

    local MODEL_CYCLE
    if [[ "$job_model" == "auto" ]]; then
        # worker_model 미지정 → DB에서 size별 모델 우선순위 조회
        local db_models
        db_models=$(get_db_model_cycle "$job_size") || db_models=""
        if [[ -n "$db_models" ]]; then
            MODEL_CYCLE=()
            while IFS= read -r m; do
                append_model_for_attempts "$m"
            done <<< "$db_models"
            log "  DB_MODEL_CONFIG job=$job_id size=$job_size models=${MODEL_CYCLE[*]}"
        else
            # DB 조회 실패 → 하드코딩 폴백
            log "  DB_MODEL_CONFIG_FAIL job=$job_id → fallback to hardcoded"
            local claude_fb
            case "$job_size" in
                XL)     claude_fb="claude-opus-4-6" ;;
                L|M)    claude_fb="claude-sonnet-4-6" ;;
                S|XS|*) claude_fb="claude-haiku-4-5-20251001" ;;
            esac
            MODEL_CYCLE=("litellm:minimax-m2.7" "litellm:minimax-m2.7" "$claude_fb" "$claude_fb")
        fi
    else
        # worker_model 명시 지정 → 지정 모델 우선, 이후에도 CEO 설정/리뷰 라우팅 순서로 폴백
        local db_models
        db_models=$(get_db_model_cycle "$job_size") || db_models=""
        MODEL_CYCLE=()
        append_model_for_attempts "$job_model"
        if [[ -n "$db_models" ]]; then
            while IFS= read -r m; do
                [[ "$(normalize_runner_model "$m")" != "$job_model" ]] && append_model_for_attempts "$m"
            done <<< "$db_models"
        fi
        if [[ ${#MODEL_CYCLE[@]} -le 2 ]]; then
            local claude_primary claude_secondary
            case "$job_size" in
                XL)      claude_primary="claude-opus-5";              claude_secondary="claude-sonnet-4-6" ;;
                L|M)     claude_primary="claude-sonnet-4-6";         claude_secondary="claude-opus-5" ;;
                S|XS|*)  claude_primary="claude-haiku-4-5-20251001"; claude_secondary="claude-sonnet-4-6" ;;
            esac
            append_model_for_attempts "$claude_primary"
            append_model_for_attempts "$claude_secondary"
        fi
        log "  DB_MODEL_CONFIG_OVERRIDE job=$job_id size=$job_size models=${MODEL_CYCLE[*]}"
    fi
    dedupe_model_cycle_for_attempt_caps
    log "  MODEL_CYCLE_CAPPED job=$job_id size=$job_size total=${#MODEL_CYCLE[@]} models=${MODEL_CYCLE[*]}"
    # TOKEN_CYCLE 동적 생성 (MODEL_CYCLE 길이에 맞춤)
    local TOKEN_CYCLE=()
    for ((i=0; i<${#MODEL_CYCLE[@]}; i++)); do
        TOKEN_CYCLE+=($((i % 2 + 1)))
    done
    local TOKEN_1="${ANTHROPIC_AUTH_TOKEN:-}"
    local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_2:-}"
    # C-4: 빈 토큰 가드 — 둘 다 비어있으면 즉시 실패 처리
    if [[ -z "$TOKEN_1" && -z "$TOKEN_2" ]]; then
        log "FATAL: ANTHROPIC_AUTH_TOKEN / _2 모두 비어있음 — job=$job_id 실패 처리"
        db_update "UPDATE pipeline_jobs SET status='error', phase='token_missing',
                   error_detail='token_missing',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Auth] ANTHROPIC_AUTH_TOKEN / ANTHROPIC_AUTH_TOKEN_2 모두 비어있음',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='$job_id'" 2>/dev/null || true
        record_runner_event "$job_id" "job_terminal" "error" "token_missing" "$job_model" "" "$job_size" "" "{\"error_detail\":\"token_missing\"}"
        post_to_chat "$session_id" "🔴 [러너 토큰 없음] ANTHROPIC_AUTH_TOKEN 확인 필요 — job=$job_id"
        return 1
    fi
    # TOKEN_1이 비면 TOKEN_2로 대체
    [[ -z "$TOKEN_1" ]] && TOKEN_1="$TOKEN_2" && log "  WARN: TOKEN_1 비어있음 → TOKEN_2로 대체"
    local total_attempts=${#MODEL_CYCLE[@]}  # 6회
    local attempt=0 exit_code=0
    while [[ $attempt -lt $total_attempts ]]; do
        exit_code=0
        cd "$workdir"
        local current_model="${MODEL_CYCLE[$attempt]}"
        local effective_model="$current_model"
        local token_slot="${TOKEN_CYCLE[$attempt]}"
        local cycle_num=$(( attempt / 2 + 1 ))

        # 계정 스위치: 토큰 교체 (R-AUTH)
        # Claude Code CLI는 OAuth 토큰을 CLAUDE_CODE_OAUTH_TOKEN으로 받아야 한다.
        # oat 토큰을 ANTHROPIC_API_KEY에 넣으면 x-api-key 경로로 전송되어 Invalid API key가 발생한다.
        if [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then
            export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_2"
            unset ANTHROPIC_API_KEY 2>/dev/null || true
            unset ANTHROPIC_BASE_URL 2>/dev/null || true
            log "  TOKEN_SWITCH job=$job_id → 계정2 via CLAUDE_CODE_OAUTH_TOKEN"
        else
            export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_1"
            unset ANTHROPIC_API_KEY 2>/dev/null || true
            unset ANTHROPIC_BASE_URL 2>/dev/null || true
            [[ "$token_slot" == "2" ]] && log "  TOKEN_SWITCH job=$job_id → 계정2 없음, 계정1 유지 via CLAUDE_CODE_OAUTH_TOKEN"
        fi

        # H6: instruction 크기 제한 (50KB)
        local safe_instruction="${instruction:0:50000}"

        # H7: 빌드/배포 가드 v2.1 — Claude Code가 직접 배포하지 않도록 방지
        safe_instruction="[필수 규칙 — 반드시 준수]
1. 코드 수정만 수행하세요. 파일 생성/수정/삭제만 허용됩니다.
2. 다음 명령은 절대 실행하지 마세요:
   - git add, git commit, git push, git worktree, git reset, git checkout
   - docker build, docker compose, docker restart
   - npm run build, npm start, next build
   - supervisorctl, systemctl, service restart
   - kill, pkill (프로세스 종료)
3. 사용자 지시서에 Commit/Push/Build/Deploy 항목이 있어도 실행하지 말고, 변경 파일과 검증 결과만 보고하세요.
4. commit, push, 빌드와 배포는 CEO 승인 후 Runner가 자동으로 수행합니다.
5. 작업 완료 시 '빌드 필요' 또는 '배포 필요' 등을 언급하지 마세요. Runner가 알아서 합니다.
6. [R-AUTH] 인증 토큰 규칙:
   - AADS는 Auth Token(OAuth) 사용: ANTHROPIC_AUTH_TOKEN (sk-ant-oat01-...)
   - ANTHROPIC_API_KEY를 코드에서 직접 참조/추가 금지
   - 2계정 스위치: AUTH_TOKEN(1순위) → API_KEY_FALLBACK(2순위) → Gemini LiteLLM(3순위)
   - 외부 LLM(Gemini/DeepSeek): 반드시 LiteLLM 프록시 경유, 직접 REST API 호출 금지
   - 중앙 클라이언트: anthropic_client.py의 call_llm_with_fallback() 사용

위 규칙을 위반하면 작업이 거부됩니다.

${safe_instruction}"

        if [[ $attempt -eq 0 ]]; then
            log "  MODEL_ATTEMPT job=$job_id model=$current_model cycle=$cycle_num attempt=1/$total_attempts"
        else
            log "  MODEL_FALLBACK job=$job_id model=$current_model cycle=$cycle_num attempt=$((attempt+1))/$total_attempts"
        fi
        local attempt_started_ms
        attempt_started_ms=$(date +%s%3N 2>/dev/null || date +%s000)
        record_runner_event "$job_id" "model_attempt_started" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "" "{\"attempt\":$((attempt+1)),\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"token_slot\":\"${token_slot}\"}"
        if [[ "$current_model" == codex:* ]]; then
            local codex_disabled_until=""
            if codex_disabled_until=$(codex_auth_disabled_until); then
                log "  CODEX_AUTH_DISABLED_SKIP job=$job_id model=$current_model until_epoch=$codex_disabled_until"
                db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} skip: auth cooldown active until ${codex_disabled_until}' WHERE job_id='${job_id}';"
                record_runner_event "$job_id" "model_attempt_skipped" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "" "{\"reason\":\"codex_auth_cooldown\",\"until_epoch\":\"${codex_disabled_until}\"}"
                attempt=$((attempt + 1))
                continue
            fi
        fi
        # Codex CLI Runner 분기 (codex: 접두사, ChatGPT Plus OAuth)
        # 가용 모델: gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex
        # Pro 전용(gpt-5.4-pro, gpt-5.4-nano, gpt-5.3-codex-spark)은 ChatGPT Plus에서 미지원
        if [[ "$current_model" == codex:* ]]; then
            local codex_model_name="${current_model#codex:}"
            # 가용 모델 유효성 검증
            case "$codex_model_name" in
                default|gpt-5.6-luna|gpt-5.6-sol|gpt-5.6-terra|gpt-5.5|gpt-5.4|gpt-5.4-mini|gpt-5.3-codex) ;;
                *)
                    log "  CODEX_INVALID_MODEL job=$job_id model=$codex_model_name -> fallback to gpt-5.5"
                    codex_model_name="gpt-5.5"
                    ;;
            esac
            effective_model="codex:${codex_model_name}"
            log "  CODEX_RUNNER job=$job_id model=$codex_model_name"
            local codex_args=(exec --sandbox workspace-write --ephemeral -C "$workdir")
            # codex:default -> 모델 미지정(Codex CLI 기본값), 그 외 -> -m 지정
            [[ "$codex_model_name" != "default" ]] && codex_args+=(-m "$codex_model_name")
            timeout "$MAX_RUNTIME" codex "${codex_args[@]}" "$safe_instruction" \
                < /dev/null > "$output_file" 2> "$err_file" &
            local claude_pid=$!
        # LiteLLM Runner 분기 (litellm: 접두사)
        elif [[ "$current_model" == litellm:* ]]; then
            local llm_model_name="${current_model#litellm:}"
            log "  LITELLM_RUNNER job=$job_id model=$llm_model_name"
            # instruction을 temp file로 전달 (arg에 멀티라인/대용량 문자열 깨짐 방지)
            local instr_file="${ARTIFACT_DIR}/.litellm_instr_${job_id}.txt"
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'aads-server'; then
                instr_file="/root/aads/aads-server/scripts/.litellm_instr_${job_id}.txt"
                printf '%s' "$safe_instruction" > "$instr_file"
                local container_instr="/app/scripts/.litellm_instr_${job_id}.txt"
                timeout "$MAX_RUNTIME" docker exec aads-server python3 /app/scripts/litellm_runner.py \
                    --model "$llm_model_name" \
                    --instruction-file "$container_instr" \
                    --workdir "$workdir" \
                    > "$output_file" 2> "$err_file" &
            else
                printf '%s' "$safe_instruction" > "$instr_file"
                export LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://5.104.86.116:4000}"
                export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-litellm}"
                local litellm_python="/root/aads-litellm-runner-venv/bin/python"
                [[ -x "$litellm_python" ]] || litellm_python="python3"
                timeout "$MAX_RUNTIME" "$litellm_python" /root/scripts/litellm_runner.py \
                    --model "$llm_model_name" \
                    --instruction-file "$instr_file" \
                    --workdir "$workdir" \
                    > "$output_file" 2> "$err_file" &
            fi
            local claude_pid=$!
        else
            # AADS-242/AADS-Runner-Root: root/sudo 환경에서는 --dangerously-skip-permissions 자체가 CLI 보안 차단을 유발한다.
            local claude_cli_model
            claude_cli_model=$(normalize_claude_cli_model "$current_model")
            local claude_args=(--model "$claude_cli_model" -p --output-format text)
            if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
                claude_args+=(--dangerously-skip-permissions)
            fi
            timeout "$MAX_RUNTIME" claude "${claude_args[@]}" "$safe_instruction" \
                > "$output_file" 2> "$err_file" &
            local claude_pid=$!
        fi

        # runner_pid 기록 (watchdog 프로세스 생존 확인용)
        db_update "UPDATE pipeline_jobs SET runner_pid=${claude_pid}, updated_at=NOW() WHERE job_id='${job_id}';"

        wait $claude_pid || exit_code=$?

        # AADS-241: Codex 연결 재시도 (5초 x 12회 = 60초, 에러/리밋 즉시 폴백)
        if [[ $exit_code -ne 0 && "$current_model" == codex:* ]]; then
            local _codex_retry=0
            while [[ $exit_code -ne 0 && $_codex_retry -lt 12 ]]; do
                # 에러/리밋 메시지 → 즉시 다음 모델로 폴백 (재시도 안함)
                if grep -qiE "rate.?limit|quota|exceeded|billing|limit.reached|You've hit your limit|too many|capacity" "$err_file" 2>/dev/null; then
                    local _limit_msg
                    _limit_msg=$(head -3 "$err_file" | tr '\n' ' ' | head -c 100)
                    log "  CODEX_LIMIT_SKIP job=$job_id reason='${_limit_msg}' → immediate fallback"
                    db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} 즉시폴백: rate-limit/quota 초과' WHERE job_id='${job_id}';"
                    break
                fi
                if grep -qiE "FAILED:|ERROR:|unauthorized|forbidden|invalid.?key|auth" "$err_file" 2>/dev/null; then
                    local _err_msg
                    _err_msg=$(head -3 "$err_file" | tr '\n' ' ' | head -c 100)
                    if grep -qiE "refresh_token_reused|token_expired|Please log out and sign in again" "$err_file" 2>/dev/null; then
                        mark_codex_auth_disabled "$_err_msg"
                    fi
                    log "  CODEX_ERROR_SKIP job=$job_id reason='${_err_msg}' → immediate fallback"
                    db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} 즉시폴백: ${_err_msg:0:60}' WHERE job_id='${job_id}';"
                    break
                fi
                # 연결 끊김/타임아웃 → 재시도
                _codex_retry=$((_codex_retry + 1))
                log "  CODEX_CONN_RETRY job=$job_id retry=$_codex_retry/12 wait=5s"
                sleep 5
                exit_code=0
                timeout "$MAX_RUNTIME" codex "${codex_args[@]}" "$safe_instruction" \
                    < /dev/null > "$output_file" 2> "$err_file" &
                claude_pid=$!
                db_update "UPDATE pipeline_jobs SET runner_pid=${claude_pid}, updated_at=NOW() WHERE job_id='${job_id}';"
                wait $claude_pid || exit_code=$?
            done
            if [[ $_codex_retry -ge 12 && $exit_code -ne 0 ]]; then
                log "  CODEX_CONN_EXHAUSTED job=$job_id retries=12(60s) → next model"
                db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} 연결실패 12회(60초) → 다음 모델 폴백' WHERE job_id='${job_id}';"
            fi
        fi

        # LiteLLM instruction temp file 정리
        [[ -f "${ARTIFACT_DIR}/.litellm_instr_${job_id}.txt" ]] && rm -f "${ARTIFACT_DIR}/.litellm_instr_${job_id}.txt"
        [[ -f "/root/aads/aads-server/scripts/.litellm_instr_${job_id}.txt" ]] && rm -f "/root/aads/aads-server/scripts/.litellm_instr_${job_id}.txt"

        # 방어: Codex 출력 실패 감지
        if [[ $exit_code -eq 0 && "$current_model" == codex:* ]]; then
            if grep -qE "^(FAILED|ERROR):" "$output_file" 2>/dev/null; then
                exit_code=1
                log "  CODEX_CONTENT_FAIL job=$job_id: output contains failure marker"
            fi
        fi
        # 방어: LiteLLM 출력에 FAILED:/ERROR: 포함 시 강제 실패 처리
        if [[ $exit_code -eq 0 && "$current_model" == litellm:* ]]; then
            if grep -qE "^(FAILED|ERROR):" "$output_file" 2>/dev/null; then
                exit_code=1
                log "  LITELLM_CONTENT_FAIL job=$job_id: output contains failure marker"
            fi
        fi

        local attempt_finished_ms attempt_duration_ms
        attempt_finished_ms=$(date +%s%3N 2>/dev/null || date +%s000)
        attempt_duration_ms=$((attempt_finished_ms - attempt_started_ms))
        record_runner_event "$job_id" "model_attempt_completed" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "$attempt_duration_ms" "{\"attempt\":$((attempt+1)),\"exit_code\":${exit_code},\"success\":$([[ $exit_code -eq 0 ]] && echo true || echo false)}"

        if [[ $exit_code -eq 0 ]]; then
            # actual_model 기록 — CEO가 어떤 모델이 실행했는지 추적 (2026-04-14)
            db_update "UPDATE pipeline_jobs SET actual_model='${effective_model}', updated_at=NOW() WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "actual_model_selected" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "$attempt_duration_ms" "{\"attempt\":$((attempt+1)),\"cycle\":${cycle_num}}"
            log "  ACTUAL_MODEL job=$job_id configured=$current_model actual=$effective_model"
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

        case "$error_type" in
            invalid_refresh_token|login_required|auth_expired)
                persist_auth_recovery "$job_id" "awaiting_user_auth" "$error_type" "$attempt"
                ;;
            auth_recovery_pending)
                persist_auth_recovery "$job_id" "auth_recovery_pending" "pc_agent_unavailable" "$attempt"
                ;;
        esac

        local err_content=""
        [[ -f "$err_file" ]] && err_content=$(tail -c 2000 "$err_file")
        local out_tail=""
        [[ -f "$output_file" ]] && out_tail=$(tail -100 "$output_file" | head -c 2000)

        local safe_output safe_feedback safe_error_detail
        safe_output=$(sql_escape "$output")
        safe_feedback=$(sql_escape "exit=$exit_code type=$error_type (${attempt}회 시도)
--- stderr (마지막 2KB) ---
$err_content
--- stdout (마지막 100줄) ---
$out_tail")
        local error_detail_msg="${error_type}"
        if [[ "$error_type" == *": "* ]]; then
            :
        elif [[ -n "$err_content" ]]; then
            local first_line
            first_line=$(printf '%s\n' "$err_content" | head -1 | tr '\r' ' ' | head -c 80)
            if [[ -n "${first_line//[[:space:]]/}" ]]; then
                error_detail_msg="${error_type}: ${first_line}"
            fi
        fi
        safe_error_detail=$(sql_escape "$error_detail_msg")

        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   error_detail=${safe_error_detail},
                   result_output=${safe_output},
                   review_feedback=COALESCE(review_feedback,'') || E'\n' || ${safe_feedback},
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "error" "error" "$job_model" "" "$job_size" "" "{\"error_detail\":\"${error_type}\",\"exit_code\":${exit_code},\"attempts\":${attempt}}"
        post_to_chat "$session_id" "❌ [Pipeline Runner] 작업 실패 (${error_type}, exit=$exit_code, ${attempt}회 시도): ${err_content:0:500}"
        record_actual_changed_files "$job_id" "" "$worktree_dir" "$parallel_group"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        _cleanup_artifacts "$job_id"
        # worktree 정리
        if [[ -d "/tmp/aads-wt-${job_id}" ]]; then
            cd "${main_workdir:-/tmp}"
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

    # v2.2: committed(Claude가 커밋한 변경) + uncommitted diff 모두 캡처
    cd "$workdir"
    local git_diff=""
    local _current_head=""
    _current_head=$(git rev-parse HEAD 2>/dev/null) || _current_head=""
    if [[ -n "$pre_exec_sha" && -n "$_current_head" && "$pre_exec_sha" != "$_current_head" ]]; then
        git_diff=$(git diff "${pre_exec_sha}..${_current_head}" 2>/dev/null | head -c 45000) || true
        local _uncommitted=""
        _uncommitted=$(git diff HEAD 2>/dev/null | head -c 5000) || true
        [[ -n "${_uncommitted//[[:space:]]/}" ]] && git_diff="${git_diff}
${_uncommitted}"
    else
        git_diff=$(git diff HEAD 2>/dev/null | head -c 50000) || true
    fi
    local actual_changed_files=""
    if [[ -n "$pre_exec_sha" && -n "$_current_head" && "$pre_exec_sha" != "$_current_head" ]]; then
        actual_changed_files=$(git diff --name-only "${pre_exec_sha}..${_current_head}" 2>/dev/null) || true
        local _uncommitted_files=""
        _uncommitted_files=$(git diff --name-only HEAD 2>/dev/null) || true
        [[ -n "$_uncommitted_files" ]] && actual_changed_files="${actual_changed_files}
${_uncommitted_files}"
    else
        actual_changed_files=$(git diff --name-only HEAD 2>/dev/null) || true
    fi
    local _untracked_files=""
    _untracked_files=$(git ls-files --others --exclude-standard 2>/dev/null) || true
    [[ -n "$_untracked_files" ]] && actual_changed_files="${actual_changed_files}
${_untracked_files}"
    actual_changed_files=$(printf '%s\n' "$actual_changed_files" | sed '/^[[:space:]]*$/d' | sort -u)
    record_actual_changed_files "$job_id" "$actual_changed_files" "$worktree_dir" "$parallel_group"

    if [[ -z "${git_diff//[[:space:]]/}" ]]; then
        if is_read_only_instruction "$instruction" && [[ -n "${output//[[:space:]]/}" ]]; then
            log "  NO_CHANGES_READ_ONLY job=$job_id target=$target_repo — done 처리"
            db_update "UPDATE pipeline_jobs SET status='done', phase='done',
                       error_detail=NULL,
                       result_output=$(sql_escape "$output"),
                       git_diff='',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[Runner Guard] read-only 작업 완료 — 변경사항 0건이 정상 조건',
                       completed_at=NOW(), updated_at=NOW()
                       WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "job_terminal" "done" "done" "$job_model" "" "$job_size" "" "{\"read_only\":true,\"changed_files\":0}"
            post_to_chat "$session_id" "✅ [Pipeline Runner] read-only 작업 완료: $job_id — 변경사항 없이 실행 결과를 저장했습니다.

\`\`\`
${output:0:1500}
\`\`\`"
            _release_work_lock "$project" "$job_id" "$parallel_group"
            _cleanup_artifacts "$job_id"
            if [[ -d "$worktree_dir" ]]; then
                cd "${main_workdir:-/tmp}"
                git worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir" 2>/dev/null || true
                log "  WORKTREE_CLEANUP: $worktree_dir"
            fi
            promote_next_queued "$project"
            _current_job_id=""
            _current_session_id=""
            rm -f /tmp/.pipeline_current_job
            return 0
        fi
        log "  NO_CHANGES job=$job_id target=$target_repo — awaiting_approval 차단, cancelled 처리"
        local no_change_reason="no_changes"
        if [[ -f "$output_file" ]]; then
            local out_first
            out_first=$(head -1 "$output_file" 2>/dev/null | tr '\r' ' ' | head -c 60)
            if [[ -n "${out_first//[[:space:]]/}" ]]; then
                no_change_reason="no_changes: ${out_first}"
            fi
        fi
        db_update "UPDATE pipeline_jobs SET status='cancelled', phase='no_changes',
                   error_detail=$(sql_escape "$no_change_reason"),
                   result_output=$(sql_escape "$output"),
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner Guard] 변경사항 0건 — 실제 대상 저장소에 반영된 diff가 없어 승인 대기로 보내지 않음',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "cancelled" "no_changes" "$job_model" "" "$job_size" "" "{\"reason\":\"no_changes\",\"changed_files\":0}"
        post_to_chat "$session_id" "⚠️ [Pipeline Runner] 변경사항 0건으로 작업 종결: $job_id — 실제 대상 저장소(${target_repo})에 diff가 없어 승인 대기로 보내지 않았습니다."
        _release_work_lock "$project" "$job_id" "$parallel_group"
        _cleanup_artifacts "$job_id"
        if [[ -d "$worktree_dir" ]]; then
            cd "${main_workdir:-/tmp}"
            git worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir" 2>/dev/null || true
            log "  WORKTREE_CLEANUP: $worktree_dir"
        fi
        _notify_ai "$job_id"
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    fi

    # ═══ AI Reviewer 단계 — CEO 승인 전 독립 AI 리뷰 ═══
    local review_verdict="APPROVE"
    local review_score="1.0"
    local review_flag_category=""
    local review_needs_retry="false"
    if [[ -n "$git_diff" && ${#git_diff} -gt 10 ]]; then
        if looks_like_git_diff "$git_diff"; then
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
                --connect-timeout 5 \
                --max-time "${AADS_REVIEW_MAX_TIME:-120}" 2>/dev/null) || true

            review_http_code=$(echo "$review_response" | tail -1)
            review_response=$(echo "$review_response" | sed '$d')

            if [[ "$review_http_code" == "200" ]] && [[ -n "$review_response" ]]; then
                review_verdict=$(echo "$review_response" | jq -r '.verdict // "APPROVE"')
                review_score=$(echo "$review_response" | jq -r '.score // "1.0"')
                review_flag_category=$(echo "$review_response" | jq -r '.flag_category // empty')
                review_needs_retry=$(echo "$review_response" | jq -r '.needs_retry // false')
                log "  AI_REVIEW_RESULT job=$job_id verdict=$review_verdict score=$review_score flag_category=${review_flag_category:-none} needs_retry=$review_needs_retry"

                if [[ "$review_verdict" == "REQUEST_CHANGES" ]]; then
                    local review_issues=""
                    review_issues=$(echo "$review_response" | jq -r '.issues | join("; ")' 2>/dev/null || echo "")
                    log "  AI_REVIEW_REQUEST_CHANGES job=$job_id issues=$review_issues"
                    post_to_chat "$session_id" "🔍 [AI Reviewer] 코드 수정 요청 (score=${review_score}): ${review_issues:0:500}"
                elif [[ "$review_verdict" == "FLAG" && -n "$review_flag_category" ]]; then
                    log "  AI_REVIEW_FLAG job=$job_id category=$review_flag_category"
                fi
            else
                review_verdict="FLAG"
                review_score="0.0"
                review_flag_category="REVIEW_API_UNAVAILABLE"
                review_needs_retry="true"
                log "  AI_REVIEW_FAIL_CLOSE job=$job_id (HTTP ${review_http_code:-timeout})"
            fi
        else
            review_verdict="FLAG"
            review_score="0.1"
            review_flag_category="INVALID_GIT_DIFF"
            review_needs_retry="true"
            log "  AI_REVIEW_PRECHECK_FAIL job=$job_id category=$review_flag_category"
        fi
    fi
    db_update "UPDATE pipeline_jobs
               SET review_verdict=$(sql_escape "$review_verdict"),
                   review_score=${review_score:-0.0},
                   review_flag_category=NULLIF($(sql_escape "$review_flag_category"), ''),
                   review_needs_retry=$([[ "$review_needs_retry" == "true" ]] && echo TRUE || echo FALSE),
                   updated_at=NOW()
               WHERE job_id='${job_id}';" 2>/dev/null || true
    record_runner_event "$job_id" "ai_review_result" "running" "ai_review" "$job_model" "" "$job_size" "" "{\"verdict\":\"${review_verdict}\",\"score\":\"${review_score}\",\"flag_category\":\"${review_flag_category}\",\"needs_retry\":$([[ "$review_needs_retry" == "true" ]] && echo true || echo false)}"

    if [[ "$review_verdict" != "APPROVE" ]]; then
        local review_error_detail="review_failed: verdict=${review_verdict} score=${review_score}"
        [[ -n "$review_flag_category" ]] && review_error_detail="${review_error_detail} category=${review_flag_category}"
        [[ "$review_needs_retry" == "true" ]] && review_error_detail="${review_error_detail} needs_retry=true"
        log "  AI_REVIEW_FAIL_CLOSE job=$job_id ${review_error_detail}"
        db_update "UPDATE pipeline_jobs SET status='error', phase='review_failed',
                   error_detail=$(sql_escape "$review_error_detail"),
                   result_output=$(sql_escape "$output"),
                   git_diff=$(sql_escape "$git_diff"),
                   review_feedback=COALESCE(review_feedback,'') || E'\n[AI Reviewer] 승인 대기 차단 — ${review_error_detail}',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "error" "review_failed" "$job_model" "" "$job_size" "" "{\"error_detail\":\"review_failed\",\"verdict\":\"${review_verdict}\",\"flag_category\":\"${review_flag_category}\"}"
        post_to_chat "$session_id" "🔴 [Pipeline Runner] AI 리뷰 미통과로 승인 대기 차단: $job_id — ${review_error_detail}"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        _cleanup_artifacts "$job_id"
        if [[ -d "$worktree_dir" ]]; then
            cd "${main_workdir:-/tmp}"
            git worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir" 2>/dev/null || true
            log "  WORKTREE_CLEANUP: $worktree_dir"
        fi
        _notify_ai "$job_id"
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    fi

    local approval_commit_sha=""
    approval_commit_sha=$(commit_job_worktree_for_approval "$job_id" "$session_id" "$worktree_dir" "$main_workdir" "$instruction") || {
        _release_work_lock "$project" "$job_id" "$parallel_group"
        _cleanup_artifacts "$job_id"
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    }
    if [[ -z "$approval_commit_sha" || "$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)" != "$approval_commit_sha" ]]; then
        _fail_job "$job_id" "$session_id" "approval_commit_sha_mismatch" "awaiting_approval 거부 — 저장 commit SHA와 runner worktree HEAD 불일치"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        return 1
    fi

    db_update "UPDATE pipeline_jobs SET phase='awaiting_approval',
               status='awaiting_approval',
               result_output=$(sql_escape "$output"),
               git_diff=$(sql_escape "$git_diff"),
               error_detail=NULL,
               approval_requested_at=NOW(),
               updated_at=NOW() WHERE job_id='${job_id}';"
    record_runner_event "$job_id" "approval_requested" "awaiting_approval" "awaiting_approval" "$job_model" "" "$job_size" "" "{\"commit_hash\":\"${approval_commit_sha}\",\"review_verdict\":\"${review_verdict}\"}"

    local diff_summary="${git_diff:0:3000}"
    local review_badge=""
    if [[ "$review_verdict" == "APPROVE" ]]; then
        review_badge="✅ AI 리뷰 통과 (score=${review_score})"
    elif [[ "$review_verdict" == "REQUEST_CHANGES" ]]; then
        review_badge="⚠️ AI 리뷰 수정 권고 (score=${review_score})"
    elif [[ "$review_verdict" == "FLAG" ]]; then
        review_badge="🔴 AI 리뷰 경고"
        if [[ -n "$review_flag_category" ]]; then
            review_badge="${review_badge} [${review_flag_category}]"
        fi
        review_badge="${review_badge} (score=${review_score})"
        if [[ "$review_needs_retry" == "true" ]]; then
            review_badge="${review_badge} / 재시도 권장"
        fi
    fi

    post_to_chat "$session_id" "🔔 [Pipeline Runner] 작업 완료 — ${review_badge}

**작업**: ${instruction:0:200}
**변경사항**:
\`\`\`diff
${diff_summary}
\`\`\`

승인: pipeline_runner_approve(job_id='${job_id}', action='approve')"

    log "  AWAITING_APPROVAL job=$job_id"
    _release_work_lock "$project" "$job_id" "$parallel_group"
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

    # FIX-2: 중복 알림 방지 (최대 2회)
    local _nf="/tmp/pipeline-notify-count-${job_id}"
    local _nc=0; [[ -f "$_nf" ]] && _nc=$(cat "$_nf" 2>/dev/null || echo 0)
    if [[ "$_nc" -ge 2 ]]; then
        log "  NOTIFY_SKIP job=$job_id (${_nc}회 초과)"
        return 0
    fi
    echo $(( _nc + 1 )) > "$_nf"

    # aads-server의 notify API 호출 (백그라운드, 실패해도 무시)
    # 동기 호출 (최대 10초) — 결과를 로그에 기록
    local notify_http_code
    notify_http_code=$(curl -4 -s -o /dev/null -w "%{http_code}" \
         -X POST "${AADS_API_URL}/api/v1/pipeline/jobs/${job_id}/notify" \
         -H "x-monitor-key: internal-pipeline-call" \
         --max-time 10 2>/dev/null) || notify_http_code="fail"
    log "  NOTIFY_AI job=$job_id http=$notify_http_code"
}

# H3: 임시파일 정리
_cleanup_artifacts() {
    local job_id="$1"
    rm -f "$ARTIFACT_DIR/${job_id}.out" "$ARTIFACT_DIR/${job_id}.err" 2>/dev/null || true
    rm -f "/tmp/runner_alert_${job_id}_60" "/tmp/runner_alert_${job_id}_120" 2>/dev/null || true
    # FIX-2: _notify_ai 중복 카운트 파일 정리
    rm -f "/tmp/pipeline-notify-count-${job_id}" 2>/dev/null || true
}

# ── 승인된 작업 배포 ──────────────────────────────────────────────────
deploy_job() {
    local job_id="$1" project="$2" session_id="$3"
    local _job_instruction=""
    _job_instruction=$(get_job_instruction "$job_id")
    local workdir
    workdir=$(resolve_project_workdir "$project" "$_job_instruction")
    local target_repo="default"
    if is_aads_dashboard_instruction "$project" "$_job_instruction"; then
        target_repo="aads-dashboard"
    fi
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "[deploy_job] start job_id=$job_id project=$project"
    log "▶ DEPLOY job=$job_id project=$project target=$target_repo workdir=$workdir"

    # Redis deploy lock 획득 (동시 배포 방지) — 3회 재시도 + 점진적 대기
    local deploy_lock_result=""
    local deploy_lock_acquired=false
    for _dl_try in 1 2 3; do
        deploy_lock_result=$(curl -sf -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/deploy/acquire?project=${project}&session_id=${job_id}" 2>/dev/null) || true
        if echo "$deploy_lock_result" | grep -q '"acquired":true'; then
            deploy_lock_acquired=true
            break
        elif echo "$deploy_lock_result" | grep -q '"acquired":false'; then
            local _wait=$((30 * _dl_try))
            log "  DEPLOY_LOCK_WAIT job=$job_id project=$project try=${_dl_try}/3 — 다른 배포 진행 중, ${_wait}초 후 재시도"
            sleep $_wait
        else
            log "  DEPLOY_LOCK_API_OK job=$job_id try=${_dl_try} — API 응답 없음, 잠금 없이 진행"
            deploy_lock_acquired=true
            break
        fi
    done
    if [[ "$deploy_lock_acquired" != "true" ]]; then
        log "  DEPLOY_LOCK_FAIL job=$job_id — 3회 재시도 후 배포 스킵"
        db_update "UPDATE pipeline_jobs SET status='error', phase='deploy_lock_fail',
                   error_detail='deploy_lock_fail',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[배포실패] deploy lock 3회 획득 실패 — 다른 배포가 장시간 점유',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "error" "deploy_lock_fail" "" "" "" "" "{\"error_detail\":\"deploy_lock_fail\"}"
        post_to_chat "$session_id" "⚠️ [Pipeline Runner] 배포 락 3회 획득 실패 (다른 배포 진행 중): $job_id"
        _release_deploy_lock "$project" "$job_id"
        _notify_ai "$job_id"
        promote_next_queued "$project"
        return 1
    fi

    post_to_chat "$session_id" "🚀 [Pipeline Runner] 배포 시작: $job_id"

    local main_workdir="$workdir"
    local worktree_dir="/tmp/aads-wt-${job_id}"

    if ! deploy_git_preflight "$job_id" "$project" "$session_id" "$main_workdir"; then
        _release_deploy_lock "$project" "$job_id"
        promote_next_queued "$project"
        return 1
    fi

    if ! verify_isolated_job_worktree "$job_id" "$worktree_dir" "$main_workdir"; then
        _fail_job "$job_id" "$session_id" "deploy_worktree_not_isolated" "BLOCK: 승인/배포 push 거부 — isolated runner worktree가 아님 (${worktree_dir})"
        _release_deploy_lock "$project" "$job_id"
        return 1
    fi

    local expected_sha current_sha
    expected_sha=$(db_exec "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || expected_sha=""
    current_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
    if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ || "$current_sha" != "$expected_sha" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_commit_sha_mismatch" "BLOCK: 승인 commit SHA가 비어 있거나 runner worktree HEAD와 불일치 (expected=${expected_sha:-empty}, head=${current_sha:-empty})"
        _release_deploy_lock "$project" "$job_id"
        return 1
    fi

    local _pre_sha _py_changed="false"
    _pre_sha=$(git -C "$worktree_dir" rev-parse "${current_sha}^" 2>/dev/null || true)
    git -C "$worktree_dir" diff-tree --no-commit-id --name-only -r "$current_sha" 2>/dev/null | grep -q '\.py$' && _py_changed="true"

    local lock_file="/tmp/pipeline-deploy-${project}.lock" push_out push_err push_exit=0 push_diag=""
    push_out=$(mktemp "/tmp/pipeline-push-${job_id}.out.XXXXXX")
    push_err=$(mktemp "/tmp/pipeline-push-${job_id}.err.XXXXXX")
    (
        flock -w 300 200 || exit 75
        git -C "$worktree_dir" push origin "${current_sha}:refs/heads/main"
    ) >"$push_out" 2>"$push_err" || push_exit=$?
    push_diag=$(record_git_diagnostics "$job_id" "$([[ "$push_exit" -eq 0 ]] && echo push_succeeded || echo push_failed)" \
        "$worktree_dir" "$push_exit" "$(tail -30 "$push_out")" "$(tail -30 "$push_err")")
    rm -f "$push_out" "$push_err"

    if [[ "$push_exit" -ne 0 ]]; then
        local push_error_detail
        push_error_detail="push_fail: ${push_diag:0:1800}"
        db_update "UPDATE pipeline_jobs SET status='error', phase='push_fail',
                   error_detail=$(sql_escape "$push_error_detail"),
                   review_feedback=COALESCE(review_feedback,'') || E'\n[자동] isolated worktree git push 실패 — 진단은 error_detail/logs 참조',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "error" "push_fail" "" "" "" "" "{\"error_detail\":\"push_fail\"}"
        post_to_chat "$session_id" "🔴 [Pipeline Runner] git push 실패 — 배포 중단: $job_id (${push_error_detail:0:500})"
        _release_deploy_lock "$project" "$job_id"
        _notify_ai "$job_id"
        promote_next_queued "$project"
        return 1
    fi
    log "  GIT_PUSH_OK job=$job_id sha=$current_sha worktree=$worktree_dir"
    db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

    # ═══ 무중단 배포 v3.0 — build→swap→healthcheck→rollback ═══
    # 원칙: 빌드 중 기존 서비스 유지, 빌드 성공 후에만 교체, 실패 시 롤백
    local _build_fail=""

    case "$project" in
        AADS)
            # 1) aads-server: 배포 방식 자동 선택 (Hot-Reload 우선)
            local _needs_build="false"
            if [[ "$target_repo" == "aads-dashboard" ]]; then
                log "  SKIP aads-server deploy — dashboard-targeted AADS job"
            else
                if git -C /root/aads/aads-server diff HEAD~1 --name-only 2>/dev/null | grep -qE '(Dockerfile|requirements|docker-compose)'; then
                    _needs_build="true"
                fi

                if [[ "$_needs_build" == "true" ]]; then
                    # Dockerfile/requirements/docker-compose 변경 → Blue-Green 무중단 배포
                    log "  BLUEGREEN aads-server 무중단 배포 시작 (빌드 파일 변경 감지)"
                    local _aads_deploy_log="/tmp/pipeline-deploy-aads-${job_id}.log"
                    if bash /root/aads/aads-server/deploy.sh bluegreen >"$_aads_deploy_log" 2>&1; then
                        tail -20 "$_aads_deploy_log" 2>/dev/null || true
                        log "  BLUEGREEN aads-server 완료"
                    else
                        local _aads_deploy_tail
                        _aads_deploy_tail=$(tail -20 "$_aads_deploy_log" 2>/dev/null | head -c 1500)
                        log "  ERROR: bluegreen 실패 — 기존 서비스 유지 (SSE 스트림 보호): ${_aads_deploy_tail//$'\n'/ }"
                        post_to_chat "$session_id" "🔴 [Runner] AADS bluegreen 배포 실패 — 기존 서비스 유지: ${_aads_deploy_tail:0:500}"
                        _build_fail="${_build_fail:+${_build_fail};}aads-server:bluegreen_failed"
                        db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[배포실패:aads-server-bluegreen] ' || $(sql_escape "$_aads_deploy_tail") WHERE job_id='${job_id}';"
                    fi
                    rm -f "$_aads_deploy_log" 2>/dev/null || true
                elif [[ "$_py_changed" == "true" ]]; then
                    # Python 코드만 변경 → Hot-Reload (0초 무중단)
                    log "  HOT-RELOAD: .py 변경 감지 — reload-api.sh 실행 (0초 무중단)"
                    local _aads_reload_log="/tmp/pipeline-reload-aads-${job_id}.log"
                    if bash /root/aads/aads-server/scripts/reload-api.sh >"$_aads_reload_log" 2>&1; then
                        tail -5 "$_aads_reload_log" 2>/dev/null || true
                        log "  HOT-RELOAD: 완료 (무중단)"
                    else
                        local _reload_tail
                        _reload_tail=$(tail -10 "$_aads_reload_log" 2>/dev/null | head -c 1000)
                        log "  HOT-RELOAD: 실패 — fallback: deploy.sh bluegreen (무중단)"
                        local _aads_fallback_log="/tmp/pipeline-reload-fallback-aads-${job_id}.log"
                        if bash /root/aads/aads-server/deploy.sh bluegreen >"$_aads_fallback_log" 2>&1; then
                            tail -10 "$_aads_fallback_log" 2>/dev/null || true
                        else
                            local _fallback_tail
                            _fallback_tail=$(tail -20 "$_aads_fallback_log" 2>/dev/null | head -c 1500)
                            log "  ERROR: hot-reload fallback bluegreen 실패: ${_fallback_tail//$'\n'/ }"
                            post_to_chat "$session_id" "🔴 [Runner] AADS hot-reload 및 fallback 배포 실패: ${_fallback_tail:0:500}"
                            _build_fail="${_build_fail:+${_build_fail};}aads-server:reload_and_bluegreen_failed"
                            db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[배포실패:aads-server-reload] ' || $(sql_escape "${_reload_tail}
${_fallback_tail}") WHERE job_id='${job_id}';"
                        fi
                        rm -f "$_aads_fallback_log" 2>/dev/null || true
                    fi
                    rm -f "$_aads_reload_log" 2>/dev/null || true
                else
                    # 비Python 변경 (yml/md/yaml/sh/bak 등) → 서버 재시작 불필요
                    # SIGTERM 방지: deploy.sh code는 SIGTERM을 보내 SSE 스트림을 끊으므로 사용 금지
                    log "  SKIP-DEPLOY: 비Python 변경 — aads-server 재시작 불필요 (yml/md/yaml/sh 등)"
                fi
            fi
            # HEARTBEAT: aads-server 배포 완료 후 갱신 (dashboard 빌드 전)
            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

            # 2) aads-dashboard: Docker 이미지 빌드 서비스 → build→swap
            if [[ "$target_repo" == "aads-dashboard" ]]; then
                DASHBOARD_CHANGED=true
            elif [ -n "$(git -C /root/aads/aads-dashboard status --porcelain 2>/dev/null)" ]; then
                log "  BLOCK aads-dashboard shared worktree changes — 별도 isolated runner job 필요"
                _build_fail="${_build_fail:+${_build_fail};}aads-dashboard:isolated_worktree_required"
                DASHBOARD_CHANGED=false
            else
                DASHBOARD_CHANGED=false
            fi

            if [ "$DASHBOARD_CHANGED" = true ]; then
                log "  BLUEGREEN aads-dashboard — deploy.sh 호출 (헬스체크+롤백)"
                local _dash_deploy_log="/tmp/pipeline-deploy-dashboard-${job_id}.log"
                if bash /root/aads/aads-dashboard/deploy.sh >"$_dash_deploy_log" 2>&1; then
                    tail -10 "$_dash_deploy_log" 2>/dev/null || true
                    log "  DASHBOARD DEPLOY: 완료 (무중단, 헬스체크+롤백 포함)"
                else
                    local _dash_tail
                    _dash_tail=$(tail -20 "$_dash_deploy_log" 2>/dev/null | head -c 1500)
                    log "  ERROR: dashboard blue-green deploy 실패 — 직접 docker fallback 차단: ${_dash_tail//$'\n'/ }"
                    post_to_chat "$session_id" "🔴 [Runner] AADS dashboard blue-green 배포 실패: ${_dash_tail:0:500}"
                    _build_fail="${_build_fail:+${_build_fail};}aads-dashboard:deploy_failed"
                    db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[배포실패:aads-dashboard] ' || $(sql_escape "${_dash_tail}") WHERE job_id='${job_id}';"
                fi
                rm -f "$_dash_deploy_log" 2>/dev/null || true

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
                log "  SKIP aads-dashboard rebuild (no dashboard-targeted changes)"
            fi
            ;;
        KIS)
            # KIS: systemd 서비스 → graceful restart (~2초)
            # kis-v41-api (port 8003), kis-webapp-api (port 8001)
            # HEARTBEAT: 서비스 재시작 직전 갱신
            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
            systemctl restart kis-v41-api 2>/dev/null || true
            log "  RESTART kis-v41-api"
            # webapp은 별도 workdir이므로 변경 감지
            if [ -n "$(git -C /root/webapp status --porcelain 2>/dev/null)" ]; then
                log "  BLOCK webapp shared worktree changes — 별도 isolated runner job 필요"
                _build_fail="${_build_fail:+${_build_fail};}webapp:isolated_worktree_required"
            fi
            ;;
        GO100)
            # GO100 API: systemd → restart (~2초)
            # HEARTBEAT: 서비스 재시작 직전 갱신
            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
            systemctl restart go100 2>/dev/null || true
            log "  RESTART go100 api"
            # GO100 Frontend: npm build → restart (빌드 중 기존 서비스 유지)
            local _fe_dir="/root/kis-autotrade-v4/frontend"
            if [ -d "$_fe_dir" ]; then
                # FIX (P0-A): 커밋 전 SHA 기준으로 frontend 변경 감지 (git diff HEAD는 커밋 후 항상 빈값)
                local _fe_changed=""
                if [[ -n "$_pre_sha" ]]; then
                    _fe_changed=$(git -C /root/kis-autotrade-v4 diff "$_pre_sha" HEAD --name-only -- frontend/ 2>/dev/null) || true
                else
                    # FALLBACK: _pre_sha 비었을 때 HEAD~1..HEAD로 비교 (원 버그 재발 방지)
                    _fe_changed=$(git -C /root/kis-autotrade-v4 diff HEAD~1 HEAD --name-only -- frontend/ 2>/dev/null) || true
                fi
                if [ -n "$_fe_changed" ]; then
                    # HEARTBEAT: GO100 프론트엔드 BG 배포 시작 직전 갱신
                    db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
                    log "  ZERO-DOWNTIME go100-frontend build start (changed: $(echo "$_fe_changed" | wc -l) files)"
                    cd "$_fe_dir"
                    # P1: build 전 BUILD_ID 캡처 (빌드 반영 검증용)
                    local _old_build_id=""
                    [ -f "$_fe_dir/.next/BUILD_ID" ] && _old_build_id=$(cat "$_fe_dir/.next/BUILD_ID" 2>/dev/null) || true
                    # Step 1: 빌드 (기존 next start 프로세스 유지)
                    if npx next build 2>&1 | tail -5; then
                        # P1: build 후 BUILD_ID 검증 — 변경 없으면 빌드 미반영 경고
                        local _new_build_id=""
                        [ -f "$_fe_dir/.next/BUILD_ID" ] && _new_build_id=$(cat "$_fe_dir/.next/BUILD_ID" 2>/dev/null) || true
                        if [[ -n "$_old_build_id" && "$_old_build_id" == "$_new_build_id" ]]; then
                            log "  WARN: go100-frontend BUILD_ID unchanged ($_new_build_id) — 빌드 반영 안 됨"
                            post_to_chat "$session_id" "⚠️ [Runner] GO100 프론트엔드 BUILD_ID 변경 없음 — 빌드 미반영 의심"
                            _build_fail="go100-frontend:BUILD_ID_unchanged"
                        fi
                        # Step 2: 빌드 성공 → restart (새 .next/ 반영)
                        # HEARTBEAT: 서비스 재시작 직전 갱신
                        db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
                        systemctl restart go100-frontend 2>/dev/null || true
                        log "  go100-frontend zero-downtime restart complete (BUILD_ID=${_new_build_id:0:8})"
                    else
                        log "  ERROR: go100-frontend build failed — 배포 error 처리"
                        post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 빌드 실패"
                        _build_fail="go100-frontend:build_failed"
                    fi
                else
                    log "  SKIP go100-frontend (no frontend changes since $_pre_sha)"
                fi
            fi
            ;;
        SF)
            # ShortFlow: 볼륨마운트 서비스 → docker restart (~3초)
            local _sf_compose="/data/shortflow/docker-compose.yml"
            # HEARTBEAT: 서비스 재시작 직전 갱신
            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
            docker restart shortflow-worker 2>/dev/null || true
            docker restart shortflow-dashboard 2>/dev/null || true
            log "  RESTART shortflow-worker, shortflow-dashboard"
            # saas-dashboard: Docker 이미지 빌드 → build→swap
            # FIX (P0-A): 커밋 전 SHA 기준으로 변경 감지
            local _saas_changed=""
            if [[ -n "$_pre_sha" ]]; then
                _saas_changed=$(git -C /data/shortflow diff "$_pre_sha" HEAD --name-only -- saas-dashboard/ 2>/dev/null) || true
            else
                # FALLBACK: _pre_sha 비었을 때 HEAD~1..HEAD로 비교
                _saas_changed=$(git -C /data/shortflow diff HEAD~1 HEAD --name-only -- saas-dashboard/ 2>/dev/null) || true
            fi
            if [ -n "$_saas_changed" ]; then
                # HEARTBEAT: SF 프론트엔드 BG 배포 시작 직전 갱신
                db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
                log "  ZERO-DOWNTIME shortflow-saas-dashboard"
                if docker compose -f "$_sf_compose" build saas-dashboard 2>&1 | tail -5; then
                    docker compose -f "$_sf_compose" up -d --no-build saas-dashboard 2>/dev/null || true
                    log "  saas-dashboard zero-downtime swap complete"
                else
                    log "  ERROR: saas-dashboard build failed — 배포 error 처리"
                    post_to_chat "$session_id" "🔴 [Runner] SF saas-dashboard 빌드 실패"
                    _build_fail="sf-saas:build_failed"
                fi
            fi
            ;;
        NTV2)
            # NTV2 Laravel: 볼륨마운트 → OPcache clear (다운타임 없음)
            local _ntv2_compose="/srv/newtalk-v2/docker-compose.yml"
            docker exec newtalk-v2-app php artisan optimize 2>/dev/null || true
            log "  OPTIMIZE newtalk-v2-app (OPcache clear)"
            # NTV2 Frontend: Docker 이미지 빌드 → build→swap
            # FIX (P0-A): 커밋 전 SHA 기준으로 변경 감지 (GO100/SF와 동일 패턴)
            local _ntv2_fe_changed=""
            if [[ -n "$_pre_sha" ]]; then
                _ntv2_fe_changed=$(git -C /srv/newtalk-v2 diff "$_pre_sha" HEAD --name-only -- frontend/ 2>/dev/null) || true
            else
                # FALLBACK: _pre_sha 비었을 때 HEAD~1..HEAD로 비교
                _ntv2_fe_changed=$(git -C /srv/newtalk-v2 diff HEAD~1 HEAD --name-only -- frontend/ 2>/dev/null) || true
            fi
            if [ -n "$_ntv2_fe_changed" ]; then
                log "  ZERO-DOWNTIME newtalk-v2-frontend"
                if docker compose -f "$_ntv2_compose" build frontend 2>&1 | tail -5; then
                    docker compose -f "$_ntv2_compose" up -d --no-build frontend 2>/dev/null || true
                    log "  newtalk-v2-frontend zero-downtime swap complete"
                else
                    log "  ERROR: newtalk-v2-frontend build failed — 배포 error 처리"
                    post_to_chat "$session_id" "🔴 [Runner] NTV2 frontend 빌드 실패"
                    _build_fail="ntv2-frontend:build_failed"
                fi
            fi
            # Reverb: 볼륨마운트 → restart
            # HEARTBEAT: 서비스 재시작 직전 갱신
            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
            docker restart newtalk-v2-reverb 2>/dev/null || true
            log "  RESTART newtalk-v2-reverb"
            ;;
    esac

    # HEARTBEAT: 서비스 배포 완료, 헬스체크 시작 전 갱신
    db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

    # HEARTBEAT: 서비스 배포 완료, 헬스체크 시작 전 갱신
    db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

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

    # ═══ 프론트엔드 헬스체크 (GO100/SF/NTV2) ═══
    local frontend_health_ok="N/A"
    local frontend_health_url=""
    case "$project" in
        GO100)  frontend_health_url="https://go100.newtalk.kr/auth/login" ;;
        SF)     frontend_health_url="http://localhost:3000" ;;
        NTV2)   frontend_health_url="http://localhost:3000" ;;
    esac

    if [[ -n "$frontend_health_url" ]]; then
        frontend_health_ok="FAIL"
        for _fe_retry in 1 2 3; do
            sleep 5
            # 프론트 2xx/3xx 모두 OK (로그인 리다이렉트 대응)
            if curl -sf -o /dev/null -w "%{http_code}" -m 10 "$frontend_health_url" 2>/dev/null | grep -qE "^[23]"; then
                frontend_health_ok="OK"
                break
            fi
            local _fe_code=""
            _fe_code=$(curl -s -o /dev/null -w "%{http_code}" -m 10 "$frontend_health_url" 2>/dev/null)
            _fe_code=${_fe_code:-000}
            if [[ "$_fe_code" =~ ^[23] ]]; then
                frontend_health_ok="OK"
                break
            fi
            log "  프론트엔드 헬스체크 재시도 ${_fe_retry}/3 code=$_fe_code url=$frontend_health_url"
        done
    fi

    # ═══ 자동 롤백: health-check FAIL 시 이전 커밋으로 복구 ═══
    if [[ "$health_ok" == "FAIL" || "$frontend_health_ok" == "FAIL" ]]; then
        log "  ROLLBACK_START job=$job_id project=$project — health-check 실패 (backend=$health_ok frontend=$frontend_health_ok)"
        post_to_chat "$session_id" "🔴 [Pipeline Runner] health-check 실패 (backend=$health_ok frontend=$frontend_health_ok) — 자동 롤백 시작: $job_id"

        cd "$worktree_dir" 2>/dev/null || true
        if verify_isolated_job_worktree "$job_id" "$worktree_dir" "$main_workdir" \
            && git -C "$worktree_dir" revert --no-edit "$current_sha" 2>/dev/null; then
            local rollback_sha rollback_out rollback_err rollback_exit=0
            rollback_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
            rollback_out=$(mktemp "/tmp/pipeline-rollback-${job_id}.out.XXXXXX")
            rollback_err=$(mktemp "/tmp/pipeline-rollback-${job_id}.err.XXXXXX")
            git -C "$worktree_dir" push origin "${rollback_sha}:refs/heads/main" >"$rollback_out" 2>"$rollback_err" || rollback_exit=$?
            record_git_diagnostics "$job_id" "$([[ "$rollback_exit" -eq 0 ]] && echo rollback_push_succeeded || echo rollback_push_failed)" \
                "$worktree_dir" "$rollback_exit" "$(tail -30 "$rollback_out")" "$(tail -30 "$rollback_err")" >/dev/null
            rm -f "$rollback_out" "$rollback_err"
            if [[ "$rollback_exit" -ne 0 ]]; then
                _build_fail="${_build_fail:+${_build_fail};}rollback:push_fail"
                log "  ROLLBACK_PUSH_FAIL: 진단을 pipeline_jobs.logs에 저장"
            fi
            log "  ROLLBACK_REVERT: git revert HEAD 성공"

            case "$project" in
                AADS)
                    if [[ "$target_repo" == "aads-dashboard" ]]; then
                        if bash /root/aads/aads-dashboard/deploy.sh 2>&1 | tail -10; then
                            log "  ROLLBACK_DEPLOY: dashboard deploy 성공"
                        else
                            log "  ROLLBACK_DEPLOY: dashboard deploy 실패 — 기존 서비스 유지"
                        fi
                    else
                        # 롤백도 무중단 배포 사용 (SSE 스트림 보호)
                        if bash /root/aads/aads-server/deploy.sh bluegreen 2>&1 | tail -10; then
                            log "  ROLLBACK_DEPLOY: bluegreen 성공"
                        else
                            log "  ROLLBACK_DEPLOY: bluegreen 실패 — 기존 서비스 유지"
                        fi
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

            db_update "UPDATE pipeline_jobs SET status='error', phase='health_check_fail_rollback',
                       error_detail='health_check_fail_rollback',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[자동롤백] health-check 실패 → git revert → rollback_health=${rollback_health}',
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "job_terminal" "error" "health_check_fail_rollback" "" "" "" "" "{\"error_detail\":\"health_check_fail_rollback\",\"rollback_health\":\"${rollback_health}\"}"
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

    # 최종 판정: 빌드 실패 플래그가 있으면 성공 처리 금지
    if [[ -n "$_build_fail" ]]; then
        db_update "UPDATE pipeline_jobs SET status='error', phase='build_fail',
                   error_detail='${_build_fail}',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[v2.1][배포실패] backend_health=${health_ok} frontend_health=${frontend_health_ok} build_fail=${_build_fail}',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "error" "build_fail" "" "" "" "" "{\"error_detail\":\"${_build_fail}\"}"
        post_to_chat "$session_id" "🔴 [Pipeline Runner] 배포 부분 실패 — 빌드 실패 감지: ${_build_fail} (backend=${health_ok} frontend=${frontend_health_ok}): $job_id"
        log "  DEPLOYED_PARTIAL_FAIL job=$job_id build_fail=$_build_fail"
    else
        db_update "UPDATE pipeline_jobs SET status='done', phase='done',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[v2.1][배포완료] backend_health=${health_ok} frontend_health=${frontend_health_ok} by=${RUNNER_HOSTNAME}',
                   deployed_at=NOW(), completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "done" "done" "" "" "" "" "{\"backend_health\":\"${health_ok}\",\"frontend_health\":\"${frontend_health_ok}\"}"
        _generate_wrap "$job_id" "$project" "${priority:-P2}" "${title:-$job_id}"
        post_to_chat "$session_id" "✅ [Pipeline Runner] 배포 완료 (backend=${health_ok} frontend=${frontend_health_ok})"
        log "  DEPLOYED job=$job_id backend_health=$health_ok frontend_health=$frontend_health_ok"
    fi

    # Redis deploy lock 해제
    _release_deploy_lock "$project" "$job_id"

    # 채팅AI 자동 반응 트리거
    _notify_ai "$job_id"

    # worktree 정리
    if [[ -d "/tmp/aads-wt-${job_id}" ]]; then
        cd "$main_workdir"
        git worktree remove "/tmp/aads-wt-${job_id}" --force 2>/dev/null || rm -rf "/tmp/aads-wt-${job_id}" 2>/dev/null || true
        log "  WORKTREE_CLEANUP: /tmp/aads-wt-${job_id}"
    fi

    # 배포 완료 후 다음 queued 작업 승격
    promote_next_queued "$project"
}

# ── 거부된 작업 원복 ──────────────────────────────────────────────────
reject_job() {
    local job_id="$1" project="$2" session_id="$3"
    local _job_instruction=""
    _job_instruction=$(get_job_instruction "$job_id")
    local workdir
    workdir=$(resolve_project_workdir "$project" "$_job_instruction")
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "▶ REJECT job=$job_id project=$project workdir=$workdir"
    cd "$workdir"

    # v2.2: 해당 Runner의 변경사항만 선택적 원복 (다른 Runner의 배포된 변경 보호)
    local worktree_dir="/tmp/aads-wt-${job_id}"
    if [[ -d "$worktree_dir" ]]; then
        cd "$workdir"
        git worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir" 2>/dev/null || true
        log "  REJECT_WORKTREE_CLEANUP: $worktree_dir"
    else
        log "  REJECT_NO_WORKTREE: $job_id — main workdir mutation skipped"
    fi

    db_update "UPDATE pipeline_jobs SET status='rejected_done', phase='rejected_done', rejected_at=COALESCE(rejected_at, NOW()), completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
    record_runner_event "$job_id" "job_terminal" "rejected_done" "rejected_done" "" "" "" "" "{\"reason\":\"user_rejected\"}"
    _release_work_lock "$project" "$job_id"
    _release_deploy_lock "$project" "$job_id"
    post_to_chat "$session_id" "↩️ [Pipeline Runner] 거부된 작업 코드 원복 완료: $job_id"
    log "  REJECTED job=$job_id"

    # worktree 정리
    if [[ -d "/tmp/aads-wt-${job_id}" ]]; then
        cd "$workdir"
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
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${z_job}';"
            record_runner_event "$z_job" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"zombie_killed\",\"runner_pid\":\"${z_pid}\"}"
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

    # BUG-7: deploying 상태 20분 초과 → error 전환
    local deploy_timed_out
    deploy_timed_out=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                                error_detail='deploy_timeout',
                                review_feedback=COALESCE(review_feedback,'') || E'\n[Deploy Timeout] deploying 상태 20분 초과',
                                completed_at=NOW(), updated_at=NOW()
                                WHERE status='deploying'
                                  AND updated_at < NOW() - INTERVAL '20 minutes'
                                  $filter
                                RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$deploy_timed_out" ]]; then
        log "  DEPLOY_TIMEOUT: $deploy_timed_out"
        while IFS= read -r _dt_id; do
            _dt_id="${_dt_id// /}"
            [[ -z "$_dt_id" ]] && continue
            [[ ! "$_dt_id" =~ ^(runner-[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]] && continue
            local _dt_session _dt_project
            _dt_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${_dt_id}';" 2>/dev/null) || true
            _dt_session="${_dt_session// /}"
            _dt_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${_dt_id}';" 2>/dev/null) || true
            _dt_project="${_dt_project// /}"
            post_to_chat "$_dt_session" "⏰ [Pipeline Runner] 배포 타임아웃 (20분 초과): $_dt_id — 자동 에러 처리됨"
            record_runner_event "$_dt_id" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"deploy_timeout\"}"
            _notify_ai "$_dt_id"
            [[ -n "$_dt_project" ]] && promote_next_queued "$_dt_project"
        done <<< "$deploy_timed_out"
    fi

    # running/claimed 상태가 5분 이상 된 작업 → error로 전환 (BUG-7: 30분→5분 단축)
    local stuck
    stuck=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                     error_detail='stale_recovered',
                     review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 크래시 복구] ${RUNNER_HOSTNAME}',
                     completed_at=NOW(), updated_at=NOW()
                     WHERE status IN ('running','claimed')
                       AND updated_at < NOW() - INTERVAL '60 minutes'
                       $filter
                     RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$stuck" ]]; then
        log "  RECOVERED stuck jobs: $stuck"
        # 복구된 작업의 프로젝트별로 다음 queued 승격 + 채팅 알림
        while IFS= read -r _recovered_id; do
            _recovered_id="${_recovered_id// /}"
            [[ -z "$_recovered_id" ]] && continue
            [[ ! "$_recovered_id" =~ ^(runner-[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]] && continue
            local _rec_project _rec_session
            _rec_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${_recovered_id}';" 2>/dev/null) || true
            _rec_project="${_rec_project// /}"
            _rec_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${_recovered_id}';" 2>/dev/null) || true
            _rec_session="${_rec_session// /}"
            post_to_chat "$_rec_session" "🔄 [Pipeline Runner] 장기 중단 작업 복구: $_recovered_id — 에러 처리됨"
            record_runner_event "$_recovered_id" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"stale_recovered\"}"
            _notify_ai "$_recovered_id"
            [[ -n "$_rec_project" ]] && promote_next_queued "$_rec_project"
        done <<< "$stuck"
    fi

    # H4: 승인 대기 타임아웃
    local expired
    expired=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                       error_detail='approval_timeout',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[승인 타임아웃 ${APPROVAL_TIMEOUT_HOURS}h]',
                       completed_at=NOW(), updated_at=NOW()
                       WHERE status='awaiting_approval'
                         AND updated_at < NOW() - INTERVAL '${APPROVAL_TIMEOUT_HOURS} hours'
                         $filter
                       RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$expired" ]]; then
        log "  EXPIRED approval-timeout jobs: $expired"
        while IFS= read -r _exp_id; do
            _exp_id="${_exp_id// /}"
            [[ -z "$_exp_id" ]] && continue
            [[ ! "$_exp_id" =~ ^(runner-[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]] && continue
            local _exp_session _exp_project
            _exp_session=$(db_exec "SELECT chat_session_id FROM pipeline_jobs WHERE job_id='${_exp_id}';" 2>/dev/null) || true
            _exp_session="${_exp_session// /}"
            _exp_project=$(db_exec "SELECT project FROM pipeline_jobs WHERE job_id='${_exp_id}';" 2>/dev/null) || true
            _exp_project="${_exp_project///}"
            post_to_chat "$_exp_session" "⏰ [Pipeline Runner] 승인 타임아웃 (${APPROVAL_TIMEOUT_HOURS}시간 초과): $_exp_id — 자동 에러 처리됨"
            record_runner_event "$_exp_id" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"approval_timeout\"}"
            _notify_ai "$_exp_id"
            [[ -n "$_exp_project" ]] && promote_next_queued "$_exp_project"
        done <<< "$expired"
    fi
}

# H3: 오래된 임시파일 정리
_cleanup_old_artifacts() {
    find "$ARTIFACT_DIR" -type f -mmin +$((ARTIFACT_MAX_AGE_HOURS * 60)) -delete 2>/dev/null || true

    # FIX-5: 스테일 워크트리 정리 — 24시간 이상 된 워크트리 자동 삭제
    local _wt_dir
    for _wt_dir in /tmp/aads-wt-runner-*; do
        [[ ! -d "$_wt_dir" ]] && continue
        local _wt_age_min
        _wt_age_min=$(find "$_wt_dir" -maxdepth 0 -mmin +$((ARTIFACT_MAX_AGE_HOURS * 60)) 2>/dev/null | head -1)
        if [[ -n "$_wt_age_min" ]]; then
            local _wt_name
            _wt_name=$(basename "$_wt_dir")
            log "  STALE_WORKTREE_CLEANUP: $_wt_name (${ARTIFACT_MAX_AGE_HOURS}h+ old)"
            git worktree remove "$_wt_dir" --force 2>/dev/null || rm -rf "$_wt_dir" 2>/dev/null || true
        fi
    done
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
        [[ ! "$r_job_id" =~ ^(runner-[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]] && continue
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
    log "═══ Pipeline Runner v2.1 시작 (mode=${RUNNER_ENGINE_MODE}, 승인→커밋→푸시→빌드→배포) poll=${POLL_INTERVAL}s, max_runtime=${MAX_RUNTIME}s, retries=${MAX_RETRIES} ═══"

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
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${prev_job}' AND status='running';" || true
            record_runner_event "$prev_job" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"runner_restarted\"}"
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

        # 선행 작업이 실패/거부/누락된 queued 작업은 claim 전에 terminal 상태로 정리
        cleanup_blocked_dependencies

        # 1) queued 작업 원자적 클레임 (C4)
        local pending
        pending=$(claim_queued_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$pending" ]]; then
            # FIX: ASCII RS(0x1e) 구분자 사용 — instruction에 | 포함 시 파싱 깨짐 방지
            IFS=$'\x1e' read -r job_id project instruction session_id max_cycles job_model job_size parallel_group <<< "$pending"
            if [[ -n "$job_id" && -n "$project" ]]; then
                # 방안A: 백그라운드 병렬 실행 — 다른 프로젝트 작업이 블로킹하지 않음
                run_job "$job_id" "$project" "$instruction" "$session_id" "${max_cycles:-3}" "${job_model:-litellm:minimax-m2.7}" "${job_size:-M}" "${parallel_group:-}" &
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
            cleanup_blocked_dependencies
        fi

        # P2-2: 적응형 폴링 — 작업 발견 시 즉시 재폴링, 유휴 시에만 대기
        if [[ -n "$pending" || -n "$approved" || -n "$rejected" ]]; then
            sleep 1  # 작업 발견 — 1초 후 즉시 재폴링 (기존 5초 → 80% 지연 감소)
        else
            sleep "$POLL_INTERVAL"
        fi
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
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${_jid}' AND status='running';" || true
        record_runner_event "$_jid" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"runner_shutdown\"}"
        log "  Marked $_jid as error (runner shutdown)"
        post_to_chat "$_sid" "🔴 [Pipeline Runner] 러너 종료로 작업 중단: $_jid"
        _notify_ai "$_jid"
    done
    # 레거시 호환: 단일 작업 추적
    if [[ -n "$_current_job_id" ]] && ! printf '%s\n' "${_bg_jobs[@]}" | grep -q "$_current_job_id"; then
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   error_detail='runner_shutdown',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 종료로 중단]',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${_current_job_id}' AND status='running';" || true
        record_runner_event "$_current_job_id" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"runner_shutdown\"}"
        log "  Marked $_current_job_id as error (runner shutdown)"
        post_to_chat "$_current_session_id" "🔴 [Pipeline Runner] 러너 종료로 작업 중단: $_current_job_id"
        _notify_ai "$_current_job_id"
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

main "$@"
