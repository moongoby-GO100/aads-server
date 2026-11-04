#!/usr/bin/env bash
# 카페24 백업(_aads-backup) 보관정책 B안 DRY-RUN (CEO 승인 2026-11-04).
#
# 규칙(폴더명 날짜 기준, mtime 아님):
#   a) 최근 30일 일별 폴더 보존 (오늘 기준 0~30일 전, 경계: 30일 전 보존 / 31일 전 후보)
#   b) 매월 1일 폴더(YYYYMM01)는 오늘로부터 12개월 전 같은 날짜 이후분 보존
#   c) 그 외 YYYYMMDD 폴더는 삭제 후보
#   d) 가장 최신 폴더는 항상 보존
#   e) ^[0-9]{8}$ 에 맞지 않거나 달력에 없는 날짜/미래 날짜 폴더는 후보에서 제외하고 "수동 검토"
#
# 이 스크립트는 읽기 전용이다(원격 du 만 사용). 삭제 기능은 구현하지 않았다.
#   usage: offload_retention.sh [--today YYYYMMDD]

RETENTION_DAILY_DAYS=30
RETENTION_MONTHLY_MONTHS=12
RETENTION_MAX_CANDIDATE_PCT=50
RETENTION_EXPECTED_ROOT="/home/danharoo/www/data/files/goods/goodscode/_aads-backup"

# YYYYMMDD -> 1970-01-01 기준 일수. 형식/달력이 틀리면 rc=1.
retention_day_number() {
    local d="$1" secs back
    [[ "$d" =~ ^[0-9]{8}$ ]] || return 1
    secs="$(date -u -d "${d:0:4}-${d:4:2}-${d:6:2} 00:00:00" +%s 2>/dev/null)" || return 1
    back="$(date -u -d "@$secs" +%Y%m%d 2>/dev/null)" || return 1
    [[ "$back" == "$d" ]] || return 1
    printf '%s\n' $((secs / 86400))
}

# retention_classify TODAY  (stdin: 폴더명, 줄당 하나)
# stdout: VERDICT<TAB>폴더명<TAB>사유
# VERDICT: KEEP_LATEST | KEEP_DAILY | KEEP_MONTHLY | DELETE_CANDIDATE | MANUAL
retention_classify() {
    local today="$1" today_n name n age latest="" latest_n=-1 monthly_cutoff
    local -a names=()
    local -A day_n=()

    today_n="$(retention_day_number "$today")" || { echo "invalid today: $today" >&2; return 2; }
    monthly_cutoff="$(printf '%04d%s' $((10#${today:0:4} - RETENTION_MONTHLY_MONTHS / 12)) "${today:4:4}")"

    while IFS= read -r name || [[ -n "$name" ]]; do
        [[ -n "$name" ]] || continue
        names+=("$name")
        if n="$(retention_day_number "$name")" && (( n <= today_n )); then
            day_n[$name]="$n"
            if (( n > latest_n )); then latest_n="$n"; latest="$name"; fi
        fi
    done

    for name in "${names[@]}"; do
        if [[ ! "$name" =~ ^[0-9]{8}$ ]]; then
            printf 'MANUAL\t%s\t날짜 폴더 형식(YYYYMMDD) 아님\n' "$name"
        elif [[ -z "${day_n[$name]:-}" ]]; then
            if retention_day_number "$name" >/dev/null; then
                printf 'MANUAL\t%s\t오늘(%s)보다 미래 날짜\n' "$name" "$today"
            else
                printf 'MANUAL\t%s\t달력에 없는 날짜\n' "$name"
            fi
        elif [[ "$name" == "$latest" ]]; then
            printf 'KEEP_LATEST\t%s\t가장 최신 폴더\n' "$name"
        else
            age=$((today_n - day_n[$name]))
            if (( age <= RETENTION_DAILY_DAYS )); then
                printf 'KEEP_DAILY\t%s\t%d일 전 (일별 %d일 이내)\n' "$name" "$age" "$RETENTION_DAILY_DAYS"
            elif [[ "${name:6:2}" == "01" ]] && (( 10#$name >= 10#$monthly_cutoff )); then
                printf 'KEEP_MONTHLY\t%s\t매월 1일분 (%s 이후)\n' "$name" "$monthly_cutoff"
            else
                printf 'DELETE_CANDIDATE\t%s\t%d일 전, 일별 보존 초과 및 월 보존 대상 아님\n' "$name" "$age"
            fi
        fi
    done
}

# retention_check_limits TOTAL CANDIDATES : 0 이면 진행 가능.
retention_check_limits() {
    local total="$1" cands="$2"
    if (( total <= 0 )); then
        echo "ABORT: 파싱된 일자 폴더가 0개" >&2
        return 1
    fi
    if (( cands * 100 > total * RETENTION_MAX_CANDIDATE_PCT )); then
        echo "ABORT: 삭제 후보 ${cands}/${total} 가 ${RETENTION_MAX_CANDIDATE_PCT}% 초과" >&2
        return 1
    fi
    return 0
}

retention_human_kb() {
    awk -v kb="${1:-0}" 'BEGIN {
        if (kb >= 1073741824) printf "%.1fT", kb / 1073741824
        else if (kb >= 1048576) printf "%.1fG", kb / 1048576
        else if (kb >= 1024) printf "%.1fM", kb / 1024
        else printf "%dK", kb
    }'
}

retention_main() {
    set -uo pipefail
    local today="" remote="root@114.207.244.86"
    local ssh_opts='-p 7916 -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=20 -o ServerAliveCountMax=6'
    local remote_root="${AADS_RETENTION_REMOTE_ROOT-$RETENTION_EXPECTED_ROOT}"
    local listing line size name verdict reason rc

    while (( $# > 0 )); do
        case "$1" in
            --today) today="${2:-}"; shift 2 || { echo "usage: $0 [--today YYYYMMDD]" >&2; return 2; } ;;
            *) echo "usage: $0 [--today YYYYMMDD]" >&2; return 2 ;;
        esac
    done
    [[ -n "$today" ]] || today="$(TZ=Asia/Seoul date +%Y%m%d)"
    retention_day_number "$today" >/dev/null || { echo "ABORT: invalid --today: $today" >&2; return 2; }

    if [[ -z "$remote_root" || "$remote_root" != "$RETENTION_EXPECTED_ROOT" ]]; then
        echo "ABORT: REMOTE_ROOT 가 비었거나 예상 경로와 다름: '${remote_root}'" >&2
        return 1
    fi

    if [[ -n "${AADS_RETENTION_LISTING_FILE:-}" ]]; then
        listing="$(cat -- "$AADS_RETENTION_LISTING_FILE")" || { echo "ABORT: listing 파일 읽기 실패" >&2; return 1; }
    else
        # 읽기 전용: du 만 사용. 출력은 "<KiB>\t<폴더명>/" 형식.
        listing="$(timeout 900 ssh $ssh_opts "$remote" "cd '$remote_root' && du -sk -- */" 2>/dev/null)"
        rc=$?
        if [[ -z "$listing" ]]; then
            echo "ABORT: 원격 조회 결과 없음 (ssh/du rc=$rc)" >&2
            return 1
        fi
    fi

    local -A size_kb=()
    local -a names=()
    while IFS=$'\t' read -r size name; do
        [[ "$size" =~ ^[0-9]+$ && -n "$name" ]] || continue
        name="${name%/}"
        size_kb[$name]="$size"
        names+=("$name")
    done <<<"$listing"

    local classified
    classified="$(printf '%s\n' "${names[@]}" | retention_classify "$today")" || return 1

    local total=0 cands=0 cand_kb=0 keep_kb=0 manual_n=0 keep_n=0
    local -a out_keep=() out_cand=() out_manual=()
    while IFS=$'\t' read -r verdict name reason; do
        [[ -n "$verdict" ]] || continue
        size="${size_kb[$name]:-0}"
        line="$(printf '  %-28s %8s  %s' "$name" "$(retention_human_kb "$size")" "$reason")"
        case "$verdict" in
            DELETE_CANDIDATE) total=$((total + 1)); cands=$((cands + 1)); cand_kb=$((cand_kb + size)); out_cand+=("$line") ;;
            MANUAL) manual_n=$((manual_n + 1)); out_manual+=("$line") ;;
            *) total=$((total + 1)); keep_n=$((keep_n + 1)); keep_kb=$((keep_kb + size)); out_keep+=("[$verdict] ${line:1}") ;;
        esac
    done <<<"$classified"

    echo "== 카페24 백업 보관정책 DRY-RUN (삭제 안 함) =="
    echo "기준일: $today / 경로: $remote_root"
    echo "규칙: 일별 ${RETENTION_DAILY_DAYS}일 + 매월 1일 ${RETENTION_MONTHLY_MONTHS}개월 + 최신 1개 항상 보존"

    retention_check_limits "$total" "$cands" || return 1

    echo
    echo "-- 보존 대상 ${keep_n}개 ($(retention_human_kb "$keep_kb")) --"
    printf '%s\n' "${out_keep[@]}"
    echo
    echo "-- 삭제 후보 ${cands}개 ($(retention_human_kb "$cand_kb")) --"
    (( cands > 0 )) && printf '%s\n' "${out_cand[@]}"
    echo
    echo "-- 수동 검토 ${manual_n}개 (후보 아님) --"
    (( manual_n > 0 )) && printf '%s\n' "${out_manual[@]}"
    echo
    echo "요약: 전체 ${total} / 보존 ${keep_n} / 후보 ${cands} / 수동 검토 ${manual_n} / 삭제 실행 0건"
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    retention_main "$@"
    exit $?
fi
