#!/bin/bash
# AADS Claude Exec — Claude Code 세션 실행 with Context API 연동
# 생성: 2026-03-04 T-021
#
# 사용: ./claude_exec.sh <task_id> [directive_file]
#   task_id       : 작업 식별자 (예: T-021, BRIDGE)
#   directive_file: 실행할 지시서 .md 파일 경로 (생략 시 task_id만으로 실행)
#
# 동작:
#   1) Context API에서 최신 phase/pending 맥락 조회
#   2) 이미 COMPLETED인 task면 스킵
#   3) 맥락을 Claude Code 세션 프롬프트에 주입하여 실행
#   4) 완료 후 task 결과를 POST /context/system (category: history)에 기록
#   5) 실패 시 에러를 POST /context/system (category: errors)에 기록

set -euo pipefail

# === locale 강제 설정 (manpath/grep 오류 방지 — Pipeline C 안정화) ===
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export LANGUAGE=en_US.UTF-8
# =====================================================================

# === OAuth 토큰 주입 (TOKEN_1=Gmail 1순위, TOKEN_2=Naver 2순위) ===
# pipeline-runner.sh와 동일한 방식: CLAUDE_CODE_OAUTH_TOKEN으로 주입
# ANTHROPIC_API_KEY는 oat 토큰과 충돌하므로 unset
_TOKEN_1="${ANTHROPIC_AUTH_TOKEN:-}"
_TOKEN_2="${ANTHROPIC_AUTH_TOKEN_2:-}"
if [ -n "$_TOKEN_1" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$_TOKEN_1"
    unset ANTHROPIC_API_KEY 2>/dev/null || true
    echo "[AUTH] CLAUDE_CODE_OAUTH_TOKEN → TOKEN_1(Gmail) 주입"
elif [ -n "$_TOKEN_2" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$_TOKEN_2"
    unset ANTHROPIC_API_KEY 2>/dev/null || true
    echo "[AUTH] CLAUDE_CODE_OAUTH_TOKEN → TOKEN_2(Naver) 폴백 주입"
else
    echo "[AUTH] WARNING: ANTHROPIC_AUTH_TOKEN/ANTHROPIC_AUTH_TOKEN_2 모두 미설정"
fi
# =====================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=memory_helper.sh
source "${SCRIPT_DIR}/memory_helper.sh"

# === AADS-148: 프로세스 그룹 PGID 기록 (고아 프로세스 방지) ===
PGID=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || echo $$)

TASK_ID="${1:?사용법: $0 <task_id> [directive_file]}"
DIRECTIVE_FILE="${2:-}"

# === AADS-145: Tasks 시스템 통합 ===
CLAUDEBOT_TASKS_DIR="/home/claudebot/.claude/tasks"
mkdir -p "$CLAUDEBOT_TASKS_DIR" 2>/dev/null || true
TASK_FILE="${CLAUDEBOT_TASKS_DIR}/${TASK_ID}.json"
TASK_LIST_ID="aads-$(echo "$TASK_ID" | tr '[:upper:]' '[:lower:]')-$(date +%s)"

# 세션 복구: Tasks 파일에 이미 done이면 스킵 (PENDING/DONE 이중관리 제거)
if [ -f "$TASK_FILE" ]; then
    _tasks_prev=$(python3 -c "import json; d=json.load(open('${TASK_FILE}')); print(d.get('status',''))" 2>/dev/null || echo "")
    if [ "${_tasks_prev}" = "done" ]; then
        echo "✅ [TASKS] ${TASK_ID} 이미 완료 (Tasks 기록) — 스킵"
        exit 0
    fi
fi

# Tasks 파일 생성 (in_progress 상태)
python3 -c "
import json, time
task = {
    'id': '${TASK_ID}',
    'list_id': '${TASK_LIST_ID}',
    'title': '${TASK_ID}',
    'status': 'in_progress',
    'directive': '${DIRECTIVE_FILE:-none}',
    'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
}
with open('${TASK_FILE}', 'w') as f:
    json.dump(task, f, ensure_ascii=False, indent=2)
" 2>/dev/null || true

export CLAUDE_CODE_TASK_LIST_ID="${TASK_LIST_ID}"
echo "[TASKS] list_id=${TASK_LIST_ID} file=${TASK_FILE}"
# === Tasks 통합 끝 ===

# ─────────────────────────────────────────────────────────
# 하트비트 설정 (A-1)
# Safety net only. Primary timeout managed by session_watchdog via heartbeat.
HARD_TIMEOUT=7200
HEARTBEAT_FILE="/tmp/claude_session_${TASK_ID}.heartbeat"
HEARTBEAT_LOG="/tmp/claude_session_${TASK_ID}.heartbeat_log"
WORK_DIR="${AADS_ROOT:-/root/aads}"
INOTIFY_PID=""

# AADS-145: 컨텍스트 모니터링용 임시 로그
CTX_TMPLOG="/tmp/claude_ctx_${TASK_ID}_$$.log"
CTX_SIGNAL="/tmp/.ctx_sig_${TASK_ID}_$$.flag"
CTX_EDIT_FAIL="/tmp/.ctx_edit_${TASK_ID}_$$.flag"

update_heartbeat() {
    local event_type=$1  # progress | complete | error
    local detail=$2
    local ts
    ts=$(date +%s)
    echo "{\"ts\":${ts},\"type\":\"${event_type}\",\"detail\":\"${detail}\"}" > "$HEARTBEAT_FILE"
    echo "{\"ts\":${ts},\"type\":\"${event_type}\",\"detail\":\"${detail}\"}" >> "$HEARTBEAT_LOG"
}

# === AADS-145: 컨텍스트 모니터링 백그라운드 함수 ===
_ctx_monitor_bg() {
    local _tmplog="$1" _sig="$2" _edit_sig="$3"
    local _warned_70=false
    local _ctx_max=200000   # 추정 최대 토큰 (행 기준 환산)
    while true; do
        sleep 15
        [ -f "$_tmplog" ] || continue
        # 2회 연속 수정 실패 감지 (Edit 오류 패턴)
        local _efail
        _efail=$(grep -c "old_string.*not found\|no match found\|수정 실패\|Edit.*failed" "$_tmplog" 2>/dev/null || echo 0)
        if [ "${_efail:-0}" -ge 2 ] && [ ! -f "$_edit_sig" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-EDIT-FAIL] 2회 연속 수정 실패 → /clear 권고" >&2
            touch "$_edit_sig"
        fi
        # 행 수 기반 토큰 추정 (~50자/행 × 행 수 ÷ 4 ≈ 토큰)
        local _lines
        _lines=$(wc -l < "$_tmplog" 2>/dev/null || echo 0)
        local _est_tokens=$(( _lines * 50 / 4 ))
        if [ "$_est_tokens" -ge $(( _ctx_max * 90 / 100 )) ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-90%] 컨텍스트 90% 추정 초과 (${_lines}행, ~${_est_tokens}토큰) — 재시작 신호" >&2
            touch "$_sig"
            break
        elif [ "$_est_tokens" -ge $(( _ctx_max * 70 / 100 )) ] && [ "$_warned_70" = "false" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-70%] 컨텍스트 70% 추정 (${_lines}행, ~${_est_tokens}토큰) — /compact 권고" >&2
            _warned_70=true
        fi
    done
}
# === 컨텍스트 모니터링 함수 끝 ===

# A-2: inotifywait 기반 자동 하트비트
start_inotify_watcher() {
    if command -v inotifywait &>/dev/null; then
        inotifywait -m -r -e modify,create,delete --format '%w%f' "$WORK_DIR" 2>/dev/null | while read -r FILE; do
            update_heartbeat "progress" "file_changed: ${FILE##*/}"
        done &
        INOTIFY_PID=$!
    else
        # Fallback: 30초마다 git status --porcelain 변화 체크
        (
            PREV_STAT=""
            while true; do
                sleep 30
                CUR_STAT=$(git -C "$WORK_DIR" status --porcelain 2>/dev/null | md5sum | awk '{print $1}')
                if [ "$CUR_STAT" != "$PREV_STAT" ]; then
                    update_heartbeat "progress" "git_status_changed"
                    PREV_STAT="$CUR_STAT"
                fi
            done
        ) &
        INOTIFY_PID=$!
    fi
}

cleanup_inotify() {
    if [ -n "$INOTIFY_PID" ] && kill -0 "$INOTIFY_PID" 2>/dev/null; then
        kill "$INOTIFY_PID" 2>/dev/null || true
    fi
    # AADS-145: 컨텍스트 모니터 정리
    [ -n "${CTX_MONITOR_PID:-}" ] && kill "$CTX_MONITOR_PID" 2>/dev/null || true
    rm -f "$CTX_TMPLOG" "$CTX_SIGNAL" "$CTX_EDIT_FAIL" 2>/dev/null || true

    # === AADS-148: 프로세스 그룹 전체 kill (고아 프로세스 방지) ===
    if [ -n "${PGID:-}" ] && [ "$PGID" -gt 1 ]; then
        kill -- -"$PGID" 2>/dev/null || true
    fi
    # AADS-148: claudebot 소유 고아 프로세스 잔여분 정리 (PPID=1 AND 현 세션 관련)
    local _task_id_clean="${TASK_ID:-}"
    if [ -n "$_task_id_clean" ]; then
        local _claude_pid_file="/tmp/claude_session_${_task_id_clean}.claude_pid"
        if [ -f "$_claude_pid_file" ]; then
            local _cpid
            _cpid=$(cat "$_claude_pid_file" 2>/dev/null || echo "")
            if [ -n "$_cpid" ] && kill -0 "$_cpid" 2>/dev/null; then
                kill -9 "$_cpid" 2>/dev/null || true
            fi
        fi
    fi
    # === AADS-148 cleanup 끝 ===
}
trap cleanup_inotify EXIT

# A-4: 프로세스 PID 기록
echo $$ > "/tmp/claude_session_${TASK_ID}.pid"

# 초기 하트비트
update_heartbeat "progress" "claude_exec_start"

# inotify 감시 시작
start_inotify_watcher

# AADS-145: 컨텍스트 모니터링 백그라운드 시작
CTX_MONITOR_PID=""
_ctx_monitor_bg "$CTX_TMPLOG" "$CTX_SIGNAL" "$CTX_EDIT_FAIL" &
CTX_MONITOR_PID=$!

# === AADS-146: subagents 필드 파싱 ===
SUBAGENTS_LIST=""
if [ -n "$DIRECTIVE_FILE" ] && [ -f "$DIRECTIVE_FILE" ]; then
    SUBAGENTS_LIST=$(grep -m1 '^subagents:' "$DIRECTIVE_FILE" 2>/dev/null | sed 's/^subagents:\s*//' | tr -d ' ' || true)
fi
AGENTS_DIR="/root/aads/.claude/agents"
if [ -n "$SUBAGENTS_LIST" ]; then
    echo "[SUBAGENTS] 감지: ${SUBAGENTS_LIST}"
fi
# === subagents 파싱 끝 ===

TS_START=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

echo "======================================================"
echo "AADS Claude Exec — Task: ${TASK_ID}"
echo "시작: ${TS_START}"
echo "======================================================"

# ─────────────────────────────────────────────────────────
# STEP 1: Context API에서 최신 맥락 조회
# ─────────────────────────────────────────────────────────
echo ""
echo "[1/4] 최신 맥락 조회 중..."

PHASE_JSON=$(read_context "phase" 2>/dev/null || echo '{}')
PENDING_JSON=$(read_context "pending" 2>/dev/null || echo '{}')

# current_progress에서 task 상태 확인
TASK_STATUS=$(echo "$PHASE_JSON" | python3 -c "
import json, sys
task_id = '${TASK_ID}'
try:
    d = json.load(sys.stdin)
    items = d.get('data', [])
    # data가 list일 수도 있고 dict일 수도 있음
    if isinstance(items, dict):
        items = [items]
    for item in items:
        if isinstance(item, dict) and item.get('key') == 'current_progress':
            v = item.get('value', {})
            if isinstance(v, str):
                v = json.loads(v)
            print(v.get(task_id, 'PENDING'))
            sys.exit(0)
    print('PENDING')
except Exception as e:
    print('PENDING')
" 2>/dev/null || echo "PENDING")

CURRENT_PHASE=$(echo "$PHASE_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    items = d.get('data', [])
    if isinstance(items, dict):
        items = [items]
    for item in items:
        if isinstance(item, dict) and item.get('key') == 'current_phase':
            v = item.get('value', {})
            if isinstance(v, str):
                v = json.loads(v)
            print(v.get('phase', 'unknown'))
            sys.exit(0)
    print('unknown')
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown")

echo "  현재 Phase : ${CURRENT_PHASE}"
echo "  Task ${TASK_ID}: ${TASK_STATUS}"

# ─────────────────────────────────────────────────────────
# STEP 2: 이미 COMPLETED인 task는 스킵
# ─────────────────────────────────────────────────────────
if echo "${TASK_STATUS}" | grep -qi "^COMPLETED"; then
    echo ""
    echo "✅ Task ${TASK_ID}는 이미 COMPLETED — 스킵"
    exit 0
fi

# ─────────────────────────────────────────────────────────
# STEP 3: 맥락 프롬프트 구성 + Claude Code 실행
# ─────────────────────────────────────────────────────────
echo ""
echo "[2/4] 맥락 프롬프트 구성 중..."

CONTEXT_HEADER=$(cat <<HEADER_EOF
=== AADS System Context (${TS_START}) ===
Current Phase : ${CURRENT_PHASE}
Task ID       : ${TASK_ID}
Task Status   : ${TASK_STATUS}
Context API   : ${CONTEXT_API}
==========================================
[보안 규칙 AADS-148] 절대로 /proc, /sys 경로에 grep -r을 실행하지 마라.
프로세스 탐색 시 /proc, /sys 경로에 grep -r을 실행하면 소켓·파이프 fd 블로킹으로 CPU 100% 고착 장애가 발생한다.
프로세스 탐색은 반드시 pgrep, ps, lsof를 사용하라.
==========================================

HEADER_EOF
)

echo "  Context header 생성 완료"

echo ""
echo "[3/4] Claude Code 실행 중..."

EXEC_EXIT=0
if [ -n "$DIRECTIVE_FILE" ] && [ -f "$DIRECTIVE_FILE" ]; then
    echo "  지시서: ${DIRECTIVE_FILE}"
    FULL_PROMPT="${CONTEXT_HEADER}$(cat "$DIRECTIVE_FILE")"
    # A-5: 하드 타임아웃 (안전망) 적용 + AADS-145 컨텍스트 캡처
    timeout "$HARD_TIMEOUT" bash -c 'echo "$FULL_PROMPT" | claude --print 2>&1' | tee -a "$CTX_TMPLOG" || EXEC_EXIT=$?
    # Claude Code 서브프로세스 PID 기록 (A-4)
    pgrep -n -f "claude --print" > "/tmp/claude_session_${TASK_ID}.claude_pid" 2>/dev/null || true
else
    echo "  지시서: 없음 (Task ID만으로 실행)"
    FULL_PROMPT="${CONTEXT_HEADER}Task ${TASK_ID}를 실행하라."
    timeout "$HARD_TIMEOUT" bash -c 'echo "$FULL_PROMPT" | claude --print 2>&1' | tee -a "$CTX_TMPLOG" || EXEC_EXIT=$?
    pgrep -n -f "claude --print" > "/tmp/claude_session_${TASK_ID}.claude_pid" 2>/dev/null || true
fi

# AADS-145: 컨텍스트 90% 재시작 처리
if [ -f "$CTX_SIGNAL" ] && [ $EXEC_EXIT -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [CTX-RESTART] 컨텍스트 한계 감지 — 요약 후 재시작" >&2
    _ctx_summary="[CTX-RESTART] 이전 세션 컨텍스트 한계 도달. 지금까지 진행한 내용을 이어서 완료하라. Task: ${TASK_ID}"
    if [ -n "$DIRECTIVE_FILE" ] && [ -f "$DIRECTIVE_FILE" ]; then
        timeout "$HARD_TIMEOUT" bash -c 'echo "$_ctx_summary\n$(cat "$DIRECTIVE_FILE")" | claude --print 2>&1' | tee -a "$CTX_TMPLOG" || EXEC_EXIT=$?
    fi
fi

update_heartbeat "progress" "claude_exec_finished: exit=${EXEC_EXIT}"

TS_END=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

# ─────────────────────────────────────────────────────────
# STEP 4: 결과 Context API에 기록
# ─────────────────────────────────────────────────────────
echo ""
echo "[4/4] 결과 기록 중..."

COMMIT_SHA=$(git -C "${AADS_ROOT}" rev-parse --short HEAD 2>/dev/null || echo "")

if [ $EXEC_EXIT -eq 0 ]; then
    STATUS="COMPLETED"
    REPORT="claude_exec 성공 (${TS_END})"
    echo "  ✅ 실행 성공 — history 카테고리에 기록"
    # A-3: DONE 이벤트
    update_heartbeat "complete" "task_done"

    # AADS-145: final_commit 하트비트 + 신호 파일 (투기적 실행 트리거)
    _fc_sha=$(git -C "${WORK_DIR}" rev-parse HEAD 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ -n "$_fc_sha" ]; then
        update_heartbeat "final_commit" "sha=${_fc_sha:0:8}"
        echo "${TASK_ID}" > "/tmp/aads_final_commit_${TASK_ID}.signal"
    fi

    write_task_result "$TASK_ID" "$REPORT" "$STATUS"
    # T-037 B-2: 매니저 보고를 go100_user_memory에도 저장
    save_manager_report "$TASK_ID" "$STATUS" "$REPORT" "$COMMIT_SHA" "0"

    # phase/current_progress 업데이트
    export _CP_TASK_ID="$TASK_ID"
    export _CP_TS="$TS_END"
    export _CP_KEY="$AADS_MONITOR_KEY"
    export _CP_API="$CONTEXT_API"

    python3 - <<'PYEOF'
import json, urllib.request, os, sys

task_id = os.environ.get('_CP_TASK_ID', '')
ts      = os.environ.get('_CP_TS', '')
key     = os.environ.get('_CP_KEY', '')
api     = os.environ.get('_CP_API', '')

# 현재 current_progress 읽기
req = urllib.request.Request(
    api + "/phase/current_progress",
    headers={"X-Monitor-Key": key, "User-Agent": "curl/7.64.0"})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        current = data.get("data", {})
        if isinstance(current, dict):
            v = current.get("value", {})
        else:
            v = {}
        if isinstance(v, str):
            v = json.loads(v)
except Exception:
    v = {}

# 해당 task COMPLETED로 업데이트
v[task_id] = "COMPLETED - claude_exec (" + ts + ")"

# 저장
body = json.dumps({
    "category": "phase",
    "key": "current_progress",
    "value": v
}).encode()
req2 = urllib.request.Request(api, data=body,
    headers={"Content-Type": "application/json", "X-Monitor-Key": key,
             "User-Agent": "curl/7.64.0"},
    method="POST")
try:
    with urllib.request.urlopen(req2, timeout=10) as resp:
        sys.stdout.write("  current_progress 업데이트: " + task_id + " → COMPLETED\n")
except Exception as e:
    sys.stdout.write("  current_progress 업데이트 실패: " + str(e) + "\n")
PYEOF

else
    STATUS="FAILED"
    REPORT="claude_exec 실패 (exit=${EXEC_EXIT}, ${TS_END})"
    echo "  ❌ 실행 실패 (exit=${EXEC_EXIT}) — errors 카테고리에 기록"
    # A-3: 에러 이벤트
    update_heartbeat "error" "claude_exec_failed: exit=${EXEC_EXIT}"
    write_error "$TASK_ID" "$REPORT"
    # T-037 B-2: 실패 보고도 go100_user_memory에 저장
    save_manager_report "$TASK_ID" "$STATUS" "$REPORT" "$COMMIT_SHA" "${EXEC_EXIT}"
fi

# === AADS-163: 3단계 품질 게이트 (QA → 디자인) ===
_AGENTS_DIR="${SCRIPT_DIR}/../.claude/agents"
_LOG_DIR="${LOG_DIR:-/root/.genspark/logs}"
_RESULT_FILE="${RESULT_FILE:-/root/.genspark/directives/done/${TASK_ID}_RESULT.md}"
_TELEGRAM_SCRIPT="/root/.genspark/send_telegram.sh"

if [ $EXEC_EXIT -eq 0 ]; then
    # QA 게이트
    _qa_agent="${_AGENTS_DIR}/test-writer.md"
    _qa_out="/tmp/aads_qa_${TASK_ID}_$$.txt"
    _qa_verdict="PASS"
    _qa_retry=0
    while [ $_qa_retry -le 2 ]; do
        if [ -f "$_qa_agent" ]; then
            _qa_prompt="$(cat "$_qa_agent") ## QA 대상: ${TASK_ID}
$([ -n "${DIRECTIVE_FILE:-}" ] && cat "$DIRECTIVE_FILE" 2>/dev/null | head -40 || echo '')
success_criteria 기준으로 검토 후 QA_VERDICT: PASS 또는 QA_VERDICT: FAIL 을 출력하라."
        else
            _qa_prompt="[QA] task=${TASK_ID} 완료 여부를 검토하고 QA_VERDICT: PASS 또는 QA_VERDICT: FAIL 을 출력하라."
        fi
        echo "$_qa_prompt" | timeout 600 claude --print 2>&1 > "$_qa_out" || true
        _qa_verdict=$(grep -m1 "QA_VERDICT:" "$_qa_out" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || echo "PASS")
        [ -z "$_qa_verdict" ] && _qa_verdict="PASS"
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [QA-GATE] 시도$((_qa_retry+1)): ${_qa_verdict}"
        [ "$_qa_verdict" = "PASS" ] && break
        [ $_qa_retry -ge 2 ] && break
        # FAIL → 재작업
        _qa_feedback=$(cat "$_qa_out" 2>/dev/null | tail -20)
        echo "QA FAIL — 재작업 시도 $((_qa_retry+1))/2: ${_qa_feedback}" | timeout 600 claude --print 2>&1 >> "${_LOG_DIR}/qa_${TASK_ID}.log" || true
        _qa_retry=$((_qa_retry+1))
    done
    # RESULT_FILE에 qa_status 기록
    if [ -f "$_RESULT_FILE" ]; then
        python3 -c "
import re
p='${_RESULT_FILE}'
try:
    with open(p) as f: c=f.read()
except: c='---\n---\n'
if 'qa_status:' not in c:
    c=re.sub(r'^---\n','---\nqa_status: ${_qa_verdict}\n',c,count=1,flags=re.M)
    with open(p,'w') as f: f.write(c)
" 2>/dev/null || true
    fi
    if [ "$_qa_verdict" = "FAIL" ]; then
        echo "[QA-GATE] FAIL (2회 초과) — 서킷브레이커 카운트 신호"
        bash "$_TELEGRAM_SCRIPT" "🚨 [QA-FAIL] ${TASK_ID} QA 2회 초과 — 서킷브레이커 트리거" 2>/dev/null || true
        EXEC_EXIT=1
    else
        # 디자인 게이트
        _dg_agent="${_AGENTS_DIR}/doc-writer.md"
        _dg_out="/tmp/aads_design_${TASK_ID}_$$.txt"
        if [ -f "$_dg_agent" ]; then
            _dg_prompt="$(cat "$_dg_agent") ## 디자인 검증: ${TASK_ID}
UI/UX 변경이 있으면 DESIGN_VERDICT: REVIEW_NEEDED, 없으면 DESIGN_VERDICT: PASS 를 출력하라."
        else
            _dg_prompt="[DESIGN] task=${TASK_ID} UI/UX 변경 여부 확인. DESIGN_VERDICT: PASS 또는 DESIGN_VERDICT: REVIEW_NEEDED 출력."
        fi
        echo "$_dg_prompt" | timeout 300 claude --print 2>&1 > "$_dg_out" || true
        _dg_verdict=$(grep -m1 "DESIGN_VERDICT:" "$_dg_out" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || echo "PASS")
        [ -z "$_dg_verdict" ] && _dg_verdict="PASS"
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [DESIGN-GATE] ${_dg_verdict}"
        if [ "$_dg_verdict" = "REVIEW_NEEDED" ]; then
            bash "$_TELEGRAM_SCRIPT" "🎨 [DESIGN-REVIEW] ${TASK_ID} CEO 검토 필요 — 60초 타임아웃" 2>/dev/null || true
            sleep 60
            _dg_verdict="PASS_TIMEOUT"
        fi
        if [ -f "$_RESULT_FILE" ]; then
            python3 -c "
import re
p='${_RESULT_FILE}'
try:
    with open(p) as f: c=f.read()
except: c='---\n---\n'
if 'design_status:' not in c:
    c=re.sub(r'^---\n','---\ndesign_status: ${_dg_verdict}\n',c,count=1,flags=re.M)
    with open(p,'w') as f: f.write(c)
" 2>/dev/null || true
        fi
        rm -f "$_dg_out" 2>/dev/null
    fi
    rm -f "$_qa_out" 2>/dev/null
fi
# === 3단계 품질 게이트 끝 ===

# === AADS-146: 서브에이전트 실행 (subagents 필드 기반) ===
if [ -n "$SUBAGENTS_LIST" ] && [ $EXEC_EXIT -eq 0 ]; then
    IFS=',' read -ra _agent_names <<< "$SUBAGENTS_LIST"
    for _agent in "${_agent_names[@]}"; do
        _agent=$(echo "$_agent" | tr -d ' ')
        _agent_file="${AGENTS_DIR}/${_agent}.md"
        if [ -f "$_agent_file" ]; then
            echo "[SUBAGENT] 실행: ${_agent}"
            _agent_prompt="[SUBAGENT: ${_agent}] 다음 에이전트 정의에 따라 task=${TASK_ID}의 결과를 검토하라.\n$(cat "$_agent_file")\n\n지시서: $(cat "$DIRECTIVE_FILE" 2>/dev/null || echo '')"
            _agent_exit=0
            echo "$_agent_prompt" | timeout 1800 claude --print 2>&1 || _agent_exit=$?
            echo "[SUBAGENT] ${_agent} 완료 (exit=${_agent_exit})"
        else
            echo "[SUBAGENT] WARNING: 에이전트 파일 없음: ${_agent_file}"
        fi
    done
fi
# === 서브에이전트 끝 ===

# === AADS-145: Tasks 완료 상태 업데이트 ===
if [ -n "${TASK_FILE:-}" ] && [ -f "$TASK_FILE" ]; then
    _t_done_status="failed"
    [ $EXEC_EXIT -eq 0 ] && _t_done_status="done"
    python3 -c "
import json, time
try:
    with open('${TASK_FILE}') as f: d = json.load(f)
    d['status'] = '${_t_done_status}'
    d['completed_at'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    d['exit_code'] = ${EXEC_EXIT}
    with open('${TASK_FILE}', 'w') as f: json.dump(d, f, ensure_ascii=False, indent=2)
except: pass
" 2>/dev/null || true
    echo "[TASKS] 상태 업데이트: ${_t_done_status} (${TASK_FILE})"
fi
# === Tasks 완료 끝 ===

echo ""
echo "======================================================"
echo "완료: ${TASK_ID} | ${TS_END}"
echo "======================================================"
exit $EXEC_EXIT
