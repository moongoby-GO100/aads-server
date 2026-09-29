#!/usr/bin/env bash
# DEPLOY_ONLY 가드 검증 (AADS-RUNNER-DEPLOYONLY-FALSE-POSITIVE-DEADLOCK-20260930)
#   R1: 릴리스잡 판정이 헤더 선언에만 반응하는가 — SQL 정규식을 psql 로 직접 평가
#   R2: 교착 경고(warn_stuck_dependency_queue) — TEMP 테이블 위에서 실행, 실제 pipeline_jobs 는 건드리지 않는다
# 실행: bash tests/check_deploy_only_guard.sh   (PGUSER/PGDATABASE 환경 + aads-postgres 컨테이너 또는 PGHOST 접속 필요)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/pipeline-runner.sh"
FX="$ROOT/tests/fixtures/deploy_only_guard"
PASS=0; FAIL=0

_psql() {
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "${PG_CONTAINER:-aads-postgres}"; then
        docker exec -i "${PG_CONTAINER:-aads-postgres}" psql -U "$PGUSER" -d "$PGDATABASE" "$@"
    else
        PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" "$@"
    fi
}

check() { # name expected actual
    if [[ "$2" == "$3" ]]; then PASS=$((PASS+1)); echo "  PASS  $1 (=$3)"
    else FAIL=$((FAIL+1)); echo "  FAIL  $1 expected=$2 actual=$3"; fi
}

# 러너 스크립트 전체를 source 하면 메인 루프가 돈다 — 필요한 정의만 잘라 온다.
eval "$(grep -m1 '^DEPLOY_ONLY_HEADER_SQL=' "$SCRIPT")"
[[ -n "${DEPLOY_ONLY_HEADER_SQL:-}" ]] || { echo "DEPLOY_ONLY_HEADER_SQL 정의를 찾지 못했다"; exit 2; }
extract_fn() { awk -v n="$1" '$0 ~ "^"n"\\(\\) \\{" {f=1} f {print} f && /^}$/ {exit}' "$SCRIPT"; }

# ── R1 ────────────────────────────────────────────────────────────────
is_release() { # 지시서 원문 → t/f  (t = 릴리스잡으로 판정 = 보호 예외 적용)
    printf "SELECT (%s) FROM (SELECT \$fx\$%s\$fx\$::text AS instruction) p;" "$DEPLOY_ONLY_HEADER_SQL" "$1" \
        | _psql -q -t -A
}

echo "== R1: DEPLOY_ONLY 헤더 한정 판정"
check "①-a 헤더 1행 'DEPLOY_ONLY: true'" t "$(is_release $'DEPLOY_ONLY: true\nTASK_ID: X\nTITLE: 배포')"
check "①-b 헤더 5행(runner-8e41830c 형태)" t "$(is_release $'TASK_ID: X\nTITLE: t\nPRIORITY: P1\nSIZE: XS\nDEPLOY_ONLY: true\n\n본문')"
check "①-c 콜론 앞뒤 공백 변형" t "$(is_release $'TASK_ID: X\n  DEPLOY_ONLY :  true')"
check "②-a 본문 산문 안 언급" f "$(is_release $'TASK_ID: X\n\n이 잡은 커밋까지만. 릴리스는 별도 DEPLOY_ONLY 잡이 처리한다.')"
check "②-b 불릿 안 언급" f "$(is_release $'TASK_ID: X\n- DEPLOY_ONLY가 아님 — 코드 수정 잡')"
check "②-c 'DEPLOY_ONLY: false'" f "$(is_release $'DEPLOY_ONLY: false\nTASK_ID: X')"
check "②-d 소문자 산문 deploy_only" f "$(is_release $'TASK_ID: X\n종결 경로(no_changes / read_only / deploy_only)')"
filler=$(printf 'x\n%.0s' {1..25})
check "②-e 21행 이후의 선언은 무시(러너 head -20 과 일치)" f "$(is_release "${filler}"$'DEPLOY_ONLY: true')"
check "③ runner-6a9a2950 실제 원문(일반 코드잡, 오탐 사고 원인)" f "$(is_release "$(cat "$FX/runner-6a9a2950.txt")")"
check "④-a 실제 릴리스잡 runner-8e41830c 원문" t "$(is_release "$(cat "$FX/runner-8e41830c.txt")")"
check "④-b 실제 릴리스잡 runner-436672d3 원문" t "$(is_release "$(cat "$FX/runner-436672d3.txt")")"

echo "== R1 보조: 가드 SQL 에 본문 substring 매칭이 남아 있지 않은가"
guard_ilike=$(sed -n '/^cleanup_blocked_dependencies() {/,/^}$/p' "$SCRIPT" | grep -c "ILIKE '%DEPLOY_ONLY%'")
check "cleanup_blocked_dependencies 안 ILIKE '%DEPLOY_ONLY%' 개수" 0 "$guard_ilike"
guard_use=$(sed -n '/^cleanup_blocked_dependencies() {/,/^}$/p' "$SCRIPT" | grep -c 'NOT (${DEPLOY_ONLY_HEADER_SQL})')
check "가드 SQL 의 헤더 판정 사용 횟수(취소 2 = existing/missing)" 2 "$guard_use"

# ── R2 ────────────────────────────────────────────────────────────────
echo "== R2: 교착 경고"
eval "$(extract_fn _dep_stuck_sql)"
eval "$(extract_fn warn_stuck_dependency_queue)"
declare -A _DEP_STUCK_WARNED=()
_DEP_STUCK_LAST_CHECK=0

PRELUDE="CREATE TEMP TABLE pipeline_jobs AS SELECT * FROM public.pipeline_jobs WHERE false;
INSERT INTO pipeline_jobs (job_id,status,depends_on,created_at,updated_at,completed_at,chat_session_id,instruction) VALUES
 ('par-bad',  'rejected_done',NULL,       NOW()-INTERVAL '3 hours', NOW()-INTERVAL '2 hours', NOW()-INTERVAL '2 hours','s','x'),
 ('par-done', 'done',         NULL,       NOW()-INTERVAL '3 hours', NOW()-INTERVAL '2 hours', NOW()-INTERVAL '2 hours','s','x'),
 ('par-fresh','error',        NULL,       NOW()-INTERVAL '3 hours', NOW()-INTERVAL '5 minutes',NOW()-INTERVAL '5 minutes','s','x'),
 ('par-run',  'running',      NULL,       NOW()-INTERVAL '3 hours', NOW()-INTERVAL '2 hours', NULL,'s','x'),
 ('kid-stuck','queued','par-bad',  NOW()-INTERVAL '3 hours',NOW(),NULL,'c0a11000-0917-4000-8000-0000000000a1','x'),
 ('kid-ok',   'queued','par-done', NOW()-INTERVAL '3 hours',NOW(),NULL,'c0a11000-0917-4000-8000-0000000000a1','x'),
 ('kid-fresh','queued','par-fresh',NOW()-INTERVAL '3 hours',NOW(),NULL,'c0a11000-0917-4000-8000-0000000000a1','x'),
 ('kid-wait', 'queued','par-run',  NOW()-INTERVAL '3 hours',NOW(),NULL,'c0a11000-0917-4000-8000-0000000000a1','x');"

db_exec() { { echo "$PRELUDE"; printf '%s' "$1"; } | _psql -q -t -A -P footer=off -F $'\x1e' 2>&1; }
LOGF=$(mktemp); CHATF=$(mktemp); trap 'rm -f "$LOGF" "$CHATF"' EXIT
log() { echo "$*" >> "$LOGF"; }
post_to_chat() { echo "$1|$2" >> "$CHATF"; }

DEP_STUCK_CHECK_INTERVAL_SEC=0 warn_stuck_dependency_queue
check "V3-a 부모 rejected_done·2시간 경과 → 경고 대상" 1 "$(grep -c 'DEP_STUCK_WARN job=kid-stuck' "$LOGF")"
check "V3-a 채팅 경고 전송" 1 "$(grep -c 'kid-stuck' "$CHATF")"
msg=$(cat "$CHATF")
for needle in "kid-stuck" "par-bad" "rejected_done" "분째"; do
    [[ "$msg" == *"$needle"* ]] && r=yes || r=no
    check "V3-a 경고 문구에 '$needle' 포함" yes "$r"
done
wait_min=$(grep -o 'wait_min=[0-9]*' "$LOGF" | head -1 | cut -d= -f2)
[[ "$wait_min" =~ ^[0-9]+$ && "$wait_min" -ge 119 && "$wait_min" -le 122 ]] && r=ok || r="bad($wait_min)"
check "V3-a 대기 경과 분(부모 종료 시점 기준 ≈120)" ok "$r"
check "V3-b 부모 done → 무경고" 0 "$(grep -c 'kid-ok' "$LOGF")"
check "V3-c 부모 terminal 이지만 임계(30분) 미만 → 무경고" 0 "$(grep -c 'kid-fresh' "$LOGF")"
check "V3-d 부모 running → 무경고" 0 "$(grep -c 'kid-wait' "$LOGF")"
DEP_STUCK_CHECK_INTERVAL_SEC=0 warn_stuck_dependency_queue
check "V3-e 재점검해도 재경고 쿨다운 안에서는 중복 전송 없음" 1 "$(grep -c 'kid-stuck' "$CHATF")"
before=$(wc -l < "$LOGF")
DEP_STUCK_CHECK_INTERVAL_SEC=3600 warn_stuck_dependency_queue
check "V3-f 점검 주기 미도래 시 DB 조회·로그 없음" "$before" "$(wc -l < "$LOGF")"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
