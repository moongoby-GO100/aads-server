#!/bin/bash
# ops queue target ACCT/app: release the OBYS/ACCT app IMAGE on cafe24 (server-114) for one release SHA.
#
#   deploy_acct_app_cafe24.sh "<release_sha 40hex>" "<run_id>" ["<marker>"]   build/reuse image -> migrate -> candidate -> cutover -> 5 min watch
#   deploy_acct_app_cafe24.sh rollback "<acct-app-candidate-rN>" ["<run_id>"]  point cafe24 apache back at an existing container
#
# fb.newtalk.kr -> contabo116 nginx (UNCHANGED here) -> https://cafe24 apache -> acct-pg bridge IP:<port> -> app container.
# scripts/deploy_acct_fb_cutover.sh only moves routing; THIS script is the app-image release path. The only
# thing it flips is the upstream PORT inside the cafe24 apache fb vhost (00-zz-fb.newtalk.kr.conf).
#
# Release flow (each step fail-closed; production is untouched until step 6):
#   1 release SHA must be a full 40-hex commit pushed to origin; isolated detached worktree; flock; time budget
#   2 release tree checks: allowlisted migrations tracked/clean, no DROP/TRUNCATE/DELETE, marker present, no secrets,
#     dependency files unchanged vs. the running revision
#   3 image acct-candidate:<sha8> on cafe24, ONCE per SHA: present -> reuse (never rebuilt). Otherwise a thin
#     overlay `FROM <running image id>` + COPY app/migrations/deploy of the release (tracked files only, bounded
#     context, no secrets, revision label overridden).
#   4 acct-pg migrations: fixed allowlist, probe "already applied", pg_dump backup (path printed), apply in a transaction
#   5 candidate acct-app-candidate-r<N+1>: same env/netns/mounts/user as the running one, other port; health + marker
#     check; failure -> candidate removed, exit (production unaffected)
#   6 cutover: apache upstream port swap (configtest -> graceful -> local + public verify), failure -> previous port
#   7 5 min public watch (/health/live, /api/v1/health, index marker); failure -> previous port restored
#
# Static assets (scripts/obys_release_assets.py): when the image is built, the build-context COPY of the obys
# shell HTML gets ?v=<sha8> on every modules/*.js|css ref and sw.js CACHE_VERSION gets <sha8> (repository files
# are never edited). Right after the cutover is verified, the Cloudflare copies of index.html, sw.js and the
# module URLs are removed with purge_cache(files) using /root/.cloudflare_env on cafe24, read at run time. A
# purge problem is a WARNING plus edge_purge in the result JSON, never a failed release; purge_everything is
# never used (same zone as the newtalk.kr shop).
# The previous container is NEVER stopped or removed. Docker mutations are only allowed on acct-app-candidate-r<N>
# names (docker_mut); a before/after snapshot proves every other container is unchanged.
#
# Exit codes: 2 bad args/SHA | 3 already running | 4 release worktree unavailable/dirty/HEAD mismatch
#   5 release content rejected (not pushed, marker/migration/secret check) | 6 image build/reuse/dependency gate
#   7 migration failed | 8 candidate failed (removed) | 9 cutover failed (recovered) | 10 5-min watch failed (recovered)
#   11 unrelated container changed / guard refused | 12 cafe24 state not as expected | 124 time budget | 129/143 HUP/TERM
#
# Recovery by hand (also printed in the result JSON):
#   bash scripts/deploy_acct_app_cafe24.sh rollback acct-app-candidate-r<N> <run_id>
#
# Previous container: ACCT_PREV_CONTAINER=<name> pins it. Unset -> auto-detected from the apache upstream port
# (the single RUNNING acct-app-candidate-r<N> publishing that port; 0 or 2+ matches -> exit 12, never a guess).
# Every later check (netns, upstream ip/port, image id, revision label, WorkingDir) runs the same either way.

set -euo pipefail

# 상태 경로 5개는 '-' 기본값이다: 미설정이면 기본값, 설정됐는데 비어 있으면 validate_state_paths 가 exit 2 로 거부한다.
SOURCE_REPO="${AADS_DEPLOY_SOURCE_REPO:-/root/aads/aads-server}"
REPO="${AADS_DEPLOY_REPO_DIR:-}"
WORKTREE_ROOT="${ACCT_APP_RELEASE_ROOT-/root/aads/state/acct-app-releases}"
RESULT_DIR="${ACCT_APP_RESULT_DIR-/root/aads/state/acct-app-release-results}"
LOCK="${ACCT_APP_LOCK_FILE-/tmp/aads-acct-app-cafe24.lock}"
CAFE24_SSH="${CAFE24_SSH:-server-114}"
FB_HOST="fb.newtalk.kr"
NS_CONTAINER="${NS_CONTAINER:-acct-pg}"
NS_NET="${ACCT_NS_NETWORK:-acct_net}"
[[ $NS_NET =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "bad ACCT_NS_NETWORK '$NS_NET'" >&2; exit 2; }
PG_CONTAINER="${ACCT_PG_CONTAINER:-acct-pg}"
PG_DB="${ACCT_PG_DB:-obys}"
AUTH_PG_DB="${ACCT_AUTH_PG_DB:-obys_auth}" # 인증 DB(saas_users). 업무 DB(PG_DB)와 반드시 다른 DB 여야 한다.
PG_USER="${ACCT_PG_USER:-}"
PG_BACKUP_DIR="${ACCT_PG_BACKUP_DIR-/root/acct-release-backups}"
APACHE_SITE="${ACCT_APACHE_SITE:-/etc/apache2/sites-available/00-zz-fb.newtalk.kr.conf}"
APACHE_BACKUP_DIR="${ACCT_APACHE_BACKUP_DIR-/root/fb-cutover-backups}"
PREV_CONTAINER="${ACCT_PREV_CONTAINER:-}"   # empty -> discover_state picks it from the apache upstream port
PREV_AUTODETECT=false
R8_IMAGE_ID="sha256:71e5e811c220efbe9bc527f36836e0cdc9638794ec01a6b1bbd5256ea5e8150e"
EXPECTED_PREV_IMAGE_ID=""   # resolved by resolve_expected_prev_image_id once PREV_CONTAINER is known
DEFAULT_MARKER="downloadSignedContractPdf"
BUDGET_SECONDS="${DEPLOY_BUDGET_SECONDS:-1380}"   # target timeout_seconds is 1500; leave room for recovery
RECOVERY_RESERVE=90
MONITOR_SECONDS="${MONITOR_SECONDS:-300}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-15}"
MONITOR_FAIL_THRESHOLD="${MONITOR_FAIL_THRESHOLD:-2}"
BUILD_TIMEOUT="${ACCT_BUILD_TIMEOUT:-600}"
HEALTH_WAIT_SECONDS="${ACCT_HEALTH_WAIT_SECONDS:-90}"
CONTEXT_MAX_MB="${ACCT_CONTEXT_MAX_MB:-100}"
# overlay2 의 레이어 상한은 128 이다. 매 릴리스가 직전 이미지 위에 레이어를 쌓으므로 기반이 이 깊이에 닿기 전에 평탄화한다.
FLATTEN_LAYER_THRESHOLD="${ACCT_FLATTEN_LAYER_THRESHOLD:-100}"   # 기반 레이어 수가 이 값 이상이면 평탄화 기반으로 빌드
MAX_IMAGE_LAYERS="${ACCT_MAX_IMAGE_LAYERS:-120}"                 # 같은 SHA 의 기존 이미지가 이 값 이상이면 재사용하지 않고 -f<N> 태그로 재빌드
IMAGE_LAYER_DIE_LIMIT="${ACCT_IMAGE_LAYER_DIE_LIMIT:-125}"       # 후보 시작 전 최종 이미지가 이 값 이상이면 중단(exit 6)
for _v in FLATTEN_LAYER_THRESHOLD MAX_IMAGE_LAYERS IMAGE_LAYER_DIE_LIMIT; do
    [[ ${!_v} =~ ^[1-9][0-9]*$ ]] || { echo "bad $_v '${!_v}' (positive integer required)" >&2; exit 2; }
done
unset _v
SSH_DEFAULT_TIMEOUT="${ACCT_SSH_TIMEOUT:-120}"
# 출구 프록시(deploy/acct-egress). 비어 있으면(기본) 비활성 — 후보 컨테이너 인자는 기존과 같다.
# 예: ACCT_APP_EGRESS_PROXY_URL=http://acct-egress-proxy:8888 (호스트 부분 = 프록시 컨테이너 이름)
EGRESS_PROXY_URL="${ACCT_APP_EGRESS_PROXY_URL:-}"
EGRESS_NO_PROXY_DEFAULT="localhost,127.0.0.1,${NS_CONTAINER}"
[[ $PG_CONTAINER == "$NS_CONTAINER" ]] || EGRESS_NO_PROXY_DEFAULT+=",${PG_CONTAINER}"
EGRESS_NO_PROXY="${ACCT_APP_NO_PROXY:-$EGRESS_NO_PROXY_DEFAULT}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

# Fixed on purpose: nothing from the environment or the command line can add to it.
ACCT_MIGRATION_ALLOWLIST=(
    20261030_obys_work_schedules.sql
    20260930_obys_attendance_pwa_gps.sql
    20261001_obys_hrdoc_expiry_integrity_superseded.sql
    20261003_obys_clobe_collection.sql
    20261008_obys_clobe_mcp_store.sql
    20261014_obys_journal_source_clobe.sql
)
# 인증 DB(AUTH_PG_DB)에만 적용하는 마이그레이션. 업무 DB 목록과 섞지 않는다 — 대상 DB 가 다르기 때문이다.
ACCT_AUTH_MIGRATION_ALLOWLIST=(
    20261014_obys_auth_password_recovery.sql
)
BUILD_PATHS=(app migrations deploy)
# 운영 이미지에서 뺄 옛 시안. 저장소 파일은 그대로 두고 (1) git archive 에서 제외하고 (2) 기반 이미지에 남은 사본을 RUN rm 으로 지운다.
# 되돌리기 = 아래 목록에서 해당 줄을 삭제한다(다음 릴리스부터 다시 배포됨).
# 절대 넣지 말 것: mockup-v4-1.html(V4.1 운영 화면), mockup-employee-contract-v1.html, mockup-signup-v1.html, index.html.
RELEASE_EXCLUDE_PATHS=(
    app/static/apps/obys/mockup-v2.html
    app/static/apps/obys/mockup-v3.html
    app/static/apps/obys/mockup-v4.html
    app/static/apps/obys/mockup-final.html
)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSET_TOOL="${ACCT_ASSET_TOOL:-$SCRIPT_DIR/obys_release_assets.py}"
CF_ENV_FILE="${ACCT_CF_ENV_FILE:-/root/.cloudflare_env}"
OBYS_STATIC_REL="app/static/apps/obys"
CANDIDATE_RE='^acct-app-candidate-r[0-9]+$'

RUN_ID="0"
RELEASE_SHA=""
SHA8=""
MARKER="$DEFAULT_MARKER"
IMAGE_TAG=""
IMAGE_ID=""
IMAGE_REUSED=false
BASE_LAYERS=""
FLATTENED=false
IMAGE_LAYERS=""
BT=0
NEW_CONTAINER=""
NEW_PORT=""
OLD_PORT=""
UP_IP=""
PREV_IMAGE_ID=""
PREV_REV=""
BASE_REV_KEYS=()
PG_BACKUP_PATH=""
APACHE_BACKUP=""
CUTOVER_KST=""
CANDIDATE_CREATED=0
CUTOVER_ATTEMPTED=0
RECOVERED=0
COMPLETED=0
MIGRATIONS_APPLIED=()
MIGRATIONS_SKIPPED=()
ASSET_VERSION=""
ASSET_STAMP="not_run"
PURGE_STATUS="not_run"
PURGE_JSON=""
PURGE_RUNS=0
PURGE_URLS=()
UNRELATED_CHANGED=""
SNAPSHOT_BEFORE=""
release_wt=""
BUILD_CTX=""
MODE="deploy"
declare -a CAND_LINES=()

say() { printf '[acct-app-cafe24 run=%s] %s\n' "$RUN_ID" "$*"; }
die() { local rc=$1; shift; printf '[acct-app-cafe24 run=%s] FAIL: %s\n' "$RUN_ID" "$*" >&2; exit "$rc"; }
kst() { TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST'; }
left() { echo $((BUDGET_SECONDS - SECONDS)); }
need_budget() { # need_budget <seconds> <what>
    (( $(left) >= $1 )) || die 124 "time budget exhausted before: $2 (left $(left)s, need ${1}s)"
}

# ---- remote primitives (tests replace ssh_c / pub_get_* / sleep) --------------------------------
ssh_c() {
    local t="${SSH_CALL_TIMEOUT:-$SSH_DEFAULT_TIMEOUT}"
    timeout -k 10 "$t" ssh "${SSH_OPTS[@]}" "$CAFE24_SSH" "$@"
}
rcmd() { ssh_c "$(printf '%q ' "$@")"; }
rdocker() { rcmd docker "$@"; }
rscript() { # rscript <op-tag> <script-variable-name> args...   (script text goes over stdin to `bash -s`)
    local op=$1 body=$2
    shift 2
    ssh_c "ACCT_REMOTE_OP=$op $(printf '%q ' bash -s -- "$@")" <<<"${!body}"
}
pub_get_body() { curl -s -m 10 -H 'Cache-Control: no-cache' "$1"; }
pub_get_code() { curl -s -m 10 -o /dev/null -w '%{http_code}' -H 'Cache-Control: no-cache' "$1"; }

# Only acct-app-candidate-r<N> may be mutated; the running (previous) container may never be stopped/removed.
owned_name() { [[ ${1:-} =~ $CANDIDATE_RE ]]; }
docker_mut() { # docker_mut <rm|stop|start> [-f] <container>
    local verb=$1 name
    shift
    [[ $1 == -f ]] && shift
    name=${1:-}
    case $verb in rm | stop | start) ;; *)
        say "docker_mut: verb '$verb' not allowed"
        return 11
        ;;
    esac
    if ! owned_name "$name"; then
        say "docker_mut: refusing '$verb $name' (only acct-app-candidate-r<N> may be changed)"
        return 11
    fi
    if [[ $verb != start && -z $PREV_CONTAINER ]]; then
        say "docker_mut: refusing '$verb $name' before the previous container is known"
        return 11
    fi
    if [[ $verb != start && $name == "$PREV_CONTAINER" ]]; then
        say "docker_mut: refusing to $verb the previous container $name"
        return 11
    fi
    case $verb in
        rm) rdocker rm -f "$name" >/dev/null ;;
        stop) rdocker stop "$name" >/dev/null ;;
        start) rdocker start "$name" >/dev/null ;;
    esac
}

# ---- state directory guard ----------------------------------------------------------------------
# 환경변수로 바뀔 수 있는 상태 경로가 비었거나 상대경로이거나 '.'/'..' 를 품거나 시스템 루트 자체이면 거부한다.
# 거부는 mkdir·flock·원격 호출보다 앞선다(exit 2). 이미 존재하는 경로 구성요소 중 하나라도 심볼릭 링크이면(마지막 요소뿐 아니라 상위 디렉터리 포함)
# 거부한다 — mkdir -p·rm -rf·flock 이 링크를 따라 밖으로 나가는 것을 막는다.
normalize_abs_path() { # collapse repeated '/', drop a trailing '/'; '/' stays '/'
    local p=$1
    while [[ $p == *//* ]]; do p=${p//\/\//\/}; done
    [[ $p == / ]] || p=${p%/}
    printf '%s' "${p:-/}"
}
unsafe_state_path() { # unsafe_state_path <value>: prints the reason and returns 0 when the path must be refused
    local v=$1 n d
    [[ -n $v ]] || { echo "empty"; return 0; }
    [[ $v == /* ]] || { echo "not an absolute path"; return 0; }
    [[ $v =~ [[:cntrl:][:space:]] ]] && { echo "contains whitespace or control characters"; return 0; }
    [[ /$v/ == */../* || /$v/ == */./* ]] && { echo "contains a '.' or '..' component"; return 0; }
    n="$(normalize_abs_path "$v")"
    local acc="" rest=${n#/} part
    while [[ -n $rest ]]; do
        part=${rest%%/*}
        if [[ $rest == */* ]]; then rest=${rest#*/}; else rest=""; fi
        acc="$acc/$part"
        [[ -L $acc ]] && { echo "contains an existing symbolic link ($acc)"; return 0; }
    done
    for d in / /root /etc /usr /var /tmp /home /bin /boot /dev /lib /lib64 /opt /proc /run /sbin /srv /sys /mnt ${HOME:+"$(normalize_abs_path "$HOME")"}; do
        [[ $n == "$d" ]] && { echo "is the system directory $d itself"; return 0; }
    done
    return 1
}
validate_state_paths() { # validate_state_paths <VAR_NAME>...
    local name why
    for name in "$@"; do
        if why="$(unsafe_state_path "${!name}")"; then
            die 2 "unsafe $name '${!name}': $why"
        fi
    done
}
path_is_under() { # path_is_under <path> <root>: strictly below root, no '.'/'..' components
    local p root
    p="$(normalize_abs_path "$1")"
    root="$(normalize_abs_path "$2")"
    [[ $p == /* && $root != / && $p == "$root"/?* ]] || return 1
    [[ /$p/ == */../* || /$p/ == */./* ]] && return 1
    return 0
}

# ---- release worktree ---------------------------------------------------------------------------
is_release_wt_tempdir() { # make_release_worktree 의 mktemp 결과(<WORKTREE_ROOT>/<소문자 12hex>.<6자>)만 true. 링크는 아님.
    local p root
    p="$(normalize_abs_path "$1")"
    root="$(normalize_abs_path "$2")"
    path_is_under "$p" "$root" || return 1
    [[ ${p%/*} == "$root" && ${p##*/} =~ ^[0-9a-f]{12}\.[A-Za-z0-9]{6}$ && ! -L $p ]]
}
cleanup_release_wt() {
    [[ -n "$release_wt" ]] || return 0
    if git -C "$SOURCE_REPO" worktree remove --force "$release_wt" >/dev/null 2>&1; then
        :
    elif is_release_wt_tempdir "$release_wt" "$WORKTREE_ROOT"; then
        rm -rf "$release_wt"
    else
        say "WARNING: not removing '$release_wt': not a release worktree directory directly below $WORKTREE_ROOT"
    fi
    git -C "$SOURCE_REPO" worktree prune >/dev/null 2>&1 || true
    release_wt=""
}

make_release_worktree() { # sets REPO: isolated detached worktree at exactly the full release SHA
    local full="$RELEASE_SHA"
    git -C "$SOURCE_REPO" rev-parse --git-dir >/dev/null 2>&1 || die 4 "source repository $SOURCE_REPO unavailable"
    if ! git -C "$SOURCE_REPO" cat-file -e "${full}^{commit}" 2>/dev/null; then
        timeout 120 git -C "$SOURCE_REPO" fetch --no-tags -q origin '+refs/heads/*:refs/remotes/origin/*' >/dev/null 2>&1 || true
    fi
    git -C "$SOURCE_REPO" cat-file -e "${full}^{commit}" 2>/dev/null || die 4 "release ${full:0:12} not found in $SOURCE_REPO even after fetching origin"
    if [[ -z "$(git -C "$SOURCE_REPO" branch -r --contains "$full" 2>/dev/null)" ]]; then
        timeout 120 git -C "$SOURCE_REPO" fetch --no-tags -q origin '+refs/heads/*:refs/remotes/origin/*' >/dev/null 2>&1 || true
        [[ -n "$(git -C "$SOURCE_REPO" branch -r --contains "$full" 2>/dev/null)" ]] \
            || die 5 "release ${full:0:12} is not pushed to any origin branch"
    fi
    mkdir -p "$WORKTREE_ROOT" || die 4 "cannot create $WORKTREE_ROOT"
    release_wt="$(mktemp -d "$WORKTREE_ROOT/${full:0:12}.XXXXXX")" || die 4 "cannot create release worktree dir"
    if ! git -C "$SOURCE_REPO" worktree add --detach -q "$release_wt" "$full" >/dev/null 2>&1; then
        cleanup_release_wt
        die 4 "git worktree add failed for ${full:0:12}"
    fi
    REPO="$release_wt"
    say "isolated release worktree ${full:0:12} at $REPO (source $SOURCE_REPO untouched)"
}

# ---- release tree checks ------------------------------------------------------------------------
migration_allowed() { # exact name from the fixed allowlist only
    local n=$1 m
    for m in "${ACCT_MIGRATION_ALLOWLIST[@]}" "${ACCT_AUTH_MIGRATION_ALLOWLIST[@]}"; do
        [[ $m == "$n" ]] && return 0
    done
    return 1
}

migration_db() { # migration_db <name>: 마이그레이션이 적용되는 DB 이름
    local n=$1 m
    for m in "${ACCT_AUTH_MIGRATION_ALLOWLIST[@]}"; do
        [[ $m == "$n" ]] && { printf '%s' "$AUTH_PG_DB"; return 0; }
    done
    printf '%s' "$PG_DB"
}

migration_scan() { # fail-closed: transaction wrapper required, no DROP/TRUNCATE/DELETE (comments stripped; DROP CONSTRAINT is data-neutral and allowed)
    python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
text = re.sub(r"--[^\n]*", " ", text)
text = re.sub(r"\bDROP\s+CONSTRAINT\b", " ", text, flags=re.I)
bad = []
if re.search(r"\bDROP\b", text, re.I):
    bad.append("DROP")
if re.search(r"\bTRUNCATE\b", text, re.I):
    bad.append("TRUNCATE")
if re.search(r"\bDELETE\s+FROM\b", text, re.I):
    bad.append("DELETE FROM")
if not re.search(r"^\s*BEGIN\s*;", text, re.I | re.M):
    bad.append("missing BEGIN;")
if not re.search(r"^\s*COMMIT\s*;", text, re.I | re.M):
    bad.append("missing COMMIT;")
if bad:
    print("rejected: " + ", ".join(bad))
    sys.exit(1)
PY
}

validate_release() {
    local head m path hit
    head="$(git -C "$REPO" rev-parse HEAD)"
    [[ $head == "$RELEASE_SHA" ]] || die 4 "release worktree HEAD ${head:0:12} != release ${RELEASE_SHA:0:12}"
    [[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]] || die 4 "release worktree has uncommitted tracked changes"
    for m in "${ACCT_MIGRATION_ALLOWLIST[@]}" "${ACCT_AUTH_MIGRATION_ALLOWLIST[@]}"; do
        path="migrations/$m"
        git -C "$REPO" ls-files --error-unmatch "$path" >/dev/null 2>&1 || die 5 "allowlisted migration $path is not tracked at ${RELEASE_SHA:0:12}"
        migration_scan "$REPO/$path" || die 5 "migration $m failed the safety scan"
    done
    grep -qF -- "$MARKER" "$REPO/app/static/apps/obys/index.html" 2>/dev/null \
        || die 5 "marker '$MARKER' is not in app/static/apps/obys/index.html of the release"
    local tok_re='sk-''ant-(oat01|api03)-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY'
    hit="$(git -C "$REPO" grep -I -l -E "$tok_re" HEAD -- "${BUILD_PATHS[@]}" 2>/dev/null || true)"
    [[ -z $hit ]] || die 5 "secret-like token in build context paths: $hit"
    hit="$(git -C "$REPO" ls-tree -r --name-only HEAD -- "${BUILD_PATHS[@]}" 2>/dev/null | grep -E '(^|/)\.env|\.pem$|\.key$|(^|/)id_rsa' || true)"
    [[ -z $hit ]] || die 5 "secret-like file in build context paths: $hit"
}

# ---- cafe24 remote scripts (`bash -s`) ----------------------------------------------------------
read -r -d '' R_PREV_INFO <<'EOS' || true
set -u
PREV=$1; NS=$2; NS_NET=${3:-acct_net}
echo "PREV_RUNNING=$(docker inspect -f '{{.State.Running}}' "$PREV" 2>/dev/null || echo missing)"
img=$(docker inspect -f '{{.Image}}' "$PREV" 2>/dev/null || true)
echo "PREV_IMAGE_ID=$img"
echo "PREV_NETMODE=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$PREV" 2>/dev/null || true)"
echo "WORKDIR=$(docker inspect -f '{{.Config.WorkingDir}}' "$PREV" 2>/dev/null || true)"
echo "REV_LABELS=$(docker image inspect -f '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' "$img" 2>/dev/null | grep -E '^[^=]*revision=' | tr '\n' ';')"
echo "NS_ID=$(docker inspect -f '{{.Id}}' "$NS" 2>/dev/null || true)"
echo "NS_RUNNING=$(docker inspect -f '{{.State.Running}}' "$NS" 2>/dev/null || echo missing)"
echo "NS_IP=$(docker inspect -f "{{with index .NetworkSettings.Networks \"$NS_NET\"}}{{.IPAddress}}{{end}}" "$NS" 2>/dev/null | awk '{print $1}')"
echo "NS_IPS=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$NS" 2>/dev/null | xargs)"
echo "NS_NETWORKS=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' "$NS" 2>/dev/null | xargs)"
pub=0; lo=0
while IFS= read -r pl; do
  [[ -n ${pl//[[:space:]]/} ]] || continue
  addr="${pl##*-> }"
  [[ $pl == *"-> "* && $addr == *:* ]] || { pub=$((pub + 1)); continue; }
  host="${addr%:*}"; host="${host#[}"; host="${host%]}"
  case $host in
    127.0.0.1 | ::1) lo=$((lo + 1)) ;;
    *) pub=$((pub + 1)) ;;
  esac
done < <(docker port "$NS" 2>/dev/null || true)
echo "NS_PUBLISHED=$pub"
echo "NS_LOOPBACK=$lo"
echo "PG_USER=$(docker exec "$NS" printenv POSTGRES_USER 2>/dev/null || true)"
EOS

read -r -d '' R_LIST_CANDIDATES <<'EOS' || true
set -u
for n in $(docker ps -a --format '{{.Names}}' | grep -E '^acct-app-candidate-r[0-9]+$' | sort); do
  run=$(docker inspect -f '{{.State.Running}}' "$n" 2>/dev/null)
  words=$(docker inspect -f '{{range .Config.Entrypoint}}{{.}} {{end}}{{range .Config.Cmd}}{{.}} {{end}}' "$n" 2>/dev/null)
  port=$(grep -oE -e '--port[= ][0-9]+' <<<"$words" | head -1 | grep -oE '[0-9]+$' || true)
  img=$(docker inspect -f '{{.Image}}' "$n" 2>/dev/null)
  rel=$(docker inspect -f '{{index .Config.Labels "acct.release.sha"}}' "$n" 2>/dev/null)
  echo "$n|$run|$port|$img|$rel"
done
EOS

read -r -d '' R_SNAPSHOT <<'EOS' || true
docker ps -aq | xargs -r docker inspect -f '{{.Name}} {{.Id}} {{.State.Running}} {{.State.StartedAt}}' | sort
EOS

read -r -d '' R_PG_BACKUP <<'EOS' || true
set -euo pipefail
c=$1; u=$2; d=$3; dir=$4; label=$5
umask 077
p=$dir
while [[ $p == *//* ]]; do p=${p//\/\//\/}; done
while [[ $p == */ && $p != / ]]; do p=${p%/}; done
while [[ $p == /?* ]]; do
  [[ ! -L $p ]] || { echo "refusing symlinked backup dir component $p of $dir" >&2; exit 2; }
  p=${p%/*}
done
mkdir -p "$dir"
f="$dir/${d}_${label}_$(date +%Y%m%d_%H%M%S).dump"
docker exec "$c" pg_dump -Fc -U "$u" -d "$d" > "$f"
[[ -s $f ]] || { rm -f "$f"; echo "empty pg_dump output" >&2; exit 1; }
docker exec -i "$c" pg_restore -l < "$f" >/dev/null || { echo "pg_restore -l cannot read $f" >&2; exit 1; }
echo "BACKUP_PATH=$f"
echo "BACKUP_BYTES=$(stat -c %s "$f")"
EOS

read -r -d '' R_START_CANDIDATE <<'EOS' || true
set -euo pipefail
NEW=$1; PREV=$2; NS=$3; IMAGE=$4; PORT=$5; OLDPORT=$6; SHA=$7; RUN=$8
[[ $NEW =~ ^acct-app-candidate-r[0-9]+$ ]] || { echo "refusing container name $NEW"; exit 90; }
[[ $NEW != "$PREV" ]] || { echo "new container equals previous"; exit 90; }
if docker inspect "$NEW" >/dev/null 2>&1; then echo "$NEW already exists"; exit 91; fi
umask 077
envf=$(mktemp /dev/shm/acct-app-env.XXXXXX 2>/dev/null || mktemp)
trap 'rm -f "$envf"' EXIT
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PREV" | grep -v -e '^APP_PORT=' -e '^$' > "$envf" || true
echo "APP_PORT=$PORT" >> "$envf"
# 출구 프록시(선택). $9=프록시 URL, $10=NO_PROXY. 비어 있으면 PREV 의 환경을 그대로 둔다.
egress_proxy=${9:-}; egress_noproxy=${10:-}
if [[ -n $egress_proxy ]]; then
  [[ $egress_proxy =~ ^http://([a-z0-9][a-z0-9._-]*):[0-9]{2,5}$ ]] || { echo "refusing egress proxy '$egress_proxy'"; exit 94; }
  proxy_host=${BASH_REMATCH[1]}
  [[ $egress_noproxy =~ ^[A-Za-z0-9._:,-]+$ ]] || { echo "refusing NO_PROXY '$egress_noproxy'"; exit 94; }
  [[ $(docker inspect -f '{{.State.Running}}' "$proxy_host" 2>/dev/null) == true ]] \
    || { echo "egress proxy container $proxy_host is not running"; exit 94; }
  grep -v -e '^HTTPS_PROXY=' -e '^HTTP_PROXY=' -e '^NO_PROXY=' -e '^https_proxy=' -e '^http_proxy=' -e '^no_proxy=' "$envf" > "$envf.new" || true
  mv "$envf.new" "$envf"
  printf '%s\n' "HTTPS_PROXY=$egress_proxy" "HTTP_PROXY=$egress_proxy" "NO_PROXY=$egress_noproxy" \
    "https_proxy=$egress_proxy" "http_proxy=$egress_proxy" "no_proxy=$egress_noproxy" >> "$envf"
fi
# 클로브 수집용 추가 설정(예: OBYS_VAULT_KEY). 값은 저장소·스크립트·로그 어디에도 없고 카페24 root 전용 파일에만 있다.
# 경로는 고정, 파일은 root:600 이어야 하며, 아래 이름만 받는다(PREV 의 같은 이름 값을 덮어쓴다).
extra=/root/acct-app-extra.env
if [[ -f $extra ]]; then
  [[ $(stat -c '%a %U' "$extra") == "600 root" ]] || { echo "refusing $extra: must be mode 600 owned by root"; exit 93; }
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    k=${line%%=*}
    case $k in
      OBYS_VAULT_KEY|OBYS_PUBLIC_BASE_URL|CLOBE_OAUTH_REDIRECT_URI|CLOBE_STORE_DATABASE_URL) ;;
      *) echo "refusing $extra: key '$k' is not allowed"; exit 93 ;;
    esac
    [[ $line == "$k="?* ]] || { echo "refusing $extra: '$k' has no value"; exit 93; }
    grep -v "^$k=" "$envf" > "$envf.new" || true
    mv "$envf.new" "$envf"
    printf '%s\n' "$line" >> "$envf"
  done < "$extra"
fi
args=(run -d --name "$NEW" --network "container:$NS" --env-file "$envf"
      --label "acct.release.sha=$SHA" --label "acct.release.run=$RUN" --label "acct.release.prev=$PREV")
rp=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$PREV")
[[ -z $rp || $rp == no ]] || args+=(--restart "$rp")
user=$(docker inspect -f '{{.Config.User}}' "$PREV")
[[ -z $user ]] || args+=(--user "$user")
mem=$(docker inspect -f '{{.HostConfig.Memory}}' "$PREV")
[[ -z $mem || $mem == 0 ]] || args+=(--memory "$mem")
while IFS='|' read -r typ src dst rw; do
  [[ -n $typ ]] || continue
  if [[ $typ == tmpfs ]]; then args+=(--tmpfs "$dst"); continue; fi
  m="type=$typ,source=$src,target=$dst"
  [[ $rw == false ]] && m+=",readonly"
  args+=(--mount "$m")
done < <(docker inspect -f '{{range .Mounts}}{{.Type}}|{{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}|{{.Destination}}|{{.RW}}{{println}}{{end}}' "$PREV")
words=()
while IFS= read -r w; do [[ -n $w ]] && words+=("$w"); done < <(docker inspect -f '{{range .Config.Entrypoint}}{{println .}}{{end}}{{range .Config.Cmd}}{{println .}}{{end}}' "$PREV")
hasep=$(docker inspect -f '{{len .Config.Entrypoint}}' "$PREV")
if [[ $hasep != 0 ]]; then args+=(--entrypoint "${words[0]}"); words=("${words[@]:1}"); fi
out=(); prev=""; n=0
for w in "${words[@]}"; do
  if [[ $prev == --port && $w == "$OLDPORT" ]]; then w=$PORT; n=$((n+1))
  elif [[ $w == "--port=$OLDPORT" ]]; then w="--port=$PORT"; n=$((n+1)); fi
  out+=("$w"); prev=$w
done
[[ $n == 1 ]] || { echo "cannot rewrite --port $OLDPORT in the previous command (matches: $n)"; exit 92; }
docker "${args[@]}" "$IMAGE" "${out[@]}"
EOS

read -r -d '' R_APACHE_UPSTREAM <<'EOS' || true
set -u
site=$1
# 주석 줄은 읽지 않는다 — 설치본 vhost 9행 주석에 'http://<ip>:8111' 예시가 남아 있어 두 포트로 오판했다(2026-10-06 run 5590).
ups=$(grep -vE '^[[:space:]]*#' "$site" | grep -oE 'http://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+' | sort -u)
[[ -n $ups && $(wc -l <<<"$ups") == 1 ]] || { echo "ambiguous or missing upstream in $site: $ups" >&2; exit 1; }
sed -E 's#http://([0-9.]+):([0-9]+)#\1 \2#' <<<"$ups"
EOS

read -r -d '' R_APACHE_SWITCH <<'EOS' || true
set -uo pipefail
SITE=$1; FROM=$2; TO=$3; BK=$4; MARKER=$5; FB=$6
CTL=${ACCT_APACHECTL:-apache2ctl}
p=$BK
while [[ $p == *//* ]]; do p=${p//\/\//\/}; done
while [[ $p == */ && $p != / ]]; do p=${p%/}; done
while [[ $p == /?* ]]; do
  [[ ! -L $p ]] || { echo "FAIL refusing symlinked backup dir component $p of $BK"; exit 1; }
  p=${p%/*}
done
mkdir -p "$BK"
ts=$(date +%Y%m%d_%H%M%S)
bk="$BK/$(basename "$SITE").app-release.$ts"
cp -p "$SITE" "$bk" || { echo "FAIL backup of $SITE"; exit 1; }
echo "APACHE_BACKUP=$bk"
restore() {
  echo "ROLLBACK apache to $bk"
  cat "$bk" > "$SITE"
  "$CTL" configtest 2>&1 && "$CTL" graceful
}
ups=$(grep -vE '^[[:space:]]*#' "$SITE" | grep -oE 'http://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+' | sort -u)
[[ $(wc -l <<<"$ups") == 1 && $ups == *":$FROM" ]] || { echo "FAIL upstream is '$ups', expected port $FROM; nothing changed"; exit 1; }
n_from=$(grep -cE "://[0-9.]+:$FROM/" "$SITE")
[[ $n_from -ge 1 ]] || { echo "FAIL no upstream lines with port $FROM"; exit 1; }
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
sed -E "s#(://[0-9.]+):$FROM/#\1:$TO/#g" "$SITE" > "$tmp"
n_to=$(grep -cE "://[0-9.]+:$TO/" "$tmp")
left=$(grep -cE "://[0-9.]+:$FROM/" "$tmp")
[[ $n_to == "$n_from" && $left == 0 ]] || { echo "FAIL rewrite mismatch (from=$n_from to=$n_to left=$left); nothing changed"; exit 1; }
declare -A base
for h in pick.newtalk.kr shotflow.newtalk.kr v2.newtalk.kr; do
  base[$h]=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h/")
done
cat "$tmp" > "$SITE"
"$CTL" configtest 2>&1 || { restore; exit 1; }
"$CTL" graceful || { restore; exit 1; }
sleep 2
vf=0
body=$(curl -s -m 15 --resolve "$FB:443:127.0.0.1" "https://$FB/health/live")
[[ $body == *'"status":"ok"'* ]] && echo "OK   health/live" || { echo "FAIL health/live: $body"; vf=1; }
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' --resolve "$FB:443:127.0.0.1" "https://$FB/api/v1/health")
[[ $code == 200 || $code == 401 ]] && echo "OK   api/v1/health $code" || { echo "FAIL api/v1/health $code"; vf=1; }
if [[ $MARKER != - ]]; then
  idx=$(curl -s -m 15 --resolve "$FB:443:127.0.0.1" "https://$FB/static/apps/obys/index.html")
  [[ $idx == *"$MARKER"* ]] && echo "OK   index marker $MARKER" || { echo "FAIL index marker $MARKER missing"; vf=1; }
fi
for h in "${!base[@]}"; do
  now=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' --resolve "$h:443:127.0.0.1" "https://$h/")
  [[ $now == "${base[$h]}" ]] && echo "OK   baseline $h ${base[$h]}" || { echo "FAIL baseline $h ${base[$h]} -> $now"; vf=1; }
done
[[ $vf == 0 ]] || { restore; exit 1; }
echo "APPLIED upstream port $FROM -> $TO backup=$bk"
EOS

read -r -d '' R_APACHE_RESTORE <<'EOS' || true
set -uo pipefail
SITE=$1; BK=$2
ctl=${ACCT_APACHECTL:-apache2ctl}
[[ -f $BK ]] || { echo "FAIL backup $BK missing"; exit 1; }
cat "$BK" > "$SITE"
"$ctl" configtest 2>&1 && "$ctl" graceful
EOS

# ---- cafe24 state discovery ---------------------------------------------------------------------
declare -A INFO=()
load_prev_info() {
    local out k v
    out="$(rscript prev_info R_PREV_INFO "$PREV_CONTAINER" "$NS_CONTAINER" "$NS_NET")" || die 12 "cannot read $PREV_CONTAINER state on $CAFE24_SSH"
    INFO=()
    while IFS='=' read -r k v; do
        [[ -n $k ]] && INFO[$k]="$v"
    done <<<"$out"
}

parse_rev_labels() { # REV_LABELS=key=val;key=val;
    local item key val
    BASE_REV_KEYS=()
    PREV_REV=""
    IFS=';' read -ra items <<<"${INFO[REV_LABELS]:-}"
    for item in "${items[@]}"; do
        [[ -n $item ]] || continue
        key="${item%%=*}"
        val="${item#*=}"
        BASE_REV_KEYS+=("$key")
        if [[ -z $PREV_REV || $key == org.opencontainers.image.revision ]]; then PREV_REV="$val"; fi
    done
}

list_candidates() {
    local out
    out="$(rscript list_candidates R_LIST_CANDIDATES)" || die 12 "cannot list acct-app-candidate containers"
    CAND_LINES=()
    while IFS= read -r line; do
        [[ -n $line ]] && CAND_LINES+=("$line")
    done <<<"$out"
}

apache_upstream() { # prints "<ip> <port>"
    local out
    out="$(rscript apache_upstream R_APACHE_UPSTREAM "$APACHE_SITE")" || return 1
    [[ $out =~ ^[0-9.]+\ [0-9]+$ ]] || return 1
    printf '%s\n' "$out"
}

cand_field() { # cand_field <name> <1-based field>   (name|running|port|image|relsha)
    local line
    for line in "${CAND_LINES[@]}"; do
        if [[ ${line%%|*} == "$1" ]]; then
            cut -d'|' -f"$2" <<<"$line"
            return 0
        fi
    done
    return 1
}

detect_prev_container() { # needs CAND_LINES + OLD_PORT; sets PREV_CONTAINER
    local line name run port matches=() listing=""
    for line in "${CAND_LINES[@]}"; do
        IFS='|' read -r name run port _ <<<"$line"
        listing+="${listing:+, }${name}:${port}:${run}"
        if [[ $run == true && $port == "$OLD_PORT" ]]; then matches+=("$name"); fi
    done
    (( ${#matches[@]} == 1 )) \
        || die 12 "cannot auto-detect the previous container: ${#matches[@]} running candidate(s) publish apache upstream port $OLD_PORT (candidates name:port:running = ${listing:-none}); set ACCT_PREV_CONTAINER to the container to keep for rollback"
    PREV_CONTAINER="${matches[0]}"
    PREV_AUTODETECT=true
    say "previous container auto-detected from apache upstream port $OLD_PORT: $PREV_CONTAINER"
}

resolve_expected_prev_image_id() {
    if [[ -n ${ACCT_EXPECTED_PREV_IMAGE_ID+x} ]]; then
        EXPECTED_PREV_IMAGE_ID="$ACCT_EXPECTED_PREV_IMAGE_ID"
    elif [[ $PREV_CONTAINER == acct-app-candidate-r8 ]]; then
        EXPECTED_PREV_IMAGE_ID="$R8_IMAGE_ID"
    else
        EXPECTED_PREV_IMAGE_ID=""
    fi
}

discover_state() {
    local up cur_ip cand_port cand_img nm extra_nets item
    up="$(apache_upstream)" || die 12 "cannot read the fb vhost upstream from $APACHE_SITE"
    read -r UP_IP OLD_PORT <<<"$up"
    list_candidates
    if [[ -z $PREV_CONTAINER ]]; then detect_prev_container; fi
    resolve_expected_prev_image_id
    load_prev_info
    [[ ${INFO[PREV_RUNNING]:-} == true ]] || die 12 "previous container $PREV_CONTAINER is not running"
    [[ ${INFO[NS_RUNNING]:-} == true ]] || die 12 "$NS_CONTAINER is not running"
    [[ ${INFO[NS_ID]:-} != "" && ${INFO[PREV_NETMODE]:-} == "container:${INFO[NS_ID]}" ]] \
        || die 12 "$PREV_CONTAINER network mode '${INFO[PREV_NETMODE]:-}' is not container:$NS_CONTAINER"
    [[ ${INFO[NS_PUBLISHED]:-1} == 0 ]] || die 12 "$NS_CONTAINER publishes host ports on a non-loopback address"
    if [[ ${INFO[NS_LOOPBACK]:-0} =~ ^[1-9][0-9]*$ ]]; then
        say "warn: $NS_CONTAINER loopback-only port(s) ${INFO[NS_LOOPBACK]}"
    fi
    cur_ip="${INFO[NS_IP]:-}"
    [[ -n $cur_ip ]] || die 12 "$NS_CONTAINER is not attached to network $NS_NET"
    extra_nets=""
    for item in ${INFO[NS_NETWORKS]:-}; do
        [[ ${item%%=*} == "$NS_NET" ]] || extra_nets+="${extra_nets:+ }$item"
    done
    [[ -z $extra_nets ]] || say "warn: $NS_CONTAINER extra network(s): $extra_nets"
    [[ $cur_ip == "$UP_IP" ]] || die 12 "apache upstream ip $UP_IP != $NS_CONTAINER ip '$cur_ip' on network $NS_NET (networks: ${INFO[NS_NETWORKS]:-})"
    cand_port="$(cand_field "$PREV_CONTAINER" 3 || true)"
    [[ $cand_port == "$OLD_PORT" ]] || die 12 "apache upstream port $OLD_PORT is not the port ($cand_port) of $PREV_CONTAINER"
    PREV_IMAGE_ID="${INFO[PREV_IMAGE_ID]:-}"
    [[ -n $PREV_IMAGE_ID ]] || die 12 "cannot read the image id of $PREV_CONTAINER"
    if $PREV_AUTODETECT; then
        cand_img="$(cand_field "$PREV_CONTAINER" 4 || true)"
        [[ $cand_img == "$PREV_IMAGE_ID" ]] \
            || die 12 "auto-detected $PREV_CONTAINER image '$cand_img' (container list) != '$PREV_IMAGE_ID' (inspect)"
    fi
    if [[ -n $EXPECTED_PREV_IMAGE_ID && $PREV_IMAGE_ID != "$EXPECTED_PREV_IMAGE_ID" ]]; then
        die 12 "$PREV_CONTAINER image $PREV_IMAGE_ID != expected $EXPECTED_PREV_IMAGE_ID (set ACCT_EXPECTED_PREV_IMAGE_ID after a release)"
    fi
    parse_rev_labels
    [[ -n $PREV_REV ]] || die 12 "previous image has no *revision label; cannot establish the base revision"
    [[ -n ${INFO[WORKDIR]:-} && ${INFO[WORKDIR]} != / ]] || die 12 "previous container has no usable WorkingDir"
    nm=0
    local line
    for line in "${CAND_LINES[@]}"; do
        n="${line%%|*}"
        n="${n##*-r}"
        (( n > nm )) && nm=$n
    done
    NEW_CONTAINER="acct-app-candidate-r$((nm + 1))"
    say "previous=$PREV_CONTAINER port=$OLD_PORT image=${PREV_IMAGE_ID:0:19} rev=$PREV_REV upstream=$UP_IP -> new=$NEW_CONTAINER"
}

check_deps_unchanged() {
    local base="$PREV_REV" changed
    [[ $base =~ ^[0-9a-f]{7,40}$ ]] || die 6 "previous revision '$base' is not a git sha"
    git -C "$REPO" cat-file -e "${base}^{commit}" 2>/dev/null || die 6 "previous revision $base not found in the release repository"
    changed="$(git -C "$REPO" diff --name-only "$base" "$RELEASE_SHA" -- deploy/obys/requirements.obys.lock requirements.runtime.lock pyproject.toml ':(glob)*.py' 2>&1)" \
        || die 6 "cannot diff $base..${RELEASE_SHA:0:8}"
    [[ -z $changed ]] || die 6 "files outside the overlay changed since $base ($(tr '\n' ' ' <<<"$changed")); this script cannot install dependencies or root modules - needs a full image build"
    local n
    n="$(git -C "$REPO" diff --name-only "$base" "$RELEASE_SHA" -- migrations ':(exclude)migrations/rollback' ':(exclude)migrations/drafts' | wc -l)"
    say "migrations changed in $base..${RELEASE_SHA:0:8}: $n file(s); only the $(( ${#ACCT_MIGRATION_ALLOWLIST[@]} + ${#ACCT_AUTH_MIGRATION_ALLOWLIST[@]} )) allowlisted OBYS migrations are ever applied"
}

snapshot_unrelated() { # all containers except the one this run creates
    local out
    out="$(rscript snapshot R_SNAPSHOT)" || return 1
    if [[ -n $NEW_CONTAINER ]]; then
        grep -v -E "^/${NEW_CONTAINER} " <<<"$out" || true
    else
        printf '%s\n' "$out"
    fi
}

check_unrelated_unchanged() { # sets UNRELATED_CHANGED
    local after
    after="$(snapshot_unrelated)" || { UNRELATED_CHANGED="snapshot-unavailable"; return 1; }
    UNRELATED_CHANGED="$(diff <(printf '%s\n' "$SNAPSHOT_BEFORE") <(printf '%s\n' "$after") | grep -E '^[<>]' || true)"
    [[ -z $UNRELATED_CHANGED ]]
}

# ---- image: once per release SHA ----------------------------------------------------------------
image_info() { # prints "<id>|<acct.release.sha>|<revision>" or fails when the image does not exist
    rdocker image inspect -f '{{.Id}}|{{index .Config.Labels "acct.release.sha"}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1" 2>/dev/null
}

# 통장사본 OCR(tesseract) 설치 목록은 고정이다. env·인자로 늘릴 수 없다(공급망 표면 고정).
readonly OCR_APT_PACKAGES=(tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng)

ocr_install_run() { # the single RUN line: skip when tesseract + kor + eng already exist, else fixed-allowlist apt install
    local pk="${OCR_APT_PACKAGES[*]}"
    printf '%s' "RUN if command -v tesseract >/dev/null 2>&1 && tesseract --list-langs 2>&1 | grep -qx kor && tesseract --list-langs 2>&1 | grep -qx eng; then echo 'tesseract already present; skip'; "
    printf '%s' "else DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $pk && rm -rf /var/lib/apt/lists/*; fi && tesseract --list-langs 2>&1 | grep -qx kor"
    printf '\n'
}

write_dockerfile() { # write_dockerfile <ctx dir>
    local ctx=$1 keys="" k p base_user=""
    for k in "${BASE_REV_KEYS[@]}" org.opencontainers.image.revision; do
        [[ " $keys " == *" $k "* ]] || keys+="$k "
    done
    # apt 설치는 root 로 해야 하므로, 설치 뒤 기반 이미지의 원래 USER 로 되돌린다.
    base_user="$(rdocker image inspect -f '{{.Config.User}}' "$BASE_REF" 2>/dev/null || true)"
    [[ $base_user =~ ^[A-Za-z0-9_.:-]*$ ]] || die 6 "base image USER '$base_user' has unexpected characters"
    {
        echo "FROM $BASE_REF"
        echo "USER root"
        ocr_install_run
        printf 'LABEL'
        for k in $keys; do printf ' %s="%s"' "$k" "$SHA8"; done
        printf ' acct.release.sha="%s" acct.release.run="%s" acct.release.base="%s"\n' "$RELEASE_SHA" "$RUN_ID" "$PREV_IMAGE_ID"
        for p in "${BUILD_PATHS_PRESENT[@]}"; do echo "COPY $p ./$p"; done
        # COPY 와 같은 WORKDIR 상대경로. root 구간에서 지우고 레이어는 RUN 1개만 늘린다.
        if (( ${#RELEASE_EXCLUDE_PATHS[@]} > 0 )); then
            for p in "${RELEASE_EXCLUDE_PATHS[@]}"; do [[ $p =~ ^[A-Za-z0-9_./-]+$ ]] || die 6 "bad release exclude path '$p'"; done
            echo "RUN rm -f$(printf ' ./%s' "${RELEASE_EXCLUDE_PATHS[@]}")"
        fi
        [[ -z $base_user || $base_user == root ]] || echo "USER $base_user"
    } >"$ctx/Dockerfile"
}

stamp_assets() { # stamp the obys shell in the build context (never the repository checkout)
    local out
    ASSET_VERSION="$SHA8"
    [[ -d $BUILD_CTX/$OBYS_STATIC_REL ]] || { ASSET_STAMP="no_obys_dir"; say "asset stamp: no $OBYS_STATIC_REL in the release; skipped"; return 0; }
    out="$(python3 "$ASSET_TOOL" stamp "$BUILD_CTX/$OBYS_STATIC_REL" "$ASSET_VERSION")" \
        || die 6 "obys asset version stamping failed for $ASSET_VERSION"
    ASSET_STAMP="stamped"
    say "asset stamp: ${out#OBYS_ASSET_STAMP }"
}

image_layers() { # image_layers <ref> -> prints the RootFS layer count; fails when the image is missing or the value is not a number
    local n
    n="$(rdocker image inspect -f '{{len .RootFS.Layers}}' "$1" 2>/dev/null)" || return 1
    [[ $n =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$n"
}

compute_build_timeout() { # sets BT
    BT=$(( $(left) - MONITOR_SECONDS - RECOVERY_RESERVE - 180 ))
    (( BT >= 120 )) || die 124 "not enough time budget left to build (left $(left)s)"
    (( BT > BUILD_TIMEOUT )) && BT=$BUILD_TIMEOUT
    return 0
}

# 기반 이미지의 Config(JSON, 환경변수 R_CFG)를 그대로 복원하는 평탄화 Dockerfile 을 출력한다.
flatten_dockerfile() { # flatten_dockerfile <src ref>
    R_SRC="$1" python3 -c '
import json, os, re, sys

cfg = json.loads(os.environ["R_CFG"]) or {}


def bad(msg):
    sys.stderr.write("flatten: " + msg + "\n")
    sys.exit(1)


def q(v):
    if re.search(r"[\n\r\0]", v):
        bad("control character in a config value")
    return "\"" + v.replace("\\", "\\\\").replace("\"", "\\\"").replace("$", "\\$") + "\""


def dur(ns):
    return "%dns" % int(ns)


out = ["FROM %s AS src" % os.environ["R_SRC"], "FROM scratch", "COPY --from=src / /"]
for item in cfg.get("Env") or []:
    k, sep, v = item.partition("=")
    if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", k):
        bad("unsupported Env entry")
    out.append("ENV %s=%s" % (k, q(v)))
labels = cfg.get("Labels") or {}
if labels:
    out.append("LABEL " + " ".join("%s=%s" % (q(k), q(v)) for k, v in sorted(labels.items())))
for port in sorted(cfg.get("ExposedPorts") or {}):
    if not re.fullmatch(r"[0-9]+(-[0-9]+)?/(tcp|udp|sctp)", port):
        bad("unsupported ExposedPorts entry")
    out.append("EXPOSE " + port)
vols = sorted(cfg.get("Volumes") or {})
if vols:
    out.append("VOLUME " + json.dumps(vols))
if cfg.get("WorkingDir"):
    out.append("WORKDIR " + q(cfg["WorkingDir"]))
if cfg.get("User"):
    out.append("USER " + q(cfg["User"]))
if cfg.get("Shell"):
    out.append("SHELL " + json.dumps(cfg["Shell"]))
if cfg.get("StopSignal"):
    out.append("STOPSIGNAL " + q(cfg["StopSignal"]))
hc = cfg.get("Healthcheck") or {}
test = hc.get("Test") or []
if test == ["NONE"]:
    out.append("HEALTHCHECK NONE")
elif test:
    opts = ""
    for flag, key in (("interval", "Interval"), ("timeout", "Timeout"), ("start-period", "StartPeriod")):
        if hc.get(key):
            opts += " --%s=%s" % (flag, dur(hc[key]))
    if hc.get("Retries"):
        opts += " --retries=%d" % int(hc["Retries"])
    if test[0] == "CMD-SHELL" and len(test) == 2:
        if re.search(r"[\n\r]", test[1]):
            bad("newline in Healthcheck command")
        out.append("HEALTHCHECK%s CMD %s" % (opts, test[1]))
    elif test[0] == "CMD":
        out.append("HEALTHCHECK%s CMD %s" % (opts, json.dumps(test[1:])))
    else:
        bad("unsupported Healthcheck.Test")
if cfg.get("Entrypoint"):
    out.append("ENTRYPOINT " + json.dumps(cfg["Entrypoint"]))
if cfg.get("Cmd"):
    out.append("CMD " + json.dumps(cfg["Cmd"]))
print("\n".join(out))
'
}

# 기반 이미지 레이어가 임계값 이상이면 한 층짜리 평탄화 기반을 만들어 BASE_REF 를 그 태그로 바꾼다.
# docker create/export 없이: 다단계 빌드(FROM scratch + COPY --from)로 파일시스템만 옮기고 Config 는 inspect 값으로 복원한다.
flatten_base() { # flatten_base <src ref>
    local src=$1 flat n cfg df
    flat="acct-candidate-flatbase:${PREV_IMAGE_ID#sha256:}"
    flat="${flat:0:36}"
    if n="$(image_layers "$flat")" && (( n < FLATTEN_LAYER_THRESHOLD )); then
        say "flattened base $flat already exists (layers=$n); reusing"
    else
        cfg="$(rdocker image inspect -f '{{json .Config}}' "$src")" || die 6 "cannot read the Config of base image $src"
        df="$(R_CFG="$cfg" flatten_dockerfile "$src")" || die 6 "cannot generate the flatten Dockerfile for $src"
        compute_build_timeout
        say "flattening base ${PREV_IMAGE_ID:0:19} (layers=$BASE_LAYERS >= $FLATTEN_LAYER_THRESHOLD) into $flat (timeout ${BT}s)"
        printf '%s\n' "$df" | SSH_CALL_TIMEOUT=$BT rdocker build --pull=false -q -t "$flat" - >/dev/null \
            || die 6 "flatten build failed or timed out"
        n="$(image_layers "$flat")" || die 6 "flattened base $flat not found afterwards"
        (( n < FLATTEN_LAYER_THRESHOLD )) || die 6 "flattened base $flat still has $n layers (threshold $FLATTEN_LAYER_THRESHOLD)"
    fi
    BASE_REF="$flat"
    FLATTENED=true
}

build_image() {
    local kb p
    compute_build_timeout
    BUILD_CTX="$(mktemp -d)"
    BUILD_PATHS_PRESENT=()
    for p in "${BUILD_PATHS[@]}"; do
        [[ -n "$(git -C "$REPO" ls-tree --name-only HEAD -- "$p")" ]] && BUILD_PATHS_PRESENT+=("$p")
    done
    [[ ${#BUILD_PATHS_PRESENT[@]} -gt 0 ]] || die 6 "no build paths exist at the release"
    local -a excl=()
    for p in "${RELEASE_EXCLUDE_PATHS[@]}"; do excl+=(":(exclude)$p"); done
    git -C "$REPO" archive --format=tar HEAD "${BUILD_PATHS_PRESENT[@]}" ':(exclude)app/static/gallery' "${excl[@]}" | tar -x -C "$BUILD_CTX" \
        || die 6 "git archive of the release failed"
    stamp_assets
    kb="$(du -sk "$BUILD_CTX" | cut -f1)"
    (( kb <= CONTEXT_MAX_MB * 1024 )) || die 6 "build context ${kb}KB exceeds ${CONTEXT_MAX_MB}MB (AGENTS.md rule 11) - investigate before releasing"
    # BuildKit resolves a bare "sha256:<id>" FROM as a registry name (docker.io/library/sha256) and fails;
    # pin the running image id under a local tag and build FROM that tag instead.
    BASE_REF="acct-candidate-base:${PREV_IMAGE_ID#sha256:}"
    BASE_REF="${BASE_REF:0:33}"
    rdocker tag "$PREV_IMAGE_ID" "$BASE_REF" >/dev/null || die 6 "cannot tag base image $PREV_IMAGE_ID as $BASE_REF"
    BASE_LAYERS="$(image_layers "$BASE_REF")" || die 6 "cannot read the layer count of base image $BASE_REF"
    if (( BASE_LAYERS >= FLATTEN_LAYER_THRESHOLD )); then
        flatten_base "$BASE_REF"
        compute_build_timeout
    fi
    write_dockerfile "$BUILD_CTX"
    say "building $IMAGE_TAG on $CAFE24_SSH once (context ${kb}KB, timeout ${BT}s, base ${PREV_IMAGE_ID:0:19}, base_layers=$BASE_LAYERS, flattened=$FLATTENED)"
    # shellcheck disable=SC2016
    tar -C "$BUILD_CTX" -cf - . | SSH_CALL_TIMEOUT=$BT rdocker build --pull=false -q -t "$IMAGE_TAG" - >/dev/null \
        || die 6 "image build failed or timed out"
}

# 기존 이미지의 라벨을 검증한다. 맞으면 IMAGE_ID 를 채우고 0, 이미지가 없으면 1.
inspect_existing_image() { # inspect_existing_image <tag>
    local info lbl_sha lbl_rev
    info="$(image_info "$1")" && [[ -n $info ]] || return 1
    IFS='|' read -r IMAGE_ID lbl_sha lbl_rev <<<"$info"
    [[ $lbl_rev == "$SHA8" ]] || die 6 "$1 exists with revision label '$lbl_rev' != $SHA8; refusing to reuse or rebuild"
    [[ -z $lbl_sha || $lbl_sha == "$RELEASE_SHA" ]] || die 6 "$1 exists for a different release $lbl_sha"
    return 0
}

reuse_image() { # reuse_image -> 0 when IMAGE_TAG exists and is shallow enough to start
    local n
    inspect_existing_image "$IMAGE_TAG" || return 1
    n="$(image_layers "$IMAGE_TAG")" || die 6 "cannot read the layer count of $IMAGE_TAG"
    if (( n >= MAX_IMAGE_LAYERS )); then
        say "image $IMAGE_TAG has $n layers (>= $MAX_IMAGE_LAYERS); not reusing"
        return 2
    fi
    IMAGE_LAYERS="$n"
    IMAGE_REUSED=true
    ASSET_STAMP="image_reused"
    say "image $IMAGE_TAG already exists (${IMAGE_ID:0:19}, layers=$n); reusing, no rebuild"
    return 0
}

check_layer_depth() {
    (( IMAGE_LAYERS < IMAGE_LAYER_DIE_LIMIT )) \
        || die 6 "image layer depth $IMAGE_LAYERS exceeds limit ($IMAGE_LAYER_DIE_LIMIT); flatten"
}

ensure_image() {
    local info lbl_sha lbl_rev rc=0 n=0 base_tag="acct-candidate:${SHA8}"
    IMAGE_TAG="$base_tag"
    reuse_image || rc=$?
    if (( rc == 0 )); then
        check_layer_depth
        return 0
    fi
    if (( rc == 2 )); then
        # 깊은 이미지는 건드리지 않고 새 이름(-f<N>)으로 재빌드한다. 이미 있는 -f<N> 이 얕으면 그것을 쓴다.
        while (( n < 20 )); do
            n=$((n + 1))
            IMAGE_TAG="${base_tag}-f${n}"
            rc=0
            reuse_image || rc=$?
            (( rc == 0 )) && { check_layer_depth; return 0; }
            (( rc == 1 )) && break
        done
        (( rc == 1 )) || die 6 "no usable $base_tag-f<N> tag (tried f1..f20); clean up old candidate images deliberately"
    fi
    check_deps_unchanged
    build_image
    info="$(image_info "$IMAGE_TAG")" || die 6 "built image $IMAGE_TAG not found afterwards"
    IFS='|' read -r IMAGE_ID lbl_sha lbl_rev <<<"$info"
    [[ $lbl_rev == "$SHA8" && $lbl_sha == "$RELEASE_SHA" ]] || die 6 "built image labels wrong: revision=$lbl_rev release=$lbl_sha"
    IMAGE_LAYERS="$(image_layers "$IMAGE_TAG")" || die 6 "cannot read the layer count of built image $IMAGE_TAG"
    say "built $IMAGE_TAG ${IMAGE_ID:0:19} (layers=$IMAGE_LAYERS)"
    check_layer_depth
}

# ---- migrations ---------------------------------------------------------------------------------
migration_probe_sql() { # prints SQL returning t when the migration is already applied
    case $1 in
        20260930_obys_attendance_pwa_gps.sql)
            # 지점 3 + 근태 13 컬럼, geofence_result CHECK, 열린 출근 인덱스, (역할이 있으면) 새 컬럼 권한.
            echo "/*probe*/ SELECT (SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND ((table_name='yeoljeong_branches' AND column_name IN ('latitude','longitude','geofence_radius_m')) OR (table_name='yeoljeong_attendance_records' AND column_name IN ('check_in_at','check_out_at','check_in_lat','check_in_lng','check_in_accuracy_m','check_out_lat','check_out_lng','check_out_accuracy_m','check_in_distance_m','check_out_distance_m','geofence_result','device_info','location_consent_at')))) = 16 AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.yeoljeong_attendance_records'::regclass AND conname='yeoljeong_attendance_records_geofence_result_check') AND to_regclass('public.idx_yeoljeong_attendance_pwa_open') IS NOT NULL AND (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acct_business_runtime_r5') OR (has_column_privilege('acct_business_runtime_r5','public.yeoljeong_attendance_records','check_in_at','SELECT') AND has_column_privilege('acct_business_runtime_r5','public.yeoljeong_attendance_records','check_in_at','INSERT') AND has_column_privilege('acct_business_runtime_r5','public.yeoljeong_attendance_records','geofence_result','UPDATE') AND has_column_privilege('acct_business_runtime_r5','public.yeoljeong_branches','latitude','SELECT') AND has_column_privilege('acct_business_runtime_r5','public.yeoljeong_branches','latitude','UPDATE')));"
            ;;
        20261001_obys_hrdoc_expiry_integrity_superseded.sql)
            echo "/*probe*/ SELECT count(*) = 4 FROM information_schema.columns WHERE table_schema='public' AND table_name='yeoljeong_onboarding_documents' AND column_name IN ('expires_at','sha256','stored_path','superseded_by');"
            ;;
        20261003_obys_clobe_collection.sql)
            echo "/*probe*/ SELECT (SELECT count(*) FROM unnest(ARRAY['obys_clobe_company_link','obys_clobe_collection_lease','obys_clobe_collection_run','obys_clobe_item','obys_clobe_ledger_entry','obys_clobe_collection_state']) t WHERE to_regclass('public.'||t) IS NOT NULL) = 6 AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='obys_clobe_company_link' AND column_name='missing_streak');"
            ;;
        20261008_obys_clobe_mcp_store.sql)
            # 4개 테이블 + (역할이 있으면) 그 역할의 클로브 테이블 권한. 역할이 없는 DB 는 GRANT 를 건너뛰므로 테이블만 본다.
            echo "/*probe*/ SELECT (SELECT count(*) FROM unnest(ARRAY['clobe_mcp_oauth_client','clobe_mcp_oauth_state','clobe_mcp_connection','clobe_mcp_tools']) t WHERE to_regclass('public.'||t) IS NOT NULL) = 4 AND (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acct_business_runtime_r5') OR (has_table_privilege('acct_business_runtime_r5','public.clobe_mcp_connection','SELECT,INSERT,UPDATE') AND has_table_privilege('acct_business_runtime_r5','public.obys_clobe_company_link','SELECT,INSERT,UPDATE')));"
            ;;
        20261014_obys_journal_source_clobe.sql)
            # 전표 source_type CHECK 제약이 있고, 정의에 'clobe' 가 들어 있으며, 검증 완료(convalidated)인 경우.
            echo "/*probe*/ SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('public.yeoljeong_journal_vouchers') AND conname='yeoljeong_journal_vouchers_source_type_check' AND convalidated AND pg_get_constraintdef(oid) LIKE '%''clobe''%');"
            ;;
        20261014_obys_auth_password_recovery.sql)
            # 인증 DB: saas_users 3컬럼 + 재설정 요청·감사 테이블 + 열린 요청 유일 인덱스.
            echo "/*probe*/ SELECT (SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='saas_users' AND column_name IN ('must_change_password','session_version','password_changed_at')) = 3 AND to_regclass('public.obys_password_reset_requests') IS NOT NULL AND to_regclass('public.obys_password_reset_audit') IS NOT NULL AND to_regclass('public.uq_obys_pwreset_open_per_user_tenant_method') IS NOT NULL;"
            ;;
        20261030_obys_work_schedules.sql)
            # 근무 일정 테이블 + 활성 일정 유일 인덱스 + (역할이 있으면) 런타임 역할의 읽기·쓰기 권한.
            echo "/*probe*/ SELECT to_regclass('public.yeoljeong_work_schedules') IS NOT NULL AND to_regclass('public.idx_obys_work_schedules_active') IS NOT NULL AND (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='acct_business_runtime_r5') OR has_table_privilege('acct_business_runtime_r5','public.yeoljeong_work_schedules','SELECT,INSERT,UPDATE'));"
            ;;
        *) return 1 ;;
    esac
}

pg_query_db() { # pg_query_db <db> <sql>  (prints psql -At output)
    rdocker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$1" -X -Atq -v ON_ERROR_STOP=1 -f - <<<"$2"
}

pg_query() { # pg_query <sql>  (업무 DB)
    pg_query_db "$PG_DB" "$1"
}

apply_migration() { # apply_migration <name>: allowlist only
    local m=$1 file out db
    migration_allowed "$m" || { say "migration '$m' is not in the allowlist; refusing"; return 7; }
    file="$REPO/migrations/$m"
    [[ -f $file ]] || { say "migration file $file missing"; return 7; }
    migration_scan "$file" >/dev/null || { say "migration $m failed the safety scan; refusing"; return 7; }
    db="$(migration_db "$m")"
    say "applying $m to $PG_CONTAINER/$db (single transaction, ON_ERROR_STOP)"
    if ! SSH_CALL_TIMEOUT=180 rdocker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$db" -X -q -v ON_ERROR_STOP=1 -f - <"$file"; then
        say "migration $m failed (transaction rolled back by ON_ERROR_STOP)"
        return 7
    fi
    out="$(pg_query_db "$db" "$(migration_probe_sql "$m")")" || return 7
    [[ $out == t ]] || { say "migration $m applied but its probe says '$out'"; return 7; }
}

run_migrations() {
    local m state db out
    local pending=() pending_dbs=()
    if [[ -z $PG_USER ]]; then PG_USER="${INFO[PG_USER]:-}"; fi
    [[ -n $PG_USER ]] || PG_USER=postgres
    [[ $AUTH_PG_DB != "$PG_DB" ]] || die 7 "auth DB '$AUTH_PG_DB' must differ from the business DB '$PG_DB'"
    out="$(pg_query "/*identity*/ SELECT to_regclass('public.yeoljeong_onboarding_documents') IS NOT NULL;")" \
        || die 7 "cannot query $PG_CONTAINER/$PG_DB"
    [[ $out == t ]] || die 7 "$PG_DB on $PG_CONTAINER is not the OBYS business DB (yeoljeong_onboarding_documents missing)"
    if [[ ${#ACCT_AUTH_MIGRATION_ALLOWLIST[@]} -gt 0 ]]; then
        out="$(pg_query_db "$AUTH_PG_DB" "/*identity*/ SELECT to_regclass('public.saas_users') IS NOT NULL AND to_regclass('public.tenant_memberships') IS NOT NULL AND to_regclass('public.yeoljeong_onboarding_documents') IS NULL;")" \
            || die 7 "cannot query $PG_CONTAINER/$AUTH_PG_DB"
        [[ $out == t ]] || die 7 "$AUTH_PG_DB on $PG_CONTAINER is not the OBYS auth DB (saas_users/tenant_memberships missing, or it is the business DB)"
    fi
    for m in "${ACCT_MIGRATION_ALLOWLIST[@]}" "${ACCT_AUTH_MIGRATION_ALLOWLIST[@]}"; do
        db="$(migration_db "$m")"
        state="$(pg_query_db "$db" "$(migration_probe_sql "$m")")" || die 7 "probe failed for $m"
        if [[ $state == t ]]; then
            MIGRATIONS_SKIPPED+=("$m")
            say "migration $m already applied; skipping"
        else
            pending+=("$m")
            [[ " ${pending_dbs[*]:-} " == *" $db "* ]] || pending_dbs+=("$db")
        fi
    done
    [[ ${#pending[@]} -gt 0 ]] || return 0
    need_budget $((MONITOR_SECONDS + RECOVERY_RESERVE + 120)) "migrations"
    for db in "${pending_dbs[@]}"; do
        out="$(SSH_CALL_TIMEOUT=300 rscript pg_backup R_PG_BACKUP "$PG_CONTAINER" "$PG_USER" "$db" "$PG_BACKUP_DIR" "pre_${SHA8}")" \
            || die 7 "pg_dump backup of $db failed; no migration applied"
        out="$(sed -n 's/^BACKUP_PATH=//p' <<<"$out" | head -1)"
        [[ -n $out ]] || die 7 "pg_dump backup path of $db missing from output; no migration applied"
        say "pg_dump backup of $db on $CAFE24_SSH: $out"
        PG_BACKUP_PATH="${PG_BACKUP_PATH:+$PG_BACKUP_PATH,}$out"
    done
    for m in "${pending[@]}"; do
        apply_migration "$m" || die 7 "migration $m failed; backup $PG_BACKUP_PATH; no cutover performed (running app untouched)"
        MIGRATIONS_APPLIED+=("$m")
    done
}

# ---- candidate ----------------------------------------------------------------------------------
pick_port() { # first free port in 8112..8199: not used by any acct-app-candidate (even stopped) and connection refused
    local p line used=" " rc
    for line in "${CAND_LINES[@]}"; do used+="$(cut -d'|' -f3 <<<"$line") "; done
    for p in $(seq 8112 8199); do
        [[ $used == *" $p "* || $p == "$OLD_PORT" ]] && continue
        rc=0
        rcmd curl -s -m 3 -o /dev/null "http://$UP_IP:$p/" >/dev/null 2>&1 || rc=$?
        if [[ $rc == 7 ]]; then
            NEW_PORT=$p
            return 0
        fi
    done
    return 1
}

direct_get_body() { rcmd curl -s -m 8 "$1"; }
direct_get_code() { rcmd curl -s -m 8 -o /dev/null -w '%{http_code}' "$1"; }

candidate_ok() { # candidate_ok <base-url> <marker|->
    local b="$1" body code
    body="$(direct_get_body "$b/health/live" 2>/dev/null)" || return 1
    [[ $body == *'"status":"ok"'* ]] || return 1
    code="$(direct_get_code "$b/api/v1/health" 2>/dev/null)" || return 1
    [[ $code == 200 || $code == 401 ]] || return 1
    if [[ $2 != - ]]; then
        body="$(direct_get_body "$b/static/apps/obys/index.html" 2>/dev/null)" || return 1
        [[ $body == *"$2"* ]] || return 1
    fi
}

candidate_ocr_ok() { # candidate_ocr_ok <container>: tesseract has the kor and eng language data
    local langs
    langs="$(rdocker exec "$1" tesseract --list-langs 2>&1)" || return 1
    grep -qx kor <<<"$langs" && grep -qx eng <<<"$langs"
}

start_candidate() {
    local waited=0 st
    pick_port || die 8 "no free candidate port in 8112..8199"
    need_budget $((MONITOR_SECONDS + RECOVERY_RESERVE + HEALTH_WAIT_SECONDS + 60)) "candidate start"
    CANDIDATE_CREATED=1
    say "starting $NEW_CONTAINER on port $NEW_PORT (same env/netns/mounts as $PREV_CONTAINER)"
    local proxy_args=()
    if [[ -n $EGRESS_PROXY_URL ]]; then
        proxy_args=("$EGRESS_PROXY_URL" "$EGRESS_NO_PROXY")
        say "egress proxy enabled for $NEW_CONTAINER: $EGRESS_PROXY_URL (NO_PROXY=$EGRESS_NO_PROXY)"
    fi
    rscript start_candidate R_START_CANDIDATE "$NEW_CONTAINER" "$PREV_CONTAINER" "$NS_CONTAINER" "$IMAGE_TAG" "$NEW_PORT" "$OLD_PORT" "$RELEASE_SHA" "$RUN_ID" ${proxy_args[@]+"${proxy_args[@]}"} \
        >/dev/null || die 8 "candidate container did not start"
    while (( waited <= HEALTH_WAIT_SECONDS )); do
        if candidate_ok "http://$UP_IP:$NEW_PORT" "$MARKER"; then
            st="$(rdocker inspect -f '{{.State.Running}} {{.RestartCount}}' "$NEW_CONTAINER" 2>/dev/null || true)"
            [[ $st == "true 0" ]] || die 8 "candidate healthy but state is '$st'"
            candidate_ocr_ok "$NEW_CONTAINER" || die 8 "candidate $NEW_CONTAINER has no tesseract kor/eng language data (bankbook OCR would fail); removing it (production untouched)"
            say "candidate $NEW_CONTAINER healthy on $UP_IP:$NEW_PORT (health, api auth layer, marker '$MARKER', tesseract kor)"
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    rdocker logs --tail 30 "$NEW_CONTAINER" 2>&1 | sed 's/^/[candidate log] /' || true
    die 8 "candidate not healthy within ${HEALTH_WAIT_SECONDS}s; removing it (production untouched)"
}

# ---- cutover / recovery -------------------------------------------------------------------------
apache_switch() { # apache_switch <from-port> <to-port> <marker|->
    local out rc=0
    out="$(SSH_CALL_TIMEOUT=120 rscript apache_switch R_APACHE_SWITCH "$APACHE_SITE" "$1" "$2" "$APACHE_BACKUP_DIR" "$3" "$FB_HOST")" || rc=$?
    printf '%s\n' "$out" | sed 's/^/[apache] /'
    local bk
    bk="$(sed -n 's/^APACHE_BACKUP=//p' <<<"$out" | head -1)"
    [[ -z $APACHE_BACKUP && -n $bk ]] && APACHE_BACKUP="$bk"
    return "$rc"
}

verify_public() { # verify_public <marker|-> : the same checks the 5-minute watch repeats
    local body code
    body="$(pub_get_body "https://$FB_HOST/health/live" 2>/dev/null)" || return 1
    [[ $body == *'"status":"ok"'* ]] || return 1
    code="$(pub_get_code "https://$FB_HOST/api/v1/health" 2>/dev/null)" || return 1
    [[ $code == 200 || $code == 401 ]] || return 1
    if [[ $1 != - ]]; then
        body="$(pub_get_body "https://$FB_HOST/static/apps/obys/index.html" 2>/dev/null)" || return 1
        [[ $body == *"$1"* ]] || return 1
    fi
}

recover_apache() { # idempotent; after a successful recovery the edge copies fetched from the new app are purged again
    recover_apache_port
    if [[ $RECOVERED == 1 && $PURGE_RUNS == 1 ]]; then
        purge_edge
    fi
    return 0
}

recover_apache_port() { # idempotent: put the apache upstream back on the previous port
    [[ $CUTOVER_ATTEMPTED == 1 ]] || return 0
    local cur up attempt
    for attempt in 1 2; do
        up="$(apache_upstream 2>/dev/null || true)"
        cur="${up##* }"
        if [[ $cur == "$OLD_PORT" ]]; then
            RECOVERED=1
            say "apache upstream is on the previous port $OLD_PORT"
            return 0
        fi
        [[ $cur =~ ^[0-9]+$ ]] || cur="$NEW_PORT"
        say "recovery attempt $attempt: apache upstream $cur -> $OLD_PORT"
        apache_switch "$cur" "$OLD_PORT" - && continue
        sleep 2
    done
    if [[ -n $APACHE_BACKUP ]]; then
        say "recovery: restoring apache vhost from $APACHE_BACKUP"
        rscript apache_restore R_APACHE_RESTORE "$APACHE_SITE" "$APACHE_BACKUP" | sed 's/^/[apache] /' || true
    fi
    up="$(apache_upstream 2>/dev/null || true)"
    if [[ ${up##* } == "$OLD_PORT" ]]; then
        RECOVERED=1
        say "apache upstream is on the previous port $OLD_PORT"
    else
        say "RECOVERY FAILED - run by hand: bash scripts/deploy_acct_app_cafe24.sh rollback $PREV_CONTAINER"
    fi
}

purge_edge() { # Cloudflare stored copies of the obys shell; every problem is a warning only
    local out line rc=0 st
    PURGE_RUNS=$((PURGE_RUNS + 1))
    PURGE_STATUS="failed"
    PURGE_JSON='{"status": "failed", "reason": "url_list_unavailable"}'
    if (( ${#PURGE_URLS[@]} == 0 )) && [[ -n $REPO && -d $REPO/$OBYS_STATIC_REL ]]; then
        mapfile -t PURGE_URLS < <(python3 "$ASSET_TOOL" urls "$REPO/$OBYS_STATIC_REL" "${ASSET_VERSION:-$SHA8}" "https://$FB_HOST" 2>/dev/null) || PURGE_URLS=()
    fi
    if (( ${#PURGE_URLS[@]} == 0 )); then
        say "WARNING: edge purge not attempted - cannot build the URL list"
        return 0
    fi
    R_CF_PURGE="$(
        printf '%s\n' 'set -u' "read -r -d '' PYSRC <<'__OBYS_PY__' || true"
        cat "$ASSET_TOOL"
        printf '%s\n' '__OBYS_PY__' 'exec python3 -c "$PYSRC" purge --env-file "$1" "${@:2}"'
    )"
    out="$(SSH_CALL_TIMEOUT=60 rscript cf_purge R_CF_PURGE "$CF_ENV_FILE" "${PURGE_URLS[@]}" 2>&1)" || rc=$?
    line="$(sed -n 's/^PURGE_RESULT //p' <<<"$out" | tail -1)"
    if [[ -z $line ]]; then
        PURGE_JSON="{\"status\": \"failed\", \"reason\": \"no_result_rc_$rc\"}"
        say "WARNING: edge purge gave no result (rc=$rc); release is unaffected"
        return 0
    fi
    PURGE_JSON="$line"
    st="$(sed -n 's/.*"status": "\([a-z]*\)".*/\1/p' <<<"$line" | head -1)"
    PURGE_STATUS="${st:-failed}"
    case $PURGE_STATUS in
        ok) say "edge purge: ${#PURGE_URLS[@]} urls removed from Cloudflare (purge_cache files)" ;;
        *) say "WARNING: edge purge $PURGE_STATUS ($line); release is unaffected" ;;
    esac
    return 0
}

cutover() {
    need_budget $((MONITOR_SECONDS + RECOVERY_RESERVE + 30)) "cutover (watch + recovery must still fit)"
    CUTOVER_ATTEMPTED=1
    say "cutover: apache $APACHE_SITE upstream port $OLD_PORT -> $NEW_PORT (contabo116 fb.conf untouched)"
    if ! apache_switch "$OLD_PORT" "$NEW_PORT" "$MARKER"; then
        recover_apache
        die 9 "apache switch failed; previous port restored=$RECOVERED"
    fi
    CUTOVER_KST="$(kst)"
    if ! verify_public "$MARKER"; then
        recover_apache
        die 9 "public verification failed after the switch; previous port restored=$RECOVERED"
    fi
    say "cutover verified publicly at $CUTOVER_KST"
    purge_edge
}

monitor() {
    local iters=$(( (MONITOR_SECONDS + MONITOR_INTERVAL - 1) / MONITOR_INTERVAL )) i fails=0 st
    say "public watch: ${MONITOR_SECONDS}s every ${MONITOR_INTERVAL}s (fail after $MONITOR_FAIL_THRESHOLD consecutive misses)"
    for ((i = 1; i <= iters; i++)); do
        sleep "$MONITOR_INTERVAL"
        st="$(rdocker inspect -f '{{.State.Running}}' "$NEW_CONTAINER" 2>/dev/null || echo unknown)"
        if [[ $st == true ]] && verify_public "$MARKER"; then
            fails=0
        else
            fails=$((fails + 1))
            say "watch miss $fails/$MONITOR_FAIL_THRESHOLD at tick $i (container running=$st)"
            (( fails >= MONITOR_FAIL_THRESHOLD )) && return 1
        fi
    done
    return 0
}

# ---- result -------------------------------------------------------------------------------------
emit_result() { # emit_result <status>
    local status=$1 out
    mkdir -p "$RESULT_DIR" 2>/dev/null || true
    out="$(
        R_STATUS="$status" R_SHA="$RELEASE_SHA" R_RUN="$RUN_ID" R_MODE="$MODE" R_PREV="$PREV_CONTAINER" \
            R_PREV_IMAGE="$PREV_IMAGE_ID" R_PREV_PORT="$OLD_PORT" R_NEW="$NEW_CONTAINER" R_NEW_PORT="$NEW_PORT" \
            R_IMAGE_TAG="$IMAGE_TAG" R_IMAGE_ID="$IMAGE_ID" R_REUSED="$IMAGE_REUSED" R_PGBK="$PG_BACKUP_PATH" \
            R_APBK="$APACHE_BACKUP" R_KST="$CUTOVER_KST" R_RECOVERED="$RECOVERED" R_UNRELATED="$UNRELATED_CHANGED" \
            R_APPLIED="${MIGRATIONS_APPLIED[*]:-}" R_SKIPPED="${MIGRATIONS_SKIPPED[*]:-}" R_MARKER="$MARKER" \
            R_ASSET_VERSION="$ASSET_VERSION" R_ASSET_STAMP="$ASSET_STAMP" R_BASE_LAYERS="$BASE_LAYERS" R_FLATTENED="$FLATTENED" R_IMAGE_LAYERS="$IMAGE_LAYERS" R_PURGE_STATUS="$PURGE_STATUS" R_PURGE_JSON="$PURGE_JSON" \
            python3 - <<'PY'
import json, os
e = os.environ.get
try:
    purge_detail = json.loads(e("R_PURGE_JSON") or "null") or {}
except ValueError:
    purge_detail = {}
purge_detail["status"] = e("R_PURGE_STATUS") or "not_run"
doc = {
    "status": e("R_STATUS"),
    "mode": e("R_MODE"),
    "release_sha": e("R_SHA"),
    "run_id": e("R_RUN"),
    "previous": {"container": e("R_PREV"), "image_id": e("R_PREV_IMAGE"), "port": e("R_PREV_PORT")},
    "new": {"container": e("R_NEW"), "port": e("R_NEW_PORT"), "image": e("R_IMAGE_TAG"),
            "image_id": e("R_IMAGE_ID"), "image_reused": e("R_REUSED") == "true"},
    "backups": {"pg_dump": e("R_PGBK"), "apache_vhost": e("R_APBK")},
    "migrations": {"applied": (e("R_APPLIED") or "").split(), "already_applied": (e("R_SKIPPED") or "").split()},
    "base_layers": int(e("R_BASE_LAYERS")) if (e("R_BASE_LAYERS") or "").isdigit() else None,
    "flattened": e("R_FLATTENED") == "true",
    "image_layers": int(e("R_IMAGE_LAYERS")) if (e("R_IMAGE_LAYERS") or "").isdigit() else None,
    "marker": e("R_MARKER"),
    "assets": {"version": e("R_ASSET_VERSION"), "stamp": e("R_ASSET_STAMP")},
    "edge_purge": purge_detail,
    "cutover_kst": e("R_KST"),
    "recovered_previous_port": e("R_RECOVERED") == "1",
    "unrelated_containers_changed": (e("R_UNRELATED") or "").splitlines(),
    "rollback_command": "bash /root/aads/aads-server/scripts/deploy_acct_app_cafe24.sh rollback %s %s"
                        % (e("R_PREV"), e("R_RUN")),
}
print(json.dumps(doc, ensure_ascii=False))
PY
    )" || return 0
    printf 'ACCT_APP_RELEASE_RESULT %s\n' "$out"
    printf '%s\n' "$out" >"$RESULT_DIR/acct-app-${RUN_ID}.json" 2>/dev/null || true
}

abort_cleanup() {
    recover_apache || true
    if [[ $CANDIDATE_CREATED == 1 && ( $CUTOVER_ATTEMPTED != 1 || $RECOVERED == 1 ) ]]; then
        say "removing failed candidate $NEW_CONTAINER (the previous container is untouched)"
        docker_mut rm -f "$NEW_CONTAINER" || true
    fi
    emit_result failed || true
}

on_exit() {
    local rc=$?
    trap - EXIT
    if (( rc != 0 )) && [[ $COMPLETED != 1 && $MODE == deploy ]]; then
        abort_cleanup || true
    fi
    cleanup_release_wt
    [[ -n $BUILD_CTX ]] && rm -rf "$BUILD_CTX"
    exit "$rc"
}

acquire_lock() {
    exec 9>"$LOCK"
    flock -n 9 || die 3 "ACCT app release already running"
}

# ---- entrypoints --------------------------------------------------------------------------------
main_deploy() {
    RELEASE_SHA="${1:-}"
    RUN_ID="${2:-0}"
    MARKER="${3:-${ACCT_APP_MARKER:-$DEFAULT_MARKER}}"
    [[ $RELEASE_SHA =~ ^[0-9a-fA-F]{40}$ ]] || die 2 "the full 40-hex release SHA is required"
    RELEASE_SHA="${RELEASE_SHA,,}"
    SHA8="${RELEASE_SHA:0:8}"
    [[ $RUN_ID =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die 2 "invalid run id"
    [[ $MARKER =~ ^[A-Za-z0-9_.:/-]{3,100}$ ]] || die 2 "invalid marker string"
    [[ $MONITOR_SECONDS =~ ^[0-9]+$ && $MONITOR_INTERVAL =~ ^[1-9][0-9]*$ ]] || die 2 "invalid monitor settings"
    MODE=deploy
    validate_state_paths WORKTREE_ROOT RESULT_DIR PG_BACKUP_DIR APACHE_BACKUP_DIR LOCK
    acquire_lock
    trap on_exit EXIT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    if [[ -z $REPO ]]; then make_release_worktree; fi
    validate_release
    say "release ${RELEASE_SHA:0:12} validated: allowlisted migrations safe, marker '$MARKER' present, context clean"

    discover_state
    SNAPSHOT_BEFORE="$(snapshot_unrelated)" || die 12 "cannot snapshot containers on $CAFE24_SSH"

    local existing
    existing="$(for l in "${CAND_LINES[@]}"; do [[ $(cut -d'|' -f5 <<<"$l") == "$RELEASE_SHA" ]] && cut -d'|' -f1,3 <<<"$l"; done | head -1 || true)"
    if [[ -n $existing ]]; then
        if [[ ${existing#*|} == "$OLD_PORT" ]]; then
            NEW_CONTAINER="${existing%%|*}"
            NEW_PORT="$OLD_PORT"
            IMAGE_TAG="acct-candidate:${SHA8}"
            say "release ${SHA8} is already served by ${existing%%|*} on port $OLD_PORT; nothing to do"
            COMPLETED=1
            emit_result already_active
            return 0
        fi
        die 12 "container ${existing%%|*} for this release already exists but is not the active upstream; use rollback/remove it deliberately"
    fi

    ensure_image
    run_migrations
    start_candidate
    if ! check_unrelated_unchanged; then
        die 11 "unrelated containers changed during the release: $UNRELATED_CHANGED (cutover not performed)"
    fi
    cutover
    if ! monitor; then
        recover_apache
        die 10 "public watch failed; previous port restored=$RECOVERED"
    fi
    check_unrelated_unchanged || say "WARNING: other containers changed during the release (not by this script): $UNRELATED_CHANGED"
    COMPLETED=1
    emit_result switched
    say "done: fb served by $NEW_CONTAINER (port $NEW_PORT, image $IMAGE_TAG); previous $PREV_CONTAINER kept running for rollback"
}

main_rollback() {
    local target="${1:-}"
    RUN_ID="${2:-0}"
    MODE=rollback
    owned_name "$target" || die 2 "rollback target must match $CANDIDATE_RE"
    [[ $RUN_ID =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die 2 "invalid run id"
    validate_state_paths RESULT_DIR APACHE_BACKUP_DIR LOCK
    acquire_lock
    trap on_exit EXIT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    local up cur_port run port
    up="$(apache_upstream)" || die 12 "cannot read the fb vhost upstream from $APACHE_SITE"
    read -r UP_IP cur_port <<<"$up"
    OLD_PORT="$cur_port"
    PREV_CONTAINER="$target"
    list_candidates
    run="$(cand_field "$target" 2 || true)"
    port="$(cand_field "$target" 3 || true)"
    [[ -n $run && $port =~ ^[0-9]+$ ]] || die 12 "container $target not found or its --port cannot be determined"
    if [[ $run != true ]]; then
        say "$target is not running; starting it"
        docker_mut start "$target" || die 12 "cannot start $target"
        sleep 5
    fi
    candidate_ok "http://$UP_IP:$port" - || die 12 "$target does not answer health on $UP_IP:$port; apache left untouched"
    if [[ $cur_port == "$port" ]]; then
        say "apache already points at $target ($port)"
        COMPLETED=1
        NEW_CONTAINER="$target"; NEW_PORT="$port"
        emit_result already_active
        return 0
    fi
    NEW_CONTAINER="$target"; NEW_PORT="$port"
    say "rollback: apache upstream port $cur_port -> $port ($target)"
    if ! apache_switch "$cur_port" "$port" -; then
        die 9 "apache switch failed (apache restored from backup by the switch itself)"
    fi
    verify_public - || die 9 "public health failed after the rollback switch; apache backup: ${APACHE_BACKUP:-none}"
    CUTOVER_KST="$(kst)"
    COMPLETED=1
    emit_result rolled_back
    say "rollback complete: fb served by $target on port $port"
}

main() {
    case "${1:-}" in
        rollback) shift; main_rollback "$@" ;;
        *) main_deploy "$@" ;;
    esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
