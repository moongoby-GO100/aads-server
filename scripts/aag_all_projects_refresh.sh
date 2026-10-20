#!/usr/bin/env bash
# Refresh the six-project AAG inventory from the central AADS host.
# Runtime artifacts stay outside every repository so scans never dirty worktrees.
#
# 실행 중 자기 보호 (2026-09-22 11:13 CEST 사고): systemd 타이머가 이 스크립트를
# 읽고 있는 도중 배포 도구가 같은 경로에 새 버전을 write했다. bash 는 러닝
# 스크립트를 한 번에 버퍼링하지 않고 바이트 오프셋으로 순차 실행하므로, 파일이
# 밀리자 "line 137: syntax error near unexpected token '('" 로 죽었다(전체
# journalctl 근거는 오류사전 aag.script_edited_while_running). flock 은 이
# 스크립트끼리의 중복 실행만 막지, 외부 편집 도구는 그 lock 을 모른다 — 막을
# 방법은 실행 시작 즉시 자신을 불변 스냅샷으로 복제해 그 사본을 실행하는 것뿐이다.
# AAG_FROZEN 은 재실행 시 이 블록을 건너뛰기 위한 표식이다.
if [[ -z "${AAG_FROZEN:-}" ]]; then
    frozen="$(mktemp /tmp/aag-all-projects-refresh.frozen.XXXXXX.sh)"
    cp -- "$0" "$frozen"
    chmod +x "$frozen"
    AAG_FROZEN=1 exec bash "$frozen" "$@"
fi
# `exec` replaced the pre-freeze process image, so any trap set before it is
# gone — this one runs only inside the frozen copy (bash sets $0 to $frozen
# here) and deletes it on exit regardless of success/failure.
trap 'rm -f "$0"' EXIT

set -uo pipefail

REPO_DIR="${AAG_REPO_DIR:-/root/aads/aads-server}"
STATE_DIR="${AAG_STATE_DIR:-/var/lib/aads/aag}"
LOG="${AAG_ALL_PROJECTS_LOG:-/var/log/aads-pipeline/aag-all-projects.log}"
REMOTE_114="${AAG_REMOTE_114:-root@114.207.244.86}"
REMOTE_STAGE="${AAG_REMOTE_STAGE:-/tmp/aag-central-scanner}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15)
SCHED_PY="$REPO_DIR/scripts/aag_scheduler_state.py"
SCHED_OWNER="all-projects:$$:$(date +%s)"
# 하위 스크립트 종료코드 계약: 0=성공(스캔+적재) / 7=SKIP(실패도 성공도 아님) / 그 외=실패.
EXIT_SKIPPED=7
failures=()
skipped=()
refreshed=()

mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
sched() { AAG_STATE_DIR="$STATE_DIR" python3 "$SCHED_PY" "$@"; }
LOCK_FILE="${AAG_ALL_PROJECTS_LOCK:-/tmp/aag-all-projects-refresh.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '[%s] STATUS project=ALL result=skipped reason=already_running\n' "$(date '+%F %T %Z')" >>"$LOG"
    sched skip ALL --reason already_running >/dev/null 2>&1 || true
    exit 0
fi

log() { printf '[%s] %s\n' "$(date '+%F %T %Z')" "$*" | tee -a "$LOG"; }
failed() { failures+=("$1"); log "STATUS project=$1 result=failed detail=$2"; }
# skip 는 실패도 성공도 아니다 — refreshed 에 넣지 않으므로 freshness 증거로 세지 않는다.
skipped_project() { skipped+=("$1"); log "STATUS project=$1 result=skipped reason=$2${3:+ $3}"; }
beat() { sched heartbeat ALL --owner "$SCHED_OWNER" >/dev/null 2>&1 || true; }

refresh_aads() {
    if systemctl start aads-aag-struct-check.service \
        && python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
            "AADS=/var/log/aads-pipeline/aag-struct/aads-graph.json" >>"$LOG" 2>&1; then
        refreshed+=(AADS)
        log "STATUS project=AADS result=success"
    else
        failed AADS "scan_or_publish"
    fi
}

refresh_go100_and_kis() {
    bash "$REPO_DIR/scripts/aag_go100_refresh.sh" >>"$LOG" 2>&1
    local go100_rc=$?
    if ((go100_rc == EXIT_SKIPPED)); then
        # "already running — skip" 뒤에 result=success 와 KIS 재발행이 이어지던 결함(2026-10-20).
        local why
        why="$(sched status GO100 --field last_skip_reason 2>/dev/null)"
        skipped_project GO100 "${why:-go100_exit_7}"
        skipped_project KIS upstream_go100_skipped "scope=shared_monorepo"
        return
    elif ((go100_rc != 0)); then
        failed GO100 "remote_scan_or_publish rc=$go100_rc"
        return
    fi
    refreshed+=(GO100)
    log "STATUS project=GO100 result=success"
    if python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
        "KIS@GO100=$STATE_DIR/go100/go100-graph.json" >>"$LOG" 2>&1; then
        refreshed+=(KIS)
        log "STATUS project=KIS result=success scope=shared_monorepo"
    else
        failed KIS "shared_monorepo_publish"
    fi
}

refresh_generic_114() {
    local project="$1" root="$2" rules="$3" slug="$4"
    local remote_out="/var/lib/aads/aag/$slug"
    mkdir -p "$STATE_DIR/$slug"
    if ! ssh "${SSH_OPTS[@]}" "$REMOTE_114" \
        "mkdir -p '$REMOTE_STAGE' '$remote_out'" >>"$LOG" 2>&1; then
        failed "$project" "remote_stage"
        return
    fi
    if ! scp "${SSH_OPTS[@]}" -q \
        "$REPO_DIR/tools/aag/scan_aads.py" \
        "$REPO_DIR/tools/aag/v2_contract.py" \
        "$REPO_DIR/tools/aag/$rules" \
        "$REMOTE_114:$REMOTE_STAGE/" >>"$LOG" 2>&1; then
        failed "$project" "scanner_sync"
        return
    fi
    ssh "${SSH_OPTS[@]}" "$REMOTE_114" \
        "cd '$REMOTE_STAGE' && AAG_PROJECT='$project' timeout 900 python3 scan_aads.py --root '$root' --rules '$REMOTE_STAGE/$rules' --out-dir '$remote_out' --json" \
        >>"$LOG" 2>&1
    local scan_rc=$?
    # Scanner rc=1 means findings exist; rc>=2 means the scan itself failed.
    if ((scan_rc >= 2)); then
        failed "$project" "remote_scan"
        return
    fi
    if ! scp "${SSH_OPTS[@]}" -q \
        "$REMOTE_114:$remote_out/$slug-graph.json" "$STATE_DIR/$slug/" \
        >>"$LOG" 2>&1; then
        failed "$project" "artifact_fetch"
        return
    fi
    if python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
        "$project=$STATE_DIR/$slug/$slug-graph.json" >>"$LOG" 2>&1; then
        refreshed+=("$project")
        log "STATUS project=$project result=success"
    else
        failed "$project" "snapshot_publish"
    fi
}

refresh_nas() {
    local remote_out="/var/lib/aads/aag/nas"
    mkdir -p "$STATE_DIR/nas"
    if ! ssh "${SSH_OPTS[@]}" "$REMOTE_114" \
        "mkdir -p '$REMOTE_STAGE' '$remote_out'" >>"$LOG" 2>&1 \
        || ! scp "${SSH_OPTS[@]}" -q \
            "$REPO_DIR/tools/aag/scan_aads.py" \
            "$REPO_DIR/tools/aag/v2_contract.py" \
            "$REPO_DIR/tools/aag/rules_nas.yml" \
            "$REMOTE_114:$REMOTE_STAGE/" >>"$LOG" 2>&1; then
        failed NAS "scanner_sync"
        return
    fi
    ssh "${SSH_OPTS[@]}" "$REMOTE_114" \
        "git -C /srv/newtalk-v2 fetch -q origin main && tmp=\$(mktemp -d /tmp/aag-nas-main.XXXXXX) && trap 'rm -rf \"\$tmp\"' EXIT && git -C /srv/newtalk-v2 archive refs/remotes/origin/main | tar -x -C \"\$tmp\" && sha=\$(git -C /srv/newtalk-v2 rev-parse refs/remotes/origin/main) && cd '$REMOTE_STAGE' && AAG_PROJECT=NAS AAG_REPOSITORY_ID=newtalk-v2 AADS_COMMIT_SHA=\"\$sha\" AAG_EXPECTED_TARGET_REF_HEAD_SHA=\"\$sha\" AAG_TARGET_REF=refs/heads/main timeout 900 python3 scan_aads.py --root \"\$tmp\" --rules '$REMOTE_STAGE/rules_nas.yml' --out-dir '$remote_out' --json" \
        >>"$LOG" 2>&1
    local scan_rc=$?
    if ((scan_rc >= 2)); then
        failed NAS "clean_ref_scan"
        return
    fi
    if ! scp "${SSH_OPTS[@]}" -q \
        "$REMOTE_114:$remote_out/nas-graph.json" "$STATE_DIR/nas/" \
        >>"$LOG" 2>&1; then
        failed NAS "artifact_fetch"
        return
    fi
    if python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
        "NAS=$STATE_DIR/nas/nas-graph.json" >>"$LOG" 2>&1; then
        refreshed+=(NAS)
        log "STATUS project=NAS result=success source=origin/main"
    else
        failed NAS "snapshot_publish"
    fi
}

refresh_ntv2() {
    local remote_out="/var/lib/aads/aag/ntv2"
    mkdir -p "$STATE_DIR/ntv2"
    if ! ssh "${SSH_OPTS[@]}" "$REMOTE_114" \
        "mkdir -p '$REMOTE_STAGE' '$remote_out'" >>"$LOG" 2>&1 \
        || ! scp "${SSH_OPTS[@]}" -q \
            "$REPO_DIR/tools/aag/scan_ntv2_adapter.py" \
            "$REMOTE_114:$REMOTE_STAGE/" >>"$LOG" 2>&1; then
        failed NTV2 "scanner_sync"
        return
    fi
    ssh "${SSH_OPTS[@]}" "$REMOTE_114" \
        "git -C /srv/newtalk-v2 fetch -q origin main && tmp=\$(mktemp -d /tmp/aag-ntv2-main.XXXXXX) && trap 'rm -rf \"\$tmp\"' EXIT && git -C /srv/newtalk-v2 archive refs/remotes/origin/main | tar -x -C \"\$tmp\" && sha=\$(git -C /srv/newtalk-v2 rev-parse refs/remotes/origin/main) && timeout 900 python3 '$REMOTE_STAGE/scan_ntv2_adapter.py' --root \"\$tmp\" --scanner \"\$tmp/tools/aag/scan.py\" --rules \"\$tmp/tools/aag/rules.yml\" --out-dir '$remote_out' --commit-sha \"\$sha\" --expected-head-sha \"\$sha\"" \
        >>"$LOG" 2>&1
    local scan_rc=$?
    if ((scan_rc != 0)); then
        failed NTV2 "clean_ref_scan"
        return
    fi
    if ! scp "${SSH_OPTS[@]}" -q \
        "$REMOTE_114:$remote_out/ntv2-graph.json" "$STATE_DIR/ntv2/" \
        >>"$LOG" 2>&1; then
        failed NTV2 "artifact_fetch"
        return
    fi
    if python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
        "NTV2=$STATE_DIR/ntv2/ntv2-graph.json" >>"$LOG" 2>&1; then
        refreshed+=(NTV2)
        log "STATUS project=NTV2 result=success source=origin/main"
    else
        failed NTV2 "snapshot_publish"
    fi
}

begin_rc=0
sched begin ALL --owner "$SCHED_OWNER" --pid "$$" --stale-min 60 >/dev/null 2>&1 || begin_rc=$?
if ((begin_rc == EXIT_SKIPPED)); then
    log "STATUS project=ALL result=skipped reason=live_owner"
    exit 0
elif ((begin_rc != 0)); then
    log "WARN scheduler_state_unavailable rc=$begin_rc — continuing without heartbeat"
fi

log "START projects=AADS,GO100,KIS,SF,NTV2,NAS"
beat; refresh_aads
beat; refresh_go100_and_kis
beat; refresh_generic_114 SF /data/shortflow rules_sf.yml sf
beat; refresh_ntv2
beat; refresh_nas

join_csv() { local IFS=,; echo "$*"; }

if ((${#failures[@]})); then
    log "DONE result=failed projects=$(join_csv "${failures[@]}") refreshed=$(join_csv "${refreshed[@]:-}") skipped=$(join_csv "${skipped[@]:-}")"
    sched finish ALL --owner "$SCHED_OWNER" --outcome failed --reason "failed=$(join_csv "${failures[@]}")" --exit-code 1 >/dev/null 2>&1 || true
    exit 1
fi
if ((${#skipped[@]})); then
    # 일부 프로젝트를 스캔하지 않았다. "success projects=6" 이라고 적지 않는다.
    log "DONE result=partial_skipped refreshed=$(join_csv "${refreshed[@]:-}") skipped=$(join_csv "${skipped[@]}")"
    sched finish ALL --owner "$SCHED_OWNER" --outcome partial --reason "partial_skipped=$(join_csv "${skipped[@]}")" --exit-code 0 >/dev/null 2>&1 || true
    exit 0
fi
log "DONE result=success projects=6"
sched finish ALL --owner "$SCHED_OWNER" --outcome success --reason all_refreshed --exit-code 0 >/dev/null 2>&1 || true
