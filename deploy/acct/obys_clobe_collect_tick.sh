#!/bin/bash
# 오비서 클로브 수집 1시간 틱 — cafe24(server-114) 에 설치되어 systemd timer 가 부른다.
#   저장소 위치: deploy/acct/obys_clobe_collect_tick.sh
#   설치 경로:   /srv/acct/bin/obys_clobe_collect_tick.sh   (scripts/install_obys_clobe_timer_cafe24.sh)
#
# 활성 앱 컨테이너 = cafe24 apache fb vhost 의 upstream 포트로 --port 를 띄운 acct-app-candidate-rN 이
# 정확히 한 개일 때만 그 안에서 수집을 돌린다. 하나로 못 정하면 아무 컨테이너에도 쓰지 않고 exit 2.
# (scripts/deploy_acct_app_cafe24.sh 의 R_APACHE_UPSTREAM / R_LIST_CANDIDATES 와 같은 판별.)
#
# 종료코드: 0 수집 성공 / 1 수집 실패(스케줄러 JSON 참조) / 2 활성 컨테이너 판별 실패(수집 안 함) / 124 시간 초과
set -uo pipefail

APACHE_SITE="${ACCT_APACHE_SITE:-/etc/apache2/sites-available/00-zz-fb.newtalk.kr.conf}"
CANDIDATE_RE='^acct-app-candidate-r[0-9]+$'
TOTAL_TIMEOUT="${OBYS_COLLECT_TIMEOUT:-900}"
PYTHON_BIN="${OBYS_COLLECT_PYTHON:-python3}"

fail() { echo "obys_clobe_collect_tick: $*" >&2; exit 2; }

[[ -r $APACHE_SITE ]] || fail "cannot read $APACHE_SITE"
# 주석 줄은 읽지 않는다(설치본 vhost 주석에 예시 포트가 남아 있다).
ups=$(grep -vE '^[[:space:]]*#' "$APACHE_SITE" | grep -oE 'http://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+' | sort -u)
[[ -n $ups && $(wc -l <<<"$ups") == 1 ]] || fail "ambiguous or missing upstream in $APACHE_SITE: ${ups//$'\n'/ }"
port=${ups##*:}

names=$(timeout 30 docker ps --format '{{.Names}}') || fail "docker ps failed"
active=()
while IFS= read -r n; do
    [[ $n =~ $CANDIDATE_RE ]] || continue
    words=$(timeout 30 docker inspect -f '{{range .Config.Entrypoint}}{{.}} {{end}}{{range .Config.Cmd}}{{.}} {{end}}' "$n" 2>/dev/null) \
        || fail "docker inspect $n failed"
    p=$(grep -oE -e '--port[= ][0-9]+' <<<"$words" | head -1 | grep -oE '[0-9]+$' || true)
    [[ $p == "$port" ]] && active+=("$n")
done <<<"$names"

[[ ${#active[@]} -eq 1 ]] || fail "expected exactly 1 active candidate on port $port, found ${#active[@]}: ${active[*]:-none}"

exec timeout -k 15 "$TOTAL_TIMEOUT" docker exec "${active[0]}" "$PYTHON_BIN" -m app.services.obys_collection_scheduler --once
