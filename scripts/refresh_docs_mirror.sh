#!/usr/bin/env bash
# 문서 색인 원본 — origin/main 전용 미러를 갱신한다.
#
# 2026-10-02 실측. index_docs.py 가 개발 작업트리(/root/aads/aads-server)를 읽었는데
# 그 트리가 `ahead 4, behind 48` 이라 origin/main 의 신규 문서가 색인되지 않았다.
# 작업트리는 사용자 작업 커밋을 들고 있어 fetch/reset 을 할 수 없다. 그래서 색인은
# 이 미러에서만 읽는다.
#
#   refresh_docs_mirror.sh
#
# 환경변수
#   DOCS_MIRROR_DIR         미러 위치 (기본 /root/aads/mirrors/aads-server)
#   DOCS_MIRROR_ORIGIN_URL  clone 원본. 없으면 개발 작업트리의 .git/config 를 **텍스트로**
#                           읽는다 (git 명령을 그 트리에 실행하지 않는다)
#   DOCS_MIRROR_BRANCH      추적 브랜치 (기본 main)
#   DOCS_MIRROR_TIMEOUT     git 네트워크 작업 상한 초 (기본 300)
#   DOCS_MIRROR_LOCK        flock 파일 (기본 /tmp/refresh_docs_mirror.lock)
#
# 종료코드: 0 갱신 성공 / 1 git 작업 실패 / 2 설정·경로 가드 위반 / 75 이미 실행 중
#
# 이 스크립트의 git 명령은 전부 미러에만 간다. reset --hard / clean -fd 는 작업을 지우는
# 명령이라, 대상이 개발 작업트리이거나 미러가 아닌 곳이면 실행 전에 막는다.
set -uo pipefail

LIVE_TREE="${DOCS_MIRROR_LIVE_TREE:-/root/aads/aads-server}"
MIRROR="${DOCS_MIRROR_DIR:-/root/aads/mirrors/aads-server}"
BRANCH="${DOCS_MIRROR_BRANCH:-main}"
GIT_TIMEOUT="${DOCS_MIRROR_TIMEOUT:-300}"
LOCK_FILE="${DOCS_MIRROR_LOCK:-/tmp/refresh_docs_mirror.lock}"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}"

log() { printf '[refresh_docs_mirror] %s %s\n' "$(date '+%F %T')" "$*"; }

# 미러 안에 두면 git clean 이 지운다 — 바깥에 둔다.
last_ok_file() { printf '%s.last_ok' "${MIRROR%/}"; }

last_ok_sha() {
  local f; f="$(last_ok_file)"
  if [ -s "$f" ]; then cut -d' ' -f1 "$f"; else printf '없음'; fi
}

fail() {  # fail <코드> <메시지>
  local code="$1"; shift
  log "실패: $* (마지막 성공 SHA: $(last_ok_sha))" >&2
  return "$code"
}

# 두 경로가 같거나 한쪽이 다른 쪽 안에 있으면 0
overlaps() {
  local a b
  a="$(realpath -m -- "$1")"; b="$(realpath -m -- "$2")"
  [ "$a" = "$b" ] || [[ "$a/" == "$b/"* ]] || [[ "$b/" == "$a/"* ]]
}

# 미러에만 git 을 실행한다. 호출마다 경로 가드를 다시 확인한다.
mgit() {
  if overlaps "$MIRROR" "$LIVE_TREE"; then
    log "경로 가드: $MIRROR 는 개발 작업트리 $LIVE_TREE 와 겹친다 — git 실행 거부" >&2
    return 2
  fi
  git -C "$MIRROR" "$@"
}

origin_url() {
  if [ -n "${DOCS_MIRROR_ORIGIN_URL:-}" ]; then
    printf '%s' "$DOCS_MIRROR_ORIGIN_URL"; return 0
  fi
  local cfg="$LIVE_TREE/.git/config"
  [ -f "$cfg" ] || return 1
  awk '/^\[remote "origin"\]/{f=1; next} /^\[/{f=0} f && $1=="url"{sub(/^[^=]*=[ \t]*/,""); print; exit}' "$cfg"
}

main() {
  if overlaps "$MIRROR" "$LIVE_TREE"; then
    fail 2 "미러 경로 $MIRROR 가 개발 작업트리 $LIVE_TREE 와 겹친다"; return
  fi

  exec 9>"$LOCK_FILE" || { fail 2 "락 파일 열기 실패: $LOCK_FILE"; return; }
  if ! flock -n 9; then
    log "다른 refresh 가 실행 중이다 — 건너뜀" >&2
    return 75
  fi

  if [ ! -e "$MIRROR" ]; then
    local url
    url="$(origin_url)" || url=""
    if [ -z "$url" ]; then
      fail 2 "clone 원본을 모른다. DOCS_MIRROR_ORIGIN_URL 을 지정하라"; return
    fi
    mkdir -p "$(dirname "$MIRROR")" || { fail 1 "상위 디렉터리 생성 실패"; return; }
    log "미러 최초 생성: $MIRROR"
    if ! timeout "$GIT_TIMEOUT" git clone --no-checkout --branch "$BRANCH" -- "$url" "$MIRROR"; then
      rm -rf -- "$MIRROR"   # 방금 만든 반쪽짜리 clone 이다 (위에서 없음을 확인했다)
      fail 1 "git clone 실패/시간초과"; return
    fi
  fi

  # reset --hard 가 부모 저장소를 때리는 것을 막는다 — 미러는 자기 .git 디렉터리를 가진다.
  if [ ! -d "$MIRROR/.git" ]; then
    fail 2 "$MIRROR 는 독립 git 저장소가 아니다 (.git 디렉터리 없음) — reset/clean 거부"; return
  fi
  local top
  top="$(realpath -m -- "$(mgit rev-parse --show-toplevel 2>/dev/null)")"
  if [ "$top" != "$(realpath -m -- "$MIRROR")" ]; then
    fail 2 "git toplevel($top) 이 미러와 다르다 — reset/clean 거부"; return
  fi

  if ! timeout "$GIT_TIMEOUT" git -C "$MIRROR" fetch --quiet --prune origin "$BRANCH"; then
    fail 1 "git fetch 실패/시간초과"; return
  fi
  if ! mgit reset --hard --quiet "origin/$BRANCH"; then
    fail 1 "git reset --hard origin/$BRANCH 실패"; return
  fi
  if ! mgit clean -fdq; then
    fail 1 "git clean 실패"; return
  fi

  local sha
  sha="$(mgit rev-parse HEAD)" || { fail 1 "HEAD 확인 실패"; return; }
  printf '%s %s\n' "$sha" "$(date '+%F %T')" > "$(last_ok_file)"
  log "성공: origin/$BRANCH = $sha ($MIRROR)"
  return 0
}

# 함수로 감싸 끝까지 파싱한 뒤 실행한다 — 미러 안의 이 파일이 reset 으로 바뀌어도
# 실행 중인 bash 가 읽던 파일이 뒤틀리지 않는다.
main "$@"
exit $?
