#!/bin/bash

# 동시 실행 제한 (실제 claude 바이너리 기준, 최대 4개)
MAX_CONCURRENT=4
CURRENT=$(pgrep -u claudebot -x claude | wc -l)
if [ "$CURRENT" -ge "$MAX_CONCURRENT" ]; then
  echo "[$(date)] 동시 실행 제한 초과 ($CURRENT/$MAX_CONCURRENT) - 대기"
  exit 1
fi
# Usage: claude_exec.sh <directive_file> <project> <workdir> [timeout] [max_turns] [model]

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
}
# === 계정 스위치 끝 ===

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
MAX_TIMEOUT="${4:-1200}"
MAX_TURNS="${5:-200}"
MODEL="${6:-sonnet}"

# PRIORITY 추출 (P0/P1/P2/P3)
PRIORITY=$(grep -m1 -iE '^priority[[:space:]]*[:：]' "$DIRECTIVE_FILE" 2>/dev/null \
    | sed 's/^[^:：]*[:：][[:space:]]*//' | awk '{print $1}' | tr -d '[:space:]')
PRIORITY="${PRIORITY:-P2}"

DONE_DIR="/root/.genspark/directives/done"
RUNNING_DIR="/root/.genspark/directives/running"
LOG_DIR="/root/.genspark/logs"
TELEGRAM_SCRIPT="/root/.genspark/send_telegram.sh"
FILENAME=$(basename "$DIRECTIVE_FILE")

# ── AADS Lifecycle API 함수 ──────────────────────────────────────────────────
AADS_OPS_URL="https://aads.newtalk.kr/api/v1/ops"

# task_id 추출 (지시서 파일에서)
_extract_task_id() {
    local f="$1"
    grep -m1 -oP '(?:Task ID|task_id)\s*[:：]\s*\K\S+' "$f" 2>/dev/null | head -1
}

# project 태그 추출 (파일명에서)
_extract_project() {
    echo "$1" | cut -d'_' -f1 | tr '[:lower:]' '[:upper:]'
}

# 서버명 (hostname 기반)
_this_server() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    case "$ip" in
        211.*) echo "211" ;;
        68.*)  echo "68"  ;;
        114.*) echo "114" ;;
        *)     hostname -s 2>/dev/null ;;
    esac
}

# Lifecycle 상태 보고: aads_lifecycle <task_id> <project> <status> [error_detail] [title] [file_path]
aads_lifecycle() {
    local tid="$1" proj="$2" st="$3" err="$4" title="$5" fpath="$6"
    [ -z "$tid" ] || [ -z "$proj" ] || [ -z "$st" ] && return 1
    local srv
    srv=$(_this_server)
    local json="{\"task_id\":\"${tid}\",\"project\":\"${proj}\",\"status\":\"${st}\",\"server\":\"${srv}\""
    [ -n "$title" ] && json="${json},\"title\":\"${title}\""
    [ -n "$fpath" ] && json="${json},\"file_path\":\"${fpath}\""
    [ -n "$err" ]   && json="${json},\"error_detail\":$(python3 -c "import json; print(json.dumps('$err'))" 2>/dev/null || echo "\"${err}\"")}"
    [ -z "$err" ]   && json="${json}}"
    curl -s -X POST "${AADS_OPS_URL}/directive-lifecycle" \
        -H "Content-Type: application/json" \
        -d "$json" --connect-timeout 5 --max-time 10 > /dev/null 2>&1 &
}

# Cost 기록: aads_cost <task_id> <project> <log_file>
aads_cost() {
    local tid="$1" proj="$2" logf="$3"
    [ -z "$tid" ] || [ ! -f "$logf" ] && return 1
    # Claude JSON 출력에서 usage 추출
    local cost_usd input_tok output_tok
    cost_usd=$(grep -oP '"total_cost_usd"\s*:\s*\K[0-9.]+' "$logf" 2>/dev/null | tail -1)
    input_tok=$(grep -oP '"input_tokens"\s*:\s*\K[0-9]+' "$logf" 2>/dev/null | tail -1)
    output_tok=$(grep -oP '"output_tokens"\s*:\s*\K[0-9]+' "$logf" 2>/dev/null | tail -1)
    [ -z "$cost_usd" ] && cost_usd="0"
    [ -z "$input_tok" ] && input_tok="0"
    [ -z "$output_tok" ] && output_tok="0"
    local model_name
    model_name=$(grep -oP '"model"\s*:\s*"\K[^"]+' "$logf" 2>/dev/null | tail -1)
    [ -z "$model_name" ] && model_name="$MODEL"
    curl -s -X POST "${AADS_OPS_URL}/cost" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\":\"${tid}\",\"project\":\"${proj}\",\"model\":\"${model_name:-sonnet}\",\"input_tokens\":${input_tok},\"output_tokens\":${output_tok},\"cost_usd\":${cost_usd}}" \
        --connect-timeout 5 --max-time 10 > /dev/null 2>&1 &
}

# Commit 기록: aads_commit <task_id> <result_file>
aads_commit() {
    local tid="$1" rf="$2"
    [ -z "$tid" ] || [ ! -f "$rf" ] && return 1
    local sha msg repo
    sha=$(grep -oP '(?:commit|커밋)[:\s]*\K[0-9a-f]{7,40}' "$rf" 2>/dev/null | head -1)
    [ -z "$sha" ] && return 0
    msg=$(grep -m1 -oP '(?:feat|fix|docs|refactor|chore)\(.+?\):.+' "$rf" 2>/dev/null | head -1)
    repo=$(grep -oP 'github\.com[:/]\K[^/]+/[^/.\s]+' "$rf" 2>/dev/null | head -1)
    curl -s -X POST "${AADS_OPS_URL}/commit" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\":\"${tid}\",\"repo\":\"${repo:-unknown}\",\"commit_sha\":\"${sha}\",\"message\":$(python3 -c "import json; print(json.dumps('${msg:-no message}'))" 2>/dev/null || echo "\"commit\"")}" \
        --connect-timeout 5 --max-time 10 > /dev/null 2>&1 &
}
# ── AADS Lifecycle API 끝 ───────────────────────────────────────────────────

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
# ────────────────────────────────────────────────────────────────────────────
RESULT_FILE="${DONE_DIR}/${FILENAME%.md}_RESULT.md"
LOG_FILE="${LOG_DIR}/claude_${PROJECT}_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR" "$DONE_DIR"

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

# ── Lifecycle: running 기록 ──────────────────────────────────────────────────
_TASK_ID=$(_extract_task_id "$DIRECTIVE_FILE")
[ -z "$_TASK_ID" ] && _TASK_ID=$(echo "$FILENAME" | sed 's/_BRIDGE\.md$//' | sed 's/\.md$//')
_TITLE=$(grep -m1 -oP '제목\s*[:：]\s*\K.+' "$DIRECTIVE_FILE" 2>/dev/null | head -c 120)
aads_lifecycle "$_TASK_ID" "$PROJECT" "running" "" "$_TITLE" "$DIRECTIVE_FILE"
# AADS-139: source_channel_id 추출 — 완료 시 원본 대화창에 보고
_SOURCE_CHANNEL=$(grep -m1 '<!-- source_channel_id:' "$DIRECTIVE_FILE" 2>/dev/null | sed 's/.*source_channel_id:[[:space:]]*//' | sed 's/[[:space:]]*-->.*//' | tr -d '[:space:]')

# ── Claude Code 실행 ──────────────────────────────────────────────────────
# 핵심 지시 순서:
#  1. WORKDIR에서만 작업 (절대 /tmp 사용 금지)
#  2. 지시서 읽기 및 실행
#  3. 결과 RESULT_FILE에 저장
timeout ${MAX_TIMEOUT} su - claudebot -c \
  "cd ${WORKDIR} && \$(which claude 2>/dev/null || echo /usr/local/bin/claude) -p \
  --dangerously-skip-permissions --max-turns ${MAX_TURNS} --model ${MODEL} --output-format json \
  '중요: 작업 디렉토리는 ${WORKDIR} 이다. 모든 파일 생성·수정은 반드시 ${WORKDIR} 내부에서만 수행하라. /tmp, /home, ~/ 등 다른 경로에 절대 파일을 생성하지 마라. cat ${DIRECTIVE_FILE} 파일을 읽고 지시대로 모두 실행하라. 작업 완료 후 실행한 모든 내용과 결과를 빠짐없이 원문 그대로 ${RESULT_FILE} 에 저장하라. 절대 요약하지 마라. YAML 프런트매터(project, task_id, completed_at KST)를 파일 상단에 포함하라.'" \
  > "$LOG_FILE" 2>&1

EXIT_CODE=$?

# === 사용량 DB 기록 ===
if [ -f "$LOG_FILE" ]; then
  cat "$LOG_FILE" | bash /root/.genspark/scripts/usage_logger.sh "$PROJECT" "$FILENAME" 2>/dev/null
fi

# === Rate Limit / Auth 오류 재시도 로직 ===
if [ $EXIT_CODE -ne 0 ] && [ $EXIT_CODE -ne 124 ]; then
  _needs_retry=0
  _retry_reason=""
  if grep -qi "rate.limit\|too many\|429\|quota\|overloaded" "$LOG_FILE" 2>/dev/null; then
    _needs_retry=1; _retry_reason="rate_limit"
  elif grep -qi "authentication_error\|OAuth.*expired\|401.*auth\|Failed to authenticate\|Credit balance\|credit.*low\|balance.*too low" "$LOG_FILE" 2>/dev/null; then
    _needs_retry=1; _retry_reason="auth_error"
  fi
  if [ "$_needs_retry" = "1" ]; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [RETRY] ${_retry_reason} 감지 — 계정 스위치 후 재시도" >> "$LOG_FILE"
    _switch_result=$(switch_account 2>&1)
    echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [RETRY] ${_switch_result}" >> "$LOG_FILE"
    # 스위치 후 새 API Key 로드
    _NEW_KEY=$(_load_api_key)
    [ -n "$_NEW_KEY" ] && export ANTHROPIC_API_KEY="$_NEW_KEY"
    sleep 5
    timeout ${MAX_TIMEOUT} su - claudebot -c \
      "cd ${WORKDIR} && \$(which claude 2>/dev/null || echo /usr/local/bin/claude) -p \
      --dangerously-skip-permissions --max-turns ${MAX_TURNS} --model ${MODEL} --output-format json \
      '중요: 작업 디렉토리는 ${WORKDIR} 이다. cat ${DIRECTIVE_FILE} 파일을 읽고 지시대로 모두 실행하라. 결과를 ${RESULT_FILE} 에 저장하라.'" \
      >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
    echo "[$(date +'%Y-%m-%d %H:%M:%S KST')] [RETRY] 재시도 완료 (exit_code: $EXIT_CODE)" >> "$LOG_FILE"
  fi
fi
# === Rate Limit / Auth 오류 재시도 끝 ===

if [ $EXIT_CODE -eq 124 ]; then
    cat > "$RESULT_FILE" <<EOF
---
project: ${PROJECT}
task_id: TIMEOUT
completed_at: $(date '+%Y-%m-%d %H:%M:%S KST')
status: timeout
---
## 타임아웃 종료 (${MAX_TIMEOUT}초)
$(tail -20 "$LOG_FILE")
EOF
    aads_lifecycle "$_TASK_ID" "$PROJECT" "failed" "timeout_${MAX_TIMEOUT}s"
    bash "$TELEGRAM_SCRIPT" "⏰ [${PROJECT}] 타임아웃 (${MAX_TIMEOUT}초): ${FILENAME}" 2>/dev/null
    aads_queue_msg "${PROJECT}" "chat" "⏰ [${PROJECT}] 타임아웃 종료 (${MAX_TIMEOUT}초)
파일: ${FILENAME}
상태: timeout"
    aads_queue_msg "${PROJECT}" "telegram" "⏰ [${PROJECT}] 타임아웃: ${FILENAME}"
    # AADS-139: source_channel에 결과 보고
    [ -n "$_SOURCE_CHANNEL" ] && [ "$_SOURCE_CHANNEL" != "$PROJECT" ] && \
        aads_queue_msg "${_SOURCE_CHANNEL}" "chat" "[AADS] ${_TASK_ID} timeout
소요: ${MAX_TIMEOUT}초 초과
커밋: N/A
결과: TIMEOUT
다음: 지시 대기"
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
    aads_lifecycle "$_TASK_ID" "$PROJECT" "failed" "exit_code_${EXIT_CODE}"
    bash "$TELEGRAM_SCRIPT" "❌ [${PROJECT}] 에러 종료 (code:${EXIT_CODE}): ${FILENAME}" 2>/dev/null
    aads_queue_msg "${PROJECT}" "chat" "❌ [${PROJECT}] 에러 종료 (code:${EXIT_CODE})
파일: ${FILENAME}
$(tail -5 "$LOG_FILE" 2>/dev/null)"
    aads_queue_msg "${PROJECT}" "telegram" "❌ [${PROJECT}] 에러 (code:${EXIT_CODE}): ${FILENAME}"
    # AADS-139: source_channel에 결과 보고
    [ -n "$_SOURCE_CHANNEL" ] && [ "$_SOURCE_CHANNEL" != "$PROJECT" ] && \
        aads_queue_msg "${_SOURCE_CHANNEL}" "chat" "[AADS] ${_TASK_ID} failed
소요: N/A
커밋: N/A
결과: FAIL (exit_code: ${EXIT_CODE})
다음: 지시 대기"
else
    bash "$TELEGRAM_SCRIPT" "✅ [${PROJECT}] 작업 완료: ${FILENAME}" 2>/dev/null
    # RESULT 파일에서 task_id 추출 (더 정확한 값으로 업데이트)
    _task_id=$(grep -m1 '^task_id:' "$RESULT_FILE" 2>/dev/null | awk '{print $2}')
    [ -n "$_task_id" ] && [ "$_task_id" != "ERROR" ] && [ "$_task_id" != "TIMEOUT" ] && _TASK_ID="$_task_id"
    aads_lifecycle "$_TASK_ID" "$PROJECT" "completed"
    aads_cost "$_TASK_ID" "$PROJECT" "$LOG_FILE"
    aads_commit "$_TASK_ID" "$RESULT_FILE"
    aads_queue_msg "${PROJECT}" "chat" "✅ [${PROJECT}] 작업 완료
Task: ${_TASK_ID:-${FILENAME%.md}}
파일: ${FILENAME}
보고서: https://github.com/moongoby/project-docs/blob/master"
    aads_queue_msg "${PROJECT}" "telegram" "✅ [${PROJECT}] 완료: ${_TASK_ID:-${FILENAME}}"
    # AADS-139: source_channel에 결과 보고
    _commit_sha=$(grep -m1 'commit_sha\|커밋' "$RESULT_FILE" 2>/dev/null | grep -oE '[0-9a-f]{7,40}' | head -1)
    [ -n "$_SOURCE_CHANNEL" ] && [ "$_SOURCE_CHANNEL" != "$PROJECT" ] && \
        aads_queue_msg "${_SOURCE_CHANNEL}" "chat" "[AADS] ${_TASK_ID} completed
소요: N/A
커밋: ${_commit_sha:-N/A}
결과: PASS
다음: 지시 대기"

    # ── P2(15분 이하)/P3: 자동 health-check ──────────────────────────────
    if [ "$PRIORITY" = "P2" ] || [ "$PRIORITY" = "P3" ]; then
        echo "Auto health-check (5min wait)..."
        sleep 300
        HEALTH=$(curl -s "${AADS_OPS_URL}/health-check" --connect-timeout 10 --max-time 15 2>/dev/null)
        HEALTHY=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pipeline_healthy',False))" 2>/dev/null)
        if [ "$HEALTHY" != "True" ]; then
            echo "HEALTH_CHECK_FAILED — auto-generating WRAP file"
            cat > "/root/.genspark/directives/done/${_TASK_ID}_WRAP_AUTO.md" <<EOF
# ${_TASK_ID} Auto Wrap up (health-check 실패)
- date: $(date '+%Y-%m-%d %H:%M:%S')
- health_check: FAILED
- pipeline_healthy: $HEALTHY
- action_required: CEO 확인 필요
EOF
            bash "$TELEGRAM_SCRIPT" "⚠️ ${_TASK_ID} health-check 실패 — WRAP 자동 생성" 2>/dev/null
        fi
    fi
    # ─────────────────────────────────────────────────────────────────────
fi

# running 파일 정리
rm -f "${RUNNING_DIR}/${FILENAME}" 2>/dev/null

echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] ${PROJECT} 실행 완료 (exit:${EXIT_CODE})" >> "${LOG_DIR}/auto_trigger.log"
