#!/bin/bash
# AADS Auto Trigger — pending 지시서 감지 + 자동 실행
# 생성: 2026-03-04 T-021
# T-106: P0→P1→P2 우선순위 실행, 긴급 선점(PREEMPT_P0), 파일명 우선순위
#
# 사용:
#   ./auto_trigger.sh              # DIRECTIVES_DIR의 모든 pending 지시서 처리
#   DIRECTIVES_DIR=/path ./auto_trigger.sh
#   ./auto_trigger.sh --dry-run    # 선택 로직만 확인, 실제 이동/실행 없음
#   PREEMPT_P0=true ./auto_trigger.sh  # P0 선점 모드
#
# 동작:
#   1) PENDING_DIR에서 P0→P1→P2 우선순위로 지시서 파일 선택
#   2) PREEMPT_P0=true이면 running 작업을 pending으로 되돌리고 P0 즉시 실행
#   3) 선택된 파일을 RUNNING_DIR로 이동
#   4) 각 파일에서 Task ID 추출
#   5) Context API에서 phase/current_progress 조회
#   6) 이미 COMPLETED인 task는 스킵
#   7) PENDING task는 claude_exec.sh로 실행
#   8) 완료 후 current_phase.last_completed 자동 POST
#   9) 우선순위 선택 로그 → /var/log/aads/auto_trigger_priority.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=memory_helper.sh
source "${SCRIPT_DIR}/memory_helper.sh"

DIRECTIVES_DIR="${DIRECTIVES_DIR:-/root/.genspark/directives/running}"
DONE_DIR="${DONE_DIR:-/root/.genspark/directives/done}"

# T-106: 우선순위 디렉토리 및 로그 경로
PENDING_DIR="${PENDING_DIR:-/root/.genspark/directives/pending}"
RUNNING_DIR="${RUNNING_DIR:-/root/.genspark/directives/running}"
PRIORITY_LOG="/var/log/aads/auto_trigger_priority.log"

# T-106: --dry-run 플래그 파싱
DRY_RUN=false
for _arg in "$@"; do
    [ "$_arg" = "--dry-run" ] && DRY_RUN=true
done

TS_START=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

echo "======================================================"
echo "AADS Auto Trigger"
echo "감시 디렉토리: ${DIRECTIVES_DIR}"
echo "시작: ${TS_START}"
echo "======================================================"

# ─── T-106: 우선순위 로그 함수 ──────────────────────────────
_log_priority() {
    local p0="$1" p1="$2" p2="$3" selected="$4"
    local log_dir="/var/log/aads"
    mkdir -p "$log_dir" 2>/dev/null || true
    if [ -n "$selected" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') | SCAN: P0=${p0}, P1=${p1}, P2=${p2} | SELECTED: ${selected}" \
            >> "${PRIORITY_LOG}" 2>/dev/null || true
    fi
}

# ─── T-106: pending에서 우선순위 기반 파일 선택 함수 ─────────
_select_next_file() {
    local pending_dir="$1"

    # 파일 수 집계
    local p0_content_count p1_content_count total_count
    p0_content_count=$(grep -rl "P0-CRITICAL" "${pending_dir}"/*.md 2>/dev/null | wc -l || echo 0)
    p1_content_count=$(grep -rl "P1-HIGH" "${pending_dir}"/*.md 2>/dev/null | wc -l || echo 0)
    total_count=$(ls "${pending_dir}"/*.md 2>/dev/null | wc -l || echo 0)
    local p2_count=$(( total_count - p0_content_count - p1_content_count ))
    [ "$p2_count" -lt 0 ] && p2_count=0

    local next_file="" reason=""

    # 1순위: 파일명에 _P0_ 포함
    next_file=$(ls "${pending_dir}"/*_P0_*.md 2>/dev/null | head -1 || true)
    if [ -n "$next_file" ]; then
        reason="filename P0 priority"
    fi

    # 2순위: 내용에 P0-CRITICAL 포함
    if [ -z "$next_file" ]; then
        next_file=$(grep -rl "P0-CRITICAL" "${pending_dir}"/*.md 2>/dev/null | head -1 || true)
        [ -n "$next_file" ] && reason="content P0-CRITICAL priority"
    fi

    # 3순위: 파일명에 _P1_ 포함
    if [ -z "$next_file" ]; then
        next_file=$(ls "${pending_dir}"/*_P1_*.md 2>/dev/null | head -1 || true)
        [ -n "$next_file" ] && reason="filename P1 priority"
    fi

    # 4순위: 내용에 P1-HIGH 포함
    if [ -z "$next_file" ]; then
        next_file=$(grep -rl "P1-HIGH" "${pending_dir}"/*.md 2>/dev/null | head -1 || true)
        [ -n "$next_file" ] && reason="content P1-HIGH priority"
    fi

    # 5순위: 기존 FIFO (가장 오래된 파일)
    if [ -z "$next_file" ]; then
        next_file=$(ls -t "${pending_dir}"/*.md 2>/dev/null | tail -1 || true)
        [ -n "$next_file" ] && reason="FIFO (P2-NORMAL)"
    fi

    if [ -n "$next_file" ]; then
        _log_priority "$p0_content_count" "$p1_content_count" "$p2_count" \
            "$(basename "$next_file") | REASON: $reason"
        echo "$next_file"
    fi
}

# ─── T-106: PREEMPT_P0 긴급 선점 처리 ──────────────────────
if [ "${PREEMPT_P0:-false}" = "true" ]; then
    P0_EXISTS=$(grep -rl "P0-CRITICAL" "${PENDING_DIR}"/*.md 2>/dev/null | head -1 || true)
    RUNNING_EXISTS=$(ls "${RUNNING_DIR}"/*.md 2>/dev/null | head -1 || true)
    if [ -n "$P0_EXISTS" ] && [ -n "$RUNNING_EXISTS" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') P0 PREEMPT: Moving running task back to pending"
        mv "${RUNNING_DIR}"/*.md "${PENDING_DIR}/"
    fi
fi

# ─── T-106: pending → running 우선순위 선택 이동 ────────────
if [ -d "$PENDING_DIR" ] && ls "${PENDING_DIR}"/*.md 2>/dev/null | head -1 > /dev/null 2>&1; then
    NEXT_FILE=$(_select_next_file "$PENDING_DIR")
    if [ -n "$NEXT_FILE" ] && [ -f "$NEXT_FILE" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Selected: $(basename "$NEXT_FILE")"
        if [ "$DRY_RUN" = "true" ]; then
            echo "[DRY-RUN] Would move: $NEXT_FILE → $RUNNING_DIR/"
            exit 0
        fi
        mkdir -p "$RUNNING_DIR"
        mv "$NEXT_FILE" "$RUNNING_DIR/"
        DIRECTIVES_DIR="$RUNNING_DIR"
    fi
fi

# ─── 단일 지시서 처리 함수 ───────────────────────────────────
_process_directive() {
    local directive_file="$1"
    local filename
    filename=$(basename "$directive_file")

    # Task ID 추출: 파일 내 "Task ID: T-XXX" 줄 우선, 없으면 파일명에서 추출
    local task_id
    task_id=$(grep -m1 "^Task ID:" "$directive_file" 2>/dev/null \
        | sed 's/^Task ID:[[:space:]]*//' | tr -d '[:space:]') || true
    if [ -z "$task_id" ]; then
        # 파일명 패턴: AADS_YYYYMMDD_HHMMSS_LABEL.md → LABEL
        task_id=$(echo "$filename" \
            | sed 's/^AADS_[0-9]*_[0-9]*_//; s/\.md$//')
    fi

    # T-100: RESULT 파일은 지시서가 아니므로 스킵
    if [[ "$filename" == *"RESULT"* ]]; then
        echo "SKIP: Result file, not a directive — ${filename}"
        return 0
    fi

    echo ""
    echo "--- 지시서: ${filename} | Task: ${task_id} ---"

    # ─── phase/current_progress 최신 조회 ───
    local phase_json
    phase_json=$(read_context "phase" 2>/dev/null || echo '{}')

    local current_status
    current_status=$(echo "$phase_json" | python3 -c "
import json, sys
task_id = '${task_id}'
try:
    d = json.load(sys.stdin)
    items = d.get('data', [])
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
except Exception:
    print('PENDING')
" 2>/dev/null || echo "PENDING")

    echo "  현재 상태: ${current_status}"

    # ─── 이미 COMPLETED면 스킵 ───
    if echo "${current_status}" | grep -qi "^COMPLETED"; then
        echo "  ✅ 이미 COMPLETED — 스킵"
        return 0
    fi

    # ─── claude_exec.sh로 실행 ───
    echo "  🚀 실행 시작..."
    local exec_exit=0
    local ts_exec_start
    ts_exec_start=$(date +%s%3N)
    "${SCRIPT_DIR}/claude_exec.sh" "$task_id" "$directive_file" || exec_exit=$?
    local ts_exec_end
    ts_exec_end=$(date +%s%3N)
    local exec_duration_ms=$(( ts_exec_end - ts_exec_start ))

    # ─── T-092: 비용 자동 추적 ───
    local project="${PROJECT:-AADS}"
    # 결과 파일 경로 추정: directive 파일명에서 _RESULT.md 패턴
    local result_file=""
    if [ -n "$directive_file" ]; then
        local base_name
        base_name=$(basename "$directive_file" .md)
        result_file="${DONE_DIR}/${base_name}_RESULT.md"
    fi

    echo "  💰 비용 추적 중 (task=${task_id}, duration=${exec_duration_ms}ms)..."
    python3 "${SCRIPT_DIR}/cost_tracker.py" record \
        --task-id "$task_id" \
        --project "$project" \
        --result-file "$result_file" 2>&1 || true

    local ts_done
    ts_done=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

    if [ $exec_exit -eq 0 ]; then
        echo "  ✅ 실행 완료: ${task_id} (${ts_done})"

        # current_phase.last_completed 자동 업데이트
        export _AT_TASK_ID="$task_id"
        export _AT_TS="$ts_done"
        export _AT_KEY="$AADS_MONITOR_KEY"
        export _AT_API="$CONTEXT_API"

        python3 - <<'PYEOF'
import json, urllib.request, os, sys

task_id = os.environ.get('_AT_TASK_ID', '')
ts      = os.environ.get('_AT_TS', '')
key     = os.environ.get('_AT_KEY', '')
api     = os.environ.get('_AT_API', '')

# current_phase 읽기
req = urllib.request.Request(
    api + "/phase/current_phase",
    headers={"X-Monitor-Key": key, "User-Agent": "curl/7.64.0"})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        phase = data.get("data", {})
        if isinstance(phase, dict):
            v = phase.get("value", {})
        else:
            v = {}
        if isinstance(v, str):
            v = json.loads(v)
except Exception:
    v = {}

# last_completed 추가/갱신
v["last_completed"] = task_id + " (" + ts + ")"

# 저장
body = json.dumps({
    "category": "phase",
    "key": "current_phase",
    "value": v
}).encode()
req2 = urllib.request.Request(api, data=body,
    headers={"Content-Type": "application/json", "X-Monitor-Key": key,
             "User-Agent": "curl/7.64.0"},
    method="POST")
try:
    with urllib.request.urlopen(req2, timeout=10) as resp:
        sys.stdout.write("  phase 업데이트: last_completed=" + task_id + "\n")
except Exception as e:
    sys.stdout.write("  phase 업데이트 실패: " + str(e) + "\n")
PYEOF

        # AADS-108: 지시서 완료 시 환경 스냅샷 즉시 갱신
        python3 /root/aads/scripts/collect_env_snapshot.py event "task_completed_${task_id}" &

    else
        echo "  ❌ 실행 실패: ${task_id} (exit=${exec_exit})"
        # T-038: 실행 실패 자동 보고
        report_error \
            "task_execution_failure" \
            "auto_trigger.sh" \
            "68" \
            "Task ${task_id} failed with exit code ${exec_exit}" || true
    fi
}

# ─── 메인: pending 지시서 순회 ──────────────────────────
if [ ! -d "$DIRECTIVES_DIR" ]; then
    echo "⚠️ 지시서 디렉토리 없음: ${DIRECTIVES_DIR}"
    exit 0
fi

mkdir -p "$DONE_DIR"

FOUND=0
for f in "${DIRECTIVES_DIR}"/*.md; do
    [ -f "$f" ] || continue
    FOUND=1
    _process_directive "$f"
done

if [ $FOUND -eq 0 ]; then
    echo ""
    echo "  (pending 지시서 없음)"
fi

echo ""
echo "======================================================"
echo "Auto Trigger 완료: $(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')"
echo "======================================================"
