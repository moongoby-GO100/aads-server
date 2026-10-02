#!/usr/bin/env bash
# 러너 원격 동기화 런처 (AADS-RUNNER-SYNC-SOURCE-ORIGIN-MAIN, 2026-10-03).
#
# 설치 위치: /usr/local/sbin/aads-runner-sync-launcher  (공유 체크아웃 밖의 고정 경로)
#
# 왜 필요한가. 타이머가 돌리던 sync 스크립트는 공유 체크아웃(/root/aads/aads-server) 안의
# 파일이었고, 그 체크아웃은 다른 세션이 쓰는 중이라 HEAD 가 origin/main 보다 62커밋 뒤였다.
# "미커밋이면 defer" 게이트가 257회 넘게 defer 했고, 체크아웃이 정리되면 이번에는 옛 HEAD 의
# pipeline-runner.sh 로 원격 러너를 되돌릴 위험이 있었다. sync 스크립트 자체도 그 체크아웃 안에
# 있으므로 sync 스크립트만 고쳐서는 효과가 없다(부트스트랩) — 이 런처가 체크아웃 밖에서
# origin/main 의 깨끗한 export 를 만들고 그 안의 sync 스크립트를 실행한다.
#
# 공유 체크아웃에는 git fetch 로 ref(refs/remotes) 만 갱신하고, 작업본·HEAD·index 는 읽지도
# 바꾸지도 않는다(archive 는 객체 DB 만 읽는다). stash/reset/checkout/pull 없음.
#
# 설치(CEO 승인 후, 이 스크립트는 하지 않는다):
#   cp /etc/systemd/system/aads-pipeline-runner-sync.service /root/aads-pipeline-runner-sync.service.bak-pre-launcher
#   install -m 0755 <origin/main 의 scripts/runner_sync_launcher.sh> /usr/local/sbin/aads-runner-sync-launcher
#   install -m 0644 <origin/main 의 scripts/systemd/aads-pipeline-runner-sync.service> /etc/systemd/system/
#   systemctl daemon-reload && systemctl start aads-pipeline-runner-sync.service
# 롤백: 백업을 /etc/systemd/system/ 으로 되돌리고 systemctl daemon-reload.
#
# 종료코드: fetch/export 실패 = 1 (아무것도 설치하지 않음). 그 외는 sync 스크립트의 종료코드.
set -euo pipefail

SYNC_REPO="${AADS_RUNNER_SYNC_REPO:-/root/aads/aads-server}"
SYNC_REMOTE="${AADS_RUNNER_SYNC_REMOTE:-origin}"
SYNC_BRANCH="${AADS_RUNNER_SYNC_BRANCH:-main}"
EXPORT_BASE="${AADS_RUNNER_SYNC_EXPORT_BASE:-/var/tmp}"
FETCH_TIMEOUT="${AADS_RUNNER_SYNC_FETCH_TIMEOUT:-60}"

# 연속 fetch/export 실패 카운터. 일시적 네트워크 실패는 5분 뒤 재시도로 회복되지만,
# 6회(30분) 이상 이어지면 원격 러너가 최신 정본을 못 받는 상태가 고착된 것이다.
FAIL_STATE_FILE="${AADS_RUNNER_SYNC_FAIL_STATE:-/tmp/aads-pipeline-runner-sync.export-fail-count}"
FAIL_ESCALATE_AFTER="${AADS_RUNNER_SYNC_FAIL_ESCALATE_AFTER:-6}"

# export 대상. scripts/ 전체는 56MB 라 5분마다 풀 수 없다 — sync 가 설치하는 원본과
# sync 스크립트가 source 하는 파일, 유닛 파일(AADS_RUNNER_SYNC_UNITS=1)만 푼다.
REQUIRED_FILES=(
    scripts/pipeline-runner.sh
    scripts/claude_model_contract.py
    scripts/sync_pipeline_runner_remote.sh
    scripts/runner_busy_lib.sh
    scripts/runner_cli_usage.py
    tools/aag/brief.py
)
EXPORT_PATHSPECS=(
    "${REQUIRED_FILES[@]}"
    scripts/aads-pipeline-litellm-runner.114.service
    scripts/aads-pipeline-litellm-runner.211.service
    scripts/aads-pipeline-runner.244.service
    scripts/aads-pipeline-runner.service
)

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$*"
}

note_export_failure() {
    local reason="$1" n=0
    n=$(cat "$FAIL_STATE_FILE" 2>/dev/null || echo 0)
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    n=$((n + 1))
    printf '%s' "$n" > "$FAIL_STATE_FILE" 2>/dev/null || true
    log "ERROR origin/main export failed (${reason}); nothing installed"
    if [[ "$FAIL_ESCALATE_AFTER" =~ ^[0-9]+$ && "$n" -ge "$FAIL_ESCALATE_AFTER" ]]; then
        log "ERROR sync stalled: ${n} consecutive fetch/export failures — 원격 러너가 최신 정본을 받지 못하고 있다. ${SYNC_REPO} 의 ${SYNC_REMOTE} 접근(네트워크/키)을 확인하라"
    fi
    return 0
}

reset_export_failure() {
    rm -f "$FAIL_STATE_FILE" 2>/dev/null || true
}

EXPORT_DIR=""
cleanup() {
    [[ -n "$EXPORT_DIR" && -d "$EXPORT_DIR" ]] && rm -rf "$EXPORT_DIR"
    return 0
}

main() {
    local sha rc=0 required

    [[ -d "$SYNC_REPO" ]] || { note_export_failure "repo missing: ${SYNC_REPO}"; exit 1; }

    # 비정상 종료(SIGKILL 제외)에도 임시 export 를 남기지 않는다. SIGKILL 로 남은 것은 아래에서 회수한다.
    trap cleanup EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    find "$EXPORT_BASE" -maxdepth 1 -type d -name 'aads-runner-sync.*' -mmin +120 -exec rm -rf {} + 2>/dev/null || true

    export GIT_TERMINAL_PROMPT=0
    export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}"
    if ! timeout "$FETCH_TIMEOUT" git -C "$SYNC_REPO" -c gc.auto=0 fetch --quiet --no-tags \
            "$SYNC_REMOTE" "+refs/heads/${SYNC_BRANCH}:refs/remotes/${SYNC_REMOTE}/${SYNC_BRANCH}"; then
        note_export_failure "git fetch ${SYNC_REMOTE} ${SYNC_BRANCH}"
        exit 1
    fi
    sha=$(git -C "$SYNC_REPO" rev-parse --verify "refs/remotes/${SYNC_REMOTE}/${SYNC_BRANCH}^{commit}" 2>/dev/null) || sha=""
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { note_export_failure "cannot resolve ${SYNC_REMOTE}/${SYNC_BRANCH}"; exit 1; }

    EXPORT_DIR=$(mktemp -d "${EXPORT_BASE%/}/aads-runner-sync.XXXXXX") || { note_export_failure "mktemp"; exit 1; }
    if ! git -C "$SYNC_REPO" archive --format=tar "$sha" -- "${EXPORT_PATHSPECS[@]}" | tar -x -C "$EXPORT_DIR"; then
        note_export_failure "git archive ${sha}"
        exit 1
    fi
    for required in "${REQUIRED_FILES[@]}"; do
        [[ -s "${EXPORT_DIR}/${required}" ]] || { note_export_failure "missing in ${sha}: ${required}"; exit 1; }
    done
    reset_export_failure
    log "export ${SYNC_REMOTE}/${SYNC_BRANCH}=${sha} -> ${EXPORT_DIR}"

    # exec 하지 않는다 — 끝난 뒤 trap 이 임시 export 를 지워야 한다.
    set +e
    AADS_RUNNER_SYNC_EXPORTED=1 AADS_RUNNER_SYNC_SOURCE_SHA="$sha" \
        bash "${EXPORT_DIR}/scripts/sync_pipeline_runner_remote.sh" "$@"
    rc=$?
    set -e
    exit "$rc"
}

main "$@"
