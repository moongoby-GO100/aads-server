#!/usr/bin/env bash
# 고아가 된 조회용 프로세스를 회수한다.
#
# 에이전트가 `docker exec ... tail -n 200 access.log` 같은 조회를 띄운 뒤
# 결과만 받고 파이프를 닫지 않으면, 컨테이너 안의 tail 은 읽는 쪽이 사라진 채
# 남는다. CPU 는 0% 라 눈에 띄지 않지만 FD·PID 를 붙든다.
#
# 2026-09-14 실측: 이런 프로세스가 40개, 최장 123일. 프로세스 총수 464개 중
# 34개가 이것이었다. 부하 자체보다 `pgrep`·`ps` 조회를 느리게 만드는 게 문제다.
#
# 근본 해결은 호출 시 timeout 을 붙이는 것이고, 이 스크립트는 그물이다.
set -uo pipefail

AGE_DAYS="${REAP_AGE_DAYS:-1}"
LOG_TAG="reap-stale"

# 대상: etime 에 '일' 단위가 있고(=하루 이상), 명령이 조회 도구인 것.
# 서비스가 걸리지 않도록 tail/grep/cat 만 본다 — 이 셋은 장기 실행이 정상이 아니다.
mapfile -t targets < <(
    ps -eo pid,etime,args --no-headers \
      | awk -v d="$AGE_DAYS" '$2 ~ /^[0-9]+-/ { split($2,a,"-"); if (a[1] >= d) print }' \
      | grep -E "\b(tail|grep|cat)\b" \
      | grep -vE "grep -E|awk " \
      | awk '{print $1}'
)

[ "${#targets[@]}" -eq 0 ] && exit 0

killed=0
for pid in "${targets[@]}"; do
    kill -TERM "$pid" 2>/dev/null && killed=$((killed + 1))
done
sleep 3
for pid in "${targets[@]}"; do
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
done

logger -t "$LOG_TAG" "reaped=${killed} age>=${AGE_DAYS}d total_procs=$(ps -eo pid --no-headers | wc -l)"
echo "$(date '+%F %T') reaped=${killed} (age>=${AGE_DAYS}d)"
