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
#   1. 원인별 재시도는 릴리스 SHA 기준 기본 1회 (AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS).
#      단 target_drain_busy 처럼 "기다리는 것이 교정"인 원인만 예산을 따로 둔다
#      (AADS_DEPLOY_AUTOHEAL_DRAIN_MAX_ATTEMPTS, 기본 5) — autoheal_max_attempts 참조
#   2. 동일 릴리스 SHA·원인 조합별 최소 쿨다운 180초 (AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC).
#      다른 SHA 또는 다른 원인은 서로 쿨다운을 공유하지 않는다 — 한 릴리스의
#      disk_full 쿨다운이 다른 릴리스의 target_drain_busy 재시도를 막으면 안 된다.
#   3. 화이트리스트 원인만 재시도하고, 나머지는 CEO 에스컬레이션으로 끝낸다
#   4. AADS_DEPLOY_AUTOHEAL=0 이면 전체 비활성 (기존 동작과 동일)
#   5. 작업 트리를 건드리는 교정(stash/checkout/clean)은 절대 하지 않는다

AADS_DEPLOY_AUTOHEAL="${AADS_DEPLOY_AUTOHEAL:-1}"
AADS_DEPLOY_AUTOHEAL_DRYRUN="${AADS_DEPLOY_AUTOHEAL_DRYRUN:-0}"
AUTOHEAL_STATE_DIR="${AADS_DEPLOY_AUTOHEAL_STATE_DIR:-/tmp/aads-deploy-autoheal}"
AUTOHEAL_MAX_ATTEMPTS="${AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS:-1}"
AUTOHEAL_COOLDOWN_SEC="${AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC:-180}"
AUTOHEAL_BUILDER_PRUNE_UNTIL="${AADS_DEPLOY_AUTOHEAL_BUILDER_PRUNE_UNTIL:-48h}"
AUTOHEAL_BUILDER_PRUNE_TIGHT="${AADS_DEPLOY_AUTOHEAL_BUILDER_PRUNE_TIGHT:-6h}"
AUTOHEAL_LAST_REMEDIATION="none"
AUTOHEAL_SUCCESSOR_RUN_ID=""
AUTOHEAL_SUCCESSOR_KIND=""
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
        # 후보 슬롯에 채팅 턴이 살아 있어 drain 이 끝나지 않은 경우.
        # 2026-09-21 24시간 실측: 실패·차단 48건 중 14건이 이것이었고,
        # 사유는 전부 "active streams=1" 또는 "=2" 였다. 분류가 없어서
        # other → manual 로 빠져 한 건도 자동 재개되지 않았다.
        # 이 실패는 코드·자원 문제가 아니라 **시점 문제**다. 몇 분 뒤면
        # 그 턴이 끝나 같은 배포가 그대로 성공한다 — 가장 재시도할 값이 크다.
        *"active streams="*)
            echo "target_drain_busy" ;;
        *"memory limit mismatch"*)
            echo "mem_limit_mismatch" ;;
        *"release context too large"*)
            echo "release_context_too_large" ;;
        *"dependency lock"*)
            echo "dependency_lock_stale" ;;
        *"queued deploy wait timeout"*|*"flock acquisition failed"*)
            echo "lock_wait_timeout" ;;
        # 아래 두 가지는 "unexpected error exit=" 안에 섞여 들어오지만 성격이
        # 전혀 다르다. 한 통에 넣으면 둘 다 manual 로 빠져 자동 교정이 안 된다.
        # (2026-09-13 실측: manual 8건 중 3건이 이 두 유형)
        #
        # 컨테이너 재생성 실패. 대개 일시적 자원 경합이라 재시도로 풀린다.
        *"unexpected error exit="*"docker compose"*|*"unexpected error exit="*"force-recreate"*)
            echo "container_recreate_fail" ;;
        # deploy.sh 를 없는 모드로 호출한 것. 재시도해도 같은 결과다.
        *"unknown mode:"*)
            echo "invalid_mode" ;;
        # 의존성 이미지가 없어 빌드가 막힌 경우. 고치는 방법이 정해져 있다 —
        # warm-deps 로 의존성 이미지를 만들고 같은 릴리스를 다시 태운다.
        # 2026-09-21 24시간 실측: build_candidate_image 실패 17건 전부가
        # control audit 에 "dependency image missing; warm-deps required" 를
        # 남겼는데, error_summary 에는 "return 1" 만 남아 unexpected_exit(manual)
        # 로 빠졌다. 17건 중 한 건도 자동 재개되지 않았다.
        *"dependency image missing"*|*"warm-deps required"*)
            echo "dependency_image_missing" ;;
        # 의존성 이미지가 있지만 키 라벨이 어긋난 경우. warm-deps 가 현재 키로
        # 다시 만들어 붙이므로 같은 교정으로 풀린다.
        *"immutable dependency image mismatch"*|*"dependency image verification failed"*)
            echo "dependency_image_mismatch" ;;
        # 릴리스 태그가 다른 revision 을 가리킨다. 이미지를 덮어쓰는 것은
        # 무결성 위반이라 자동 교정 대상이 아니다 — 사유만 분명히 남긴다.
        *"immutable image tag mismatch"*)
            echo "release_image_tag_mismatch" ;;
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
        disk_full|dirty_worktree|stale_heartbeat|standby_sync_fail|lock_wait_timeout|source_dir_missing|target_drain_busy|dependency_image_missing|dependency_image_mismatch)
            echo "retry" ;;
        # 컨테이너 재생성은 자원 경합으로 실패하는 경우가 많아 재시도할 값이 있다.
        # 다만 컷오버 이후라면 이미 트래픽이 넘어간 뒤이므로 손대지 않는다.
        container_recreate_fail)
            if [[ "${DEPLOY_UPSTREAM_SWITCHED:-false}" == "true" ]] || autoheal_phase_is_post_switch "$phase"; then
                echo "manual"
            else
                echo "retry"
            fi
            ;;
        # 호출 방식이 틀린 것이라 재시도해도 같은 결과다. 사람이 고쳐야 한다.
        invalid_mode)
            echo "manual" ;;
        # 같은 태그가 다른 revision 을 가리키는 무결성 위반이다. 자동으로
        # 덮어쓰지 않는다 — 어느 이미지가 맞는지는 사람이 판정해야 한다.
        release_image_tag_mismatch)
            echo "manual" ;;
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

# 배포 소스(릴리스 worktree)가 실재하는지 본다. 메시지 파싱보다 확실한 판정이다.
autoheal_source_dir_ok() {
    local dir="${COMPOSE_DIR:-}"
    [[ -n "$dir" && -f "${dir}/docker-compose.prod.yml" ]]
}

# docker image/builder prune 은 삭제를 비동기로 끝낸다. 2026-09-13 08:00 KST 실측에서
# prune 직후 avail 이 17,863MB 였다가 1~2분 뒤 25,436MB 로 회복됐는데, 즉시 한 번만
# 재확인해서 "회수 실패"로 조기 에스컬레이션했다. 짧게 정착을 기다리며 재확인한다.
autoheal_wait_disk_recovery() {
    local attempts interval i
    attempts="${AADS_DEPLOY_AUTOHEAL_DISK_RECHECKS:-6}"
    interval="${AADS_DEPLOY_AUTOHEAL_DISK_RECHECK_SEC:-5}"
    i=1
    while (( i <= attempts )); do
        if require_build_disk_free >/dev/null 2>&1; then
            autoheal_log "빌드 디스크 임계 복귀 확인 (${i}/${attempts}회차)"
            return 0
        fi
        if (( i < attempts )); then
            sleep "$interval"
        fi
        i=$(( i + 1 ))
    done
    return 1
}

# 릴리스 worktree 가 배포 도중 사라진 경우(정리 스크립트·수동 삭제) 같은 커밋으로
# 다시 만든다. 운영 트리(STATE_DIR)와 미커밋 작업은 절대 건드리지 않는다.
autoheal_recreate_release_worktree() {
    local sha dir repo
    sha="${AADS_RELEASE_SHA:-}"
    dir="${COMPOSE_DIR:-}"
    repo="${STATE_DIR:-/root/aads/aads-server}"
    if [[ -z "$sha" || "$sha" == "unknown" || -z "$dir" ]]; then
        return 1
    fi
    if [[ "$dir" == "$repo" ]]; then
        return 1
    fi
    git -C "$repo" worktree prune >/dev/null 2>&1 || true
    git -C "$repo" worktree add --detach "$dir" "$sha" >/dev/null 2>&1 || return 1
    [[ -f "${dir}/docker-compose.prod.yml" ]]
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
                autoheal_log "디스크 회수 1단계: release image + dangling image + build cache(until=${AUTOHEAL_BUILDER_PRUNE_UNTIL})"
                prune_old_release_images || true
                docker image prune -f >/dev/null 2>&1 || true
                docker builder prune -f --filter "until=${AUTOHEAL_BUILDER_PRUNE_UNTIL}" >/dev/null 2>&1 || true

                # 2026-09-21 #4993/#4994 실측: 1단계 회수량이 363MB 뿐이라 878MB 부족을
                # 메우지 못하고 두 배포가 연속 차단됐다. 빌드 캐시 23.71GB 중 56건
                # 가운데 55건이 active 로 잡혀 until=48h 필터에 전부 걸러졌기 때문이다.
                # 필터를 좁혀 한 번 더 훑는다.
                if ! require_build_disk_free >/dev/null 2>&1; then
                    autoheal_log "디스크 회수 2단계: build cache(until=${AUTOHEAL_BUILDER_PRUNE_TIGHT})"
                    docker builder prune -f --filter "until=${AUTOHEAL_BUILDER_PRUNE_TIGHT}" >/dev/null 2>&1 || true
                fi

                # 3단계는 빌드 캐시를 전량 버린다. 다음 빌드가 캐시 없이 도는 비용을
                # 치르지만 배포가 막히는 것보다 낫다. 이미지·컨테이너·볼륨은 건드리지
                # 않으므로 실행 중 서비스에는 영향이 없다.
                if [[ "${AADS_DEPLOY_AUTOHEAL_DISK_HARD_PRUNE:-1}" == "1" ]] \
                   && ! require_build_disk_free >/dev/null 2>&1; then
                    autoheal_log "디스크 회수 3단계: build cache 전량(-a) — 다음 빌드는 캐시 없이 돈다"
                    docker builder prune -af >/dev/null 2>&1 || true
                fi
            fi
            AUTOHEAL_LAST_REMEDIATION="disk_reclaim"
            if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" != "1" ]] && ! autoheal_wait_disk_recovery; then
                autoheal_log "❌ 3단계 회수 후에도 빌드 디스크 임계 미달 — 자동 재개를 중단한다"
                return 1
            fi
            autoheal_log "✅ 빌드 디스크 임계 복귀"
            ;;
        target_drain_busy)
            # 고칠 것이 없다. 기다리는 것이 교정이다.
            # 후보 슬롯의 채팅 턴이 끝나기를 잠깐 기다린 뒤 같은 릴리스를 다시 건다.
            # 여기서 스트림을 끊지 않는다 — 끊으면 생성 중이던 답변이 사라진다.
            AUTOHEAL_LAST_REMEDIATION="wait_for_target_drain"
            local wait_max="${AADS_DEPLOY_AUTOHEAL_DRAIN_WAIT:-120}"
            local waited=0
            autoheal_log "후보 슬롯 drain 대기: 최대 ${wait_max}초 (스트림을 끊지 않는다)"
            if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" == "1" ]]; then
                autoheal_log "DRYRUN: drain 대기 생략"
            else
                # 끝났는지 보고 나간다. 고정 대기는 스트림이 10초 만에 끝나도
                # 120초를 채우고, 그만큼 배포 레인을 붙잡는다.
                local _drain_left
                while (( waited < wait_max )); do
                    sleep 10
                    waited=$((waited + 10))
                    if declare -F stream_count_for_port >/dev/null 2>&1 && [[ -n "${NEW_PORT:-}" ]]; then
                        _drain_left="$(stream_count_for_port "$NEW_PORT" 2>/dev/null || echo unknown)"
                        if [[ "$_drain_left" == "0" ]]; then
                            autoheal_log "후보 슬롯 drain 조기 완료 (${waited}초, active=0)"
                            break
                        fi
                    fi
                done
            fi
            autoheal_log "✅ drain 대기 완료(${waited}초) — 같은 릴리스로 재개한다"
            ;;
        source_dir_missing)
            autoheal_log "릴리스 소스 복구: ${COMPOSE_DIR:-unknown} 재생성 (sha=${AADS_RELEASE_SHA:-unknown})"
            AUTOHEAL_LAST_REMEDIATION="recreate_release_worktree"
            if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" == "1" ]]; then
                autoheal_log "DRYRUN: worktree 재생성 생략"
            elif ! autoheal_recreate_release_worktree; then
                autoheal_log "❌ 릴리스 worktree 재생성 실패 — 자동 재개를 중단한다"
                return 1
            fi
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
        dependency_image_missing|dependency_image_mismatch)
            # deploy.sh 자신이 "Run 'bash deploy.sh warm-deps' before bluegreen" 라고
            # 적어 두고 사람을 기다렸다. 그 한 줄을 여기서 대신 실행한다.
            # 릴리스 worktree 의 deploy.sh 를 쓴다 — 그쪽은 커밋된 clean 트리라
            # warm-deps 의 worktree 게이트를 그대로 통과한다.
            # 자식에서는 자가치유를 끈다(AADS_DEPLOY_AUTOHEAL=0). 재시도 판단은
            # 이 프로세스가 갖고 있고, 자식이 또 큐를 넣으면 폭주한다.
            AUTOHEAL_LAST_REMEDIATION="warm_dependency_image"
            local warm_timeout="${AADS_DEPLOY_AUTOHEAL_WARMDEPS_TIMEOUT:-1800}"
            [[ "$warm_timeout" =~ ^[0-9]+$ ]] || warm_timeout=1800
            local warm_script=""
            if [[ -f "${COMPOSE_DIR:-}/deploy.sh" ]]; then
                warm_script="${COMPOSE_DIR}/deploy.sh"
            elif [[ -f "${STATE_DIR:-}/deploy.sh" ]]; then
                warm_script="${STATE_DIR}/deploy.sh"
            fi
            if [[ -z "$warm_script" ]]; then
                autoheal_log "❌ warm-deps 를 돌릴 deploy.sh 를 찾을 수 없다 — 자동 재개를 중단한다"
                return 1
            fi
            autoheal_log "의존성 이미지 warm-up: ${warm_script} warm-deps (최대 ${warm_timeout}초)"
            if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" == "1" ]]; then
                autoheal_log "DRYRUN: warm-deps 생략"
            elif ! AADS_DEPLOY_AUTOHEAL=0 timeout --kill-after=30s "$warm_timeout" \
                    bash "$warm_script" warm-deps; then
                autoheal_log "❌ 의존성 이미지 warm-up 실패 — 자동 재개를 중단한다"
                return 1
            fi
            autoheal_log "✅ 의존성 이미지 준비 완료 — 같은 릴리스로 재개한다"
            ;;
        *)
            return 1 ;;
    esac
    return 0
}

# ── 4단계: 재시도 예산 · 쿨다운 (폭주 차단) ─────────────────────────────────
# 릴리스 SHA·원인 둘 다 상태 파일명(및 SQL 리터럴)로 그대로 쓰인다. 구분자·공백·
# "unknown" 플레이스홀더를 거부해 경로 이탈(../)이나 다른 릴리스와의 상태 공유를
# 막는다. 유효하지 않으면 실패시켜 호출자가 별도 원인(코드)으로 재분류하게 한다.
autoheal_key_valid() {
    [[ "$1" =~ ^[a-zA-Z0-9_-]{1,64}$ && "$1" != "unknown" \
       && "$2" =~ ^[a-zA-Z0-9_]{1,64}$ && "$2" != "unknown" ]]
}

autoheal_attempt_file() {
    local cause="${1:-unknown}"
    local sha="${AADS_RELEASE_SHA:-unknown}"
    autoheal_key_valid "$sha" "$cause" || return 1
    echo "${AUTOHEAL_STATE_DIR}/${sha}.${cause}.attempts"
}

autoheal_attempt_count() {
    local file
    file="$(autoheal_attempt_file "${1:-unknown}")" || { echo 0; return 1; }
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
    file="$(autoheal_attempt_file "$cause")" || return 1
    n="$(autoheal_attempt_count "$cause")"
    mkdir -p "$AUTOHEAL_STATE_DIR" 2>/dev/null || true
    echo "$((n + 1))" > "$file" 2>/dev/null || true
}

# 원인별 재시도 예산. 전역 1회는 "고쳐서 다시"인 원인에는 맞지만
# "기다렸다 다시"인 원인에는 모자란다.
# 2026-09-21 실측: #4988(19:57 KST) target_drain_busy 를 retry_launched 로 살려
# 놓고도, 120초 뒤 #4990(20:08 KST)에서 스트림이 아직 3건이라 예산 1/1 이
# 소진돼 결국 사람에게 넘어갔다. 활성 채팅 턴은 수 분~십수 분 이어지므로
# 한 번의 대기로는 끝나지 않는다. 이 원인의 교정은 부작용이 없고(스트림을
# 끊지 않는다) 비용도 대기뿐이라 예산을 늘리는 것이 안전하다.
autoheal_max_attempts() {
    local cause="${1:-unknown}"
    # 운영자가 전역 예산을 명시했으면 그것이 우선이다(긴급 차단 경로 유지).
    if [[ -n "${AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS:-}" ]]; then
        echo "$AUTOHEAL_MAX_ATTEMPTS"
        return 0
    fi
    local n
    case "$cause" in
        target_drain_busy)
            n="${AADS_DEPLOY_AUTOHEAL_DRAIN_MAX_ATTEMPTS:-5}" ;;
        *)
            n="$AUTOHEAL_MAX_ATTEMPTS" ;;
    esac
    [[ "$n" =~ ^[0-9]+$ ]] || n="$AUTOHEAL_MAX_ATTEMPTS"
    echo "$n"
}

autoheal_cooldown_file() {
    local cause="${1:-unknown}"
    local sha="${AADS_RELEASE_SHA:-unknown}"
    autoheal_key_valid "$sha" "$cause" || return 1
    echo "${AUTOHEAL_STATE_DIR}/${sha}.${cause}.last_launch_epoch"
}

autoheal_cooldown_ok() {
    local cause="${1:-unknown}"
    local marker
    marker="$(autoheal_cooldown_file "$cause")" || return 1
    local last now cooldown
    last="$(cat "$marker" 2>/dev/null || echo 0)"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    now="$(date +%s)"
    cooldown="$AUTOHEAL_COOLDOWN_SEC"
    [[ "$cooldown" =~ ^[0-9]+$ ]] || cooldown=180
    if (( now - last < cooldown )); then
        autoheal_log "쿨다운 중: 마지막 자가치유 기동 $((now - last))초 전 (최소 ${cooldown}초)"
        return 1
    fi
    return 0
}

autoheal_cooldown_stamp() {
    local cause="${1:-unknown}"
    local marker
    marker="$(autoheal_cooldown_file "$cause")" || return 1
    mkdir -p "$AUTOHEAL_STATE_DIR" 2>/dev/null || true
    date +%s > "$marker" 2>/dev/null
}

# ── 5단계: 재개 — 동일 릴리스를 큐에 넣고 clean worktree 워커로 기동 ────────
# 후속 run 인정 조건은 scripts/start_aads_deploy_queue_worker.sh 가 실제로 집는
# 조건과 반드시 같아야 한다(AI 리뷰 P1, f19c3491 반려 사유). 그 워커는
#   status='queued' AND phase='queued_for_deploy' AND COALESCE(auto_start,FALSE)=TRUE
# 인 행만 claim 한다 — auto_start=false 수동 대기 행은 아무도 자동으로 집지 않는다.
# running/verifying/syncing_standby 는 이미 실행 중인 상태 그 자체이므로 추가
# phase 조건 없이 인정한다. 이 조건이 어긋나면 "후속이 있다"고 믿고 원본
# blocked 행을 superseded 로 세탁하면서 실제로는 아무도 재개하지 않는 상태가
# 생긴다.
queue_autoheal_retry_request() {
    local cause="${1:-unknown}"
    AUTOHEAL_SUCCESSOR_RUN_ID=""
    AUTOHEAL_SUCCESSOR_KIND=""
    if ! autoheal_key_valid "${AADS_RELEASE_SHA:-unknown}" "$cause" \
       || [[ ! "${DEPLOY_RUN_ID:-0}" =~ ^[0-9]+$ ]]; then
        autoheal_log "❌ 재개 큐 키 또는 원본 run ID가 유효하지 않다"
        return 1
    fi
    if ! deploy_db_available; then
        autoheal_log "❌ DB 불가 — 큐 등록 생략"
        return 1
    fi
    local release_sql reason_sql run_id
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    reason_sql="$(sql_escape "autoheal retry: cause=${cause}; remediation=${AUTOHEAL_LAST_REMEDIATION}")"

    # 실행 중인 distinct successor가 있으면 새 큐/워커가 필요 없다. legacy NULL은
    # API/production 기본값으로 정규화하되, 다른 component/env는 절대 섞지 않는다.
    run_id="$(
        deploy_db_exec "
            SELECT id FROM deploy_runs
             WHERE id <> ${DEPLOY_RUN_ID}
               AND upper(trim(project))='AADS'
               AND release_sha='$release_sql'
               AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
               AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
               AND status IN ('running','verifying','syncing_standby')
             ORDER BY id DESC LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$run_id" =~ ^[0-9]+$ && "$run_id" != "${DEPLOY_RUN_ID}" ]]; then
        AUTOHEAL_SUCCESSOR_RUN_ID="$run_id"
        AUTOHEAL_SUCCESSOR_KIND="active"
        autoheal_log "동일 릴리스 active successor 확인: deploy_run_id=${run_id}"
        return 0
    fi

    # 이미 worker가 집을 수 있는 queued successor가 있으면 그 정확한 ID를
    # 재사용한다. waiting_batch_predecessor/auto_start=false는 대상이 아니다.
    run_id="$(
        deploy_db_exec "
            SELECT id FROM deploy_runs
             WHERE id <> ${DEPLOY_RUN_ID}
               AND upper(trim(project))='AADS'
               AND release_sha='$release_sql'
               AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
               AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
               AND status='queued' AND phase='queued_for_deploy'
               AND COALESCE(auto_start, FALSE)=TRUE
             ORDER BY id ASC LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$run_id" =~ ^[0-9]+$ && "$run_id" != "${DEPLOY_RUN_ID}" ]]; then
        AUTOHEAL_SUCCESSOR_RUN_ID="$run_id"
        AUTOHEAL_SUCCESSOR_KIND="queued"
        autoheal_log "기존 eligible queued successor 재사용: deploy_run_id=${run_id}"
        return 0
    fi

    run_id="$(
        deploy_db_exec "
            INSERT INTO deploy_runs(project, release_sha, status, phase, phase_started_at,
                                    deploy_pid, last_heartbeat_at, queue_position,
                                    error_summary, requested_by, request_source,
                                    commit_status, push_status, auto_start,
                                    component, target_env,
                                    requested_at, created_at, updated_at)
            SELECT 'AADS', '$release_sql', 'queued', 'queued_for_deploy', NOW(),
                   $$, NOW(), 1, '$reason_sql', 'deploy.sh_autoheal', 'autoheal_${cause}',
                   'committed', 'pushed', TRUE, 'api', 'production', NOW(), NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM deploy_runs
                WHERE id <> ${DEPLOY_RUN_ID}
                  AND upper(trim(project))='AADS'
                  AND release_sha='$release_sql'
                  AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
                  AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
                  AND ((status='queued' AND phase='queued_for_deploy' AND COALESCE(auto_start, FALSE) = TRUE)
                      OR status IN ('running','verifying','syncing_standby'))
            )
            RETURNING id;
        " | tail -1 | tr -d '[:space:]'
    )"

    if [[ "$run_id" =~ ^[0-9]+$ && "$run_id" != "${DEPLOY_RUN_ID}" ]]; then
        AUTOHEAL_SUCCESSOR_RUN_ID="$run_id"
        AUTOHEAL_SUCCESSOR_KIND="queued"
        autoheal_log "신규 재개 큐 등록: deploy_run_id=${run_id}, release=${AADS_RELEASE_SHA}"
        return 0
    fi

    # NOT EXISTS와 INSERT 사이 경합으로 RETURNING이 비면 별도 snapshot에서
    # active를 먼저, 그 다음 eligible queued를 다시 확인한다.
    run_id="$(
        deploy_db_exec "
            SELECT id FROM deploy_runs
             WHERE id <> ${DEPLOY_RUN_ID}
               AND upper(trim(project))='AADS' AND release_sha='$release_sql'
               AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
               AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
               AND status IN ('running','verifying','syncing_standby')
             ORDER BY id DESC LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$run_id" =~ ^[0-9]+$ && "$run_id" != "${DEPLOY_RUN_ID}" ]]; then
        AUTOHEAL_SUCCESSOR_RUN_ID="$run_id"
        AUTOHEAL_SUCCESSOR_KIND="active"
        autoheal_log "INSERT 경합 후 active successor 확인: deploy_run_id=${run_id}"
        return 0
    fi
    run_id="$(
        deploy_db_exec "
            SELECT id FROM deploy_runs
             WHERE id <> ${DEPLOY_RUN_ID}
               AND upper(trim(project))='AADS' AND release_sha='$release_sql'
               AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
               AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production'
               AND status='queued' AND phase='queued_for_deploy'
               AND COALESCE(auto_start, FALSE)=TRUE
             ORDER BY id ASC LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$run_id" =~ ^[0-9]+$ && "$run_id" != "${DEPLOY_RUN_ID}" ]]; then
        AUTOHEAL_SUCCESSOR_RUN_ID="$run_id"
        AUTOHEAL_SUCCESSOR_KIND="queued"
        autoheal_log "INSERT 경합 후 eligible queued successor 확인: deploy_run_id=${run_id}"
        return 0
    fi
    autoheal_log "❌ 재개 큐와 실행 가능한 후속 run을 확인할 수 없다"
    return 1
}

autoheal_confirm_successor_active() {
    local successor_id="${AUTOHEAL_SUCCESSOR_RUN_ID:-}"
    local release_sql attempts delay i state
    [[ "$successor_id" =~ ^[0-9]+$ && "$successor_id" != "${DEPLOY_RUN_ID:-}" ]] || return 1
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    attempts="${AADS_DEPLOY_AUTOHEAL_CLAIM_POLLS:-15}"
    delay="${AADS_DEPLOY_AUTOHEAL_CLAIM_POLL_SEC:-2}"
    [[ "$attempts" =~ ^[0-9]+$ && "$attempts" -ge 1 && "$attempts" -le 30 ]] || attempts=15
    [[ "$delay" =~ ^[0-9]+$ && "$delay" -le 5 ]] || delay=2
    for ((i=0; i<attempts; i++)); do
        state="$(
            deploy_db_exec "
                SELECT status FROM deploy_runs
                 WHERE id=${successor_id} AND id <> ${DEPLOY_RUN_ID}
                   AND upper(trim(project))='AADS' AND release_sha='$release_sql'
                   AND COALESCE(NULLIF(lower(trim(component)), ''), 'api')='api'
                   AND COALESCE(NULLIF(lower(trim(target_env)), ''), 'production')='production';
            " 2>/dev/null | tail -1 | tr -d '[:space:]'
        )" || return 1
        case "$state" in
            running|verifying|syncing_standby)
                AUTOHEAL_SUCCESSOR_KIND="active"
                return 0 ;;
            queued) ;;
            *) return 1 ;;
        esac
        (( i + 1 < attempts )) && sleep "$delay"
    done
    return 1
}

launch_autoheal_worker() {
    local cause="${1:-unknown}"
    if [[ "${AUTOHEAL_SUCCESSOR_KIND:-}" == "active" ]]; then
        return 0
    fi
    local launcher=""
    if [[ -x "${COMPOSE_DIR}/scripts/start_aads_deploy_queue_worker.sh" ]]; then
        launcher="${COMPOSE_DIR}/scripts/start_aads_deploy_queue_worker.sh"
    elif [[ -x "${STATE_DIR}/scripts/start_aads_deploy_queue_worker.sh" ]]; then
        launcher="${STATE_DIR}/scripts/start_aads_deploy_queue_worker.sh"
    else
        autoheal_log "❌ 큐 워커 런처를 찾을 수 없다 — 재개 불가"
        return 1
    fi
    # 재개 모드는 배포 모드로만 넘긴다.
    # 2026-09-13 09:09 KST #378: MODE=status 로 시작된 호출이 실패하자
    # 그 MODE 가 그대로 재개 워커에 전달되어 code_validation 에서 다시 죽었다.
    local retry_mode="${MODE:-bluegreen}"
    case "$retry_mode" in
        bluegreen|code|reload|build) ;;
        *)
            autoheal_log "재개 모드 보정: '${retry_mode}' 는 배포 모드가 아니다 → bluegreen 으로 대체"
            retry_mode="bluegreen"
            ;;
    esac
    if [[ "$AADS_DEPLOY_AUTOHEAL_DRYRUN" == "1" ]]; then
        autoheal_log "DRYRUN: 워커 기동 생략 (${launcher} ${retry_mode} autoheal_${cause})"
        return 0
    fi
    autoheal_cooldown_stamp "$cause" || return 1
    local launch_out
    if ! launch_out="$(bash "$launcher" "$retry_mode" "autoheal_${cause}" 2>&1)"; then
        autoheal_log "❌ 큐 워커 기동 실패: ${launch_out}"
        return 1
    fi
    autoheal_log "$launch_out"
    # already-running/queue-empty/exit 0은 단독 성공 근거가 아니다. 호출자는
    # 이 함수 뒤 정확한 successor ID의 DB active 전환을 bounded poll로 확인한다.
    return 0
}

# 자가치유 결과를 deploy_runs 에 남긴다.
# 기존에는 escalate 가 로그·audit_control·텔레그램에만 찍혀서,
# 대시보드/DB 만 보는 쪽에서는 "왜 멈췄고 재개는 시도했는지" 를 알 수 없었다.
# (2026-09-13 #376: error_summary 가 "insufficient build disk" 한 줄뿐이었다)
autoheal_record_outcome() {
    local outcome="${1:-}"
    local cause="${2:-unknown}"
    local detail="${3:-}"
    if [[ -z "${DEPLOY_RUN_ID:-}" ]]; then
        autoheal_log "결과 기록 생략: DEPLOY_RUN_ID 없음 (outcome=${outcome}, cause=${cause})"
        return 0
    fi
    if ! deploy_db_available; then
        autoheal_log "결과 기록 생략: DB 불가 (outcome=${outcome}, cause=${cause})"
        return 0
    fi
    local note_sql
    note_sql="$(sql_escape "autoheal ${outcome}: cause=${cause}; remediation=${AUTOHEAL_LAST_REMEDIATION}; ${detail}")"
    deploy_db_exec "
        UPDATE deploy_runs
           SET error_summary = CONCAT_WS(' | ', NULLIF(error_summary, ''), '${note_sql}'),
               updated_at = NOW()
         WHERE id = ${DEPLOY_RUN_ID};
    " >/dev/null 2>&1 || true
    autoheal_log "결과 기록: deploy_runs#${DEPLOY_RUN_ID} ← ${outcome}/${cause}"

    # dirty 게이트/drain 대기에서 멈춘 배포는 "실패" 가 아니라 "경로 변경" 또는
    # "시점 문제" 다. 같은 릴리스가 그대로 재개되고 성공하면 원본 행만
    # blocked 로 남아 대시보드에서 실패한 배포로 읽힌다(SLO 왜곡).
    # 2026-09-16 11:20 KST 실측: 최근 3일 blocked 27건이 전부 dirty_worktree
    # 경로였고 후속 런은 모두 success 였다.
    #
    # 다만 원장을 바로잡는 것은 "실제로 실행 가능한 후속이 확인됐고 워커
    # 기동에 성공한 경우"에만 해야 한다(AI 리뷰 P1, f19c3491 반려 사유).
    # AUTOHEAL_SUCCESSOR_RUN_ID 는 queue_autoheal_retry_request 가 워커의
    # 실제 claim 조건(auto_start=TRUE AND phase='queued_for_deploy', 또는
    # running/verifying/syncing_standby)과 동일한 조건으로 확인한 후속
    # run id 다 — 이 값이 없으면(빈 문자열) UPDATE 자체가 걸리지 않는다.
    # WHERE 절의 EXISTS 서브쿼리도 같은 조건으로 다시 확인해, 큐 등록과
    # UPDATE 사이에 후속 행 상태가 바뀌었을 가능성까지 막는다(상태 세탁 금지).
    if [[ "$outcome" == "retry_launched" && "${AUTOHEAL_SUCCESSOR_RUN_ID:-}" =~ ^[0-9]+$ \
          && ( "$cause" == "target_drain_busy" \
               || ( "$cause" == "dirty_worktree" && "$AUTOHEAL_LAST_REMEDIATION" == "route_clean_worktree" ) ) ]]; then
        local successor_id="$AUTOHEAL_SUCCESSOR_RUN_ID" release_sql phase_sql original_phase
        release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
        phase_sql="superseded_by_autoheal_reroute"
        original_phase="preflight"
        if [[ "$cause" == "target_drain_busy" ]]; then
            phase_sql="superseded_by_autoheal_drain_retry"
            original_phase="target_slot_drain"
        fi
        local superseded_id
        superseded_id="$(deploy_db_exec "
            UPDATE deploy_runs
               SET status = 'superseded',
                   phase = '${phase_sql}',
                   error_summary = CONCAT_WS(' | ', NULLIF(error_summary, ''),
                       'autoheal successor deploy_run_id=${successor_id}'),
                   updated_at = NOW()
             WHERE id = ${DEPLOY_RUN_ID}
               AND status = 'blocked'
               AND phase = '${original_phase}'
               AND EXISTS (
                   SELECT 1 FROM deploy_runs successor
                    WHERE successor.id = ${successor_id}
                      AND successor.id <> ${DEPLOY_RUN_ID}
                      AND upper(trim(successor.project)) = 'AADS'
                      AND successor.release_sha = '${release_sql}'
                      AND COALESCE(NULLIF(lower(trim(successor.component)), ''), 'api')='api'
                      AND COALESCE(NULLIF(lower(trim(successor.target_env)), ''), 'production')='production'
                      AND successor.status IN ('running','verifying','syncing_standby')
               )
            RETURNING id;
        " 2>/dev/null | tail -1 | tr -d '[:space:]')"
        if [[ "$superseded_id" != "${DEPLOY_RUN_ID}" ]]; then
            autoheal_log "❌ 원장 보정 중단: successor #${successor_id} active 재검증 실패"
            return 1
        fi
        autoheal_log "원장 보정: deploy_runs#${DEPLOY_RUN_ID} blocked → superseded (후속 run=#${successor_id}, ${cause})"
    fi
    return 0
}

autoheal_escalate() {
    local cause="${1:-unknown}"
    local reason="${2:-}"
    autoheal_log "🚨 자동 복구 불가 — CEO 에스컬레이션: cause=${cause}, reason=${reason}"
    autoheal_record_outcome "escalated" "$cause" "reason=${reason}"
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

    local phase err cause policy attempts budget
    phase="${DEPLOY_CURRENT_PHASE:-unknown}"
    err="${DEPLOY_LAST_FAIL_ERROR:-}"
    if [[ -z "${err//[[:space:]]/}" && -n "${DEPLOY_RUN_ID:-}" ]] && deploy_db_available; then
        err="$(deploy_db_exec "SELECT COALESCE(error_summary,'') FROM deploy_runs WHERE id=${DEPLOY_RUN_ID};" | tail -1)"
    fi
    cause="$(classify_deploy_failure "$phase" "$err")"
    # 2026-09-13 #368/#370 실측: 릴리스 worktree 가 사라진 배포는 compose 파일을 못 열고
    # 매번 unexpected_exit 로 떨어져 수동 개입이 필요했다. 소스 디렉터리 실재 여부는
    # 에러 문자열보다 확실한 근거이므로 일반 분류를 이 판정으로 덮어쓴다.
    # container_recreate_fail 도 포함한다. 2026-09-13 오전 분류 세분화(D6)로
    # "unexpected error exit=... docker compose ..." 가 unexpected_exit 대신
    # container_recreate_fail 로 잡히면서, 소스가 사라진 경우에도 이 보정이
    # 걸리지 않게 됐다. 컨테이너 재생성을 재시도해도 compose 파일이 없으면
    # 같은 실패가 반복될 뿐이다 — 워크트리 복구가 먼저다.
    # 기존 테스트 2건이 이 회귀를 잡고 있었으나 pre-commit 게이트가 이 파일을
    # 돌리지 않아 드러나지 않았다.
    if [[ "$cause" == "unexpected_exit" || "$cause" == "other" || "$cause" == "container_recreate_fail" ]] \
       && ! autoheal_source_dir_ok; then
        autoheal_log "릴리스 소스 부재 확인: ${COMPOSE_DIR:-unknown} — 분류를 source_dir_missing 으로 보정"
        cause="source_dir_missing"
    fi
    if ! autoheal_key_valid "${AADS_RELEASE_SHA:-unknown}" "$cause"; then
        autoheal_escalate "$cause" "유효하지 않은 릴리스 SHA 또는 원인 키(release=${AADS_RELEASE_SHA:-unknown})"
        return 0
    fi
    policy="$(autoheal_policy "$cause" "$phase")"
    attempts="$(autoheal_attempt_count "$cause")"
    budget="$(autoheal_max_attempts "$cause")"
    autoheal_log "실패 감지: rc=${rc}, phase=${phase}, cause=${cause}, policy=${policy}, attempts=${attempts}/${budget}"
    audit_control "autoheal" "deploy_runs:${DEPLOY_RUN_ID:-none}" "classified" \
        "cause=${cause}; policy=${policy}; phase=${phase}; attempts=${attempts}/${budget}" || true

    if [[ "$policy" != "retry" ]]; then
        autoheal_escalate "$cause" "정책상 자동 재시도 대상이 아님 (phase=${phase})"
        return 0
    fi
    if (( attempts >= budget )); then
        autoheal_escalate "$cause" "재시도 예산 소진 (${attempts}/${budget})"
        return 0
    fi
    if ! autoheal_cooldown_ok "$cause"; then
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
    if launch_autoheal_worker "$cause" && autoheal_confirm_successor_active; then
        if ! autoheal_record_outcome "retry_launched" "$cause" "release=${AADS_RELEASE_SHA:-unknown}"; then
            autoheal_escalate "$cause" "successor active 재검증 또는 원장 전환 실패"
            return 0
        fi
        autoheal_log "✅ 자가치유 successor 인계 확인: cause=${cause}, successor=${AUTOHEAL_SUCCESSOR_RUN_ID}"
        audit_control "autoheal" "deploy_runs:${DEPLOY_RUN_ID:-none}" "retry_launched" \
            "cause=${cause}; remediation=${AUTOHEAL_LAST_REMEDIATION}; release=${AADS_RELEASE_SHA:-unknown}" || true
        if declare -F notify >/dev/null 2>&1; then
            notify "🔄 배포 자동복구 재개: ${cause} → ${AUTOHEAL_LAST_REMEDIATION} (release=${AADS_RELEASE_SHA:-unknown})" || true
        fi
    else
        autoheal_escalate "$cause" "재개 워커 기동 실패 또는 successor active 확인 실패"
    fi
    return 0
}
