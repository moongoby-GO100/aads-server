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
# AADS-178: archived 디렉토리 (유효하지 않은 파일 이동)
ARCHIVED_DIR="${ARCHIVED_DIR:-/root/.genspark/directives/archived}"
PRIORITY_LOG="/var/log/aads/auto_trigger_priority.log"
_TDLOG_PRIMARY="/var/log/aads/trigger_decisions.log"
if touch "${_TDLOG_PRIMARY}" 2>/dev/null; then
    TRIGGER_DECISION_LOG="${_TDLOG_PRIMARY}"
else
    TRIGGER_DECISION_LOG="/root/aads/logs/trigger_decisions.log"
    mkdir -p /root/aads/logs 2>/dev/null || true
fi

# AADS-141 A-1: signal 파일 경로
SIGNAL_FILE="/tmp/aads_trigger_next.signal"

# T-106: --dry-run 플래그 파싱
DRY_RUN=false
SIGNAL_TRIGGERED=false
for _arg in "$@"; do
    [ "$_arg" = "--dry-run" ] && DRY_RUN=true
done

# AADS-141 A-1: signal 파일 감지 → 즉시 실행 모드
if [ -f "${SIGNAL_FILE}" ]; then
    _SIGNAL_CONTENT=$(cat "${SIGNAL_FILE}" 2>/dev/null || echo "")
    rm -f "${SIGNAL_FILE}"
    SIGNAL_TRIGGERED=true
    mkdir -p "$(dirname "${TRIGGER_DECISION_LOG}")" 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SIGNAL_TRIGGER | content=${_SIGNAL_CONTENT} | mode=immediate | skip_cron_wait=true" \
        >> "${TRIGGER_DECISION_LOG}" 2>/dev/null || true
fi

TS_START=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

echo "======================================================"
echo "AADS Auto Trigger"
echo "감시 디렉토리: ${DIRECTIVES_DIR}"
echo "시작: ${TS_START}"
if [ "$SIGNAL_TRIGGERED" = "true" ]; then
    echo "모드: SIGNAL 기반 즉시 투입 (크론 주기 대기 없음)"
else
    echo "모드: 크론 주기 실행 (fallback)"
fi
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

# ─── AADS-146: Writer/Reviewer 리뷰 세션 스폰 함수 ──────────
# 사용: _spawn_review_session <task_id> <directive_file> <result_file>
# P0/P1 + review_required:true 시 자동 리뷰 세션 생성
# PASS → 자동 push 유지, NEEDS_REVISION → 피드백 파일 생성 후 재실행 트리거
_spawn_review_session() {
    local task_id="$1"
    local directive_file="$2"
    local result_file="$3"
    local review_log="/var/log/aads/review_sessions.log"
    local review_result_file="/tmp/aads_review_${task_id}_$$.txt"
    mkdir -p "$(dirname "$review_log")" 2>/dev/null || true

    echo "$(date '+%Y-%m-%d %H:%M:%S') | REVIEW_START | task=${task_id}" >> "$review_log" 2>/dev/null || true

    # security-reviewer 에이전트로 리뷰 수행
    local _agent_file="/root/aads/.claude/agents/security-reviewer.md"
    local _review_prompt=""
    if [ -f "$_agent_file" ]; then
        _review_prompt="$(cat "$_agent_file")

## 리뷰 대상 태스크: ${task_id}
지시서:
$(cat "$directive_file" 2>/dev/null || echo '[지시서 없음]')

결과 파일:
$(cat "$result_file" 2>/dev/null | head -200 || echo '[결과 파일 없음]')

위 내용을 검토하고 SECURITY_REVIEW: PASS 또는 NEEDS_REVISION 결과를 출력하라."
    else
        _review_prompt="[REVIEW] task=${task_id} 완료 결과를 검토하라.
결과: $(cat "$result_file" 2>/dev/null | head -100 || echo '[없음]')
검토 후 반드시 SECURITY_REVIEW: PASS 또는 SECURITY_REVIEW: NEEDS_REVISION 을 출력하라."
    fi

    local _review_exit=0
    echo "$_review_prompt" | timeout 1800 claude --print 2>&1 > "$review_result_file" || _review_exit=$?

    # 결과 파싱
    local _verdict
    _verdict=$(grep -m1 "SECURITY_REVIEW:" "$review_result_file" 2>/dev/null | awk '{print $2}' || echo "UNKNOWN")

    echo "$(date '+%Y-%m-%d %H:%M:%S') | REVIEW_RESULT | task=${task_id} | verdict=${_verdict}" >> "$review_log" 2>/dev/null || true

    if [ "$_verdict" = "PASS" ]; then
        echo "  ✅ [REVIEW] PASS — git push 유지"
        echo "$(date '+%Y-%m-%d %H:%M:%S') | REVIEW_PASS | task=${task_id}" >> "$review_log" 2>/dev/null || true
    elif [ "$_verdict" = "NEEDS_REVISION" ]; then
        echo "  ⚠️ [REVIEW] NEEDS_REVISION — 피드백 파일 생성"
        local _feedback_file="${PENDING_DIR}/REVIEW_FEEDBACK_${task_id}_$(date +%Y%m%d%H%M%S).md"
        cat > "$_feedback_file" <<FEEDEOF
task_id: ${task_id}-REVISION
project: AADS
priority: P1-HIGH
review_feedback: true
original_task: ${task_id}
description: 리뷰 결과 수정 필요 (NEEDS_REVISION)

## 리뷰 피드백
$(cat "$review_result_file" 2>/dev/null || echo '[리뷰 결과 없음]')

위 피드백을 반영하여 ${task_id} 작업을 수정하라.
FEEDEOF
        echo "$(date '+%Y-%m-%d %H:%M:%S') | REVIEW_NEEDS_REVISION | task=${task_id} | feedback=${_feedback_file}" >> "$review_log" 2>/dev/null || true
    else
        echo "  ⚠️ [REVIEW] 결과 파싱 실패 (verdict=${_verdict}) — PASS로 처리"
    fi

    rm -f "$review_result_file" 2>/dev/null || true
}
# ─── 리뷰 세션 함수 끝 ───────────────────────────────────────

# ─── AADS-146: Git Worktree 병렬 실행 함수 ──────────────────
# 사용: _parallel_worktree <parallel_group_id> <directive_file1> [directive_file2 ...]
# parallel_group 필드가 있는 지시서를 감지하여 각각 별도 worktree에서 병렬 실행
_parallel_worktree() {
    local group_id="$1"
    shift
    local files=("$@")
    local wt_base="/root/aads/.worktrees"
    local merge_log="/var/log/aads/worktree_merge.log"
    mkdir -p "$wt_base" "$(dirname "$merge_log")" 2>/dev/null || true

    echo "[WORKTREE] parallel_group=${group_id} 병렬 실행 시작 (${#files[@]}개 지시서)"
    echo "$(date '+%Y-%m-%d %H:%M:%S') | WORKTREE_START | group=${group_id} | count=${#files[@]}" >> "$merge_log" 2>/dev/null || true

    local pids=()
    local wt_paths=()
    local wt_results=()

    for directive_file in "${files[@]}"; do
        local task_id
        task_id=$(grep -oP '(AADS|KIS|GO100|SF|NT|SALES|NAS|T)-\d+' "$directive_file" 2>/dev/null | head -1 \
            || basename "$directive_file" .md)
        local wt_path="${wt_base}/${group_id}_${task_id}"

        # worktree 생성 (각 task별 독립 브랜치)
        local wt_branch="worktree/${group_id}/${task_id}_$$"
        local wt_exit=0

        # aads-server repo에 worktree 생성
        local _repo="/root/aads/aads-server"
        if [ -d "${_repo}/.git" ]; then
            git -C "$_repo" worktree add -b "$wt_branch" "$wt_path" HEAD 2>/dev/null \
                || { echo "[WORKTREE] WARNING: worktree 생성 실패 — fallback to main"; wt_path="$_repo"; }
        fi

        echo "[WORKTREE] task=${task_id} worktree=${wt_path}"

        # 백그라운드에서 claude_exec.sh 실행 (worktree 경로 주입)
        (
            export WORKTREE_PATH="$wt_path"
            export WORKTREE_BRANCH="$wt_branch"
            export WORKTREE_GROUP="$group_id"
            "${SCRIPT_DIR}/claude_exec.sh" "$task_id" "$directive_file"
            echo $? > "${wt_path}.exit"
        ) &
        pids+=($!)
        wt_paths+=("$wt_path")
        wt_results+=("${wt_path}.exit")
    done

    # 모든 병렬 작업 완료 대기
    echo "[WORKTREE] 모든 병렬 작업 대기 중..."
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done

    echo "[WORKTREE] 모든 병렬 작업 완료 — 머지 시작"

    # 자동 머지 스크립트 실행
    "${SCRIPT_DIR}/merge_worktree.sh" "$group_id" "${wt_paths[@]}" \
        >> "$merge_log" 2>&1 || true

    echo "$(date '+%Y-%m-%d %H:%M:%S') | WORKTREE_DONE | group=${group_id}" >> "$merge_log" 2>/dev/null || true
}

# ─── AADS-146: parallel_group 지시서 감지 함수 ───────────────
# pending 디렉토리에서 동일 parallel_group을 가진 지시서들을 묶어 반환
_get_parallel_groups() {
    local pending_dir="$1"
    # parallel_group 필드가 있는 파일들 추출
    grep -rl "^parallel_group:" "${pending_dir}"/*.md 2>/dev/null \
        | xargs -r grep -h "^parallel_group:" 2>/dev/null \
        | awk '{print $2}' | sort -u || true
}

_get_files_by_group() {
    local pending_dir="$1" group="$2"
    grep -rl "^parallel_group:.*${group}" "${pending_dir}"/*.md 2>/dev/null || true
}
# ─── Worktree 함수 끝 ────────────────────────────────────────

# ─── AADS-147: STATUS.md 자동 업데이트 함수 ─────────────────
# 사용: _update_status_md <task_id> <result> <commit_sha> <report_url> <next_pending>
# result: SUCCESS | FAILED | PARTIAL
_update_status_md() {
    local task_id="$1"
    local result="${2:-SUCCESS}"
    local commit_sha="${3:-}"
    local report_url="${4:-}"
    local next_pending="${5:-none}"
    local status_file="/root/aads/aads-docs/STATUS.md"
    local completed_at
    completed_at=$(TZ='Asia/Seoul' date '+%Y-%m-%dT%H:%M:%S+09:00')

    cat > "$status_file" <<EOF
last_completed: ${task_id}
completed_at: "${completed_at}"
result: ${result}
commit_sha: ${commit_sha}
report_url: ${report_url}
chat_delivered: false
next_pending: ${next_pending}
EOF

    # git add + commit + push
    local docs_dir="/root/aads/aads-docs"
    if [ -d "${docs_dir}/.git" ]; then
        local git_out
        git_out=$(git -C "$docs_dir" add STATUS.md 2>&1 && \
            git -C "$docs_dir" commit -m "chore(status): ${task_id} 완료 — ${result} $(date '+%Y-%m-%d %H:%M KST')" 2>&1 && \
            git -C "$docs_dir" push origin main 2>&1) || true
        echo "[STATUS-MD] 업데이트 완료: task=${task_id} result=${result} sha=${commit_sha:0:8}"
        echo "[STATUS-MD] git: $(echo "$git_out" | tail -2)"
    else
        echo "[STATUS-MD] WARNING: aads-docs git 디렉토리 없음"
    fi
}
# ─── STATUS.md 업데이트 함수 끝 ──────────────────────────────

# ─── AADS-143: git-push 검증 함수 ───────────────────────────
# 사용: verify_git_push <PROJECT> <RESULT_FILE> [REPO_OWNER] [REPO_NAME] [BRANCH]
verify_git_push() {
    local proj="$1"
    local result_file="$2"
    local repo_owner="${3:-moongoby-GO100}"
    local repo_name="${4:-aads-docs}"
    local branch="${5:-master}"
    local LOG_DIR="/root/.genspark/logs"
    local TELEGRAM_SCRIPT="/root/.genspark/send_telegram.sh"

    # RESULT_FILE이 생성될 때까지 최대 7200초 대기 (폴링 10초)
    local waited=0
    while [ ! -f "$result_file" ] && [ "$waited" -lt 7200 ]; do
        sleep 10
        waited=$((waited + 10))
    done

    if [ ! -f "$result_file" ]; then
        echo "[PUSH-VERIFY] RESULT 파일 없음 (7200초 초과): $result_file" >> "${LOG_DIR}/push_verify.log"
        return 1
    fi

    # commit_sha 추출
    local sha
    sha=$(grep -m1 '^commit_sha:' "$result_file" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')

    if [ -z "$sha" ] || [ "$sha" = "null" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [PUSH-VERIFY] commit_sha 없음 — push 검증 스킵: $result_file" >> "${LOG_DIR}/push_verify.log"
        return 0
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [PUSH-VERIFY] $proj commit_sha=${sha:0:8} push 확인 시작" >> "${LOG_DIR}/push_verify.log"

    # GitHub raw URL 생성 (HANDOVER.md 기준)
    local raw_url="https://raw.githubusercontent.com/${repo_owner}/${repo_name}/${sha}/HANDOVER.md"
    local retries=3
    local backoff=10
    local http_code

    for i in $(seq 1 $retries); do
        http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 "$raw_url" 2>/dev/null)
        if [ "$http_code" = "200" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [PUSH-VERIFY] OK $proj SHA=${sha:0:8} HTTP 200" >> "${LOG_DIR}/push_verify.log"
            return 0
        fi
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [PUSH-VERIFY] $proj HTTP ${http_code} (시도 ${i}/${retries}) ${backoff}초 대기" >> "${LOG_DIR}/push_verify.log"
        sleep $backoff
        backoff=$((backoff * 2))
    done

    # 3회 실패 처리
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [PUSH-VERIFY] FAILED $proj SHA=${sha:0:8} url=${raw_url}" >> "${LOG_DIR}/push_failed.log"

    # Telegram 알림
    bash "$TELEGRAM_SCRIPT" "🔴 [${proj}] git-push 검증 실패
SHA: ${sha:0:8}
→ 수동 push 확인 필요" 2>/dev/null

    # recovery_logs DB 기록
    local aads_url
    aads_url=$(grep '^AADS_API_URL=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    [ -n "$aads_url" ] && curl -s -X POST "${aads_url}/ops/recovery-logs" \
        -H "Content-Type: application/json" \
        -d "{\"project\":\"${proj}\",\"issue_type\":\"push_failed\",\"detail\":\"commit_sha=${sha}\",\"status\":\"failed\",\"created_at\":\"$(date '+%Y-%m-%d %H:%M KST')\"}" \
        --max-time 10 > /dev/null 2>&1

    # 매니저 에스컬레이션 트리거
    bash "$TELEGRAM_SCRIPT" "🚨 [ESCALATION] ${proj} git-push_failed 매니저 확인 요청" 2>/dev/null

    return 1
}
# ─── push 검증 함수 끝 ───────────────────────────────────────

# ─── AADS-113: 지시서 라이프사이클 DB 기록 함수 ─────────────
record_lifecycle() {
    local task_id="$1" status="$2" timestamp="$3"
    local project="${PROJECT:-AADS}"
    [ -z "$timestamp" ] && timestamp=$(TZ='Asia/Seoul' date '+%Y-%m-%dT%H:%M:%S+09:00')
    curl -s -X POST "http://localhost:8080/api/v1/ops/directive-lifecycle" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\":\"${task_id}\",\"project\":\"${project}\",\"status\":\"${status}\",\"timestamp\":\"${timestamp}\"}" \
        > /dev/null 2>&1 || true
}

# ─── D-025: impact/effort 점수 계산 함수 ────────────────────
# 점수 = impact_score(H=3,M=2,L=1) × 10 + effort_score(L=3,M=2,H=1)
# 높은 점수 = 먼저 실행
_impact_effort_score() {
    local file="$1"
    local impact effort impact_score effort_score
    impact=$(grep -m1 '^impact:' "$file" 2>/dev/null | awk '{print toupper($2)}' | tr -d ' ')
    effort=$(grep -m1 '^effort:' "$file" 2>/dev/null | awk '{print toupper($2)}' | tr -d ' ')
    case "${impact:-M}" in H) impact_score=3 ;; L) impact_score=1 ;; *) impact_score=2 ;; esac
    case "${effort:-M}" in L) effort_score=3 ;; H) effort_score=1 ;; *) effort_score=2 ;; esac
    echo $(( impact_score * 10 + effort_score ))
}

# ─── D-025: 후보 목록에서 impact/effort 점수 최고 파일 선택 ──
_best_by_score() {
    local best_file="" best_score=-1
    for f in "$@"; do
        [ -f "$f" ] || continue
        local score
        score=$(_impact_effort_score "$f")
        if [ "$score" -gt "$best_score" ]; then
            best_score=$score
            best_file="$f"
        fi
    done
    echo "$best_file"
}

# ─── AADS-145: 투기적 실행 — final_commit 기반 다음작업 프리로드 ─
_speculative_preload() {
    local _pend_dir="$1" _fail_flag="$2"
    # 다음 후보 선택
    local _next_file
    _next_file=$(_select_next_file "$_pend_dir" 2>/dev/null) || return 0
    [ -z "$_next_file" ] || [ ! -f "$_next_file" ] && return 0
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [SPEC-PRELOAD] 다음 작업 git pull 시작: $(basename "$_next_file")"
    # 주요 repo git pull 선제 실행 (컨텍스트 준비)
    for _repo_dir in /root/aads/aads-docs /root/aads/aads-server /root/aads/aads-dashboard; do
        if [ -d "${_repo_dir}/.git" ]; then
            git -C "$_repo_dir" pull --quiet 2>/dev/null &
        fi
    done
    wait 2>/dev/null
    # 후처리 실패 확인
    if [ -f "$_fail_flag" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [SPEC-PRELOAD] 후처리 실패 감지 — 프리로드 취소"
        rm -f "$_fail_flag"
        return 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] [SPEC-PRELOAD] 프리로드 완료: $(basename "$_next_file")"
    return 0
}
# ─── 투기적 실행 함수 끝 ────────────────────────────────────────

# ─── AADS-178: 브릿지 파일 유효성 필터링 ──────────────────────
# >>>DIRECTIVE_START 블록이 없는 pending 파일을 archived로 이동
# 동일 task_id 중복 파일은 최신 1개만 유지, 나머지 archived
_filter_invalid_pending() {
    local pending_dir="$1"
    local archived_dir
    archived_dir="${pending_dir%pending}archived"
    mkdir -p "$archived_dir" 2>/dev/null || true

    # 1) >>>DIRECTIVE_START 블록 없는 파일 archived 이동
    for _pf in "${pending_dir}"/*.md; do
        [ -f "$_pf" ] || continue
        if ! grep -q ">>>DIRECTIVE_START" "$_pf" 2>/dev/null; then
            echo "[PREFLIGHT-FILTER] DIRECTIVE_START 없음 → archived: $(basename "$_pf")"
            mv "$_pf" "$archived_dir/" 2>/dev/null || true
        fi
    done

    # 2) 동일 task_id 중복 파일 처리 — 최신 1개 유지, 나머지 archived
    declare -A _seen_task_ids
    # 최신 순으로 정렬 (ls -t: 수정시간 최신 우선)
    while IFS= read -r _pf; do
        [ -f "$_pf" ] || continue
        local _tid
        _tid=$(grep -oP '(AADS|KIS|GO100|SF|NT|SALES|NAS|T)-\d+' "$_pf" 2>/dev/null | head -1 || true)
        [ -z "$_tid" ] && continue
        if [ -n "${_seen_task_ids[$_tid]:-}" ]; then
            echo "[PREFLIGHT-FILTER] 중복 task_id=${_tid} → archived: $(basename "$_pf")"
            mv "$_pf" "$archived_dir/" 2>/dev/null || true
        else
            _seen_task_ids["$_tid"]="$_pf"
        fi
    done < <(ls -t "${pending_dir}"/*.md 2>/dev/null || true)
}

# ─── AADS-178: DEPENDS_ON 교차 확인 (done폴더 + API) ──────────
# 사용: _check_depends_on <task_id> <directive_file>
# 반환: 0=충족, 1=미충족(pending 유지)
# 미충족 시 30초 후 재확인 (최대 3회, exponential backoff: 30/60/120s)
_check_depends_on() {
    local task_id="$1"
    local directive_file="$2"
    local depends_on_id
    depends_on_id=$(grep -m1 '^DEPENDS_ON:' "$directive_file" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || true)
    [ -z "$depends_on_id" ] && return 0  # DEPENDS_ON 없으면 통과

    local done_dir="${DONE_DIR:-/root/.genspark/directives/done}"
    local pending_dir="${PENDING_DIR:-/root/.genspark/directives/pending}"
    local aads_url
    aads_url=$(grep '^AADS_API_URL=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
    [ -z "$aads_url" ] && aads_url="http://localhost:8080/api/v1"

    local max_retries=3
    local backoff=30

    for i in $(seq 1 $max_retries); do
        # 1차: done 폴더 파일명 매칭
        local folder_met=false
        if ls "${done_dir}"/*"${depends_on_id}"*RESULT*.md 2>/dev/null | head -1 | grep -q .; then
            folder_met=true
        fi

        # 2차: AADS API 교차 확인
        local api_met=false
        if [ "$folder_met" = "true" ]; then
            api_met=true  # 폴더에서 확인됐으면 API는 추가 검증 생략 가능
        else
            local preflight_resp
            preflight_resp=$(curl -s --max-time 10 \
                "${aads_url}/directives/preflight?task_id=${task_id}&depends_on=${depends_on_id}" \
                2>/dev/null || echo "")
            if echo "$preflight_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('depends_met') else 1)" 2>/dev/null; then
                api_met=true
            fi
        fi

        if [ "$folder_met" = "true" ] || [ "$api_met" = "true" ]; then
            echo "  ✅ [DEPENDS_ON] ${depends_on_id} 충족 (시도 ${i}/${max_retries})"
            return 0
        fi

        echo "  ⏳ [DEPENDS_ON] ${depends_on_id} 미충족 (시도 ${i}/${max_retries}) — ${backoff}초 후 재확인"

        if [ "$i" -lt "$max_retries" ]; then
            sleep "$backoff"
            backoff=$(( backoff * 2 ))
        fi
    done

    # 3회 실패 → pending 유지 + Telegram 알림
    echo "  ❌ [DEPENDS_ON] ${depends_on_id} 미충족 — pending 유지 + Telegram 알림"
    # 파일을 다시 pending으로 이동 (이미 running 이동 전이므로 위치 확인)
    if [ -f "${pending_dir}/$(basename "$directive_file")" ]; then
        echo "  [DEPENDS_ON] 파일이 이미 pending에 있음"
    else
        mv "$directive_file" "${pending_dir}/" 2>/dev/null || true
    fi

    bash "/root/.genspark/send_telegram.sh" \
        "⚠️ [DEPENDS_ON 미충족] ${task_id}
선행 태스크: ${depends_on_id}
→ pending 유지 (3회 확인 실패)" 2>/dev/null || true
    return 1
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

    # 1순위: 파일명에 _P0_ 포함 → D-025 impact/effort 정렬
    local p0_name_files
    p0_name_files=$(ls "${pending_dir}"/*_P0_*.md 2>/dev/null || true)
    if [ -n "$p0_name_files" ]; then
        # shellcheck disable=SC2086
        next_file=$(_best_by_score $p0_name_files)
        [ -n "$next_file" ] && reason="filename P0 priority (impact/effort sorted)"
    fi

    # 2순위: 내용에 P0-CRITICAL 포함 → D-025 impact/effort 정렬
    if [ -z "$next_file" ]; then
        local p0_content_files
        p0_content_files=$(grep -rl "P0-CRITICAL" "${pending_dir}"/*.md 2>/dev/null || true)
        if [ -n "$p0_content_files" ]; then
            # shellcheck disable=SC2086
            next_file=$(_best_by_score $p0_content_files)
            [ -n "$next_file" ] && reason="content P0-CRITICAL priority (impact/effort sorted)"
        fi
    fi

    # 3순위: 파일명에 _P1_ 포함 → D-025 impact/effort 정렬
    if [ -z "$next_file" ]; then
        local p1_name_files
        p1_name_files=$(ls "${pending_dir}"/*_P1_*.md 2>/dev/null || true)
        if [ -n "$p1_name_files" ]; then
            # shellcheck disable=SC2086
            next_file=$(_best_by_score $p1_name_files)
            [ -n "$next_file" ] && reason="filename P1 priority (impact/effort sorted)"
        fi
    fi

    # 4순위: 내용에 P1-HIGH 포함 → D-025 impact/effort 정렬
    if [ -z "$next_file" ]; then
        local p1_content_files
        p1_content_files=$(grep -rl "P1-HIGH" "${pending_dir}"/*.md 2>/dev/null || true)
        if [ -n "$p1_content_files" ]; then
            # shellcheck disable=SC2086
            next_file=$(_best_by_score $p1_content_files)
            [ -n "$next_file" ] && reason="content P1-HIGH priority (impact/effort sorted)"
        fi
    fi

    # 5순위: P2 전체 → D-025 impact/effort 정렬 후 FIFO fallback
    if [ -z "$next_file" ]; then
        local p2_files
        p2_files=$(ls "${pending_dir}"/*.md 2>/dev/null || true)
        if [ -n "$p2_files" ]; then
            # shellcheck disable=SC2086
            next_file=$(_best_by_score $p2_files)
            if [ -n "$next_file" ]; then
                reason="P2-NORMAL (impact/effort sorted)"
            else
                next_file=$(ls -t "${pending_dir}"/*.md 2>/dev/null | tail -1 || true)
                [ -n "$next_file" ] && reason="FIFO (P2-NORMAL fallback)"
            fi
        fi
    fi

    if [ -n "$next_file" ]; then
        _log_priority "$p0_content_count" "$p1_content_count" "$p2_count" \
            "$(basename "$next_file") | REASON: $reason"
        echo "$next_file"
    fi
}

# ─── AADS-178: pending 파일 유효성 필터링 (DIRECTIVE_START 블록 + 중복 task_id) ──
if [ -d "$PENDING_DIR" ] && ls "${PENDING_DIR}"/*.md 2>/dev/null | head -1 > /dev/null 2>&1; then
    _filter_invalid_pending "$PENDING_DIR"
fi

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
        # AADS-113: queued 상태 기록 (pending → running 이동 시)
        _QUEUED_TASK_ID=$(grep -oP '(AADS|KIS|GO100|SF|NT|SALES|NAS|T)-\d+' "$RUNNING_DIR/$(basename "$NEXT_FILE")" 2>/dev/null | head -1 || true)
        [ -n "$_QUEUED_TASK_ID" ] && record_lifecycle "$_QUEUED_TASK_ID" "queued"
        # AADS-141 A-2: 투입 결정 로그
        mkdir -p "$(dirname "${TRIGGER_DECISION_LOG}")" 2>/dev/null || true
        echo "$(date '+%Y-%m-%d %H:%M:%S') | DISPATCH | task=${_QUEUED_TASK_ID:-unknown} | file=$(basename "$NEXT_FILE") | signal=${SIGNAL_TRIGGERED} | mode=$([ "$SIGNAL_TRIGGERED" = "true" ] && echo IMMEDIATE || echo CRON)" \
            >> "${TRIGGER_DECISION_LOG}" 2>/dev/null || true
        DIRECTIVES_DIR="$RUNNING_DIR"
    fi
fi

# ─── 단일 지시서 처리 함수 ───────────────────────────────────
_process_directive() {
    local directive_file="$1"
    local filename
    filename=$(basename "$directive_file")

    # T-107: Task ID 추출 — 접두사 패턴 인식 (AADS-xxx, KIS-xxx, T-xxx 등)
    local task_id
    task_id=$(grep -oP '(AADS|KIS|GO100|SF|NT|SALES|NAS|T)-\d+' "$directive_file" 2>/dev/null | head -1) || true
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

    # ─── AADS-145: Tasks 시스템으로 완료 여부 확인 (PENDING/DONE 이중관리 제거) ───
    local _tasks_json="/home/claudebot/.claude/tasks/${task_id}.json"
    if [ -f "$_tasks_json" ]; then
        local _ts
        _ts=$(python3 -c "import json; d=json.load(open('${_tasks_json}')); print(d.get('status',''))" 2>/dev/null || echo "")
        if [ "$_ts" = "done" ]; then
            echo "  ✅ [TASKS] 이미 완료 (${task_id}) — 스킵"
            return 0
        fi
    fi

    # ─── 이미 COMPLETED면 스킵 ───
    if echo "${current_status}" | grep -qi "^COMPLETED"; then
        echo "  ✅ 이미 COMPLETED — 스킵"
        return 0
    fi

    # ─── AADS-178: DEPENDS_ON 교차 확인 (done폴더 + API) ───
    if ! _check_depends_on "$task_id" "$directive_file"; then
        echo "  ⏭️ [DEPENDS_ON] 미충족 — 이 지시서 스킵"
        return 0
    fi

    # ─── AADS-113: running 상태 기록 ───
    record_lifecycle "$task_id" "running"

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

    # === AADS-163: RESULT_FILE에서 qa_status / design_status 추출 ===
    local _qa_status _design_status
    _qa_status=$(grep -m1 '^qa_status:' "${result_file}" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || echo "")
    _design_status=$(grep -m1 '^design_status:' "${result_file}" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]' || echo "")

    # qa_status=FAIL → 서킷브레이커 차단 신호
    if [ "${_qa_status}" = "FAIL" ]; then
        echo "  ❌ [QA-GATE] qa_status=FAIL — 서킷브레이커 카운트 증가 (project=${project})"
        # circuit_breaker_state API에 실패 기록
        local _aads_url
        _aads_url=$(grep '^AADS_API_URL=' /root/.env.aads 2>/dev/null | cut -d= -f2-)
        [ -n "$_aads_url" ] && curl -s -X POST "${_aads_url}/ops/circuit-breaker/increment" \
            -H "Content-Type: application/json" \
            -d "{\"project\":\"${project}\",\"task_id\":\"${task_id}\",\"reason\":\"qa_gate_fail\"}" \
            --max-time 10 > /dev/null 2>&1 || true
        bash "/root/.genspark/send_telegram.sh" "🚨 [QA-FAIL] ${task_id} QA 2회 초과 실패 — 서킷브레이커 카운트+1 (project=${project})" 2>/dev/null || true
        exec_exit=1
    fi
    # === QA/디자인 상태 추출 끝 ===

    if [ $exec_exit -eq 0 ]; then
        echo "  ✅ 실행 완료: ${task_id} (${ts_done})"
        # AADS-113: completed 상태 기록
        record_lifecycle "$task_id" "completed"

        # AADS-146: Writer/Reviewer 패턴 — review_required:true 감지
        local _review_required _priority
        _review_required=$(grep -m1 '^review_required:' "$directive_file" 2>/dev/null | awk '{print tolower($2)}' | tr -d ' ' || echo "false")
        _priority=$(grep -m1 '^priority:' "$directive_file" 2>/dev/null | awk '{print $2}' | tr -d ' ' || echo "")
        if [ "$_review_required" = "true" ] && echo "$_priority" | grep -qiE "P0|P1"; then
            echo "  🔍 [REVIEW] review_required=true + ${_priority} 감지 → 리뷰 세션 스폰"
            _spawn_review_session "$task_id" "$directive_file" "$result_file" &
            echo "  🔍 [REVIEW] 리뷰 세션 백그라운드 시작 (PID: $!)"
        fi

        # AADS-147: STATUS.md 자동 업데이트
        local _status_sha _status_report _status_next
        _status_sha=$(grep -m1 '^commit_sha:' "${result_file}" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
        [ -z "$_status_sha" ] && _status_sha=$(git -C /root/aads/aads-docs log --format="%H" -1 2>/dev/null || echo "")
        _status_report="https://github.com/moongoby-GO100/aads-docs/blob/main/reports/$(basename "${result_file}" 2>/dev/null || echo '')"
        _status_next=$(_select_next_file "$PENDING_DIR" 2>/dev/null | xargs -r basename | sed 's/\.md$//' || echo "none")
        [ -z "$_status_next" ] && _status_next="none"
        _update_status_md "$task_id" "SUCCESS" "$_status_sha" "$_status_report" "$_status_next"

        # AADS-163: Telegram 알림에 QA/디자인 판정 포함
        local _qa_tg="${_qa_status:-N/A}" _dg_tg="${_design_status:-N/A}"
        bash "/root/.genspark/send_telegram.sh" "✅ [${project}] ${task_id} 완료
QA: ${_qa_tg} | 디자인: ${_dg_tg}
커밋: ${_status_sha:0:8}" 2>/dev/null || true

        # AADS-145: final_commit 신호 감지 → 투기적 프리로드 (후처리와 병렬)
        local _fc_signal="/tmp/aads_final_commit_${task_id}.signal"
        local _preload_fail="/tmp/aads_preload_fail_${task_id}_$$"
        if [ -f "$_fc_signal" ]; then
            rm -f "$_fc_signal"
            echo "  🚀 [SPEC] final_commit 감지 — 다음 작업 프리로드 병렬 시작"
            _speculative_preload "$PENDING_DIR" "$_preload_fail" &
        fi

        # AADS-143: git-push 검증 (백그라운드 비동기 실행)
        if [ -n "$result_file" ]; then
            local _proj_upper
            _proj_upper=$(echo "${PROJECT:-AADS}" | tr '[:lower:]' '[:upper:]')
            # 프로젝트별 repo 매핑
            local _repo_owner _repo_name
            case "$_proj_upper" in
                AADS)   _repo_owner="moongoby-GO100"; _repo_name="aads-docs" ;;
                GO100)  _repo_owner="moongoby-GO100"; _repo_name="go100-docs" ;;
                KIS)    _repo_owner="moongoby-GO100"; _repo_name="kis-docs" ;;
                SF)     _repo_owner="moongoby-GO100"; _repo_name="sf-docs" ;;
                NTV2)   _repo_owner="moongoby-GO100"; _repo_name="ntv2-docs" ;;
                NAS)    _repo_owner="moongoby-GO100"; _repo_name="nas-docs" ;;
                *)      _repo_owner="moongoby-GO100"; _repo_name="aads-docs" ;;
            esac
            ( verify_git_push "$_proj_upper" "$result_file" "$_repo_owner" "$_repo_name" "master" ) &
            echo "  🔍 git-push 검증 백그라운드 시작 (PID: $!)"
        fi

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
        # AADS-113: failed 상태 기록
        record_lifecycle "$task_id" "failed"
        # AADS-147: STATUS.md 실패 업데이트
        _update_status_md "$task_id" "FAILED" "" "" "none"
        # AADS-145: 투기적 프리로드 취소 (실행 실패시)
        [ -n "${_preload_fail:-}" ] && touch "$_preload_fail" 2>/dev/null || true
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

# AADS-146: parallel_group 지시서 선제 처리
_PROCESSED_BY_GROUP=()
if [ -d "$PENDING_DIR" ]; then
    _groups=$(_get_parallel_groups "$PENDING_DIR" 2>/dev/null || true)
    for _grp in $_groups; do
        mapfile -t _grp_files < <(_get_files_by_group "$PENDING_DIR" "$_grp" 2>/dev/null || true)
        if [ "${#_grp_files[@]}" -gt 1 ]; then
            echo "[PARALLEL] group=${_grp} 지시서 ${#_grp_files[@]}개 감지 → Worktree 병렬 실행"
            # running으로 이동
            for _gf in "${_grp_files[@]}"; do
                [ -f "$_gf" ] && mv "$_gf" "$RUNNING_DIR/" 2>/dev/null || true
                _PROCESSED_BY_GROUP+=("$(basename "$_gf")")
            done
            _grp_running=()
            for _gf in "${_grp_files[@]}"; do
                _grp_running+=("${RUNNING_DIR}/$(basename "$_gf")")
            done
            _parallel_worktree "$_grp" "${_grp_running[@]}"
        fi
    done
fi

FOUND=0
for f in "${DIRECTIVES_DIR}"/*.md; do
    [ -f "$f" ] || continue
    # 이미 parallel_group 처리된 파일은 스킵
    _basename_f=$(basename "$f")
    _skip=false
    for _pg in "${_PROCESSED_BY_GROUP[@]}"; do
        [ "$_pg" = "$_basename_f" ] && _skip=true && break
    done
    [ "$_skip" = "true" ] && continue
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
