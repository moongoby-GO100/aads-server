#!/bin/bash
# Meta-Watchdog (L3) — L2 감시자 생존 확인 및 재시작
# 서버 68 (68.183.183.11), cron */10 (10분 주기)
# AADS-131: L3 조치
# v1.1: 중복코드제거, 인증에러제외, graceful reload, 10분주기

LOG="/root/aads/logs/meta_watchdog.log"
mkdir -p "$(dirname "$LOG")"
TG_TOKEN="${TG_BOT_TOKEN}"
CHAT_ID="${TG_CHAT_ID}"
ALERT_COUNT_FILE="/root/aads/meta_watchdog_alert_count"
MAX_ALERTS=3  # 동일 이슈 최대 3회 알림

# .env에서 TG 토큰 로딩
source /root/.genspark/.env.oauth 2>/dev/null

log_msg() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"; }

send_alert() {
    local component="$1" message="$2"
    local count_key="${component}_count"
    local current
    current=$(grep "^${count_key}=" "$ALERT_COUNT_FILE" 2>/dev/null | cut -d= -f2)
    current=${current:-0}

    if [ "$current" -lt "$MAX_ALERTS" ] 2>/dev/null; then
        curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
          -d chat_id="${CHAT_ID}" \
          -d text="🚨 [META-WATCHDOG] ${message}" > /dev/null 2>&1
        sed -i "/${count_key}=/d" "$ALERT_COUNT_FILE" 2>/dev/null
        echo "${count_key}=$((current+1))" >> "$ALERT_COUNT_FILE"
    fi
}

reset_alert() {
    local component="$1"
    sed -i "/${component}_count=/d" "$ALERT_COUNT_FILE" 2>/dev/null
}

check_and_recover() {
    local name="$1" check_cmd="$2" restart_cmd="$3"

    if eval "$check_cmd" > /dev/null 2>&1; then
        reset_alert "$name"
        return 0
    fi

    log_msg "WARNING: $name DOWN, attempting restart..."
    eval "$restart_cmd" > /dev/null 2>&1
    sleep 5

    if eval "$check_cmd" > /dev/null 2>&1; then
        log_msg "OK: $name recovered"
        reset_alert "$name"
        send_alert "$name" "✅ ${name} 자동 복구 성공"
        return 0
    else
        log_msg "CRITICAL: $name restart FAILED"
        send_alert "$name" "❌ ${name} 재시작 실패. 수동 조치 필요."
        return 1
    fi
}

# --- L2 감시 대상 ---

# 0. session_watchdog (로컬 서버 68) — AADS-140
check_and_recover "session_watchdog" \
    "pgrep -f session_watchdog.sh > /dev/null" \
    "nohup /root/aads/scripts/session_watchdog.sh >> /root/aads/logs/session_watchdog.log 2>&1 &"

# 1. watchdog_daemon (서버 114)
check_and_recover "watchdog_114" \
    "ssh -o ConnectTimeout=5 -p 7916 root@114.207.244.86 'systemctl is-active watchdog_daemon --quiet'" \
    "ssh -o ConnectTimeout=5 -p 7916 root@114.207.244.86 'systemctl restart watchdog_daemon'"

# 2. aads-bridge (로컬 서버 68)
check_and_recover "aads_bridge" \
    "systemctl is-active aads-bridge --quiet" \
    "systemctl restart aads-bridge"

# 3. pipeline_monitor cron (로컬)
check_and_recover "pipeline_monitor_cron" \
    "crontab -l 2>/dev/null | grep -q pipeline_monitor" \
    "(crontab -l 2>/dev/null; echo '*/2 * * * * /root/.genspark/pipeline_monitor.sh >> /var/log/pipeline_monitor.log 2>&1') | sort -u | crontab -"

# 4. auto_trigger (로컬 서버 68)
check_and_recover "auto_trigger_68" \
    "pgrep -f auto_trigger.sh > /dev/null" \
    "nohup /root/.genspark/auto_trigger.sh >> /var/log/auto_trigger.log 2>&1 &"

# 5. auto_trigger (서버 114)
check_and_recover "auto_trigger_114" \
    "ssh -o ConnectTimeout=5 -p 7916 root@114.207.244.86 'pgrep -f auto_trigger.sh > /dev/null'" \
    "ssh -o ConnectTimeout=5 -p 7916 root@114.207.244.86 'nohup /root/.genspark/auto_trigger.sh >> /var/log/auto_trigger.log 2>&1 &'"

# 6. health-check API (v1.1: 인증에러 제외, graceful reload, 쿨다운 600초)
HC_LOCKFILE="/tmp/meta_watchdog_hc_restart.lock"
HC_COOLDOWN=600

HC_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    "http://localhost:8100/api/v1/health")

if [ "$HC_CODE" != "200" ]; then
    # v1.1: 401/403/422 인증/권한 에러는 재시작으로 해결 불가 — 스킵
    if [ "$HC_CODE" = "401" ] || [ "$HC_CODE" = "403" ] || [ "$HC_CODE" = "422" ]; then
        log_msg "INFO: health-check HTTP $HC_CODE (auth/validation error) — restart skipped, not a crash"
        send_alert "healthcheck_auth" "⚠️ health-check HTTP $HC_CODE (인증/권한 에러). 재시작 불필요 — 설정 확인 필요."
    else
        HC_SKIP=0
        if [ -f "$HC_LOCKFILE" ]; then
            LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$HC_LOCKFILE" 2>/dev/null || echo 0) ))
            if [ "$LOCK_AGE" -lt "$HC_COOLDOWN" ]; then
                log_msg "INFO: health-check HTTP $HC_CODE but cooldown active (${LOCK_AGE}s/${HC_COOLDOWN}s) — skip restart"
                HC_SKIP=1
            else
                rm -f "$HC_LOCKFILE"
            fi
        fi

        if [ "$HC_SKIP" -eq 0 ]; then
            log_msg "WARNING: health-check API HTTP $HC_CODE — 15초 후 재확인"
            sleep 15
            HC_CODE_RETRY=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
                "http://localhost:8100/api/v1/health")
            if [ "$HC_CODE_RETRY" != "200" ]; then
                log_msg "CRITICAL: health-check API 연속 실패 (${HC_CODE} → ${HC_CODE_RETRY}). graceful reload 시도"
                touch "$HC_LOCKFILE"
                # v1.1: deploy.sh code 대신 docker exec graceful reload (SSE 스트림 보호)
                docker exec aads-server supervisorctl signal HUP aads-api 2>/dev/null
                sleep 15
                HC_CODE2=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
                    "http://localhost:8100/api/v1/health")
                if [ "$HC_CODE2" != "200" ]; then
                    send_alert "healthcheck_api" "health-check API graceful reload 실패 (HTTP $HC_CODE2). Docker 확인 필요."
                    log_msg "CRITICAL: graceful reload 후에도 실패. 수동 조치 필요."
                else
                    reset_alert "healthcheck_api"
                    log_msg "OK: health-check API recovered via graceful reload"
                fi
            else
                reset_alert "healthcheck_api"
                log_msg "OK: health-check API 일시적 실패 후 자동 복구 (${HC_CODE} → ${HC_CODE_RETRY})"
            fi
        fi
    fi
fi

# 7. 장기 running 작업 긴급 정리 (40분+ 정체)
STALLED_RUNNING=$(curl -s --max-time 10 "https://aads.newtalk.kr/api/v1/ops/stalled" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(len([t for t in d.get('stalled',[]) if t['status']=='running' and t.get('stalled_seconds',0)>2400]))" 2>/dev/null)
if [ "${STALLED_RUNNING:-0}" -gt 0 ]; then
    log_msg "EMERGENCY: $STALLED_RUNNING tasks running >40min, triggering emergency slot clear"
    send_alert "slot_emergency" "⚠️ ${STALLED_RUNNING}건 40분+ running 정체. 긴급 슬롯 해제 시도 중."
    ssh -o ConnectTimeout=5 -p 7916 root@114.207.244.86 \
      'OLDEST=$(pgrep -fo "claude" 2>/dev/null); [ -n "$OLDEST" ] && kill -TERM "$OLDEST" && echo "Killed $OLDEST"' 2>/dev/null
fi

log_msg "Meta-watchdog cycle complete (HC=$HC_CODE, stalled_running=${STALLED_RUNNING:-0})"
