#!/bin/bash
# AADS-146: Worktree 자동 머지 스크립트
# 사용: merge_worktree.sh <group_id> <wt_path1> [wt_path2 ...]
#
# 동작:
#   1) 각 worktree의 변경사항을 main 브랜치로 머지 시도
#   2) 충돌 시 squash merge fallback
#   3) 완료 후 worktree 정리
#   4) 머지 결과 로그 기록

set -euo pipefail

GROUP_ID="${1:?사용법: $0 <group_id> <wt_path...>}"
shift
WTP_PATHS=("$@")
MERGE_LOG="/var/log/aads/worktree_merge.log"
mkdir -p "$(dirname "$MERGE_LOG")" 2>/dev/null || true

_repo="/root/aads/aads-server"

echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_START | group=${GROUP_ID} | worktrees=${#WTP_PATHS[@]}"

for wt_path in "${WTP_PATHS[@]}"; do
    [ -d "$wt_path" ] || continue

    # exit code 확인
    exit_code=0
    if [ -f "${wt_path}.exit" ]; then
        exit_code=$(cat "${wt_path}.exit}" 2>/dev/null || echo "1")
    fi

    # worktree 브랜치 이름 획득
    wt_branch=$(git -C "$wt_path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

    if [ -z "$wt_branch" ] || [ "$wt_branch" = "HEAD" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_SKIP | path=${wt_path} | reason=no_branch"
        continue
    fi

    echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_TRY | branch=${wt_branch} | exit=${exit_code}"

    if [ "$exit_code" != "0" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_SKIP | branch=${wt_branch} | reason=task_failed"
        # 실패한 worktree 정리
        git -C "$_repo" worktree remove --force "$wt_path" 2>/dev/null || rm -rf "$wt_path" 2>/dev/null || true
        git -C "$_repo" branch -D "$wt_branch" 2>/dev/null || true
        rm -f "${wt_path}.exit"
        continue
    fi

    # 메인 브랜치로 머지
    main_branch=$(git -C "$_repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||' || echo "main")

    merge_ok=false
    git -C "$_repo" checkout "$main_branch" 2>/dev/null || true

    # 일반 머지 시도
    if git -C "$_repo" merge --no-ff "$wt_branch" -m "merge(worktree/${GROUP_ID}): ${wt_branch} 병렬작업 완료" 2>/dev/null; then
        merge_ok=true
        echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_OK | branch=${wt_branch}"
    else
        # 충돌 시 abort + squash merge fallback
        git -C "$_repo" merge --abort 2>/dev/null || true
        if git -C "$_repo" merge --squash "$wt_branch" 2>/dev/null; then
            git -C "$_repo" commit -m "squash(worktree/${GROUP_ID}): ${wt_branch} 병렬작업 [squash]" 2>/dev/null && merge_ok=true
            echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_SQUASH | branch=${wt_branch}"
        else
            git -C "$_repo" reset --hard HEAD 2>/dev/null || true
            echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_FAIL | branch=${wt_branch} | reason=conflict"
        fi
    fi

    # worktree 정리
    git -C "$_repo" worktree remove --force "$wt_path" 2>/dev/null || rm -rf "$wt_path" 2>/dev/null || true
    git -C "$_repo" branch -D "$wt_branch" 2>/dev/null || true
    rm -f "${wt_path}.exit"

    if [ "$merge_ok" = "true" ]; then
        # push
        git -C "$_repo" push origin "$main_branch" 2>/dev/null && \
            echo "$(date '+%Y-%m-%d %H:%M:%S') | PUSH_OK | group=${GROUP_ID} | branch=${main_branch}" \
            || echo "$(date '+%Y-%m-%d %H:%M:%S') | PUSH_FAIL | group=${GROUP_ID}"
    fi
done

echo "$(date '+%Y-%m-%d %H:%M:%S') | MERGE_DONE | group=${GROUP_ID}"
