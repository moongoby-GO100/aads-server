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
CLAUDE_MODEL_CONTRACT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/claude_model_contract.py"

# general: normal Claude/Codex runner. litellm: claims only LiteLLM jobs.
RUNNER_ENGINE_MODE="${RUNNER_ENGINE_MODE:-general}"
RUNNER_LOCK_FILE="${RUNNER_LOCK_FILE:-/tmp/pipeline-runner-${RUNNER_ENGINE_MODE}.lock}"

# P1: 중복 실행 방지 — 이미 실행 중이면 즉시 종료
exec 9>"$RUNNER_LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 이미 실행 중인 러너가 있습니다. 종료." >&2
    exit 0
fi

# RUNNER_MAINTENANCE_PROTOCOL=1 — mandatory host-shared lifetime admission.
# Acquire before credentials, startup recovery or any job/DB operation. Legacy
# hosts are enrolled only by an explicitly authorized bootstrap, never by sync.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runner_busy_lib.sh"
runner_maintenance_enter /run/aads-runner-maintenance \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")" || exit 3
readonly RUNNER_MAINTENANCE_ROOT RUNNER_MAINTENANCE_SOURCE RUNNER_MAINTENANCE_ACTIVE

# ── 설정 ──────────────────────────────────────────────────────────────
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"
# 비밀번호는 EnvironmentFile에서 로드 (systemd)
PGPASSWORD="${PGPASSWORD:-}"
export PGPASSWORD

POLL_INTERVAL="${POLL_INTERVAL:-5}"
# Host-local nginx tracks the active blue/green API. Never pin review traffic
# to blue:8100 while green:8102 is active or blue is being synchronized.
AADS_API_URL="${AADS_API_URL:-http://127.0.0.1}"
MAX_RUNTIME="${MAX_RUNTIME:-7200}"
MAX_RETRIES="${MAX_RETRIES:-2}"               # H5: Claude 실패 시 재시도 횟수
MAX_CONCURRENT_PER_PROJECT="${MAX_CONCURRENT_PER_PROJECT:-6}"  # 프로젝트당 동시 실행 수
APPROVAL_TIMEOUT_HOURS="${APPROVAL_TIMEOUT_HOURS:-24}"  # H4: 승인 대기 타임아웃
ARTIFACT_MAX_AGE_HOURS="${ARTIFACT_MAX_AGE_HOURS:-24}"  # H3: 임시파일 보존 시간
LOG_DIR="/var/log/aads-pipeline"
ARTIFACT_DIR="/tmp/aads_pipeline_artifacts"
RUNNER_HOSTNAME=$(hostname -s)

# ── Claude 릴레이 슬롯 자격증명 (AADS-RUNNER-SLOT-AUTH, 2026-09-14) ─────
# .env 고정 oat 토큰은 refresh 수단이 없어 만료/revoke 되면 러너 전체가 정지한다.
# 2026-09-14 실측: 계정1 429(주간한도), 계정2 401(revoked)로 35단 폴백이 전멸했는데
# 같은 시각 릴레이는 정상이었다. 릴레이가 쓰는 슬롯 자격증명은 accessToken 과
# refreshToken 을 함께 들고 있어 CLI 가 스스로 갱신하기 때문이다.
# 러너도 같은 래퍼를 경유해 그 자격증명을 공유한다. 슬롯이 없거나 불완전하면
# 조용히 기존 고정 토큰 경로로 폴백하므로 슬롯이 없는 서버(211/114)는 영향이 없다.
CLAUDE_RELAY_SLOT_HOME_ROOT="${CLAUDE_RELAY_SLOT_HOME_ROOT:-/root/.claude-relay-slots}"
CLAUDE_LEASE_SLOT_HOME_ROOT="${CLAUDE_LEASE_SLOT_HOME_ROOT:-/root/.claude-lease}"
CLAUDE_SLOT_CREDENTIAL_WRAPPER="${CLAUDE_SLOT_CREDENTIAL_WRAPPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/claude-slot-credentials-wrapper.sh}"
RUNNER_USE_SLOT_CREDENTIALS="${RUNNER_USE_SLOT_CREDENTIALS:-1}"

# 러너가 사용할 Claude CLI 바이너리.
# 2026-09-14 실측 A/B(4/4, 동일 자격증명·동일 래퍼·동일 모델):
#   호스트 전역 CLI 2.1.270 → "You've hit your weekly limit" 로 전량 차단
#   릴레이 번들 CLI 2.1.259 → 정상 응답
# 자격증명 문제가 아니라 CLI 버전 문제였다. 릴레이와 같은 버전을 고정 경로에 두고 쓴다.
# 바이너리(216MB)는 저장소 밖에 두며, 없으면 전역 claude 로 조용히 폴백한다.
RUNNER_CLAUDE_CLI_BIN="${RUNNER_CLAUDE_CLI_BIN:-/root/aads/vendor/claude-cli/claude}"
[[ -x "$RUNNER_CLAUDE_CLI_BIN" ]] || RUNNER_CLAUDE_CLI_BIN="claude"

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
    # ACCT(회계) 는 jinah244(5.104.85.244) 에만 있다. /srv/biseo 는 회계 원본 자료
    # 5.4G 가 섞인 상위 디렉터리이고, 실제 git 저장소는 그 두 단계 아래다.
    # (origin: github.com/dossau2018-oss/biseo, .git 48M)
    ["ACCT"]="/srv/biseo/회계비서/회계비서"
)

AADS_DASHBOARD_WORKDIR="${AADS_DASHBOARD_WORKDIR:-/root/aads/aads-dashboard}"

# AADS에는 한 작업에 하나의 명시적 TARGET만 허용한다. 본문의 경로나 저장소 이름은
# 읽기 참고일 수 있으므로 routing 근거가 아니다. TARGET이 없으면 기존 AADS 기본값(backend)을
# 유지하되, dashboard 작업은 반드시 canonical TARGET 행을 명시해야 한다.
# 허용 문법(공백만 앞뒤 허용):
#   TARGET: /root/aads/aads-server
#   TARGET: /root/aads/aads-dashboard
aads_instruction_target() {
    local project="$1" instruction="${2:-}"
    local line payload target_rows=0 target="" invalid=false
    [[ "$project" == "AADS" ]] || { printf '%s\n' "default"; return 0; }

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*TARGET[[:space:]]*: ]] || continue
        target_rows=$((target_rows + 1))
        payload="${line#*:}"
        payload=$(printf '%s' "$payload" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
        case "$payload" in
            /root/aads/aads-server) target="backend" ;;
            /root/aads/aads-dashboard) target="dashboard" ;;
            *) invalid=true ;;
        esac
    done <<< "$instruction"

    # Duplicate directives, mixed targets, unknown targets, and a same-line list
    # are all fail-closed. Exact matching rejects /root/aads/aads-server-other.
    if [[ "$invalid" == "true" || "$target_rows" -gt 1 ]]; then
        return 1
    fi
    [[ -n "$target" ]] || target="backend"
    printf '%s\n' "$target"
}

is_aads_backend_instruction() {
    local target
    target=$(aads_instruction_target "$1" "${2:-}") || return 1
    [[ "$1" == "AADS" && "$target" == "backend" ]]
}

is_aads_dashboard_instruction() {
    local target
    target=$(aads_instruction_target "$1" "${2:-}") || return 1
    [[ "$1" == "AADS" && "$target" == "dashboard" ]]
}

resolve_project_workdir() {
    local project="$1" instruction="${2:-}" target
    if [[ "$project" != "AADS" ]]; then
        printf '%s\n' "${PROJECT_WORKDIR[$project]:-}"
        return 0
    fi
    target=$(aads_instruction_target "$project" "$instruction") || return 1
    if [[ "$target" == "dashboard" ]]; then
        printf '%s\n' "$AADS_DASHBOARD_WORKDIR"
    else
        printf '%s\n' "${PROJECT_WORKDIR[$project]:-}"
    fi
}

fail_invalid_aads_target() {
    local job_id="$1" session_id="$2"
    _fail_job "$job_id" "$session_id" "invalid_aads_target" \
        "AADS TARGET은 단 하나의 canonical 행만 허용: TARGET: /root/aads/aads-server 또는 TARGET: /root/aads/aads-dashboard"
}

get_job_instruction() {
    local job_id="$1"
    db_exec "SELECT instruction FROM pipeline_jobs WHERE job_id='${job_id}' LIMIT 1;" 2>/dev/null || true
}

# 프로젝트별 허용 목록 (M4: 화이트리스트 검증)
# ACCT 는 jinah244 전용이다. 여기에 없으면 러너가 job 을 집고도 invalid_project 로
# 즉시 죽는다 — 2026-09-14 편입 스모크에서 실제로 그렇게 실패했다.
VALID_PROJECTS="AADS KIS GO100 SF NTV2 ACCT"

# 실행 서버 이름. pipeline_jobs.runner_host 와 하트비트에 쓴다.
RUNNER_HOST_NAME="${AADS_RUNNER_HOST_NAME:-$(hostname -s 2>/dev/null || hostname)}"

# 이 러너가 살아 있음을 DB 에 남긴다. 조회 측이 원격 systemctl 을 호출하지 않고
# DB 만 읽어 서버 가동 여부를 판단할 수 있게 한다.
runner_heartbeat() {
    db_update "INSERT INTO pipeline_runner_hosts (host, projects, engine_mode, max_concurrent, last_seen_at)
               VALUES ('${RUNNER_HOST_NAME}', '${RUNNER_PROJECTS:-}', '${RUNNER_ENGINE_MODE:-}',
                       NULLIF('${MAX_CONCURRENT_SERVER:-}', '')::int, NOW())
               ON CONFLICT (host) DO UPDATE SET
                 projects=EXCLUDED.projects,
                 engine_mode=EXCLUDED.engine_mode,
                 max_concurrent=EXCLUDED.max_concurrent,
                 last_seen_at=NOW();" 2>/dev/null || true
}

MAX_JOB_RUNTIME="${MAX_JOB_RUNTIME:-$MAX_RUNTIME}"  # CLI 상한보다 먼저 작업을 종료하지 않음
# 러너는 공개 URL(Cloudflare)로 검수 API 를 부른다. Cloudflare 는 약 100초에
# 원본 응답을 포기하고 524 를 돌려주므로, 420초를 기다려도 쓸 수 있는 시간은
# 100초뿐이다. 서버 쪽 검수 마감(REVIEW_TOTAL_DEADLINE_SEC=85)보다 조금 길게
# 잡아, 정상 응답은 받고 프록시가 끊기 전에 우리가 먼저 포기하도록 한다.
AADS_REVIEW_MAX_TIME="${AADS_REVIEW_MAX_TIME:-95}"
AADS_REVIEW_MAX_ATTEMPTS="${AADS_REVIEW_MAX_ATTEMPTS:-3}"
AADS_REVIEW_MAX_RUNTIME="${AADS_REVIEW_MAX_RUNTIME:-$((AADS_REVIEW_MAX_TIME * AADS_REVIEW_MAX_ATTEMPTS + 120))}"
# 최초 리뷰도 durable request를 먼저 저장한 뒤 상태를 폴링한다. 이 대기 예산은
# 후보별 모델 timeout과 별개다. 서버의 async deadline(기본 500초)이 모든 DB
# 후보를 소진할 시간을 갖도록 여유를 둔다.
AADS_REVIEW_ASYNC_WAIT_SEC="${AADS_REVIEW_ASYNC_WAIT_SEC:-540}"
AADS_REVIEW_POLL_INTERVAL="${AADS_REVIEW_POLL_INTERVAL:-3}"
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
# 배포 락 TTL 갱신 — 응답 JSON 을 그대로 출력한다(무응답이면 빈 문자열). acquire 는 SET NX 라
# 홀더가 다시 불러도 TTL 이 늘지 않으므로 전용 엔드포인트를 쓴다.
_renew_deploy_lock() {
    local project="$1" job_id="$2"
    curl -sf --connect-timeout 3 --max-time 10 -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/deploy/renew?project=${project}&session_id=${job_id}" 2>/dev/null || true
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

approved_document_runner_brief() {
    # The claimed job is the server-side tenant/project authority. No instruction text
    # or workspace display name participates in document scope resolution.
    local job_id="$1" project="$2" rows=""
    local unavailable='승인된 정본 문서 없음/조회 불가'
    [[ "$job_id" =~ ^[a-zA-Z0-9_-]+$ && "$project" =~ ^[A-Z0-9][A-Z0-9_-]{0,63}$ ]] || { printf '%s' "$unavailable"; return; }
    rows=$(db_exec "SELECT COALESCE(json_agg(json_build_object('key',d.document_key,'title',d.title,'version',d.version,'excerpt',d.excerpt))::text,'[]')
        FROM (SELECT h.document_key,r.title,r.version,left(r.content,1300) AS excerpt
              FROM pipeline_jobs p JOIN project_document_heads h
                ON h.tenant_id=p.tenant_id AND h.project_key=p.project
              JOIN project_document_revisions r ON r.id=h.approved_revision_id
                AND r.head_id=h.id AND r.tenant_id=h.tenant_id AND r.project_key=h.project_key
              WHERE p.job_id='${job_id}' AND p.tenant_id IS NOT NULL AND p.project='${project}'
                AND EXISTS (
                  SELECT 1 FROM chat_sessions s JOIN chat_workspaces w ON w.id=s.workspace_id
                  JOIN project_document_grants g ON g.tenant_id=p.tenant_id
                    AND g.project_key=p.project AND g.user_id=s.user_id::text
                    AND g.access IN ('read','write','approve')
                  WHERE s.id=p.chat_session_id AND s.tenant_id=p.tenant_id
                    AND w.tenant_id=p.tenant_id AND w.project_key=p.project)
              ORDER BY h.updated_at DESC LIMIT 8) d;" 2>/dev/null) || rows=''
    [[ -n "$rows" ]] || { printf '%s' "$unavailable"; return; }
    printf '%s' "$rows" | python3 -c '
import json, re, sys
empty = "승인된 정본 문서 없음/조회 불가"
secret = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16})|(?i:(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|secret[_-]?key|private[_-]?key|database[_-]?url)[\x27\"]?\s*[:=]\s*[\x27\"]?[^\s\x27\"]+)")
try:
    rows = json.load(sys.stdin)
    parts = ["[프로젝트 승인 정본 문서]", "아래는 승인 포인터가 가리키는 문서의 발췌입니다."]
    for row in rows[:8]:
        raw_title, raw_excerpt = str(row.get("title") or ""), str(row.get("excerpt") or "")
        title, excerpt = raw_title[:120], raw_excerpt[:1200]
        if not excerpt or secret.search(raw_title) or secret.search(raw_excerpt):
            continue
        item = "\n- %s (%s): %s\n%s" % (str(row.get("key") or "")[:128], str(row.get("version") or "")[:32], title, excerpt)
        if len("\n".join(parts)) + 1 + len(item) > 3000:
            break
        parts.append(item)
    print("\n".join(parts) if len(parts) > 2 else empty)
except (ValueError, TypeError, AttributeError):
    print(empty)
' 2>/dev/null || printf '%s' "$unavailable"
}

# 실패 지점에서 오류 사전(ohvis_wiki_error_book)을 조회한다.
#
# 2026-09-14, 같은 실패를 세 세션이 "러너 계정 문제" 로 보고했다. 실제 원인은
# 호스트/컨테이너 경로 불일치였고 로그에는 Codex 시작 배너만 남아 있었다.
# 증상에서 원인으로 가는 길이 없으면 사람이 매번 처음부터 추적한다.
#
# 조회 실패는 무시한다 — 사전이 없다고 작업을 막으면 안 된다.
lookup_error_book() {
    local err_file="$1" job_id="${2:-}"
    [[ -s "$err_file" ]] || return 0
    local book="/root/aads/aads-server/scripts/error_book.py"
    [[ -x "$book" ]] || return 0
    local hit
    # --record: 사전에 없는 오류는 후보로 남긴다. 원인은 비워 두되 증상이
    # 어디에도 안 남는 일은 막는다. 다음 사람이 원인을 채워 active 로 올린다.
    # match 는 "알려진 오류 없음" 일 때 종료코드 1 을 준다. 그 경우에도 후보
    # 기록 메시지가 출력에 있으므로 종료코드로 버리면 안 된다.
    hit=$(timeout 30 "$book" match "$err_file" --bump --record --source "runner:${job_id}" 2>/dev/null || true)
    # 알려진 오류든 새 후보든 로그에 남긴다. 후보가 조용히 기록되면 운영자는
    # 새 오류가 사전에 들어온 사실 자체를 모른다.
    case "$hit" in
        *"알려진 오류:"*|*"후보로 기록:"*) ;;
        *) return 0 ;;
    esac
    while IFS= read -r line; do
        [[ -n "$line" ]] && log "  ERROR_BOOK job=${job_id} ${line}"
    done <<< "$hit"
}

db_update() {
    # psql 은 SQL 이 실패해도 종료코드 0 을 돌려준다(ON_ERROR_STOP 없을 때).
    # 여기에 stderr 까지 버리고 있었으므로 **쓰기 실패가 완전히 보이지 않았다.**
    #
    # 2026-09-16 runner-1bf4a718 (AADS-AAG-001-R2, L 사이즈 33분 작업)이 이렇게 사라졌다:
    #   06:02:05 AI_REVIEW_HOLD  → status='review_hold' UPDATE 가 적용되지 않음
    #   06:03:02 WATCHDOG_DEAD_PROCESS → status 가 아직 'running' 이라 error 로 전환
    # 행을 열어보면 review_feedback 에 [AI Reviewer] 줄이 없고 watchdog 줄만 있다.
    # result_output·git_diff 도 0바이트다 — 그 UPDATE 는 통째로 실패했다.
    # 결과: "리뷰 인프라 장애(재검수 가능)" 가 "프로세스 사망" 으로 둔갑하고
    # 33분치 산출물과 diff 가 DB 에서 사라졌다.
    #
    # 실패를 치료하지는 못해도 **보이게는 만든다.** 원인을 모르는 채 조용히
    # 지나가는 쪽이 훨씬 비싸다.
    #
    # 종료코드는 언제나 0 이다 — 이 스크립트는 `set -e` 로 돈다. 여기서
    # 실패를 반환하면 DB 한 줄 때문에 러너 전체가 죽는다.
    local _rc=0 _out="" _sql_head="" _err_tail=""
    _out=$(printf '%s' "$1" | _psql_cmd -v ON_ERROR_STOP=1 2>&1) || _rc=$?
    if (( _rc != 0 )); then
        # head -c 는 pipe 의 읽기 끝을 먼저 닫아 pipefail 환경에서 진단 자체가
        # Broken pipe 를 만들 수 있다. Bash substring 으로 잘라 원래 DB 오류만 남긴다.
        _sql_head="${1:0:160}"
        _err_tail="${_out: -400}"
        _sql_head="${_sql_head//$'\n'/ }"
        _err_tail="${_err_tail//$'\n'/ }"
        log "  DB_UPDATE_FAILED rc=${_rc} sql_head=${_sql_head} err=${_err_tail}"
    fi
    return 0
}

RUNNER_CLI_USAGE_BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runner_cli_usage.py"

# AADS-LLM-M9-COST-BASIS (2026-09-30): 러너 CLI 사용량을 oauth_usage_log 에
# job_id 와 함께 남긴다. 그동안 러너는 CLI 를 직접 띄우고 아무것도 적지 않아
# 러너 비용이 러너 작업에 귀속되지 않았다(pipeline_jobs 조인 0건).
#
# claude_cli 는 사용량을 얻으려고 --output-format json 으로 띄운다. 그러면 출력
# 파일이 JSON 원문이 되므로 되돌리기는 러너 정확성의 일부다. 그래서 둘을 나눈다
# (rework 1, 2026-09-30 리뷰 지적 1·2):
#   runner_cli_usage_ready          헬퍼·python3 가 없으면 json 으로 띄우지 않는다.
#   restore_runner_claude_output    출력 파일을 text 결과로 되돌리고 **검사한다**.
#                                   되돌리지 못하면 1 — 호출자가 시도를 실패로 본다.
#   record_runner_cli_usage         보존된 원문을 읽기만 한다. 실패해도 사용량 한
#                                   건을 잃을 뿐 출력 파일은 건드리지 않는다.
runner_cli_usage_ready() {
    [[ -f "$RUNNER_CLI_USAGE_BIN" ]] && command -v python3 >/dev/null 2>&1
}

runner_output_is_cli_result_json() {
    # CLI 결과 객체는 {"type":"result",...} 한 줄이다. 파일이 그것으로 **시작할 때만** 참이다 —
    # 되돌린 text 결과가 본문에서 이 문자열을 언급해도 실패로 오판하지 않는다.
    # grep -q 파이프는 쓰지 않는다 — 일찍 닫히면 SIGPIPE(141)가 pipefail 로 거짓이 된다.
    local f="$1" head_text=""
    [[ -s "$f" ]] || return 1
    head_text=$(head -c 256 "$f" 2>/dev/null | tr -d ' \t\r\n') || true
    [[ "$head_text" == '{"type":"result"'* ]]
}

restore_runner_claude_output() {
    local job_id="$1" output_file="$2"
    local restore_out="" rc=0
    restore_out=$(timeout 30 python3 "$RUNNER_CLI_USAGE_BIN" restore --output-file "$output_file" 2>&1) || rc=$?
    if runner_output_is_cli_result_json "$output_file" && command -v jq >/dev/null 2>&1; then
        # 헬퍼가 실패했다(시간초과·예외). 헬퍼와 무관한 jq 로 한 번 더 되돌린다.
        [[ -s "${output_file}.usage.json" ]] || cp -f "$output_file" "${output_file}.usage.json"
        if jq -er 'select(.type == "result") | (.result // "")' "${output_file}.usage.json" > "${output_file}.tmp" 2>/dev/null; then
            mv -f "${output_file}.tmp" "$output_file"
        else
            rm -f "${output_file}.tmp"
        fi
    fi
    if runner_output_is_cli_result_json "$output_file"; then
        log "  RUNNER_CLI_JSON_RESTORE_FAILED job=$job_id rc=$rc detail='${restore_out:0:200}' → 출력이 JSON 원문이라 이 시도를 실패로 본다"
        return 1
    fi
    [[ $rc -eq 0 ]] || log "  RUNNER_CLI_JSON_RESTORE_WARN job=$job_id rc=$rc detail='${restore_out:0:200}' (출력은 text 로 확인됨)"
    return 0
}

# 실행 모델 영수증 (2026-10-02). claude_cli 를 json 으로 띄우면 CLI 가 실제로 쓴
# 모델을 modelUsage 로 보고한다. 그 메인 모델을 actual_model 로 남긴다. 근거가 없으면
# 빈 문자열 — 호출자는 unverified 를 유지한다(추측하지 않는다).
runner_claude_receipt_model() {
    local output_file="$1" m=""
    runner_cli_usage_ready || return 0
    m=$(timeout 15 python3 "$RUNNER_CLI_USAGE_BIN" model --output-file "$output_file" 2>/dev/null) || m=""
    [[ "$m" =~ ^[A-Za-z0-9._:-]{1,60}$ ]] && printf '%s' "$m"
    return 0
}

record_runner_cli_usage() {
    local job_id="$1" kind="$2" output_file="$3" slot="$4" model="$5" exit_code="$6" duration_ms="$7"
    [[ -n "$job_id" ]] || return 0
    if ! runner_cli_usage_ready; then
        log "  RUNNER_CLI_USAGE_SKIP job=$job_id reason=helper_or_python3_missing bin=$RUNNER_CLI_USAGE_BIN"
        return 0
    fi
    local usage_sql="" usage_err rc=0
    usage_err=$(mktemp 2>/dev/null || echo "/tmp/runner_cli_usage.$$.err")
    usage_sql=$(timeout 30 python3 "$RUNNER_CLI_USAGE_BIN" usage \
        --kind "$kind" --output-file "$output_file" --job-id "$job_id" \
        --slot "$slot" --model "$model" \
        --exit-code "${exit_code:-0}" --duration-ms "${duration_ms:-0}" 2>"$usage_err") || rc=$?
    if [[ $rc -ne 0 || -z "$usage_sql" ]]; then
        # 조용히 삼키지 않는다 — 사용량 누락은 비용 계측 결손이다.
        log "  RUNNER_CLI_USAGE_FAILED job=$job_id kind=$kind rc=$rc err='$(head -c 300 "$usage_err" 2>/dev/null | tr '\n' ' ')'"
    else
        db_update "$usage_sql"
    fi
    rm -f "$usage_err"
    return 0
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

wait_runner_cli_process() {
    local job_id="$1" pid="$2" output_file="$3" err_file="$4" model="$5" actual_model="$6" size="$7"
    local attempt_no="$8" total_attempts="$9" cycle_num="${10}" runner_kind="${11}" started_ms="${12}" retry_no="${13:-0}"
    local first_stdout_ms="" first_stderr_ms="" now_ms elapsed_ms proc_stat

    while true; do
        proc_stat=$(ps -p "$pid" -o stat= 2>/dev/null || true)
        [[ -z "$proc_stat" || "$proc_stat" == *Z* ]] && break
        if [[ -z "$first_stdout_ms" && -s "$output_file" ]]; then
            now_ms=$(date +%s%3N 2>/dev/null || date +%s000)
            elapsed_ms=$((now_ms - started_ms))
            first_stdout_ms="$elapsed_ms"
            record_runner_event "$job_id" "cli_first_stdout" "running" "claude_code_work" "$model" "$actual_model" "$size" "$elapsed_ms" "{\"attempt\":${attempt_no},\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"runner_kind\":\"${runner_kind}\",\"pid\":${pid},\"retry\":${retry_no}}"
        fi
        if [[ -z "$first_stderr_ms" && -s "$err_file" ]]; then
            now_ms=$(date +%s%3N 2>/dev/null || date +%s000)
            elapsed_ms=$((now_ms - started_ms))
            first_stderr_ms="$elapsed_ms"
            record_runner_event "$job_id" "cli_first_stderr" "running" "claude_code_work" "$model" "$actual_model" "$size" "$elapsed_ms" "{\"attempt\":${attempt_no},\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"runner_kind\":\"${runner_kind}\",\"pid\":${pid},\"retry\":${retry_no}}"
        fi
        sleep 1
    done

    if [[ -z "$first_stdout_ms" && -s "$output_file" ]]; then
        now_ms=$(date +%s%3N 2>/dev/null || date +%s000)
        elapsed_ms=$((now_ms - started_ms))
        record_runner_event "$job_id" "cli_first_stdout" "running" "claude_code_work" "$model" "$actual_model" "$size" "$elapsed_ms" "{\"attempt\":${attempt_no},\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"runner_kind\":\"${runner_kind}\",\"pid\":${pid},\"retry\":${retry_no},\"captured_after_exit\":true}"
    fi
    if [[ -z "$first_stderr_ms" && -s "$err_file" ]]; then
        now_ms=$(date +%s%3N 2>/dev/null || date +%s000)
        elapsed_ms=$((now_ms - started_ms))
        record_runner_event "$job_id" "cli_first_stderr" "running" "claude_code_work" "$model" "$actual_model" "$size" "$elapsed_ms" "{\"attempt\":${attempt_no},\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"runner_kind\":\"${runner_kind}\",\"pid\":${pid},\"retry\":${retry_no},\"captured_after_exit\":true}"
    fi

    wait "$pid"
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
        SELECT 2 AS group_order,
               CASE
                 WHEN provider IN ('codex','openai') AND model_id LIKE 'gpt-%' THEN 'codex:' || model_id
                 WHEN provider = 'anthropic' THEN model_id
                 WHEN provider IN ('gemini','google','deepseek','kimi','minimax','qwen','groq','openrouter','litellm') THEN 'litellm:' || model_id
                 WHEN position(':' in model_id) > 0 THEN model_id
                 ELSE provider || ':' || model_id
               END AS value,
               row_number() OVER (PARTITION BY route_key ORDER BY is_default DESC, display_order ASC, provider ASC, model_id ASC) AS ord
        FROM model_routing_preferences mrp
        -- 일반 chat llm 라우트에는 PC/Vision 등 코드 러너와 호환되지 않는
        -- 모델도 포함된다. 러너는 검증된 runner_llm 라우트만 폴백한다.
        WHERE mrp.route_key = 'runner_llm'
          AND mrp.is_enabled = TRUE
          -- codex 실행 가드: codex 로 해석되는 행은 llm_models is_executable=TRUE 만 통과.
          -- Python 대응: app/services/pipeline_runner_service.py 의 codex_executable 가드
          -- (Python 은 provider=openai 만, 여기선 codex 로 해석되는 codex/openai 전체 — 실행 경로가 같은 codex CLI 이므로).
          -- group_order 0/1 (CEO 지정 runner_model_config) 에는 걸지 않는다.
          AND (
            NOT (mrp.provider IN ('codex','openai') AND mrp.model_id LIKE 'gpt-%')
            OR EXISTS (
              SELECT 1 FROM llm_models lm
              WHERE lm.provider = 'codex'
                AND lm.model_id = mrp.model_id
                AND lm.is_executable = TRUE
            )
          )
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
        claude-*|opus|sonnet|haiku)
            # Keep invalid IDs unchanged for explicit launch rejection below;
            # do not crash the polling runner while building its model cycle.
            python3 "$CLAUDE_MODEL_CONTRACT" "$model" || printf '%s\n' "$model"
            ;;
        groq-*|minimax-*|kimi-*|qwen*|deepseek-*|gemini-*|dashscope-*|openrouter-*|glm-*)
            # AADS-RUNNER-LITELLM-PREFIX (2026-09-17)
            # runner_model_config 의 AI_REVIEW 목록은 provider 정보 없이 원시 id 를
            # 담는다("groq-gpt-oss-120b"). 접두사가 없으면 러너가 이를 Claude CLI
            # 모델로 해석해 MODEL_CONTRACT_REJECTED 로 통째로 건너뛴다.
            # 2026-09-17 실측: ACCT 러너(jinah244)에서 Claude 주간한도·Gemini 키
            # 정지·kimi/minimax 한도·deepseek 잔액부족으로 20개 사다리가 전멸했는데,
            # 유일하게 200 을 돌려주던 groq 두 개가 바로 이 이유로 시도조차 되지 않았다.
            echo "litellm:${model}"
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
    python3 "$CLAUDE_MODEL_CONTRACT" "${1:-}"
}

# 슬롯 자격증명 경로를 stdout 으로 돌려준다.
# 쓸 수 없으면 아무것도 출력하지 않고 1 을 반환해 호출측이 고정 토큰으로 폴백한다.
# accessToken 만 있고 refreshToken 이 없는 파일은 갱신이 불가능하므로 거부한다.
slot_credentials_file() {
    local slot="${1:-1}"
    [[ "$RUNNER_USE_SLOT_CREDENTIALS" == "1" ]] || return 1
    [[ -x "$CLAUDE_SLOT_CREDENTIAL_WRAPPER" ]] || return 1
    command -v flock >/dev/null 2>&1 || return 1
    local cred="${CLAUDE_RELAY_SLOT_HOME_ROOT}/slot${slot}/.claude/.credentials.json"
    [[ -f "$cred" ]] || return 1
    python3 - "$cred" <<'PY' || return 1
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    raise SystemExit(1)
oauth = payload.get("claudeAiOauth", payload)
if not isinstance(oauth, dict):
    raise SystemExit(1)
for field in ("accessToken", "refreshToken"):
    value = oauth.get(field)
    if not isinstance(value, str) or not value:
        raise SystemExit(1)
PY
    printf '%s' "$cred"
}

# contabo116 이 10분마다 배달하는 읽기 전용 단기 lease. 원격 서버에는
# refresh token을 복사하지 않으므로 서버 간 회전 경합이 없다. 만료 5분 이내
# 토큰은 거부해 실행 중 401이 나는 것을 막는다. stdout은 토큰 전용이며 로그하지 않는다.
leased_slot_token() {
    local slot="${1:-1}"
    local cred="${CLAUDE_LEASE_SLOT_HOME_ROOT}/slot${slot}/.claude/.credentials.json"
    [[ -f "$cred" ]] || return 1
    python3 - "$cred" <<'PY' || return 1
import json
import sys
import time

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    raise SystemExit(1)
oauth = payload.get("claudeAiOauth", payload)
token = oauth.get("accessToken") if isinstance(oauth, dict) else None
expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
try:
    expires_at = float(expires_at)
    if expires_at > 100_000_000_000:
        expires_at /= 1000.0
except (TypeError, ValueError):
    raise SystemExit(1)
if not isinstance(token, str) or not token or expires_at <= time.time() + 300:
    raise SystemExit(1)
sys.stdout.write(token)
PY
}

# DB(llm_api_keys) 기반 Anthropic 계정 사다리 (AADS-RUNNER-SLOT4, 2026-09-19)
#
# 왜 필요한가. 여기는 오래도록 `i % 2 + 1` 로 계정1·2만 돌았다. 그런데 대표님
# 계정은 DB 에 4개가 등록돼 있고 우선순위 1번(슬롯4)은 한도가 남아 있었다.
# 2026-09-19 05:36~05:44 KST 실측: 계정1·2 가 동시에 주간한도(해제 09-19 23:59 /
# 09-23 02:59 KST)에 걸린 동안 사다리 15칸 중 claude 8칸이 전부 헛시도로 탔고,
# 슬롯3·4 는 단 한 번도 시도되지 않았다. 한도가 남았는데 러너만 멈춰 있었다.
#
# 규칙 셋.
#  1. rate_limited_until 이 지난/비어 있는 계정만 돌려준다 — 죽은 계정을 때리지 않는다.
#  2. priority 오름차순 — 대표님이 대시보드에서 정한 순서가 그대로 사다리가 된다.
#  3. 실제로 쓸 수 있는 슬롯만 남긴다. 중앙은 refresh 가능 자격증명을,
#     원격 서버는 contabo116이 배달한 4개 단기 lease를 사용한다.
# 조회 실패 시 아무것도 출력하지 않고 1 을 반환해 호출측이 기존 2슬롯 경로로 폴백한다.
get_db_anthropic_slots() {
    local rows
    rows=$(db_exec "SELECT CASE key_name
                             WHEN 'ANTHROPIC_AUTH_TOKEN'   THEN '1'
                             WHEN 'ANTHROPIC_AUTH_TOKEN_2' THEN '2'
                             WHEN 'ANTHROPIC_AUTH_TOKEN_3' THEN '3'
                             WHEN 'ANTHROPIC_AUTH_TOKEN_4' THEN '4'
                             ELSE '' END AS slot
                    FROM llm_api_keys
                    WHERE provider='anthropic'
                      AND is_active = TRUE
                      AND (rate_limited_until IS NULL OR rate_limited_until <= NOW())
                    ORDER BY priority ASC, id ASC;" 2>/dev/null) || return 1
    [[ -z "$rows" ]] && return 1
    local slot out=""
    while IFS= read -r slot; do
        slot="${slot//[^0-9]/}"
        [[ -z "$slot" ]] && continue
        if [[ -n "$(slot_credentials_file "$slot" || true)" ]]; then
            out+="${slot}"$'\n'
        elif leased_slot_token "$slot" >/dev/null 2>&1; then
            out+="${slot}"$'\n'
        elif [[ "$slot" == "1" && -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
            out+="${slot}"$'\n'
        elif [[ "$slot" == "2" && -n "${ANTHROPIC_AUTH_TOKEN_2:-}" ]]; then
            out+="${slot}"$'\n'
        fi
    done <<< "$rows"
    [[ -z "$out" ]] && return 1
    printf '%s' "$out"
}

# 한도가 남은 Codex 계정의 홈 디렉터리를 stdout 으로 돌려준다.
#
# 전역 쿨다운 마커(/tmp/aads-codex-auth-disabled-until) 하나가 codex 전체를
# 막고 있었다. 2026-09-19 실측: CODEX_OAUTH_JINAH(priority 1)는 한도가 남아
# 있는데도 MAIN 계정이 터뜨린 마커 때문에 사다리의 codex 6칸이 통째로 skip 됐다.
# 계정 홈은 materialize_codex_accounts.py 가 만들어 두고, CODEX_HOME 으로
# 계정을 고르는 방식은 codex_usage.py 가 이미 쓰고 있는 것과 같다.
codex_pick_account_home() {
    local state="${AADS_CODEX_ACCOUNTS_STATE:-/root/.codex-accounts/state.json}"
    [[ -f "$state" ]] || return 1
    python3 - "$state" <<'PY' || return 1
import json
import os
import re
import sys
import time

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    raise SystemExit(1)

now = time.time()
prefix = os.environ.get("AADS_CODEX_REVOKED_MARKER_PREFIX") or "/tmp/aads-codex-revoked-"
best = None
for acct in payload.get("accounts", []):
    if not isinstance(acct, dict):
        continue
    if not acct.get("is_active") or not acct.get("has_auth"):
        continue
    # 서버측 폐기(401 token_revoked)는 로컬 만료와 무관하다. codex_usage.py 가 state.json 에
    # 내려주는 auth_usable 이 false 면 건너뛴다. 필드가 없으면(옛 state.json) 쓸 수 있는 것으로 본다.
    if acct.get("auth_usable") is False:
        continue
    until = acct.get("rate_limited_until_epoch")
    try:
        if until is not None and float(until) > now:
            continue
    except (TypeError, ValueError):
        continue
    try:
        prio = int(acct.get("priority", 9999))
    except (TypeError, ValueError):
        prio = 9999
    name = acct.get("key_name") or ""
    if not name:
        continue
    # 러너가 401 을 직접 본 계정의 계정별 마커(mark_codex_account_revoked). 전역 마커가 아니다.
    marker = prefix + re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            if float(handle.read().strip() or 0) > now:
                continue
        os.unlink(marker)
    except (OSError, ValueError):
        pass
    if best is None or prio < best[0]:
        best = (prio, name)

if best is None:
    raise SystemExit(1)
home = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), best[1])
if not os.path.isdir(home):
    raise SystemExit(1)
print(home)
PY
}

# 읽기전용은 앞 20줄 안에서 줄 전체가 `MODE: READ_ONLY`(대소문자 무시, 앞뒤 공백 허용)일 때만 참이다.
# app/services/pipeline_runner_service.py 의 _is_read_only_instruction 과 같은 규칙이다 — 한쪽만 고치지 마라.
# 2026-10-03 실측: 본문 자연어("수정하지", "read-only" 등)로 판정하던 때 새 파일만 만든 작업
# (runner-4bc5f67e, runner-cb6d75c2)이 읽기전용으로 오판돼 done 처리 후 worktree 째 소실됐다.
is_read_only_instruction() {
    local instruction="${1:-}"
    printf '%s' "$instruction" | head -20 | grep -qiE '^[[:space:]]*MODE[[:space:]]*:[[:space:]]*READ_ONLY[[:space:]]*$'
}

# stdin: NUL 구분 경로 목록. $1: git add 플래그(-N 기본, 무시된 파일 복구는 -fN).
# ls-files 결과를 줄 단위로 xargs 에 넘기면 core.quotePath 때문에 한글 파일명이
# "d/\352\270\260...md" 로 따옴표·8진수 출력돼 pathspec 불일치로 명령 전체가 실패(rc=123)하고,
# ASCII 파일까지 하나도 intent-to-add 되지 않았다. 일괄 실패 시 파일별로 재시도하고 결과를 log 로 남긴다.
_intent_to_add_nul() {
    local _flag="${1:--N}" _p="" _total=0 _failed=0 _rc=0
    local -a _paths=()
    while IFS= read -r -d '' _p; do
        [[ -n "$_p" ]] && _paths+=("$_p")
    done
    _total=${#_paths[@]}
    [[ $_total -eq 0 ]] && return 0
    printf '%s\0' "${_paths[@]}" | xargs -0 -r git add "$_flag" -- 2>/dev/null
    _rc=$?
    if [[ $_rc -ne 0 ]]; then
        for _p in "${_paths[@]}"; do
            git add "$_flag" -- "$_p" 2>/dev/null || _failed=$((_failed + 1))
        done
        log "  INTENT_TO_ADD_BATCH_FAILED flag=$_flag batch_rc=$_rc files=$_total failed=$_failed — 파일별 재시도"
    fi
    return 0
}

is_deploy_only_instruction() {
    local instruction="${1:-}"
    # DEPLOY_ONLY_HEADER_SQL 과 같은 규칙이다 — 한쪽만 고치지 마라.
    printf '%s' "$instruction" | head -20 | grep -qE '^[[:space:]]*DEPLOY_ONLY[[:space:]]*:[[:space:]]*true([^[:alnum:]_]|$)'
}

# is_deploy_only_instruction(셸)과 같은 규칙의 SQL 표현(별칭 p 고정). 앞 20줄 안에서
# 줄 시작이 `DEPLOY_ONLY: true` 인 잡만 릴리스잡이다. 본문 산문의 언급은 릴리스잡이 아니다.
# 경고: 두 판정은 반드시 같이 고쳐라. 한쪽만 고치면 러너는 릴리스잡으로 보는데 가드는 취소하는 괴리가 생긴다.
# (tests/check_deploy_only_guard.sh R3 가 두 판정의 일치를 검사한다.)
# 2026-09-30 실측: ILIKE '%DEPLOY_ONLY%' 본문 매칭이 일반 코드잡 runner-6a9a2950 을
# 릴리스잡으로 오탐해 취소·해제 분기를 모두 우회, 큐가 2시간 40분 멈췄다.
DEPLOY_ONLY_HEADER_SQL="array_to_string((string_to_array(p.instruction, chr(10)))[1:20], chr(10)) ~ '(^|\\n)[[:space:]]*DEPLOY_ONLY[[:space:]]*:[[:space:]]*true\\y'"

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
    # 모델 출력과 diff 를 byte 단위(head -c)로 제한하면 마지막 UTF-8 문자가
    # 중간에서 잘릴 수 있다. PostgreSQL은 그 한 바이트 때문에 UPDATE 전체를
    # 거부한다(runner-a6626b7f). DB 경계에서 유효한 UTF-8만 통과시킨다.
    val=$(printf '%s' "$val" | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null) || true
    # $esc$ 토큰이 포함되면 제거 (인젝션 방지)
    val="${val//\$esc\$/}"
    echo "\$esc\$${val}\$esc\$"
}

looks_like_git_diff() {
    local content="$1"
    # 공백 여부 판정에 ${content//[[:space:]]/} 를 쓰면 43KB diff 하나에 11.3초가
    # 걸린다(2026-09-17 실측). =~ 는 첫 비공백 문자에서 멈춘다 — 0.013초.
    [[ "$content" =~ [^[:space:]] ]] || return 1

    # grep 을 파이프로 먹이지 않는다. `printf | grep -q` 는 grep 이 첫 줄에서
    # 매치하고 즉시 빠져나가므로, 아직 50KB 를 쓰고 있던 printf 가 EPIPE/SIGPIPE
    # 로 죽는다(종료코드 141). 이 스크립트는 `set -o pipefail` 이라 그 141 이
    # 파이프라인 결과가 되고, **유효한 diff 인데 INVALID_GIT_DIFF 로 반려**된다.
    # 2026-09-17 runner-5b77fc1f 가 그렇게 죽었다 — 저장된 git_diff 43,837자는
    # 정상 diff 였고 테스트·gitleaks 도 통과한 상태였다.
    # here-string 은 파이프라인이 아니므로 grep 자신의 종료코드만 남는다.
    if grep -q '^diff --git a/.* b/.*$' <<< "$content"; then
        return 0
    fi

    if grep -q '^--- ' <<< "$content" \
        && grep -q '^+++ ' <<< "$content" \
        && grep -q '^@@ ' <<< "$content"; then
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

# ── push 대상 조상관계 사전 판별 (AADS-RUNNER-PUSH-STALE-BASE) ──────────
# origin/main 과 승인 SHA 의 조상 관계로 push 가능 여부를 미리 가른다.
#   already_present : 승인 SHA 가 이미 origin/main 에 포함 → push 불필요(멱등)
#   fast_forward    : origin/main 이 승인 SHA 의 조상 → 정상 push 가능
#   stale_base      : 어느 쪽도 조상이 아님 → base 가 낡음(non-fast-forward)
#   fetch_fail      : 원격 조회 실패 → 판별 불가, 기존 push 경로로 진행
# force push 는 어떤 경우에도 하지 않는다. stale_base 는 재작업/재승인 대상이다.
classify_push_state() {
    local repo="$1" sha="$2" remote_branch="${3:-main}"
    local remote_sha=""
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "fetch_fail"; return 0; }
    remote_sha=$(git -C "$repo" ls-remote origin "refs/heads/${remote_branch}" 2>/dev/null | awk 'NR==1{print $1}') || true
    if [[ ! "$remote_sha" =~ ^[0-9a-f]{40}$ ]]; then
        echo "fetch_fail"
        return 0
    fi
    if ! git -C "$repo" cat-file -e "${remote_sha}^{commit}" 2>/dev/null; then
        git -C "$repo" fetch --quiet origin "${remote_branch}" 2>/dev/null || true
    fi
    if ! git -C "$repo" cat-file -e "${remote_sha}^{commit}" 2>/dev/null; then
        echo "fetch_fail"
        return 0
    fi
    if git -C "$repo" merge-base --is-ancestor "$sha" "$remote_sha" 2>/dev/null; then
        echo "already_present"
    elif git -C "$repo" merge-base --is-ancestor "$remote_sha" "$sha" 2>/dev/null; then
        echo "fast_forward"
    else
        echo "stale_base"
    fi
}

# ── stale_base 자동 rebase (AADS-RUNNER-PUSH-AUTO-REBASE) ──────────────
# 2026-09-16 하루에 같은 원인으로 세 건이 멈췄다(cd394808 외). 승인과 push
# 사이에 origin/main 이 전진하면 승인 SHA 는 non-fast-forward 가 된다.
# force push 는 금지이고, "사람이 그때그때 rebase 한다" 는 규칙으로만 남는다 —
# 규칙으로만 남은 것은 또 일어난다(R-ERRBOOK).
#
# 다만 **양쪽이 건드린 파일이 하나도 겹치지 않으면** 옮겨 붙이는 것은 안전하다.
# 겹치면 사람의 명시 확인(review_feedback 의 `[REBASE-ATTESTED]`)이 있을 때만
# 시도한다. 텍스트로 안 겹쳐도 같은 파일이면 의미가 충돌할 수 있고, 그 판단은
# 사람 몫이기 때문이다. 확인이 있어도 실제 충돌·비FF 는 여전히 막는다.
#
# 성공하면 새 SHA 를 stdout 으로 돌려주고 0, 그 외에는 1 을 돌려준다.
# 내부 로그는 전부 stderr 로 보낸다 — stdout 은 SHA 전용이다.
# 실패해도 워크트리는 원래 SHA 로 되돌린다.
attempt_stale_base_rebase() {
    local repo="$1" sha="$2" job_id="$3" remote_branch="${4:-main}"
    local remote_sha="" base="" job_files="" inc_files="" overlap="" new_sha="" n_commits="" attested=""

    [[ "${AUTO_REBASE_STALE_BASE:-1}" == "0" ]] && return 1

    # 격리 워크트리에서만 한다. 라이브 저장소를 rebase 하면 다른 세션 작업이 날아간다.
    [[ "$repo" == "/tmp/aads-wt-${job_id}" ]] || return 1
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 1

    remote_sha=$(git -C "$repo" ls-remote origin "refs/heads/${remote_branch}" 2>/dev/null | awk 'NR==1{print $1}') || true
    [[ "$remote_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    git -C "$repo" fetch --quiet origin "${remote_branch}" 2>/dev/null || true
    git -C "$repo" cat-file -e "${remote_sha}^{commit}" 2>/dev/null || return 1

    base=$(git -C "$repo" merge-base "$sha" "$remote_sha" 2>/dev/null) || return 1
    [[ -n "$base" ]] || return 1

    # 옮겨 붙일 커밋 수가 많으면 하지 않는다 — 그 규모는 사람이 봐야 한다.
    n_commits=$(git -C "$repo" rev-list --count "${base}..${sha}" 2>/dev/null || echo 999)
    [[ "$n_commits" -ge 1 && "$n_commits" -le 3 ]] || return 1

    job_files=$(git -C "$repo" diff --name-only "$base" "$sha" 2>/dev/null | sort -u)
    inc_files=$(git -C "$repo" diff --name-only "$base" "$remote_sha" 2>/dev/null | sort -u)
    [[ -n "$job_files" ]] || return 1
    overlap=$(comm -12 <(printf '%s\n' "$job_files") <(printf '%s\n' "$inc_files") | head -5)
    if [[ -n "$overlap" ]]; then
        # 조회 실패·빈 결과·job_id 형식 이상은 모두 "표식 없음" (fail-closed)
        attested=""
        if [[ "$job_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            attested=$(db_exec "SELECT position('[REBASE-ATTESTED]' in COALESCE(review_feedback,'')) > 0 FROM pipeline_jobs WHERE job_id='${job_id}' LIMIT 1;" 2>/dev/null | tr -d '[:space:]') || attested=""
        fi
        if [[ "$attested" != "t" ]]; then
            log "  AUTO_REBASE_SKIP job=$job_id — 같은 파일을 양쪽이 건드림: $(printf '%s' "$overlap" | tr '\n' ' ')" >&2
            return 1
        fi
        log "  AUTO_REBASE_ATTESTED job=$job_id overlap=$(printf '%s' "$overlap" | tr '\n' ' ')" >&2
        record_runner_event "$job_id" "auto_rebase_attested" "info" >/dev/null 2>&1 || true
    fi

    if ! git -C "$repo" rebase --quiet --onto "$remote_sha" "$base" "$sha" >/dev/null 2>&1; then
        git -C "$repo" rebase --abort >/dev/null 2>&1 || true
        git -C "$repo" checkout --detach "$sha" >/dev/null 2>&1 || true
        log "  AUTO_REBASE_FAIL job=$job_id — rebase 실패, 원래 SHA 로 되돌림" >&2
        return 1
    fi

    new_sha=$(git -C "$repo" rev-parse HEAD 2>/dev/null) || true
    [[ "$new_sha" =~ ^[0-9a-f]{40}$ ]] || { git -C "$repo" checkout --detach "$sha" >/dev/null 2>&1 || true; return 1; }

    # 옮겨 붙인 결과가 실제로 fast-forward 인지 확인한다. 아니면 되돌린다.
    if [[ "$(classify_push_state "$repo" "$new_sha" "$remote_branch")" != "fast_forward" ]]; then
        git -C "$repo" checkout --detach "$sha" >/dev/null 2>&1 || true
        log "  AUTO_REBASE_REVERT job=$job_id — rebase 후에도 fast-forward 가 아님" >&2
        return 1
    fi

    log "  AUTO_REBASE_OK job=$job_id ${sha:0:8} -> ${new_sha:0:8} (겹친 파일 $(printf '%s' "$overlap" | grep -c .), 커밋 ${n_commits}개)" >&2
    printf '%s' "$new_sha"
    return 0
}

# ── 단일 커밋의 patch-id (AADS-RUNNER-APPROVAL-PATCHID-INHERIT) ────────────
# `git diff <sha>^ <sha> | git patch-id --stable` 의 첫 필드를 stdout 으로 낸다.
# 부모가 정확히 하나인 커밋만 계산한다 — 루트 커밋(<sha>^ 없음)·머지 커밋은
# "내용이 같다"를 증명할 수 없으므로 빈 문자열을 낸다. 실패는 항상 빈 문자열이고
# 반환값은 0 이다. 호출 측은 빈 값을 "재승인 필요" 로 읽어야 한다.
commit_patch_id() {
    local repo="$1" sha="$2" parents="" pid=""
    [[ -n "$repo" && "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
    git -C "$repo" cat-file -e "${sha}^{commit}" 2>/dev/null || return 0
    parents=$(git -C "$repo" rev-list --parents -n 1 "$sha" 2>/dev/null | awk '{print NF-1}') || parents=""
    [[ "$parents" == "1" ]] || return 0
    pid=$(git -C "$repo" diff "${sha}^" "$sha" 2>/dev/null | git -C "$repo" patch-id --stable 2>/dev/null | awk 'NR==1{print $1}') || pid=""
    [[ "$pid" =~ ^[0-9a-f]{40}$ ]] || return 0
    printf '%s' "$pid"
    return 0
}

# ── 자동 rebase 결과가 이전 CEO 승인을 상속할 수 있는가 ─────────────────
# 2026-09-30 runner-e3b882c2 는 patch-id 가 같은 SHA 4개(cd3e96f1→cddf31b8→
# 488e5764→136b739e)로 승인을 세 번 받다가 MAX_RUNTIME 7200s 에 zombie_killed 됐다.
# SHA 만 바뀌고 내용은 한 줄도 바뀌지 않았다.
#
# 상속은 **증명 가능한 조건** 에서만 한다. 하나라도 어긋나면 재승인이다.
#   - 마지막 approval_decision 이 approved 이고, 그것이 마지막 approval_requested
#     보다 뒤에 있다. 승인 SHA 는 그 approval_requested.metadata.commit_hash 다
#     (approval_decision metadata 에는 commit_hash 가 없다 — app/api/pipeline_runner.py).
#   - 승인 SHA 와 rebase SHA 가 모두 단일 커밋 변경이다(<sha>^ 가 origin/main 조상).
#     여러 커밋이면 마지막 커밋의 patch-id 만으로는 전체 내용을 증명할 수 없다.
#   - 두 patch-id 가 모두 계산되고 서로 같다.
#   - 이 잡의 누적 상속 횟수가 AADS_APPROVAL_INHERIT_MAX 미만이다.
#
# stdout: "inherit <사유> <승인SHA> <patch-id>" 또는 "reapprove <사유> <승인SHA|-> <patch-id|->"
inherit_approval_decision() {
    local job_id="$1" repo="$2" new_sha="$3"
    local max="${AADS_APPROVAL_INHERIT_MAX:-5}" approved_sha="" count="" old_pid="" new_pid="" c=""
    [[ "$max" =~ ^[0-9]+$ ]] || max=5

    approved_sha=$(db_exec "WITH req AS (
                              SELECT id, COALESCE(metadata->>'commit_hash','') AS sha
                              FROM pipeline_runner_events
                              WHERE job_id='${job_id}' AND event_type='approval_requested'
                              ORDER BY id DESC LIMIT 1),
                            dec AS (
                              SELECT id, status FROM pipeline_runner_events
                              WHERE job_id='${job_id}' AND event_type='approval_decision'
                              ORDER BY id DESC LIMIT 1)
                            SELECT req.sha FROM req, dec
                            WHERE dec.status='approved' AND dec.id > req.id;" 2>/dev/null | tr -d '[:space:]') || approved_sha=""
    if [[ ! "$approved_sha" =~ ^[0-9a-f]{40}$ ]]; then
        echo "reapprove no_approved_sha - -"
        return 0
    fi

    count=$(db_exec "SELECT COUNT(*) FROM pipeline_runner_events
                     WHERE job_id='${job_id}' AND event_type='approval_inherited_same_patch_id';" 2>/dev/null | tr -d '[:space:]') || count=""
    if [[ ! "$count" =~ ^[0-9]+$ ]]; then
        echo "reapprove inherit_count_unknown ${approved_sha} -"
        return 0
    fi
    if (( count >= max )); then
        echo "reapprove inherit_limit_exceeded(${count}/${max}) ${approved_sha} -"
        return 0
    fi

    for c in "$approved_sha" "$new_sha"; do
        if ! git -C "$repo" merge-base --is-ancestor "${c}^" origin/main 2>/dev/null; then
            echo "reapprove not_single_commit ${approved_sha} -"
            return 0
        fi
    done

    old_pid=$(commit_patch_id "$repo" "$approved_sha")
    new_pid=$(commit_patch_id "$repo" "$new_sha")
    if [[ -z "$old_pid" || -z "$new_pid" ]]; then
        echo "reapprove patch_id_unavailable ${approved_sha} -"
        return 0
    fi
    if [[ "$old_pid" != "$new_pid" ]]; then
        echo "reapprove patch_id_changed ${approved_sha} ${new_pid}"
        return 0
    fi
    echo "inherit same_patch_id ${approved_sha} ${new_pid}"
    return 0
}

# ── 지시서가 배포를 금지했는가 (AADS-RUNNER-DEPLOY-DIRECTIVE-GATE) ──────
# 0 = 금지(배포하지 마라), 1 = 제약 없음.
# 오탐(배포해도 되는데 건너뜀)은 사람이 별도 승인으로 배포하면 끝이지만,
# 미탐(금지인데 배포함)은 오늘처럼 운영 중인 안전장치를 되돌린다.
# 그래서 애매하면 건너뛰는 쪽으로 판정한다.
#
# 2026-09-18 (AADS-RUNNER-DEPLOY-FORBID-REGEX-P1): 고정 문자열 13개의 부분일치로는
# "빌드·배포 실행 금지" 처럼 트리거 단어와 금지어 사이에 다른 말이 낀 표현을 못 잡았다.
# 그래서 정규식으로 바꾸되, 트리거(배포/재기동/...)와 금지어(금지/하지 마/말라) 사이의
# 간격을 최대 12자로 좁게 묶어 무관한 문장까지 걸리는 것을 막는다.
#
# 2026-10-04 (AADS-RUNNER-PUSH-ONLY-ENFORCE): runner-9d5d8d45 지시문에
# "PUSH_ONLY. 빌드·배포·운영 migration 금지." 가 있었는데 게이트가 둘 다 놓쳐
# 07:54 KST 에 blue/green 배포가 실행됐다. 원인은 둘이다.
#   1) PUSH_ONLY 라는 구조화 선언을 아무도 읽지 않았다.
#   2) "배포" 와 "금지" 사이 간격이 12자로 묶여, "·운영 migration " (14자) 에서 빗나갔다.
# 아래 규칙은 기존 규칙에 **더하기만** 한다 — 이전에 막던 표현은 계속 막는다.
# 정규식은 줄/절 단위로 쪼갠 뒤 짧은 입력에만 적용한다(중첩 반복 금지, R-BG).
#
# 2026-10-06 (AADS-RUNNER-DEPLOY-FORBID-KO-NEGATION): GO100 runner-890f5c73 지시서의
# "## 금지\n배포·빌드·재기동·env 설정(…)·크론 변경을 하지 않는다." 가 통과해 go100.service 가
# 약 2.5분 중단됐다. 금지어가 (금지|하지 마|말라) 뿐이라 "하지 않는다/않습니다/안 한다" 부정형이
# 빠져 있었고, 트리거와 서술어 사이가 40자를 넘는 나열문은 어떤 정규식으로도 닿지 않았다.
#   1) 부정형(하지 않/하지 말/않는다/않습니다/안 한다/안 합니다)을 금지어에 더한다.
#      "배포 후 … 하지 않으면 안 된다" 같은 순서 문장은 오탐이므로 re_seq 연결어가 낀 간격을
#      건너뛴다. 이 필터가 없는 1차 정규식에는 기존 금지어에 "하지 말" 만 더한다.
#   2) "## 금지" / "금지:" / "금지 사항" 헤더 블록(다음 # 헤더 전까지)에 배포/빌드/재기동류
#      단어가 있으면 서술어와의 거리와 상관없이 금지로 본다.
instruction_forbids_deploy() {
    local text="$1" lowered="" scrubbed="" line="" clause="" rest="" gap="" trigger="" sep="" nl=$'\n'
    local re_sep='[_[:space:]-]?' re_bullet='^[[:space:]>*#-]*' re_word='([^[:alnum:]_]|$)'
    local re_off_push='push[_-]?only[[:space:]]*[:=][[:space:]]*(false|no|0|off)([^[:alnum:]_]|$)'
    local re_push_any='(^|[^[:alnum:]_])push[_-]?only([^[:alnum:]_]|$)'
    local re_push_line="${re_bullet}push[[:space:]]+only${re_word}"
    local re_policy_val="(push${re_sep}only|commit${re_sep}only|no${re_sep}deploy|forbid(den)?|deny|denied|none|block(ed)?)${re_word}"
    local re_policy="${re_bullet}(deploy(ment)?|release)${re_sep}policy[[:space:]]*[:=][[:space:]]*${re_policy_val}"
    local re_deploy_off="${re_bullet}deploy(ment)?[[:space:]]*[:=][[:space:]]*(false|no|off|0|forbid(den)?|deny|denied|none)${re_word}"
    local re_neg='(금지|하지[[:space:]]*(마|말|않)|않는다|않습니다|안[[:space:]]*(한다|합니다)|말라)'
    local re_wide="(배포|재기동|deploy)([^${nl}]{0,40})${re_neg}"
    local re_block_head='^[[:space:]>*-]*#*[[:space:]]*[*]*(금지|금지[[:space:]]*사항)[*]*[[:space:]]*([:：]|$)'
    local re_block_end='^[[:space:]]*#'
    local re_block_word='(배포|빌드|재기동|재시작|deploy|restart)'
    local in_block=0
    local re_seq='(후|뒤|이후|다음|하되|하고|하며|하면|전에|먼저|then|after|before)'
    [[ -n "$text" ]] || return 1
    lowered=$(printf '%s' "$text" | tr '[:upper:]' '[:lower:]')
    if [[ "$lowered" =~ (배포|재기동|빌드.{0,4}배포|deploy).{0,12}(금지|하지[[:space:]]*(마|말)|말라) ]]; then
        return 0
    fi
    if [[ "$lowered" =~ (커밋|push)[[:space:]]*까지만 ]]; then
        return 0
    fi
    if [[ "$lowered" =~ do[[:space:]_-]*not[[:space:]_-]*deploy ]] || [[ "$lowered" =~ no[[:space:]_-]*deploy ]]; then
        return 0
    fi

    # 구조화 선언: PUSH_ONLY / DEPLOY_POLICY: push_only / DEPLOY: false.
    # "PUSH_ONLY: false" 처럼 명시적으로 끈 표기만 제외한다(그 외 애매한 것은 막는다).
    scrubbed="$lowered"
    while [[ "$scrubbed" =~ $re_off_push ]]; do
        scrubbed="${scrubbed/"${BASH_REMATCH[0]}"/ }"
    done
    if [[ "$scrubbed" =~ $re_push_any ]]; then
        return 0
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ $re_push_line ]] || [[ "$line" =~ $re_policy ]] || [[ "$line" =~ $re_deploy_off ]]; then
            return 0
        fi
    done <<< "$scrubbed"

    # 금지 헤더 블록: "## 금지", "금지:", "금지 사항" 다음 줄부터 다음 # 헤더 전까지.
    # 헤더와 같은 줄에 적힌 내용("금지: 배포 …")도 블록에 포함한다.
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ $re_block_head ]]; then
            in_block=1
            rest="${line:${#BASH_REMATCH[0]}}"
            if [[ "$rest" =~ $re_block_word ]]; then
                return 0
            fi
            continue
        fi
        if (( in_block )); then
            if [[ "$line" =~ $re_block_end ]]; then
                in_block=0
            elif [[ "$line" =~ $re_block_word ]]; then
                return 0
            fi
        fi
    done <<< "$lowered"

    # 긴 한글 문구: 같은 절 안에서 트리거와 금지어 사이를 40자까지 허용한다.
    # "배포 후 … 하지 마" 처럼 순서/연결어가 낀 간격은 별개 문장으로 보고 건너뛴다.
    clause="$lowered"
    for sep in '.' '!' '?' ';' '。'; do
        clause="${clause//"$sep"/$nl}"
    done
    while IFS= read -r line || [[ -n "$line" ]]; do
        rest="$line"
        while [[ "$rest" =~ $re_wide ]]; do
            trigger="${BASH_REMATCH[1]}"
            gap="${BASH_REMATCH[2]}"
            if ! [[ "$gap" =~ $re_seq ]]; then
                return 0
            fi
            rest="${rest#*"$trigger"}"
        done
    done <<< "$clause"
    return 1
}

# 승인 시점/빌드 직전에 지시서를 DB 에서 다시 읽는다. 조회 실패·빈 값을 "제약 없음" 으로
# 해석하면 안 된다 — 그러면 DB 순단 한 번이 배포 허용이 된다(fail-closed).
# stdout=지시서, rc 0=읽음 / 1=확인 불가.
read_job_instruction_strict() {
    local job_id="$1" attempt=0 out="" rc=0
    [[ "$job_id" =~ ^[a-zA-Z0-9_-]+$ ]] || return 1
    while (( attempt < ${DEPLOY_DIRECTIVE_LOOKUP_ATTEMPTS:-3} )); do
        attempt=$((attempt + 1))
        rc=0
        out=$(db_exec "SELECT COALESCE(instruction,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null) || rc=$?
        if [[ "$rc" -eq 0 && -n "${out//[[:space:]]/}" ]]; then
            printf '%s' "$out"
            return 0
        fi
        (( attempt < ${DEPLOY_DIRECTIVE_LOOKUP_ATTEMPTS:-3} )) && sleep "${DEPLOY_DIRECTIVE_LOOKUP_RETRY_SLEEP:-2}"
    done
    return 1
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

ensure_approved_job_worktree() {
    local job_id="$1" worktree_dir="$2" main_workdir="$3" expected_sha="$4"
    if verify_isolated_job_worktree "$job_id" "$worktree_dir" "$main_workdir"; then
        return 0
    fi
    [[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    git -C "$main_workdir" cat-file -e "${expected_sha}^{commit}" 2>/dev/null || return 1
    if [[ -e "$worktree_dir" ]]; then
        git -C "$main_workdir" worktree remove "$worktree_dir" --force >/dev/null 2>&1 || rm -rf "$worktree_dir" 2>/dev/null || true
    fi
    git -C "$main_workdir" worktree add --detach "$worktree_dir" "$expected_sha" >/dev/null 2>&1 || return 1
    verify_isolated_job_worktree "$job_id" "$worktree_dir" "$main_workdir" || return 1
    log "  WORKTREE_RESTORED_FOR_DEPLOY: $worktree_dir sha=$expected_sha"
    return 0
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

# AADS-RUNNER-REVIEW-DIFF-TRUNCATION (2026-09-30): capture_job_diff_text 는 45000/5000/
# 50000B 에서 조용히 자른다. 리뷰어는 앞부분만 보고 "뒤에 있어야 할 파일이 없다"고
# 반려했다(runner-0e9ce12d/0f81541a/67757d58). 잘렸다는 사실과 전체 변경 파일 목록을
# 리뷰 요청 본문에만 앞머리로 붙인다. 공유 블록의 출력은 DB git_diff 와 스위퍼의
# drift 비교 기준이라 건드리지 않는다 — 여기서 만든 앞머리는 저장하지 않는다.
# 아래 상한은 capture_job_diff_text 의 head -c 값과 같아야 한다(단위 테스트가 대조).
review_diff_stat_section() {
    local repo="$1" title="$2"
    shift 2
    local stat="" shown="" file_count=""
    stat=$(git -C "$repo" diff --stat=200 "$@" 2>/dev/null) || stat=""
    [[ -n "$stat" ]] || return 0
    if [[ $(printf '%s' "$stat" | wc -c) -le 6000 ]]; then
        printf 'DIFFSTAT (%s — 절단 없음)\n%s\n' "$title" "$stat"
        return 0
    fi
    # 잘린 마지막 줄(반쪽 경로·깨진 UTF-8)은 버린다. 파일 수는 잘리지 않은 출력에서 센다.
    shown=$(head -c 6000 <<< "$stat" | sed '$d')
    file_count=$(git -C "$repo" diff --name-only "$@" 2>/dev/null | wc -l | tr -d '[:space:]')
    printf 'DIFFSTAT (%s — 앞부분만)\n%s\n[DIFFSTAT TRUNCATED — 파일 %s개]\n' "$title" "$shown" "${file_count:-0}"
    return 0
}

review_diff_truncation_notice() {
    local full_bytes="$1" included_bytes="$2"
    printf '[DIFF TRUNCATED] 전체 %sB 중 앞 %sB 만 아래에 포함됨. 아래 diff 에 특정 파일이 보이지 않는 것은 "그 파일이 없다"는 뜻이 아니다. 변경 파일 전체 목록은 바로 아래 DIFFSTAT 을 근거로 판정하라.\n' \
        "$full_bytes" "$included_bytes"
    return 0
}

# stdout: 잘렸으면 앞머리(고지 + diffstat + 구분선), 잘리지 않았으면 빈 출력.
build_review_diff_prefix() {
    local repo="$1" base_sha="${2:-}"
    local cap_committed=45000 cap_uncommitted=5000 cap_single=50000
    local head_sha="" full_bytes=0 included_bytes=0 c_full=0 u_full=0
    head_sha=$(git -C "$repo" rev-parse HEAD 2>/dev/null) || head_sha=""
    local stat_text=""
    if [[ -n "$base_sha" && -n "$head_sha" && "$base_sha" != "$head_sha" ]]; then
        c_full=$(git -C "$repo" diff "${base_sha}..${head_sha}" 2>/dev/null | wc -c | tr -d '[:space:]') || c_full=0
        u_full=$(git -C "$repo" diff HEAD 2>/dev/null | wc -c | tr -d '[:space:]') || u_full=0
        full_bytes=$(( c_full + u_full ))
        included_bytes=$(( (c_full > cap_committed ? cap_committed : c_full) + (u_full > cap_uncommitted ? cap_uncommitted : u_full) ))
        if (( c_full <= cap_committed && u_full <= cap_uncommitted )); then
            return 0
        fi
        stat_text=$(review_diff_stat_section "$repo" "전체 변경 파일" "${base_sha}..${head_sha}")
        if (( u_full > 0 )); then
            stat_text="${stat_text}
$(review_diff_stat_section "$repo" "미커밋 변경 — git diff HEAD --stat" HEAD)"
        fi
    else
        full_bytes=$(git -C "$repo" diff HEAD 2>/dev/null | wc -c | tr -d '[:space:]') || full_bytes=0
        (( full_bytes > cap_single )) || return 0
        included_bytes=$cap_single
        stat_text=$(review_diff_stat_section "$repo" "전체 변경 파일" HEAD)
    fi
    printf '%s\n%s\n=== DIFF (원문 시작) ===\n' \
        "$(review_diff_truncation_notice "$full_bytes" "$included_bytes")" "$stat_text"
    return 0
}

# 서버 재시작/러너 종료로 재큐잉된 적이 있는 job 인가 (review_feedback 의 재큐잉 표지).
job_was_requeued() {
    local job_id="$1" flag=""
    [[ "$job_id" =~ ^runner-[0-9a-zA-Z_-]+$ ]] || return 1
    flag=$(db_exec "SELECT CASE WHEN position('[SERVER_RESTART_REQUEUE]' in COALESCE(review_feedback,'')) > 0
                                 OR position('[RUNNER_SHUTDOWN_REQUEUE]' in COALESCE(review_feedback,'')) > 0
                           THEN 1 ELSE 0 END FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null) || flag=""
    flag="${flag//[[:space:]]/}"
    [[ "$flag" == "1" ]]
}

# 재큐잉 job 의 워크트리에서 지시서 밖 변경(추가/미추적 파일 포함)을 찾는다.
# 2026-10-06 이전 실행이 재생성된 경로의 다른 main 커밋을 자기 산출물로 읽었고,
# 신규 파일(A)을 제외한 검사는 관계없는 DR02 파일 4개를 통과시켰다.
# docs/HANDOVER.md 는 R-001 예외다. 위반 파일은 되돌리거나 지우지 않고 보존한다.
# stdout: 위반 경로(한 줄에 하나). 반환: 0=위반 없음/적용 대상 아님, 1=위반 있음.
# 끄는 법: RUNNER_REQUEUE_SCOPE_GUARD=0
requeue_scope_violations() {
    local job_id="$1" worktree_dir="$2" instruction="$3" pre_exec_sha="${4:-}"
    [[ "${RUNNER_REQUEUE_SCOPE_GUARD:-1}" == "1" ]] || return 0
    job_was_requeued "$job_id" || return 0
    local changed="" committed="" untracked="" head_sha="" path base dir violations=""
    changed=$(git -C "$worktree_dir" diff --name-only --diff-filter=ADMRT HEAD 2>/dev/null) || changed=""
    head_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null) || head_sha=""
    if [[ "$pre_exec_sha" =~ ^[0-9a-f]{40}$ && -n "$head_sha" && "$pre_exec_sha" != "$head_sha" ]]; then
        committed=$(git -C "$worktree_dir" diff --name-only --diff-filter=ADMRT "${pre_exec_sha}..${head_sha}" 2>/dev/null) || committed=""
    fi
    untracked=$(git -C "$worktree_dir" ls-files --others --exclude-standard 2>/dev/null) || return 1
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        case "$path" in
            .runner_full_diff.patch|HANDOVER.md|*/HANDOVER.md) continue ;;
        esac
        base="${path##*/}"
        dir="${path%/*}"
        [[ "$instruction" == *"$path"* || "$instruction" == *"$base"* ]] && continue
        [[ "$path" == */* && "$instruction" == *"$dir"* ]] && continue
        violations+="${path}"$'\n'
    done < <(printf '%s\n%s\n%s\n' "$changed" "$committed" "$untracked" | sed '/^[[:space:]]*$/d' | sort -u)
    [[ -z "$violations" ]] && return 0
    printf '%s' "$violations"
    return 1
}

# HEAD 변경만으로 워커 산출물임을 증명하지 못한다. 재큐잉이 경로를 새 main으로
# 재생성하면 이전 실행의 base와 다른 clean HEAD가 생긴다. 공용 main에 이미 포함된
# HEAD나 base와 무관한 HEAD를 채택하지 않는다. 전체 attempt fencing의 대체는 아니다.
verify_worker_commit_provenance() {
    local repo="$1" base_sha="$2" head_sha="$3" ref ref_sha ancestor_rc have_main=0
    [[ "$base_sha" =~ ^[0-9a-f]{40}$ && "$head_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    git -C "$repo" cat-file -e "${base_sha}^{commit}" 2>/dev/null || return 1
    [[ "$base_sha" != "$head_sha" ]] || return 0
    git -C "$repo" merge-base --is-ancestor "$base_sha" "$head_sha" 2>/dev/null || return 1
    for ref in refs/remotes/origin/main refs/heads/main; do
        ref_sha=$(git -C "$repo" rev-parse --verify "${ref}^{commit}" 2>/dev/null) || continue
        have_main=1
        ancestor_rc=0
        git -C "$repo" merge-base --is-ancestor "$head_sha" "$ref_sha" 2>/dev/null || ancestor_rc=$?
        # 0: 공용 main에 이미 포함됨. 1 이외 오류도 provenance 확인 불가다.
        [[ "$ancestor_rc" -eq 1 ]] || return 1
    done
    [[ "$have_main" -eq 1 ]]
}

# 계약: stdout 은 40자 hex commit SHA 단 하나만 낸다 — 호출부가
# `approval_commit_sha=$(commit_job_worktree_for_approval ...)` 로 그대로
# 캡처한다. 정보성 로그는 반드시 stderr(`log ... >&2`)로 보내라 — 2026-09-18
# runner-09a6fe14 가 stdout 오염(로그 한 줄이 SHA 앞에 섞임)으로
# approval_commit_sha_mismatch 처리돼 죽었다.
commit_job_worktree_for_approval() {
    local job_id="$1" session_id="$2" worktree_dir="$3" main_workdir="$4" instruction="$5"
    local pre_exec_sha="${6:-}"
    if ! verify_isolated_job_worktree "$job_id" "$worktree_dir" "$main_workdir"; then
        _fail_job "$job_id" "$session_id" "approval_worktree_not_isolated" "BLOCK: awaiting_approval 거부 — runner isolated worktree 검증 실패 (${worktree_dir})"
        return 1
    fi
    local provenance_head
    provenance_head=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null) || provenance_head=""
    if ! verify_worker_commit_provenance "$worktree_dir" "$pre_exec_sha" "$provenance_head"; then
        _fail_job "$job_id" "$session_id" "approval_commit_provenance_mismatch" \
            "awaiting_approval 거부 — 작업 시작 base와 현재 HEAD의 산출물 소유권 확인 불가(워크트리/파일 보존): base=${pre_exec_sha} head=${provenance_head}"
        return 1
    fi
    # .runner_full_diff.patch 는 사람이 보라고 워크트리에 남기는 파일이다. .gitignore 와
    # 무관하게(과거 커밋을 체크아웃해 추적 상태로 돌아온 경우 포함) 커밋에 넣지 않는다.
    # 재큐잉을 거친 job 이면 지시서가 언급하지 않은 파일의 변경을 스테이징 전에 막는다.
    # 파일을 되돌리거나 지우지 않는다 — 차단하고 보고할 뿐이며 워크트리는 그대로 남는다.
    local scope_violations=""
    if scope_violations=$(requeue_scope_violations "$job_id" "$worktree_dir" "$instruction" "$pre_exec_sha"); then
        :
    else
        local scope_list
        scope_list=$(printf '%s\n' "$scope_violations" | head -20 | tr '\n' ',' | sed 's/,$//')
        _fail_job "$job_id" "$session_id" "approval_requeue_scope_violation" \
            "awaiting_approval 거부 — 재큐잉 job 의 워크트리에 지시서 밖 파일 변경이 있음(워크트리 보존, 자동 되돌림 없음): ${scope_list:0:900}"
        return 1
    fi
    # stderr 를 버리면 실패 원인이 영영 남지 않는다 — 2026-10-02 GO100 4건이
    # 같은 approval_commit_stage_failed 로 끝났는데 git 의 오류 문구가 없어 원인 확정이 불가능했다.
    local stage_err stage_rc=0
    stage_err=$(mktemp "/tmp/pipeline-approval-stage-${job_id}.err.XXXXXX")
    { git -C "$worktree_dir" add -A -- . && { git -C "$worktree_dir" reset -q -- .runner_full_diff.patch || true; }; } >/dev/null 2>"$stage_err" || stage_rc=$?
    if [[ "$stage_rc" -ne 0 ]]; then
        local stage_msg
        stage_msg=$(head -c 1024 "$stage_err" | tr -d '\r' | mask_git_diagnostics | head -c 1024)
        record_git_diagnostics "$job_id" "approval_commit_stage_failed" "$worktree_dir" "$stage_rc" "" "$stage_msg" >/dev/null
        rm -f "$stage_err"
        _fail_job "$job_id" "$session_id" "approval_commit_stage_failed" "awaiting_approval 거부 — runner worktree stage 실패 (rc=${stage_rc}): ${stage_msg:-stderr 없음}"
        return 1
    fi
    rm -f "$stage_err"
    # 워커가 격리 워크트리 안에서 자기 변경을 이미 커밋해 두는 경우가 있다.
    # 그러면 스테이징에 남는 것이 없어 git commit 이 1 을 반환하고, 그것을
    # 실패로 처리하면 멀쩡한 산출물이 통째로 버려진다 — 2026-09-17 13:08 KST
    # runner-66b4d212(GO100, 리뷰 0.96 APPROVE)가 그렇게 error 로 끝났고
    # 43줄짜리 결과물은 detached HEAD 에 남아 아무도 회수하지 않았다.
    # 새 커밋이 필요 없을 뿐이므로 그 HEAD 를 승인 커밋으로 채택한다.
    # 정말 아무 일도 하지 않은 잡(HEAD 가 작업 시작 SHA 그대로)만 실패다.
    local adopted_sha="" deploy_only_no_commit=0
    if git -C "$worktree_dir" diff --cached --quiet 2>/dev/null; then
        adopted_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
        if [[ ! "$adopted_sha" =~ ^[0-9a-f]{40}$ || -z "$pre_exec_sha" ]]; then
            adopted_sha=""
        elif [[ "$adopted_sha" == "$pre_exec_sha" ]]; then
            # DEPLOY_ONLY job 은 정의상 코드 변경/커밋을 만들지 않으므로
            # HEAD == pre_exec_sha 가 정상이다 — "아무 일도 하지 않은 잡" 판정에서 제외한다.
            if is_deploy_only_instruction "$instruction"; then
                deploy_only_no_commit=1
            else
                adopted_sha=""
            fi
        fi
    fi
    if [[ -n "$adopted_sha" && "$deploy_only_no_commit" -eq 1 ]]; then
        log "  DEPLOY_ONLY_APPROVAL_NO_COMMIT job=$job_id sha=$adopted_sha (DEPLOY_ONLY, 변경 없음 — 커밋 생략)" >&2
        record_runner_event "$job_id" "deploy_only_approval_no_commit" "running" "deploy_only_no_commit" "" "" "" "" "{\"deploy_only\":true,\"changed_files\":0}" >/dev/null
    elif [[ -n "$adopted_sha" ]]; then
        log "  APPROVAL_COMMIT_ADOPTED_EXISTING job=$job_id sha=$adopted_sha (워커가 워크트리에서 이미 커밋)" >&2
    else
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
    fi
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

# cwd 가 해당 경로 또는 하위인 PID 목록. 삭제된 cwd도 이전 작성자가 살아 있는 증거다.
worktree_busy_pids() {
    local dir="$1" p cwd out=""
    [[ -n "$dir" ]] || return 0
    for p in /proc/[0-9]*; do
        [[ "${p#/proc/}" == "$$" || "${p#/proc/}" == "${BASHPID:-$$}" ]] && continue
        cwd=$(readlink "$p/cwd" 2>/dev/null) || continue
        # /proc 는 unlinked cwd에 이 literal suffix를 붙인다. 경로가 없어도
        # 이전 프로세스가 같은 이름을 다시 열 수 있으므로 재생성을 차단한다.
        cwd="${cwd%" (deleted)"}"
        [[ "$cwd" == "$dir" || "$cwd" == "$dir"/* ]] && out+="${p#/proc/} "
    done
    printf '%s' "${out% }"
    return 0
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

    # 경로가 이미 삭제됐어도 그 cwd를 보유한 이전 시도가 남을 수 있다.
    local _busy_pids
    _busy_pids=$(worktree_busy_pids "$worktree_dir")
    if [[ -n "$_busy_pids" ]]; then
        _fail_job "$job_id" "$session_id" "worktree_path_busy" \
            "worktree 재사용 거부 — 기존 프로세스와 산출물 보존: path=${worktree_dir} pids=${_busy_pids}"
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

    # fetch 동안 새 소유자가 들어오거나 경로가 삭제된 경우도 보존한다.
    _busy_pids=$(worktree_busy_pids "$worktree_dir")
    if [[ -n "$_busy_pids" ]]; then
        _fail_job "$job_id" "$session_id" "worktree_path_busy" \
            "worktree 재사용 거부 — 기존 프로세스와 산출물 보존: path=${worktree_dir} pids=${_busy_pids}"
        return 1
    fi
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

# "무관한 dirty" 판정: main workdir dirty 파일 ∩ job_target_files(대상 파일) == ∅.
# 겹치면 무관하지 않음 → 차단. 상시 dirty(goals.py, acct-purchase-mockup.html 등
# 2026-09-18 실측 4파일)는 대상 파일이 아니므로 보통 무관 판정을 받는다.
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

    # behind>0, ahead=0 이면 자동 FF 대상 (AADS-DEPLOY-PREFLIGHT-FFONLY-DIRTY-SCOPED-20260918).
    # dirty 파일이 있어도 이번 job 의 배포 대상 파일과 겹치지 않으면 FF 를 시도한다.
    # 겹치면 시도하지 않고 아래 behind/ahead 검사에서 그대로 차단된다.
    if [[ "${behind:-999}" -gt 0 && "${ahead:-999}" -eq 0 ]]; then
        local ff_ok=1 ff_dirty_paths ff_target_files ff_overlap
        if [[ "${dirty:-999}" -ne 0 ]]; then
            ff_dirty_paths=$(git_dirty_paths "$main_workdir")
            ff_target_files=$(job_target_files "$job_id")
            ff_overlap=$(comm -12 <(printf '%s\n' "$ff_dirty_paths") <(printf '%s\n' "$ff_target_files") 2>/dev/null)
            [[ -n "${ff_overlap//[[:space:]]/}" ]] && ff_ok=0
        fi
        if [[ "$ff_ok" -eq 1 ]]; then
            if git -C "$main_workdir" merge --ff-only origin/main >/dev/null 2>&1; then
                log "  DEPLOY_PREFLIGHT_FFONLY_SYNC: behind=${behind} ahead=0 dirty=${dirty:-0} → merge --ff-only 성공"
                counts=$(git_ahead_behind_counts "$main_workdir" "origin/main") || counts="999 999"
                ahead="${counts%% *}"
                behind="${counts##* }"
                dirty=$(git_dirty_count "$main_workdir")
            else
                git -C "$main_workdir" merge --abort >/dev/null 2>&1 || true
                _fail_job "$job_id" "$session_id" "deploy_preflight_ff_conflict" \
                    "배포 차단: fast-forward 중 충돌 발생 (behind=${behind}, dirty=${dirty:-0})"
                return 1
            fi
        fi
    fi

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

# 승인된 AADS 격리 릴리스는 공유 main 의 ahead/dirty 상태와 독립적이다.
# 공유 본체를 동기화하는 기존 4인자 preflight 계약은 그대로 둔다.
# 성공 시 DEPLOY_ISOLATED_EFFECTIVE_SHA 에 이후 단계가 써야 할 SHA 를 남긴다.
# patch-id 승인 상속이 일어났으면 격리 worktree HEAD, 아니면 인자로 받은 승인 SHA 와 같다.
DEPLOY_ISOLATED_EFFECTIVE_SHA=""
deploy_isolated_git_preflight() {
    local job_id="$1" project="$2" session_id="$3" main_workdir="$4"
    local worktree_dir="$5" approved_sha="$6" main_root worktree_root
    local main_common worktree_common registered remote_sha head_sha worktree_status push_state
    local inherit_verdict="" inherit_reason="" inherit_from="" inherit_pid="" dirty_head=""

    DEPLOY_ISOLATED_EFFECTIVE_SHA=""
    if [[ "$project" != "AADS" || ! "$job_id" =~ ^[a-zA-Z0-9_-]+$ \
        || ! "$approved_sha" =~ ^[0-9a-f]{40}$ ]]; then
        _fail_job "$job_id" "$session_id" "deploy_isolated_identity_invalid" "격리 릴리스 소유권 또는 승인 SHA 오류"
        return 1
    fi
    main_root=$(git -C "$main_workdir" rev-parse --show-toplevel 2>/dev/null) || main_root=""
    worktree_root=$(git -C "$worktree_dir" rev-parse --show-toplevel 2>/dev/null) || worktree_root=""
    if [[ -z "$main_root" || -z "$worktree_root" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_worktree_not_isolated" "격리 릴리스 저장소 경로 확인 실패"
        return 1
    fi
    main_root=$(realpath "$main_root") || return 1
    worktree_root=$(realpath "$worktree_root") || return 1
    main_common=$(git -C "$main_root" rev-parse --git-common-dir 2>/dev/null) || main_common=""
    worktree_common=$(git -C "$worktree_root" rev-parse --git-common-dir 2>/dev/null) || worktree_common=""
    if [[ -z "$main_common" || -z "$worktree_common" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_worktree_not_isolated" "격리 릴리스 공통 저장소 확인 실패"
        return 1
    fi
    [[ "$main_common" == /* ]] || main_common="$main_root/$main_common"
    [[ "$worktree_common" == /* ]] || worktree_common="$worktree_root/$worktree_common"
    main_common=$(realpath -m "$main_common") || return 1
    worktree_common=$(realpath -m "$worktree_common") || return 1
    registered=$(git -C "$main_root" worktree list --porcelain 2>/dev/null) || registered=""
    if [[ "$worktree_root" != "/tmp/aads-wt-${job_id}" || "$worktree_root" == "$main_root" \
        || "$main_common" != "$worktree_common" ]] \
        || ! printf '%s\n' "$registered" | grep -Fxq "worktree $worktree_root"; then
        _fail_job "$job_id" "$session_id" "deploy_worktree_not_isolated" "격리 worktree 등록/저장소/작업 소유권 확인 실패"
        return 1
    fi
    head_sha=$(git -C "$worktree_root" rev-parse HEAD 2>/dev/null) || head_sha=""
    worktree_status=$(git -C "$worktree_root" status --porcelain --untracked-files=all 2>/dev/null) || worktree_status="status_failed"
    # 러너 자신이 남기는 산출물 한 경로만 제외한다(.gitignore 비의존 — dashboard 는 무시 규칙이 없다).
    # 추적 상태로 수정된 경우(" M ...")나 다른 untracked 파일은 그대로 차단한다.
    if [[ "$worktree_status" != "status_failed" ]]; then
        worktree_status=$(printf '%s\n' "$worktree_status" | grep -vxF -- '?? .runner_full_diff.patch' | sed '/^$/d') || true
    fi
    if [[ -n "$worktree_status" ]]; then
        # dirty 는 patch-id 로 구제하지 않는다 — 커밋되지 않은 변경은 승인 대상이 아니다.
        dirty_head=$(printf '%s\n' "$worktree_status" | sed -n '1,5p' | tr '\n' '|')
        _fail_job "$job_id" "$session_id" "deploy_isolated_worktree_dirty" "격리 worktree dirty (git status --porcelain 앞 5줄): ${dirty_head}"
        return 1
    fi
    git -C "$worktree_root" fetch --prune origin >/dev/null 2>&1 || {
        _fail_job "$job_id" "$session_id" "deploy_fetch_failed" "격리 릴리스 origin fetch 실패"
        return 1
    }
    if [[ "$head_sha" != "$approved_sha" ]]; then
        # 승인 후 base 가 들어와 리베이스되면 내용이 같아도 SHA 가 달라진다.
        # patch-id 가 같음을 증명할 수 있을 때만 승인을 상속한다 (fetch 뒤라야 origin/main 이 최신이다).
        read -r inherit_verdict inherit_reason inherit_from inherit_pid \
            <<< "$(inherit_approval_decision "$job_id" "$worktree_root" "$head_sha")" || true
        log "  DEPLOY_PREFLIGHT_PATCHID_CHECK job=$job_id verdict=${inherit_verdict:-none} reason=${inherit_reason:-none} approved=${inherit_from:--} patch_id=${inherit_pid:--}"
        if [[ "$inherit_verdict" != "inherit" ]]; then
            _fail_job "$job_id" "$session_id" "deploy_isolated_sha_or_dirty" "격리 worktree HEAD/승인 SHA 불일치 — patch-id 동등성 증명 실패 (${inherit_reason:-unknown}): head=${head_sha} approved=${approved_sha}"
            return 1
        fi
        db_update "UPDATE pipeline_jobs SET commit_hash='${head_sha}',
                   review_feedback=COALESCE(review_feedback,'') || E'\n' || $(sql_escape "[승인상속] patch-id 동일(${inherit_pid}) — ${approved_sha}→${head_sha}"),
                   updated_at=NOW()
                   WHERE job_id='${job_id}' AND status='deploying' AND commit_hash='${approved_sha}';"
        if [[ "$(db_exec "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]')" != "$head_sha" ]]; then
            _fail_job "$job_id" "$session_id" "deploy_rebase_inherit_persist_failed" "승인 상속 SHA 저장 실패"
            return 1
        fi
        record_runner_event "$job_id" "approval_inherited_same_patch_id" "info" "approved" "" "" "" "" "{\"from\":\"${approved_sha}\",\"to\":\"${head_sha}\",\"patch_id\":\"${inherit_pid}\",\"approved_sha\":\"${inherit_from}\"}"
        log "  DEPLOY_PREFLIGHT_PATCHID_INHERIT job=$job_id from=${approved_sha} to=${head_sha} patch_id=${inherit_pid}"
        approved_sha="$head_sha"
    fi
    remote_sha=$(git -C "$worktree_root" ls-remote origin refs/heads/main 2>/dev/null | awk 'NR==1 {print $1}') || remote_sha=""
    if [[ ! "$remote_sha" =~ ^[0-9a-f]{40}$ \
        || "$(git -C "$worktree_root" rev-parse --verify origin/main 2>/dev/null)" != "$remote_sha" \
        || "$(git -C "$worktree_root" remote get-url origin 2>/dev/null)" != "$(git -C "$main_root" remote get-url origin 2>/dev/null)" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_origin_missing" "격리 릴리스 origin/main 일치 확인 실패"
        return 1
    fi
    push_state=$(classify_push_state "$worktree_root" "$approved_sha")
    case "$push_state" in
        already_present)
            log "  DEPLOY_PREFLIGHT_ALREADY_PRESENT: job=$job_id sha=$approved_sha"
            record_runner_event "$job_id" "deploy_already_present" "info" "deploying" "" "" "" "" "{\"sha\":\"${approved_sha}\"}"
            ;;
        fast_forward|stale_base) ;;
        *)
            _fail_job "$job_id" "$session_id" "deploy_origin_missing" "격리 릴리스 origin/main 판별 실패: ${push_state}"
            return 1
            ;;
    esac
    DEPLOY_ISOLATED_EFFECTIVE_SHA="$approved_sha"
    log "  DEPLOY_ISOLATED_PREFLIGHT_OK: job=$job_id sha=$approved_sha origin=$remote_sha"
}

# ── GO100 프론트 번들 신선도 ──────────────────────────────────────────
# 2026-09-30 runner-9c1f2658: 포트 curl 200 만 보고 frontend_health=OK 로 보고했지만
# 활성 슬롯 번들은 구버전이었다. go100-frontend.service 는 masked 이고 실제 서비스는
# blue(3000)/green(3001), 번들은 frontend/.next.<color> 다. 활성 슬롯은 nginx 가 정한다.
GO100_REPO_DIR="${GO100_REPO_DIR:-/root/kis-autotrade-v4}"
GO100_NGINX_CONF="${GO100_NGINX_CONF:-/etc/nginx/sites-enabled/go100}"

# stdout: green | blue | "" (판정 불가). 항상 0 으로 끝난다 (set -e 안전).
go100_active_frontend_color() {
    local _conf="${1:-$GO100_NGINX_CONF}"
    local _port=""
    if [ -r "$_conf" ]; then
        _port=$(awk '
            /^[[:space:]]*upstream[[:space:]]+go100_frontend([[:space:]]|\{|$)/ { inb=1; next }
            inb && /^[[:space:]]*server[[:space:]]+127\.0\.0\.1:[0-9]+/ && !/[[:space:]](down|backup)/ {
                match($0, /127\.0\.0\.1:[0-9]+/)
                print substr($0, RSTART + 10, RLENGTH - 10)
                exit
            }
            inb && /\}/ { inb=0 }
        ' "$_conf" 2>/dev/null) || true
    fi
    case "$_port" in
        3001) echo "green" ;;
        3000) echo "blue" ;;
        *) echo "" ;;
    esac
    return 0
}

# stdout: "status|color|build_id8|built_epoch|commit_epoch"
# status: fresh | stale | active_slot_unknown | build_id_missing | commit_epoch_unknown
# 항상 0 으로 끝난다 (set -e 안전). 판정은 호출측이 status 로 한다.
verify_go100_bundle_fresh() {
    local _repo="${1:-$GO100_REPO_DIR}" _conf="${2:-$GO100_NGINX_CONF}"
    local _color="" _commit_epoch="" _bid_file="" _built_epoch="" _bid=""
    _color=$(go100_active_frontend_color "$_conf") || _color=""
    if [ -z "$_color" ]; then
        echo "active_slot_unknown||||"
        return 0
    fi
    _bid_file="$_repo/frontend/.next.${_color}/BUILD_ID"
    if [ ! -f "$_bid_file" ]; then
        echo "build_id_missing|${_color}|||"
        return 0
    fi
    _bid=$(head -c 8 "$_bid_file" 2>/dev/null | tr -d '[:space:]|') || _bid=""
    _built_epoch=$(stat -c %Y "$_bid_file" 2>/dev/null) || _built_epoch=""
    _commit_epoch=$(git -C "$_repo" show -s --format=%ct HEAD 2>/dev/null) || _commit_epoch=""
    if ! [[ "$_commit_epoch" =~ ^[0-9]+$ ]] || ! [[ "$_built_epoch" =~ ^[0-9]+$ ]]; then
        echo "commit_epoch_unknown|${_color}|${_bid}|${_built_epoch}|${_commit_epoch}"
        return 0
    fi
    if (( _built_epoch < _commit_epoch )); then
        echo "stale|${_color}|${_bid}|${_built_epoch}|${_commit_epoch}"
    else
        echo "fresh|${_color}|${_bid}|${_built_epoch}|${_commit_epoch}"
    fi
    return 0
}

# ── GO100 프론트 BG 배포 실패 사유 분류 (AADS-RUNNER-GO100-FE-FAILURE-LABELS-20260930) ──
# 2026-09-30 runner-06e8ddf5: Deploy Gate 의 dirty worktree 차단이 go100-frontend:build_failed 로
# 보고됐다. BG 스크립트(GO100 저장소 소유)는 모든 실패를 exit 1 로 내므로 종료코드로는
# 구분할 수 없고, 락이 잡혀 있으면 요청을 큐에 넣고 exit 0 한다(배포 안 됨).
# 그래서 rc 가 아니라 전체 출력의 고정 문자열로 분류한다. 라벨 문자열은 이 두 함수에만 둔다.

# kind → 라벨. 모르는 kind 는 bg_failed_unclassified 로 보낸다("분류 못 함"을 "빌드 실패"로 부르지 않는다).
go100_fe_failure_label() {
    case "${1:-}" in
        deploy_gate_blocked)     echo "go100-frontend:deploy_gate_blocked" ;;
        deploy_lock_busy)        echo "go100-frontend:deploy_lock_busy" ;;
        deploy_queued_not_applied) echo "go100-frontend:deploy_queued_not_applied" ;;
        build_failed)            echo "go100-frontend:build_failed" ;;
        health_failed)           echo "go100-frontend:health_failed" ;;
        release_worktree_failed) echo "go100-frontend:release_worktree_failed" ;;
        bluegreen_script_missing) echo "go100-frontend:bluegreen_script_missing" ;;
        *)                       echo "go100-frontend:bg_failed_unclassified" ;;
    esac
    return 0
}

# 0 = 출력 파일에 고정 문자열이 있다. 파일이 없거나 비어 있으면 1.
_go100_fe_out_has() {
    [ -n "${1:-}" ] && [ -r "$1" ] && grep -qF -- "$2" "$1" 2>/dev/null
}

# $1=BG 스크립트 rc  $2=전체 출력 파일
# stdout: 실패 라벨, 성공이면 빈 문자열. 항상 0 으로 끝난다 (set -e 안전).
go100_fe_bg_classify() {
    local _rc="${1:-1}" _out="${2:-}"
    if [ "$_rc" = "0" ]; then
        # rc=0 이어도 큐에 넣고 빠진 것이면 아무것도 배포되지 않았다.
        if _go100_fe_out_has "$_out" "queued_for_deploy"; then
            go100_fe_failure_label deploy_queued_not_applied
        fi
    elif _go100_fe_out_has "$_out" "Deploy Gate 차단"; then
        go100_fe_failure_label deploy_gate_blocked
    elif _go100_fe_out_has "$_out" "동시 배포 차단" || _go100_fe_out_has "$_out" "release worktree 사용 중"; then
        go100_fe_failure_label deploy_lock_busy
    elif _go100_fe_out_has "$_out" "빌드 실패"; then
        go100_fe_failure_label build_failed
    elif _go100_fe_out_has "$_out" "standby health 실패" \
        || _go100_fe_out_has "$_out" "외부 프론트 헬스 확인 실패" \
        || _go100_fe_out_has "$_out" "inactive 서비스 health 실패"; then
        go100_fe_failure_label health_failed
    else
        go100_fe_failure_label bg_failed_unclassified
    fi
    return 0
}

# $1=라벨 $2=rc → 채팅 알림 문구
go100_fe_failure_chat_text() {
    local _label="${1:-}" _rc="${2:-?}"
    case "$_label" in
        *:deploy_gate_blocked)       echo "GO100 프론트엔드 배포 안 됨 — Deploy Gate 차단(빌드는 시작되지 않음, rc=$_rc)" ;;
        *:deploy_lock_busy)          echo "GO100 프론트엔드 배포 안 됨 — 다른 배포가 락 점유 중(rc=$_rc)" ;;
        *:deploy_queued_not_applied) echo "GO100 프론트엔드 배포 안 됨 — 락 경합으로 큐에 이관됨(rc=$_rc, 이번 SHA 는 아직 미반영)" ;;
        *:health_failed)             echo "GO100 프론트엔드 blue-green 배포 실패 — 헬스 확인 실패(rc=$_rc)" ;;
        *:bg_failed_unclassified)    echo "GO100 프론트엔드 blue-green 배포 실패 — 사유 미분류(rc=$_rc, runner.log 확인)" ;;
        *)                           echo "GO100 프론트엔드 blue-green 배포 실패 (rc=$_rc)" ;;
    esac
    return 0
}

# ── GO100 프론트 clean 릴리스 워크트리 (AADS-RUNNER-GO100-FE-CLEAN-WORKTREE-20260930) ──
# 2026-09-30 14:18 go100-frontend:build_failed 는 빌드 오류가 아니었다. BG 스크립트를
# 여러 세션이 공유하는 런타임 워크트리(상시 dirty)에서 돌려 Deploy Gate 가 dirty 로 막았고,
# 런타임 HEAD 가 origin/main 과 갈라져 있어 방금 push 한 SHA 가 아닌 무관한 SHA 를
# 배포 대상으로 봤다. 같은 SHA 를 clean 워크트리에서 돌린 14:34 수동 배포는 성공했다.
# 그래서 push 한 SHA 로 워크트리를 만들어 GO100_RELEASE_WORKDIR 로 주입한다.
#
# node_modules 는 런타임 트리로 symlink 한다(package-lock.json 이 같을 때만).
#   - 14:34 성공 배포와 09-21/09-04 릴리스 워크트리가 모두 이 방식이었다(검증된 경로).
#   - 복사 비용 0. symlink 의 realpath 가 배포마다 같아 webpack 영속 캐시
#     (.next-build-cache)의 node_modules 항목이 워크트리 경로가 바뀌어도 재사용된다.
#     cp -al 은 경로가 배포마다 달라져 그 항목들이 캐시 미스가 된다.
#   - frontend/.gitignore 의 `/node_modules`(슬래시 없음)가 symlink 도 무시하므로 게이트는 clean.
#   - 리스크: 빌드 중 런타임에서 npm install 이 돌면 같은 트리를 본다. 의존성이 바뀐
#     SHA 는 lock 비교로 걸러 그 경우에만 워크트리에서 npm ci 를 돈다(매 배포 아님).
# 실패 시 런타임 워크트리로 폴백하지 않는다 — 폴백하면 이번 버그로 조용히 되돌아간다.
GO100_RELEASE_WORKTREE_ROOT="${GO100_RELEASE_WORKTREE_ROOT:-/opt/go100}"
GO100_RELEASE_WORKTREE_KEEP="${GO100_RELEASE_WORKTREE_KEEP:-2}"
GO100_RELEASE_WORKTREE_MIN_FREE_MB="${GO100_RELEASE_WORKTREE_MIN_FREE_MB:-3072}"
GO100_RELEASE_WORKTREE_MIN_AGE_MIN="${GO100_RELEASE_WORKTREE_MIN_AGE_MIN:-30}"
GO100_RELEASE_NPM_CI_TIMEOUT="${GO100_RELEASE_NPM_CI_TIMEOUT:-1800}"

# 0 = 어떤 프로세스의 cwd 나 cmdline 이 경로 아래에 있다(큐 워커가 나중에 쓰는 워크트리 포함).
_go100_path_in_use() {
    local _dir="$1"
    [ -n "$(find /proc -mindepth 2 -maxdepth 2 -name cwd \( -lname "$_dir" -o -lname "$_dir/*" \) -print -quit 2>/dev/null)" ] && return 0
    grep -qsaF -- "$_dir" /proc/[0-9]*/cmdline 2>/dev/null && return 0
    return 1
}

# 워크트리 하나를 지운다. symlink 는 먼저 끊어 런타임 node_modules 에 닿지 않게 한다.
_go100_remove_release_worktree() {
    local _repo="$1" _dir="$2"
    case "$_dir" in
        "$GO100_RELEASE_WORKTREE_ROOT"/frontend-release-*) ;;
        *) return 1 ;;
    esac
    [ -L "$_dir/frontend/node_modules" ] && rm -f "$_dir/frontend/node_modules"
    git -C "$_repo" worktree remove --force "$_dir" >/dev/null 2>&1 || true
    [ -e "$_dir" ] && rm -rf --one-file-system -- "$_dir"
    [ ! -e "$_dir" ]
}

# frontend-release-* 중 최신(mtime) keep 개만 남기고 git worktree prune 으로 유령을 치운다.
# 사용 중이거나 MIN_AGE 분 안에 바뀐 것(다른 세션이 만드는 중일 수 있음)은 건너뛴다. 항상 0.
go100_prune_release_worktrees() {
    local _repo="${1:-$GO100_REPO_DIR}" _keep="${2:-$GO100_RELEASE_WORKTREE_KEEP}"
    local _i=0 _d
    [[ "$_keep" =~ ^[0-9]+$ ]] || _keep=2
    while IFS= read -r _d; do
        [ -d "$_d" ] || continue
        _i=$((_i + 1))
        (( _i <= _keep )) && continue
        if _go100_path_in_use "$_d"; then
            log "  GO100_RELEASE_WT keep(in_use) $_d"
            continue
        fi
        if [ -n "$(find "$_d" -maxdepth 0 -mmin "-${GO100_RELEASE_WORKTREE_MIN_AGE_MIN}" 2>/dev/null)" ]; then
            log "  GO100_RELEASE_WT keep(recent<${GO100_RELEASE_WORKTREE_MIN_AGE_MIN}m) $_d"
            continue
        fi
        if _go100_remove_release_worktree "$_repo" "$_d"; then
            log "  GO100_RELEASE_WT removed $_d"
        else
            log "  WARN: GO100_RELEASE_WT remove failed $_d"
        fi
    done < <(ls -1dt "$GO100_RELEASE_WORKTREE_ROOT"/frontend-release-* 2>/dev/null)
    git -C "$_repo" worktree prune 2>/dev/null || true
    return 0
}

# stdout: 만든 워크트리 경로. 실패 시 1 + stderr 사유, 만들던 워크트리는 지운다.
go100_create_frontend_release_worktree() {
    local _repo="$1" _sha="$2" _dir="" _free_mb="" _fe=""
    _go100_rwt_fail() {
        echo "release_worktree: $*" >&2
        [ -n "$_dir" ] && _go100_remove_release_worktree "$_repo" "$_dir" >/dev/null 2>&1
        git -C "$_repo" worktree prune 2>/dev/null || true
        return 1
    }
    [[ "$_sha" =~ ^[0-9a-f]{7,40}$ ]] || { _go100_rwt_fail "push SHA 형식 오류: '${_sha}'"; return 1; }
    if ! git -C "$_repo" cat-file -e "${_sha}^{commit}" 2>/dev/null; then
        timeout 120 git -C "$_repo" fetch --quiet origin main >&2 2>&1 || true
    fi
    _sha=$(git -C "$_repo" rev-parse --verify --quiet "${_sha}^{commit}" 2>/dev/null) \
        || { _go100_rwt_fail "커밋 없음: $2 (repo=$_repo)"; return 1; }
    mkdir -p "$GO100_RELEASE_WORKTREE_ROOT" || { _go100_rwt_fail "루트 생성 실패: $GO100_RELEASE_WORKTREE_ROOT"; return 1; }
    _free_mb=$(df -Pm "$GO100_RELEASE_WORKTREE_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
    if ! [[ "$_free_mb" =~ ^[0-9]+$ ]] || (( _free_mb < GO100_RELEASE_WORKTREE_MIN_FREE_MB )); then
        _go100_rwt_fail "디스크 부족: free=${_free_mb:-?}MB < ${GO100_RELEASE_WORKTREE_MIN_FREE_MB}MB ($GO100_RELEASE_WORKTREE_ROOT)" || return 1
    fi
    _dir="$GO100_RELEASE_WORKTREE_ROOT/frontend-release-${_sha:0:9}-$(date +%Y%m%d-%H%M%S)"
    local _base="$_dir" _n=1
    while [ -e "$_dir" ]; do
        _n=$((_n + 1))
        _dir="${_base}-${_n}"
        if (( _n > 20 )); then
            _dir=""
            _go100_rwt_fail "경로 충돌: ${_base}-*" || return 1
        fi
    done
    git -C "$_repo" worktree add --detach "$_dir" "$_sha" >&2 2>&1 \
        || { _go100_rwt_fail "git worktree add 실패: $_dir @ $_sha"; return 1; }
    _fe="$_dir/frontend"
    [ -f "$_fe/package.json" ] || { _go100_rwt_fail "frontend/package.json 없음 @ $_sha"; return 1; }
    [ -f "$_dir/scripts/deploy_frontend_blue_green.sh" ] \
        || { _go100_rwt_fail "scripts/deploy_frontend_blue_green.sh 없음 @ $_sha"; return 1; }
    if cmp -s "$_repo/frontend/package-lock.json" "$_fe/package-lock.json" \
        && [ -x "$_repo/frontend/node_modules/.bin/next" ]; then
        ln -s "$_repo/frontend/node_modules" "$_fe/node_modules" \
            || { _go100_rwt_fail "node_modules symlink 실패"; return 1; }
        echo "release_worktree: node_modules=symlink -> $_repo/frontend/node_modules" >&2
    else
        echo "release_worktree: package-lock.json 이 런타임과 다름(또는 런타임 node_modules 없음) — 이 SHA 만 npm ci" >&2
        ( cd "$_fe" && timeout "$GO100_RELEASE_NPM_CI_TIMEOUT" npm ci --no-audit --no-fund ) >&2 2>&1 \
            || { _go100_rwt_fail "npm ci 실패 (timeout=${GO100_RELEASE_NPM_CI_TIMEOUT}s)"; return 1; }
    fi
    [[ "$(git -C "$_dir" rev-parse HEAD 2>/dev/null)" == "$_sha" ]] \
        || { _go100_rwt_fail "HEAD 불일치 (expected=$_sha)"; return 1; }
    if [ -n "$(git -C "$_dir" status --porcelain 2>/dev/null)" ]; then
        git -C "$_dir" status --porcelain 2>/dev/null | head -10 >&2
        _go100_rwt_fail "생성 직후 dirty — 게이트 통과 불가" || return 1
    fi
    echo "$_dir"
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
    elif echo "$combined" | grep -qi "hit your weekly limit\|weekly limit\|usage limit reached\|hit your limit"; then
        # 계정 주간/사용 한도 소진 — 일시적 429(rate_limit)와 구분한다.
        # 한도는 해제 시각까지 재시도해도 소용이 없고, 빈 실패 1건마다 LLM 15회가
        # 소모된다. 2026-09-19 05:37~05:44 KST 러너 4건 연속 실패가 전부
        # type=unknown 으로 기록돼 원장만 보고는 원인을 알 수 없었다.
        local quota_line=""
        quota_line=$(printf '%s\n' "$combined" | grep -iE -m1 "hit your (weekly )?limit|weekly limit|usage limit" | head -c 120)
        if [[ -n "${quota_line//[[:space:]]/}" ]]; then
            echo "llm_quota_exhausted: ${quota_line}"
        else
            echo "llm_quota_exhausted"
        fi
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

# 서버가 계정 토큰을 폐기하면(401 token_revoked) 로컬 만료 검사로는 알 수 없고, 계정을
# 1순위로 고르는 한 매 시도가 401 로 죽는다(2026-10-01 CODEX_OAUTH_JINAH). 전역 마커는
# 2026-09-19 에 사다리 6칸을 통째로 skip 시킨 전력이 있어 쓰지 않고, 해당 계정만 제외한다.
# 마커 경로 규칙(키 이름의 [^A-Za-z0-9_.-] → _)은 codex_pick_account_home 의 파이썬과 같다.
codex_revoked_marker_path() {
    local key="${1:-}"
    key="${key//[^A-Za-z0-9_.-]/_}"
    printf '%s%s' "${AADS_CODEX_REVOKED_MARKER_PREFIX:-/tmp/aads-codex-revoked-}" "$key"
}

# 실패한 codex 실행의 stderr/stdout 에서 서버측 폐기 신호를 찾는다. 401 과 폐기 문구가 둘 다
# 있어야 한다. stdout 은 모델 본문이 섞일 수 있어 짧은 것(에러 본문 크기)만 본다.
codex_failure_is_token_revoked() {
    local err_file="${1:-}" out_file="${2:-}" f size
    for f in "$err_file" "$out_file"; do
        [[ -n "$f" && -s "$f" ]] || continue
        if [[ "$f" == "$out_file" && "$f" != "$err_file" ]]; then
            size=$(wc -c < "$f" 2>/dev/null || echo 999999)
            [[ "$size" -le 4096 ]] || continue
        fi
        if grep -qiE 'token_revoked|invalidated oauth token|workspace routing discovery unauthorized' "$f" 2>/dev/null \
           && grep -qE '(^|[^0-9])401([^0-9]|$)' "$f" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

mark_codex_account_revoked() {
    local key="${1:-}" reason="${2:-http401_revoked}"
    [[ -n "$key" ]] || return 0
    local ttl="${AADS_CODEX_REVOKED_TTL:-${AADS_CODEX_AUTH_DISABLED_TTL:-7200}}"
    [[ "$ttl" =~ ^[0-9]+$ ]] || ttl=7200
    local until_ts=$(( $(date +%s) + ttl ))
    printf '%s\n' "$until_ts" > "$(codex_revoked_marker_path "$key")" 2>/dev/null || true
    log "  CODEX_ACCOUNT_REVOKED_SET key=$key reason=${reason:0:60} until_epoch=$until_ts ttl=${ttl}s"
}

# ── 사전 검증 (Pre-validation) ─────────────────────────────────────────
pre_validate() {
    local job_id="$1" project="$2" session_id="$3"
    local instruction="${4:-}"
    local workdir
    if ! workdir=$(resolve_project_workdir "$project" "$instruction"); then
        fail_invalid_aads_target "$job_id" "$session_id"
        return 1
    fi

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
    local recorded_error="${5:-$error_type}"
    log "  FAIL_FAST job=$job_id type=$error_type: $detail"
    local safe_detail safe_error
    safe_detail=$(sql_escape "$detail")
    safe_error=$(sql_escape "$recorded_error")
    db_update "UPDATE pipeline_jobs SET status='error', phase='error',
               error_detail=${safe_error},
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
                        ORDER BY CASE WHEN instruction ~* '(^|[[:space:]])PRIORITY:[[:space:]]*P0' THEN 1 ELSE 0 END DESC,
                                 CASE WHEN logs @> '[{\"event\": \"file_conflict_dependency_requeued\"}]'::jsonb THEN 1 ELSE 0 END DESC,
                                 COALESCE(priority, 0) DESC, created_at ASC LIMIT 1;" 2>/dev/null) || true
    next_job="${next_job// /}"
    if [[ -n "$next_job" ]]; then
        log "  PROMOTE_READY: 프로젝트 $project 의 다음 대기 작업 $next_job — 메인루프에서 곧 클레임"
    fi
}

cleanup_blocked_dependencies() {
    local released blocked_existing blocked_missing

    # ── 자동 부여된 의존성은 부모가 죽으면 풀어준다 (AADS-RUNNER-AUTODEP-RELEASE) ──
    # 2026-09-16, 잡 하나가 error 로 끝나자 뒤에 줄 서 있던 잡 4개가 연쇄로 취소됐다
    # (5a2953e2 → 863c0791, 97dcd84b → e7597fb8). 네 건 모두 사람이 건 의존성이 아니라
    # **같은 파일을 만진다는 이유로 시스템이 자동으로 건 것**이었다. 서로 다른 세션의
    # 작업이 남의 실패에 끌려 죽었다.
    #
    # 자동 의존성의 목적은 같은 파일을 동시에 고치지 않게 줄을 세우는 것 하나뿐이다.
    # 부모가 terminal 로 끝났으면 그 파일을 더 건드리지 않는다 — 줄 설 이유가 사라진다.
    # 그래서 취소가 아니라 **의존성만 풀고 대기열에 그대로 남긴다.**
    # 사람이 명시한 depends_on 은 "저게 끝나야 이게 의미가 있다" 는 뜻이므로 종전대로 취소한다.
    # 2026-09-19 추가 — cancelled 는 실패가 아니다.
    #
    # 이날 배치 릴리스 체인 5건이 통째로 멈췄다. 선두가 cancelled(중복이라 OPS 가
    # 거둔 것)로 끝나자 뒤의 4건이 "선행 작업이 cancelled 라 자동 진행 불가"로
    # 연쇄 취소 대상이 됐다. 그런데 cancelled 는 대부분 **중복·대체·수동 회수**이고,
    # "이 일을 하지 말라"는 뜻이 아니다. 뒤 작업은 여전히 해야 한다.
    # 그래서 명시적 depends_on 이라도 부모가 cancelled 면 **취소하지 않고 의존성만 푼다.**
    # 진짜 실패(error/rejected/rejected_done)는 종전대로 뒤를 막는다.
    #
    # 2026-10-06 예외 — blocked_dependency 로 취소된 부모는 "취소"가 아니라 상류 실패의 전파다.
    # 체인 runner-1d168a6d → runner-f3378426 → runner-1fc518dc(둘 다 명시 depends_on):
    # 1d168a6d 가 error(approval_commit_failed) 로 끝나 f3378426 이 blocked_dependency 로 취소되자,
    # 위 규칙이 그 취소를 "실패 아님"으로 읽어 1fc518dc 의 의존성을 풀었고 선행 결과 없이 시작됐다.
    # 그래서 cancelled 면제는 phase<>'blocked_dependency' 일 때만 적용하고, 그 경우는 아래
    # blocked_existing 가 자식도 blocked_dependency 로 연쇄 차단한다(원 상류를 error_detail 에 남김).
    released=$(db_exec "UPDATE pipeline_jobs p SET depends_on=NULL,
                        review_feedback=COALESCE(p.review_feedback,'') || E'\n[Runner Guard] 선행 작업 ' || p.depends_on || ' 이 ' || dep.status || CASE WHEN dep.status='cancelled' AND COALESCE(dep.phase,'') <> 'blocked_dependency' THEN ' 로 끝났다 — 취소는 실패가 아니므로 의존성만 풀고 단독 실행한다' ELSE ' 로 끝나 같은 파일을 더 건드리지 않는다 — 자동 부여된 의존성을 풀고 단독 실행한다' END,
                        updated_at=NOW()
                        FROM pipeline_jobs dep
                        WHERE p.depends_on = dep.job_id
                          AND p.status='queued'
                          AND p.phase IN ('queued','coding')
                          AND dep.status IN ('error','rejected','rejected_done','cancelled')
                          AND (p.logs @> '[{\"event\": \"file_conflict_auto_dependency\"}]'::jsonb
                               OR (dep.status = 'cancelled' AND COALESCE(dep.phase,'') <> 'blocked_dependency'))
                        RETURNING p.job_id;" 2>/dev/null) || true

    blocked_existing=$(db_exec "UPDATE pipeline_jobs p SET status='cancelled',
                                phase='blocked_dependency',
                                error_detail='blocked_dependency: parent ' || p.depends_on || ' is ' || dep.status
                                    || CASE WHEN dep.phase='blocked_dependency'
                                            THEN ' (root ' || COALESCE(substring(dep.error_detail from 'root ([A-Za-z0-9_-]+)'),
                                                                       substring(dep.error_detail from 'parent ([A-Za-z0-9_-]+) is'),
                                                                       dep.job_id) || ')'
                                            ELSE '' END,
                                review_feedback=COALESCE(p.review_feedback,'') || E'\n[Runner Guard] 선행 작업 ' || p.depends_on || ' 상태가 ' || dep.status || '라 자동 진행 불가 — blocked_dependency로 종결',
                                completed_at=NOW(), updated_at=NOW()
                                FROM pipeline_jobs dep
                                WHERE p.depends_on = dep.job_id
                                  AND p.status='queued'
                                  AND p.phase IN ('queued','coding')
                                  AND (dep.status IN ('error','rejected','rejected_done')
                                       OR (dep.status='cancelled' AND dep.phase='blocked_dependency'))
                                  AND NOT (p.logs @> '[{\"event\": \"file_conflict_auto_dependency\"}]'::jsonb)
                                  -- 릴리스 잡(DEPLOY_ONLY)은 앞 잡이 실패해도 죽이지 않는다.
                                  -- 2026-09-19 실측: runner-30757edd 가 선행 runner-e345161d
                                  -- 의 error 에 끌려 blocked_dependency 로 취소됐다. 그 잡의
                                  -- 일은 '앞에서 push 된 것들을 한 번에 배포하는 것' 이므로,
                                  -- 앞 잡 하나가 실패해도 이미 push 된 커밋은 여전히 배포해야
                                  -- 한다. 릴리스 잡까지 죽으면 배포 주체가 사라져 큐가 통째로
                                  -- 방치된다 — 오늘 실제로 그렇게 됐다.
                                  -- 판정은 헤더 선언만 본다(DEPLOY_ONLY_HEADER_SQL). 본문 산문의
                                  -- 언급까지 잡는 substring 매칭은 2026-09-30 큐 정지를 불렀다.
                                  AND NOT (${DEPLOY_ONLY_HEADER_SQL})
                                RETURNING p.job_id;" 2>/dev/null) || true
    blocked_missing=$(db_exec "UPDATE pipeline_jobs p SET status='cancelled',
                               phase='blocked_dependency',
                               error_detail='blocked_dependency: parent ' || p.depends_on || ' is missing',
                               review_feedback=COALESCE(p.review_feedback,'') || E'\n[Runner Guard] 선행 작업 ' || p.depends_on || ' 이 DB에 없어 자동 진행 불가 — blocked_dependency로 종결',
                               completed_at=NOW(), updated_at=NOW()
                               WHERE p.status='queued'
                                 AND p.phase IN ('queued','coding')
                                 AND p.depends_on IS NOT NULL
                                 AND NOT (p.logs @> '[{\"event\": \"file_conflict_auto_dependency\"}]'::jsonb)
                                 AND NOT (${DEPLOY_ONLY_HEADER_SQL})
                                 AND NOT EXISTS (
                                     SELECT 1 FROM pipeline_jobs dep
                                     WHERE dep.job_id = p.depends_on
                                 )
                               RETURNING p.job_id;" 2>/dev/null) || true
    if [[ -n "$released" ]]; then
        log "  AUTODEP_RELEASED ${released//$'\n'/,} — 부모가 terminal 이라 자동 의존성 해제, 대기열 유지"
    fi
    if [[ -n "$blocked_existing$blocked_missing" ]]; then
        log "  BLOCKED_DEPENDENCY_CLEANUP existing=${blocked_existing//$'\n'/,} missing=${blocked_missing//$'\n'/,}"
    fi

    warn_stuck_dependency_queue
    apply_batch_release_directive
}

# 교착 안전망 — 부모가 terminal(done 이외)인데 queued 로 남은 잡을 경고한다.
#
# claim SQL 은 dep.status='done' 만 허용하므로, 취소 분기(cleanup_blocked_dependencies)와
# 해제 분기 어느 쪽에도 걸리지 않은 잡은 영구히 claim 되지 않는다. 2026-09-30 에는
# 그런 잡 하나가 큐 선두에서 2시간 40분간 조용히 큐 전체를 세웠다.
# 여기서는 상태를 바꾸지 않는다 — 보이게 만드는 것까지만 한다.
#
# 대기 경과 = 부모가 terminal 이 된 시각(없으면 잡 생성 시각) 중 늦은 쪽부터의 분.
# 부모가 방금 끝나 가드가 곧 처리할 잡을 경고하지 않도록 임계(기본 30분)를 둔다.
# 환경변수: DEP_STUCK_WARN_MIN(30) / DEP_STUCK_CHECK_INTERVAL_SEC(300) / DEP_STUCK_REWARN_SEC(1800)
_dep_stuck_sql() {
    local warn_min="$1"
    printf '%s' "SELECT p.job_id, COALESCE(p.depends_on,''), COALESCE(dep.status,'missing'),
        FLOOR(EXTRACT(EPOCH FROM (NOW() - GREATEST(p.created_at, COALESCE(dep.completed_at, dep.updated_at)))) / 60)::int,
        COALESCE(p.chat_session_id,'')
      FROM pipeline_jobs p
      LEFT JOIN pipeline_jobs dep ON dep.job_id = p.depends_on
      WHERE p.status='queued'
        AND p.depends_on IS NOT NULL
        AND (dep.job_id IS NULL OR dep.status IN ('error','rejected','rejected_done','cancelled'))
        AND NOW() - GREATEST(p.created_at, COALESCE(dep.completed_at, dep.updated_at)) >= INTERVAL '${warn_min} minutes'
      ORDER BY p.created_at;"
}

declare -gA _DEP_STUCK_WARNED=()
_DEP_STUCK_LAST_CHECK=0

warn_stuck_dependency_queue() {
    local warn_min="${DEP_STUCK_WARN_MIN:-30}"
    local check_iv="${DEP_STUCK_CHECK_INTERVAL_SEC:-300}"
    local rewarn_iv="${DEP_STUCK_REWARN_SEC:-1800}"
    [[ "$warn_min" =~ ^[0-9]{1,5}$ ]] || warn_min=30
    [[ "$check_iv" =~ ^[0-9]{1,6}$ ]] || check_iv=300
    [[ "$rewarn_iv" =~ ^[0-9]{1,6}$ ]] || rewarn_iv=1800

    local now
    now=$(date +%s)
    (( now - _DEP_STUCK_LAST_CHECK >= check_iv )) || return 0
    _DEP_STUCK_LAST_CHECK=$now

    local rows
    rows=$(db_exec "$(_dep_stuck_sql "$warn_min")" 2>/dev/null) || return 0
    [[ -n "$rows" ]] || return 0

    local s_job s_parent s_pstatus s_wait s_session last msg
    while IFS=$'\x1e' read -r s_job s_parent s_pstatus s_wait s_session; do
        [[ "$s_job" =~ ^[a-zA-Z0-9_-]+$ ]] || continue
        last="${_DEP_STUCK_WARNED[$s_job]:-0}"
        (( now - last >= rewarn_iv )) || continue
        _DEP_STUCK_WARNED[$s_job]=$now
        msg="⚠️ [Pipeline Runner] 큐 교착 의심: ${s_job} 이 선행 ${s_parent}(status=${s_pstatus}) 때문에 ${s_wait}분째 queued 로 남아 있다 — claim 조건(dep.status='done')을 영원히 만족하지 못한다. 자동 조치는 하지 않았다. 수동 확인 필요(선행 재실행 또는 depends_on 해제/취소)."
        log "  DEP_STUCK_WARN job=$s_job parent=$s_parent parent_status=$s_pstatus wait_min=$s_wait"
        post_to_chat "$s_session" "$msg" || true
    done <<< "$rows"
}

# 배치 릴리스 창이 열려 있으면 새로 들어온 잡에도 같은 지시를 붙인다.
#
# 2026-09-18 에 대기 잡 4건을 "커밋·푸시까지만" 으로 묶어 배포를 1회로 줄였는데,
# 편입이 **수동 UPDATE** 라 그 뒤 들어온 잡은 매번 샜다. 09-18 에 2건, 09-19 에
# runner-90cf3402 가 또 샜다 — 샌 잡은 자기 혼자 배포해 "배포가 잦다" 는 문제를
# 되살린다. 제출 경로(API·MCP·수동 INSERT)가 여럿이라 한 곳을 고쳐서는 못 막는다.
# 큐를 보는 이 자리에서 붙이면 경로와 무관하게 걸린다.
#
# 창의 정의: 같은 프로젝트에 배치 지시를 단 잡이 아직 살아 있으면(queued/running/
# review_hold) 창이 열린 것으로 본다. 그 잡들이 모두 끝나면 자동으로 닫힌다.
# 릴리스 잡(DEPLOY_ONLY)에는 붙이지 않는다 — 붙이면 자기가 자기를 막는다.
apply_batch_release_directive() {
    local attached
    attached=$(db_exec "UPDATE pipeline_jobs p
                        SET instruction = p.instruction || E'\n\n## [OPS 배치 릴리스 지시 — 자동 부착]\n이 잡은 커밋까지만 수행한다. 빌드·배포·재기동은 금지한다.\npush 이후의 릴리스는 별도 DEPLOY_ONLY 잡이 최신 origin/main 을 1회 배포하여 이 잡을 포함해 일괄 반영한다.',
                            updated_at=NOW()
                        WHERE p.status='queued'
                          AND p.phase IN ('queued','coding')
                          AND p.instruction NOT ILIKE '%OPS 배치 릴리스 지시%'
                          AND p.instruction NOT ILIKE '%DEPLOY_ONLY%'
                          AND EXISTS (SELECT 1 FROM pipeline_jobs q
                                      WHERE q.project = p.project
                                        AND q.job_id <> p.job_id
                                        AND q.status IN ('queued','running','review_hold')
                                        AND q.instruction ILIKE '%OPS 배치 릴리스 지시%')
                        RETURNING p.job_id;" 2>/dev/null) || true
    if [[ -n "$attached" ]]; then
        log "  BATCH_DIRECTIVE_ATTACHED ${attached//$'\n'/,} — 배치 릴리스 창이 열려 있어 커밋·푸시까지만 수행하도록 편입"
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

    # 중복 제출 방지: 10분 내 done이면 경고, 진행 중인 동일 작업이 있으면 차단.
    # 이미 cancelled/dedup_blocked 인 작업은 "진행 중"이 아니다 — 예전에는 NOT IN (done,error,
    # rejected_done) 이라 제출 측 dedup 으로 취소된 형제를 근거로 자신을 취소해 둘 다 사라졌다
    # (runner-62f55395 ↔ runner-7f065ebd, 2026-10-02). queued/claimed 끼리는 (created_at, job_id)
    # 가 앞선 쪽만 근거가 되므로 동시에 서로를 취소하는 일도 없다.
    local dup_job
    dup_job=$(db_exec "SELECT d.job_id FROM pipeline_jobs d, pipeline_jobs me
                       WHERE me.job_id='${job_id}'
                         AND d.project='${project}'
                         AND d.instruction_hash='${inst_hash}'
                         AND d.job_id != me.job_id
                         AND COALESCE(d.phase,'') <> 'dedup_blocked'
                         AND (
                           (d.status NOT IN ('done','error','rejected_done','cancelled','queued','claimed'))
                           OR (d.status IN ('queued','claimed')
                               AND (d.created_at, d.job_id) < (me.created_at, me.job_id))
                           OR (d.status = 'done' AND d.updated_at > NOW() - INTERVAL '10 minutes')
                         )
                       LIMIT 1;" 2>/dev/null) || true
    if [[ -n "$dup_job" ]]; then
        dup_job="${dup_job// /}"
        # 10분 내 done이거나 아직 진행 중이면 차단
        local dup_status dup_phase
        dup_status=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${dup_job}';" 2>/dev/null) || true
        dup_status="${dup_status// /}"
        dup_phase=$(db_exec "SELECT COALESCE(phase,'') FROM pipeline_jobs WHERE job_id='${dup_job}';" 2>/dev/null) || true
        dup_phase="${dup_phase// /}"
        # 취소 직전 재확인: 가리키는 작업이 그 사이 취소/종료됐으면 자신이 진행한다.
        if [[ -z "$dup_status" || "$dup_status" =~ ^(cancelled|error|rejected_done)$ || "$dup_phase" == "dedup_blocked" ]]; then
            log "  DEDUP_SKIP: 기존 작업 $dup_job 이 이미 비활성(${dup_status:-없음}/${dup_phase}) — $job_id 는 계속 진행"
        elif [[ "$dup_status" != "done" ]]; then
            log "  DEDUP_BLOCK: 동일 작업 진행 중: $dup_job ($dup_status) — $job_id 차단"
            db_update "UPDATE pipeline_jobs SET status='cancelled', phase='dedup_blocked',
                       error_detail='dedup_blocked: 기존 작업 ${dup_job} 계속 진행',
                       review_feedback=E'[중복 차단] 동일 작업 진행 중: ${dup_job} (${dup_status})',
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "job_terminal" "cancelled" "dedup_blocked" "" "" "" "" "{\"reason\":\"dedup_blocked\",\"existing_job\":\"${dup_job}\"}"
            return 1
        else
            log "  DEDUP_WARN: 10분 내 동일 작업 완료: $dup_job (계속 실행하되 경고)"
            db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[DEDUP 경고] 유사 작업: ${dup_job}',
                       updated_at=NOW() WHERE job_id='${job_id}';"
        fi
    fi

    return 0
}

# ── 프로세스 생존 확인 (watchdog) ──────────────────────────────────────
_watchdog_check() {
    local filter="$1"
    # 모델 실행과 리뷰는 서로 다른 예산을 사용한다. 리뷰 진입 시 updated_at을
    # 갱신하므로 긴 모델 실행 직후 정상 리뷰가 전체 job 상한에 잘리지 않는다.
    local timed_out
    timed_out=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='error',
                         error_detail='timeout_max_runtime',
                         review_feedback=COALESCE(review_feedback,'') || E'\n[Watchdog] 단계별 최대 실행시간 초과 타임아웃',
                         completed_at=NOW(), updated_at=NOW()
                         WHERE status='running'
                           AND started_at IS NOT NULL
                           AND (
                             (phase = 'ai_review'
                              AND updated_at < NOW() - INTERVAL '${AADS_REVIEW_MAX_RUNTIME} seconds')
                             OR
                             (phase IS DISTINCT FROM 'ai_review'
                              AND started_at < NOW() - INTERVAL '${MAX_JOB_RUNTIME} seconds')
                           )
                           $filter
                         RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$timed_out" ]]; then
        log "  WATCHDOG_TIMEOUT: $timed_out"
        # 타임아웃된 작업의 session_id 조회 → 채팅 알림 + AI 자동 반응
        for t_job in $timed_out; do
            t_job="${t_job// /}"
            [[ -z "$t_job" ]] && continue
            local t_session t_project t_pid t_scope
            IFS=$'\x1e' read -r t_session t_project t_pid t_scope <<< "$(db_exec "SELECT chat_session_id, project, runner_pid, COALESCE(parallel_group, '') FROM pipeline_jobs WHERE job_id='${t_job}';" 2>/dev/null || true)"
            t_session="${t_session// /}"
            t_project="${t_project// /}"
            t_pid="${t_pid// /}"
            t_scope="${t_scope// /}"
            if [[ "$t_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$t_pid" 2>/dev/null; then
                pkill -TERM -P "$t_pid" 2>/dev/null || true
                kill -TERM "$t_pid" 2>/dev/null || true
            fi
            db_update "UPDATE pipeline_jobs SET runner_pid=NULL WHERE job_id='${t_job}' AND status='error';" || true
            _release_work_lock "$t_project" "$t_job" "$t_scope"
            post_to_chat "$t_session" "⏰ [Pipeline Runner] 단계별 실행시간 초과: $t_job — 자동 종료됨"
            record_runner_event "$t_job" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"timeout_max_runtime\"}"
            _notify_ai "$t_job"
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
    # intent 를 명시한다. 2026-09-17 까지 이 INSERT 는 intent 를 비워 뒀고,
    # 화면은 러너 메시지를 본문 문자열('[Pipeline Runner]')로만 알아볼 수
    # 있었다. 그 결과 7일간 525건이 전부 숨김으로 묻혀, 러너가 32분을
    # 일하는 동안 대화창은 완전히 조용했다(세션 9fa305c5 실측).
    #
    # is_hidden 은 그대로 true 로 둔다 — DB 트리거가 intent='pipeline_runner'
    # + model_used 빈값이면 숨김으로 찍는다. 메시지 수·목록 미리보기를
    # 건드리지 않으려는 것이고, 화면 노출은 조회 필터가 따로 허용한다.
    db_update "INSERT INTO chat_messages (id, session_id, role, content, intent, created_at)
               VALUES (gen_random_uuid(), '${session_id}'::uuid, 'assistant',
                       ${safe_content}, 'pipeline_runner', NOW());" || true
}

# C4: 원자적 Job 클레임 — UPDATE ... RETURNING으로 동시 실행 방지
# 프로젝트별 동시실행 Lock: 같은 프로젝트에 running/claimed 작업이 있으면 claim하지 않음
claim_queued_job() (
    # Both engine services on one host share this lock. Keep it until the
    # queued -> claimed UPDATE commits, so the last server slot cannot race.
    if [[ -n "${MAX_CONCURRENT_SERVER:-}" ]]; then
        [[ "$MAX_CONCURRENT_SERVER" =~ ^[1-9][0-9]{0,2}$ && -n "$1" ]] || return 1
        exec 8>"${RUNNER_CAPACITY_LOCK_FILE:-/tmp/pipeline-runner-capacity.lock}"
        flock -x 8 || return 1
        local server_running
        server_running=$(db_exec "SELECT count(*) FROM pipeline_jobs WHERE status IN ('running','claimed') $1;") || return 1
        server_running="${server_running//[[:space:]]/}"
        [[ "$server_running" =~ ^[0-9]+$ ]] || return 1
        (( server_running < MAX_CONCURRENT_SERVER )) || return 0
    fi
    _claim_queued_job "$1"
)

_claim_queued_job() {
    local filter="$1"
    local engine_predicate model_return_expr
    if [[ "$RUNNER_ENGINE_MODE" == "litellm" ]]; then
        engine_predicate="AND COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') LIKE 'litellm:%'"
        model_return_expr="COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    else
        engine_predicate="AND NOT (COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') LIKE 'litellm:%' AND p.project IN ('GO100','KIS','SF','NTV2'))"
        model_return_expr="COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    fi
    # UTF-8 바이트 hex로 반환해 줄바꿈/RS/pipe를 포함한 원문을 보존한다.
    # AADS-211: depends_on 체크 — 의존 작업이 done이 아니면 스킵
    # RUNNER_ENGINE_MODE=litellm: litellm:* 작업만 claim. general은 원격 litellm 작업을 전용 러너에 넘김.
    # 어느 서버가 집었는지 남긴다. runner_pid 는 숫자라 호스트를 구분하지 못한다.
    db_exec "UPDATE pipeline_jobs SET status='claimed', runner_host='${RUNNER_HOST_NAME}', updated_at=NOW()
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
                ORDER BY CASE WHEN p.instruction ~* '(^|[[:space:]])PRIORITY:[[:space:]]*P0' THEN 1 ELSE 0 END DESC,
                         CASE WHEN p.logs @> '[{\"event\": \"file_conflict_dependency_requeued\"}]'::jsonb THEN 1 ELSE 0 END DESC,
                         COALESCE(p.priority, 0) DESC, p.created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, encode(convert_to(instruction, 'UTF8'), 'hex'), chat_session_id, max_cycles, ${model_return_expr}, COALESCE(size,'M'), COALESCE(parallel_group,'');"
}

# 러너 재기동 직후 status=claimed / started_at NULL 로 고착된 작업을 되돌린다
# (runner-14f2dd22, 2026-10-02). claim 은 DB 에서만 일어나고 run_job 서브셸이 죽으면
# 그 행을 아무도 running 으로 올리지 않는다. 살아 있는 서브셸(_bg_jobs)의 job 은 제외하고,
# 같은 호스트의 다른 엔진 러너가 집은 작업을 건드리지 않도록 이 엔진이 claim 하는 집합으로만 한정한다.
reclaim_orphan_claims() {
    local _entry _pid _jid _live="" _engine_pred _rows
    for _pid in "${!_bg_jobs[@]}"; do
        kill -0 "$_pid" 2>/dev/null || continue
        _entry="${_bg_jobs[$_pid]}"
        _jid="${_entry%%|*}"
        [[ "$_jid" =~ ^[A-Za-z0-9_-]+$ ]] && _live+="${_live:+,}'${_jid}'"
    done
    [[ -n "${_current_job_id:-}" && "$_current_job_id" =~ ^[A-Za-z0-9_-]+$ ]] && _live+="${_live:+,}'${_current_job_id}'"
    if [[ "$RUNNER_ENGINE_MODE" == "litellm" ]]; then
        _engine_pred="AND COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), '') LIKE 'litellm:%'"
    else
        _engine_pred="AND NOT (COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), '') LIKE 'litellm:%' AND project IN ('GO100','KIS','SF','NTV2'))"
    fi
    _rows=$(db_exec "UPDATE pipeline_jobs SET status='queued', phase='queued', runner_host=NULL, updated_at=NOW()
                     WHERE status='claimed'
                       AND runner_host=$(sql_escape "$RUNNER_HOST_NAME")
                       AND started_at IS NULL
                       AND updated_at < NOW() - INTERVAL '2 minutes'
                       ${_live:+AND job_id NOT IN (${_live})}
                       ${_engine_pred}
                     RETURNING job_id;" 2>/dev/null) || return 0
    while IFS= read -r _jid; do
        _jid="${_jid// /}"
        [[ -n "$_jid" ]] || continue
        log "  ORPHAN_CLAIM_REQUEUE job=${_jid} host=${RUNNER_HOST_NAME} — claimed/started_at NULL 고아를 queued 로 회수"
        record_runner_event "$_jid" "job_requeued" "queued" "queued" "" "" "" "" "{\"error_detail\":\"orphan_claim_requeued\"}"
    done <<< "$_rows"
}

# claim 결과의 instruction_hex를 Bash 내장 printf로 디코딩한다. 외부 도구
# 의존성이 없고, 잘못된 hex는 호출자가 fail-closed 처리한다.
decode_claim_instruction() {
    local hex="$1" escaped="" i byte
    decoded_instruction=""
    [[ "$hex" =~ ^([[:xdigit:]]{2})*$ ]] || return 2
    [[ -n "$hex" ]] || return 1
    for ((i=0; i<${#hex}; i+=2)); do
        byte="${hex:i:2}"
        escaped+="\\x${byte}"
    done
    printf -v decoded_instruction '%b' "$escaped"
    [[ -n "$decoded_instruction" ]] || return 1
}

dispatch_claimed_job() {
    local pending="$1" job_id project instruction_hex session_id max_cycles job_model job_size parallel_group
    local decode_rc
    IFS=$'\x1e' read -r job_id project instruction_hex session_id max_cycles job_model job_size parallel_group <<< "$pending"
    [[ -n "$job_id" && -n "$project" ]] || return 0
    decoded_instruction=""
    decode_claim_instruction "$instruction_hex" || {
        decode_rc=$?
        if (( decode_rc == 1 )); then
            _fail_job "$job_id" "$session_id" "EMPTY_INSTRUCTION" "Claimed job instruction is empty"
        else
            _fail_job "$job_id" "$session_id" "INVALID_INSTRUCTION_HEX" "Claimed job instruction is not valid UTF-8 hex"
        fi
        return 0
    }
    run_job "$job_id" "$project" "$decoded_instruction" "$session_id" "${max_cycles:-3}" "${job_model:-litellm:minimax-m2.7}" "${job_size:-M}" "${parallel_group:-}" &
    _bg_jobs[$!]="${job_id}|${session_id}"
    log "  BG_START: job=$job_id pid=$! (parallel)"
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

# ── review_hold dirty 산출물 복구 (AADS-REVIEWHOLD-DIRTY-STRAND-P0) ──
#
# 검수 전 커밋(commit_job_worktree_for_approval)이 도입되기 전 버전의 러너가
# 남긴 review_hold 잡은 워크트리가 dirty 하고 commit_hash 가 NULL 이다.
# 스위퍼는 그 워크트리를 커밋하지 못한다 — 스위퍼가 커밋을 만들면 pre-commit
# 훅(API 키 탐지·ruff·단위테스트)을 거치지 않은 리비전이 승인 큐로 올라간다.
# 그래서 역할을 나눈다:
#   스위퍼: 재검수 APPROVE + "워크트리 변경 == 검수받은 git_diff" 확정 →
#           error_detail='review_hold_recovery_pending' 표시만 남긴다.
#   러너(여기): 그 표시를 보고 **기존 커밋 경로로만** 재진입한다.
# 두 프로세스는 DB 플래그로만 만난다 — 어느 쪽도 상대 스크립트를 부르지 않는다.
#
# phase 는 'review_hold' 그대로 둔다. 대시보드 보드 상태(_TASK_BOARD_STATUS_SQL)가
# phase='review_hold' 로 이 칸을 판정하므로, 여기서 phase 를 바꾸면 복구 대기 중인
# 잡이 보드에서 'error' 로 보인다.
claim_review_hold_recovery_job() {
    local filter="$1"
    db_exec "UPDATE pipeline_jobs SET error_detail='review_hold_recovery_committing', updated_at=NOW()
             WHERE job_id = (
                SELECT job_id FROM pipeline_jobs
                WHERE status='review_hold'
                  AND error_detail='review_hold_recovery_pending'
                  AND commit_hash IS NULL $filter
                ORDER BY updated_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, chat_session_id;"
}

recover_review_hold_job() {
    local job_id="$1" project="$2" session_id="$3"
    local worktree_dir="/tmp/aads-wt-${job_id}"
    local instruction main_workdir base_sha commit_sha raw_commit_sha now_status

    instruction=$(get_job_instruction "$job_id")
    if ! main_workdir=$(resolve_project_workdir "$project" "$instruction"); then
        fail_invalid_aads_target "$job_id" "$session_id"
        return 1
    fi

    # 산출물이 사라졌으면 복구할 것이 없다 — 구조적 terminal.
    if [[ ! -d "$worktree_dir" ]] || [[ "$(git -C "$worktree_dir" rev-parse --is-inside-work-tree 2>/dev/null || true)" != "true" ]]; then
        log "  REVIEW_HOLD_RECOVERY_NO_ARTIFACT job=$job_id worktree=$worktree_dir"
        db_update "UPDATE pipeline_jobs SET status='error', phase='review_hold_no_artifact',
                   error_detail='review_hold_no_artifact',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner] review_hold 복구 불가 — 워크트리 소실: ${worktree_dir}',
                   completed_at=NOW(), updated_at=NOW()
                   WHERE job_id='${job_id}' AND status='review_hold';"
        record_runner_event "$job_id" "job_terminal" "error" "review_hold_no_artifact" "" "" "" "" "{\"error_detail\":\"review_hold_no_artifact\"}"
        _notify_ai "$job_id"
        return 1
    fi

    base_sha=$(resolve_job_base_sha "$worktree_dir")
    log "▶ REVIEW_HOLD_RECOVERY job=$job_id project=$project worktree=$worktree_dir base=${base_sha:0:12}"

    # 기존 승인 커밋 경로 그대로 — pre-commit 훅을 거치고 --no-verify 를 쓰지 않는다.
    raw_commit_sha=$(commit_job_worktree_for_approval "$job_id" "$session_id" "$worktree_dir" "$main_workdir" "$instruction" "$base_sha") || return 1
    commit_sha=$(printf '%s' "$raw_commit_sha" | tr -d '\r' | grep -oE '[0-9a-f]{40}' | tail -n1) || commit_sha=""
    if [[ -z "$commit_sha" || "$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)" != "$commit_sha" ]]; then
        _fail_job "$job_id" "$session_id" "review_hold_recovery_sha_mismatch" "review_hold 복구 커밋 SHA 와 runner worktree HEAD 불일치"
        return 1
    fi

    db_update "UPDATE pipeline_jobs SET status='awaiting_approval', phase='awaiting_approval',
               commit_hash='${commit_sha}',
               error_detail=NULL, runner_pid=NULL,
               approval_requested_at=NOW(),
               review_feedback=COALESCE(review_feedback,'') || E'\n[Runner] review_hold 산출물 복구 커밋 완료 — ${commit_sha}',
               updated_at=NOW()
               WHERE job_id='${job_id}' AND status='review_hold';"
    now_status=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null || true)
    now_status="${now_status// /}"
    if [[ "$now_status" != "awaiting_approval" ]]; then
        _fail_job "$job_id" "$session_id" "review_hold_recovery_persist_failed" "복구 커밋 ${commit_sha} 은 만들었으나 승인 상태 저장 실패 — 워크트리 보존"
        return 1
    fi

    log "  REVIEW_HOLD_RECOVERED job=$job_id sha=$commit_sha — 승인 대기로 이동"
    record_runner_event "$job_id" "approval_requested" "awaiting_approval" "awaiting_approval" "" "" "" "" "{\"commit_hash\":\"${commit_sha}\",\"source\":\"review_hold_recovery\"}"
    post_to_chat "$session_id" "🔁 [Pipeline Runner] review_hold 산출물 복구 완료: $job_id — 재검수를 통과한 diff 그대로 커밋(${commit_sha:0:8})하고 승인 대기로 옮겼습니다. 푸시·배포는 기존 승인 경로에서 진행됩니다."
    _notify_ai "$job_id"
    return 0
}

# ── 작업 실행 ─────────────────────────────────────────────────────────
run_job() {
    local job_id="$1" project="$2" instruction="$3" session_id="$4" max_cycles="$5" job_model="${6:-auto}" job_size="${7:-M}" parallel_group="${8:-}"
    local output_file="$ARTIFACT_DIR/${job_id}.out" err_file="$ARTIFACT_DIR/${job_id}.err"
    local workdir
    if ! workdir=$(resolve_project_workdir "$project" "$instruction"); then
        fail_invalid_aads_target "$job_id" "$session_id"
        return 1
    fi
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
    # 상한 단일 출처: Redis 작업잠금 상한을 API env 기본값(6)에 맡기지 않고 러너의
    # MAX_CONCURRENT_PER_PROJECT 를 그대로 넘긴다 (API 가 min(max,50) 으로 클램프).
    local work_lock_max_param=""
    [[ "${MAX_CONCURRENT_PER_PROJECT:-}" =~ ^[1-9][0-9]{0,2}$ ]] && work_lock_max_param="&max=${MAX_CONCURRENT_PER_PROJECT}"
    lock_result=$(curl -sf -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/work/acquire?project=${project}&session_id=${job_id}${work_lock_scope_param}${work_lock_max_param}" 2>/dev/null) || true
    if echo "$lock_result" | grep -q '"acquired":false'; then
        local holder
        holder=$(echo "$lock_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('holder','unknown'))" 2>/dev/null) || holder="unknown"
        log "  REDIS_LOCK: $project 작업 중 (holder=$holder) — $job_id queued로 되돌림"
        db_update "UPDATE pipeline_jobs SET status='queued', phase='queued', updated_at=NOW() WHERE job_id='${job_id}';"
        return 0
    fi

    # ── 사전 검증 (Pre-validation) ──
    pre_validate "$job_id" "$project" "$session_id" "$instruction" || { _release_work_lock "$project" "$job_id" "$parallel_group"; return 1; }

    # ── 목업 승인 bundle 실행 직전 재검증 (submit 때와 같은 bundle, 같은 규칙) ──
    # 0=통과/해당없음, 10=승인 상태가 맞지 않음, 20=게이트 판정 불가. 10·20 은 워커를 시작하지 않는다.
    # 실행 중인 다른 작업은 건드리지 않는다.
    local _mockup_out _mockup_rc=0
    _mockup_out=$(printf '%s' "$instruction" | timeout 30 python3 "$(dirname "${BASH_SOURCE[0]}")/verify_mockup_approval.py" \
        --job-id "$job_id" --phase pre_execution --instruction-file - 2>&1) || _mockup_rc=$?
    # 게이트 스크립트 자체의 오류(rc 가 10/20 이 아님)는 목업·대시보드 작업일 때만 차단한다 — 무관한 작업이 인프라 오류로 멈추지 않게.
    if [ "$_mockup_rc" -ne 0 ] && [ "$_mockup_rc" -ne 10 ] && [ "$_mockup_rc" -ne 20 ] \
        && ! printf '%s' "$instruction" | grep -qE 'MOCKUP_REVIEW_ID|aads-dashboard|UI_TASK'; then
        log "  MOCKUP_GATE job=$job_id script_error rc=$_mockup_rc (non-UI task, continuing)"
        _mockup_rc=0
    fi
    if [ "$_mockup_rc" -ne 0 ]; then
        log "  MOCKUP_GATE job=$job_id rc=$_mockup_rc ${_mockup_out:0:300}"
        _fail_job "$job_id" "$session_id" "mockup_approval_required" "목업 승인 bundle 검증 실패(rc=$_mockup_rc): ${_mockup_out:0:300}"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        return 1
    fi

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
            # 이전 시도가 아직 .out/.err를 쓰고 있을 수 있어 산출물은 보존한다.
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
    # runner_pid must identify the stable per-job subshell, not a short-lived
    # model CLI child. During model fallback the child exits normally before
    # the next attempt starts; publishing that PID lets the watchdog race the
    # fallback loop and misclassify a live job as process_died.
    db_update "UPDATE pipeline_jobs SET status='running', phase='claude_code_work',
               runner_pid=${BASHPID}, started_at=NOW(), updated_at=NOW()
               WHERE job_id='${job_id}';"
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
            claude_fb="claude-sonnet-5-5"
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
                XL)      claude_primary="claude-sonnet-5-5"; claude_secondary="claude-opus-5-5" ;;
                L|M)     claude_primary="claude-sonnet-5-5"; claude_secondary="claude-opus-5-5" ;;
                S|XS|*)  claude_primary="claude-sonnet-5-5"; claude_secondary="claude-haiku-4-5-20251001" ;;
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
    # AADS-RUNNER-SLOT-DEPRIORITIZE (2026-09-14)
    # 한도가 소진된 계정을 첫 시도에서 뒤로 미룬다. 2026-09-14 실측: 계정1 이 주간한도
    # 429(resets Sep 16, 3am KST) 라 매 job 이 attempt 1 을 40초씩 헛썼다. 최종 status 는
    # done 이라 상태만 봐서는 보이지 않는 낭비였다.
    # 만료 시각이 지나면 파일을 지우지 않아도 스스로 원래 순서(1,2,1,2)로 돌아간다 —
    # 사람이 되돌리는 것을 잊어도 안전하게 복귀한다.
    local _slot_first=1
    if [[ -f /root/.claude/slot_limits.env ]]; then
        # shellcheck disable=SC1091
        source /root/.claude/slot_limits.env 2>/dev/null || true
    fi
    local _slot1_until="${AADS_RUNNER_SLOT1_LIMITED_UNTIL:-0}"
    [[ "$_slot1_until" =~ ^[0-9]+$ ]] || _slot1_until=0
    if (( _slot1_until > $(date +%s) )); then
        _slot_first=2
        log "  SLOT_DEPRIORITIZE job=$job_id 계정1 한도소진(until=$_slot1_until) → 계정2 우선"
    fi
    # AADS-RUNNER-SLOT4 (2026-09-19): 계정 사다리를 DB 에서 받는다.
    # 한도가 남은 계정만 priority 순으로 돈다. DB 조회가 실패하거나 남은 계정이
    # 하나도 없으면 아래 기존 2슬롯 경로로 조용히 폴백한다(동작 보존).
    local AVAIL_SLOTS=() _db_slot_line="" _db_slots=""
    _db_slots=$(get_db_anthropic_slots) || _db_slots=""
    if [[ -n "$_db_slots" ]]; then
        while IFS= read -r _db_slot_line; do
            [[ -n "$_db_slot_line" ]] && AVAIL_SLOTS+=("$_db_slot_line")
        done <<< "$_db_slots"
    fi
    if [[ ${#AVAIL_SLOTS[@]} -gt 0 ]]; then
        log "  DB_SLOT_CYCLE job=$job_id available=${AVAIL_SLOTS[*]} (llm_api_keys priority 순)"
        for ((i=0; i<${#MODEL_CYCLE[@]}; i++)); do
            TOKEN_CYCLE+=("${AVAIL_SLOTS[$(( i % ${#AVAIL_SLOTS[@]} ))]}")
        done
    else
        log "  DB_SLOT_CYCLE_FAIL job=$job_id → 기존 2슬롯 경로로 폴백"
        for ((i=0; i<${#MODEL_CYCLE[@]}; i++)); do
            if [[ "$_slot_first" == "2" ]]; then
                TOKEN_CYCLE+=($(( (i + 1) % 2 + 1 )))
            else
                TOKEN_CYCLE+=($(( i % 2 + 1 )))
            fi
        done
    fi
    # AADS-RUNNER-SLOT-LEASE (2026-09-14): 대여 토큰을 job 마다 다시 읽는다.
    # 스크립트 상단의 source 는 데몬 기동 시 딱 한 번만 돈다. 슬롯이 없는 서버
    # (contabo14 / cafe24_114)는 contabo116 이 10분마다 밀어 넣는 임시 accessToken 으로
    # 사는데, 기동 시점 값을 붙들고 있으면 만료되는 순간 그 데몬은 재시작 전까지
    # 전량 실패한다. 여기서 다시 읽으면 데몬을 안 건드려도 항상 최신 출입증을 쓴다.
    # 슬롯 자격증명이 있는 contabo116 에서는 이 파일이 없어 아무 일도 하지 않는다.
    # shellcheck disable=SC1091
    source /root/scripts/runner.env 2>/dev/null || true
    local TOKEN_1="${ANTHROPIC_AUTH_TOKEN:-}"
    local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_2:-}"
    # C-4: 빈 토큰 가드 — 둘 다 비어있으면 즉시 실패 처리
    # 단, 슬롯 자격증명이 살아 있으면 고정 토큰이 없어도 실행 가능하므로 차단하지 않는다.
    local _slot_auth_available=""
    _slot_auth_available="$(slot_credentials_file 1 || slot_credentials_file 2 || slot_credentials_file 3 || slot_credentials_file 4 || leased_slot_token 1 || leased_slot_token 2 || leased_slot_token 3 || leased_slot_token 4 || true)"
    if [[ -z "$TOKEN_1" && -z "$TOKEN_2" && -z "$_slot_auth_available" ]]; then
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
        # 2026-09-12: 접두사 없는 Codex 모델명(gpt-*)이 Claude CLI 경로로 잘못 라우팅되어
        # MODEL_CONTRACT_REJECTED(unsupported_claude_model) 무한 루프를 유발하는 문제 방지 — codex: 접두사 자동 보정
        if [[ "$current_model" == gpt-* ]]; then
            log "  MODEL_PREFIX_NORMALIZED job=$job_id from=$current_model to=codex:$current_model"
            current_model="codex:${current_model}"
        fi
        local effective_model="$current_model"
        local token_slot="${TOKEN_CYCLE[$attempt]}"
        local cycle_num=$(( attempt / 2 + 1 ))

        # 계정 스위치: 토큰 교체 (R-AUTH)
        # 1순위 — 릴레이 슬롯 자격증명(refresh 가능). CLI 가 만료 전 스스로 갱신하므로
        #          고정 토큰처럼 한 번 죽으면 끝나는 상태가 되지 않는다.
        # 2순위 — .env 고정 oat 토큰 (슬롯이 없는 서버의 기존 경로).
        # Claude Code CLI는 OAuth 토큰을 CLAUDE_CODE_OAUTH_TOKEN으로 받아야 한다.
        # oat 토큰을 ANTHROPIC_API_KEY에 넣으면 x-api-key 경로로 전송되어 Invalid API key가 발생한다.
        local slot_cred_file="" leased_token=""
        slot_cred_file="$(slot_credentials_file "$token_slot" || true)"
        [[ -z "$slot_cred_file" ]] && leased_token="$(leased_slot_token "$token_slot" || true)"
        if [[ -n "$slot_cred_file" ]]; then
            # 래퍼가 격리 HOME 에 자격증명을 staging 하고 CLAUDE_CODE_OAUTH_TOKEN 을 unset 한다.
            # 여기서 고정 토큰을 export 하면 래퍼가 지우기 전까지 우선순위가 뒤집히므로 지운다.
            unset CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true
            unset ANTHROPIC_API_KEY 2>/dev/null || true
            unset ANTHROPIC_BASE_URL 2>/dev/null || true
            log "  TOKEN_SWITCH job=$job_id → 계정${token_slot} via slot_credentials (refreshable)"
        elif [[ -n "$leased_token" ]]; then
            export CLAUDE_CODE_OAUTH_TOKEN="$leased_token"
            unset ANTHROPIC_API_KEY 2>/dev/null || true
            unset ANTHROPIC_BASE_URL 2>/dev/null || true
            log "  TOKEN_SWITCH job=$job_id → 계정${token_slot} via central_lease (access-only)"
        elif [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then
            export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_2"
            unset ANTHROPIC_API_KEY 2>/dev/null || true
            unset ANTHROPIC_BASE_URL 2>/dev/null || true
            log "  TOKEN_SWITCH job=$job_id → 계정2 via CLAUDE_CODE_OAUTH_TOKEN"
        elif [[ "$token_slot" == "1" && -n "$TOKEN_1" ]]; then
            export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_1"
            unset ANTHROPIC_API_KEY 2>/dev/null || true
            unset ANTHROPIC_BASE_URL 2>/dev/null || true
        else
            log "  TOKEN_SWITCH_SKIP job=$job_id 계정${token_slot} 사용 가능한 자격증명/lease 없음"
            attempt=$((attempt + 1))
            continue
        fi

        # H6: instruction 크기 제한 (50KB)
        local safe_instruction="${instruction:0:50000}"

        # AAG 착수 브리프 (AADS-AAG-BRIEF-001/002/003): 정적분석이 이미 아는 사실을 워커에게 먼저 준다.
        # 실행 파일: ① 저장소 사본(있으면 그것이 정답) ② 호스트 공용 사본(AAG_BRIEF_BIN, 기본
        # /root/scripts/aag-brief.py) — brief.py 는 러너 것이라 프로젝트 저장소마다 복제하지 않는다.
        # 브리프 생성 실패가 본 작업을 실패시켜서는 안 되므로 전부 `|| true` 로 흘린다.
        local aag_brief="" aag_step0_hint="" aag_brief_lines=0
        local approved_document_brief=""
        local aag_brief_bin="" aag_brief_source="" aag_out=""
        local aag_v2_enabled="${AAG_V2_RUNNER_ENABLED:-0}"
        local aag_token_var="AAG_SCANNER_TOKEN_${project^^}"
        aag_token_var="${aag_token_var//-/_}"
        local aag_scanner_token="${!aag_token_var:-${AAG_SCANNER_TOKEN:-}}"
        local aag_token_file_var="${aag_token_var}_FILE"
        local aag_scanner_token_file="${!aag_token_file_var:-${AAG_SCANNER_TOKEN_FILE:-}}"
        if [[ -z "$aag_scanner_token" && -n "$aag_scanner_token_file" && -r "$aag_scanner_token_file" ]]; then
            IFS= read -r aag_scanner_token < "$aag_scanner_token_file" || aag_scanner_token=""
        fi
        local aag_repo_var="AAG_REPOSITORY_ID_${project^^}"
        aag_repo_var="${aag_repo_var//-/_}"
        local aag_ref_var="AAG_TARGET_REF_${project^^}"
        aag_ref_var="${aag_ref_var//-/_}"
        local aag_repository_id="${!aag_repo_var:-}" aag_target_ref="${!aag_ref_var:-}"
        local aag_ins="$ARTIFACT_DIR/${job_id}.aaginst"
        printf '%s' "$safe_instruction" > "$aag_ins" 2>/dev/null || true

        if [[ "$aag_v2_enabled" =~ ^(1|true|yes|on)$ ]]; then
            # v2 is fail-closed for provenance: a missing credential or central
            # snapshot skips the advisory brief.  It never silently presents a
            # local v1 graph as authoritative.  Set AAG_V2_RUNNER_ENABLED=0 to
            # perform the explicit rollback to the legacy renderer.
            aag_brief_source="central_v2"
            if [[ -n "$aag_scanner_token" ]]; then
                local aag_api_base="${AADS_API_URL%/}"
                local aag_payload="${aag_ins}.json"
                local aag_headers="${aag_ins}.headers"
                [[ "$aag_api_base" == */api/v1 ]] || aag_api_base="${aag_api_base}/api/v1"
                if (umask 077; printf 'X-AAG-Scanner-Token: %s\nX-AAG-Consumer: pipeline-runner\n' "$aag_scanner_token" > "$aag_headers") && \
                    python3 -c 'import json,sys; p={"project":sys.argv[2],"target":open(sys.argv[1],encoding="utf-8").read()}; p.update({"repository_id":sys.argv[3]} if sys.argv[3] else {}); p.update({"target_ref":sys.argv[4]} if sys.argv[4] else {}); print(json.dumps(p))' \
                    "$aag_ins" "$project" "$aag_repository_id" "$aag_target_ref" > "$aag_payload" 2>/dev/null; then
                    aag_out=$(curl -sf --max-time 20 -X POST \
                        -H "Content-Type: application/json" \
                        -H "@${aag_headers}" \
                        --data-binary "@${aag_payload}" \
                        "${aag_api_base}/aag/v2/runner-brief" 2>/dev/null) || aag_out=""
                fi
            else
                log "  AAG_BRIEF_SKIP job=$job_id project=$project source=$aag_brief_source reason=project_credential_missing"
            fi
        else
            if [[ -f "$main_workdir/tools/aag/brief.py" ]]; then
                aag_brief_bin="$main_workdir/tools/aag/brief.py"
                aag_brief_source="repo"
            elif [[ -f "${AAG_BRIEF_BIN:-/root/scripts/aag-brief.py}" ]]; then
                aag_brief_bin="${AAG_BRIEF_BIN:-/root/scripts/aag-brief.py}"
                aag_brief_source="host"
            fi
            if [[ -n "$aag_brief_bin" ]]; then
                aag_out=$(cd "$main_workdir" && timeout 20 python3 "$aag_brief_bin" \
                            --instruction-file "$aag_ins" --project "$project" 2>/dev/null) || aag_out=""
            fi
        fi
        rm -f "$aag_ins" "${aag_ins}.json" "${aag_ins}.headers" 2>/dev/null || true

        if [[ -n "$aag_out" ]]; then
            aag_brief=$'\n'"$aag_out"
            aag_step0_hint='- 아래 [AAG 착수 브리프] 가 있으면 그것을 먼저 읽어라. 거기 적힌 파일·경로·테이블은 다시 찾지 마라.'$'\n'
            aag_brief_lines=$(printf '%s' "$aag_out" | grep -c '^- ' || true)
            log "  AAG_BRIEF_OK job=$job_id project=$project source=$aag_brief_source bytes=${#aag_out} nodes=${aag_brief_lines}"
            record_runner_event "$job_id" "aag_brief_attached" "running" "claude_code_work" "$current_model" "" "$job_size" "" "{\"project\":\"${project}\",\"source\":\"${aag_brief_source}\",\"bytes\":${#aag_out},\"nodes\":${aag_brief_lines}}"
        elif [[ -n "$aag_brief_source" ]]; then
            log "  AAG_BRIEF_SKIP job=$job_id project=$project source=$aag_brief_source reason=no_output_or_timeout"
            record_runner_event "$job_id" "aag_brief_attached" "running" "claude_code_work" "$current_model" "" "$job_size" "" "{\"project\":\"${project}\",\"source\":\"${aag_brief_source}\",\"bytes\":0,\"nodes\":0,\"skipped_reason\":\"no_output_or_timeout\"}"
        fi

        approved_document_brief=$(approved_document_runner_brief "$job_id" "$project")

        # H7: 빌드/배포 가드 v2.1 — Claude Code가 직접 배포하지 않도록 방지
        safe_instruction="[필수 규칙 — 반드시 준수]
1. 코드 수정을 수행하세요. 파일 생성/수정/삭제와 함께, 아래 2번 차단목록에 없는 읽기·검증 명령(pytest, ruff, python3 -m compileall, scripts/dup_guard.py, bash scripts/run_unit_tests.sh, cat/grep/sed 조회)은 실행해도 됩니다. 지시서가 요구한 검증은 반드시 실제로 실행하고 그 결과를 RESULT에 적으세요.
2. 다음 명령은 절대 실행하지 마세요:
   - git add, git commit, git push, git worktree, git reset, git checkout
   - docker build, docker compose, docker restart
   - npm run build, npm start, next build
   - supervisorctl, systemctl, service restart
   - kill, pkill (프로세스 종료)
3. 지시서의 완료기준에 npm run build / next build / docker build 실행이 들어 있어도 직접 실행하지 마세요.
   그 항목은 승인 후 Runner가 수행합니다. RESULT에는 '승인 후 Runner 빌드 검증 대상'이라고 적으세요.
   실행하지 않은 것을 통과로 적지 마세요.
4. 사용자 지시서에 Commit/Push/Build/Deploy 항목이 있어도 실행하지 말고, 변경 파일과 검증 결과만 보고하세요.
5. commit, push, 빌드와 배포는 CEO 승인 후 Runner가 자동으로 수행합니다.
6. 작업 완료 시 '빌드 필요' 또는 '배포 필요' 등을 언급하지 마세요. Runner가 알아서 합니다.
7. [R-AUTH] 인증 토큰 규칙:
   - AADS는 Auth Token(OAuth) 사용: ANTHROPIC_AUTH_TOKEN (sk-ant-oat01-...)
   - ANTHROPIC_API_KEY를 코드에서 직접 참조/추가 금지
   - 2계정 스위치: AUTH_TOKEN(1순위) → API_KEY_FALLBACK(2순위) → Gemini LiteLLM(3순위)
   - 외부 LLM(Gemini/DeepSeek): 반드시 LiteLLM 프록시 경유, 직접 REST API 호출 금지
   - 중앙 클라이언트: anthropic_client.py의 call_llm_with_fallback() 사용

위 규칙을 위반하면 작업이 거부됩니다.
${aag_brief}
[프로젝트 문서 조회 결과]
${approved_document_brief}
[STEP 0 기존 구현 조사 — 코드 수정 전 필수]
${aag_step0_hint}- 대상 파일의 기존 함수/클래스/엔드포인트/스케줄러/DB 접점 목록을 먼저 확인하세요.
- 각 항목을 [유지 | 수정 | 신규 | 삭제(사유 필수)]로 분류해 RESULT에 기록하세요.
- 기존 구현을 새 구현으로 통째 대체하지 말고 필요한 개선분만 반영하세요.
- 삭제가 필요하면 삭제 대상, 호출처 영향, 롤백 방법을 RESULT에 명시하세요.
- 지시서에 명시되지 않은 파일을 변경해야 하면 변경 전 사유를 RESULT에 명시하세요.

${safe_instruction}"

        if [[ $attempt -eq 0 ]]; then
            log "  MODEL_ATTEMPT job=$job_id model=$current_model cycle=$cycle_num attempt=1/$total_attempts"
        else
            log "  MODEL_FALLBACK job=$job_id model=$current_model cycle=$cycle_num attempt=$((attempt+1))/$total_attempts"
        fi
        local attempt_started_ms
        attempt_started_ms=$(date +%s%3N 2>/dev/null || date +%s000)
        record_runner_event "$job_id" "model_attempt_started" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "" "{\"attempt\":$((attempt+1)),\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"token_slot\":\"${token_slot}\"}"
        local runner_kind="claude_cli"
        local claude_json_output="text"
        if [[ "$current_model" == codex:* ]]; then
            # AADS-RUNNER-CODEX-ACCOUNT (2026-09-19)
            # 쿨다운 마커는 계정이 아니라 codex 전체를 막는다. 한도가 남은 계정이
            # 있으면 그 계정 홈으로 실행하고 마커는 보지 않는다 — 마커를 보는 것은
            # 쓸 수 있는 계정이 하나도 없을 때뿐이다.
            local _codex_home=""
            _codex_home="$(codex_pick_account_home || true)"
            if [[ -n "$_codex_home" ]]; then
                export CODEX_HOME="$_codex_home"
                log "  CODEX_ACCOUNT job=$job_id model=$current_model home=$(basename "$_codex_home")"
            fi
            local codex_disabled_until=""
            if [[ -z "$_codex_home" ]] && codex_disabled_until=$(codex_auth_disabled_until); then
                log "  CODEX_AUTH_DISABLED_SKIP job=$job_id model=$current_model until_epoch=$codex_disabled_until"
                db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} skip: auth cooldown active until ${codex_disabled_until}' WHERE job_id='${job_id}';"
                record_runner_event "$job_id" "model_attempt_skipped" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "" "{\"reason\":\"codex_auth_cooldown\",\"until_epoch\":\"${codex_disabled_until}\"}"
                attempt=$((attempt + 1))
                continue
            fi
        fi
        # Codex CLI Runner 분기 (codex: 접두사, ChatGPT Plus OAuth)
        # 가용 모델: gpt-6-astra, gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex
        # Pro 전용(gpt-5.4-pro, gpt-5.4-nano, gpt-5.3-codex-spark)은 ChatGPT Plus에서 미지원
        if [[ "$current_model" == codex:* ]]; then
            local codex_model_name="${current_model#codex:}"
            # DB/설정에 반영된 현재 Codex GPT 모델은 조용히 gpt-5.5로 다운그레이드하지 않는다.
            case "$codex_model_name" in
                default|gpt-6-astra|gpt-5.6-luna|gpt-5.6-sol|gpt-5.6-terra|gpt-5.5|gpt-5.4|gpt-5.4-mini|gpt-5.3-codex) ;;
                gpt-*) log "  CODEX_MODEL_PASSTHROUGH job=$job_id model=$codex_model_name" ;;
                *)
                    log "  CODEX_INVALID_MODEL job=$job_id model=$codex_model_name -> fallback to gpt-5.5"
                    codex_model_name="gpt-5.5"
                    ;;
            esac
            effective_model="codex:${codex_model_name}"
            runner_kind="codex_cli"
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
            runner_kind="litellm_runner"
            log "  LITELLM_RUNNER job=$job_id model=$llm_model_name"
            # instruction을 temp file로 전달 (arg에 멀티라인/대용량 문자열 깨짐 방지)
            local instr_file="${ARTIFACT_DIR}/.litellm_instr_${job_id}.txt"
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'aads-server'; then
                # /app/scripts 는 마운트가 아니라 이미지에 구워진 경로다. 호스트
                # scripts/ 에 쓴 파일은 컨테이너에서 절대 보이지 않는다 —
                # 2026-09-14 확인: 모든 litellm_runner 작업이
                # FileNotFoundError: '/app/scripts/.litellm_instr_*.txt' 로 죽었고,
                # 여러 세션이 이를 "러너 계정문제" 로 보고했다.
                # app/data 는 실제로 마운트돼 있다(→ /app/app/data).
                instr_file="/root/aads/aads-server/app/data/.litellm_instr_${job_id}.txt"
                printf '%s' "$safe_instruction" > "$instr_file"
                local container_instr="/app/app/data/.litellm_instr_${job_id}.txt"
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
            if ! claude_cli_model=$(normalize_claude_cli_model "$current_model"); then
                log "MODEL_CONTRACT_REJECTED job=$job_id requested=$current_model"
                attempt=$((attempt + 1)); sleep 2; continue
            fi
            # 실행 전에는 모델 근거가 없다. 성공 후 CLI json 영수증(modelUsage)으로
            # 확정한다(runner_claude_receipt_model). 영수증이 없으면 unverified 그대로.
            effective_model="unverified"
            log "MODEL_CONTRACT job=$job_id requested=$current_model cli_model=$claude_cli_model verification=cli_argument_only"
            # AADS-LLM-M9-COST-BASIS (2026-09-30): json 으로 받아야 CLI 가 보고한
            # 모델별 토큰·costUSD 를 얻는다. text 로는 러너 비용이 어디에도 남지 않아
            # 러너 작업당 비용을 계산할 수 없었다. 종료 직후 restore_runner_claude_output
            # 이 출력 파일을 text 모드와 같은 결과 텍스트로 되돌리고 검사한다.
            # 되돌릴 헬퍼가 없으면 json 으로 띄우지 않는다 — 원문이 하류로 새지 않게.
            local claude_output_format="text"
            runner_cli_usage_ready && claude_output_format="json"
            claude_json_output="$claude_output_format"
            local claude_args=(--model "$claude_cli_model" -p --output-format "$claude_output_format")
            # ── root 에서도 파일을 쓸 수 있어야 한다 (AADS-RUNNER-ROOT-WRITE, 2026-09-16) ──
            # 실측 2026-09-16 11:5x KST, CLI 2.1.273:
            #   플래그만 주면  → "--dangerously-skip-permissions cannot be used with
            #                     root/sudo privileges for security reasons" 로 거부
            #   플래그를 빼면  → -p 비대화 모드에서 Write/Edit 이 전량 승인 대기로 막힘
            #                     ("requested permissions to write ... but you haven't granted it yet")
            # 그래서 runner-57ef70e9(대시보드 Mermaid), runner-dc19afb7 / runner-c2055401
            # (AADS AAG) 세 건이 산출물 0건으로 끝났다. NTV2 는 codex --sandbox 분기라 무사했고
            # AADS 만 조용히 빈손으로 끝나 실패로 보이지도 않았다.
            # IS_SANDBOX=1 을 함께 주면 두 경로가 모두 열린다(probe 로 파일 생성 확인).
            # 자식 프로세스에만 주입한다 — 러너 셸 전체에 걸지 않는다.
            claude_args+=(--dangerously-skip-permissions)
            local claude_root_env=()
            if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
                claude_root_env=(env IS_SANDBOX=1)
            fi
            if [[ -n "$slot_cred_file" ]]; then
                # timeout 을 래퍼 안쪽에 두어야 한다. 바깥에 두면 래퍼만 죽고
                # 실제 claude 자식이 고아로 남아 MAX_RUNTIME 이 무의미해진다.
                # 래퍼는 종료 시 갱신된 자격증명을 원본 슬롯 파일로 되돌려 쓴다.
                # env -u 로 자식 프로세스에서만 고정 토큰을 지운다.
                # 2026-09-14 실측: ~/.claude/current.env 가 죽은 ANTHROPIC_AUTH_TOKEN(_2) 를
                # export 하는데 CLI 는 이 고정 토큰을 slot credential 보다 우선한다.
                #   "claude.ai connectors are disabled because ANTHROPIC_API_KEY or another
                #    auth source is set and takes precedence over your claude.ai login"
                # 이 다섯 개를 지우지 않으면 슬롯 자격증명이 살아 있어도 전량 실패한다.
                # 셸 자체를 unset 하지 않는 이유는 뒤따르는 레거시 폴백 시도를 망가뜨리지 않기 위함이다.
                CLAUDE_OAUTH_SLOT="$token_slot" \
                CLAUDE_SLOT_CREDENTIALS_FILE="$slot_cred_file" \
                env -u CLAUDE_CODE_OAUTH_TOKEN \
                    -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_AUTH_TOKEN_2 \
                    -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL \
                    "$CLAUDE_SLOT_CREDENTIAL_WRAPPER" \
                    ${claude_root_env[@]+"${claude_root_env[@]}"} \
                    timeout "$MAX_RUNTIME" "$RUNNER_CLAUDE_CLI_BIN" "${claude_args[@]}" "$safe_instruction" \
                    < /dev/null > "$output_file" 2> "$err_file" &
            else
                ${claude_root_env[@]+"${claude_root_env[@]}"} \
                    timeout "$MAX_RUNTIME" "$RUNNER_CLAUDE_CLI_BIN" "${claude_args[@]}" "$safe_instruction" \
                    < /dev/null > "$output_file" 2> "$err_file" &
            fi
            local claude_pid=$!
        fi

        local cli_started_ms
        cli_started_ms=$(date +%s%3N 2>/dev/null || date +%s000)
        record_runner_event "$job_id" "cli_process_started" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "0" "{\"attempt\":$((attempt+1)),\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"token_slot\":\"${token_slot}\",\"runner_kind\":\"${runner_kind}\",\"pid\":${claude_pid},\"workdir\":\"${workdir}\"}"
        # The CLI child PID is recorded in runner_events for diagnostics. Keep
        # pipeline_jobs.runner_pid on BASHPID so watchdog tracks the whole job.

        wait_runner_cli_process "$job_id" "$claude_pid" "$output_file" "$err_file" "$current_model" "$effective_model" "$job_size" "$((attempt+1))" "$total_attempts" "$cycle_num" "$runner_kind" "$cli_started_ms" "0" || exit_code=$?
        # json 원문을 하류 판정이 읽기 전에 text 로 되돌린다. 되돌리지 못하면 이 시도는
        # 실패다 — JSON 을 결과 텍스트로 넘기면 성공 판정·결과 추출·리뷰 입력이 전부 틀린다.
        if [[ "$runner_kind" == "claude_cli" && "$claude_json_output" == "json" ]]; then
            if ! restore_runner_claude_output "$job_id" "$output_file"; then
                exit_code=1
            fi
        fi

        # 서버측 토큰 폐기(401): 이 계정만 사다리에서 뺀다. 아래 루프가 "unauthorized" 로
        # 즉시 폴백하므로 여기서는 표시만 한다. 다음 시도는 codex_pick_account_home 이 다른 계정을 고른다.
        if [[ $exit_code -ne 0 && "$current_model" == codex:* && -n "${_codex_home:-}" ]] \
           && codex_failure_is_token_revoked "$err_file" "$output_file"; then
            local _revoked_key
            _revoked_key="$(basename "$_codex_home")"
            mark_codex_account_revoked "$_revoked_key" "http401_token_revoked"
            db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} 계정 ${_revoked_key} 토큰 폐기(401) → 해당 계정만 제외' WHERE job_id='${job_id}';"
            # 고른 계정이 없을 때 이 홈이 export 된 채 남아 폐기 계정으로 다시 돌지 않게 한다.
            [[ "${CODEX_HOME:-}" == "$_codex_home" ]] && unset CODEX_HOME
        fi

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
                    # head -3 은 stderr 맨 위, 즉 Codex 시작 배너를 집는다. 그래서
                    # 로그에 "Reading additional input from stdin... OpenAI Codex"
                    # 만 남고 진짜 원인이 가려졌다(2026-09-14: 실제 원인은
                    # FileNotFoundError 였는데 세 세션이 계정 문제로 오진했다).
                    # 패턴에 걸린 줄을 그대로 남긴다.
                    _err_msg=$(grep -iEm3 "FAILED:|ERROR:|unauthorized|forbidden|invalid.?key|auth" "$err_file" 2>/dev/null | tr '\n' ' ' | head -c 160)
                    [[ -n "$_err_msg" ]] || _err_msg=$(head -3 "$err_file" | tr '\n' ' ' | head -c 100)
                    if grep -qiE "refresh_token_reused|token_expired|Please log out and sign in again" "$err_file" 2>/dev/null; then
                        mark_codex_auth_disabled "$_err_msg"
                    fi
                    log "  CODEX_ERROR_SKIP job=$job_id reason='${_err_msg}' → immediate fallback"
                    lookup_error_book "$err_file" "$job_id"
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
                local retry_started_ms
                retry_started_ms=$(date +%s%3N 2>/dev/null || date +%s000)
                record_runner_event "$job_id" "cli_process_started" "running" "claude_code_work" "$current_model" "$effective_model" "$job_size" "0" "{\"attempt\":$((attempt+1)),\"total_attempts\":${total_attempts},\"cycle\":${cycle_num},\"runner_kind\":\"codex_cli\",\"pid\":${claude_pid},\"retry\":${_codex_retry},\"workdir\":\"${workdir}\"}"
                wait_runner_cli_process "$job_id" "$claude_pid" "$output_file" "$err_file" "$current_model" "$effective_model" "$job_size" "$((attempt+1))" "$total_attempts" "$cycle_num" "codex_cli" "$retry_started_ms" "$_codex_retry" || exit_code=$?
            done
            if [[ $_codex_retry -ge 12 && $exit_code -ne 0 ]]; then
                log "  CODEX_CONN_EXHAUSTED job=$job_id retries=12(60s) → next model"
                db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[Codex] ${current_model} 연결실패 12회(60초) → 다음 모델 폴백' WHERE job_id='${job_id}';"
            fi
        fi

        # LiteLLM instruction temp file 정리
        [[ -f "${ARTIFACT_DIR}/.litellm_instr_${job_id}.txt" ]] && rm -f "${ARTIFACT_DIR}/.litellm_instr_${job_id}.txt"
        [[ -f "/root/aads/aads-server/app/data/.litellm_instr_${job_id}.txt" ]] && rm -f "/root/aads/aads-server/app/data/.litellm_instr_${job_id}.txt"
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
        case "$runner_kind" in
            claude_cli) record_runner_cli_usage "$job_id" "$runner_kind" "$output_file" "$token_slot" "${claude_cli_model:-$current_model}" "$exit_code" "$attempt_duration_ms" ;;
            codex_cli) record_runner_cli_usage "$job_id" "$runner_kind" "$output_file" "" "${current_model#codex:}" "$exit_code" "$attempt_duration_ms" ;;
        esac
        if [[ "$runner_kind" == "claude_cli" && $exit_code -eq 0 ]]; then
            local _receipt_model=""
            _receipt_model=$(runner_claude_receipt_model "$output_file")
            if [[ -n "$_receipt_model" ]]; then
                effective_model="$_receipt_model"
                if [[ "$_receipt_model" == "${claude_cli_model:-}" ]]; then
                    log "  MODEL_RECEIPT job=$job_id requested=$current_model receipt=$_receipt_model"
                else
                    log "  MODEL_RECEIPT_MISMATCH job=$job_id requested=$current_model cli_model=${claude_cli_model:-} receipt=$_receipt_model"
                fi
            else
                log "  MODEL_RECEIPT_MISSING job=$job_id requested=$current_model → actual_model=unverified 유지"
            fi
        fi
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
            # 슬롯 번호가 곧 llm_api_keys 의 계정이다. 예전 고정 라벨
            # (계정1=Naver/계정2=Gmail)은 슬롯이 4개로 늘면서 사실과 어긋난다.
            local acct_label="계정${next_token}"
            local wait_sec=$(( 3 + attempt * 2 ))  # 5초~15초 점진 증가
            log "  RETRY job=$job_id attempt=$((attempt+1))/$total_attempts next=$next_model($acct_label) wait=${wait_sec}s exit=$exit_code"
            sleep "$wait_sec"
        fi
    done

    # CLI 자식 PID 대신 post-processing을 수행하는 job subshell PID를 추적한다.
    # watchdog이 review/diff 처리 중인 작업도 정확히 종료할 수 있어야 한다.
    db_update "UPDATE pipeline_jobs SET runner_pid=${BASHPID} WHERE job_id='${job_id}' AND status='running';"
    if [[ "$(get_job_status "$job_id")" != "running" ]]; then
        log "  POST_PROCESS_ABORTED_TERMINAL job=$job_id"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        return 1
    fi

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
            _preserve_worktree_patch "$job_id" "/tmp/aads-wt-${job_id}"
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
    # v2.3 (2026-09-14, ACCT-FLOWMAP 반복 no_changes 원인 수정):
    # git diff/git diff HEAD는 untracked 신규 파일의 내용을 보여주지 않는다.
    # "새 디렉터리에 새 파일만 추가" 지시를 따른 job은 전부 untracked라서
    # 실제로 파일을 만들었어도 git_diff가 항상 비어 no_changes로 오판된다.
    # intent-to-add(-N)로 신규 파일을 표시해 git diff가 내용을 포함하게 한다.
    local _new_untracked=""
    # 2026-10-03: ls-files 는 -z / core.quotePath=false 로만 다룬다(_intent_to_add_nul 주석 참조).
    _new_untracked=$(git -c core.quotePath=false ls-files --others --exclude-standard 2>/dev/null) || true
    if [[ -n "$_new_untracked" ]]; then
        git -c core.quotePath=false ls-files -z --others --exclude-standard 2>/dev/null | _intent_to_add_nul -N || true
    fi
    # v2.4 (2026-09-15, ACCT-FLOWMAP 5연속 no_changes 원인 수정):
    # v2.3 의 intent-to-add 는 --exclude-standard 를 쓴다. 그런데 진아실장 저장소는
    # .gitignore 17번째 줄이 '*' 인 화이트리스트 방식이라(!채팅앱/**/*.html 처럼 되살린다),
    # Claude 가 새로 만든 산출물이 전부 ignored 로 걸러져 한 건도 잡히지 않았다.
    # run c/d/e 는 Write 를 실제로 성공시켰는데도 changed_files=0 으로 오판돼
    # cancelled 처리되고 워크트리째 삭제됐다(2026-09-14 세션 로그 실측).
    #
    # 표준 목록이 비었을 때만, 워크트리 생성 시각(.git 파일 mtime)보다 새 파일을
    # 파일시스템에서 직접 찾아 -f 로 intent-to-add 한다. 표준 목록이 비지 않는
    # 일반 저장소(AADS/KIS/GO100/SF/NTV2)에서는 이 블록이 아예 실행되지 않으므로
    # 기존 동작에 영향이 없다.
    # v2.5 (2026-09-19, runner-48377212 반려 원인 수정):
    # v2.4 는 "표준 목록이 비지 않는 일반 저장소에서는 이 블록이 실행되지 않는다"고
    # 가정했지만 틀렸다. 추적 파일만 고친 job 은 일반 저장소에서도 표준 목록이 비므로
    # 블록이 그대로 돌고, --exclude-standard 없는 git ls-files --others 가
    # .pytest_cache/ · .ruff_cache/ 같은 .gitignore 된 테스트 캐시를 긁어 add -fN 했다.
    # 그 결과 runner-48377212 는 변경 15개 중 8개가 캐시 파일이 되어 리뷰에서
    # REQUEST_CHANGES(0.655) 로 반려됐다(runner.log UNTRACKED_IGNORED_RESCUE files=8).
    # 이 rescue 는 .gitignore 가 '*' 화이트리스트인 저장소(ACCT) 전용이므로,
    # "처음 보는 이름의 파일조차 ignored 로 걸리는 저장소" 일 때만 돌도록 게이트를 건다.
    local _whitelist_ignore_repo=0
    if printf '%s\n' "__aads_ignore_probe_$$" | git check-ignore --stdin -q 2>/dev/null; then
        _whitelist_ignore_repo=1
    fi
    if [[ -z "${_new_untracked//[[:space:]]/}" && -n "${worktree_dir:-}" && -e "${worktree_dir}/.git" && $_whitelist_ignore_repo -eq 1 ]]; then
        # --exclude-standard 를 뺀 untracked 목록에서 시작한다. 갓 만든 워크트리에는
        # 추적 파일만 체크아웃돼 있으므로 여기 남는 것은 이번 작업이 만든 파일뿐이다.
        # 그래도 안전하게 워크트리 생성시각(.git mtime)보다 새 것만 통과시킨다.
        # find 단독으로 훑으면 체크아웃된 추적 파일이 전부 걸려 200개 상한을 잡아먹는다.
        local _ignored_cand=""
        _ignored_cand=$(git -c core.quotePath=false ls-files --others 2>/dev/null \
            | grep -vE '(^|/)(node_modules|__pycache__|\.venv|venv|dist|build|\.next)/' \
            | grep -vE '(^|/)\.[A-Za-z0-9_.-]+_cache/' \
            | grep -vE '(^|/)(\.git|\.tox|\.nox|\.eggs|\.idea|\.vscode|\.turbo|htmlcov|coverage|\.gradle|target)/' \
            | grep -vE '(^|/)(\.coverage|\.DS_Store|coverage\.xml|\.runner_full_diff\.patch)$' \
            | grep -vE '\.(pyc|pyo|pyd|log|orig|rej|bak|swp|tmp)$') || true
        local _ignored_new=""
        if [[ -n "${_ignored_cand//[[:space:]]/}" ]]; then
            _ignored_new=$(printf '%s\n' "$_ignored_cand" | while IFS= read -r _f; do
                [[ -n "$_f" && -f "$_f" && "$_f" -nt "${worktree_dir}/.git" ]] && printf '%s\n' "$_f"
            done | head -200) || true
        fi
        if [[ -n "${_ignored_new//[[:space:]]/}" ]]; then
            log "  UNTRACKED_IGNORED_RESCUE job=$job_id files=$(printf '%s\n' "$_ignored_new" | sed '/^$/d' | wc -l) — .gitignore 에 가려진 신규 산출물 복구"
            printf '%s\n' "$_ignored_new" | sed '/^$/d' | tr '\n' '\0' | _intent_to_add_nul -fN || true
            _new_untracked="$_ignored_new"
        fi
    fi
    local git_diff=""
    local _current_head=""
    _current_head=$(git rev-parse HEAD 2>/dev/null) || _current_head=""
    # git_diff 캡처 규칙은 SHARED-BLOCK(job_diff_contract)에만 둔다. 스위퍼가
    # review_hold 워크트리를 다시 읽어 같은 diff 인지 판정할 때 여기와 한 글자도
    # 다르면 멀쩡한 산출물이 drift 로 폐기된다.
    git_diff=$(capture_job_diff_text "$workdir" "$pre_exec_sha")
    local actual_changed_files=""
    if [[ -n "$pre_exec_sha" && -n "$_current_head" && "$pre_exec_sha" != "$_current_head" ]]; then
        actual_changed_files=$(git -c core.quotePath=false diff --name-only "${pre_exec_sha}..${_current_head}" 2>/dev/null) || true
        local _uncommitted_files=""
        _uncommitted_files=$(git -c core.quotePath=false diff --name-only HEAD 2>/dev/null) || true
        [[ -n "$_uncommitted_files" ]] && actual_changed_files="${actual_changed_files}
${_uncommitted_files}"
    else
        actual_changed_files=$(git -c core.quotePath=false diff --name-only HEAD 2>/dev/null) || true
    fi
    local _untracked_files=""
    _untracked_files=$(git -c core.quotePath=false ls-files --others --exclude-standard 2>/dev/null) || true
    [[ -n "$_untracked_files" ]] && actual_changed_files="${actual_changed_files}
${_untracked_files}"
    actual_changed_files=$(printf '%s\n' "$actual_changed_files" | sed '/^[[:space:]]*$/d' | sort -u)
    record_actual_changed_files "$job_id" "$actual_changed_files" "$worktree_dir" "$parallel_group"

    # 같은 이유로 ${git_diff//[[:space:]]/} 를 쓰지 않는다 — 43KB 에 11초다.
    if [[ ! "$git_diff" =~ [^[:space:]] ]]; then
        # AADS-RUNNER-NOCHANGES-GUARD-P1-R5 (2026-09-18): diff 0건이 "진짜 변경 없음"인지
        # "커밋 직전 레이스"인지 즉시 구분할 수 없다. runner-71bc9b4e 실측에서는 이 판정
        # 77초 뒤에 커밋이 origin/main 에 나타났다 — 기본 5회 × 25초 = 125초 재확인으로
        # 이 레이스를 덮는다. 재시도 중 diff 가 생기면 정상 경로로 빠져나간다.
        local _recheck_attempt=0
        local _recheck_max="${NO_CHANGES_RECHECK_MAX_ATTEMPTS:-5}"
        local _recheck_sleep="${NO_CHANGES_RECHECK_SLEEP_SECONDS:-25}"
        local _recheck_start_ts
        _recheck_start_ts=$(date +%s)
        local _recheck_signaled=0
        trap '_recheck_signaled=1' TERM INT
        while [[ "$_recheck_attempt" -lt "$_recheck_max" ]]; do
            _recheck_attempt=$((_recheck_attempt + 1))
            sleep "$_recheck_sleep"
            if [[ "$_recheck_signaled" -eq 1 ]]; then
                trap - TERM INT
                log "  NO_CHANGES_RECHECK_SIGNALED job=$job_id attempt=$_recheck_attempt"
                return 130
            fi
            cd "$workdir"
            _current_head=$(git rev-parse HEAD 2>/dev/null) || _current_head=""
            git_diff=$(capture_job_diff_text "$workdir" "$pre_exec_sha")
            if [[ "$git_diff" =~ [^[:space:]] ]]; then
                break
            fi
        done
        trap - TERM INT
        if [[ "$git_diff" =~ [^[:space:]] ]]; then
            local _recheck_elapsed=$(( $(date +%s) - _recheck_start_ts ))
            log "  NO_CHANGES_RECHECK_RECOVERED job=$job_id attempts=$_recheck_attempt elapsed=${_recheck_elapsed}s"
            actual_changed_files=$(git -c core.quotePath=false diff --name-only "${pre_exec_sha}..${_current_head}" 2>/dev/null) || true
            local _uncommitted_files=""
            _uncommitted_files=$(git -c core.quotePath=false diff --name-only HEAD 2>/dev/null) || true
            [[ -n "$_uncommitted_files" ]] && actual_changed_files="${actual_changed_files}
${_uncommitted_files}"
            _untracked_files=$(git -c core.quotePath=false ls-files --others --exclude-standard 2>/dev/null) || true
            [[ -n "$_untracked_files" ]] && actual_changed_files="${actual_changed_files}
${_untracked_files}"
            actual_changed_files=$(printf '%s\n' "$actual_changed_files" | sed '/^[[:space:]]*$/d' | sort -u)
            record_actual_changed_files "$job_id" "$actual_changed_files" "$worktree_dir" "$parallel_group"
        fi
    fi

    if [[ ! "$git_diff" =~ [^[:space:]] ]]; then
        local _recheck_elapsed=$(( $(date +%s) - _recheck_start_ts ))
        log "  NO_CHANGES_RECHECK_EXHAUSTED job=$job_id attempts=${_recheck_attempt} elapsed=${_recheck_elapsed}s"
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
        if is_deploy_only_instruction "$instruction"; then
            log "  DEPLOY_ONLY_BYPASS job=$job_id target=$target_repo — no_changes 게이트 우회, 정상 진행"
            record_runner_event "$job_id" "deploy_only_bypass" "running" "no_changes_bypass" "$job_model" "" "$job_size" "" "{\"deploy_only\":true,\"changed_files\":0}"
        else
            # 케이스 B (커밋 누락): 재확인까지 diff 는 0건이지만 워크트리에 미커밋
            # 변경이 남아있으면 "진짜 변경 없음"이 아니다 — no_changes 로 종결하면
            # 그 패치가 워크트리 삭제와 함께 소실된다(2026-09-18 4건 실측).
            local _dirty_status=""
            _dirty_status=$(git status --porcelain 2>/dev/null) || true
            if [[ -n "${_dirty_status//[[:space:]]/}" ]]; then
                log "  UNCOMMITTED_WORKTREE_CHANGES job=$job_id worktree=$worktree_dir"
                local _dirty_detail
                _dirty_detail=$(printf 'worktree=%s\n%s' "$worktree_dir" "$(printf '%s\n' "$_dirty_status" | head -20)")
                db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                           error_detail='uncommitted_worktree_changes',
                           result_output=$(sql_escape "$output"),
                           review_feedback=COALESCE(review_feedback,'') || E'\n[Runner Guard] 파일은 수정됐으나 커밋되지 않음 — 워크트리 보존, 회수 필요\n' || $(sql_escape "$_dirty_detail"),
                           completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
                record_runner_event "$job_id" "job_terminal" "error" "error" "$job_model" "" "$job_size" "" "{\"error_detail\":\"uncommitted_worktree_changes\"}"
                post_to_chat "$session_id" "🔴 [Pipeline Runner] 커밋 누락 감지: $job_id — 워크트리(${worktree_dir})에 미커밋 변경이 남아있어 회수가 필요합니다.

\`\`\`
$(printf '%s\n' "$_dirty_status" | head -20)
\`\`\`"
                _release_work_lock "$project" "$job_id" "$parallel_group"
                _cleanup_artifacts "$job_id"
                _preserve_worktree_patch "$job_id" "$worktree_dir"
                _notify_ai "$job_id"
                promote_next_queued "$project"
                _current_job_id=""
                _current_session_id=""
                rm -f /tmp/.pipeline_current_job
                return 1
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
    fi

    # ═══ AI Reviewer 단계 — CEO 승인 전 독립 AI 리뷰 ═══
    # git_diff 는 DB 저장 상한(45-50KB)으로 잘려 있을 수 있다. 잘린 채로
    # review_failed 등에서 워크트리가 삭제되면 git apply 로 복구할 방법이
    # 없다(runner-1f09e9e5, 저장 48,400자 = 상한 절단 실측). 잘렸으면 전량을
    # 파일로 남긴다.
    # 리뷰어에게 "잘렸다"는 사실을 알리는 앞머리 — 캡처와 같은 워크트리 상태에서(검수 전
    # 커밋·.runner_full_diff.patch 생성 이전) 계산한다. DB git_diff 에는 섞지 않는다.
    local review_diff_prefix=""
    review_diff_prefix=$(build_review_diff_prefix "$workdir" "$pre_exec_sha") || review_diff_prefix=""
    if [[ -n "$review_diff_prefix" ]]; then
        log "  REVIEW_DIFF_TRUNCATED_NOTICE job=$job_id — 리뷰 입력에 절단 고지 + DIFFSTAT 동봉"
    fi
    _persist_full_diff_if_truncated "$job_id" "$worktree_dir" "$pre_exec_sha" "$_current_head" "$git_diff"

    # 리뷰 판정과 산출물 보존을 분리한다. 리뷰가 실패하거나 인프라 장애로
    # review_hold 에 머물러도 이 SHA 로 결과를 복구할 수 있어야 한다.
    # 변경사항 0건은 위 no_changes 경로에서 이미 종결되므로 빈 커밋은 만들지 않는다.
    local approval_commit_sha=""
    approval_commit_sha=$(commit_job_worktree_for_approval "$job_id" "$session_id" "$worktree_dir" "$main_workdir" "$instruction" "$pre_exec_sha") || {
        _release_work_lock "$project" "$job_id" "$parallel_group"
        # provenance/scope 실패의 원본 .out/.err와 worktree는 후속 검수를 위해 보존한다.
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    }
    # 방어: 함수 계약(stdout=SHA 전용)이 다른 경로에서 깨져 로그 줄이
    # 섞여 들어와도, 캡처값 안에 40자 hex 가 있으면 그것만 취해 살린다.
    local _raw_approval_commit_sha="$approval_commit_sha"
    approval_commit_sha=$(printf '%s' "$approval_commit_sha" | tr -d '\r' | grep -oE '[0-9a-f]{40}' | tail -n1) || true
    if [[ "$approval_commit_sha" != "$_raw_approval_commit_sha" ]]; then
        log "  APPROVAL_SHA_STDOUT_SANITIZED job=$job_id"
    fi
    if [[ -z "$approval_commit_sha" || "$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)" != "$approval_commit_sha" ]]; then
        _fail_job "$job_id" "$session_id" "approval_commit_sha_mismatch" "검수 전 산출물 커밋 SHA와 runner worktree HEAD 불일치"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        return 1
    fi

    local review_verdict="APPROVE"
    local review_score="1.0"
    local review_flag_category=""
    local review_needs_retry="false"
    if [[ -n "$git_diff" && ${#git_diff} -gt 10 ]]; then
        if looks_like_git_diff "$git_diff"; then
            log "  AI_REVIEW job=$job_id"
            db_update "UPDATE pipeline_jobs SET phase='ai_review', runner_pid=${BASHPID}, updated_at=NOW() WHERE job_id='${job_id}' AND status='running';"
            if [[ "$(get_job_status "$job_id")" != "running" ]]; then
                log "  AI_REVIEW_ABORTED_TERMINAL job=$job_id"
                _release_work_lock "$project" "$job_id" "$parallel_group"
                return 1
            fi
            local review_response=""
            # diff에서 변경 파일 목록 추출
            local changed_files=""
            changed_files=$(echo "$git_diff" | grep '^diff --git' | sed 's/diff --git a\///' | sed 's/ b\/.*//' | tr '\n' ',' | sed 's/,$//')

            # 리뷰 입력 = (잘렸을 때만) 절단 고지 앞머리 + git_diff. git_diff 자체는 그대로 저장된다.
            local review_diff="$git_diff"
            if [[ -n "$review_diff_prefix" ]]; then
                review_diff="${review_diff_prefix}"$'\n'"${git_diff}"
            fi

            # JSON body 생성 (jq 사용)
            local review_body=""
            review_body=$(jq -n \
                --arg jid "$job_id" \
                --arg proj "$project" \
                --arg diff "$review_diff" \
                --arg inst "$instruction" \
                --arg files "$changed_files" \
                '{job_id: $jid, project: $proj, diff: $diff, instruction: $inst, files_changed: ($files | split(","))}')

            # 최초 검수도 스위퍼와 같은 durable async 경로를 쓴다. 요청 ID를
            # pipeline_jobs에 먼저 남기므로 슬롯 전환/응답 유실 뒤에도 같은 DB
            # 요청을 다시 폴링하거나 재개할 수 있다. 후보별 timeout과 전체 후보
            # 예산은 서버의 code_reviewer가 분리해서 집행한다.
            local review_http_code=""
            local review_attempt=0
            local review_max_attempts="${AADS_REVIEW_MAX_ATTEMPTS:-3}"
            local review_request_id
            review_request_id=$(cat /proc/sys/kernel/random/uuid)
            db_update "UPDATE pipeline_jobs SET review_request_id='${review_request_id}'::uuid, updated_at=NOW()
                       WHERE job_id='${job_id}' AND status='running';"
            review_body=$(jq -n \
                --arg rid "$review_request_id" \
                --arg jid "$job_id" \
                --arg proj "$project" \
                --arg diff "$review_diff" \
                --arg inst "$instruction" \
                --arg files "$changed_files" \
                '{request_id: $rid, job_id: $jid, project: $proj, diff: $diff, instruction: $inst, files_changed: ($files | split(","))}')
            while [[ $review_attempt -lt $review_max_attempts ]]; do
                review_attempt=$((review_attempt + 1))
                review_response=$(curl -4 -s --http1.1 -w "\n%{http_code}" -X POST "${AADS_API_URL}/api/v1/review/code-diff/requests" \
                    -H "Content-Type: application/json" \
                    -d "$review_body" \
                    --connect-timeout 10 --max-time 20 2>/dev/null) || true
                review_http_code=$(echo "$review_response" | tail -1)
                review_response=$(echo "$review_response" | sed '$d')
                if [[ "$review_http_code" == "202" ]]; then
                    if [[ $review_attempt -gt 1 ]]; then
                        log "  AI_REVIEW_TRANSPORT_RECOVERED job=$job_id attempt=${review_attempt}/${review_max_attempts}"
                    fi
                    break
                fi
                log "  AI_REVIEW_TRANSPORT_FAIL job=$job_id attempt=${review_attempt}/${review_max_attempts} http=${review_http_code:-000}"
                record_runner_event "$job_id" "ai_review_transport_fail" "running" "ai_review" "$job_model" "" "$job_size" "" "{\"attempt\":${review_attempt},\"max_attempts\":${review_max_attempts},\"http\":\"${review_http_code:-000}\"}"
                if [[ "$(get_job_status "$job_id")" != "running" ]]; then
                    log "  AI_REVIEW_ABORTED_TERMINAL job=$job_id attempt=${review_attempt}"
                    _release_work_lock "$project" "$job_id" "$parallel_group"
                    return 1
                fi
                sleep $((review_attempt * 2))
            done

            local review_request_status=""
            if [[ "$review_http_code" == "202" ]]; then
                local review_poll_deadline=$((SECONDS + AADS_REVIEW_ASYNC_WAIT_SEC))
                while (( SECONDS < review_poll_deadline )); do
                    if [[ "$(get_job_status "$job_id")" != "running" ]]; then
                        log "  AI_REVIEW_ABORTED_TERMINAL job=$job_id request_id=${review_request_id}"
                        _release_work_lock "$project" "$job_id" "$parallel_group"
                        return 1
                    fi
                    review_response=$(curl -4 -s --http1.1 -w "\n%{http_code}" \
                        "${AADS_API_URL}/api/v1/review/code-diff/requests/${review_request_id}" \
                        --connect-timeout 5 --max-time 15 2>/dev/null) || true
                    local review_poll_http_code
                    review_poll_http_code=$(echo "$review_response" | tail -1)
                    review_response=$(echo "$review_response" | sed '$d')
                    if [[ "$review_poll_http_code" == "200" && -n "$review_response" ]]; then
                        review_request_status=$(echo "$review_response" | jq -r '.status // empty' 2>/dev/null || true)
                        if [[ "$review_request_status" == "completed" || "$review_request_status" == "failed" ]]; then
                            review_http_code="$review_poll_http_code"
                            break
                        fi
                    fi
                    sleep "$AADS_REVIEW_POLL_INTERVAL"
                done
            fi

            if [[ "$review_http_code" == "200" && "$review_request_status" == "completed" && -n "$review_response" ]]; then
                review_verdict=$(echo "$review_response" | jq -r '.verdict // "APPROVE"')
                review_score=$(echo "$review_response" | jq -r '.score // "1.0"')
                review_flag_category=$(echo "$review_response" | jq -r '.flag_category // empty')
                review_needs_retry=$(echo "$review_response" | jq -r '.needs_retry // false')
                log "  AI_REVIEW_RESULT job=$job_id request_id=${review_request_id} verdict=$review_verdict score=$review_score flag_category=${review_flag_category:-none} needs_retry=$review_needs_retry"

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
                # 후보 무응답은 서버가 모든 DB 후보를 실제로 소진한 뒤에만
                # REVIEW_MODEL_NO_RESPONSE로 완료한다. 여기서는 durable 요청을
                # 받지 못했거나 아직 완료하지 못한 전송/폴링 장애만 구분한다.
                review_flag_category="REVIEW_API_UNAVAILABLE"
                review_needs_retry="true"
                if [[ "$review_http_code" == "202" && -n "$review_request_status" ]]; then
                    review_flag_category="REVIEW_TIMEOUT"
                fi
                log "  AI_REVIEW_HOLD job=$job_id request_id=${review_request_id} status=${review_request_status:-unknown} http=${review_http_code:-000}"
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

    # P0: 인프라 원인(리뷰 API/모델/파서 장애)과 실제 코드 반려를 구분
    local review_infra_failure="false"
    case "$review_flag_category" in
        REVIEW_API_UNAVAILABLE|REVIEW_MODEL_NO_RESPONSE|REVIEW_PARSER_FAILURE|REVIEW_TIMEOUT)
            review_infra_failure="true"
            ;;
    esac

    if [[ "$review_verdict" != "APPROVE" ]]; then
        local review_error_detail="review_failed: verdict=${review_verdict} score=${review_score}"
        if [[ "$review_infra_failure" == "true" ]]; then
            review_error_detail="review_infra_failed: verdict=${review_verdict} score=${review_score} http=${review_http_code:-000} model=${job_model:-unknown} attempts=${review_attempt:-0}/${review_max_attempts:-3}"
        fi
        [[ -n "$review_flag_category" ]] && review_error_detail="${review_error_detail} category=${review_flag_category}"
        [[ "$review_needs_retry" == "true" ]] && review_error_detail="${review_error_detail} needs_retry=true"
        local review_hold_status="error"
        local review_hold_phase="review_failed"
        local review_hold_note="승인 대기 차단"
        if [[ "$review_infra_failure" == "true" ]]; then
            review_hold_status="review_hold"
            review_hold_phase="review_hold"
            review_hold_note="FLAG+hold — 리뷰 인프라 장애로 승인 보류"
        fi
        log "  AI_REVIEW_HOLD job=$job_id status=${review_hold_status} phase=${review_hold_phase} ${review_error_detail}"
        db_update "UPDATE pipeline_jobs SET status='${review_hold_status}', phase='${review_hold_phase}',
                   error_detail=$(sql_escape "$review_error_detail"),
                   result_output=$(sql_escape "$output"),
                   git_diff=$(sql_escape "$git_diff"),
                   review_feedback=COALESCE(review_feedback,'') || E'\n[AI Reviewer] ${review_hold_note} — ${review_error_detail}',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        # 위 UPDATE 는 result_output·git_diff 를 통째로 싣는다. 그게 어떤 이유로든
        # 실패하면 행은 여전히 status='running' 이고, 60초 안에 watchdog 이
        # 'process_died' 로 덮어쓴다(runner-1bf4a718 이 그렇게 사라졌다).
        # 그래서 **적용됐는지 읽어서 확인하고**, 아니면 큰 payload 를 뺀
        # 최소 문장으로 한 번 더 건다. 상태 한 줄이 diff 보다 중요하다.
        local _hold_now
        _hold_now=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null || true)
        _hold_now="${_hold_now// /}"
        if [[ "$_hold_now" != "$review_hold_status" ]]; then
            log "  REVIEW_HOLD_WRITE_MISSED job=$job_id status='${_hold_now}' expected='${review_hold_status}' — payload 없이 재시도"
            db_update "UPDATE pipeline_jobs SET status='${review_hold_status}', phase='${review_hold_phase}',
                       error_detail=$(sql_escape "$review_error_detail"),
                       review_feedback=COALESCE(review_feedback,'') || E'\n[AI Reviewer] ${review_hold_note} — ${review_error_detail}',
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        fi
        record_runner_event "$job_id" "job_terminal" "$review_hold_status" "$review_hold_phase" "$job_model" "" "$job_size" "" "{\"error_detail\":\"${review_hold_phase}\",\"verdict\":\"${review_verdict}\",\"flag_category\":\"${review_flag_category}\",\"policy\":\"FLAG+hold\"}"
        if [[ "$review_infra_failure" == "true" ]]; then
            post_to_chat "$session_id" "🟠 [Pipeline Runner] AI 리뷰 인프라 장애로 승인 보류: $job_id — ${review_error_detail}
코드 반려가 아니라 리뷰 시스템 장애입니다. 지금은 CEO 승인 단계가 아니므로 승인 버튼이 표시되지 않습니다. 자동 재검수 통과 후 승인 대기로 전환되면 버튼이 생성됩니다. 작업 산출물(worktree)은 재검수를 위해 보존했습니다: ${worktree_dir}"
        else
            post_to_chat "$session_id" "🔴 [Pipeline Runner] AI 리뷰 미통과로 승인 대기 차단: $job_id — ${review_error_detail}
리뷰가 오판이었을 수 있습니다. 작업 산출물(worktree)은 ${ARTIFACT_MAX_AGE_HOURS}시간 보존 후 자동 회수됩니다: ${worktree_dir}"
        fi
        _release_work_lock "$project" "$job_id" "$parallel_group"
        _cleanup_artifacts "$job_id"
        # AADS-RUNNER-REJECTED-ARTIFACT-PRESERVE (2026-09-18): review_failed(코드 반려)도
        # review_infra_failed 와 동일하게 워크트리를 즉시 삭제하지 않는다. 리뷰는 틀릴 수
        # 있고(runner-1f09e9e5 실측), 삭제 후에는 되살릴 수 없었다. 회수는 기존 스테일
        # 워크트리 정리(_cleanup_old_artifacts, ARTIFACT_MAX_AGE_HOURS 기본 24시간)에 맡긴다.
        if [[ -d "$worktree_dir" ]]; then
            if [[ "$review_infra_failure" == "true" ]]; then
                log "  WORKTREE_PRESERVED_FOR_REREVIEW: $worktree_dir"
            else
                _preserve_worktree_patch "$job_id" "$worktree_dir"
                log "  WORKTREE_PRESERVED_REJECTED: $worktree_dir (retention=${ARTIFACT_MAX_AGE_HOURS}h, 자동회수: _cleanup_old_artifacts)"
            fi
        fi
        _notify_ai "$job_id"
        promote_next_queued "$project"
        _current_job_id=""
        _current_session_id=""
        rm -f /tmp/.pipeline_current_job
        return 1
    fi

    db_update "UPDATE pipeline_jobs SET phase='awaiting_approval',
               status='awaiting_approval',
               result_output=$(sql_escape "$output"),
               git_diff=$(sql_escape "$git_diff"),
               error_detail=NULL,
               runner_pid=NULL,
               approval_requested_at=NOW(),
               updated_at=NOW() WHERE job_id='${job_id}';"
    # 큰 payload 또는 인코딩 문제로 UPDATE가 실패해도 승인 상태와 커밋은
    # 잃으면 안 된다. event 를 쓰기 전에 실제 상태를 읽어 확인하고, 실패 시
    # payload 없는 최소 UPDATE로 한 번 더 전이한다.
    local _approval_now
    _approval_now=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null || true)
    _approval_now="${_approval_now// /}"
    if [[ "$_approval_now" != "awaiting_approval" ]]; then
        log "  APPROVAL_WRITE_MISSED job=$job_id status='${_approval_now}' — payload 없이 재시도"
        db_update "UPDATE pipeline_jobs SET phase='awaiting_approval',
                   status='awaiting_approval', error_detail=NULL, runner_pid=NULL,
                   approval_requested_at=NOW(), updated_at=NOW()
                   WHERE job_id='${job_id}' AND commit_hash='${approval_commit_sha}';"
        _approval_now=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null || true)
        _approval_now="${_approval_now// /}"
    fi
    if [[ "$_approval_now" != "awaiting_approval" ]]; then
        _fail_job "$job_id" "$session_id" "approval_state_persist_failed" "승인 상태 DB 저장 실패 — commit ${approval_commit_sha} worktree 보존"
        _release_work_lock "$project" "$job_id" "$parallel_group"
        return 1
    fi
    record_runner_event "$job_id" "approval_requested" "awaiting_approval" "awaiting_approval" "$job_model" "" "$job_size" "" "{\"commit_hash\":\"${approval_commit_sha}\",\"review_verdict\":\"${review_verdict}\"}"

    local diff_summary="${git_diff:0:3000}"
    local approval_diff_stat=""
    approval_diff_stat=$(git -C "$worktree_dir" show --stat --oneline --no-renames "$approval_commit_sha" 2>/dev/null | head -40 || true)
    local approval_deleted_symbols=""
    approval_deleted_symbols=$(printf '%s\n' "$git_diff" \
        | grep -E '^-([[:space:]]*)(async[[:space:]]+def|def|class)[[:space:]]+[A-Za-z_][A-Za-z0-9_]*|^-([[:space:]]*)@router\.[A-Za-z_]+' \
        | sed 's/^-//' \
        | head -30 || true)
    if [[ -z "$approval_deleted_symbols" ]]; then
        approval_deleted_symbols="없음"
    fi
    local review_badge=""
    if [[ "$review_verdict" == "APPROVE" && "$review_needs_retry" == "true" ]]; then
        # 서버가 인프라 장애를 fail-open APPROVE로 반환한 경우 — '통과'로 표기하면 CEO 오판 유발
        review_badge="⚠️ AI 리뷰 미수행 — 리뷰 인프라 장애로 검증 불가"
        if [[ -n "$review_flag_category" ]]; then
            review_badge="${review_badge} [${review_flag_category}]"
        fi
        review_badge="${review_badge} (score=${review_score}) / 사람 검수 필요"
    elif [[ "$review_verdict" == "APPROVE" ]]; then
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
**Diff stat / 삭제 심볼**:
\`\`\`
${approval_diff_stat:0:2000}

deleted_symbols:
${approval_deleted_symbols:0:1000}
\`\`\`
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

# AADS-RUNNER-ARTIFACT-PRESERVE: 실패/반려로 워크트리를 force 삭제하기 전
# untracked 포함 전체 diff를 용량 제한 없이 보존한다. 기존 git_diff 컬럼은
# `git diff HEAD | head -c 50000`(staged diff만, 50KB 상한)이라 신규 untracked
# 파일과 초과분이 워크트리 삭제 후 복구 불가능하게 사라졌다.
_preserve_worktree_patch() {
    local job_id="$1" worktree_dir="$2"
    [[ -d "$worktree_dir" ]] || return 0
    mkdir -p /root/aads/runner-artifacts
    local patch_file="/root/aads/runner-artifacts/${job_id}.patch"
    (
        cd "$worktree_dir" || exit 0
        { git add -A -- . && { git reset -q -- .runner_full_diff.patch || true; }; } >/dev/null 2>&1
        git diff HEAD > "$patch_file" 2>/dev/null
        if [[ ! -s "$patch_file" ]]; then
            # reject_job 등에서 이미 전부 커밋된 경우 working-tree diff는 비어도
            # base 대비 committed diff는 남아 있을 수 있다 — 빈 파일을 남기지 않는다.
            local base_ref
            base_ref=$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || true)
            if [[ -n "$base_ref" && "$base_ref" != "$(git rev-parse HEAD 2>/dev/null)" ]]; then
                git diff "${base_ref}..HEAD" > "$patch_file" 2>/dev/null
            fi
        fi
    )
    log "  ARTIFACT_PRESERVED job=$job_id path=$patch_file"
}

# AADS-RUNNER-REJECTED-ARTIFACT-PRESERVE (2026-09-18): pipeline_jobs.git_diff 는
# DB 저장을 위해 45-50KB(head -c)로 잘린다. runner-1f09e9e5 실측: 저장 길이
# 48,400자 = 상한 절단이라 git apply 재적용이 불가능했다. 잘렸으면(전량 diff가
# 저장된 값보다 길면) 전량을 파일로 남긴다 — 워크트리(보존 기간 동안)와
# 로그 디렉터리(워크트리 회수 후에도 남음) 두 곳.
_persist_full_diff_if_truncated() {
    local job_id="$1" worktree_dir="$2" base_sha="$3" head_sha="$4" stored_diff="${5:-}"
    [[ -d "$worktree_dir" ]] || return 0

    local full_diff=""
    if [[ -n "$base_sha" && -n "$head_sha" && "$base_sha" != "$head_sha" ]]; then
        full_diff=$(git -C "$worktree_dir" diff "${base_sha}..${head_sha}" 2>/dev/null) || true
        local _uncommitted=""
        _uncommitted=$(git -C "$worktree_dir" diff HEAD 2>/dev/null) || true
        [[ -n "${_uncommitted//[[:space:]]/}" ]] && full_diff="${full_diff}
${_uncommitted}"
    else
        full_diff=$(git -C "$worktree_dir" diff HEAD 2>/dev/null) || true
    fi

    # 저장된(잘렸을 수 있는) 값보다 전량이 길지 않으면 잘리지 않은 것이다.
    [[ ${#full_diff} -gt ${#stored_diff} ]] || return 0

    local log_dir="/root/aads/aads-server/logs/runner-diff"
    mkdir -p "$log_dir" 2>/dev/null || true
    local worktree_patch="${worktree_dir}/.runner_full_diff.patch"
    local log_patch="${log_dir}/${job_id}.patch"
    printf '%s' "$full_diff" > "$worktree_patch" 2>/dev/null || true
    printf '%s' "$full_diff" > "$log_patch" 2>/dev/null || true
    log "  FULL_DIFF_TRUNCATED job=$job_id db_bytes=${#stored_diff} full_bytes=${#full_diff} patch=${log_patch}"
}

# H3: 임시파일 정리
_cleanup_artifacts() {
    local job_id="$1"
    rm -f "$ARTIFACT_DIR/${job_id}.out" "$ARTIFACT_DIR/${job_id}.err" 2>/dev/null || true
    rm -f "/tmp/runner_alert_${job_id}_60" "/tmp/runner_alert_${job_id}_120" 2>/dev/null || true
    # FIX-2: _notify_ai 중복 카운트 파일 정리
    rm -f "/tmp/pipeline-notify-count-${job_id}" 2>/dev/null || true
}

_finalize_rebase_review_worker() {
    local job_id="$1" session_id="$2" rc="$3"
    [[ "$rc" == "0" ]] && return 0
    # 함수가 스스로 처리한 실패는 이미 상태를 바꿨다. running 이면 처리되지 않은 즉사다.
    [[ "$(get_job_status "$job_id")" != "running" ]] && return 0
    log "  REBASE_REVIEW_WORKER_DIED job=$job_id rc=$rc — review_hold 로 회수"
    db_update "UPDATE pipeline_jobs SET status='review_hold', phase='review_hold',
               error_detail='rebase_review_worker_died: rc=${rc}', runner_pid=NULL, updated_at=NOW()
               WHERE job_id='${job_id}' AND status='running';"
    record_runner_event "$job_id" "job_terminal" "review_hold" "review_hold" "" "" "" "" "{\"error_detail\":\"rebase_review_worker_died\",\"rc\":\"${rc}\"}"
    _notify_ai "$job_id"
    promote_next_queued "AADS"
}

# Rebased AADS commits require a fresh durable AI review and a new approval.
review_rebased_aads_sha() {
    local job_id="$1" session_id="$2" repo="$3" sha="$4"
    local remote_sha base_sha diff stored_diff instruction changed_files request_id body response http_code
    local request_status="" verdict="" score="" flag_category="" deadline
    remote_sha=$(git -C "$repo" rev-parse --verify origin/main 2>/dev/null) || remote_sha=""
    base_sha=$(git -C "$repo" merge-base "$remote_sha" "$sha" 2>/dev/null) || base_sha=""
    diff=$(git -C "$repo" diff "${base_sha}..${sha}" 2>/dev/null) || diff=""
    if [[ ! "$remote_sha" =~ ^[0-9a-f]{40}$ || ! "$base_sha" =~ ^[0-9a-f]{40}$ || -z "$diff" ]] || ! looks_like_git_diff "$diff"; then
        _fail_job "$job_id" "$session_id" "deploy_rebase_review_diff_invalid" "새 SHA AI 재검수 diff 확인 실패"
        _notify_ai "$job_id"
        return 1
    fi
    stored_diff="${diff:0:48000}"
    instruction=$(get_job_instruction "$job_id")
    changed_files=$(printf '%s' "$diff" | grep '^diff --git' | sed 's/diff --git a\///' | sed 's/ b\/.*//' | tr '\n' ',' | sed 's/,$//')
    request_id=$(cat /proc/sys/kernel/random/uuid)
    db_update "UPDATE pipeline_jobs SET git_diff=$(sql_escape "$stored_diff"),
               actual_changed_files=$(sql_escape "$(printf '%s' "$changed_files" | tr ',' '\n' | json_array_from_lines)")::jsonb,
               review_request_id='${request_id}'::uuid, updated_at=NOW()
               WHERE job_id='${job_id}' AND status='running' AND commit_hash='${sha}';"
    if [[ "$(db_exec "SELECT COALESCE(review_request_id::text,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]')" != "$request_id" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_rebase_review_request_persist_failed" "새 SHA AI 재검수 요청 기록 실패"
        _notify_ai "$job_id"
        promote_next_queued "AADS"
        return 1
    fi
    # Linux MAX_ARG_STRLEN(131072B)는 argv 한 개의 상한이다. 잘리지 않은 rebase diff
    # (실측 180,545B)를 --arg 로 넘기면 jq 가 exit 126 으로 죽고, set -e + 출력
    # /dev/null 때문에 분리 워커가 흔적 없이 사라진다(2026-09-30 runner-7f5feba1).
    local diff_file body_file
    diff_file=$(mktemp "${TMPDIR:-/tmp}/aads-rebase-diff-XXXXXX") || diff_file=""
    body_file=$(mktemp "${TMPDIR:-/tmp}/aads-rebase-body-XXXXXX") || body_file=""
    if [[ -z "$diff_file" || -z "$body_file" ]]; then
        rm -f "$diff_file" "$body_file"
        _fail_job "$job_id" "$session_id" "deploy_rebase_review_tmp_failed" "새 SHA 재검수 임시파일 생성 실패"
        _notify_ai "$job_id"
        return 1
    fi
    printf '%s' "$diff" > "$diff_file"
    if ! jq -n --arg rid "$request_id" --arg jid "$job_id" --rawfile diff "$diff_file" \
        --arg inst "$instruction" --arg files "$changed_files" \
        '{request_id:$rid, job_id:$jid, project:"AADS", diff:$diff, instruction:$inst, files_changed:($files|split(","))}' > "$body_file"; then
        rm -f "$diff_file" "$body_file"
        _fail_job "$job_id" "$session_id" "deploy_rebase_review_body_failed" "새 SHA 재검수 요청 본문 생성 실패"
        _notify_ai "$job_id"
        return 1
    fi
    response=$(curl -4 -s --http1.1 -w "\n%{http_code}" -X POST \
        "${AADS_API_URL}/api/v1/review/code-diff/requests" -H "Content-Type: application/json" \
        --data-binary "@${body_file}" --connect-timeout 10 --max-time 60 2>/dev/null) || response=""
    rm -f "$diff_file" "$body_file"
    http_code=$(printf '%s\n' "$response" | tail -1)
    response=$(printf '%s\n' "$response" | sed '$d')
    if [[ "$http_code" == "200" ]]; then
        request_status=$(printf '%s' "$response" | jq -r '.status // empty' 2>/dev/null) || request_status=""
        if [[ -z "$request_status" && -n "$(printf '%s' "$response" | jq -r '.verdict // empty' 2>/dev/null)" ]]; then
            request_status="completed"
        fi
    fi
    if [[ "$http_code" == "202" ]]; then
        deadline=$((SECONDS + AADS_REVIEW_ASYNC_WAIT_SEC))
        while (( SECONDS < deadline )); do
            if [[ "$(get_job_status "$job_id")" != "running" ]]; then
                return 1
            fi
            response=$(curl -4 -s --http1.1 -w "\n%{http_code}" \
                "${AADS_API_URL}/api/v1/review/code-diff/requests/${request_id}" \
                --connect-timeout 5 --max-time 15 2>/dev/null) || response=""
            http_code=$(printf '%s\n' "$response" | tail -1)
            response=$(printf '%s\n' "$response" | sed '$d')
            if [[ "$http_code" == "200" ]]; then
                request_status=$(printf '%s' "$response" | jq -r '.status // empty' 2>/dev/null) || request_status=""
                [[ "$request_status" == "completed" || "$request_status" == "failed" ]] && break
            fi
            sleep "$AADS_REVIEW_POLL_INTERVAL"
        done
    fi
    if [[ "$http_code" != "200" || "$request_status" != "completed" ]]; then
        db_update "UPDATE pipeline_jobs SET status='review_hold', phase='review_hold',
                   error_detail='review_infra_failed: rebased SHA review unavailable',
                   runner_pid=NULL, updated_at=NOW() WHERE job_id='${job_id}' AND status='running';"
        record_runner_event "$job_id" "ai_review_result" "review_hold" "review_hold" "" "" "" "" "{\"sha\":\"${sha}\"}"
        _notify_ai "$job_id"
        promote_next_queued "AADS"
        return 1
    fi
    verdict=$(printf '%s' "$response" | jq -r '.verdict // empty')
    score=$(printf '%s' "$response" | jq -r '.score // 0')
    [[ "$score" =~ ^[0-9]+([.][0-9]+)?$ ]] || score="0"
    flag_category=$(printf '%s' "$response" | jq -r '.flag_category // empty')
    local review_infra_failure="false"
    case "$flag_category" in
        REVIEW_API_UNAVAILABLE|REVIEW_MODEL_NO_RESPONSE|REVIEW_PARSER_FAILURE|REVIEW_TIMEOUT)
            review_infra_failure="true" ;;
    esac
    if [[ "$review_infra_failure" == "true" ]]; then
        db_update "UPDATE pipeline_jobs SET status='review_hold', phase='review_hold',
                   review_verdict=$(sql_escape "$verdict"), review_score=${score:-0},
                   review_flag_category=$(sql_escape "$flag_category"), review_needs_retry=TRUE,
                   error_detail=$(sql_escape "review_infra_failed: category=${flag_category}"),
                   runner_pid=NULL, updated_at=NOW()
                   WHERE job_id='${job_id}' AND status='running' AND commit_hash='${sha}';"
        record_runner_event "$job_id" "ai_review_result" "review_hold" "review_hold" "" "" "" "" "{\"sha\":\"${sha}\",\"flag_category\":\"${flag_category}\"}"
        _notify_ai "$job_id"
        promote_next_queued "AADS"
        return 1
    fi
    if [[ "$verdict" != "APPROVE" ]]; then
        local detail="review_failed: verdict=${verdict} score=${score} category=${flag_category}"
        db_update "UPDATE pipeline_jobs SET status='error', phase='review_failed',
                   review_verdict=$(sql_escape "$verdict"), review_score=${score:-0},
                   error_detail=$(sql_escape "$detail"), runner_pid=NULL,
                   completed_at=NOW(), updated_at=NOW()
                   WHERE job_id='${job_id}' AND status='running';"
        record_runner_event "$job_id" "job_terminal" "error" "review_failed" "" "" "" "" "{\"sha\":\"${sha}\"}"
        _notify_ai "$job_id"
        promote_next_queued "AADS"
        return 1
    fi
    db_update "UPDATE pipeline_jobs SET status='awaiting_approval', phase='awaiting_approval',
               review_verdict='APPROVE', review_score=${score:-0}, error_detail=NULL,
               runner_pid=NULL, approval_requested_at=NOW(), updated_at=NOW()
               WHERE job_id='${job_id}' AND status='running' AND commit_hash='${sha}';"
    if [[ "$(get_job_status "$job_id")" != "awaiting_approval" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_rebase_review_persist_failed" "새 SHA 재검수 승인 대기 상태 저장 실패"
        _notify_ai "$job_id"
        return 1
    fi
    record_runner_event "$job_id" "approval_requested" "awaiting_approval" "awaiting_approval" "" "" "" "" "{\"commit_hash\":\"${sha}\",\"review_verdict\":\"APPROVE\"}"
    post_to_chat "$session_id" "🔔 [Pipeline Runner] origin/main 위 새 SHA AI 재검수 통과. 새 SHA 승인 필요: $job_id (${sha})"
    _notify_ai "$job_id"
    promote_next_queued "AADS"
    return 0
}

# A remote ancestor is complete only if the routed API is serving a certified
# release that contains it.  A successful push alone says nothing about deploy.
approved_sha_is_live() {
    local repo="$1" approved_sha="$2" state_dir="$3"
    local release_row="" release_sha="" release_digest="" release_port=""
    local resolved_sha="" active_port="" active_container="" active_digest=""
    release_row=$(db_exec "SELECT release_sha || '|' || image_digest || '|' || current_slot
        FROM deploy_runs WHERE project='AADS' AND component='api'
        AND target_env='production' AND status='success' AND phase='completed'
        AND image_digest IS NOT NULL AND image_digest <> ''
        AND standby_digest=image_digest
        ORDER BY id DESC LIMIT 1;" 2>/dev/null | tail -1) || return 1
    IFS='|' read -r release_sha release_digest release_port <<< "$release_row"
    [[ "$release_sha" =~ ^[0-9a-f]{12,40}$ && "$release_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    resolved_sha=$(git -C "$repo" rev-parse --verify "${release_sha}^{commit}" 2>/dev/null) || return 1
    git -C "$repo" merge-base --is-ancestor "$approved_sha" "$resolved_sha" 2>/dev/null || return 1
    active_port=$(tr -d '[:space:]' < "${state_dir}/.active_port" 2>/dev/null) || return 1
    active_container=$(tr -d '[:space:]' < "${state_dir}/.active_container" 2>/dev/null) || return 1
    [[ "$active_port" == "$release_port" ]] || return 1
    case "$active_port:$active_container" in
        8100:aads-server|8102:aads-server-green) ;;
        *) return 1 ;;
    esac
    active_digest=$(docker inspect "$active_container" --format '{{.Image}}' 2>/dev/null) || return 1
    [[ "$active_digest" == "$release_digest" ]] || return 1
    curl -fsS --connect-timeout 3 --max-time 5 "${AADS_API_URL}/api/v1/health" >/dev/null 2>&1
}

# ── 배포 반영 게이트 (AADS-RUNNER-DONE-REQUIRES-LIVE-IMAGE, 2026-10-06) ──
# runner-a5761224(20a97a9c) 는 deploy_runs #5599 가 target_drain_busy 로 blocked 인데도
# health 만 보고 status=done · '배포 완료' 를 올렸다. /api/v1/health 는 **옛 이미지**가
# 응답해도 OK 다 — 헬스는 "서비스가 산다" 이지 "내 커밋이 올라갔다" 가 아니다.
# 실행 중인 슬롯 이미지 태그(aads-server[-green]:<sha>)가 job 커밋을 포함할 때만 반영으로 본다.
#
# 기준은 "둘 중 하나라도" 다. deploy.sh 는 새 슬롯을 먼저 컷오버하고 standby 동기화는
# 뒤따르며 active stream 때문에 보류될 수 있다(success_partial, reject_duplicate_live_release
# 의 "한쪽만 올라가 있으면 standby 동기화가 남은 상태"). 양쪽을 요구하면 정상 부분성공을
# 거짓 실패로 만든다. 반대로 둘 다 옛 이미지인 사고(#5599)는 이 기준으로도 잡힌다.
# 출력(stdout 한 줄): live:<container> | queued:<run_id>:<status> | not_live:<run_id|none>:<status|none>
aads_release_live_verdict() {
    local sha="$1"; shift
    local repo c img tag resolved
    [[ "$sha" =~ ^[0-9a-f]{12,40}$ ]] || { echo "not_live:none:none"; return 1; }
    for c in aads-server aads-server-green; do
        img=$(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null) || continue
        tag="${img##*:}"
        [[ "$tag" =~ ^[0-9a-f]{7,40}$ ]] || continue
        if [[ "$sha" == "$tag"* ]]; then
            echo "live:${c}"; return 0
        fi
        for repo in "$@"; do
            [[ -n "$repo" ]] || continue
            resolved=$(git -C "$repo" rev-parse --verify "${tag}^{commit}" 2>/dev/null) || continue
            if git -C "$repo" merge-base --is-ancestor "$sha" "$resolved" 2>/dev/null; then
                echo "live:${c}"; return 0
            fi
        done
    done
    local row="" run_id="" status=""
    row=$(db_exec "SELECT id || '|' || status FROM deploy_runs
        WHERE upper(trim(project))='AADS'
        AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
        AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
        AND length(release_sha) >= 12 AND '${sha}' LIKE release_sha || '%'
        ORDER BY (status IN ('queued','running','verifying','syncing_standby')) DESC, id DESC LIMIT 1;" 2>/dev/null | tail -1) || row=""
    IFS='|' read -r run_id status <<< "$row"
    run_id=$(printf '%s' "$run_id" | tr -cd '0-9')
    status=$(printf '%s' "$status" | tr -cd 'A-Za-z0-9_')
    case "$status" in
        queued|running|verifying|syncing_standby)
            echo "queued:${run_id:-none}:${status}"; return 2 ;;
    esac
    echo "not_live:${run_id:-none}:${status:-none}"
    return 1
}

# 0: 반영 확인 — 호출자가 done 으로 진행. 1: 이 함수가 error/deploying 을 이미 기록함.
aads_deploy_live_gate() {
    local job_id="$1" session_id="$2" sha="$3"; shift 3
    local verdict="" rc=0 run_id="" status=""
    verdict=$(aads_release_live_verdict "$sha" "$@") || rc=$?
    case "$verdict" in
        live:*)
            log "  LIVE_IMAGE_OK job=$job_id sha=${sha:0:12} slot=${verdict#live:}"
            return 0 ;;
        queued:*)
            IFS=: read -r _ run_id status <<< "$verdict"
            db_update "UPDATE pipeline_jobs SET status='deploying', phase='deploy_queued',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[배포대기] 운영 이미지에 아직 미반영 — deploy_runs #${run_id} ${status} 진행 중',
                       updated_at=NOW() WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "deploy_queued" "deploying" "deploy_queued" "" "" "" "" "{\"deploy_run_id\":\"${run_id}\",\"deploy_status\":\"${status}\"}"
            post_to_chat "$session_id" "🟡 [Pipeline Runner] 배포 대기 — 아직 운영에 반영되지 않았고 deploy_runs #${run_id} (${status}) 가 진행 중입니다: $job_id"
            log "  DEPLOY_QUEUED job=$job_id deploy_run=#${run_id} status=${status}"
            return 1 ;;
        *)
            IFS=: read -r _ run_id status <<< "${verdict:-not_live:none:none}"
            local detail="deploy_not_live:${run_id:-none}:${status:-none}"
            db_update "UPDATE pipeline_jobs SET status='error', phase='deploy_not_live',
                       error_detail='${detail}',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[배포미반영] 실행 중인 API 슬롯 이미지가 커밋 ${sha:0:12} 를 포함하지 않음 — deploy_runs #${run_id:-none} ${status:-none}',
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
            record_runner_event "$job_id" "job_terminal" "error" "deploy_not_live" "" "" "" "" "{\"error_detail\":\"${detail}\"}"
            post_to_chat "$session_id" "🔴 [Pipeline Runner] 배포 미반영 — deploy_runs #${run_id:-none} status=${status:-none}, 운영 이미지에 커밋 ${sha:0:12} 없음: $job_id"
            log "  DEPLOY_NOT_LIVE job=$job_id deploy_run=#${run_id:-none} status=${status:-none}"
            return 1 ;;
    esac
}

# ── deploy_queued 마무리 (AADS-RUNNER-DEPLOY-QUEUED-FINALIZER, 2026-10-06) ──
# aads_deploy_live_gate 는 미반영 + deploy_runs 진행 중이면 status='deploying',
# phase='deploy_queued' 로 두고 끝난다. 그 뒤 이 잡을 done/error 로 닫는 주체가 없어
# done 을 조건으로 건 예약 작업이 WAITING 에 머물렀다. 주기 훅(_recover_stuck_jobs)이
# 같은 판정(aads_release_live_verdict)을 다시 호출해 닫는다.
#   live:*       → done            queued:* → 그대로(쓰기 없음)
#   not_live:*   → error deploy_not_live    queued 가 상한 초과 → error deploy_queued_timeout
# 갱신은 WHERE status='deploying' AND phase='deploy_queued' RETURNING 으로 원자화하고,
# 행이 실제로 바뀐 경우에만 이벤트·채팅·알림을 보낸다(두 러너/두 주기가 겹쳐도 1회).
# 상한은 updated_at 기준이다 — 게이트가 deploy_queued 기록 때 갱신하고 이후 아무도 건드리지 않는다.
AADS_DEPLOY_QUEUED_MAX_SEC="${AADS_DEPLOY_QUEUED_MAX_SEC:-7200}"

aads_finalize_deploy_queued_jobs() {
    [[ -z "${RUNNER_PROJECTS:-}" || ",${RUNNER_PROJECTS}," == *",AADS,"* ]] || return 0
    local rows="" job_id="" sha="" session_id="" age="" verdict="" run_id="" status="" slot=""
    local repo="${PROJECT_WORKDIR[AADS]:-}" claimed="" detail="" phase=""
    rows=$(db_exec "SELECT job_id, COALESCE(commit_hash,''), COALESCE(chat_session_id::text,''),
                           GREATEST(EXTRACT(EPOCH FROM (NOW() - updated_at)),0)::bigint
                    FROM pipeline_jobs
                    WHERE project='AADS' AND status='deploying' AND phase='deploy_queued'
                    ORDER BY updated_at;" 2>/dev/null) || return 0
    [[ -n "$rows" ]] || return 0
    while IFS=$'\x1e' read -r job_id sha session_id age; do
        job_id="${job_id// /}"; sha="${sha// /}"; session_id="${session_id// /}"; age="${age// /}"
        [[ "$job_id" =~ ^(runner-[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]] || continue
        [[ "$age" =~ ^[0-9]+$ ]] || age=0
        verdict=$(aads_release_live_verdict "$sha" "$repo") || true
        claimed=""
        case "$verdict" in
            live:*)
                slot="${verdict#live:}"
                claimed=$(db_exec "UPDATE pipeline_jobs SET status='done', phase='done',
                    review_feedback=COALESCE(review_feedback,'') || E'\n[배포반영확인] deploy_queued → 운영 슬롯 ${slot} 이 커밋 ${sha:0:12} 를 포함',
                    deployed_at=NOW(), completed_at=NOW(), updated_at=NOW()
                    WHERE job_id='${job_id}' AND status='deploying' AND phase='deploy_queued'
                    RETURNING job_id;" 2>/dev/null) || claimed=""
                if [[ "${claimed// /}" == "$job_id" ]]; then
                    record_runner_event "$job_id" "job_terminal" "done" "done" "" "" "" "" "{\"deploy_queued_finalized\":\"live\",\"slot\":\"${slot}\"}"
                    post_to_chat "$session_id" "✅ [Pipeline Runner] 배포 반영 확인 — 운영 슬롯 ${slot} 에 커밋 ${sha:0:12} 반영됨: $job_id"
                    log "  DEPLOY_QUEUED_FINALIZED job=$job_id -> done slot=${slot}"
                    _notify_ai "$job_id"
                fi
                continue ;;
            queued:*)
                IFS=: read -r _ run_id status <<< "$verdict"
                (( age > AADS_DEPLOY_QUEUED_MAX_SEC )) || continue
                phase="deploy_queued_timeout"
                detail="deploy_queued_timeout:${run_id:-none}:${status:-none}" ;;
            *)
                IFS=: read -r _ run_id status <<< "${verdict:-not_live:none:none}"
                phase="deploy_not_live"
                detail="deploy_not_live:${run_id:-none}:${status:-none}" ;;
        esac
        claimed=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='${phase}',
            error_detail='${detail}',
            review_feedback=COALESCE(review_feedback,'') || E'\n[배포미반영] deploy_queued 재확인: ${detail} — 운영 이미지에 커밋 ${sha:0:12} 없음',
            completed_at=NOW(), updated_at=NOW()
            WHERE job_id='${job_id}' AND status='deploying' AND phase='deploy_queued'
            RETURNING job_id;" 2>/dev/null) || claimed=""
        if [[ "${claimed// /}" == "$job_id" ]]; then
            record_runner_event "$job_id" "job_terminal" "error" "$phase" "" "" "" "" "{\"error_detail\":\"${detail}\"}"
            post_to_chat "$session_id" "🔴 [Pipeline Runner] 배포 미반영 — ${detail}, 운영 이미지에 커밋 ${sha:0:12} 없음: $job_id"
            log "  DEPLOY_QUEUED_FINALIZED job=$job_id -> error ${detail}"
            _notify_ai "$job_id"
        fi
    done <<< "$rows"
    return 0
}

# ── autoheal 인계 추적 (AADS-LLM-M6-DEPLOY-REGRESSION-GUARD-RETRY, 2026-09-30) ──
# deploy.sh 는 target_drain_busy·dirty_worktree 로 멈추면 EXIT 트랩에서 같은
# 릴리스의 successor run 을 띄우고 자기 행을 superseded_by_autoheal_* 로 바꾼 뒤
# rc=1 로 나간다. 러너는 그 rc 만 보고 bluegreen_failed 로 확정했다.
# 2026-09-30 실측: runner-a5f21866(da6b42d1) 이 01:25 에 error 로 종결됐는데,
# 같은 SHA 의 successor #5290→#5291→#5292→#5293 이 01:46 에 success_partial 로
# 배포를 끝냈다. 산출물은 운영에 나갔고 작업만 실패로 남았다.
#
# 인계가 확인된 경우에만 successor 사슬을 끝까지 따라가 결과를 판정한다.
# 인계 흔적이 없으면 즉시 1 — 기존 실패 판정을 그대로 유지한다.
#   0: 같은 릴리스가 success/success_partial 로 끝났거나, 다른 run 에 흡수돼
#      approved_sha_is_live 가 확인됨
#   1: 인계 없음 / 사슬이 실패로 끝남 / 상한 소진
# stdout 마지막 줄은 판정 요약이다(review_feedback 에 남긴다).
AADS_AUTOHEAL_FOLLOW_MAX_SEC="${AADS_AUTOHEAL_FOLLOW_MAX_SEC:-2700}"
AADS_AUTOHEAL_FOLLOW_POLL_SEC="${AADS_AUTOHEAL_FOLLOW_POLL_SEC:-30}"
# successor 가 blocked 로 멈춘 직후 autoheal 이 다음 successor 를 등록하기까지의 틈.
# 이 시간 안의 failed/blocked 는 확정으로 보지 않는다.
AADS_AUTOHEAL_FOLLOW_SETTLE_SEC="${AADS_AUTOHEAL_FOLLOW_SETTLE_SEC:-90}"
# 세 시간 값의 관계 (2026-09-30 검수 확정 결함):
#   배포 락 TTL 600s (deploy_lock.py acquire_deploy_lock, 갱신 없음) <
#   DEPLOY_LOCK_MAX_WAIT_SEC 900s (대기하는 쪽) < AADS_AUTOHEAL_FOLLOW_MAX_SEC 2700s (추적하는 쪽)
# 실측 autoheal 사슬(#5290→#5293)이 1,080s 라 추적은 예측 가능하게 락 TTL 을 넘는다.
# 갱신 없이 추적하면 t=600s 에 락이 풀려 다른 잡이 병행 배포를 시작한다(배포 #414 를 죽인 조합).
# 그래서 매 poll 마다 renew 로 TTL 을 되돌리고, 갱신이 거부되면 추적을 멈춘다(fail-closed).
# 대기 측 상한(900s)이 추적 측(2700s)보다 짧으므로 대기 중인 잡은 holder 가 추적 중이어도
# 먼저 deploy_lock_fail 에 닿을 수 있다 — 그래서 재큐잉 메시지에 holder 추적 표시를 남긴다.
# 이탈 사유는 구분해 남긴다: timeout(상한 소진) / deploy_lock_lost(갱신 거부 — 다른 잡이 락을
# 쥐었거나 만료) / deploy_lock_unreadable(갱신 응답 불능). 마지막 둘은 배포 결과가 아니라
# 락 기인 이탈이라 deploy_job 이 runner 이벤트(deploy_autoheal_follow_abandoned)로도 남긴다.
# 갱신 API 가 일시적으로 무응답인 경우(bluegreen 전환 중 API 재기동)는 TTL 여유(600s) 안에서만
# 연속 AADS_AUTOHEAL_FOLLOW_RENEW_MAX_FAIL 회(기본 3 × poll 30s = 90s)까지 견딘다.
AADS_AUTOHEAL_FOLLOW_RENEW_MAX_FAIL="${AADS_AUTOHEAL_FOLLOW_RENEW_MAX_FAIL:-3}"
AUTOHEAL_FOLLOW_TAG="[배포추적]"

# 이 holder 잡이 autoheal successor 를 추적 중인지 — 대기 측 재큐잉 메시지용.
# 추적 중 heartbeat(updated_at)가 poll 마다 갱신되므로 신선도로 "지금" 을 판정한다.
_deploy_holder_follow_tag() {
    local holder="$1"
    [[ "$holder" =~ ^[A-Za-z0-9._:-]+$ ]] || return 0
    local hit
    hit=$(db_exec "SELECT count(*) FROM pipeline_jobs WHERE job_id='${holder}' AND status='deploying'
        AND position('${AUTOHEAL_FOLLOW_TAG}' in COALESCE(review_feedback, '')) > 0
        AND updated_at > NOW() - INTERVAL '180 seconds';" 2>/dev/null | tail -1 | tr -cd '0-9') || hit=""
    [[ "${hit:-0}" -gt 0 ]] && printf ' %s' "successor-추적중"
    return 0
}

follow_autoheal_successor() {
    local job_id="$1" sha="$2" since_epoch="$3" repo="$4" state_dir="$5" project="${6:-AADS}"
    local max_wait="$AADS_AUTOHEAL_FOLLOW_MAX_SEC" poll="$AADS_AUTOHEAL_FOLLOW_POLL_SEC"
    local settle="$AADS_AUTOHEAL_FOLLOW_SETTLE_SEC"
    [[ "$max_wait" =~ ^[0-9]+$ && "$max_wait" -gt 0 ]] || max_wait=2700
    [[ "$poll" =~ ^[0-9]+$ && "$poll" -gt 0 ]] || poll=30
    [[ "$settle" =~ ^[0-9]+$ ]] || settle=90
    local renew_max="$AADS_AUTOHEAL_FOLLOW_RENEW_MAX_FAIL"
    [[ "$renew_max" =~ ^[0-9]+$ && "$renew_max" -gt 0 ]] || renew_max=3
    if [[ ! "$sha" =~ ^[0-9a-f]{12,40}$ || ! "$since_epoch" =~ ^[0-9]+$ ]]; then
        echo "autoheal_follow: skip (sha/since 형식 오류)"
        return 1
    fi
    local scope="upper(trim(project))='AADS'
        AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
        AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
        AND length(release_sha) >= 12 AND '${sha}' LIKE release_sha || '%'
        AND created_at >= to_timestamp(${since_epoch})"
    local handoff=""
    handoff=$(db_exec "SELECT count(*) FROM deploy_runs WHERE ${scope}
        AND status='superseded' AND phase LIKE 'superseded_by_autoheal%';" 2>/dev/null | tail -1 | tr -cd '0-9') || handoff=""
    if [[ "${handoff:-0}" -le 0 ]]; then
        echo "autoheal_follow: no_handoff"
        return 1
    fi
    log "  AUTOHEAL_FOLLOW job=$job_id sha=${sha:0:12} — deploy.sh 가 successor 에 인계함, 최대 ${max_wait}s 추적"
    db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n${AUTOHEAL_FOLLOW_TAG} deploy.sh 가 autoheal successor 에 인계함 — 최대 ${max_wait}s 추적, 배포 락 매 poll 갱신', updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

    local waited=0 row="" run_id="" status="" phase="" age="" lock_out="" lock_reason="" renew_fail=0
    while :; do
        row=$(db_exec "SELECT id || '|' || status || '|' || COALESCE(phase, '') || '|'
            || GREATEST(0, EXTRACT(EPOCH FROM (NOW() - updated_at)))::bigint
            FROM deploy_runs WHERE ${scope} ORDER BY id DESC LIMIT 1;" 2>/dev/null | tail -1) || row=""
        IFS='|' read -r run_id status phase age <<< "$row"
        [[ "$age" =~ ^[0-9]+$ ]] || age=0
        case "$status" in
            success|success_partial)
                echo "autoheal_follow: success run=#${run_id} status=${status} phase=${phase} waited=${waited}s"
                return 0 ;;
            failed|blocked|cancelled)
                if (( age >= settle )); then
                    echo "autoheal_follow: failed run=#${run_id} status=${status} phase=${phase} waited=${waited}s"
                    return 1
                fi ;;
            superseded)
                # autoheal 사슬은 다음 행이 곧 보인다. 그 밖의 superseded(배치 흡수·
                # 신규 릴리스)는 이 SHA 를 담은 다른 run 이 나갔는지로 판정한다.
                if [[ "$phase" != superseded_by_autoheal* ]] \
                    && approved_sha_is_live "$repo" "$sha" "$state_dir"; then
                    echo "autoheal_follow: success run=#${run_id} status=superseded phase=${phase} live=contained waited=${waited}s"
                    return 0
                fi ;;
        esac
        if (( waited >= max_wait )); then
            echo "autoheal_follow: timeout run=#${run_id:-none} status=${status:-none} phase=${phase:-none} waited=${waited}s"
            return 1
        fi
        # 터미널 판정 뒤에 갱신한다 — 사슬이 이미 끝났다면 락 상태와 무관하게 결과를 따른다.
        lock_out=$(_renew_deploy_lock "$project" "$job_id") || lock_out=""
        if [[ "$lock_out" == *'"renewed":true'* ]]; then
            renew_fail=0
        elif [[ "$lock_out" == *'"renewed":false'* ]]; then
            lock_reason=$(printf '%s' "$lock_out" | sed -n 's/.*"reason":"\([^"]*\)".*/\1/p' | tr -cd 'A-Za-z0-9._-' | cut -c1-40)
            echo "autoheal_follow: deploy_lock_lost run=#${run_id:-none} reason=${lock_reason:-unknown} waited=${waited}s"
            return 1
        else
            renew_fail=$((renew_fail + 1))
            if (( renew_fail >= renew_max )); then
                echo "autoheal_follow: deploy_lock_unreadable run=#${run_id:-none} fails=${renew_fail} waited=${waited}s"
                return 1
            fi
        fi
        db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
        sleep "$poll"
        waited=$((waited + poll))
    done
}
# ── 배포 락 대기 + 재큐잉 (AADS-LLM-M6-DEPLOY-REGRESSION-GUARD, 2026-09-29) ──
# 예전 셸 경로는 30+60+90=180초만 기다리고 status='error' 로 확정했다. AADS
# bluegreen 배포 실측(deploy_runs success, 24h n=16)은 중앙값 514s·최대 631s 라
# 다른 배포가 돌고 있으면 승인된 작업이 산술적으로 폐기됐다(24h 8건).
# Python 경로(pipeline_runner_service.py 의 _DEPLOY_LOCK_*)와 같은 의미로 맞춘다:
#   - 락 미획득 라운드마다 status='queued', phase='deploy_lock_wait' 로 재큐잉해
#     승인·커밋을 보존한다. phase 가 ('queued','coding') 이 아니므로 코딩 claim 에
#     다시 잡히지 않고, 이 프로세스가 계속 대기한다.
#   - 누적 대기가 DEPLOY_LOCK_MAX_WAIT_SEC 에 닿았을 때만 terminal 로 확정하고
#     error_detail 에 누적 대기와 holder 를 남긴다.
#   - 락 API 무응답이면 기존대로 잠금 없이 진행한다(deploy.sh flock 이 최종 직렬화).
# 기본 900s 는 실측 최대 631s 를 덮는다. 환경변수 이름은 Python 경로와 같다.
DEPLOY_LOCK_MAX_WAIT_SEC="${DEPLOY_LOCK_MAX_WAIT_SEC:-900}"
DEPLOY_LOCK_BACKOFF_SEC="${DEPLOY_LOCK_BACKOFF_SEC:-10 30 60}"
DEPLOY_LOCK_WAIT_PHASE="deploy_lock_wait"

acquire_deploy_lock_with_requeue() {
    local job_id="$1" project="$2" session_id="$3"
    local max_wait="$DEPLOY_LOCK_MAX_WAIT_SEC" result="" holder="unknown" holder_note="" waited=0 requeue=0 backoff
    [[ "$max_wait" =~ ^[0-9]+$ && "$max_wait" -gt 0 ]] || max_wait=900
    local -a backoffs=()
    for backoff in $DEPLOY_LOCK_BACKOFF_SEC; do
        [[ "$backoff" =~ ^[0-9]+$ && "$backoff" -gt 0 ]] && backoffs+=("$backoff")
    done
    (( ${#backoffs[@]} > 0 )) || backoffs=(10 30 60)

    # 획득 시도는 루프 맨 위 한 곳뿐이다 — 이미 쥔 락을 다시 acquire 하면
    # holder=자기자신 으로 "점유" 응답을 받기 때문이다.
    local idx=0
    while :; do
        result=$(curl -sf -X POST -H "X-Monitor-Key: internal" "${AADS_API_URL}/api/v1/ops/locks/deploy/acquire?project=${project}&session_id=${job_id}" 2>/dev/null) || true
        if ! echo "$result" | grep -q '"acquired":false'; then
            if echo "$result" | grep -q '"acquired":true'; then
                (( requeue > 0 )) && log "  DEPLOY_LOCK_ACQUIRED job=$job_id waited=${waited}s requeue=${requeue}"
            else
                log "  DEPLOY_LOCK_API_OK job=$job_id waited=${waited}s — API 응답 없음, 잠금 없이 진행"
            fi
            if (( requeue > 0 )); then
                db_update "UPDATE pipeline_jobs SET status='deploying', phase='deploying', updated_at=NOW()
                           WHERE job_id='${job_id}' AND status='queued' AND phase='${DEPLOY_LOCK_WAIT_PHASE}';"
            fi
            return 0
        fi
        holder=$(printf '%s' "$result" | sed -n 's/.*"holder":"\([^"]*\)".*/\1/p' | tr -cd 'A-Za-z0-9._:-' | cut -c1-80)
        [[ -n "$holder" ]] || holder="unknown"
        # holder 가 autoheal successor 를 추적 중이면 락이 갱신되며 최대 AADS_AUTOHEAL_FOLLOW_MAX_SEC
        # 까지 유지된다 — 대기 상한(DEPLOY_LOCK_MAX_WAIT_SEC)이 먼저 닿을 수 있음을 표시한다.
        holder_note=$(_deploy_holder_follow_tag "$holder" 2>/dev/null) || holder_note=""
        (( waited >= max_wait )) && break
        # 백오프 한 바퀴를 다 돌아도 못 잡았으면 재큐잉으로 기록한다(Python 경로와 같은 단위).
        if (( idx >= ${#backoffs[@]} )); then
            idx=0
            requeue=$((requeue + 1))
            log "  DEPLOY_LOCK_REQUEUE job=$job_id project=$project holder=${holder}${holder_note} waited=${waited}s/${max_wait}s requeue=${requeue}"
            db_update "UPDATE pipeline_jobs SET status='queued', phase='${DEPLOY_LOCK_WAIT_PHASE}',
                       review_feedback=COALESCE(review_feedback,'') || E'\n[배포대기] deploy lock 점유로 재큐잉 ${requeue} (holder=${holder}${holder_note}, 누적대기 ${waited}s/${max_wait}s)',
                       updated_at=NOW() WHERE job_id='${job_id}' AND status IN ('deploying','queued');"
            record_runner_event "$job_id" "deploy_lock_requeued" "queued" "$DEPLOY_LOCK_WAIT_PHASE" "" "" "" "" "{\"holder\":\"${holder}\",\"holder_following_successor\":$([[ -n "$holder_note" ]] && echo true || echo false),\"waited_sec\":${waited},\"max_wait_sec\":${max_wait},\"requeue\":${requeue}}"
            (( requeue == 1 )) && post_to_chat "$session_id" "⏳ [Pipeline Runner] 다른 배포 진행 중(holder=${holder}) — 배포 대기로 재큐잉, 최대 ${max_wait}초 대기: $job_id"
        fi
        backoff=${backoffs[$idx]}
        idx=$((idx + 1))
        (( waited + backoff > max_wait )) && backoff=$((max_wait - waited))
        log "  DEPLOY_LOCK_WAIT job=$job_id project=$project holder=${holder}${holder_note} — ${backoff}초 후 재시도 (누적 ${waited}s/${max_wait}s)"
        sleep "$backoff"
        waited=$((waited + backoff))
    done

    local detail="deploy_lock_fail: waited=${waited}s max_wait=${max_wait}s requeue=${requeue} holder=${holder}"
    log "  DEPLOY_LOCK_FAIL job=$job_id — ${detail}"
    db_update "UPDATE pipeline_jobs SET status='error', phase='deploy_lock_fail',
               error_detail='${detail}',
               review_feedback=COALESCE(review_feedback,'') || E'\n[배포실패] deploy lock 누적대기 ${waited}s(상한 ${max_wait}s) 소진 — holder=${holder}',
               completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
    record_runner_event "$job_id" "job_terminal" "error" "deploy_lock_fail" "" "" "" "" "{\"error_detail\":\"deploy_lock_fail\",\"holder\":\"${holder}\",\"waited_sec\":${waited},\"max_wait_sec\":${max_wait},\"requeue\":${requeue}}"
    post_to_chat "$session_id" "⚠️ [Pipeline Runner] 배포 락 누적대기 ${waited}초 소진(holder=${holder}): $job_id"
    _release_deploy_lock "$project" "$job_id"
    _notify_ai "$job_id"
    promote_next_queued "$project"
    return 1
}

# ── 승인된 작업 배포 ──────────────────────────────────────────────────
deploy_job() {
    local job_id="$1" project="$2" session_id="$3"
    local _job_instruction=""
    _job_instruction=$(get_job_instruction "$job_id")
    local workdir
    if ! workdir=$(resolve_project_workdir "$project" "$_job_instruction"); then
        fail_invalid_aads_target "$job_id" "$session_id"
        return 1
    fi
    local target_repo="default"
    if is_aads_dashboard_instruction "$project" "$_job_instruction"; then
        target_repo="aads-dashboard"
    fi
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "[deploy_job] start job_id=$job_id project=$project"
    log "▶ DEPLOY job=$job_id project=$project target=$target_repo workdir=$workdir"

    # Redis deploy lock 획득 (동시 배포 방지) — 실측 배포시간 기반 대기 + 재큐잉
    # (AADS-LLM-M6-DEPLOY-REGRESSION-GUARD). 상한 초과 시 terminal 처리는 함수가 끝낸다.
    if ! acquire_deploy_lock_with_requeue "$job_id" "$project" "$session_id"; then
        return 1
    fi

    post_to_chat "$session_id" "🚀 [Pipeline Runner] 배포 시작: $job_id"

    local main_workdir="$workdir"
    local worktree_dir="/tmp/aads-wt-${job_id}"

    local expected_sha current_sha
    expected_sha=$(db_exec "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]') || expected_sha=""
    if [[ "$project" == "AADS" ]]; then
        if ! deploy_isolated_git_preflight "$job_id" "$project" "$session_id" "$main_workdir" "$worktree_dir" "$expected_sha"; then
            _release_deploy_lock "$project" "$job_id"
            promote_next_queued "$project"
            return 1
        fi
        expected_sha="${DEPLOY_ISOLATED_EFFECTIVE_SHA:-$expected_sha}"
    elif ! deploy_git_preflight "$job_id" "$project" "$session_id" "$main_workdir"; then
        _release_deploy_lock "$project" "$job_id"
        promote_next_queued "$project"
        return 1
    fi

    if [[ "$project" != "AADS" ]] && ! ensure_approved_job_worktree "$job_id" "$worktree_dir" "$main_workdir" "$expected_sha"; then
        _fail_job "$job_id" "$session_id" "deploy_worktree_not_isolated" "BLOCK: 승인/배포 push 거부 — isolated runner worktree 복구/검증 실패 (${worktree_dir})"
        _release_deploy_lock "$project" "$job_id"
        return 1
    fi

    current_sha=$(git -C "$worktree_dir" rev-parse HEAD 2>/dev/null || true)
    if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ || "$current_sha" != "$expected_sha" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_commit_sha_mismatch" "BLOCK: 승인 commit SHA가 비어 있거나 runner worktree HEAD와 불일치 (expected=${expected_sha:-empty}, head=${current_sha:-empty})"
        _release_deploy_lock "$project" "$job_id"
        return 1
    fi

    local _pre_sha _py_changed="false"
    _pre_sha=$(git -C "$worktree_dir" rev-parse "${current_sha}^" 2>/dev/null || true)
    git -C "$worktree_dir" diff-tree --no-commit-id --name-only -r "$current_sha" 2>/dev/null | grep -q '\.py$' && _py_changed="true"

    local lock_file="/tmp/pipeline-deploy-${project}.lock" push_out push_err push_exit=0 push_diag="" push_lock_fd=""
    local push_state="" push_recheck="" push_skipped="false" stale_detail=""

    # ── push 전 사전 판별 — non-fast-forward 를 불투명한 거부로 만들지 않는다 ──
    push_state=$(classify_push_state "$worktree_dir" "$current_sha")
    log "  PUSH_PRECHECK job=$job_id sha=$current_sha state=$push_state"

    # 원격 조회 실패는 판별 불가이므로 AADS 릴리스에서 차단한다.
    if [[ "$project" == "AADS" && "$push_state" == "fetch_fail" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_origin_missing" "승인 SHA push 사전판별 실패: fetch_fail"
        _release_deploy_lock "$project" "$job_id"
        return 1
    fi

    if [[ "$push_state" == "already_present" ]]; then
        push_skipped="true"
        log "  PUSH_ALREADY_PRESENT job=$job_id sha=$current_sha — origin/main 에 이미 포함되어 push 생략"
        record_git_diagnostics "$job_id" "push_already_present" "$worktree_dir" 0 "" "" >/dev/null
    elif [[ "$push_state" == "stale_base" ]]; then
        # 겹치는 파일이 없으면 최신 origin/main 위로 옮겨 붙인다. 겹치면 사람이 본다.
        local rebased_sha=""
        if rebased_sha=$(attempt_stale_base_rebase "$worktree_dir" "$current_sha" "$job_id"); then
            log "  PUSH_AUTO_REBASED job=$job_id ${current_sha} -> ${rebased_sha}"
            record_runner_event "$job_id" "push_stale_base_rebased" "info" "auto_rebase" "" "" "" "" "{\"from\":\"${current_sha}\",\"to\":\"${rebased_sha}\"}"
            local inherit_verdict="" inherit_reason="" inherit_from="" inherit_pid=""
            if [[ "$project" == "AADS" ]]; then
                read -r inherit_verdict inherit_reason inherit_from inherit_pid \
                    <<< "$(inherit_approval_decision "$job_id" "$worktree_dir" "$rebased_sha")" || true
                log "  APPROVAL_INHERIT_CHECK job=$job_id verdict=${inherit_verdict:-none} reason=${inherit_reason:-none} approved=${inherit_from:--} patch_id=${inherit_pid:--}"
            fi
            if [[ "$project" == "AADS" && "$inherit_verdict" == "inherit" ]]; then
                # 내용(patch-id)이 승인된 SHA 와 같다 — 승인을 상속해 배포를 이어간다.
                # review_verdict / review_request_id 는 승인 이력이므로 지우지 않는다.
                db_update "UPDATE pipeline_jobs SET commit_hash='${rebased_sha}',
                           review_feedback=COALESCE(review_feedback,'') || E'\n' || $(sql_escape "[승인상속] patch-id 동일(${inherit_pid}) — ${current_sha}→${rebased_sha}"),
                           updated_at=NOW()
                           WHERE job_id='${job_id}' AND status='deploying' AND commit_hash='${current_sha}';"
                if [[ "$(db_exec "SELECT COALESCE(commit_hash,'') FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -d '[:space:]')" != "$rebased_sha" ]]; then
                    _fail_job "$job_id" "$session_id" "deploy_rebase_inherit_persist_failed" "승인 상속 SHA 저장 실패"
                    _release_deploy_lock "$project" "$job_id"
                    return 1
                fi
                record_runner_event "$job_id" "approval_inherited_same_patch_id" "info" "approved" "" "" "" "" "{\"from\":\"${current_sha}\",\"to\":\"${rebased_sha}\",\"patch_id\":\"${inherit_pid}\",\"approved_sha\":\"${inherit_from}\"}"
                current_sha="$rebased_sha"
                expected_sha="$rebased_sha"
                _pre_sha=$(git -C "$worktree_dir" rev-parse "${current_sha}^" 2>/dev/null || true)
                push_state="fast_forward"
            elif [[ "$project" == "AADS" ]]; then
                record_runner_event "$job_id" "approval_inherit_skipped" "info" "auto_rebase" "" "" "" "" "{\"reason\":\"${inherit_reason:-unknown}\",\"from\":\"${current_sha}\",\"to\":\"${rebased_sha}\",\"approved_sha\":\"${inherit_from:--}\"}"
                if [[ "$inherit_reason" == inherit_limit_exceeded* ]]; then
                    db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n' || $(sql_escape "[승인상속 중단] ${inherit_reason} — base 경합 반복, 사람 재승인 필요 (AADS_APPROVAL_INHERIT_MAX=${AADS_APPROVAL_INHERIT_MAX:-5})"),
                               updated_at=NOW() WHERE job_id='${job_id}';"
                fi
                # 승인된 SHA 와 내용이 다르거나 증명할 수 없으므로 새 diff 를 AI 재검수하고 CEO 재승인을 받는다.
                # 재큐잉은 새 실행이다. started_at 을 두면 승인 대기 시간까지 MAX_RUNTIME 에 합산돼 좀비로 오진된다(2026-09-30 runner-e3b882c2, 7710s).
                db_update "UPDATE pipeline_jobs SET status='running', phase='ai_review',
                           started_at=NOW(), commit_hash='${rebased_sha}', review_verdict=NULL,
                           review_request_id=NULL, runner_pid=${BASHPID}, completed_at=NULL,
                           updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
                if [[ "$(get_job_status "$job_id")" != "running" ]]; then
                    _fail_job "$job_id" "$session_id" "deploy_rebase_requeue_failed" "새 SHA 재검수 상태 저장 실패"
                    _release_deploy_lock "$project" "$job_id"
                    return 1
                fi
                record_runner_event "$job_id" "deploy_stale_rebased_requeued" "running" "ai_review" "" "" "" "" "{\"from\":\"${current_sha}\",\"to\":\"${rebased_sha}\"}"
                _release_deploy_lock "$project" "$job_id"
                # The durable request is polled in a separate process; the
                # deploy worker can accept another job while review runs.
                (
                    trap '_rc=$?; _finalize_rebase_review_worker "'"$job_id"'" "'"$session_id"'" "$_rc"; exit $_rc' EXIT
                    review_rebased_aads_sha "$job_id" "$session_id" "$worktree_dir" "$rebased_sha"
                ) 9>&- </dev/null >/dev/null 2>&1 &
                local review_worker_pid=$!
                db_update "UPDATE pipeline_jobs SET runner_pid=${review_worker_pid}, updated_at=NOW()
                           WHERE job_id='${job_id}' AND status='running' AND commit_hash='${rebased_sha}';"
                return 0
            else
                db_update "UPDATE pipeline_jobs SET commit_hash='${rebased_sha}', updated_at=NOW() WHERE job_id='${job_id}';"
                current_sha="$rebased_sha"
                push_state="fast_forward"
            fi
        else
            if [[ "$project" == "AADS" ]]; then
                _fail_job "$job_id" "$session_id" "deploy_isolated_push_state" "승인 SHA push 사전판별 실패: stale_base; 자동 rebase 안전 조건 미충족" "deploy_isolated_push_state: stale_base"
                _release_deploy_lock "$project" "$job_id"
                return 1
            fi
            stale_detail="push_stale_base: 승인 SHA(${current_sha}) 의 base 가 origin/main 보다 낡아 non-fast-forward 입니다. force push 는 금지이므로 자동 복구하지 않습니다 — 최신 origin/main 위에서 재작업 후 재승인하십시오."
            db_update "UPDATE pipeline_jobs SET status='error', phase='push_stale_base',
                       error_detail=$(sql_escape "$stale_detail"),
                       review_feedback=COALESCE(review_feedback,'') || E'\n[자동] push 사전판별 stale_base — 최신 origin/main 기준 재작업 필요',
                       completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
            record_git_diagnostics "$job_id" "push_stale_base" "$worktree_dir" 1 "" "" >/dev/null
            record_runner_event "$job_id" "job_terminal" "error" "push_stale_base" "" "" "" "" "{\"error_detail\":\"push_stale_base\"}"
            post_to_chat "$session_id" "🔴 [Pipeline Runner] push 중단 — 승인 SHA 의 base 가 낡음(non-fast-forward): $job_id (${stale_detail:0:400})"
            _release_deploy_lock "$project" "$job_id"
            promote_next_queued "$project"
            return 1
        fi
    fi

    if [[ "$push_skipped" != "true" ]]; then
        push_out=$(mktemp "/tmp/pipeline-push-${job_id}.out.XXXXXX")
        push_err=$(mktemp "/tmp/pipeline-push-${job_id}.err.XXXXXX")
        if ! exec {push_lock_fd}>"$lock_file"; then
            push_exit=75
            echo "failed to open push lock fd: $lock_file" >"$push_err"
        else
            {
                if flock -w 300 "$push_lock_fd"; then
                    git -C "$worktree_dir" push origin "${current_sha}:refs/heads/main" || push_exit=$?
                else
                    push_exit=75
                fi
            } >"$push_out" 2>"$push_err"
            exec {push_lock_fd}>&-
        fi
        # push 실패 시 1회 재판별 — 동시 push 경합으로 이미 반영된 경우는 성공 처리
        if [[ "$push_exit" -ne 0 ]]; then
            push_recheck=$(classify_push_state "$worktree_dir" "$current_sha")
            log "  PUSH_RECHECK job=$job_id exit=$push_exit state=$push_recheck"
            if [[ "$push_recheck" == "already_present" ]]; then
                log "  PUSH_RACE_RESOLVED job=$job_id — push 는 거부됐으나 승인 SHA 가 origin/main 에 이미 존재"
                push_exit=0
            fi
        fi
        push_diag=$(record_git_diagnostics "$job_id" "$([[ "$push_exit" -eq 0 ]] && echo push_succeeded || echo push_failed)" \
            "$worktree_dir" "$push_exit" "$(tail -30 "$push_out")" "$(tail -30 "$push_err")")
        rm -f "$push_out" "$push_err"
    fi

    if [[ "$push_exit" -ne 0 ]]; then
        local push_error_detail
        push_error_detail="push_fail(state=${push_state}/recheck=${push_recheck:-none}): ${push_diag:0:1700}"
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
    local already_present_remote_sha=""
    if [[ "$project" == "AADS" && "$push_state" == "already_present" ]]; then
        already_present_remote_sha=$(git -C "$worktree_dir" ls-remote origin refs/heads/main 2>/dev/null | awk 'NR==1 {print $1}') || already_present_remote_sha=""
    fi
    if [[ "$project" == "AADS" && "$push_state" == "already_present" \
        && "$already_present_remote_sha" != "$expected_sha" \
        && "$already_present_remote_sha" =~ ^[0-9a-f]{40}$ ]]; then
        if ! approved_sha_is_live "$worktree_dir" "$expected_sha" "$main_workdir"; then
            _fail_job "$job_id" "$session_id" "deploy_already_present_unverified" "원격 포함 SHA 의 활성 릴리스 검증 실패"
            _release_deploy_lock "$project" "$job_id"
            promote_next_queued "$project"
            return 1
        fi
        db_update "UPDATE pipeline_jobs SET status='done', phase='deploy_already_present',
                   error_detail=NULL, completed_at=NOW(), updated_at=NOW()
                   WHERE job_id='${job_id}' AND status='deploying' AND commit_hash='${expected_sha}';"
        if [[ "$(get_job_status "$job_id")" != "done" ]]; then
            _fail_job "$job_id" "$session_id" "deploy_already_present_persist_failed" "이미 반영된 승인 SHA 완료 기록 실패"
            _release_deploy_lock "$project" "$job_id"
            return 1
        fi
        record_runner_event "$job_id" "job_terminal" "done" "deploy_already_present" "" "" "" "" "{\"sha\":\"${current_sha}\"}"
        _release_deploy_lock "$project" "$job_id"
        _notify_ai "$job_id"
        promote_next_queued "$project"
        return 0
    fi
    if [[ "$project" == "AADS" ]]; then
        local pushed_remote_sha
        pushed_remote_sha=$(git -C "$worktree_dir" ls-remote origin refs/heads/main 2>/dev/null | awk 'NR==1 {print $1}') || pushed_remote_sha=""
        if [[ "$pushed_remote_sha" != "$expected_sha" || "$current_sha" != "$expected_sha" ]]; then
            _fail_job "$job_id" "$session_id" "deploy_isolated_remote_changed" "승인 SHA와 실제 origin/main 불일치: 배포 차단"
            _release_deploy_lock "$project" "$job_id"
            return 1
        fi
    fi
    db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

    # ── 지시서의 배포 금지 제약 강제 (AADS-RUNNER-DEPLOY-DIRECTIVE-GATE) ──
    # 2026-09-16 11:06 GO100 runner-1791da41 의 지시서에는 "커밋까지만, 배포·재기동
    # 절대 금지" 가 명시돼 있었는데 승인 즉시 push→빌드→배포가 돌았고, 그 배포가
    # 헬스체크에 실패해 11:09 에 P0 청산 안전장치를 자동 revert 시켰다.
    # 사람이 쓴 제약은 코드가 막지 않으면 지켜지지 않는다(R-ERRBOOK).
    local job_instruction="" _deploy_directive_state=""
    if ! job_instruction=$(read_job_instruction_strict "$job_id"); then
        _fail_job "$job_id" "$session_id" "deploy_directive_unverifiable" "지시서 조회 실패/빈 값 — 배포 금지 제약을 확인할 수 없어 빌드·배포를 중단 (push 는 완료됨, 재승인 필요)"
        _release_deploy_lock "$project" "$job_id"
        promote_next_queued "$project"
        return 1
    fi
    if instruction_forbids_deploy "$job_instruction"; then
        log "  DEPLOY_SKIPPED_BY_DIRECTIVE job=$job_id — 지시서가 배포를 금지함, push 까지만 수행"
        db_update "UPDATE pipeline_jobs SET status='done', phase='push_only_by_directive',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[게이트] 지시서의 배포 금지 제약에 따라 push 까지만 수행하고 빌드·배포를 건너뜀',
                   deployed_at=NULL, completed_at=NOW(), updated_at=NOW() WHERE job_id='${job_id}';"
        record_runner_event "$job_id" "job_terminal" "done" "push_only_by_directive" "" "" "" "" "{\"reason\":\"deploy_forbidden_by_instruction\"}"
        post_to_chat "$session_id" "✅ [Pipeline Runner] 지시서 제약에 따라 push 까지만 수행했습니다 (빌드·배포 건너뜀): $job_id — 릴리스가 필요하면 별도 승인 후 진행하십시오."
        _release_deploy_lock "$project" "$job_id"
        _notify_ai "$job_id"
        promote_next_queued "$project"
        return 0
    fi
    _deploy_directive_state="allowed:${job_id}"

    # ═══ 무중단 배포 v3.0 — build→swap→healthcheck→rollback ═══
    # 원칙: 빌드 중 기존 서비스 유지, 빌드 성공 후에만 교체, 실패 시 롤백
    local _build_fail=""
    local _aads_live_gate="" _aads_live_blocked=""
    # 빌드 직전 불변식: 위 게이트가 이 잡에 대해 "허용" 으로 끝났을 때만 빌드한다.
    # 게이트를 건너뛰는 경로가 생겨도(리팩터·분기 추가) 여기서 fail-closed 로 막힌다.
    if [[ "$_deploy_directive_state" != "allowed:${job_id}" ]]; then
        _fail_job "$job_id" "$session_id" "deploy_directive_gate_bypassed" "배포 금지 게이트 통과 기록 없이 빌드 단계에 도달 — 빌드·배포 중단"
        _release_deploy_lock "$project" "$job_id"
        promote_next_queued "$project"
        return 1
    fi

    case "$project" in
        AADS)
            # API 릴리스는 승인된 격리 worktree의 immutable blue/green 경로만
            # 사용한다. 공유 main worktree와 hot-reload는 릴리스 SHA/이미지 증거를
            # 분리시키므로 금지한다.
            if [[ "$target_repo" == "aads-dashboard" ]]; then
                log "  SKIP aads-server deploy — dashboard-targeted AADS job"
            else
                local _release_relevant="false"
                if [[ "$_py_changed" == "true" ]]; then
                    _release_relevant="true"
                elif git -C "$worktree_dir" diff-tree --no-commit-id --name-only -r "$current_sha" 2>/dev/null \
                    | grep -qE '^(app/|scripts/|migrations/|deploy\.sh|Dockerfile|docker-compose|requirements|pyproject\.toml)'; then
                    _release_relevant="true"
                fi

                if [[ "$_release_relevant" == "true" ]]; then
                    _aads_live_gate="required"
                    local _aads_deploy_log="/tmp/pipeline-deploy-aads-${job_id}.log"
                    local _aads_deploy_since _aads_follow_out="" _aads_follow_reason=""
                    # deploy_runs.created_at 과 비교한다 — 시계 오차를 감안해 5초 앞당긴다.
                    _aads_deploy_since=$(( $(date +%s) - 5 ))
                    log "  BLUEGREEN aads-server — approved isolated worktree=$worktree_dir"
                    if AADS_DEPLOY_FOREGROUND=1 \
                       AADS_DEPLOY_SOURCE_DIR="$worktree_dir" \
                       AADS_DEPLOY_STATE_DIR="$main_workdir" \
                       bash "$worktree_dir/deploy.sh" bluegreen >"$_aads_deploy_log" 2>&1; then
                        tail -20 "$_aads_deploy_log" 2>/dev/null || true
                        log "  BLUEGREEN aads-server 완료 — deploy.sh certification gates passed"
                    elif _aads_follow_out=$(follow_autoheal_successor "$job_id" "$current_sha" \
                            "$_aads_deploy_since" "$worktree_dir" "$main_workdir" "$project"); then
                        # deploy.sh 는 rc=1 이지만 같은 릴리스를 successor 가 끝까지 배포했다.
                        _aads_follow_out=$(tail -1 <<< "$_aads_follow_out")
                        log "  BLUEGREEN aads-server 완료 — autoheal successor 인계 성공: ${_aads_follow_out}"
                        db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[배포인계] deploy.sh rc≠0 이었으나 autoheal successor 가 같은 릴리스를 배포함 — ' || $(sql_escape "$_aads_follow_out") WHERE job_id='${job_id}';"
                        record_runner_event "$job_id" "deploy_autoheal_successor_succeeded" "deploying" "deploying" "" "" "" "" "{\"sha\":\"${current_sha}\"}"
                    else
                        _aads_follow_out=$(tail -1 <<< "$_aads_follow_out")
                        log "  AUTOHEAL_FOLLOW 결과: ${_aads_follow_out:-none}"
                        case "$_aads_follow_out" in
                            *deploy_lock_lost*|*deploy_lock_unreadable*)
                                _aads_follow_reason="deploy_lock_lost"
                                [[ "$_aads_follow_out" == *deploy_lock_unreadable* ]] && _aads_follow_reason="deploy_lock_unreadable"
                                record_runner_event "$job_id" "deploy_autoheal_follow_abandoned" "deploying" "deploying" "" "" "" "" "{\"sha\":\"${current_sha}\",\"reason\":\"${_aads_follow_reason}\"}" ;;
                        esac
                        local _aads_deploy_tail
                        _aads_deploy_tail=$(tail -20 "$_aads_deploy_log" 2>/dev/null | head -c 1500)
                        log "  ERROR: isolated bluegreen 실패 — 기존 라우팅 유지/내부 롤백: ${_aads_deploy_tail//$'\n'/ }"
                        post_to_chat "$session_id" "🔴 [Runner] AADS bluegreen 배포 실패 — 기존 서비스 유지: ${_aads_deploy_tail:0:500}"
                        _build_fail="${_build_fail:+${_build_fail};}aads-server:bluegreen_failed"
                        db_update "UPDATE pipeline_jobs SET review_feedback=COALESCE(review_feedback,'') || E'\n[배포실패:aads-server-bluegreen] ' || $(sql_escape "[${_aads_follow_out:-autoheal_follow: none}] ${_aads_deploy_tail}") WHERE job_id='${job_id}';"
                    fi
                    rm -f "$_aads_deploy_log" 2>/dev/null || true
                else
                    log "  SKIP-DEPLOY: 런타임 무관 변경 — aads-server 릴리스 불필요 (docs/tests only)"
                fi
            fi
            # HEARTBEAT: aads-server 배포 완료 후 갱신 (dashboard 빌드 전)
            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

            # 2) aads-dashboard: Docker 이미지 빌드 서비스 → build→swap
            if [[ "$target_repo" == "aads-dashboard" ]]; then
                DASHBOARD_CHANGED=true
            elif [ -n "$(git -C /root/aads/aads-dashboard status --porcelain 2>/dev/null)" ]; then
                # 대시보드 공유 워킹트리가 더럽다. **이 잡이 만든 것인지** 부터 가른다.
                #
                # 2026-09-16: runner-168bac82(AADS-AAG-DEBT-002) 는 대시보드 파일을
                # 하나도 건드리지 않았는데, 다른 세션이 남긴 UsageBar.tsx 미커밋 1건
                # 때문에 build_fail 이 됐다. 백엔드 push 는 이미 성공한 뒤였다.
                # 남이 남긴 dirty 로 남의 잡을 죽이면, 그 파일이 커밋될 때까지
                # **모든 AADS 백엔드 배포가 인질이 된다.**
                #
                # 판정: 이 잡이 시작한 뒤에 바뀐 dirty 파일이 하나라도 있으면 이 잡을
                # 의심하고 막는다(원래 의도 — 공유 워크트리에 직접 쓴 잡을 잡는다).
                # 시작 전부터 더러웠으면 빌드만 건너뛰고 실패로 보지 않는다.
                # 시작 시각을 못 읽으면 예전처럼 막는다 — 모르면 안전한 쪽.
                local _dash_dirty _dash_started _dash_recent=0 _dash_f _dash_m
                _dash_dirty=$(git -C /root/aads/aads-dashboard status --porcelain 2>/dev/null \
                    | sed 's/^...//' | head -20 | tr '\n' ',' | sed 's/,$//')
                _dash_started=$(db_exec "SELECT COALESCE(EXTRACT(EPOCH FROM started_at)::bigint,0) FROM pipeline_jobs WHERE job_id='${job_id}';" 2>/dev/null | tr -cd '0-9')
                if [[ "${_dash_started:-0}" -gt 0 ]]; then
                    while IFS= read -r _dash_f; do
                        [[ -n "$_dash_f" ]] || continue
                        [[ -e "/root/aads/aads-dashboard/${_dash_f}" ]] || continue
                        _dash_m=$(stat -c %Y "/root/aads/aads-dashboard/${_dash_f}" 2>/dev/null || echo 0)
                        [[ "${_dash_m:-0}" -ge "$_dash_started" ]] && _dash_recent=$((_dash_recent + 1))
                    done < <(git -C /root/aads/aads-dashboard status --porcelain 2>/dev/null | sed 's/^...//')
                fi
                if [[ "${_dash_started:-0}" -le 0 || "${_dash_recent:-0}" -gt 0 ]]; then
                    log "  BLOCK aads-dashboard shared worktree changes — 이 잡 이후 변경 ${_dash_recent}건: ${_dash_dirty}"
                    _build_fail="${_build_fail:+${_build_fail};}aads-dashboard:isolated_worktree_required"
                else
                    log "  WARN aads-dashboard dirty — 이 잡과 무관(시작 전부터 미커밋), 빌드 생략하고 실패로 보지 않음: ${_dash_dirty}"
                fi
                DASHBOARD_CHANGED=false
            else
                DASHBOARD_CHANGED=false
            fi

            if [ "$DASHBOARD_CHANGED" = true ]; then
                log "  BLUEGREEN aads-dashboard — deploy.sh 호출 (헬스체크+롤백)"
                local _dash_deploy_log="/tmp/pipeline-deploy-dashboard-${job_id}.log"
                if AADS_RELEASE_SHA="${current_sha:0:12}" \
                   bash /root/aads/aads-dashboard/deploy.sh >"$_dash_deploy_log" 2>&1; then
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
                    "${AADS_API_URL}/api/v1/visual-qa/full-qa" 2>/dev/null) || true

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
                    # 정본 무중단 경로: 비활성 슬롯 staging 빌드 → 산출물 검증 → health → nginx 전환.
                    # go100-frontend.service 는 masked 라 systemctl restart 는 no-op 이었고,
                    # frontend/.next 는 존재하지 않아 BUILD_ID 비교도 발화할 수 없었다 (2026-09-30).
                    local _fe_bg_script="${GO100_REPO_DIR}/scripts/deploy_frontend_blue_green.sh"
                    if [ ! -f "$_fe_bg_script" ]; then
                        # npx 직접 빌드로 폴백하지 않는다 — 활성 슬롯에 닿지 않는 경로다.
                        log "  ERROR: go100-frontend blue-green 스크립트 없음: $_fe_bg_script"
                        post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 blue-green 스크립트 없음 — 배포 안 됨"
                        _build_fail="${_build_fail:+${_build_fail};}go100-frontend:bluegreen_script_missing"
                    else
                        # 공유 런타임 워크트리(상시 dirty, HEAD 가 origin/main 과 다를 수 있음)가 아니라
                        # 방금 push 한 SHA(current_sha)의 clean 워크트리에서 돌린다 (AADS-RUNNER-GO100-FE-CLEAN-WORKTREE).
                        # 생성 실패 시 런타임 워크트리로 폴백하지 않는다.
                        local _fe_release_dir="" _fe_release_errf=""
                        _fe_release_errf=$(mktemp "/tmp/go100-release-wt-${job_id}.XXXXXX")
                        go100_prune_release_worktrees "$GO100_REPO_DIR" "$(( GO100_RELEASE_WORKTREE_KEEP > 0 ? GO100_RELEASE_WORKTREE_KEEP - 1 : 0 ))"
                        if ! _fe_release_dir=$(go100_create_frontend_release_worktree "$GO100_REPO_DIR" "$current_sha" 2>"$_fe_release_errf") \
                            || [ -z "$_fe_release_dir" ]; then
                            tail -20 "$_fe_release_errf" | tee -a "$LOG_DIR/runner.log" || true
                            log "  ERROR: go100-frontend release worktree 생성 실패 (sha=${current_sha}) — 런타임 워크트리 폴백 안 함"
                            post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 clean 릴리스 워크트리 생성 실패 (sha=${current_sha:0:9}) — 배포 안 됨: $(tail -1 "$_fe_release_errf" 2>/dev/null | head -c 300)"
                            _build_fail="${_build_fail:+${_build_fail};}go100-frontend:release_worktree_failed"
                        else
                            sed 's/^/  /' "$_fe_release_errf" | tee -a "$LOG_DIR/runner.log" || true
                            _fe_bg_script="${_fe_release_dir}/scripts/deploy_frontend_blue_green.sh"
                            # HEARTBEAT: GO100 프론트엔드 BG 배포 시작 직전 갱신
                            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
                            log "  ZERO-DOWNTIME go100-frontend blue-green start (changed: $(echo "$_fe_changed" | wc -l) files, sha=${current_sha}, release=${_fe_release_dir})"
                            local _fe_bg_rc=0 _fe_bg_outf="" _fe_bg_label=""
                            # 전체 출력을 파일에 받아 분류하고, runner.log 에는 마지막 30줄만 남긴다.
                            # (tail -30 을 분류보다 앞에 두면 초반의 Deploy Gate 차단 메시지가 잘려 나간다.)
                            # 파일로 리다이렉트하므로 rc 는 BG 스크립트의 것이 그대로 남는다.
                            # .next-build-cache 시드·회수와 슬롯 교체는 GO100_RUNTIME_WORKDIR(런타임) 기준으로 그대로 돈다.
                            _fe_bg_outf=$(mktemp "/tmp/go100-fe-bg-${job_id}.XXXXXX")
                            if GO100_RELEASE_WORKDIR="$_fe_release_dir" GO100_RUNTIME_WORKDIR="$GO100_REPO_DIR" \
                                bash "$_fe_bg_script" --apply >"$_fe_bg_outf" 2>&1; then
                                _fe_bg_rc=0
                            else
                                _fe_bg_rc=$?
                            fi
                            tail -30 "$_fe_bg_outf" | tee -a "$LOG_DIR/runner.log" || true
                            _fe_bg_label=$(go100_fe_bg_classify "$_fe_bg_rc" "$_fe_bg_outf") || _fe_bg_label=""
                            rm -f "$_fe_bg_outf"
                            # HEARTBEAT: GO100 프론트엔드 BG 배포 종료 직후 갱신
                            db_update "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"
                            if [ -n "$_fe_bg_label" ]; then
                                log "  ERROR: go100-frontend blue-green 배포 실패 (rc=$_fe_bg_rc label=${_fe_bg_label})"
                                post_to_chat "$session_id" "🔴 [Runner] $(go100_fe_failure_chat_text "$_fe_bg_label" "$_fe_bg_rc")"
                                _build_fail="${_build_fail:+${_build_fail};}${_fe_bg_label}"
                            fi
                        fi
                        rm -f "$_fe_release_errf"
                        # 성공·실패와 무관하게 최근 N 개만 남긴다(큐 워커가 쓰는 워크트리는 in_use 로 보존).
                        go100_prune_release_worktrees "$GO100_REPO_DIR" "$GO100_RELEASE_WORKTREE_KEEP"
                    fi

                    # 번들 신선도 게이트: URL 200 이 아니라 활성 슬롯 BUILD_ID 가 병합 커밋보다 새것인지 본다.
                    if [[ "$_build_fail" != *"go100-frontend:"* ]]; then
                        local _fresh_out="" _fresh_status="" _fresh_color="" _fresh_bid="" _fresh_built="" _fresh_commit=""
                        _fresh_out=$(verify_go100_bundle_fresh "$GO100_REPO_DIR" "$GO100_NGINX_CONF") || _fresh_out=""
                        IFS='|' read -r _fresh_status _fresh_color _fresh_bid _fresh_built _fresh_commit <<< "$_fresh_out" || true
                        local _fresh_built_kst="-" _fresh_commit_kst="-"
                        [[ "$_fresh_built" =~ ^[0-9]+$ ]] && _fresh_built_kst=$(TZ=Asia/Seoul date -d "@$_fresh_built" '+%Y-%m-%d %H:%M:%S KST' 2>/dev/null || echo "$_fresh_built")
                        [[ "$_fresh_commit" =~ ^[0-9]+$ ]] && _fresh_commit_kst=$(TZ=Asia/Seoul date -d "@$_fresh_commit" '+%Y-%m-%d %H:%M:%S KST' 2>/dev/null || echo "$_fresh_commit")
                        case "$_fresh_status" in
                            fresh)
                                log "  go100-frontend bundle fresh (color=${_fresh_color} BUILD_ID=${_fresh_bid} built=${_fresh_built_kst})"
                                ;;
                            stale)
                                log "  ERROR: go100-frontend stale bundle (color=${_fresh_color} BUILD_ID=${_fresh_bid} built=${_fresh_built_kst} < commit=${_fresh_commit_kst})"
                                post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 구번들 서빙 중 — color=${_fresh_color} BUILD_ID=${_fresh_bid} built=${_fresh_built_kst} < commit=${_fresh_commit_kst}"
                                _build_fail="${_build_fail:+${_build_fail};}go100-frontend:stale_bundle"
                                ;;
                            build_id_missing)
                                log "  ERROR: go100-frontend BUILD_ID 없음 (color=${_fresh_color} dir=${_fe_dir}/.next.${_fresh_color})"
                                post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 활성 슬롯(${_fresh_color}) BUILD_ID 없음"
                                _build_fail="${_build_fail:+${_build_fail};}go100-frontend:build_id_missing"
                                ;;
                            commit_epoch_unknown)
                                log "  ERROR: go100-frontend 신선도 판정 불가 (color=${_fresh_color} built=${_fresh_built:-?} commit=${_fresh_commit:-?})"
                                post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 번들 신선도 판정 불가 (commit/BUILD_ID 시각 조회 실패)"
                                _build_fail="${_build_fail:+${_build_fail};}go100-frontend:freshness_unknown"
                                ;;
                            *)
                                log "  ERROR: go100-frontend 활성 슬롯 판정 불가 (${GO100_NGINX_CONF} upstream go100_frontend)"
                                post_to_chat "$session_id" "🔴 [Runner] GO100 프론트엔드 활성 슬롯 판정 불가 — 번들 신선도 미확인"
                                _build_fail="${_build_fail:+${_build_fail};}go100-frontend:active_slot_unknown"
                                ;;
                        esac
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
        AADS)   health_url="${AADS_API_URL}/api/v1/health" ;;
        KIS)    health_url="http://localhost:8003/health" ;;
        GO100)  health_url="http://localhost:8002/health" ;;
        SF)     health_url="http://localhost:8000/health" ;;
        NTV2)   health_url="http://localhost:8080" ;;
    esac

    # ── 예산 기반 헬스체크 (AADS-RUNNER-HEALTH-BUDGET, 2026-09-16) ──────────
    # 기존은 sleep 10 × 3 = 최대 약 63초였다. 그런데 실측은 이렇다:
    #   11:06:34 systemctl restart go100
    #   11:06:37 gunicorn Listening at 127.0.0.1:8002   ← 소켓은 3초 만에 열린다
    #   11:07:53 uvicorn "Application startup complete"  ← 앱은 79초 걸린다
    # 헬스체크는 11:06:56 / 11:07:16 / 11:07:37 에 돌고 11:07:43 에 FAIL 로 단정했다.
    # **준비 완료 10초 전에 포기하고** 승인된 GO100 P0 청산 안전장치를 git revert 로
    # 되돌렸다(runner-1791da41). 소켓이 열리는 시점과 앱이 준비되는 시점은 다르다.
    # 예산을 넉넉히 두고 짧게 폴링하면 빠른 서비스는 오히려 더 빨리 통과한다
    # (첫 확인이 10초 뒤 → 5초 뒤).
    local health_budget_sec="${HEALTH_CHECK_BUDGET_SEC:-180}"
    local health_interval_sec="${HEALTH_CHECK_INTERVAL_SEC:-5}"
    if [[ -n "$health_url" ]]; then
        health_ok="FAIL"
        local _waited=0
        while (( _waited < health_budget_sec )); do
            sleep "$health_interval_sec"
            _waited=$(( _waited + health_interval_sec ))
            if curl -sf -m 10 -o /dev/null "$health_url"; then
                health_ok="OK"
                log "  HEALTH_OK job=$job_id after=${_waited}s url=$health_url"
                break
            fi
            if (( _waited % 30 == 0 )); then
                log "  헬스체크 대기 ${_waited}/${health_budget_sec}s job=$job_id url=$health_url"
            fi
        done
        [[ "$health_ok" == "OK" ]] || log "  HEALTH_FAIL job=$job_id budget=${health_budget_sec}s url=$health_url"
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
                        # 롤백/revert 커밋도 격리 worktree에서 동일한 인증 경로로 배포한다.
                        if AADS_DEPLOY_FOREGROUND=1 \
                           AADS_DEPLOY_SOURCE_DIR="$worktree_dir" \
                           AADS_DEPLOY_STATE_DIR="$main_workdir" \
                           bash "$worktree_dir/deploy.sh" bluegreen 2>&1 | tail -10; then
                            log "  ROLLBACK_DEPLOY: isolated bluegreen 성공"
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

    # 배포 반영 게이트: AADS backend 릴리스를 시도한 잡만, 빌드 실패가 없을 때만 검사한다.
    # (대시보드·비-AADS 경로는 건드리지 않는다.)
    if [[ "$_aads_live_gate" == "required" && -z "$_build_fail" ]]; then
        aads_deploy_live_gate "$job_id" "$session_id" "$current_sha" "$worktree_dir" "$main_workdir" \
            || _aads_live_blocked="true"
    fi

    # 최종 판정: 빌드 실패 플래그가 있으면 성공 처리 금지
    if [[ "$_aads_live_blocked" == "true" ]]; then
        :  # aads_deploy_live_gate 가 error(deploy_not_live) 또는 deploying(deploy_queued) 을 이미 기록했다.
    elif [[ -n "$_build_fail" ]]; then
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
    if ! workdir=$(resolve_project_workdir "$project" "$_job_instruction"); then
        fail_invalid_aads_target "$job_id" "$session_id"
        return 1
    fi
    [[ -z "$workdir" || ! -d "$workdir" ]] && return 1

    log "▶ REJECT job=$job_id project=$project workdir=$workdir"
    cd "$workdir"

    # v2.2: 해당 Runner의 변경사항만 선택적 원복 (다른 Runner의 배포된 변경 보호)
    local worktree_dir="/tmp/aads-wt-${job_id}"
    if [[ -d "$worktree_dir" ]]; then
        _preserve_worktree_patch "$job_id" "$worktree_dir"
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

    # deploy_queued 잡은 아래 BUG-7(deploying 20분) 보다 먼저 같은 판정으로 마무리한다.
    aads_finalize_deploy_queued_jobs || true

    # BUG-7: 좀비 작업 강제 kill — running 상태 + MAX_RUNTIME(7200초) 초과 + runner_pid 존재
    local zombie_rows
    zombie_rows=$(db_exec "SELECT job_id, runner_pid, chat_session_id, project
                           FROM pipeline_jobs
                           WHERE status='running'
                             AND runner_pid IS NOT NULL
                             AND started_at IS NOT NULL
                             AND started_at < NOW() - INTERVAL '${MAX_RUNTIME} seconds'
                             AND updated_at < NOW() - INTERVAL '10 minutes'
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
                                  AND phase IS DISTINCT FROM 'deploy_queued'
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

    # review_hold 복구 커밋이 러너 재시작/크래시로 중단된 경우.
    # 표시만 남고 아무도 집지 않는 상태로 영원히 두지 않는다 — 산출물은
    # 워크트리에 그대로 있으므로 종결해도 되살릴 수 있다.
    local hold_recovery_stalled
    hold_recovery_stalled=$(db_exec "UPDATE pipeline_jobs SET status='error', phase='review_hold_recovery_failed',
                                     error_detail='review_hold_recovery_stalled',
                                     review_feedback=COALESCE(review_feedback,'') || E'\n[Runner] review_hold 복구 커밋 30분 초과 — 종결(워크트리 산출물은 보존)',
                                     completed_at=NOW(), updated_at=NOW()
                                     WHERE status='review_hold'
                                       AND error_detail='review_hold_recovery_committing'
                                       AND updated_at < NOW() - INTERVAL '30 minutes'
                                       $filter
                                     RETURNING job_id;" 2>/dev/null) || true
    if [[ -n "$hold_recovery_stalled" ]]; then
        log "  REVIEW_HOLD_RECOVERY_STALLED: $hold_recovery_stalled"
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

    # STALE_WORKTREE_CLEANUP: delegate runner worktrees to the shared guard.
    # It checks job status, ignored files, unmerged commits, and active users.
    local _reclaim_script
    _reclaim_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/reclaim_runner_worktrees.sh"
    if [[ -x "$_reclaim_script" ]]; then
        timeout 90 bash "$_reclaim_script" || true
    else
        _warn_reclaimer_unavailable "$_reclaim_script"
    fi
}

# 회수 스크립트는 러너와 같은 디렉터리에 있어야 한다(sync_pipeline_runner_remote.sh 가 함께 배치).
_reclaimer_script_path() {
    printf '%s/reclaim_runner_worktrees.sh' "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
}

# 회수 스크립트 부재 경고. 2026-10-05 contabo14 는 이 스크립트가 없어 5분 주기 정리마다
# "reclaimer unavailable" 이 조용히 181회/일 찍혔고 RUNNER_WT_MAX_KEEP 상한이 한 번도 돌지 않았다.
# 결함은 지속되므로 주기마다 알리면 스팸이다 — 로그·텔레그램·오류사전을 RECLAIMER_WARN_INTERVAL_MIN(기본 24h)에 한 번만 낸다.
_warn_reclaimer_unavailable() {
    local script="$1"
    local flag="${RECLAIMER_MISSING_FLAG:-/tmp/aads-runner-reclaimer-missing.flag}"
    local interval="${RECLAIMER_WARN_INTERVAL_MIN:-1440}"
    [[ "$interval" =~ ^[0-9]+$ ]] || interval=1440
    if [[ -f "$flag" && -z "$(find "$flag" -mmin +"$interval" 2>/dev/null)" ]]; then
        return 0
    fi
    : > "$flag" 2>/dev/null || true
    local msg="STALE_WORKTREE_CLEANUP: reclaimer unavailable: ${script}"
    log "  WARN ${msg} — worktree 수량 상한(RUNNER_WT_MAX_KEEP)·보존기간 정리·즉시 회수가 동작하지 않는다. sync_pipeline_runner_remote.sh 로 같은 디렉터리에 배치하라 (이 경고는 ${interval}분에 한 번)"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -s -m 10 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=⚠️ [Runner] ${RUNNER_HOST_NAME:-$(hostname -s)}: ${msg} — worktree 회수 정지 (디스크 누적 위험)" \
            -d "parse_mode=HTML" >/dev/null 2>&1 || true
    fi
    local err_file
    err_file=$(mktemp /tmp/reclaimer-missing.XXXXXX 2>/dev/null) || return 0
    printf '%s (host=%s)\n' "$msg" "${RUNNER_HOST_NAME:-unknown}" > "$err_file"
    lookup_error_book "$err_file" "reclaimer-missing"
    rm -f "$err_file"
}

# 종료한 job 의 worktree 가 깨끗하면 즉시 회수한다 (AADS-RUNNER-WT-RECLAIM-ON-FINISH).
# 판정은 reclaim_runner_worktrees.sh 의 즉시 회수 모드가 한다 — 종료 상태 여부, 미커밋/ignored 변경,
# origin/main 에 없는 커밋, 열린 프로세스. 하나라도 걸리면 보존하고 기존 24h 정책에 맡긴다.
# awaiting_approval·review_hold 등 비종료 상태는 DB 상태 확인에서 걸러진다(승인 시 커밋에 필요).
_reclaim_finished_worktree() {
    local job_id="$1" status script out line
    [[ "${RUNNER_WT_IMMEDIATE_RECLAIM:-1}" == "1" ]] || return 0
    [[ "$job_id" =~ ^runner-[0-9a-zA-Z_-]+$ ]] || return 0
    [[ -d "/tmp/aads-wt-${job_id}" ]] || return 0
    status=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id=$(sql_escape "$job_id");" 2>/dev/null | tr -d '[:space:]') || status=""
    case "$status" in
        done|error|cancelled|rejected_done|failed) ;;
        *)
            log "  WORKTREE_RECLAIM_SKIP job=$job_id status=${status:-unknown} (비종료 상태 — 보존)"
            return 0
            ;;
    esac
    script="$(_reclaimer_script_path)"
    if [[ ! -x "$script" ]]; then
        _warn_reclaimer_unavailable "$script"
        return 0
    fi
    out=$(RUNNER_WT_ONLY_JOB="$job_id" RUNNER_WT_ONLY_STATUS="$status" timeout 45 bash "$script" 2>&1) || true
    while IFS= read -r line; do
        [[ -n "$line" ]] && log "  WORKTREE_RECLAIM job=$job_id ${line}"
    done <<< "$out"
    return 0
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
# ── 스크립트 변경 시 자기 재적용 (RUNNER-SELF-RELOAD) ────────────────
# 러너는 몇 시간에서 며칠을 도는 bash 프로세스다. bash 는 기동 시 함수 정의를
# 전부 파싱하므로, 릴리스가 이 파일을 갈아도 **이미 돌고 있는 러너는 옛 코드로
# 계속 돈다.** 2026-09-29 이 함정으로 하루를 잃었다 — 배포 게이트 연쇄 차단
# 교정(ae3c1712)이 09:46 KST 에 main 에 들어갔는데, 데몬은 08:00 KST 기동본
# 이었고 그 뒤로도 승인 4건이 전부 같은 `deploy_isolated_stale_approval` 로
# 죽었다. 파일을 고치고 "고쳤다"고 보고하는 동안 운영은 옛 코드였다.
#
# 그래서 유휴 시점에 스스로 갈아끼운다. 조건 셋을 모두 만족할 때만 한다.
#   1) 진행 중 작업이 0건 — 작업 중에 바꾸면 자식 프로세스가 고아가 된다.
#   2) 새 파일이 `bash -n` 을 통과 — 쓰다 만 파일로 자살하지 않는다.
#   3) 싱글턴 lock FD 9 를 먼저 닫는다 — flock 은 같은 프로세스의 다른 FD
#      에도 걸리므로, 닫지 않고 exec 하면 새 인스턴스가 자기 락에 막힌다.
# 끄는 법: RUNNER_SELF_RELOAD=0
RUNNER_SELF_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
RUNNER_SELF_FINGERPRINT=""
RUNNER_SELF_ARGV=()

_runner_self_fingerprint() {
    [[ -f "$RUNNER_SELF_PATH" ]] || return 1
    sha256sum "$RUNNER_SELF_PATH" 2>/dev/null | awk '{print $1}'
}

maybe_reexec_on_self_change() {
    # Protocol v1 applies immutable files only in a controller transaction.
    # Self-exec must not release/reacquire the lifetime lease under admission EX.
    [[ "${RUNNER_MAINTENANCE_ACTIVE:-0}" == 1 ]] && return 0
    [[ "${RUNNER_SELF_RELOAD:-1}" == "1" ]] || return 0
    [[ -n "$RUNNER_SELF_FINGERPRINT" ]] || return 0
    (( ${#_bg_jobs[@]} == 0 )) || return 0
    [[ -z "${_current_job_id:-}" ]] || return 0
    local _now=""
    _now=$(_runner_self_fingerprint) || return 0
    [[ -n "$_now" && "$_now" != "$RUNNER_SELF_FINGERPRINT" ]] || return 0
    if ! bash -n "$RUNNER_SELF_PATH" 2>/dev/null; then
        log "  SELF_RELOAD_SKIP: 새 스크립트 구문 오류 — 현재 코드 유지 (${_now:0:12})"
        RUNNER_SELF_FINGERPRINT="$_now"
        return 0
    fi
    log "  SELF_RELOAD: 스크립트 변경 감지 — exec 로 재적용 (${RUNNER_SELF_FINGERPRINT:0:12} → ${_now:0:12})"
    exec 9>&- || true
    exec bash "$RUNNER_SELF_PATH" "${RUNNER_SELF_ARGV[@]+"${RUNNER_SELF_ARGV[@]}"}"
}

main() {
    RUNNER_SELF_ARGV=("$@")
    _init_db_mode
    log "═══ Pipeline Runner v2.1 시작 (mode=${RUNNER_ENGINE_MODE}, 승인→커밋→푸시→빌드→배포) poll=${POLL_INTERVAL}s, max_runtime=${MAX_RUNTIME}s, retries=${MAX_RETRIES} ═══"
    if [[ -n "${MAX_CONCURRENT_SERVER:-}" ]]; then
        [[ "$MAX_CONCURRENT_SERVER" =~ ^[1-9][0-9]{0,2}$ && -n "${RUNNER_PROJECTS:-}" ]] || {
            log "ERROR: server capacity requires a positive limit and RUNNER_PROJECTS"
            return 1
        }
        log "SERVER_CAPACITY limit=${MAX_CONCURRENT_SERVER} projects=${RUNNER_PROJECTS} per_project=${MAX_CONCURRENT_PER_PROJECT} (shared across engines)"
    fi

    runner_heartbeat
    log "RUNNER_HOST=${RUNNER_HOST_NAME}"
    RUNNER_SELF_FINGERPRINT=$(_runner_self_fingerprint) || RUNNER_SELF_FINGERPRINT=""
    log "RUNNER_SELF=${RUNNER_SELF_PATH} fingerprint=${RUNNER_SELF_FINGERPRINT:0:12} self_reload=${RUNNER_SELF_RELOAD:-1}"

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
            # SIGKILL 등으로 cleanup 트랩이 돌지 못한 경우의 복구 경로.
            # error 확정 대신 큐로 되돌려 자동 복구시킨다(재큐잉 한도는 helper가 판단).
            _shutdown_finalize_job "$prev_job" "" quiet
            log "WARN: 이전 running 작업 $prev_job 재큐잉 처리 (러너 재시작)"
        fi
        rm -f /tmp/.pipeline_current_job
    fi

    # C3: 시작 시 stuck 작업 복구
    _recover_stuck_jobs "$project_filter"
    reclaim_orphan_claims

    local _cycle=0
    # BUG-7: STUCK_CHECK_INTERVAL(기본 300초/5분) 기반 동적 cycle 계산
    local _stuck_check_cycles
    _stuck_check_cycles=$(( STUCK_CHECK_INTERVAL / POLL_INTERVAL ))
    [[ "$_stuck_check_cycles" -lt 1 ]] && _stuck_check_cycles=1
    log "STUCK_CHECK_INTERVAL=${STUCK_CHECK_INTERVAL}s → 매 ${_stuck_check_cycles} cycle마다 감지"
    while true; do
        # Poll admission before throttle, claims, recovery, or self reload. A
        # maintenance controller keeps us alive in QUIESCENT; no SIGTERM/requeue.
        runner_maintenance_checkpoint_or_hold
        # Legacy hosts retain the global limit. Hosts with an explicit server
        # budget enforce it atomically at claim time and keep servicing reviews.
        if [[ -z "${MAX_CONCURRENT_SERVER:-}" ]]; then
        # 기본은 이 호스트(runner_host)가 쥔 건수만 센다. 예전에는 전 서버 합계를
        # MAX_CONCURRENT_GLOBAL 과 비교해, contabo14 상한을 20 으로 올리자 AADS 러너가
        # 자기 실행 1건으로 THROTTLE 10/10 에 굶었다(2026-10-02). 전 서버 합계는
        # RUNNER_THROTTLE_SCOPE=global 로 명시한 경우에만 쓴다.
        local _running_count _throttle_host_filter=""
        if [[ "${RUNNER_THROTTLE_SCOPE:-host}" != "global" ]]; then
            _throttle_host_filter="AND runner_host=$(sql_escape "$RUNNER_HOST_NAME")"
        fi
        _running_count=$(db_exec "SELECT count(*) FROM pipeline_jobs WHERE status IN ('running','claimed') ${_throttle_host_filter};" 2>/dev/null) || _running_count="0"
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
        fi

        # 방안A: 완료된 백그라운드 작업 정리
        _reap_bg_jobs

        # 유휴 시점에 스크립트가 바뀌었으면 새 코드로 갈아끼운다 (RUNNER-SELF-RELOAD)
        maybe_reexec_on_self_change

        # 선행 작업이 실패/거부/누락된 queued 작업은 claim 전에 terminal 상태로 정리
        cleanup_blocked_dependencies

        # 1) queued 작업 원자적 클레임 (C4)
        local pending
        pending=$(claim_queued_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$pending" ]]; then
            dispatch_claimed_job "$pending"
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

        # 4) review_hold dirty 산출물 복구 — 스위퍼가 diff 동일성을 확정한 건만
        local hold_recovery
        hold_recovery=$(claim_review_hold_recovery_job "$project_filter" 2>/dev/null) || true

        if [[ -n "$hold_recovery" ]]; then
            IFS=$'\x1e' read -r job_id project session_id <<< "$hold_recovery"
            if [[ -n "$job_id" && -n "$project" ]]; then
                recover_review_hold_job "$job_id" "$project" "$session_id" &
                _bg_jobs[$!]="${job_id}|${session_id}"
                log "  BG_HOLD_RECOVERY: job=$job_id pid=$! (parallel)"
            fi
        fi

        # 주기적 정리 (STUCK_CHECK_INTERVAL 초마다 — BUG-7: 동적 주기)
        _cycle=$((_cycle + 1))
        if (( _cycle % _stuck_check_cycles == 0 )); then
            runner_heartbeat
            _recover_stuck_jobs "$project_filter"
            reclaim_orphan_claims
            _watchdog_check "$project_filter"
            _cleanup_old_artifacts
            _check_runtime_alerts "$project_filter"
            cleanup_blocked_dependencies
        fi

        # P2-2: 적응형 폴링 — 작업 발견 시 즉시 재폴링, 유휴 시에만 대기
        if [[ -n "$pending" || -n "$approved" || -n "$rejected" || -n "$hold_recovery" ]]; then
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
            _reclaim_finished_worktree "${_bg_jobs[$_pid]%%|*}"
            unset '_bg_jobs[$_pid]'
        fi
    done
}

# ── 시그널 핸들링 ────────────────────────────────────────────────────
_current_job_id=""
_current_session_id=""
# ── 러너 종료 시 작업 마감 정책 (RUNNER-SHUTDOWN-REQUEUE) ───────────
# systemctl restart 등으로 러너가 종료되면 진행 중 작업을 error로 확정하지 않고
# 큐로 되돌려 재시작 후 자동 복구시킨다. 동일 작업이 SHUTDOWN_REQUEUE_MAX회
# 이상 종료에 휘말리면 무한 재큐잉을 막기 위해 error로 확정한다.
SHUTDOWN_REQUEUE_MARK='[RUNNER_SHUTDOWN_REQUEUE]'
SHUTDOWN_REQUEUE_MAX="${SHUTDOWN_REQUEUE_MAX:-2}"

# RUNNER_RESTART_REQUEUE_APPLIED
_shutdown_finalize_job() {
    local _jid="$1" _sid="${2:-}" _quiet="${3:-}"
    [[ -z "$_jid" ]] && return 0
    # 배포 락 대기 중 종료된 작업은 코딩을 다시 돌리지 않고 승인 상태로 되돌린다 —
    # approved 클레임이 배포만 다시 시도한다 (AADS-LLM-M6-DEPLOY-REGRESSION-GUARD).
    db_update "UPDATE pipeline_jobs SET status='approved', phase='approved', updated_at=NOW()
               WHERE job_id=$(sql_escape "$_jid") AND status='queued' AND phase='${DEPLOY_LOCK_WAIT_PHASE}';" || true
    local _marks
    _marks=$(db_exec "SELECT (length(COALESCE(review_feedback,'')) - length(replace(COALESCE(review_feedback,''), $(sql_escape "$SHUTDOWN_REQUEUE_MARK"), ''))) / ${#SHUTDOWN_REQUEUE_MARK} FROM pipeline_jobs WHERE job_id=$(sql_escape "$_jid");" 2>/dev/null | tr -d '[:space:]')
    [[ "$_marks" =~ ^[0-9]+$ ]] || _marks=0
    if (( _marks >= SHUTDOWN_REQUEUE_MAX )); then
        db_update "UPDATE pipeline_jobs SET status='error', phase='error',
                   error_detail='runner_shutdown',
                   review_feedback=COALESCE(review_feedback,'') || E'\n[Runner 종료로 중단] 자동 재큐잉 한도 초과',
                   completed_at=NOW(), updated_at=NOW() WHERE job_id=$(sql_escape "$_jid") AND status IN ('running','claimed');" || true
        record_runner_event "$_jid" "job_terminal" "error" "error" "" "" "" "" "{\"error_detail\":\"runner_shutdown\"}"
        log "  Marked $_jid as error (runner shutdown, requeue limit ${SHUTDOWN_REQUEUE_MAX})"
        [[ "$_quiet" == "quiet" ]] || post_to_chat "$_sid" "🔴 [Pipeline Runner] 러너 종료로 작업 중단(자동 재큐잉 한도 초과): $_jid"
        _notify_ai "$_jid"
        return 0
    fi
    db_update "UPDATE pipeline_jobs SET status='queued', phase='queued',
               started_at=NULL, completed_at=NULL, error_detail=NULL,
               review_feedback=COALESCE(review_feedback,'') || E'\n${SHUTDOWN_REQUEUE_MARK} 러너 종료로 중단되어 자동 재큐잉',
               updated_at=NOW() WHERE job_id=$(sql_escape "$_jid") AND status IN ('running','claimed');" || true
    record_runner_event "$_jid" "job_requeued" "queued" "queued" "" "" "" "" "{\"error_detail\":\"runner_shutdown_requeued\"}"
    log "  Requeued $_jid (runner shutdown)"
    [[ "$_quiet" == "quiet" ]] || post_to_chat "$_sid" "🔄 [Pipeline Runner] 러너 종료로 중단 → 자동 재큐잉: $_jid"
}

cleanup() {
    log "═══ Pipeline Runner v2.1 종료 ═══"
    # 방안A: 모든 백그라운드 작업 정리
    for _pid in "${!_bg_jobs[@]}"; do
        IFS='|' read -r _jid _sid <<< "${_bg_jobs[$_pid]}"
        kill "$_pid" 2>/dev/null || true
        wait "$_pid" 2>/dev/null || true
        _shutdown_finalize_job "$_jid" "$_sid"
    done
    # 레거시 호환: 단일 작업 추적
    if [[ -n "$_current_job_id" ]] && ! printf '%s\n' "${_bg_jobs[@]}" | grep -q "$_current_job_id"; then
        _shutdown_finalize_job "$_current_job_id" "$_current_session_id"
    fi
    exit 0
}

trap cleanup SIGTERM SIGINT

main "$@"
