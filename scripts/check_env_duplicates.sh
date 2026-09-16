#!/bin/bash
# AADS .env 중복 키 게이트 (R-KEY / R-ERRBOOK)
#
# 왜 있는가.
# 2026-09-16, aads-server/.env 의 122~125행 블록이 126~129행에 통째로 다시
# 붙여넣어져 CLAUDE_SESSION_KEY·CLAUDE_ORG_ID·GOOGLE_CONSOLE_EMAIL·
# GOOGLE_CONSOLE_PASSWORD 가 두 벌이 됐다. 이번에는 두 벌의 값이 같아서
# 아무 일도 일어나지 않았다. 값이 달랐다면 docker compose 는 뒤 줄을 채택하므로
# "파일을 고쳤는데 컨테이너에 반영이 안 된다"로 드러났을 것이고, 그건 키 문제로는
# 보이지 않아 추적에 오래 걸린다.
#
# .env 는 .gitignore 대상이라 pre-commit 만으로는 파일 자체를 볼 기회가 없다.
# 그래서 두 자리에 건다 — pre-commit(커밋 시점의 작업 디렉터리)과
# pre_deploy_validate.sh(5분 주기 cron). 규칙 문서로만 적으면 또 일어난다.
#
# 사용:
#   check_env_duplicates.sh              기본 대상 전체 검사(사람이 읽는 출력)
#   check_env_duplicates.sh --quiet      파일별 건수만 출력(알림/로그용)
#   check_env_duplicates.sh [--quiet] FILE...   지정 파일만 검사(테스트용)
#
# 종료코드: 0 = 중복 없음 / 1 = 중복 발견
#
# 값은 절대 출력하지 않는다. 키 이름과 줄 번호까지만 낸다.

set -u

QUIET=0
if [ "${1:-}" = "--quiet" ]; then
    QUIET=1
    shift
fi

if [ "$#" -gt 0 ]; then
    TARGETS="$*"
else
    TARGETS="/root/aads/.env
/root/aads/aads-server/.env
/root/aads/aads-server/.env.litellm
/root/aads/aads-dashboard/.env
/root/aads/aads-dashboard/.env.local
/root/aads/aads-dashboard/.env.production"
fi

FOUND=0

for f in $TARGETS; do
    [ -f "$f" ] || continue

    DUPS=$(awk -F= '
        /^[A-Za-z_][A-Za-z0-9_]*=/ {
            k = $1
            c[k]++
            l[k] = (k in l) ? l[k] "," NR : NR
        }
        END {
            for (k in c)
                if (c[k] > 1)
                    printf "  %s — %d벌 (%s행)\n", k, c[k], l[k]
        }
    ' "$f")

    [ -z "$DUPS" ] && continue

    FOUND=1
    if [ "$QUIET" -eq 1 ]; then
        echo "$f: $(printf '%s\n' "$DUPS" | wc -l)건"
    else
        echo "❌ $f"
        printf '%s\n' "$DUPS"
    fi
done

if [ "$FOUND" -eq 1 ]; then
    if [ "$QUIET" -eq 0 ]; then
        echo "   뒤 줄이 앞 줄을 덮는다(docker compose last-wins)."
        echo "   두 벌의 값이 같은지 먼저 확인한 뒤 한 벌만 남겨라:"
        echo "   grep -n '^KEY=' <파일>  →  값 비교  →  sed -i '<행>d' <파일>"
    fi
    exit 1
fi

[ "$QUIET" -eq 0 ] && echo "✅ .env 중복 키 없음"
exit 0
