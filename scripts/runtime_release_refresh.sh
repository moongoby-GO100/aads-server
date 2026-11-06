#!/usr/bin/env bash
# aads-server 릴리스 사본 갱신기 (운영 폴더를 실행 경로에서 빼기 — 기획 B안 M2 1단계)
#
# 운영 폴더(RUNTIME_RELEASE_REPO)는 건드리지 않고, 그 저장소의 detached 워크트리인
# 릴리스 사본(RUNTIME_RELEASE_WT)만 push 된 origin/main SHA 로 맞춘다.
# 배포·주기 작업·서비스는 이 사본에서 코드를 실행하고, 상태 파일(.env 등)은
# 절대경로로 운영 폴더에 그대로 둔다.
#
# 운영 폴더에 허용되는 변경은 `git fetch` 의 원격 추적 ref 갱신과 worktree 메타데이터뿐이다.
# 운영 폴더의 작업트리·인덱스·HEAD 는 바꾸지 않는다.
#
# 환경변수 (기본값)
#   RUNTIME_RELEASE_WT     /root/aads/runtime/aads-server-release
#   RUNTIME_RELEASE_REPO   /root/aads/aads-server
#   RUNTIME_RELEASE_SHA    (비면 fetch 후 origin/main)
#   RUNTIME_RELEASE_REMOTE origin
#   RUNTIME_RELEASE_BRANCH main
#   RUNTIME_RELEASE_LOCK   /run/lock/aads-runtime-release.lock
#   RUNTIME_RELEASE_LOG    /var/log/aads-runtime-release.log
#   RUNTIME_RELEASE_STATE  /var/lib/aads/runtime-release.state
#   RUNTIME_RELEASE_FETCH_TIMEOUT 120
#
# 종료코드
#   0 갱신 완료 / 이미 같은 SHA / 잠금 중(DEFER)
#   1 git 작업 실패
#   2 거부 — 목표 SHA 가 틀림(형식 오류·없는 SHA·origin/main 의 조상이 아님 = push 안 됨),
#     또는 사본의 git-dir 이 운영 저장소와 HEAD·인덱스를 공유함(checkout 전에 중단)
#   3 사본이 더러워 바꾸지 않음 (REFRESH_BLOCKED)
#   4 fetch 실패 또는 원격 브랜치 ref 없음
#   5 사본 경로가 안전하지 않음 (운영 폴더 자신·다른 저장소·하위 디렉터리·워크트리 아님)
#
# 이 파일은 사본 안에서 실행되며 자기 자신도 checkout 으로 교체된다. bash 는 스크립트를
# 증분으로 읽으므로 본문을 함수로 감싸고 마지막 줄에서 한 번에 호출한다.

set -uo pipefail

WT="${RUNTIME_RELEASE_WT:-/root/aads/runtime/aads-server-release}"
REPO="${RUNTIME_RELEASE_REPO:-/root/aads/aads-server}"
WANT_SHA="${RUNTIME_RELEASE_SHA:-}"
REMOTE="${RUNTIME_RELEASE_REMOTE:-origin}"
BRANCH="${RUNTIME_RELEASE_BRANCH:-main}"
LOCK="${RUNTIME_RELEASE_LOCK:-/run/lock/aads-runtime-release.lock}"
LOG="${RUNTIME_RELEASE_LOG:-/var/log/aads-runtime-release.log}"
STATE="${RUNTIME_RELEASE_STATE:-/var/lib/aads/runtime-release.state}"
FETCH_TIMEOUT="${RUNTIME_RELEASE_FETCH_TIMEOUT:-120}"

export GIT_TERMINAL_PROMPT=0

log() {
    local line
    line="$(date '+%Y-%m-%dT%H:%M:%S%z') $*"
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "$LOG" 2>/dev/null || true
}

# 훅(post-checkout 등)은 운영 저장소 것을 공유하므로 이 갱신에서는 실행하지 않는다.
G() { git -c core.hooksPath=/dev/null -c advice.detachedHead=false "$@"; }

write_state() {
    local sha="$1" tmp
    mkdir -p "$(dirname "$STATE")" 2>/dev/null || return 0
    tmp="$(mktemp "${STATE}.XXXXXX" 2>/dev/null)" || return 0
    printf 'sha=%s\nupdated_at=%s\n' "$sha" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$tmp" \
        && mv -f "$tmp" "$STATE" || rm -f "$tmp"
}

state_sha() { sed -n 's/^sha=//p' "$STATE" 2>/dev/null | head -n1; }

resolve_target() {
    local fetched=1 full
    if timeout "$FETCH_TIMEOUT" git -C "$REPO" fetch --quiet "$REMOTE" \
        "+refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}" >/dev/null 2>&1; then
        fetched=0
    fi

    if [[ -z "$WANT_SHA" ]]; then
        if [[ $fetched -ne 0 ]]; then
            log "FETCH_FAILED remote=${REMOTE} branch=${BRANCH}"
            return 4
        fi
        full="$(git -C "$REPO" rev-parse --verify --quiet "refs/remotes/${REMOTE}/${BRANCH}^{commit}")" || {
            log "NO_REMOTE_REF ${REMOTE}/${BRANCH}"
            return 4
        }
        TARGET="$full"
        return 0
    fi

    # 명시 SHA(롤백 등)는 fetch 가 실패해도 이미 알고 있는 origin/main 으로 조상 여부를 판정한다.
    [[ $fetched -ne 0 ]] && log "WARN fetch_failed — 로컬 ${REMOTE}/${BRANCH} 로 판정"
    if [[ ! "$WANT_SHA" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
        log "SHA_REJECTED reason=bad_format sha=${WANT_SHA}"
        return 2
    fi
    full="$(git -C "$REPO" rev-parse --verify --quiet "${WANT_SHA}^{commit}")" || {
        log "SHA_REJECTED reason=unknown sha=${WANT_SHA}"
        return 2
    }
    git -C "$REPO" rev-parse --verify --quiet "refs/remotes/${REMOTE}/${BRANCH}^{commit}" >/dev/null || {
        log "NO_REMOTE_REF ${REMOTE}/${BRANCH}"
        return 4
    }
    if ! git -C "$REPO" merge-base --is-ancestor "$full" "refs/remotes/${REMOTE}/${BRANCH}" 2>/dev/null; then
        log "SHA_REJECTED reason=not_pushed sha=${full} (not ancestor of ${REMOTE}/${BRANCH})"
        return 2
    fi
    TARGET="$full"
    return 0
}

# 경로를 realpath 로 정규화한다(없으면 빈 문자열).
real_path() { (cd "$1" 2>/dev/null && pwd -P); }

# git 이 보고하는 디렉터리(상대경로 가능)를 <기준 디렉터리> 기준으로 realpath 화한다.
git_path() {
    local base="$1" opt="$2" out
    out="$(git -C "$base" rev-parse "$opt" 2>/dev/null)" || return 1
    [[ -n "$out" ]] || return 1
    (cd "$base" 2>/dev/null && cd "$out" 2>/dev/null && pwd -P)
}

# 아직 없는 경로도 가장 가까운 기존 조상의 realpath 에 나머지를 붙여 정규화한다(디렉터리를 만들지 않는다).
# 끊어진 심볼릭 링크나 `.`/`..` 조각이 남으면 판정할 수 없으므로 실패시킨다.
future_path() {
    local p="$1" tail="" base
    [[ "$p" == /* ]] || p="$PWD/$p"
    while [[ ! -e "$p" && "$p" != "/" ]]; do
        [[ -L "$p" ]] && return 1
        tail="/${p##*/}$tail"
        p="${p%/*}"
        [[ -n "$p" ]] || p="/"
    done
    case "$tail" in */.|*/./*|*/..|*/../*) return 1 ;; esac
    base="$(cd "$p" 2>/dev/null && pwd -P)" || return 1
    printf '%s%s\n' "${base%/}" "$tail"
}

path_within() { [[ "$1" == "$2" || "$1" == "$2"/* ]]; }

# 사본 위치가 운영 저장소의 작업 트리·git-dir 안쪽이거나 등록된 다른 워크트리 안쪽이면 거부한다(5).
# 신규 생성과 기존 사본 검증이 같은 판정을 쓴다 — 생성 분기가 검증을 건너뛰면 운영 폴더에 `?? 사본/` 이 생긴다.
check_wt_location() {
    local wt_real repo_real top common line listed
    wt_real="$(future_path "$WT")" || {
        log "UNSAFE_WT reason=path_unresolved wt=${WT}"
        return 5
    }
    repo_real="$(real_path "$REPO")" || return 5
    if [[ "$wt_real" == "$repo_real" ]]; then
        log "UNSAFE_WT reason=same_as_repo wt=${WT}"
        return 5
    fi
    top="$(git_path "$REPO" --show-toplevel 2>/dev/null)"
    common="$(git_path "$REPO" --git-common-dir 2>/dev/null)"
    local root
    for root in "$repo_real" "$top" "$common"; do
        if [[ -n "$root" ]] && path_within "$wt_real" "$root"; then
            log "UNSAFE_WT reason=inside_ops_repo wt=${WT} root=${root}"
            return 5
        fi
    done
    while IFS= read -r line; do
        [[ "$line" == "worktree "* ]] || continue
        listed="$(real_path "${line#worktree }")" || continue
        if [[ -n "$listed" && "$listed" != "$wt_real" ]] && path_within "$wt_real" "$listed"; then
            log "UNSAFE_WT reason=inside_registered_worktree wt=${WT} root=${listed}"
            return 5
        fi
    done < <(git -C "$REPO" worktree list --porcelain 2>/dev/null)
    return 0
}

# 사본 경로가 운영 폴더 자신이거나, 다른 저장소이거나, 저장소 안쪽 하위 디렉터리면 거부한다(5).
# 사본이 운영 저장소와 HEAD·인덱스를 공유하면 checkout 전에 거부한다(2).
# 판정은 경로 문자열이 아니라 git 이 보고하는 git-dir 실측으로 한다.
verify_worktree() {
    local wt_real top
    wt_real="$(real_path "$WT")" || return 5
    check_wt_location || return $?
    if [[ ! -f "$WT/.git" || -L "$WT/.git" ]]; then
        log "UNSAFE_WT reason=not_a_worktree wt=${WT}"
        return 5
    fi

    local repo_gd repo_common wt_gd wt_common
    repo_gd="$(git_path "$REPO" --absolute-git-dir)"
    repo_common="$(git_path "$REPO" --git-common-dir)"
    wt_gd="$(git_path "$WT" --absolute-git-dir)"
    wt_common="$(git_path "$WT" --git-common-dir)"
    if [[ -z "$repo_gd" || -z "$repo_common" || -z "$wt_gd" || -z "$wt_common" ]]; then
        log "UNSAFE_WT reason=git_dir_unresolved wt=${WT}"
        return 2
    fi
    if [[ "$wt_common" != "$repo_common" ]]; then
        log "UNSAFE_WT reason=different_repository wt=${WT}"
        return 5
    fi

    # 여기서부터는 같은 저장소다. HEAD·인덱스가 운영 쪽과 겹치면 checkout 이 운영 폴더를 바꾼다.
    # REPO 가 linked worktree 여도 기준은 --git-common-dir(repo_common) 이다.
    if [[ "$wt_gd" == "$repo_gd" ]]; then
        log "UNSAFE_WT reason=shares_repo_git_dir wt=${WT} gitdir=${wt_gd}"
        return 2
    fi
    if [[ "$wt_gd" == "$repo_common" ]]; then
        log "UNSAFE_WT reason=gitdir_is_main_git_dir wt=${WT} gitdir=${wt_gd}"
        return 2
    fi
    if [[ "$(dirname "$wt_gd")" != "$repo_common/worktrees" ]]; then
        log "UNSAFE_WT reason=gitdir_not_under_worktrees wt=${WT} gitdir=${wt_gd} common=${repo_common}"
        return 2
    fi
    local line listed found=1
    while IFS= read -r line; do
        [[ "$line" == "worktree "* ]] || continue
        listed="$(real_path "${line#worktree }")" || continue
        if [[ -n "$listed" && "$listed" == "$wt_real" ]]; then
            found=0
            break
        fi
    done < <(git -C "$REPO" worktree list --porcelain 2>/dev/null)
    if [[ $found -ne 0 ]]; then
        log "UNSAFE_WT reason=not_in_worktree_list wt=${WT}"
        return 2
    fi

    top="$(git -C "$WT" rev-parse --show-toplevel 2>/dev/null)"
    if [[ -z "$top" || "$(real_path "$top")" != "$wt_real" ]]; then
        log "UNSAFE_WT reason=toplevel_mismatch wt=${WT}"
        return 5
    fi
    return 0
}

main() {
    mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")" 2>/dev/null || true
    if ! exec 9>"$LOCK"; then
        log "LOCK_OPEN_FAILED lock=${LOCK}"
        return 1
    fi
    if ! flock -n 9; then
        log "DEFER lock busy ${LOCK}"
        return 0
    fi

    if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
        log "UNSAFE_REPO repo=${REPO}"
        return 5
    fi

    TARGET=""
    resolve_target
    local rc=$?
    [[ $rc -ne 0 ]] && return $rc

    local cur dirty
    if [[ ! -e "$WT" ]] || { [[ -d "$WT" ]] && [[ -z "$(ls -A "$WT" 2>/dev/null)" ]]; }; then
        check_wt_location || return $?
        mkdir -p "$(dirname "$WT")" 2>/dev/null || true
        if ! G -C "$REPO" worktree add --detach --quiet "$WT" "$TARGET" >/dev/null 2>&1; then
            log "REFRESH_FAILED reason=worktree_add wt=${WT} new=${TARGET}"
            return 1
        fi
        log "REFRESHED prev=none new=${TARGET} (created)"
        write_state "$TARGET"
        return 0
    fi

    verify_worktree || return $?

    cur="$(git -C "$WT" rev-parse HEAD 2>/dev/null)"
    if [[ "$cur" == "$TARGET" ]]; then
        if [[ "$(state_sha)" != "$cur" ]]; then
            write_state "$cur"
            log "STATE_SYNC sha=${cur}"
        fi
        return 0
    fi

    dirty="$(G -C "$WT" status --porcelain 2>/dev/null | wc -l)"
    if [[ "$dirty" -ne 0 ]]; then
        log "REFRESH_BLOCKED dirty=${dirty} wt=${WT} cur=${cur} want=${TARGET}"
        return 3
    fi

    if ! G -C "$WT" checkout --detach --quiet "$TARGET" >/dev/null 2>&1 \
        || [[ "$(git -C "$WT" rev-parse HEAD 2>/dev/null)" != "$TARGET" ]]; then
        log "REFRESH_FAILED reason=checkout prev=${cur} want=${TARGET}"
        return 1
    fi
    log "REFRESHED prev=${cur} new=${TARGET}"
    write_state "$TARGET"
    return 0
}

main "$@"
exit $?
