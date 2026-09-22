#!/usr/bin/env bash
# Refresh the six-project AAG inventory from the central AADS host.
# Runtime artifacts stay outside every repository so scans never dirty worktrees.
set -uo pipefail

REPO_DIR="${AAG_REPO_DIR:-/root/aads/aads-server}"
STATE_DIR="${AAG_STATE_DIR:-/var/lib/aads/aag}"
LOG="${AAG_ALL_PROJECTS_LOG:-/var/log/aads-pipeline/aag-all-projects.log}"
REMOTE_114="${AAG_REMOTE_114:-root@114.207.244.86}"
REMOTE_STAGE="${AAG_REMOTE_STAGE:-/tmp/aag-central-scanner}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15)
failures=()

mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
exec 9>"/tmp/aag-all-projects-refresh.lock"
if ! flock -n 9; then
    printf '[%s] already running — skip\n' "$(date '+%F %T %Z')" >>"$LOG"
    exit 0
fi

log() { printf '[%s] %s\n' "$(date '+%F %T %Z')" "$*" | tee -a "$LOG"; }
failed() { failures+=("$1"); log "STATUS project=$1 result=failed detail=$2"; }

refresh_aads() {
    if systemctl start aads-aag-struct-check.service \
        && python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
            "AADS=/var/log/aads-pipeline/aag-struct/aads-graph.json" >>"$LOG" 2>&1; then
        log "STATUS project=AADS result=success"
    else
        failed AADS "scan_or_publish"
    fi
}

refresh_go100_and_kis() {
    if bash "$REPO_DIR/scripts/aag_go100_refresh.sh" >>"$LOG" 2>&1; then
        log "STATUS project=GO100 result=success"
    else
        failed GO100 "remote_scan_or_publish"
        return
    fi
    if python3 "$REPO_DIR/scripts/aag_snapshot_push.py" \
        "KIS=$STATE_DIR/go100/go100-graph.json" >>"$LOG" 2>&1; then
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
        log "STATUS project=NTV2 result=success source=origin/main"
    else
        failed NTV2 "snapshot_publish"
    fi
}

log "START projects=AADS,GO100,KIS,SF,NTV2,NAS"
refresh_aads
refresh_go100_and_kis
refresh_generic_114 SF /data/shortflow rules_sf.yml sf
refresh_ntv2
refresh_nas

if ((${#failures[@]})); then
    log "DONE result=failed projects=$(IFS=,; echo "${failures[*]}")"
    exit 1
fi
log "DONE result=success projects=6"
