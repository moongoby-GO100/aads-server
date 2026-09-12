#!/bin/bash
# AADS-191 배포 자가치유 계층 — 실패 원인 분류 → 자동 교정 → 재개
#
# deploy.sh 가 source 하는 라이브러리다. 단독 실행하지 않는다.
# 설계/PRD: docs/prd/AADS-191-DEPLOY-SELF-HEALING-PRD.md
#
# 왜 필요한가:
#   deploy.sh 는 실패하면 record_deploy 로 실패만 적고 그 자리에서 끝났다.
#   start_deploy_queue_worker 호출이 성공 경로(post_success)와 락 대기 경로에만
#   있어서, 큐에 다음 릴리스가 있어도 이어받는 주체가 없었다.
#   2026-09-13 07:25 KST 에도 #366 성공 직후 큐가 #367 을 집었지만
#   "insufficient build disk" 로 preflight 에서 막힌 뒤 파이프라인이 멈췄다.
#   14일 실패·차단 241건 중 disk/dirty/stale/standby 계열은 사람이 하던 조치가
#   정해져 있다. 그 조치를 코드로 옮기고, 교정에 성공하면 스스로 재개한다.
#
# 안전 원칙(무한루프·폭주 방지):
#   1. 원인별 재시도는 릴리스 SHA 기준 기본 1회 (AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS)
#   2. 자가치유 기동 사이 최소 쿨다운 180초 (AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC)
#   3. 화이트리스트 원인만 재시도하고, 나머지는 CEO 에스컬레이션으로 끝낸다
#   4. AADS_DEPLOY_AUTOHEAL=0 이면 전체 비활성 (기존 동작과 동일)
#   5. 작업 트리를 건드리는 교정(stash/checkout/clean)은 절대 하지 않는다

AADS_DEPLOY_AUTOHEAL="${AADS_DEPLOY_AUTOHEAL:-1}"
AADS_DEPLOY_AUTOHEAL_DRYRUN="${AADS_DEPLOY_AUTOHEAL_DRYRUN:-0}"
AUTOHEAL_STATE_DIR="${AADS_DEPLOY_AUTOHEAL_STATE_DIR:-/tmp/aads-deploy-autoheal}"
AUTOHEAL_MAX_ATTEMPTS="${AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS:-1}"
AUTOHEAL_COOLDOWN_SEC="${AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC:-180}"
AUTOHEAL_BUILDER_PRUNE_UNTIL="${AADS_DEPLOY_AUTOHEAL_BUILDER_PRUNE_UNTIL:-48h}"
AUTOHEAL_LAST_REMEDIATION="none"
DEPLOY_LAST_FAIL_STATUS="${DEPLOY_LAST_FAIL_STATUS:-}"
DEPLOY_LAST_FAIL_ERROR="${DEPLOY_LAST_FAIL_ERROR:-}"

autoheal_log() {
    echo "[deploy.sh][autoheal] $*"
}

# ── 1단계: 원인 분류 ────────────────────────────────────────────────────────
# 입력은 deploy_runs 에 실제로 쌓인 phase/error_summary 문자열이다.
# 분류 키는 PRD 의 원인 코드와 1:1로 맞춘다.
classify_deploy_failure() {
    local phase="${1:-}"
    local err="${2:-}"
    local hay="${phase} ${err}"

    if [[ -z "${phase//[[:space:]]/}" && -z "${err//[[:space:]]/}" ]]; then
        echo "unknown"
        return 0
    fi

    case "$hay" in
        *"insufficient build disk"*|*"cannot read available disk"*|*"no space left on device"*)
            echo "disk_full" ;;
        *"dirty worktree"*)
            echo "dirty_worktree" ;;
        *"heartbeat exceeded"*|*"stale deploy"*|*"stale_auto"*|*"stale_process_reconciled"*|*"stale_reconciled"*)
            echo "stale_heartbeat" ;;
        *"deploy interrupted by"*|*"interrupted_post_switch"*)
            # TERM/INT 뿐 아니라 HUP/QUIT 도 같은 경로로 들어온다.
            # deploy_runs 실측(최근 14일) 기준 HUP 중단만 13건이었고,
            # 패턴을 신호 이름으로 고정하면 그만큼이 other→manual 로 새어나간다.
            echo "signal_interrupt" ;;
        *"standby same-digest sync"*)
            echo "standby_sync_fail" ;;
        *"memory limit mismatch"*)
            echo "mem_limit_mismatch" ;;
        *"release context too large"*)
            echo "release_context_too_large" ;;
        *"dependency lock"*)
            echo "dependency_lock_stale" ;;
        *"queued deploy wait timeout"*|*"flock acquisition failed"*)
            echo "lock_wait_timeout" ;;
        *"unexpected error exit="*)
            echo "unexpected_exit" ;;
        *)
            echo "other" ;;
    esac
}

# ── 2단계: 원인별 정책 (retry | manual) ─────────────────────────────────────
# retry = 교정 후 자동 재개, manual = 교정 불가로 CEO 에스컬레이션.
# 컷오버(nginx upstream 전환) 이후의 중단은 서비스가 이미 새 슬롯으로 살아 있다.
# 이 상태에서 전체 재배포를 자동으로 다시 돌리면 트래픽만 한 번 더 흔든다.
# 컷오버(nginx upstream 전환) 이후 phase 인지 이름만으로 판별한다.
# deploy.sh 2310행에서 DEPLOY_UPSTREAM_SWITCHED=true 가 되고,
# 그 이후 실행되는 phase 목록(2327~2511행)과 1:1로 맞춘다.
# 플래그가 유실된 경로(DB 복구 후 분류)에서도 재배포를 막는 2중 안전장치다.
autoheal_phase_is_post_switch() {
    case "${1:-}" in
        *post_switch*|standby_same_digest_sync|e2e_gate|db_schema_check|chat_table_check|llm_health_check|frontend_qa|p0p1_monitoring)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

autoheal_policy() {
    local cause="${1:-unknown}"
    local phase="${2:-${DEPLOY_CURRENT_PHASE:-}}"
    case "$cause" in
        disk_full|dirty_worktree|stale_heartbeat|standby_sync_fail|lock_wait_timeout)
            echo "retry" ;;
        signal_interrupt)
            if [[ "${DEPLOY_UPSTREAM_SWITCHED:-false}" == "true" ]] || autoheal_phase_is_post_switch "$phase"; then
                echo "manual"
            else
                echo "retry"
            fi
            ;;
        *)
            echo "manual" ;;
    esac
}

# ── 3단계: 원인별 자동 교정 ─────────────────────────────────────────────────
# 성공하면 0, 교정에 실패해 재개해도 같은 곳에서 막힐 것이 확실하면 1.
remediate_deploy_failure() {
    local cause="${1:-unknown}"
    AUTOHEAL_LAST_REMEDIATION="none"

    case "$cause" in
        disk_full)
            # prune_old_release_images 는 실행 중이 아닌 릴리스 이미지만 정리한다.
            # 실측(2026-09-13 07:27 KST) 기준 회수 가능량은 이미지 4.4GB 인데
            # 빌드 캐시가 22.28GB 였다. 캐시까지 회수해야 임계(기본 20GB)로 돌아온다.
            autoheal_log "디스크 회수 시작: release image retention + dangling image + build cache(until=${AUTOHEAL_BUILDER_PRUNE_UNTIL})"
            if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" == "1" ]]; then
                autoheal_log "DRYRUN: docker prune 생략"
            else
                prune_old_release_images || true
                docker image prune -f >/dev/null 2>&1 || true
                docker builder prune -f --filter "until=${AUTOHEAL_BUILDER_PRUNE_UNTIL}" >/dev/null 2>&1 || true
            fi
            AUTOHEAL_LAST_REMEDIATION="disk_reclaim"
            if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" != "1" ]] && ! require_build_disk_free >/dev/null 2>&1; then
                autoheal_log "❌ 회수 후에도 빌드 디스크 임계 미달 — 자동 재개를 중단한다"
                return 1
            fi
            autoheal_log "✅ 빌드 디스크 임계 복귀"
            ;;
        dirty_worktree)
            # 작업 트리는 절대 건드리지 않는다(CEO/러너의 미커밋 작업 보호).
            # 큐 워커는 커밋된 SHA 의 clean detached worktree 에서 돌기 때문에
            # 같은 릴리스를 큐 경로로 다시 태우면 dirty 게이트 자체가 사라진다.
            autoheal_log "dirty worktree 교정: 작업 트리 보존, 큐 워커의 clean worktree 경로로 우회한다"
            AUTOHEAL_LAST_REMEDIATION="route_clean_worktree"
            ;;
        stale_heartbeat|lock_wait_timeout)
            autoheal_log "stale/lock 교정: reconcile 은 이미 적용됨 — 재개만 수행한다"
            AUTOHEAL_LAST_REMEDIATION="reconcile_then_retry"
            ;;
        standby_sync_fail)
            # 컷오버는 끝났고 standby 한쪽만 어긋난 상태다. 같은 릴리스를 다시
            # 태우면 동일 digest 동기화 경로로 들어가 standby 만 맞춘다.
            autoheal_log "standby 동기화 교정: 동일 릴리스 재기동으로 standby 슬롯만 정렬한다"
            AUTOHEAL_LAST_REMEDIATION="standby_resync"
            ;;
        signal_interrupt)
            autoheal_log "중단 신호 교정: 컷오버 전 중단으로 판단 — 동일 릴리스를 다시 태운다"
            AUTOHEAL_LAST_REMEDIATION="restart_release"
            ;;
        *)
            return 1 ;;
    esac
    return 0
}

# ── 4단계: 재시도 예산 · 쿨다운 (폭주 차단) ─────────────────────────────────
autoheal_attempt_file() {
    local cause="${1:-unknown}"
    local sha="${AADS_RELEASE_SHA:-unknown}"
    echo "${AUTOHEAL_STATE_DIR}/${sha}.${cause}.attempts"
}

autoheal_attempt_count() {
    local file
    file="$(autoheal_attempt_file "${1:-unknown}")"
    local n
    n="$(cat "$file" 2>/dev/null || echo 0)"
    if [[ "$n" =~ ^[0-9]+$ ]]; then
        echo "$n"
    else
        echo 0
    fi
}

autoheal_attempt_bump() {
    local cause="${1:-unknown}"
    local file n
    file="$(autoheal_attempt_file "$cause")"
    n="$(autoheal_attempt_count "$cause")"
    mkdir -p "$AUTOHEAL_STATE_DIR" 2>/dev/null || true
    echo "$((n + 1))" > "$file" 2>/dev/null || true
}

autoheal_cooldown_ok() {
    local marker="${AUTOHEAL_STATE_DIR}/last_launch_epoch"
    local last now
    last="$(cat "$marker" 2>/dev/null || echo 0)"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    now="$(date +%s)"
    if (( now - last < AUTOHEAL_COOLDOWN_SEC )); then
        autoheal_log "쿨다운 중: 마지막 자가치유 기동 $((now - last))초 전 (최소 ${AUTOHEAL_COOLDOWN_SEC}초)"
        return 1
    fi
    return 0
}

autoheal_cooldown_stamp() {
    mkdir -p "$AUTOHEAL_STATE_DIR" 2>/dev/null || true
    date +%s > "${AUTOHEAL_STATE_DIR}/last_launch_epoch" 2>/dev/null || true
}

# ── 5단계: 재개 — 동일 릴리스를 큐에 넣고 clean worktree 워커로 기동 ────────
queue_autoheal_retry_request() {
    local cause="${1:-unknown}"
    if ! deploy_db_available; then
        autoheal_log "❌ DB 불가 — 큐 등록 생략"
        return 1
    fi
    local release_sql reason_sql run_id
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    reason_sql="$(sql_escape "autoheal retry: cause=${cause}; remediation=${AUTOHEAL_LAST_REMEDIATION}")"
    run_id="$(
        deploy_db_exec "
            INSERT INTO deploy_runs(project, release_sha, status, phase, phase_started_at,
                                    deploy_pid, last_heartbeat_at, queue_position,
                                    error_summary, requested_by, request_source,
                                    commit_status, push_status, auto_start,
                                    requested_at, created_at, updated_at)
            SELECT 'AADS', '$release_sql', 'queued', 'queued_for_deploy', NOW(),
                   $$, NOW(), 1, '$reason_sql', 'deploy.sh_autoheal', 'autoheal_${cause}',
                   'committed', 'pushed', TRUE, NOW(), NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM deploy_runs
                WHERE project='AADS'
                  AND status IN ('queued','running','verifying','syncing_standby')
                  AND release_sha='$release_sql'
            )
            RETURNING id;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ -n "${run_id:-}" ]]; then
        autoheal_log "재개 큐 등록: deploy_run_id=${run_id}, release=${AADS_RELEASE_SHA:-unknown}"
    else
        autoheal_log "재개 큐 등록 생략: 동일 릴리스가 이미 큐/진행 중"
    fi
    return 0
}

launch_autoheal_worker() {
    local cause="${1:-unknown}"
    local launcher=""
    if [[ -x "${COMPOSE_DIR}/scripts/start_aads_deploy_queue_worker.sh" ]]; then
        launcher="${COMPOSE_DIR}/scripts/start_aads_deploy_queue_worker.sh"
    elif [[ -x "${STATE_DIR}/scripts/start_aads_deploy_queue_worker.sh" ]]; then
        launcher="${STATE_DIR}/scripts/start_aads_deploy_queue_worker.sh"
    else
        autoheal_log "❌ 큐 워커 런처를 찾을 수 없다 — 재개 불가"
        return 1
    fi
    if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" == "1" ]]; then
        autoheal_log "DRYRUN: 워커 기동 생략 (${launcher} ${MODE:-bluegreen} autoheal_${cause})"
        return 0
    fi
    autoheal_cooldown_stamp
    bash "$launcher" "${MODE:-bluegreen}" "autoheal_${cause}" || {
        autoheal_log "❌ 큐 워커 기동 실패"
        return 1
    }
    return 0
}

autoheal_escalate() {
    local cause="${1:-unknown}"
    local reason="${2:-}"
    autoheal_log "🚨 자동 복구 불가 — CEO 에스컬레이션: cause=${cause}, reason=${reason}"
    audit_control "autoheal" "deploy_runs:${DEPLOY_RUN_ID:-none}" "escalated" \
        "cause=${cause}; reason=${reason}; release=${AADS_RELEASE_SHA:-unknown}" || true
    # notify() 는 deploy.sh 뒷부분에서 정의된다. preflight 단계 실패 시점에는
    # 아직 정의 전일 수 있으므로 존재를 확인하고 호출한다.
    if declare -F notify >/dev/null 2>&1; then
        notify "❌ 배포 자동복구 실패 (${cause}) — ${reason}. release=${AADS_RELEASE_SHA:-unknown}" || true
    fi
}

# ── 진입점: cleanup_deploy(EXIT 트랩)에서 락 해제 후 호출된다 ───────────────
deploy_autoheal_on_exit() {
    local rc="${1:-0}"
    if [[ "$rc" == "0" ]]; then
        return 0
    fi
    if [[ "${AADS_DEPLOY_AUTOHEAL}" != "1" ]]; then
        autoheal_log "비활성화 상태(AADS_DEPLOY_AUTOHEAL=0) — 기존 동작대로 종료한다"
        return 0
    fi

    local phase err cause policy attempts
    phase="${DEPLOY_CURRENT_PHASE:-unknown}"
    err="${DEPLOY_LAST_FAIL_ERROR:-}"
    if [[ -z "${err//[[:space:]]/}" && -n "${DEPLOY_RUN_ID:-}" ]] && deploy_db_available; then
        err="$(deploy_db_exec "SELECT COALESCE(error_summary,'') FROM deploy_runs WHERE id=${DEPLOY_RUN_ID};" | tail -1)"
    fi
    cause="$(classify_deploy_failure "$phase" "$err")"
    policy="$(autoheal_policy "$cause" "$phase")"
    attempts="$(autoheal_attempt_count "$cause")"
    autoheal_log "실패 감지: rc=${rc}, phase=${phase}, cause=${cause}, policy=${policy}, attempts=${attempts}/${AUTOHEAL_MAX_ATTEMPTS}"
    audit_control "autoheal" "deploy_runs:${DEPLOY_RUN_ID:-none}" "classified" \
        "cause=${cause}; policy=${policy}; phase=${phase}; attempts=${attempts}" || true

    if [[ "$policy" != "retry" ]]; then
        autoheal_escalate "$cause" "정책상 자동 재시도 대상이 아님 (phase=${phase})"
        return 0
    fi
    if (( attempts >= AUTOHEAL_MAX_ATTEMPTS )); then
        autoheal_escalate "$cause" "재시도 예산 소진 (${attempts}/${AUTOHEAL_MAX_ATTEMPTS})"
        return 0
    fi
    if ! autoheal_cooldown_ok; then
        autoheal_escalate "$cause" "쿨다운 미충족 — 연속 재시도 폭주 차단"
        return 0
    fi
    if ! remediate_deploy_failure "$cause"; then
        autoheal_escalate "$cause" "자동 교정 실패 (remediation=${AUTOHEAL_LAST_REMEDIATION})"
        return 0
    fi

    autoheal_attempt_bump "$cause"
    # 큐 등록이 실패하면(대개 DB 불가) 워커를 띄워도 집을 릴리스가 없다.
    # 그 상태로 "재개 완료"를 찍으면 실패를 은폐하게 되므로 에스컬레이션한다.
    if ! queue_autoheal_retry_request "$cause"; then
        autoheal_escalate "$cause" "재개 큐 등록 실패 — 워커를 기동해도 집을 릴리스가 없다"
        return 0
    fi
    if launch_autoheal_worker "$cause"; then
        autoheal_log "✅ 자가치유 재개 기동 완료: cause=${cause}, remediation=${AUTOHEAL_LAST_REMEDIATION}"
        audit_control "autoheal" "deploy_runs:${DEPLOY_RUN_ID:-none}" "retry_launched" \
            "cause=${cause}; remediation=${AUTOHEAL_LAST_REMEDIATION}; release=${AADS_RELEASE_SHA:-unknown}" || true
        if declare -F notify >/dev/null 2>&1; then
            notify "🔄 배포 자동복구 재개: ${cause} → ${AUTOHEAL_LAST_REMEDIATION} (release=${AADS_RELEASE_SHA:-unknown})" || true
        fi
    else
        autoheal_escalate "$cause" "재개 워커 기동 실패"
    fi
    return 0
}
