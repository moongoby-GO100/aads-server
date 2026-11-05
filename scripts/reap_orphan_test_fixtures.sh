#!/usr/bin/env bash
# 고아가 된 pytest 고정 프로그램(fixture actor) 회수.
#
# 2026-11-05 실측: 채팅 세션이 돌린 러너 점검 테스트가 중간에 끊기면서(pytest 강제 종료 →
# finally 미실행) /tmp/pytest-of-root/.../actor.sh 반복문 3개가 최장 32분 남았다. 0.02초마다
# 프로세스를 띄워 116 전체 fork 가 초당 약 320회, 커널 CPU 21%, 채팅 API 응답 최대 9.7초.
#
# 1차 방지는 테스트 반복문의 수명 상한(300초)이고, 이것은 그물이다.
# 대상: 명령이 `/tmp/pytest-of-<user>/pytest-<n>/…` 안의 스크립트이고, 부모가 1(고아)이며,
#       REAP_FIXTURE_MIN_AGE 초(기본 600) 이상 산 것만. 서비스·러너·거래 프로세스는 이 경로를 쓰지 않는다.
# 순서: 테스트가 원래 쓰는 정지 파일($FIXTURE/stop)을 먼저 만들고, 3초 뒤에도 살아 있으면 TERM.
set -uo pipefail

MIN_AGE="${REAP_FIXTURE_MIN_AGE:-600}"
PROC_ROOT="${REAP_FIXTURE_PROC_ROOT:-/proc}"
PATTERN='^(/usr/bin/|/bin/)?(ba)?sh /tmp/pytest-of-[A-Za-z0-9_.-]+/pytest-[0-9]+/[^ ]+\.sh( |$)'

mapfile -t targets < <(
    ps -eo pid=,ppid=,etimes=,args= | awk -v min="$MIN_AGE" '$2 == 1 && $3 >= min {
        pid=$1; $1=$2=$3=""; sub(/^ +/, ""); print pid "\t" $0 }' \
      | grep -E $'\t'"${PATTERN#^}" | cut -f1
)
[[ ${#targets[@]} -eq 0 ]] && exit 0

for pid in "${targets[@]}"; do
    fx=$(tr '\0' '\n' < "${PROC_ROOT}/${pid}/environ" 2>/dev/null | sed -n 's/^FIXTURE=//p' | head -1)
    if [[ "$fx" == /tmp/pytest-of-*/* && -d "$fx" ]]; then
        touch "$fx/stop" 2>/dev/null || true
    fi
done
sleep "${REAP_FIXTURE_GRACE:-3}"
reaped=0
for pid in "${targets[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
    fi
    reaped=$((reaped + 1))
done
msg="reaped_test_fixtures=${reaped} min_age=${MIN_AGE}s pids=${targets[*]}"
logger -t reap-test-fixtures "$msg" 2>/dev/null || true
echo "$(TZ=Asia/Seoul date '+%F %T KST') $msg"
