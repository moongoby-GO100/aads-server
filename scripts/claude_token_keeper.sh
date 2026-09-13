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
RENEW_BEFORE_MIN="${CLAUDE_TOKEN_RENEW_BEFORE_MIN:-30}"
# 갱신 시도 후에도 이 시간 미만으로 남아 있으면 진짜 실패다(refreshToken 만료 등).
FAIL_BELOW_MIN="${CLAUDE_TOKEN_FAIL_BELOW_MIN:-10}"

log() { echo "$(date '+%F %T') $*"; }

remaining_min() {
    python3 -c "
import json, sys, time
try:
    d = json.load(open('$1'))
except Exception:
    print(-1); sys.exit()
o = d.get('claudeAiOauth', d)
exp = o.get('expiresAt', 0)
print(int((exp/1000 - time.time()) / 60) if exp else -1)
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
for slot in 1 2; do
    home="${SLOT_ROOT}/slot${slot}"
    cred="${home}/.claude/.credentials.json"
    [ -f "$cred" ] || { log "slot${slot}: 자격증명 없음 — 건너뜀"; continue; }

    left="$(remaining_min "$cred")"
    if [[ ! "$left" =~ ^-?[0-9]+$ ]]; then
        log "slot${slot}: 만료시각 판독 실패 — 건너뜀"; continue
    fi
    if [ "$left" -gt "$RENEW_BEFORE_MIN" ]; then
        continue
    fi

    log "slot${slot}: 만료까지 ${left}분 (<= ${RENEW_BEFORE_MIN}) — 갱신"
    # CLI 는 만료가 임박하면 refreshToken 으로 스스로 갱신한다. 한도 초과(429)로
    # 응답이 실패해도 갱신 자체는 일어나므로 결과 코드로 판단하지 않는다.
    HOME="$home" timeout 120 "$CLAUDE_BIN" -p "ping" --output-format json >/dev/null 2>&1 || true

    after="$(remaining_min "$cred")"
    if [[ "$after" =~ ^-?[0-9]+$ ]] && [ "$after" -gt "$left" ]; then
        log "  갱신됨: ${left}분 → ${after}분"
        renewed=$((renewed + 1))
    elif [[ "$after" =~ ^-?[0-9]+$ ]] && [ "$after" -ge "$FAIL_BELOW_MIN" ]; then
        # CLI 가 아직 갱신할 때가 아니라고 판단한 경우. 실패가 아니다.
        # 다만 만료 시각은 남겨야 한다. 러너가 긴 작업 전에 남은 수명을 본다.
        log "  아직 갱신 시점 아님 (${after}분 남음)"
        key="ANTHROPIC_AUTH_TOKEN"
        [ "$slot" = "2" ] && key="ANTHROPIC_AUTH_TOKEN_2"
        resync_to_db "$slot" "$key" "$cred" >/dev/null 2>&1 || true
        continue
    else
        log "  ⚠️ 갱신 실패 (${left}분 → ${after}분) — refreshToken 만료로 보인다. 재로그인 필요"
        ALERT="/root/aads/aads-server/scripts/send_disk_alert.sh"
        [ -x "$ALERT" ] && "$ALERT" "Claude slot${slot} 토큰 갱신 실패 — 재로그인 필요" >/dev/null 2>&1 || true
        continue
    fi

    key="ANTHROPIC_AUTH_TOKEN"
    [ "$slot" = "2" ] && key="ANTHROPIC_AUTH_TOKEN_2"
    resync_to_db "$slot" "$key" "$cred" || log "  ⚠️ DB 동기화 실패 — 릴레이는 옛 토큰을 계속 쓴다"
done

[ "$renewed" -gt 0 ] && log "갱신 완료: ${renewed}개 슬롯"
exit 0
