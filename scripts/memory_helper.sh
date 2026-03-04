#!/bin/bash
# AADS Memory Helper — Context API 연동 공유 라이브러리
# 생성: 2026-03-04 T-021
# 사용: source scripts/memory_helper.sh

AADS_ROOT="${AADS_ROOT:-/root/aads}"
CONTEXT_API="${CONTEXT_API:-https://aads.newtalk.kr/api/v1/context/system}"
MEMORY_API="${MEMORY_API:-https://aads.newtalk.kr/api/v1/memory/log}"

# API 키 자동 로드 (환경 변수 우선, 없으면 .env에서 읽기)
_load_monitor_key() {
    if [ -z "${AADS_MONITOR_KEY:-}" ]; then
        local env_file="${AADS_ROOT}/aads-server/.env"
        if [ -f "$env_file" ]; then
            AADS_MONITOR_KEY=$(grep "^AADS_MONITOR_KEY=" "$env_file" | cut -d'=' -f2- | tr -d '[:space:]')
        fi
    fi
}
_load_monitor_key

# -------------------------------------------------------
# read_context([category], [key])
# Context API에서 맥락 읽기
# 반환: JSON 문자열
# 예시:
#   read_context                    → 전체
#   read_context "phase"            → phase 카테고리
#   read_context "phase" "current_phase" → 특정 키
# -------------------------------------------------------
read_context() {
    local category="${1:-}"
    local key="${2:-}"
    local url="${CONTEXT_API}"

    [ -n "$category" ] && url="${url}/${category}"
    [ -n "$key" ]      && url="${url}/${key}"

    curl -s \
        -H "X-Monitor-Key: ${AADS_MONITOR_KEY}" \
        "${url}" 2>/dev/null
}

# -------------------------------------------------------
# write_task_result(task_id, result_text, [status])
# 작업 결과를 history 카테고리에 기록
# -------------------------------------------------------
write_task_result() {
    local task_id="${1:?task_id required}"
    local result="${2:?result required}"
    local status="${3:-COMPLETED}"
    local ts
    ts=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

    export _WTR_TASK_ID="$task_id"
    export _WTR_RESULT="$result"
    export _WTR_STATUS="$status"
    export _WTR_TS="$ts"
    export _WTR_KEY="$AADS_MONITOR_KEY"
    export _WTR_API="$CONTEXT_API"

    python3 - <<'PYEOF'
import json, urllib.request, os, sys

task_id = os.environ.get('_WTR_TASK_ID', '')
result  = os.environ.get('_WTR_RESULT', '')
status  = os.environ.get('_WTR_STATUS', 'COMPLETED')
ts      = os.environ.get('_WTR_TS', '')
key     = os.environ.get('_WTR_KEY', '')
api     = os.environ.get('_WTR_API', '')

payload = {
    "category": "history",
    "key": "task_result_" + task_id,
    "value": {
        "task_id": task_id,
        "result": result,
        "status": status,
        "timestamp": ts
    }
}
body = json.dumps(payload).encode()
req = urllib.request.Request(api, data=body,
    headers={"Content-Type": "application/json", "X-Monitor-Key": key,
             "User-Agent": "curl/7.64.0"},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        sys.stdout.write(json.dumps({"status": "ok", "saved": data.get("saved", "")}) + "\n")
except Exception as e:
    sys.stdout.write(json.dumps({"status": "error", "error": str(e)}) + "\n")
PYEOF
}

# -------------------------------------------------------
# write_experience(title, content, [domain])
# 경험/인사이트를 history 카테고리에 기록
# -------------------------------------------------------
write_experience() {
    local title="${1:?title required}"
    local content="${2:?content required}"
    local domain="${3:-aads}"
    local ts
    ts=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')
    local epoch
    epoch=$(date +%s)

    export _WE_TITLE="$title"
    export _WE_CONTENT="$content"
    export _WE_DOMAIN="$domain"
    export _WE_TS="$ts"
    export _WE_EPOCH="$epoch"
    export _WE_KEY="$AADS_MONITOR_KEY"
    export _WE_API="$CONTEXT_API"

    python3 - <<'PYEOF'
import json, urllib.request, os, sys

title   = os.environ.get('_WE_TITLE', '')
content = os.environ.get('_WE_CONTENT', '')
domain  = os.environ.get('_WE_DOMAIN', 'aads')
ts      = os.environ.get('_WE_TS', '')
epoch   = os.environ.get('_WE_EPOCH', '0')
key     = os.environ.get('_WE_KEY', '')
api     = os.environ.get('_WE_API', '')

payload = {
    "category": "history",
    "key": "experience_" + epoch,
    "value": {
        "title": title,
        "content": content,
        "domain": domain,
        "timestamp": ts
    }
}
body = json.dumps(payload).encode()
req = urllib.request.Request(api, data=body,
    headers={"Content-Type": "application/json", "X-Monitor-Key": key,
             "User-Agent": "curl/7.64.0"},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        sys.stdout.write(json.dumps({"status": "ok", "saved": data.get("saved", "")}) + "\n")
except Exception as e:
    sys.stdout.write(json.dumps({"status": "error", "error": str(e)}) + "\n")
PYEOF
}

# -------------------------------------------------------
# write_error(task_id, error_message)
# 에러 내용을 errors 카테고리에 기록
# -------------------------------------------------------
write_error() {
    local task_id="${1:?task_id required}"
    local error="${2:?error required}"
    local ts
    ts=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')
    local epoch
    epoch=$(date +%s)

    export _WERR_TASK_ID="$task_id"
    export _WERR_ERROR="$error"
    export _WERR_TS="$ts"
    export _WERR_EPOCH="$epoch"
    export _WERR_KEY="$AADS_MONITOR_KEY"
    export _WERR_API="$CONTEXT_API"

    python3 - <<'PYEOF'
import json, urllib.request, os, sys

task_id = os.environ.get('_WERR_TASK_ID', '')
error   = os.environ.get('_WERR_ERROR', '')
ts      = os.environ.get('_WERR_TS', '')
epoch   = os.environ.get('_WERR_EPOCH', '0')
key     = os.environ.get('_WERR_KEY', '')
api     = os.environ.get('_WERR_API', '')

payload = {
    "category": "errors",
    "key": "error_" + task_id + "_" + epoch,
    "value": {
        "task_id": task_id,
        "error": error,
        "timestamp": ts
    }
}
body = json.dumps(payload).encode()
req = urllib.request.Request(api, data=body,
    headers={"Content-Type": "application/json", "X-Monitor-Key": key,
             "User-Agent": "curl/7.64.0"},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        sys.stdout.write(json.dumps({"status": "ok", "saved": data.get("saved", "")}) + "\n")
except Exception as e:
    sys.stdout.write(json.dumps({"status": "error", "error": str(e)}) + "\n")
PYEOF
}

# -------------------------------------------------------
# save_manager_report(task_id, status, report_content, commit_sha, http_code)
# T-037 B-1: 매니저/Cursor/Claude Code 보고 내용을 go100_user_memory에 저장
# POST /memory/log 엔드포인트 사용 (MEMORY_API)
# -------------------------------------------------------
save_manager_report() {
    local task_id="${1:?task_id required}"
    local status="${2:?status required}"
    local report="${3:?report_content required}"
    local commit="${4:-}"
    local http_code="${5:-}"

    export _SMR_TASK_ID="$task_id"
    export _SMR_STATUS="$status"
    export _SMR_REPORT="$report"
    export _SMR_COMMIT="$commit"
    export _SMR_HTTP_CODE="$http_code"
    export _SMR_KEY="$AADS_MONITOR_KEY"
    export _SMR_API="$MEMORY_API"
    export _SMR_TS
    _SMR_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    python3 - <<'PYEOF'
import json, urllib.request, urllib.error, os, sys

task_id   = os.environ.get('_SMR_TASK_ID', '')
status    = os.environ.get('_SMR_STATUS', '')
report    = os.environ.get('_SMR_REPORT', '')
commit    = os.environ.get('_SMR_COMMIT', '')
http_code = os.environ.get('_SMR_HTTP_CODE', '')
ts        = os.environ.get('_SMR_TS', '')
key       = os.environ.get('_SMR_KEY', '')
api       = os.environ.get('_SMR_API', '')

payload = {
    "user_id": 2,
    "memory_type": "mgr_report_" + task_id,
    "content": {
        "agent_id": "AADS_PROJECT_MGR",
        "event_type": "manager_report",
        "details": {
            "task_id": task_id,
            "status": status,
            "report": report[:500],
            "commit": commit,
            "http_code": http_code,
            "source": "cursor_claude_code",
            "manager_chat": "genspark_aads_mgr"
        },
        "logged_at": ts
    },
    "importance": 7.0,
    "expires_at": None
}
body = json.dumps(payload).encode()
req = urllib.request.Request(api, data=body,
    headers={"Content-Type": "application/json", "X-Monitor-Key": key,
             "User-Agent": "curl/7.64.0"},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        sys.stdout.write(json.dumps({"status": "ok", "saved": data.get("saved", "")}) + "\n")
except Exception as e:
    sys.stdout.write(json.dumps({"status": "error", "error": str(e)}) + "\n")
PYEOF
}
