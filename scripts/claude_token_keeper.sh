#!/usr/bin/env bash
# Claude OAuth 토큰 만료 전 갱신 — 안전망.
#
# 릴레이는 컨테이너 모드에서 DB 의 정적 access token 을 주입한다. 그래서 CLI 가
# 스스로 갱신할 기회가 없고, 토큰이 만료되면(수명 8시간) 릴레이가 401 을 반환해
# 채팅이 통째로 멈춘다. 2026-09-12 에 21:40 과 23:49 두 번 이 일이 났다.
#
# 근본 해결은 릴레이가 슬롯 자격증명을 직접 쓰게 하는 것이고 그 구현(a49eee32)이
# 이미 있으나 아직 배포되지 않았다. 이 스크립트는 그때까지의 안전망이며, 배포
# 이후에도 무해하다 — 이미 갱신된 토큰이면 DB 갱신을 건너뛴다.
#
# 슬롯 HOME 으로 CLI 를 한 번 돌리면 CLI 가 refreshToken 으로 스스로 갱신한다.
# 그 결과를 DB 에 동기화해 릴레이가 새 토큰을 쓰게 한다.
set -uo pipefail

SLOT_ROOT="${CLAUDE_RELAY_SLOT_HOME_ROOT:-/root/.claude-relay-slots}"
CLAUDE_BIN="${CLAUDE_REAL_BIN:-/usr/bin/claude}"
STATE_DIR="/root/aads/aads-server"
# 만료까지 이 시간 미만이면 갱신한다.
# CLI 는 아직 여유가 있는 토큰을 갱신하지 않는다(실측: 8시간 남은 토큰은 그대로).
# 만료됐거나 임박했을 때만 refreshToken 을 쓴다. 그래서 임계값을 크게 잡으면
# 매번 "갱신 안 됨" 경고만 나온다. 크론 주기(10분)보다 넉넉하되 짧게 둔다.
RENEW_BEFORE_MIN="${CLAUDE_TOKEN_RENEW_BEFORE_MIN:-45}"

# 겹쳐 돌지 않게 잠근다. 임박 재시도가 들어가면서 한 번 실행이 길어졌고,
# 크론 주기를 넘으면 두 실행이 **같은 자격증명 파일을 동시에** 건드린다.
# 2026-09-14 사고가 그 파일이 망가진 것이었다.
_KEEPER_LOCK="/tmp/aads-claude-token-keeper.lock"
exec 9>"$_KEEPER_LOCK"
if ! flock -n 9; then
    echo "$(date '+%F %T') 이전 실행이 아직 돌고 있다 — 건너뜀" >&2
    exit 0
fi

# 만료가 이 시간 안으로 들어오면, CLI 가 "아직 아니다" 라고 거부해도
# 같은 실행 안에서 잠깐 기다렸다 다시 시도한다.
#
# 2026-09-14 23:40 사고의 진짜 원인이 여기다. 임계값을 올리는 것으로는
# 안 풀린다 — 23:30 에 이미 시도했고 **CLI 가 거부했다.**
#
#     23:30:08  slot2: 만료까지 10분 (<= 30) — 갱신     스크립트는 시도
#     23:30:14    아직 갱신 시점 아님 (10분 남음)       CLI 가 거부
#     23:40:16    ⚠️ 갱신 실패                          이미 만료
#
# 크론이 10분 주기라 **거부와 만료 사이에 기회가 한 번도 없었다.**
# CLI 는 만료가 더 임박해야 갱신하므로, 그 순간을 놓치지 않으려면
# 짧은 간격으로 몇 번 더 두드려야 한다.
CLOSE_RETRY_MIN="${CLAUDE_TOKEN_CLOSE_RETRY_MIN:-15}"
# 재시도 횟수·간격은 **크론 주기(10분) 안에 끝나야 한다.** 겹쳐 돌면 두
# 실행이 같은 자격증명 파일을 동시에 건드린다 — 이번 사고가 바로 그
# 파일이 망가진 것이었다.
#
#   슬롯당 최악 = TIMES × (SLEEP + ping 120초)
#   2 × (45 + 120) = 330초, 슬롯 2개면 11분… 그래도 빠듯하므로 잠금을 건다.
CLOSE_RETRY_TIMES="${CLAUDE_TOKEN_CLOSE_RETRY_TIMES:-2}"
CLOSE_RETRY_SLEEP="${CLAUDE_TOKEN_CLOSE_RETRY_SLEEP:-45}"
# 갱신 시도 후에도 이 시간 미만으로 남아 있으면 진짜 실패다(refreshToken 만료 등).
FAIL_BELOW_MIN="${CLAUDE_TOKEN_FAIL_BELOW_MIN:-10}"

log() { echo "$(date '+%F %T') $*"; }

key_for_slot() {
    case "$1" in
        1) printf '%s' "ANTHROPIC_AUTH_TOKEN" ;;
        2|3|4) printf 'ANTHROPIC_AUTH_TOKEN_%s' "$1" ;;
        *) return 1 ;;
    esac
}

# accessToken 또는 refreshToken 이 비었는지. 갱신이 파일을 망가뜨렸는지
# 판단하는 유일한 기준이다 — 만료 시각만 보면 "만료됐다" 와 "지워졌다" 가
# 구분되지 않는다.
_token_field_empty() {
    python3 - "$1" <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    o = d.get("claudeAiOauth") or d
    sys.exit(0 if not (o.get("accessToken") and o.get("refreshToken")) else 1)
except Exception:
    sys.exit(0)
PYEOF
}

remaining_min() {
    python3 -c "
import json, sys, time
try:
    d = json.load(open('$1'))
except Exception:
    print(-1); sys.exit()
o = d.get('claudeAiOauth', d)
exp = o.get('expiresAt', 0)
if not exp:
    # expiresAt=0 은 두 가지다 — 읽지 못한 파일(-1)과 갱신이 토큰을 지워
    # 버린 파일(-2). 2026-09-21 19:00 KST 사고는 이 둘을 -1 하나로 뭉갠
    # 탓에 빈 파일이 '남은 수명이 늘었다'로 오인돼 확정됐다.
    print(-2 if not (o.get('accessToken') and o.get('refreshToken')) else -1)
    sys.exit()
print(int((exp/1000 - time.time()) / 60))
" 2>/dev/null || echo -1
}

resync_to_db() {
    local slot="$1" key="$2" cred="$3" container expires_iso
    # 토큰은 불투명 문자열이라 앱이 만료를 읽을 수 없다. 자격증명 파일에만 있으므로
    # 여기서 함께 넘겨 DB 에 남긴다. 러너가 긴 작업 전에 남은 수명을 볼 수 있게 된다.
    expires_iso="$(python3 -c "
import json, datetime
d = json.load(open('$cred'))
o = d.get('claudeAiOauth', d)
exp = o.get('expiresAt', 0)
print(datetime.datetime.fromtimestamp(exp/1000, datetime.timezone.utc).isoformat() if exp else '')
" 2>/dev/null || echo "")"
    container="$(cat "${STATE_DIR}/.active_container" 2>/dev/null || echo aads-server)"
    docker inspect "$container" >/dev/null 2>&1 || { log "  컨테이너 없음: $container"; return 1; }

    local tmp="/tmp/.claude-token-keeper-$$.py"
    cat > "$tmp" <<'PY'
import os, sys, asyncio
sys.path.insert(0, "/app")
from app.core.credential_vault import encrypt_value, decrypt_value
from app.core.db_pool import get_pool, init_pool
KEY = sys.argv[1]
token = sys.stdin.read().strip()
if not token.startswith("sk-ant-oat01-"):
    print("reject:format"); sys.exit(1)
async def main():
    await init_pool()
    async with get_pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT encrypted_value FROM llm_api_keys WHERE provider='anthropic' AND key_name=$1", KEY)
        if not row:
            print("reject:nokey"); return
        try:
            if decrypt_value(row["encrypted_value"]) == token:
                # 토큰이 같아도 만료 기록은 최신으로 맞춘다.
                await c.execute(
                    "UPDATE llm_api_keys SET oauth_expires_at=NULLIF($2,'')::timestamptz, updated_at=NOW() "
                    "WHERE provider='anthropic' AND key_name=$1",
                    KEY, os.getenv("AADS_TOKEN_EXPIRES_ISO", ""))
                print("skip:same"); return
        except Exception:
            pass
        await c.execute(
            "UPDATE llm_api_keys SET encrypted_value=$2, oauth_expires_at=NULLIF($3,'')::timestamptz, "
            "last_verified_at=NOW(), updated_at=NOW() "
            "WHERE provider='anthropic' AND key_name=$1",
            KEY, encrypt_value(token), os.getenv("AADS_TOKEN_EXPIRES_ISO", ""))
        print("ok:updated")
asyncio.run(main())
PY
    docker cp "$tmp" "$container":/tmp/_tk.py >/dev/null 2>&1
    export AADS_TOKEN_EXPIRES_ISO="$expires_iso"
    # 토큰은 stdin 으로만 넘긴다. 인자로 주면 프로세스 목록에 노출된다.
    local out
    out="$(python3 -c "
import json
d = json.load(open('$cred'))
o = d.get('claudeAiOauth', d)
print(o.get('accessToken',''), end='')
" 2>/dev/null | docker exec -i -e AADS_TOKEN_EXPIRES_ISO="$expires_iso" "$container" python3 /tmp/_tk.py "$key" 2>&1 | grep -oE '^(ok|skip|reject):[a-z]+' | tail -1)"
    docker exec "$container" rm -f /tmp/_tk.py >/dev/null 2>&1
    rm -f "$tmp"
    log "  DB 동기화: ${out:-unknown}"
    [[ "$out" == ok:* || "$out" == skip:* ]]
}

renewed=0
for slot in 1 2 3 4; do
    home="${SLOT_ROOT}/slot${slot}"
    cred="${home}/.claude/.credentials.json"
    # 릴레이가 같은 슬롯 자격증명을 쓸 때 쓰는 락과 같은 파일이다.
    # keeper 는 전역 락(_KEEPER_LOCK)만 있어 릴레이의 실시간 갱신과
    # 배타되지 않았다 — 동시 갱신이 refresh token 회전 충돌을 낸 유력
    # 원인이었다(2026-09-21 슬롯2 소실). 이제 CLI 를 돌리는 순간에는
    # 이 락을 같이 쓴다.
    lock="${home}/.claude/.credentials.lock"
    [ -f "$cred" ] || { log "slot${slot}: 자격증명 없음 — 건너뜀"; continue; }

    left="$(remaining_min "$cred")"
    if [[ ! "$left" =~ ^-?[0-9]+$ ]]; then
        log "slot${slot}: 만료시각 판독 실패 — 건너뜀"; continue
    fi
    if [ "$left" -gt "$RENEW_BEFORE_MIN" ]; then
        # 갱신할 필요가 없어도 DB 는 맞춰 둔다.
        #
        # 러너는 llm_api_keys 의 만료 시각과 토큰 값을 보고 슬롯을 고른다.
        # 갱신 경로에서만 동기화하면, DB 가 한 번 어긋난 뒤 토큰이 넉넉해지는
        # 순간부터 영영 고쳐지지 않는다. 2026-09-13 실측 — 파일은 382분 남아
        # 있는데 DB 는 8시간 전 만료 시각을 들고 있어 러너가 쓸 수 있는 슬롯을
        # 찾지 못했다. 10분마다 도는 작업이라 비용은 무시할 수 있다.
        key="$(key_for_slot "$slot")"
        resync_to_db "$slot" "$key" "$cred" >/dev/null 2>&1 || true
        continue
    fi

    log "slot${slot}: 만료까지 ${left}분 (<= ${RENEW_BEFORE_MIN}) — 갱신"

    # **갱신 전에 원본을 복사한다.**
    #
    # 2026-09-14 23:40 사고. `claude -p "ping"` 이 갱신에 실패하면서
    # 자격증명 파일을 **빈 값으로 덮어썼다.**
    #
    #     "accessToken": "", "refreshToken": "", "expiresAt": 0,
    #     "refreshTokenExpiresAt": 1791552330578   ← 25일 남아 있었다
    #
    # refreshToken 이 만료된 것이 아니라 **지워진 것**이다. 그런데 이
    # 스크립트는 "refreshToken 만료로 보인다" 고 오진했고, 되돌릴 백업이
    # 없어 대표님 재로그인이 필요했다. 채팅이 그동안 멈췄다.
    #
    # 안전장치가 스스로 사고를 냈다. 갱신은 실패할 수 있다 — 실패해도
    # **있던 것을 잃지는 않아야 한다.**
    backup=""
    if [ -s "$cred" ] && ! _token_field_empty "$cred"; then
        backup="${cred}.bak"
        cp -p "$cred" "$backup" 2>/dev/null || backup=""
    elif [ -s "${cred}.bak" ] && ! _token_field_empty "${cred}.bak"; then
        # 현재 파일이 이미 비었으면 **지난 주기의 정상본**을 백업으로 삼는다.
        # 2026-09-21: 빈 파일이 .bak 을 그대로 덮어써 마지막 정상본까지
        # 사라졌고, 그래서 재로그인 외에 복구 경로가 없었다.
        backup="${cred}.bak"
    fi
    # CLI 는 만료가 임박하면 refreshToken 으로 스스로 갱신한다. 한도 초과(429)로
    # 응답이 실패해도 갱신 자체는 일어나므로 결과 코드로 판단하지 않는다.
    flock -w 60 "$lock" env HOME="$home" timeout 120 "$CLAUDE_BIN" -p "ping" --output-format json >/dev/null 2>&1 || true

    after="$(remaining_min "$cred")"
    # -1 은 "만료됨"이 아니라 파일을 읽지 못했다는 신호다(remaining_min 의
    # 예외 경로). 릴레이가 자격증명 파일을 쓰는 중이면 깨진 JSON 을 읽는다.
    # 2026-09-13 15:50 실측: slot2 가 `0분 → -1분` 으로 기록돼 "refreshToken
    # 만료, 재로그인 필요"로 단정됐고 DB 동기화를 건너뛰었다. 실제로는 파일이
    # 멀쩡했고(382분 남음) DB 만 8시간 낡은 채로 남아, 러너가 만료된 토큰을
    # 집어 작업을 시작하지 못했다.
    # 한 번 더 읽어보고 판단한다.
    if [[ "$after" == "-1" ]]; then
        sleep 3
        after="$(remaining_min "$cred")"
        [[ "$after" == "-1" ]] || log "  (자격증명 파일 재읽기 성공: ${after}분)"
    fi
    # 남은 수명을 비교하기 **전에** 토큰이 아직 있는지부터 본다.
    #
    # 2026-09-21 19:00 KST slot2 사고. 갱신이 파일을 비웠는데 left=-9,
    # after=-1 이라 `-1 > -9` 가 성립해 아래 성공 분기로 들어갔고, 그 뒤의
    # 롤백 가드 두 곳이 실행되지 않아 refreshToken 이 영구 소실됐다.
    # "수명이 늘었다"는 갱신 성공의 증거가 아니다. 토큰이 있어야 성공이다.
    if _token_field_empty "$cred"; then
        if [ -n "$backup" ] && [ -s "$backup" ] && ! _token_field_empty "$backup"; then
            cp -p "$backup" "$cred" 2>/dev/null \
                && log "  ↩ 갱신이 토큰을 지웠다 — 원본 복구 (refreshToken 보존)"
            after="$(remaining_min "$cred")"
            log "  ⚠️ 갱신 실패 (${left}분 → ${after}분) — 다음 주기에 다시 시도한다"
        else
            log "  ⛔ 갱신이 토큰을 지웠고 되돌릴 백업도 없다 — 재로그인 필요"
        fi
        ALERT="/root/aads/aads-server/scripts/send_disk_alert.sh"
        [ -x "$ALERT" ] && "$ALERT" "Claude slot${slot} 자격증명이 비었다 — 재로그인 필요" >/dev/null 2>&1 || true
        continue
    fi
    if [[ "$after" =~ ^-?[0-9]+$ ]] && [ "$after" -gt "$left" ]; then
        log "  갱신됨: ${left}분 → ${after}분"
        renewed=$((renewed + 1))
    elif [[ "$after" =~ ^-?[0-9]+$ ]] && [ "$after" -ge "$FAIL_BELOW_MIN" ]; then
        # CLI 가 아직 갱신할 때가 아니라고 판단한 경우. 실패가 아니다.
        # 다만 만료 시각은 남겨야 한다. 러너가 긴 작업 전에 남은 수명을 본다.
        log "  아직 갱신 시점 아님 (${after}분 남음)"
        # 만료가 임박했는데 CLI 가 거부하면, 다음 크론(10분 뒤)에는 이미
        # 늦을 수 있다. 같은 실행 안에서 몇 번 더 두드린다.
        if [ "$after" -le "$CLOSE_RETRY_MIN" ]; then
            tries=0
            while [ "$tries" -lt "$CLOSE_RETRY_TIMES" ]; do
                tries=$((tries + 1))
                sleep "$CLOSE_RETRY_SLEEP"
                flock -w 60 "$lock" env HOME="$home" timeout 120 "$CLAUDE_BIN" -p "ping" --output-format json >/dev/null 2>&1 || true
                retry_after="$(remaining_min "$cred")"
                if [ -n "$backup" ] && [ -s "$backup" ] \
                   && _token_field_empty "$cred" && ! _token_field_empty "$backup"; then
                    cp -p "$backup" "$cred" 2>/dev/null \
                        && log "  ↩ 재시도가 토큰을 지워 원본을 되돌렸다"
                    retry_after="$(remaining_min "$cred")"
                fi
                if [[ "$retry_after" =~ ^[0-9]+$ ]] && [ "$retry_after" -gt "$after" ]; then
                    log "  갱신됨(임박 재시도 ${tries}회): ${after}분 → ${retry_after}분"
                    after="$retry_after"
                    renewed=$((renewed + 1))
                    break
                fi
                log "  임박 재시도 ${tries}/${CLOSE_RETRY_TIMES} — 아직 (${retry_after}분)"
            done
        fi
        key="$(key_for_slot "$slot")"
        resync_to_db "$slot" "$key" "$cred" >/dev/null 2>&1 || true
        continue
    else
        # 갱신에 실패해도 파일이 아직 유효하면 DB 는 맞춰 둔다. 러너는 DB 의
        # 만료 시각과 토큰 값을 보고 슬롯을 고르므로, 여기서 건너뛰면 멀쩡한
        # 토큰을 두고도 "쓸 수 있는 슬롯 없음"이 된다.
        if [[ "$after" =~ ^[0-9]+$ ]] && [ "$after" -gt 0 ]; then
            key="$(key_for_slot "$slot")"
            resync_to_db "$slot" "$key" "$cred" >/dev/null 2>&1 \
                && log "  갱신은 실패했지만 파일이 유효해 DB 는 동기화함 (${after}분)"
        fi
        # 갱신이 토큰을 지웠으면 되돌린다. 빈 파일보다 만료 임박한 토큰이 낫다 —
        # 남은 몇 분이라도 쓸 수 있고, 무엇보다 refreshToken 이 살아 있으면
        # 다음 주기에 다시 시도할 수 있다.
        if [ -n "$backup" ] && [ -s "$backup" ]; then
            if _token_field_empty "$cred" && ! _token_field_empty "$backup"; then
                cp -p "$backup" "$cred" 2>/dev/null \
                    && log "  ↩ 갱신이 토큰을 지워 원본을 되돌렸다 (refreshToken 보존)"
                after="$(remaining_min "$cred")"
            fi
        fi
        log "  ⚠️ 갱신 실패 (${left}분 → ${after}분) — 다음 주기에 다시 시도한다"
        ALERT="/root/aads/aads-server/scripts/send_disk_alert.sh"
        [ -x "$ALERT" ] && "$ALERT" "Claude slot${slot} 토큰 갱신 실패 — 재로그인 필요" >/dev/null 2>&1 || true
        continue
    fi

    key="$(key_for_slot "$slot")"
    resync_to_db "$slot" "$key" "$cred" || log "  ⚠️ DB 동기화 실패 — 릴레이는 옛 토큰을 계속 쓴다"
done

[ "$renewed" -gt 0 ] && log "갱신 완료: ${renewed}개 슬롯"
exit 0
