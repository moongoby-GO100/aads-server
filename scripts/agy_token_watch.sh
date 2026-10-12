#!/bin/bash
# 정본은 저장소 scripts/ — 설치: cp scripts/agy_token_*.sh /usr/local/bin/ (keepalive 는 contabo14·cafe24_114 에도 scp)
# Antigravity CLI(agy) 토큰 자동 유지 — 세 서버 감시 (contabo116 cron, 30분마다)
# 각 서버에서 agy_token_keepalive.sh 를 실행해 토큰 갱신을 일으키고,
# 실패(갱신 불가·ssh 불가)로 바뀔 때와 복구될 때만 텔레그램을 보낸다(상태 변화 알림, 반복 스팸 없음).
# 텔레그램 자격증명 읽는 방식은 scripts/codex_auth_sync.sh 의 load_telegram_creds 와 같다.
set -uo pipefail
export PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
LOG=/var/log/agy_token_watch.log
STATE=/var/lib/agy_token_watch; mkdir -p "$STATE"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no"
log(){ printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$*" >>"$LOG"; }
exec 8>/var/lock/agy_token_watch.lock; flock -n 8 || exit 0
TG_TOKEN=""; TG_CHAT=""
while IFS= read -r line; do
  case "$line" in
    TELEGRAM_BOT_TOKEN=*) v="${line#*=}"; v="${v//\"/}"; v="${v//\'/}"; [ -n "$v" ] && TG_TOKEN="$v" ;;
    TELEGRAM_CHAT_ID=*)   v="${line#*=}"; v="${v//\"/}"; v="${v//\'/}"; [ -n "$v" ] && TG_CHAT="$v" ;;
  esac
done < <(cat /root/aads/aads-server/.env /root/aads/.env 2>/dev/null)
tg(){ [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] && curl -s -m 20 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1; true; }
check(){ # label host port(빈 값=로컬)
  local label=$1 host=$2 port=$3 out rc
  if [ -z "$host" ]; then out=$(timeout 200 /usr/local/bin/agy_token_keepalive.sh 2>&1); rc=$?
  else out=$(timeout 220 ssh $SSH_OPTS -p "$port" "$host" /usr/local/bin/agy_token_keepalive.sh 2>&1); rc=$?; fi
  local now=OK; [ $rc -eq 0 ] || now=FAIL
  local prev; prev=$(cat "$STATE/$label" 2>/dev/null || echo OK)
  log "$label rc=$rc $out"
  if [ "$now" != "$prev" ]; then
    if [ "$now" = FAIL ]; then tg "🔴 [Antigravity CLI] $label 토큰 자동 갱신 실패(rc=$rc) — 해당 서버에서 agy 재로그인 필요"
    else tg "✅ [Antigravity CLI] $label 토큰 자동 갱신 복구"; fi
  fi
  echo "$now" > "$STATE/$label"
}
check contabo116 "" ""
check contabo14 5.104.86.14 22
check cafe24_114 114.207.244.86 7916
