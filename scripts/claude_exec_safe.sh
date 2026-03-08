#!/bin/bash
# claude_exec_safe.sh — AADS-167 안전 래퍼 (2026-03-08)
# 기존 claude_exec.sh의 모든 기능 유지 + 프로세스 그룹 격리/타임아웃/PID 관리 추가
#
# 핵심 추가사항:
#   set -m : 프로세스 그룹 격리 (별도 PGID 생성)
#   timeout --kill-after=60 ${MAX_TIMEOUT} : 이중 타임아웃 (기본 2시간)
#   --max-turns 50 --max-budget-usd $MAX_BUDGET : Claude 내부 제한
#   PID 파일 기록: /root/.genspark/pids/${TASK_ID}.pid
#   종료 시 PGID kill 체인 + claude stream-json 고아 정리
#   ulimit -u 500 : 프로세스 폭발 방지
#   exit code 124/137 → /ops/directive-lifecycle DB 기록

set -m  # 프로세스 그룹 격리 (별도 PGID 생성) — AADS-167

# 프로세스 폭발 방지
ulimit -u 500 2>/dev/null || true

# 동시 실행 제한 (실제 claude 바이너리 기준, 최대 4개)
MAX_CONCURRENT=4
CURRENT=$(pgrep -u claudebot -x claude | wc -l)
if [ "$CURRENT" -ge "$MAX_CONCURRENT" ]; then
  echo "[$(date)] 동시 실행 제한 초과 ($CURRENT/$MAX_CONCURRENT) - 대기"
  exit 1
fi
# Usage: claude_exec_safe.sh <directive_file> <project> <workdir> [timeout] [max_turns] [model] [max_budget]

# === 계정 스위치 로직 (API Key 기반 — OAuth 만료 무관) ===
CRED_DIR="/root/.claude"
CRED_A1="${CRED_DIR}/.credentials_account1.json"
CRED_A2="${CRED_DIR}/.credentials_account2.json"
CRED_CURRENT="${CRED_DIR}/.credentials.json"
CRED_BOT="/home/claudebot/.claude/.credentials.json"
API_KEYS_FILE="${CRED_DIR}/api_keys.env"
CLAUDEBOT_PROFILE="/home/claudebot/.profile"

_load_api_key() {
  local OAUTH_FILE="/root/.genspark/.env.oauth"
  # OAuth 토큰 우선 (1년 setup-token)
  if [ -f "$OAUTH_FILE" ]; then
    local cur_oauth tok
    cur_oauth=$(grep '^CURRENT_OAUTH=' "$OAUTH_FILE" | cut -d= -f2 | tr -d ' ')
    tok=$(grep "^OAUTH_TOKEN_${cur_oauth:-2}=" "$OAUTH_FILE" | cut -d= -f2 | tr -d ' ')
    if [ -n "$tok" ]; then echo "OAUTH:${tok}"; return; fi
  fi
  # fallback: API Key
  if [ -f "$API_KEYS_FILE" ]; then
    local cur_acct key1 key2
    cur_acct=$(grep '^CURRENT_ACCOUNT=' "$API_KEYS_FILE" | cut -d= -f2 | tr -d ' ')
    key1=$(grep '^API_KEY_1=' "$API_KEYS_FILE" | cut -d= -f2 | tr -d ' ')
    key2=$(grep '^API_KEY_2=' "$API_KEYS_FILE" | cut -d= -f2 | tr -d ' ')
    if [ "${cur_acct}" = "2" ] && [ -n "$key2" ]; then echo "$key2"; return; fi
    [ -n "$key1" ] && { echo "$key1"; return; }
  fi
  grep '^export ANTHROPIC_API_KEY=' "$CLAUDEBOT_PROFILE" 2>/dev/null | cut -d= -f2- | tr -d '"'
}

switch_account() {
  local OAUTH_FILE="/root/.genspark/.env.oauth"

  # OAuth 토큰 방식 스위치 (우선)
  if [ -f "$OAUTH_FILE" ]; then
    local cur_oauth tok1 tok2 new_oauth new_tok
    cur_oauth=$(grep '^CURRENT_OAUTH=' "$OAUTH_FILE" | cut -d= -f2 | tr -d ' ')
    tok1=$(grep '^OAUTH_TOKEN_1=' "$OAUTH_FILE" | cut -d= -f2 | tr -d ' ')
    tok2=$(grep '^OAUTH_TOKEN_2=' "$OAUTH_FILE" | cut -d= -f2 | tr -d ' ')

    if [ "${cur_oauth:-2}" = "2" ] && [ -n "$tok1" ]; then
      new_oauth=1; new_tok="$tok1"
    elif [ "${cur_oauth}" = "1" ] && [ -n "$tok2" ]; then
      new_oauth=2; new_tok="$tok2"
    else
      echo "[SWITCH] OAuth 토큰 스위치 불가 — 토큰 없음"
      return 1
    fi

    python3 -c "
import re
path='$OAUTH_FILE'
with open(path) as f: c=f.read()
c=re.sub(r'^CURRENT_OAUTH=.*','CURRENT_OAUTH=${new_oauth}',c,flags=re.M)
with open(path,'w') as f: f.write(c)
" 2>/dev/null

    # claudebot profile 업데이트
    if grep -q 'CLAUDE_CODE_OAUTH_TOKEN' "$CLAUDEBOT_PROFILE" 2>/dev/null; then
      python3 -c "
import re
with open('$CLAUDEBOT_PROFILE') as f: c=f.read()
c=re.sub(r'^export CLAUDE_CODE_OAUTH_TOKEN=.*','export CLAUDE_CODE_OAUTH_TOKEN=${new_tok}',c,flags=re.M)
with open('$CLAUDEBOT_PROFILE','w') as f: f.write(c)
" 2>/dev/null
    else
      echo "export CLAUDE_CODE_OAUTH_TOKEN=${new_tok}" >> "$CLAUDEBOT_PROFILE"
    fi
    export CLAUDE_CODE_OAUTH_TOKEN="$new_tok"
    unset ANTHROPIC_API_KEY
    echo "[SWITCH] OAuth token${cur_oauth:-2} → token${new_oauth} 교체 완료"
    # 스위칭 시각 기록
    python3 -c "
import time
p='/root/.genspark/.env.oauth'
try:
    with open(p) as f: c=f.read()
except: c=''
import re
if 'LAST_SWITCH_TIME=' in c:
    c=re.sub(r'LAST_SWITCH_TIME=\d*', f'LAST_SWITCH_TIME={int(time.time())}', c)
else:
    c+=f'\nLAST_SWITCH_TIME={int(time.time())}\n'
with open(p,'w') as f: f.write(c)
" 2>/dev/null
    return 0
  fi

  # fallback: API Key 방식
  local cur_acct key1 key2 new_key new_acct
  cur_acct=$(grep '^CURRENT_ACCOUNT=' "$API_KEYS_FILE" | cut -d= -f2 | tr -d ' ')
  key1=$(grep '^API_KEY_1=' "$API_KEYS_FILE" | cut -d= -f2 | tr -d ' ')
  key2=$(grep '^API_KEY_2=' "$API_KEYS_FILE" | cut -d= -f2 | tr -d ' ')
  if [ "${cur_acct:-1}" = "1" ] && [ -n "$key2" ]; then
    new_acct=2; new_key="$key2"
  elif [ -n "$key1" ]; then
    new_acct=1; new_key="$key1"
  else
    echo "[SWITCH] API Key 스위치 불가"; return 1
  fi
  python3 -c "
import re
with open('$API_KEYS_FILE') as f: c=f.read()
c=re.sub(r'^CURRENT_ACCOUNT=.*','CURRENT_ACCOUNT=${new_acct}',c,flags=re.M)
with open('$API_KEYS_FILE','w') as f: f.write(c)
" 2>/dev/null
  python3 -c "
import re
with open('$CLAUDEBOT_PROFILE') as f: c=f.read()
c=re.sub(r'^#?export ANTHROPIC_API_KEY=.*','export ANTHROPIC_API_KEY=${new_key}',c,flags=re.M)
with open('$CLAUDEBOT_PROFILE','w') as f: f.write(c)
" 2>/dev/null
  export ANTHROPIC_API_KEY="$new_key"
  echo "[SWITCH] API Key account${cur_acct:-1} → account${new_acct} 교체 완료"
  # 스위칭 시각 기록
  python3 -c "
import time
p='/root/.genspark/.env.oauth'
try:
    with open(p) as f: c=f.read()
except: c=''
import re
if 'LAST_SWITCH_TIME=' in c:
    c=re.sub(r'LAST_SWITCH_TIME=\d*', f'LAST_SWITCH_TIME={int(time.time())}', c)
else:
    c+=f'\nLAST_SWITCH_TIME={int(time.time())}\n'
with open(p,'w') as f: f.write(c)
" 2>/dev/null
}
# === 계정 스위치 끝 ===

# === 에러 분류 함수 (기존 단일 grep 대체) ===
classify_error() {
  local log="$1"

  # Level 1: 즉시 계정 스위칭 (크레딧/인증/한도 문제)
  if grep -qi "Credit balance\|credit.*low\|balance.*too low\|Rate limit reached\|Rate limit exceeded\|usage.limit\|authentication_error\|OAuth.*expired\|401.*auth\|Failed to authenticate\|Credential Restricted\|permission_error.*403" "$log" 2>/dev/null; then
    echo "SWITCH"
    return
  fi

  # Level 2: 대기 후 재시도 (서버 과부하, 일시적 장애)
  if grep -qi "Overloaded\|overloaded_error\|529\|Too many requests\|500.*api_error\|Internal server error\|service.*unavailable" "$log" 2>/dev/null; then
    echo "WAIT_RETRY"
    return
  fi

  echo "UNKNOWN"
}

pre_check_account() {
  local result
  result=$(timeout 30 claude -p "echo ok" --max-turns 1 --model sonnet 2>&1)
  if echo "$result" | grep -qi "Credit balance\|Rate limit\|auth\|Failed\|Credential"; then
    echo "[PRE-CHECK] 계정 사용 불가: $(echo $result | head -1)" >> "$LOG_FILE"
    return 1
  fi
  echo "[PRE-CHECK] 계정 정상" >> "$LOG_FILE"
  return 0
}

check_switch_cooldown() {
  local cooldown=300  # 5분
  local last=$(grep '^LAST_SWITCH_TIME=' /root/.genspark/.env.oauth 2>/dev/null | cut -d= -f2)
  local now=$(date +%s)
  if [ -n "$last" ] && [ $((now - last)) -lt $cooldown ]; then
    echo "[COOLDOWN] 스위칭 후 ${cooldown}초 미경과 — 대기" >> "$LOG_FILE"
    return 1
  fi
  return 0
}

send_telegram_alert() {
  bash "$TELEGRAM_SCRIPT" "$1" 2>/dev/null
}

handle_execution_error() {
  local action=$(classify_error "$LOG_FILE")
  local max_switch_attempts=2
  local switch_count_file="/tmp/.claude_switch_count_$$"

  case "$action" in
    SWITCH)
      local count=$(cat "$switch_count_file" 2>/dev/null || echo 0)
      if [ "$count" -ge "$max_switch_attempts" ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [LEVEL3] 양쪽 계정 모두 실패" >> "$LOG_FILE"
        send_telegram_alert "🔴 양쪽 계정 모두 사용 불가. pending ${PENDING_COUNT}건 정체. 수동 조치 필요."
        return 1
      fi
      echo $((count+1)) > "$switch_count_file"
      echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [LEVEL1] 계정 스위칭 시도 (${count}/${max_switch_attempts})" >> "$LOG_FILE"
      switch_account
      sleep 5
      if pre_check_account; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [LEVEL1] 스위칭 성공 — 재시도" >> "$LOG_FILE"
        return 0  # 재시도 가능
      else
        handle_execution_error  # 재귀 (다음 계정 시도)
      fi
      ;;
    WAIT_RETRY)
      echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [LEVEL2] 서버 과부하 — 60초 대기" >> "$LOG_FILE"
      sleep 60
      return 0  # 재시도 가능
      ;;
    *)
      echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [UNKNOWN] 미분류 에러 — 30초 대기" >> "$LOG_FILE"
      sleep 30
      return 0
      ;;
  esac
}
# === 에러 분류 함수 끝 ===

# === 인증 주입 (OAuth 토큰 우선, fallback: API Key) ===
_ACTIVE_KEY=$(_load_api_key)
if [[ "$_ACTIVE_KEY" == OAUTH:* ]]; then
  export CLAUDE_CODE_OAUTH_TOKEN="${_ACTIVE_KEY#OAUTH:}"
  unset ANTHROPIC_API_KEY
elif [ -n "$_ACTIVE_KEY" ]; then
  export ANTHROPIC_API_KEY="$_ACTIVE_KEY"
  unset CLAUDE_CODE_OAUTH_TOKEN
fi
DIRECTIVE_FILE="$1"
PROJECT="$2"
WORKDIR="$3"
MAX_TIMEOUT="${4:-7200}"    # 기본 2시간 (AADS-167: 기존 1200→7200)
MAX_TURNS="${5:-50}"        # 기본 50턴 (AADS-167: 기존 200→50, Claude 내부 제한)
MAX_BUDGET="${7:-5.00}"     # 기본 $5.00 (AADS-167: Claude 내부 예산 제한)

# D-024: 지시서 model 필드 → size 기반 자동 라우팅 → arg fallback → sonnet
_DIR_MODEL=$(grep -m1 '^model:' "${DIRECTIVE_FILE}" 2>/dev/null | awk '{print $2}' | tr -d ' ')
_DIR_SIZE=$(grep -m1 '^size:' "${DIRECTIVE_FILE}" 2>/dev/null | awk '{print $2}' | tr -d ' ')
if [ -n "$_DIR_MODEL" ]; then MODEL="$_DIR_MODEL"
elif [ "$_DIR_SIZE" = "XS" ]; then MODEL="haiku"
elif [ "$_DIR_SIZE" = "XL" ]; then MODEL="opus"
else MODEL="${6:-sonnet}"; fi

DONE_DIR="/root/.genspark/directives/done"
RUNNING_DIR="/root/.genspark/directives/running"
LOG_DIR="/root/.genspark/logs"
TELEGRAM_SCRIPT="/root/.genspark/send_telegram.sh"
PID_DIR="/root/.genspark/pids"
FILENAME=$(basename "$DIRECTIVE_FILE")

# === AADS-167: PID 관리 디렉토리 생성 ===
mkdir -p "$PID_DIR" 2>/dev/null || true
# === PID 디렉토리 생성 끝 ===

# ── AADS message_queue write 함수 (CUR-BRIDGE-AADS-MSGQUEUE-001) ──────────
aads_queue_msg() {
    local target="$1" type="$2" msg_text="$3"
    local aads_url aads_key epoch item_key
    aads_url=$(grep '^AADS_API_URL=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    aads_key=$(grep '^AADS_MONITOR_KEY=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    [ -z "$aads_url" ] || [ -z "$aads_key" ] && return 1
    epoch=$(date +%s)
    item_key="${target}_${epoch}_${type}"
    local msg_json
    msg_json=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null <<< "${msg_text}" || echo "\"${msg_text}\"")
    curl -s -X POST "${aads_url}/context/system" \
        -H "Content-Type: application/json" \
        -H "X-Monitor-Key: ${aads_key}" \
        -d "{\"category\":\"message_queue\",\"key\":\"${item_key}\",\"value\":{\"target\":\"${target}\",\"type\":\"${type}\",\"message\":${msg_json},\"status\":\"pending\",\"created_at\":\"$(date '+%Y-%m-%d %H:%M KST')\",\"source\":\"claude_exec\"}}" \
        > /dev/null 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [AADS-QUEUE] ${target}/${type} 메시지 등록: ${item_key}" >> "${LOG_DIR}/auto_trigger.log"
}
# ── AADS-122: 교훈 자동 파싱 + POST 함수 ────────────────────────────────────
aads_lesson_check() {
    local result_file="$1"
    local aads_url
    aads_url=$(grep '^AADS_API_URL=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    [ -z "$aads_url" ] && aads_url="http://localhost:8080/api/v1"
    local AADS_API="${aads_url}"
    # 결과 파일에서 ## 교훈 또는 ## Lesson 섹션 추출
    local lesson_content
    lesson_content=$(sed -n '/^## 교훈/,/^## /p' "$result_file" 2>/dev/null | head -20)
    if [ -z "$lesson_content" ]; then
        lesson_content=$(sed -n '/^## Lesson/,/^## /p' "$result_file" 2>/dev/null | head -20)
    fi
    if [ -n "$lesson_content" ]; then
        # 다음 ID 계산
        local next_id
        next_id=$(curl -s "${AADS_API}/lessons" | python3 -c "import sys,json; print(f'L-{len(json.load(sys.stdin).get(\"lessons\",[]))+1:03d}')" 2>/dev/null)
        [ -z "$next_id" ] && next_id="L-AUTO"
        # summary JSON escape
        local summary_json
        summary_json=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null <<< "${lesson_content}" || echo "\"${lesson_content}\"")
        # POST 호출
        curl -s -X POST "${AADS_API}/lessons" \
            -H "Content-Type: application/json" \
            -d "{\"id\":\"${next_id}\",\"title\":\"Auto: ${TASK_ID}\",\"category\":\"auto\",\"source_project\":\"${PROJECT}\",\"source_task\":\"${TASK_ID}\",\"severity\":\"normal\",\"summary\":${summary_json}}" \
            > /dev/null 2>&1
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [LESSON_AUTO_REGISTERED] ${next_id} from ${TASK_ID}" >> "${LOG_DIR}/auto_trigger.log"
    fi
}
# ────────────────────────────────────────────────────────────────────────────

# === AADS-167: lifecycle API 기록 함수 ===
_report_lifecycle() {
    local _task="$1" _proj="$2" _status="$3" _exit="$4" _reason="$5"
    local _aads_url _aads_key
    _aads_url=$(grep '^AADS_API_URL=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    _aads_key=$(grep '^AADS_MONITOR_KEY=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    [ -z "$_aads_url" ] && return 0
    curl -s -X POST "${_aads_url}/ops/directive-lifecycle" \
        -H "Content-Type: application/json" \
        -H "X-Monitor-Key: ${_aads_key}" \
        -d "{\"task_id\":\"${_task}\",\"project\":\"${_proj}\",\"status\":\"${_status}\",\"exit_code\":${_exit},\"reason\":\"${_reason}\",\"recorded_at\":\"$(date '+%Y-%m-%d %H:%M:%S KST')\"}" \
        > /dev/null 2>&1 || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [LIFECYCLE] ${_task} status=${_status} exit=${_exit} reason=${_reason}" >> "${LOG_DIR}/auto_trigger.log"
}
# === lifecycle API 함수 끝 ===

RESULT_FILE="${DONE_DIR}/${FILENAME%.md}_RESULT.md"
LOG_FILE="${LOG_DIR}/claude_${PROJECT}_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR" "$DONE_DIR"

# === AADS-145: Tasks 시스템 통합 ===
CLAUDEBOT_TASKS_DIR="/home/claudebot/.claude/tasks"
mkdir -p "$CLAUDEBOT_TASKS_DIR" 2>/dev/null || true
# task_id 추출 (directive 파일에서)
_TASK_ID_145=$(grep -m1 '^task_id:' "${DIRECTIVE_FILE}" 2>/dev/null | awk '{print $2}' | tr -d ' ')
# TASK_ID: 필드도 체크 (지시서 형식)
if [ -z "$_TASK_ID_145" ]; then
    _TASK_ID_145=$(grep -m1 '^TASK_ID:' "${DIRECTIVE_FILE}" 2>/dev/null | awk '{print $2}' | tr -d ' ')
fi
TASK_ID_EXEC="${_TASK_ID_145:-${FILENAME%.md}}"
TASK_FILE="${CLAUDEBOT_TASKS_DIR}/${TASK_ID_EXEC}.json"
TASK_LIST_ID="aads-$(echo "$TASK_ID_EXEC" | tr '[:upper:]' '[:lower:]')-$(date +%s)"

# 세션 복구: Tasks 파일에 이미 done이면 스킵 (PENDING/DONE 이중관리 제거)
if [ -f "$TASK_FILE" ]; then
    _tasks_prev_status=$(python3 -c "import json; d=json.load(open('${TASK_FILE}')); print(d.get('status',''))" 2>/dev/null || echo "")
    if [ "${_tasks_prev_status}" = "done" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [TASKS] ${TASK_ID_EXEC} already done (Tasks 기록) — 스킵" >> "$LOG_FILE"
        rm -f "${RUNNING_DIR}/${FILENAME}" 2>/dev/null
        exit 0
    fi
fi

# Tasks 파일 생성 (in_progress 상태)
python3 -c "
import json, time
task = {
    'id': '${TASK_ID_EXEC}',
    'list_id': '${TASK_LIST_ID}',
    'title': '${TASK_ID_EXEC}: ${PROJECT}',
    'status': 'in_progress',
    'directive': '${DIRECTIVE_FILE}',
    'result': '${RESULT_FILE}',
    'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
    'project': '${PROJECT}'
}
with open('${TASK_FILE}', 'w') as f:
    json.dump(task, f, ensure_ascii=False, indent=2)
" 2>/dev/null || true

export CLAUDE_CODE_TASK_LIST_ID="${TASK_LIST_ID}"
echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [TASKS] list_id=${TASK_LIST_ID} file=${TASK_FILE}" >> "$LOG_FILE"
# === Tasks 통합 끝 ===

# ── 사전 검증: WORKDIR 쓰기 가능 여부 확인 ────────────────────────────────
# symlink 해소 후 실제 경로 확인
REAL_WORKDIR=$(readlink -f "${WORKDIR}" 2>/dev/null || echo "${WORKDIR}")
if [ ! -d "${REAL_WORKDIR}" ]; then
    bash "$TELEGRAM_SCRIPT" "❌ [${PROJECT}] WORKDIR 없음: ${WORKDIR} (실제: ${REAL_WORKDIR})" 2>/dev/null
    cat > "$RESULT_FILE" <<EOF
---
project: ${PROJECT}
task_id: PREFLIGHT_FAIL
completed_at: $(date '+%Y-%m-%d %H:%M:%S KST')
status: error
reason: WORKDIR not found (${WORKDIR} -> ${REAL_WORKDIR})
---
EOF
    rm -f "${RUNNING_DIR}/${FILENAME}" 2>/dev/null
    exit 1
fi

# 쓰기 전 선제 권한 보장 (실제 경로 기준)
find "${REAL_WORKDIR}" -maxdepth 0 -type d -exec chmod o+w {} \; 2>/dev/null
WRITE_TEST=$(su - claudebot -c "touch '${WORKDIR}/.write_test_$$' 2>&1 && rm '${WORKDIR}/.write_test_$$' && echo OK" 2>/dev/null)
if [ "$WRITE_TEST" != "OK" ]; then
    # 자동 복구: 전체 하위 디렉토리 권한 재적용
    find "${REAL_WORKDIR}" -type d -exec chmod g+w,o+w {} \; 2>/dev/null
    WRITE_TEST2=$(su - claudebot -c "touch '${WORKDIR}/.write_test_$$' 2>&1 && rm '${WORKDIR}/.write_test_$$' && echo OK" 2>/dev/null)
    if [ "$WRITE_TEST2" != "OK" ]; then
        bash "$TELEGRAM_SCRIPT" "❌ [${PROJECT}] WORKDIR 쓰기 권한 없음: ${WORKDIR} — 작업 중단" 2>/dev/null
        cat > "$RESULT_FILE" <<EOF
---
project: ${PROJECT}
task_id: PREFLIGHT_FAIL
completed_at: $(date '+%Y-%m-%d %H:%M:%S KST')
status: error
reason: claudebot has no write access to ${WORKDIR}
---
EOF
        rm -f "${RUNNING_DIR}/${FILENAME}" 2>/dev/null
        exit 1
    fi
    bash "$TELEGRAM_SCRIPT" "⚠️ [${PROJECT}] WORKDIR 권한 자동 복구 완료: ${WORKDIR}" 2>/dev/null
fi

# 텔레그램: 작업 시작
bash "$TELEGRAM_SCRIPT" "🔄 [${PROJECT}] 작업 시작: ${FILENAME}" 2>/dev/null

# === AADS-167: PID 파일 생성 ===
_MY_PID=$$
_MY_PGID=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
_START_TIME=$(date +%s)
_PID_FILE="${PID_DIR}/${TASK_ID_EXEC}.pid"
echo "${_MY_PID}|${_MY_PGID}|${_START_TIME}|${TASK_ID_EXEC}" > "${_PID_FILE}" 2>/dev/null || true
echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [PID] 파일 생성: ${_PID_FILE} (pid=${_MY_PID} pgid=${_MY_PGID})" >> "$LOG_FILE"
# === PID 파일 생성 끝 ===

# === AADS-167: 프로세스 그룹 종료 + 고아 정리 클린업 함수 ===
_cleanup_safe() {
    local _pgid
    _pgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
    if [ -n "$_pgid" ] && [ "$_pgid" != "0" ]; then
        kill -TERM -${_pgid} 2>/dev/null || true
        sleep 3
        kill -9 -${_pgid} 2>/dev/null || true
    fi
    # claude stream-json 고아 프로세스 추가 정리
    pkill -f 'claude.*stream-json' 2>/dev/null || true
    # PID 파일 삭제
    rm -f "${_PID_FILE}" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CLEANUP] PGID=${_pgid} 프로세스 그룹 정리 완료" >> "$LOG_FILE" 2>/dev/null || true
}
trap '_cleanup_safe' EXIT
# === 클린업 함수 끝 ===

# === L1 Self-Healing: Hard Timeout ===
HARD_TIMEOUT=1800  # 30분
SOFT_WARNING=1500  # 25분 경고
CURRENT_TASK_ID="${_TASK_ID:-unknown}"

timeout_handler() {
    local TASK_ID="${CURRENT_TASK_ID:-unknown}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] TIMEOUT: $TASK_ID exceeded ${HARD_TIMEOUT}s, self-terminating"
    # lifecycle DB에 실패 기록
    _report_lifecycle "$TASK_ID" "$PROJECT" "failed" 124 "timeout_self_kill"
    # 텔레그램 알림
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TG_CHAT_ID}" \
      -d text="⏰ [L1-TIMEOUT] ${TASK_ID} 30분 초과 자체종료. 서버: $(hostname)" 2>/dev/null
    # 자식 프로세스 전부 종료
    pkill -P $$ 2>/dev/null
    exit 124
}

warning_handler() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: 25분 경과, 5분 내 완료 필요"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TG_CHAT_ID}" \
      -d text="⚠️ [L1-WARNING] ${CURRENT_TASK_ID} 25분 경과, 5분 내 자동종료" 2>/dev/null
}

trap timeout_handler ALRM
# 25분 경고 타이머
(sleep $SOFT_WARNING && warning_handler) &
WARNING_PID=$!
# 30분 강제종료 타이머
(sleep $HARD_TIMEOUT && kill -ALRM $$ 2>/dev/null) &
TIMER_PID=$!
# === L1 Self-Healing 끝 ===

# === AADS-145: 컨텍스트 모니터링 백그라운드 ===
CTX_MAX_TOKENS=200000
CTX_SIGNAL="/tmp/.ctx_overload_${$}"
CTX_EDIT_FAIL_SIGNAL="/tmp/.ctx_edit_fail_${$}"

_ctx_monitor() {
    local _log="$1" _sig="$2" _edit_sig="$3"
    local _warned_70=false
    while true; do
        sleep 15
        [ -f "$_log" ] || continue
        # 2회 연속 수정 실패 감지 (Edit 오류 패턴)
        local _efail
        _efail=$(grep -c "old_string.*not found\|no match found\|수정 실패\|Edit.*failed" "$_log" 2>/dev/null || echo 0)
        if [ "${_efail:-0}" -ge 2 ] && [ ! -f "$_edit_sig" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-EDIT-FAIL] 2회 연속 수정 실패 → /clear 권고" >> "$_log"
            touch "$_edit_sig"
        fi
        # JSON output-format에서 token usage 파싱
        local _max_t
        _max_t=$(tail -1000 "$_log" 2>/dev/null | python3 -c "
import sys, json
mx=0
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try:
        d=json.loads(line)
        u=d.get('usage',{}) or {}
        t=u.get('input_tokens',0) or 0
        if t>mx: mx=t
    except: pass
print(mx)
" 2>/dev/null || echo 0)
        [ -z "$_max_t" ] && _max_t=0
        if [ "$_max_t" -gt 0 ] 2>/dev/null; then
            local _pct=$(( _max_t * 100 / CTX_MAX_TOKENS ))
            if [ "$_pct" -ge 90 ]; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-90%] 컨텍스트 90% 초과 (${_max_t}/${CTX_MAX_TOKENS}토큰) — 재시작 신호" >> "$_log"
                touch "$_sig"
                break
            elif [ "$_pct" -ge 70 ] && [ "$_warned_70" = "false" ]; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-70%] 컨텍스트 70% (${_max_t}/${CTX_MAX_TOKENS}토큰) — /compact 권고" >> "$_log"
                _warned_70=true
            fi
        fi
    done
}

_ctx_monitor "$LOG_FILE" "$CTX_SIGNAL" "$CTX_EDIT_FAIL_SIGNAL" &
CTX_MONITOR_PID=$!
# === 컨텍스트 모니터링 끝 ===

# ── Claude Code 실행 ──────────────────────────────────────────────────────
# AADS-167: timeout --kill-after=60 이중 타임아웃 + --max-turns + --max-budget-usd 추가
# 핵심 지시 순서:
#  1. WORKDIR에서만 작업 (절대 /tmp 사용 금지)
#  2. 지시서 읽기 및 실행
#  3. 결과 RESULT_FILE에 저장
timeout --kill-after=60 ${MAX_TIMEOUT} su - claudebot -c \
  "cd ${WORKDIR} && \$(which claude 2>/dev/null || echo /usr/local/bin/claude) -p \
  --dangerously-skip-permissions --max-turns ${MAX_TURNS} --max-budget-usd ${MAX_BUDGET} --model ${MODEL} --output-format json \
  '중요: 작업 디렉토리는 ${WORKDIR} 이다. 모든 파일 생성·수정은 반드시 ${WORKDIR} 내부에서만 수행하라. /tmp, /home, ~/ 등 다른 경로에 절대 파일을 생성하지 마라. /root/.genspark/directives/pending/ 및 /root/.genspark/directives/running/ 경로의 파일을 절대 삭제·이동·수정하지 마라(파이프라인 시스템 전용). 프로세스 탐색 시 /proc, /sys 경로에 grep -r을 실행하지 마라(pgrep, ps, lsof 사용). cat ${DIRECTIVE_FILE} 파일을 읽고 지시대로 모두 실행하라. 작업 완료 후 실행한 모든 내용과 결과를 빠짐없이 원문 그대로 ${RESULT_FILE} 에 저장하라. 절대 요약하지 마라. YAML 프런트매터(project, task_id, completed_at KST)를 파일 상단에 포함하라.'" \
  > "$LOG_FILE" 2>&1

EXIT_CODE=$?

# 타이머 해제
kill $WARNING_PID 2>/dev/null
kill $TIMER_PID 2>/dev/null

# === AADS-167: exit code 124/137 → lifecycle API DB 기록 ===
if [ $EXIT_CODE -eq 124 ] || [ $EXIT_CODE -eq 137 ]; then
    local_reason="timeout"
    [ $EXIT_CODE -eq 137 ] && local_reason="killed_SIGKILL"
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [SAFE-TIMEOUT] exit_code=${EXIT_CODE} reason=${local_reason} — lifecycle API 기록" >> "$LOG_FILE"
    _report_lifecycle "${TASK_ID_EXEC}" "${PROJECT}" "error" "${EXIT_CODE}" "${local_reason}"
fi
# === exit code 감지 끝 ===

# === AADS-145: 컨텍스트 모니터 종료 + 90% 재시작 처리 ===
kill $CTX_MONITOR_PID 2>/dev/null
if [ -f "$CTX_SIGNAL" ]; then
    rm -f "$CTX_SIGNAL" "$CTX_EDIT_FAIL_SIGNAL"
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-RESTART] 컨텍스트 90% 초과 — 중간 결과 저장 후 재시작" >> "$LOG_FILE"
    mkdir -p "$(dirname "$RESULT_FILE")"
    cat >> "$RESULT_FILE" <<_MIDRESULT_MARKER 2>/dev/null
## 중간 결과 저장 (컨텍스트 90% 초과)
재시작 시각: $(date '+%Y-%m-%d %H:%M:%S KST')
_MIDRESULT_MARKER
    timeout --kill-after=60 ${MAX_TIMEOUT} su - claudebot -c \
      "cd ${WORKDIR} && \$(which claude 2>/dev/null || echo /usr/local/bin/claude) -p \
      --dangerously-skip-permissions --max-turns ${MAX_TURNS} --max-budget-usd ${MAX_BUDGET} --model ${MODEL} --output-format json \
      '이전 세션 컨텍스트 한계로 재시작. 작업 디렉토리: ${WORKDIR}. cat ${DIRECTIVE_FILE} 읽고 미완성 작업 이어서 완료. 결과를 ${RESULT_FILE} 에 이어 저장.'" \
      >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-RESTART] 재시작 완료 (exit:${EXIT_CODE})" >> "$LOG_FILE"
fi
# === 컨텍스트 재시작 끝 ===

# === AADS-163: 3단계 품질 게이트 (QA → 디자인) ===
# 사용: _run_qa_gate <task_id> <workdir> <max_retry>
# 반환: 0=PASS, 1=FAIL
_run_qa_gate() {
    local _qa_task_id="$1"
    local _qa_workdir="$2"
    local _qa_max_retry="${3:-2}"
    local _qa_agent_file="/root/aads/.claude/agents/test-writer.md"
    local _qa_log="${LOG_DIR}/qa_${_qa_task_id}_$(date +%Y%m%d_%H%M%S).log"
    local _qa_result_file="/tmp/aads_qa_${_qa_task_id}_$$.txt"
    local _qa_retry=0
    local _qa_verdict=""

    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [QA-GATE] 시작 — task=${_qa_task_id} (최대 ${_qa_max_retry}회 재시도)" >> "$LOG_FILE"

    while [ $_qa_retry -le $_qa_max_retry ]; do
        local _qa_prompt=""
        if [ -f "$_qa_agent_file" ]; then
            _qa_prompt="$(cat "$_qa_agent_file" 2>/dev/null)

## QA 대상 태스크: ${_qa_task_id}
작업 디렉토리: ${_qa_workdir}
지시서: $(cat "${DIRECTIVE_FILE}" 2>/dev/null | head -60)

위 지시서의 success_criteria를 기준으로 ${_qa_workdir} 내 변경사항을 검토하라.
검토 후 반드시 QA_VERDICT: PASS 또는 QA_VERDICT: FAIL 을 출력하고, 실패 시 구체적 이유를 제시하라."
        else
            _qa_prompt="[QA] task=${_qa_task_id}
지시서 success_criteria:
$(grep -A5 'success_criteria' "${DIRECTIVE_FILE}" 2>/dev/null | head -20)

위 기준으로 작업 결과를 검토하고 QA_VERDICT: PASS 또는 QA_VERDICT: FAIL 을 출력하라."
        fi

        echo "$_qa_prompt" | timeout 600 su - claudebot -c \
            "\$(which claude 2>/dev/null || echo /usr/local/bin/claude) --print \
            --dangerously-skip-permissions --model ${MODEL:-sonnet}" \
            > "$_qa_result_file" 2>&1 || true

        _qa_verdict=$(grep -m1 "QA_VERDICT:" "$_qa_result_file" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || echo "UNKNOWN")
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [QA-GATE] 시도 $((_qa_retry+1)): verdict=${_qa_verdict}" >> "$LOG_FILE"

        if [ "$_qa_verdict" = "PASS" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [QA-GATE] PASS 확인" >> "$LOG_FILE"
            # RESULT_FILE에 qa_status 삽입
            python3 -c "
import re, sys
path = '${RESULT_FILE}'
try:
    with open(path) as f: c = f.read()
except: c = '---\nproject: ${PROJECT}\ntask_id: ${TASK_ID}\ncompleted_at: $(date \"+%Y-%m-%d %H:%M:%S KST\")\n---\n'
if 'qa_status:' not in c:
    c = re.sub(r'^---\n', '---\nqa_status: PASS\n', c, count=1, flags=re.M)
    with open(path, 'w') as f: f.write(c)
" 2>/dev/null || true
            rm -f "$_qa_result_file" 2>/dev/null
            return 0
        fi

        if [ $_qa_retry -lt $_qa_max_retry ] && [ "$_qa_verdict" = "FAIL" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [QA-GATE] FAIL — 자동 재작업 시도 $((_qa_retry+1))/${_qa_max_retry}" >> "$LOG_FILE"
            local _qa_feedback
            _qa_feedback=$(cat "$_qa_result_file" 2>/dev/null | tail -30)
            # 재작업: Claude에게 QA 피드백 전달 + 수정 요청
            timeout --kill-after=60 ${MAX_TIMEOUT} su - claudebot -c \
                "cd ${_qa_workdir} && \$(which claude 2>/dev/null || echo /usr/local/bin/claude) -p \
                --dangerously-skip-permissions --max-turns 50 --max-budget-usd ${MAX_BUDGET} --model ${MODEL:-sonnet} --output-format json \
                'QA 검토 결과 FAIL. 다음 피드백을 반영하여 수정하라: ${_qa_feedback}
원래 지시서: $(cat "${DIRECTIVE_FILE}" 2>/dev/null | head -40)
수정 완료 후 git add -A && git commit -m \"[${_qa_task_id}] fix: QA 피드백 반영 (재시도 $((_qa_retry+1))/${_qa_max_retry})\" && git push origin main 을 실행하라.'" \
                >> "$LOG_FILE" 2>&1 || true
        fi
        _qa_retry=$((_qa_retry+1))
    done

    # 최대 재시도 초과 → FAIL 기록
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [QA-GATE] FAIL (${_qa_max_retry}회 초과) — verdict=${_qa_verdict}" >> "$LOG_FILE"
    python3 -c "
import re
path = '${RESULT_FILE}'
try:
    with open(path) as f: c = f.read()
except: c = '---\nproject: ${PROJECT}\ntask_id: ${TASK_ID}\ncompleted_at: $(date \"+%Y-%m-%d %H:%M:%S KST\")\n---\n'
if 'qa_status:' not in c:
    c = re.sub(r'^---\n', '---\nqa_status: FAIL\n', c, count=1, flags=re.M)
    with open(path, 'w') as f: f.write(c)
" 2>/dev/null || true
    rm -f "$_qa_result_file" 2>/dev/null
    return 1
}

# 사용: _run_design_gate <task_id> <workdir>
# 반환: 0=PASS/SKIP, 1=DESIGN_REVIEW_NEEDED
_run_design_gate() {
    local _dg_task_id="$1"
    local _dg_workdir="$2"
    local _dg_agent_file="/root/aads/.claude/agents/doc-writer.md"
    local _dg_result_file="/tmp/aads_design_${_dg_task_id}_$$.txt"

    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [DESIGN-GATE] 시작 — task=${_dg_task_id}" >> "$LOG_FILE"

    local _dg_prompt=""
    if [ -f "$_dg_agent_file" ]; then
        _dg_prompt="$(cat "$_dg_agent_file" 2>/dev/null)

## 디자인 검증 대상: ${_dg_task_id}
작업 디렉토리: ${_dg_workdir}
지시서:
$(cat "${DIRECTIVE_FILE}" 2>/dev/null | head -60)

위 변경사항에 UI/UX 변경이 있는지 확인하라.
- UI/UX 변경 없음: DESIGN_VERDICT: PASS 출력
- UI/UX 변경 있고 검증 완료: DESIGN_VERDICT: PASS 출력
- CEO 디자인 검토 필요: DESIGN_VERDICT: REVIEW_NEEDED 출력 (이유 포함)"
    else
        _dg_prompt="[DESIGN-CHECK] task=${_dg_task_id}
지시서에 UI/UX 변경이 있으면 DESIGN_VERDICT: REVIEW_NEEDED, 없으면 DESIGN_VERDICT: PASS 를 출력하라."
    fi

    echo "$_dg_prompt" | timeout 300 su - claudebot -c \
        "\$(which claude 2>/dev/null || echo /usr/local/bin/claude) --print \
        --dangerously-skip-permissions --model ${MODEL:-sonnet}" \
        > "$_dg_result_file" 2>&1 || true

    local _dg_verdict
    _dg_verdict=$(grep -m1 "DESIGN_VERDICT:" "$_dg_result_file" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || echo "PASS")
    [ -z "$_dg_verdict" ] && _dg_verdict="PASS"

    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [DESIGN-GATE] verdict=${_dg_verdict}" >> "$LOG_FILE"

    # RESULT_FILE에 design_status 삽입
    python3 -c "
import re
path = '${RESULT_FILE}'
try:
    with open(path) as f: c = f.read()
except: c = '---\n---\n'
if 'design_status:' not in c:
    c = re.sub(r'^---\n', '---\ndesign_status: ${_dg_verdict}\n', c, count=1, flags=re.M)
    with open(path, 'w') as f: f.write(c)
" 2>/dev/null || true

    if [ "$_dg_verdict" = "REVIEW_NEEDED" ]; then
        # CEO Chat 보고 (aads_queue_msg) + 60초 타임아웃 대기
        local _dg_detail
        _dg_detail=$(cat "$_dg_result_file" 2>/dev/null | tail -20 | tr '\n' ' ')
        aads_queue_msg "${PROJECT}" "chat" "🎨 [DESIGN-REVIEW] ${_dg_task_id} 디자인 검토 필요
이유: ${_dg_detail}
→ 60초 내 승인하지 않으면 PASS 처리됩니다."
        bash "$TELEGRAM_SCRIPT" "🎨 [${PROJECT}] 디자인 검토 필요: ${_dg_task_id} — 60초 내 승인 (타임아웃 시 PASS)" 2>/dev/null
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [DESIGN-GATE] CEO 승인 대기 (60초)..." >> "$LOG_FILE"
        sleep 60
        # 타임아웃 후 PASS 처리
        python3 -c "
import re
path = '${RESULT_FILE}'
try:
    with open(path) as f: c = f.read()
except: c = ''
c = re.sub(r'design_status: REVIEW_NEEDED', 'design_status: PASS_TIMEOUT', c)
with open(path, 'w') as f: f.write(c)
" 2>/dev/null || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [DESIGN-GATE] 타임아웃 → PASS_TIMEOUT 처리" >> "$LOG_FILE"
        rm -f "$_dg_result_file" 2>/dev/null
        return 0
    fi

    rm -f "$_dg_result_file" 2>/dev/null
    return 0
}
# === 3단계 품질 게이트 함수 끝 ===

# === 사용량 DB 기록 ===
if [ -f "$LOG_FILE" ]; then
  cat "$LOG_FILE" | bash /root/.genspark/scripts/usage_logger.sh "$PROJECT" "$FILENAME" 2>/dev/null
fi

# === 에러 처리 (3단계 분류 — SWITCH/WAIT_RETRY/UNKNOWN) ===
if [ $EXIT_CODE -ne 0 ] && [ $EXIT_CODE -ne 124 ] && [ $EXIT_CODE -ne 137 ]; then
  if handle_execution_error; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [RETRY] 에러 핸들링 후 재시도" >> "$LOG_FILE"
    timeout --kill-after=60 ${MAX_TIMEOUT} su - claudebot -c \
      "cd ${WORKDIR} && \$(which claude 2>/dev/null || echo /usr/local/bin/claude) -p \
      --dangerously-skip-permissions --max-turns ${MAX_TURNS} --max-budget-usd ${MAX_BUDGET} --model ${MODEL} --output-format json \
      '중요: 작업 디렉토리는 ${WORKDIR} 이다. cat ${DIRECTIVE_FILE} 파일을 읽고 지시대로 모두 실행하라. 결과를 ${RESULT_FILE} 에 저장하라.'" \
      >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
    echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [RETRY] 재시도 완료 (exit_code: $EXIT_CODE)" >> "$LOG_FILE"
  fi
fi
# === 에러 처리 끝 ===

if [ $EXIT_CODE -eq 124 ] || [ $EXIT_CODE -eq 137 ]; then
    cat > "$RESULT_FILE" <<EOF
---
project: ${PROJECT}
task_id: TIMEOUT
completed_at: $(date '+%Y-%m-%d %H:%M:%S KST')
status: timeout
exit_code: ${EXIT_CODE}
---
## 타임아웃/강제종료 (${MAX_TIMEOUT}초, exit=${EXIT_CODE})
$(tail -20 "$LOG_FILE")
EOF
    bash "$TELEGRAM_SCRIPT" "⏰ [${PROJECT}] 타임아웃/강제종료 (${MAX_TIMEOUT}초, exit=${EXIT_CODE}): ${FILENAME}" 2>/dev/null
    aads_queue_msg "${PROJECT}" "chat" "⏰ [${PROJECT}] 타임아웃 종료 (${MAX_TIMEOUT}초, exit=${EXIT_CODE})
파일: ${FILENAME}
상태: timeout"
    aads_queue_msg "${PROJECT}" "telegram" "⏰ [${PROJECT}] 타임아웃: ${FILENAME}"
elif [ $EXIT_CODE -ne 0 ]; then
    if [ ! -f "$RESULT_FILE" ]; then
        cat > "$RESULT_FILE" <<EOF
---
project: ${PROJECT}
task_id: ERROR
completed_at: $(date '+%Y-%m-%d %H:%M:%S KST')
status: error
exit_code: ${EXIT_CODE}
---
## 에러 종료
$(tail -20 "$LOG_FILE")
EOF
    fi
    bash "$TELEGRAM_SCRIPT" "❌ [${PROJECT}] 에러 종료 (code:${EXIT_CODE}): ${FILENAME}" 2>/dev/null
    aads_queue_msg "${PROJECT}" "chat" "❌ [${PROJECT}] 에러 종료 (code:${EXIT_CODE})
파일: ${FILENAME}
$(tail -5 "$LOG_FILE" 2>/dev/null)"
    aads_queue_msg "${PROJECT}" "telegram" "❌ [${PROJECT}] 에러 (code:${EXIT_CODE}): ${FILENAME}"
else
    # RESULT 파일에서 task_id 추출
    _task_id=$(grep -m1 '^task_id:' "$RESULT_FILE" 2>/dev/null | awk '{print $2}')
    TASK_ID="${_task_id:-${FILENAME%.md}}"

    # === AADS-163: 3단계 품질 게이트 (개발→QA→디자인) ===
    _QA_GATE_RESULT=0
    _run_qa_gate "${TASK_ID}" "${WORKDIR}" 2 || _QA_GATE_RESULT=$?

    if [ $_QA_GATE_RESULT -eq 0 ]; then
        # QA PASS → 디자인 게이트
        _run_design_gate "${TASK_ID}" "${WORKDIR}" || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [3-GATE] QA=PASS 디자인 게이트 완료" >> "$LOG_FILE"
        bash "$TELEGRAM_SCRIPT" "✅ [${PROJECT}] 작업 완료 (QA+디자인 통과): ${FILENAME}" 2>/dev/null
    else
        # QA FAIL — RESULT에 qa_status=FAIL 이미 기록됨
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [3-GATE] QA=FAIL — 서킷브레이커 연동 신호" >> "$LOG_FILE"
        bash "$TELEGRAM_SCRIPT" "❌ [${PROJECT}] QA FAIL (2회 초과): ${FILENAME} — 서킷브레이커 트리거" 2>/dev/null
    fi
    # === 3단계 품질 게이트 끝 ===

    # AADS-122: 교훈 자동 등록
    aads_lesson_check "$RESULT_FILE"

    # === AADS-143: commit SHA 기록 (git-push 감시 이중확인용) ===
    _commit_sha=""
    if [ -d "${WORKDIR}/.git" ]; then
        _commit_sha=$(git -C "${WORKDIR}" rev-parse HEAD 2>/dev/null | tr -d '[:space:]')
    fi
    if [ -n "$_commit_sha" ]; then
        # RESULT_FILE YAML 헤더에 commit_sha 삽입 (기존 헤더 끝 --- 앞에 추가)
        python3 -c "
import re, sys
path = '${RESULT_FILE}'
try:
    with open(path) as f: c = f.read()
except:
    sys.exit(0)
if 'commit_sha:' not in c:
    c = re.sub(r'^---\n', '---\ncommit_sha: ${_commit_sha}\n', c, count=1, flags=re.M)
    with open(path, 'w') as f: f.write(c)
" 2>/dev/null
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [GIT-SHA] commit_sha=${_commit_sha} 기록 완료" >> "$LOG_FILE"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [GIT-SHA] git 디렉토리 없음 또는 SHA 추출 실패 (WORKDIR=${WORKDIR})" >> "$LOG_FILE"
    fi
    # === commit SHA 기록 끝 ===

    # === AADS-145: final_commit 투기적 실행 신호 ===
    if [ -n "${_commit_sha:-}" ]; then
        echo "${TASK_ID_EXEC:-${FILENAME%.md}}" > "/tmp/aads_final_commit_${TASK_ID_EXEC:-exec}.signal"
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [FINAL-COMMIT] 신호 파일 생성 (sha=${_commit_sha:0:8})" >> "$LOG_FILE"
    fi
    # === final_commit 신호 끝 ===

    aads_queue_msg "${PROJECT}" "chat" "✅ [${PROJECT}] 작업 완료
Task: ${_task_id:-${FILENAME%.md}}
파일: ${FILENAME}
보고서: https://github.com/moongoby-GO100/aads-docs/blob/master"
    aads_queue_msg "${PROJECT}" "telegram" "✅ [${PROJECT}] 완료: ${_task_id:-${FILENAME}}"
fi

# === AADS-145: Tasks 완료 상태 업데이트 ===
if [ -n "${TASK_FILE:-}" ] && [ -f "$TASK_FILE" ]; then
    _t_done_status="failed"
    [ $EXIT_CODE -eq 0 ] && _t_done_status="done"
    python3 -c "
import json, time
try:
    with open('${TASK_FILE}') as f: d = json.load(f)
    d['status'] = '${_t_done_status}'
    d['completed_at'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    d['exit_code'] = ${EXIT_CODE}
    with open('${TASK_FILE}', 'w') as f: json.dump(d, f, ensure_ascii=False, indent=2)
except: pass
" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [TASKS] 상태 업데이트: ${_t_done_status} (${TASK_FILE})" >> "$LOG_FILE"
fi
# === Tasks 완료 끝 ===

# running 파일 정리
rm -f "${RUNNING_DIR}/${FILENAME}" 2>/dev/null

echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] ${PROJECT} 실행 완료 (exit:${EXIT_CODE})" >> "${LOG_DIR}/auto_trigger.log"
