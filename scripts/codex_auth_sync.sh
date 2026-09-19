#!/bin/bash
# Codex CLI 인증 자동 유지 스크립트
# 역할: (1) access_token 만료 감시 (2) 프리웜으로 자동 갱신 (3) 마스터→전서버 동기화
# 크론: */30 * * * * /root/aads/aads-server/scripts/codex_auth_sync.sh
# 마스터 서버: contabo116 (AADS) — 여기서 갱신 후 contabo14, cafe24_114로 배포
#
# 2026-09-19: 같은 스크립트를 jinah244 에서도 쓴다. 244 는 device-auth 로 **독립된
# 토큰 쌍**을 갖는 노드라 자격증명을 주고받지 않는다 — 노드별 차이는 코드가 아니라
# /etc/codex-auth-sync.conf 로 준다(SYNC_ENABLED=0, NODE_LABEL=jinah244).
# 서버별 스크립트 사본을 따로 만들면 한쪽이 반드시 낡는다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG="/var/log/codex_auth_sync.log"
AUTH_FILE="/root/.codex/auth.json"
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || echo /usr/bin/codex)}"
WARN_DAYS=3
# 텔레그램 자격증명은 .env 를 읽은 **뒤**에 정한다 (아래 load_telegram_creds).
# 여기서 먼저 읽던 것이 이 스크립트의 알림이 한 번도 안 나간 원인이었다.

# host + port 분리: scp는 -P, ssh는 -p 로 포트를 각각 받아야 한다.
HOST_211="5.104.86.14";     PORT_211="22"
HOST_114="114.207.244.86";  PORT_114="7916"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no"

[[ -f /root/aads/.env ]] && source /root/aads/.env
# 노드별 설정(선택). 시크릿은 넣지 않는다 — 텔레그램 토큰은 .env 가 원본이다.
[[ -f /etc/codex-auth-sync.conf ]] && source /etc/codex-auth-sync.conf

# 텔레그램 자격증명은 .env 를 통째로 source 하지 않고 필요한 두 줄만 뽑는다.
# (.env 에는 따옴표 없는 공백·특수문자 값이 있어 set -e 아래에서 source 하면 죽는다.)
#
# 2026-09-19: 이 스크립트의 알림은 만들어진 이래 **한 번도 나가지 않았다** — 두 가지가
# 겹쳤다. ① 토큰을 /root/aads/.env 에서 찾는데 실제 값은 aads-server/.env 에 있고,
# ② TELEGRAM_TOKEN 을 source 보다 먼저 읽어 항상 빈 문자열이었다. "자동 갱신 실패 시
# 알려준다" 는 안전망 자체가 없었던 셈이라, 계정이 조용히 죽어도 아무도 몰랐다.
load_telegram_creds() {
    local f line val
    for f in /root/aads/aads-server/.env /root/aads/.env; do
        [[ -f "$f" ]] || continue
        while IFS= read -r line; do
            case "$line" in
                TELEGRAM_BOT_TOKEN=*) val="${line#TELEGRAM_BOT_TOKEN=}" ;;
                TELEGRAM_CHAT_ID=*)   val="${line#TELEGRAM_CHAT_ID=}" ;;
                *) continue ;;
            esac
            val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
            # set -e 아래에서 `[[ ... ]] && VAR=..` 는 조건이 거짓일 때 스크립트를
            # 끝내 버린다. if 문으로 써야 안전하다.
            case "$line" in
                TELEGRAM_BOT_TOKEN=*) if [[ -n "$val" ]]; then TELEGRAM_TOKEN="$val"; fi ;;
                TELEGRAM_CHAT_ID=*)   if [[ -n "$val" ]]; then TELEGRAM_CHAT_ID="$val"; fi ;;
            esac
        done < "$f"
    done
}
TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
load_telegram_creds
NODE_LABEL="${NODE_LABEL:-contabo116}"
# 1 = 갱신한 자격증명을 원격으로 배포하는 마스터. 0 = 자기 토큰만 관리하는 독립 노드.
SYNC_ENABLED="${SYNC_ENABLED:-1}"

mkdir -p "$(dirname "$LOG")"

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST')" "$*" >> "$LOG"
}

send_telegram() {
    local msg="$1"
    if [[ -n "$TELEGRAM_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

get_token_remaining_days() {
    local auth_file="${1:-$AUTH_FILE}"
    python3 -c "
import json, base64, datetime, sys
try:
    d = json.load(open('${auth_file}'))
    at = d['tokens']['access_token']
    parts = at.split('.')
    pad = parts[1] + '=' * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(pad))
    exp = payload.get('exp', 0)
    # utcnow() 는 tz 정보가 없어 .timestamp() 가 그것을 **로컬 시각**으로 해석한다.
    # KST 서버(jinah244)에서는 잔여일이 9시간 부풀고, CEST 서버에서는 2시간 줄었다.
    # 부풀면 선제 갱신이 그만큼 늦게 도는 만큼 그대로 두면 안 된다(2026-09-19).
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    remaining = (exp - now) / 86400
    print(f'{remaining:.2f}')
except Exception as e:
    print('-1')
    sys.exit(1)
" 2>/dev/null
}

prewarm_codex() {
    log "PREWARM: codex exec 실행으로 토큰 자동 갱신 시도"
    local out rc
    out=$(timeout 60 "$CODEX_BIN" exec --sandbox read-only "echo codex-auth-prewarm-ok" 2>&1)
    rc=$?
    if [[ $rc -eq 0 ]] && ! grep -qiE 'usage limit|rate limit|quota' <<< "$out"; then
        log "PREWARM: 성공 — 토큰 자동 갱신됨"
        return 0
    fi
    # 사용량 한도는 인증 문제가 아니다. 재인증을 요구하면 오진이 된다
    # (2026-09-12: 폐기된 refresh token이 한도 소진을 가리는 사례가 실제 발생).
    if grep -qiE 'usage limit|rate limit|quota' <<< "$out"; then
        log "PREWARM: 사용량 한도 — 인증은 유효, 재인증 불필요"
        return 2
    fi
    log "PREWARM: 실패 (exit=$rc) — 수동 재인증 필요: ${out:0:200}"
    return 1
}

sync_to_remote() {
    local label="$1" host="$2" port="${3:-22}"

    # 독립 노드는 자격증명을 내보내지 않는다. 같은 refresh_token 사본이 두 서버에
    # 생기는 순간 회전 충돌(401 refresh_token_reused)이 되돌아온다.
    if [[ "$SYNC_ENABLED" != "1" ]]; then
        log "SYNC: 비활성 노드($NODE_LABEL) — $label 배포 건너뜀"
        return 0
    fi

    # 원격 백업 후 복사. 성패는 scp 자체의 종료코드로 판정한다
    # (과거에는 뒤따르는 chmod의 $?를 보아 실패가 성공으로 기록되었다).
    ssh $SSH_OPTS -p "$port" "$host" "cp $AUTH_FILE ${AUTH_FILE}.bak 2>/dev/null; true" 2>/dev/null
    if scp $SSH_OPTS -P "$port" "$AUTH_FILE" "${host}:$AUTH_FILE" >/dev/null 2>&1; then
        ssh $SSH_OPTS -p "$port" "$host" "chmod 600 $AUTH_FILE" 2>/dev/null
        log "SYNC: $label ($host:$port) ← 동기화 성공"
    else
        log "SYNC: $label ($host:$port) ← 동기화 실패 (scp)"
        send_telegram "🔴 [Codex Auth] ${label} 동기화 실패 — 수동 확인 필요"
    fi
}

# ── 메인 로직 ──

remaining=$(get_token_remaining_days)

if [[ "$remaining" == "-1" ]]; then
    log "ERROR: auth.json 파싱 실패"
    send_telegram "🔴 [Codex Auth] ${NODE_LABEL} auth.json 파싱 실패"
    exit 1
fi

remaining_int=${remaining%%.*}

log "CHECK: access_token 잔여 ${remaining}일"

# OAuth refresh_token 으로 직접 갱신한다 — 재로그인 없이 끝내는 유일한 경로.
#
# 2026-09-19: 이 스크립트를 만든 09-12 이후 PREWARM 이 **한 번도 발동하지 않았다**
# (로그 전수 0건). 갱신을 codex CLI 실행에 맡겨 뒀는데 CLI 는 토큰이 실제로
# 만료돼야 갱신한다. 그 사이 같은 refresh_token 사본을 가진 다른 서버가 먼저
# 갱신하면 회전이 소진돼 나머지는 401 refresh_token_reused 로 영구 무효가 된다
# — CODEX_OAUTH_JINAH 가 정확히 그렇게 죽었고 재로그인 외에 복구가 없었다.
#
# 그래서 마스터(contabo116)가 **만료 3일 전에 선제 갱신하고 즉시 배포**한다.
# 원격이 스스로 갱신할 일이 없어지면 회전 충돌도 사라진다.
oauth_refresh_file() {
    local file="${1:-$AUTH_FILE}" out
    out=$(CODEX_AUTH_FILE="$file" timeout 60 python3 \
          "${SCRIPT_DIR}/codex_token_refresh.py" --apply 2>&1) || true
    if grep -q "AUTH_FILE_UPDATED" <<< "$out"; then
        log "OAUTH_REFRESH: $file 갱신 성공 — $(grep -o '새 access_token 만료: .*' <<< "$out" | head -1)"
        return 0
    fi
    log "OAUTH_REFRESH: $file 갱신 실패 — $(tr '\n' ' ' <<< "$out" | cut -c1-200)"
    return 1
}

# 1. 만료 3일 이내 → refresh_token 으로 선제 갱신 (재로그인 불필요)
if (( remaining_int < WARN_DAYS )); then
    log "ALERT: 토큰 잔여 ${remaining}일 — OAuth 선제 갱신 시도"

    refresh_ok=0
    oauth_refresh_file "$AUTH_FILE" && refresh_ok=1

    if [[ $refresh_ok -eq 0 ]]; then
        # refresh_token 이 무효면 프리웜으로 한 번 더 — CLI 가 다른 경로로
        # 갱신해 둔 토큰이 파일에 있을 수 있다.
        # set -e 아래에서 `prewarm_codex; rc=$?` 는 실패 시 rc 를 읽기 전에 스크립트가
        # 죽는다 — 뒤의 계정 홈 감시까지 통째로 건너뛴다(2026-09-19).
        prewarm_rc=0; prewarm_codex || prewarm_rc=$?
        if [[ $prewarm_rc -eq 0 ]]; then
            refresh_ok=1
        elif [[ $prewarm_rc -eq 2 ]]; then
            send_telegram "🟡 [Codex Auth] ${NODE_LABEL} 사용량 한도로 갱신 보류 — 인증은 정상, 조치 불필요"
        else
            send_telegram "🔴 [Codex Auth] ${NODE_LABEL} 토큰 자동 갱신 실패 — CEO 수동 인증 필요: codex login --device-auth"
        fi
    fi

    if [[ $refresh_ok -eq 1 ]]; then
        new_remaining=$(get_token_remaining_days)
        log "RENEWED: 갱신 후 잔여 ${new_remaining}일"
        send_telegram "✅ [Codex Auth] ${NODE_LABEL} 토큰 자동 갱신 성공 — ${new_remaining}일 남음 (재로그인 불필요)"
        # 회전된 토큰을 즉시 배포한다. 늦으면 원격이 옛 토큰으로 갱신을 시도해
        # 회전이 어긋난다 — 이 즉시성이 이 설계의 핵심이다.
        sync_to_remote "contabo14" "$HOST_211" "$PORT_211"
        sync_to_remote "cafe24_114" "$HOST_114" "$PORT_114"
    fi

# 3. 3일 이상 → 정상, 동기화만 확인
else
    # 매일 04:30(서버 로컬시각)에만 전서버 동기화 (업데이트 스크립트 직후)
    HOUR=$(date +%H)
    MIN=$(date +%M)
    if [[ "$HOUR" == "04" && "$MIN" -ge 25 && "$MIN" -le 35 ]]; then
        log "DAILY_SYNC: 정기 전서버 동기화"
        sync_to_remote "contabo14" "$HOST_211" "$PORT_211"
        sync_to_remote "cafe24_114" "$HOST_114" "$PORT_114"
    fi
fi

# ── 계정 홈 감시 (2026-09-19 추가) ──
#
# 지금까지 이 스크립트는 /root/.codex/auth.json 하나만 봤다. 계정 홈
# (/root/.codex-accounts/<KEY>/auth.json)은 감시 대상 밖이었고, 갱신은
# "codex CLI 가 그 홈으로 실행되면 알아서" 하는 사용 종속 방식이었다.
# 그래서 CODEX_OAUTH_JINAH 토큰이 2026-09-19 04:41 KST 에 만료되는 동안
# 로그에는 MAIN 의 "잔여 3.26일" 만 30분마다 찍혔다. 그 구멍을 막는다.
check_account_homes() {
    local root="${CODEX_ACCOUNTS_ROOT:-/root/.codex-accounts}"
    [[ -d "$root" ]] || return 0
    local home name file rem rem_int prc main_real file_real
    main_real="$(readlink -f "$AUTH_FILE" 2>/dev/null || echo "$AUTH_FILE")"
    for home in "$root"/*/; do
        file="${home}auth.json"
        name="$(basename "$home")"
        # 메인 파일과 같은 실체(심볼릭 링크 어느 방향이든)면 위 메인 로직이 이미 본다.
        # 같은 파일을 두 번 갱신하면 refresh_token 회전이 어긋나 스스로 죽는다 —
        # 링크 방향은 노드마다 다르므로 realpath 로 비교한다(2026-09-19).
        file_real="$(readlink -f "$file" 2>/dev/null || echo "$file")"
        if [[ "$file_real" == "$main_real" ]]; then
            log "ACCOUNT $name: 메인 auth.json 과 동일 실체 — 중복 갱신 방지로 건너뜀"
            continue
        fi
        if [[ ! -f "$file" ]]; then
            log "ACCOUNT $name: auth.json 없음 — 대화형 재로그인 필요"
            send_telegram "🔴 [Codex Auth] ${NODE_LABEL}/${name} 인증 파일 없음 — CODEX_HOME=${home%/} codex login --device-auth"
            continue
        fi
        rem="$(get_token_remaining_days "$file")" || rem="-1"
        log "ACCOUNT $name: access_token 잔여 ${rem}일"
        if [[ "$rem" == "-1" ]]; then
            send_telegram "🔴 [Codex Auth] ${NODE_LABEL}/${name} auth.json 파싱 실패"
            continue
        fi
        rem_int=${rem%%.*}
        if (( rem_int < WARN_DAYS )); then
            log "ACCOUNT $name: 만료 ${WARN_DAYS}일 이내 — OAuth 선제 갱신 시도"
            # 계정 홈도 마스터와 같은 방식으로 refresh_token 을 직접 쓴다.
            # CLI 실행(프리웜)에 기대면 계정이 안 도는 동안 조용히 만료한다.
            if oauth_refresh_file "$file"; then
                log "ACCOUNT $name: 갱신 성공 — 잔여 $(get_token_remaining_days "$file")일"
                continue
            fi
            prc=0
            CODEX_HOME="${home%/}" prewarm_codex || prc=$?
            if [[ $prc -eq 0 ]]; then
                log "ACCOUNT $name: 갱신 성공 — 잔여 $(get_token_remaining_days "$file")일"
            elif [[ $prc -eq 2 ]]; then
                log "ACCOUNT $name: 사용량 한도 — 인증은 유효, 조치 불필요"
            else
                log "ACCOUNT $name: 자동 갱신 실패 — refresh_token 이 무효일 수 있다"
                send_telegram "🔴 [Codex Auth] ${NODE_LABEL}/${name} 자동 갱신 실패 — CODEX_HOME=${home%/} codex login --device-auth 필요"
            fi
        fi
    done
}

check_account_homes || log "ACCOUNT_SCAN: 예외 발생 — 건너뜀"

exit 0
