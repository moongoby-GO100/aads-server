#!/bin/bash
# 정본은 저장소 scripts/ — 설치: cp scripts/agy_token_*.sh /usr/local/bin/ (keepalive 는 contabo14·cafe24_114 에도 scp)
# Antigravity CLI(agy) 구글 OAuth 토큰 자동 유지 — 2026-10-12 CEO 지시("다른 CLI처럼 자동갱신")
# agy 는 호출 시 access_token(1시간)이 만료돼 있으면 refresh_token 으로 스스로 갱신한다
# (2026-10-12 contabo116 에서 expiry 를 과거로 바꾼 뒤 `agy models` 1회로 새 토큰 발급 확인).
# 이 스크립트는 가벼운 호출(agy models, 생성 할당량 미사용)로 그 갱신을 주기적으로 일으켜
# 오래 안 쓴 서버에서도 토큰이 살아 있게 하고, 갱신 뒤에도 만료면 exit 2 로 알린다.
# 노드마다 로그인이 독립(각자 refresh_token)이라 토큰을 서버 간에 복사하지 않는다.
# 호출: contabo116 의 agy_token_watch.sh 가 ssh 로 세 서버에서 실행한다.
set -uo pipefail
export PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
TOK=/root/.gemini/antigravity-cli/antigravity-oauth-token
LOG=/var/log/agy_token_keepalive.log
log(){ printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$*" >>"$LOG"; }
exec 9>/var/lock/agy_token_keepalive.lock
flock -n 9 || { log "skip: 실행 중"; echo "SKIP"; exit 0; }
remaining(){ python3 - "$TOK" <<'PY' 2>/dev/null || echo -999999
import json,sys,datetime,re
t=json.load(open(sys.argv[1]))['token']
if not t.get('refresh_token'): print(-999999); sys.exit()
e=re.sub(r'(\.\d{6})\d+',r'\1',t['expiry'])
print(int((datetime.datetime.fromisoformat(e)-datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
PY
}
if [ ! -f "$TOK" ]; then log "FAIL 토큰 파일 없음 — agy 재로그인 필요"; echo "FAIL no_token"; exit 2; fi
before=$(remaining)
W=$(mktemp -d /tmp/agy-keepalive.XXXXXX)
(cd "$W" && timeout -k 10 150 agy models >/dev/null 2>&1); rc=$?
rm -rf "$W"
# agy 는 실행마다 log/cli-*.log 를 새로 만든다(cafe24_114 2026-10-12 4,953개 66MB). 7일 지난 것만 지운다.
find /root/.gemini/antigravity-cli/log -maxdepth 1 -name "cli-*.log" -mtime +7 -delete 2>/dev/null
after=$(remaining)
if [ "$after" -gt 0 ]; then log "OK rc=$rc 남은시간 ${before}s -> ${after}s"; echo "OK ${after}s"; exit 0; fi
log "FAIL rc=$rc 남은시간 ${before}s -> ${after}s — 자동 갱신 실패, agy 재로그인 필요"; echo "FAIL rc=$rc ${after}s"; exit 2
