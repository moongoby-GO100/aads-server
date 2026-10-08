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
#   bash scripts/deploy_acct_app_cafe24.sh rollback acct-app-candidate-r8 <run_id>

set -euo pipefail

SOURCE_REPO="${AADS_DEPLOY_SOURCE_REPO:-/root/aads/aads-server}"
REPO="${AADS_DEPLOY_REPO_DIR:-}"
WORKTREE_ROOT="${ACCT_APP_RELEASE_ROOT:-/root/aads/state/acct-app-releases}"
RESULT_DIR="${ACCT_APP_RESULT_DIR:-/root/aads/state/acct-app-release-results}"
LOCK="${ACCT_APP_LOCK_FILE:-/tmp/aads-acct-app-cafe24.lock}"
CAFE24_SSH="${CAFE24_SSH:-server-114}"
FB_HOST="fb.newtalk.kr"
NS_CONTAINER="${NS_CONTAINER:-acct-pg}"
PG_CONTAINER="${ACCT_PG_CONTAINER:-acct-pg}"
PG_DB="${ACCT_PG_DB:-obys}"
PG_USER="${ACCT_PG_USER:-}"
PG_BACKUP_DIR="${ACCT_PG_BACKUP_DIR:-/root/acct-release-backups}"
APACHE_SITE="${ACCT_APACHE_SITE:-/etc/apache2/sites-available/00-zz-fb.newtalk.kr.conf}"
APACHE_BACKUP_DIR="${ACCT_APACHE_BACKUP_DIR:-/root/fb-cutover-backups}"
PREV_CONTAINER="${ACCT_PREV_CONTAINER:-acct-app-candidate-r8}"
R8_IMAGE_ID="sha256:71e5e811c220efbe9bc527f36836e0cdc9638794ec01a6b1bbd5256ea5e8150e"
if [[ -z ${ACCT_EXPECTED_PREV_IMAGE_ID+x} ]]; then
    if [[ $PREV_CONTAINER == acct-app-candidate-r8 ]]; then EXPECTED_PREV_IMAGE_ID="$R8_IMAGE_ID"; else EXPECTED_PREV_IMAGE_ID=""; fi
else
    EXPECTED_PREV_IMAGE_ID="$ACCT_EXPECTED_PREV_IMAGE_ID"
fi
DEFAULT_MARKER="downloadSignedContractPdf"
BUDGET_SECONDS="${DEPLOY_BUDGET_SECONDS:-1380}"   # target timeout_seconds is 1500; leave room for recovery
RECOVERY_RESERVE=90
MONITOR_SECONDS="${MONITOR_SECONDS:-300}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-15}"
MONITOR_FAIL_THRESHOLD="${MONITOR_FAIL_THRESHOLD:-2}"
BUILD_TIMEOUT="${ACCT_BUILD_TIMEOUT:-600}"
HEALTH_WAIT_SECONDS="${ACCT_HEALTH_WAIT_SECONDS:-90}"
CONTEXT_MAX_MB="${ACCT_CONTEXT_MAX_MB:-100}"
SSH_DEFAULT_TIMEOUT="${ACCT_SSH_TIMEOUT:-120}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

# Fixed on purpose: nothing from the environment or the command line can add to it.
ACCT_MIGRATION_ALLOWLIST=(
    20261001_obys_hrdoc_expiry_integrity_superseded.sql
    20261003_obys_clobe_collection.sql
    20261008_obys_clobe_mcp_store.sql
)
BUILD_PATHS=(app migrations deploy)
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

# ---- release worktree ---------------------------------------------------------------------------
cleanup_release_wt() {
    [[ -n "$release_wt" ]] || return 0
    git -C "$SOURCE_REPO" worktree remove --force "$release_wt" >/dev/null 2>&1 || rm -rf "$release_wt"
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
    for m in "${ACCT_MIGRATION_ALLOWLIST[@]}"; do
        [[ $m == "$n" ]] && return 0
    done
    return 1
}

migration_scan() { # fail-closed: transaction wrapper required, no DROP/TRUNCATE/DELETE (comments stripped)
    python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
text = re.sub(r"--[^\n]*", " ", text)
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
    for m in "${ACCT_MIGRATION_ALLOWLIST[@]}"; do
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
PREV=$1; NS=$2
echo "PREV_RUNNING=$(docker inspect -f '{{.State.Running}}' "$PREV" 2>/dev/null || echo missing)"
img=$(docker inspect -f '{{.Image}}' "$PREV" 2>/dev/null || true)
echo "PREV_IMAGE_ID=$img"
echo "PREV_NETMODE=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$PREV" 2>/dev/null || true)"
echo "WORKDIR=$(docker inspect -f '{{.Config.WorkingDir}}' "$PREV" 2>/dev/null || true)"
echo "REV_LABELS=$(docker image inspect -f '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' "$img" 2>/dev/null | grep -E '^[^=]*revision=' | tr '\n' ';')"
echo "NS_ID=$(docker inspect -f '{{.Id}}' "$NS" 2>/dev/null || true)"
echo "NS_RUNNING=$(docker inspect -f '{{.State.Running}}' "$NS" 2>/dev/null || echo missing)"
echo "NS_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$NS" 2>/dev/null | awk '{print $1}')"
echo "NS_PUBLISHED=$(docker port "$NS" 2>/dev/null | wc -l)"
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
    out="$(rscript prev_info R_PREV_INFO "$PREV_CONTAINER" "$NS_CONTAINER")" || die 12 "cannot read $PREV_CONTAINER state on $CAFE24_SSH"
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

discover_state() {
    local up cur_ip cand_port nm
    up="$(apache_upstream)" || die 12 "cannot read the fb vhost upstream from $APACHE_SITE"
    read -r UP_IP OLD_PORT <<<"$up"
    load_prev_info
    list_candidates
    [[ ${INFO[PREV_RUNNING]:-} == true ]] || die 12 "previous container $PREV_CONTAINER is not running"
    [[ ${INFO[NS_RUNNING]:-} == true ]] || die 12 "$NS_CONTAINER is not running"
    [[ ${INFO[NS_ID]:-} != "" && ${INFO[PREV_NETMODE]:-} == "container:${INFO[NS_ID]}" ]] \
        || die 12 "$PREV_CONTAINER network mode '${INFO[PREV_NETMODE]:-}' is not container:$NS_CONTAINER"
    [[ ${INFO[NS_PUBLISHED]:-1} == 0 ]] || die 12 "$NS_CONTAINER publishes host ports"
    cur_ip="${INFO[NS_IP]:-}"
    [[ $cur_ip == "$UP_IP" ]] || die 12 "apache upstream ip $UP_IP != $NS_CONTAINER ip '$cur_ip'"
    cand_port="$(cand_field "$PREV_CONTAINER" 3 || true)"
    [[ $cand_port == "$OLD_PORT" ]] || die 12 "apache upstream port $OLD_PORT is not the port ($cand_port) of $PREV_CONTAINER"
    PREV_IMAGE_ID="${INFO[PREV_IMAGE_ID]:-}"
    [[ -n $PREV_IMAGE_ID ]] || die 12 "cannot read the image id of $PREV_CONTAINER"
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
    say "migrations changed in $base..${RELEASE_SHA:0:8}: $n file(s); only the ${#ACCT_MIGRATION_ALLOWLIST[@]} allowlisted OBYS migrations are ever applied"
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

write_dockerfile() { # write_dockerfile <ctx dir>
    local ctx=$1 keys="" k p
    for k in "${BASE_REV_KEYS[@]}" org.opencontainers.image.revision; do
        [[ " $keys " == *" $k "* ]] || keys+="$k "
    done
    {
        echo "FROM $BASE_REF"
        printf 'LABEL'
        for k in $keys; do printf ' %s="%s"' "$k" "$SHA8"; done
        printf ' acct.release.sha="%s" acct.release.run="%s" acct.release.base="%s"\n' "$RELEASE_SHA" "$RUN_ID" "$PREV_IMAGE_ID"
        for p in "${BUILD_PATHS_PRESENT[@]}"; do echo "COPY $p ./$p"; done
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

build_image() {
    local bt kb p
    bt=$(( $(left) - MONITOR_SECONDS - RECOVERY_RESERVE - 180 ))
    (( bt >= 120 )) || die 124 "not enough time budget left to build (left $(left)s)"
    (( bt > BUILD_TIMEOUT )) && bt=$BUILD_TIMEOUT
    BUILD_CTX="$(mktemp -d)"
    BUILD_PATHS_PRESENT=()
    for p in "${BUILD_PATHS[@]}"; do
        [[ -n "$(git -C "$REPO" ls-tree --name-only HEAD -- "$p")" ]] && BUILD_PATHS_PRESENT+=("$p")
    done
    [[ ${#BUILD_PATHS_PRESENT[@]} -gt 0 ]] || die 6 "no build paths exist at the release"
    git -C "$REPO" archive --format=tar HEAD "${BUILD_PATHS_PRESENT[@]}" ':(exclude)app/static/gallery' | tar -x -C "$BUILD_CTX" \
        || die 6 "git archive of the release failed"
    stamp_assets
    kb="$(du -sk "$BUILD_CTX" | cut -f1)"
    (( kb <= CONTEXT_MAX_MB * 1024 )) || die 6 "build context ${kb}KB exceeds ${CONTEXT_MAX_MB}MB (AGENTS.md rule 11) - investigate before releasing"
    # BuildKit resolves a bare "sha256:<id>" FROM as a registry name (docker.io/library/sha256) and fails;
    # pin the running image id under a local tag and build FROM that tag instead.
    BASE_REF="acct-candidate-base:${PREV_IMAGE_ID#sha256:}"
    BASE_REF="${BASE_REF:0:33}"
    rdocker tag "$PREV_IMAGE_ID" "$BASE_REF" >/dev/null || die 6 "cannot tag base image $PREV_IMAGE_ID as $BASE_REF"
    write_dockerfile "$BUILD_CTX"
    say "building $IMAGE_TAG on $CAFE24_SSH once (context ${kb}KB, timeout ${bt}s, base ${PREV_IMAGE_ID:0:19})"
    # shellcheck disable=SC2016
    tar -C "$BUILD_CTX" -cf - . | SSH_CALL_TIMEOUT=$bt rdocker build --pull=false -q -t "$IMAGE_TAG" - >/dev/null \
        || die 6 "image build failed or timed out"
}

ensure_image() {
    local info lbl_sha lbl_rev
    IMAGE_TAG="acct-candidate:${SHA8}"
    if info="$(image_info "$IMAGE_TAG")" && [[ -n $info ]]; then
        IFS='|' read -r IMAGE_ID lbl_sha lbl_rev <<<"$info"
        [[ $lbl_rev == "$SHA8" ]] || die 6 "$IMAGE_TAG exists with revision label '$lbl_rev' != $SHA8; refusing to reuse or rebuild"
        [[ -z $lbl_sha || $lbl_sha == "$RELEASE_SHA" ]] || die 6 "$IMAGE_TAG exists for a different release $lbl_sha"
        IMAGE_REUSED=true
        ASSET_STAMP="image_reused"
        say "image $IMAGE_TAG already exists (${IMAGE_ID:0:19}); reusing, no rebuild"
        return 0
    fi
    check_deps_unchanged
    build_image
    info="$(image_info "$IMAGE_TAG")" || die 6 "built image $IMAGE_TAG not found afterwards"
    IFS='|' read -r IMAGE_ID lbl_sha lbl_rev <<<"$info"
    [[ $lbl_rev == "$SHA8" && $lbl_sha == "$RELEASE_SHA" ]] || die 6 "built image labels wrong: revision=$lbl_rev release=$lbl_sha"
    say "built $IMAGE_TAG ${IMAGE_ID:0:19}"
}

# ---- migrations ---------------------------------------------------------------------------------
migration_probe_sql() { # prints SQL returning t when the migration is already applied
    case $1 in
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
        *) return 1 ;;
    esac
}

pg_query() { # pg_query <sql>  (stdin carries the SQL; prints psql -At output)
    rdocker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -X -Atq -v ON_ERROR_STOP=1 -f - <<<"$1"
}

apply_migration() { # apply_migration <name>: allowlist only
    local m=$1 file out
    migration_allowed "$m" || { say "migration '$m' is not in the allowlist; refusing"; return 7; }
    file="$REPO/migrations/$m"
    [[ -f $file ]] || { say "migration file $file missing"; return 7; }
    migration_scan "$file" >/dev/null || { say "migration $m failed the safety scan; refusing"; return 7; }
    say "applying $m to $PG_CONTAINER/$PG_DB (single transaction, ON_ERROR_STOP)"
    if ! SSH_CALL_TIMEOUT=180 rdocker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -X -q -v ON_ERROR_STOP=1 -f - <"$file"; then
        say "migration $m failed (transaction rolled back by ON_ERROR_STOP)"
        return 7
    fi
    out="$(pg_query "$(migration_probe_sql "$m")")" || return 7
    [[ $out == t ]] || { say "migration $m applied but its probe says '$out'"; return 7; }
}

run_migrations() {
    local m state pending=() out
    if [[ -z $PG_USER ]]; then PG_USER="${INFO[PG_USER]:-}"; fi
    [[ -n $PG_USER ]] || PG_USER=postgres
    out="$(pg_query "/*identity*/ SELECT to_regclass('public.yeoljeong_onboarding_documents') IS NOT NULL;")" \
        || die 7 "cannot query $PG_CONTAINER/$PG_DB"
    [[ $out == t ]] || die 7 "$PG_DB on $PG_CONTAINER is not the OBYS business DB (yeoljeong_onboarding_documents missing)"
    for m in "${ACCT_MIGRATION_ALLOWLIST[@]}"; do
        state="$(pg_query "$(migration_probe_sql "$m")")" || die 7 "probe failed for $m"
        if [[ $state == t ]]; then
            MIGRATIONS_SKIPPED+=("$m")
            say "migration $m already applied; skipping"
        else
            pending+=("$m")
        fi
    done
    [[ ${#pending[@]} -gt 0 ]] || return 0
    need_budget $((MONITOR_SECONDS + RECOVERY_RESERVE + 120)) "migrations"
    out="$(SSH_CALL_TIMEOUT=300 rscript pg_backup R_PG_BACKUP "$PG_CONTAINER" "$PG_USER" "$PG_DB" "$PG_BACKUP_DIR" "pre_${SHA8}")" \
        || die 7 "pg_dump backup failed; no migration applied"
    PG_BACKUP_PATH="$(sed -n 's/^BACKUP_PATH=//p' <<<"$out" | head -1)"
    [[ -n $PG_BACKUP_PATH ]] || die 7 "pg_dump backup path missing from output; no migration applied"
    say "pg_dump backup on $CAFE24_SSH: $PG_BACKUP_PATH"
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

start_candidate() {
    local waited=0 st
    pick_port || die 8 "no free candidate port in 8112..8199"
    need_budget $((MONITOR_SECONDS + RECOVERY_RESERVE + HEALTH_WAIT_SECONDS + 60)) "candidate start"
    CANDIDATE_CREATED=1
    say "starting $NEW_CONTAINER on port $NEW_PORT (same env/netns/mounts as $PREV_CONTAINER)"
    rscript start_candidate R_START_CANDIDATE "$NEW_CONTAINER" "$PREV_CONTAINER" "$NS_CONTAINER" "$IMAGE_TAG" "$NEW_PORT" "$OLD_PORT" "$RELEASE_SHA" "$RUN_ID" \
        >/dev/null || die 8 "candidate container did not start"
    while (( waited <= HEALTH_WAIT_SECONDS )); do
        if candidate_ok "http://$UP_IP:$NEW_PORT" "$MARKER"; then
            st="$(rdocker inspect -f '{{.State.Running}} {{.RestartCount}}' "$NEW_CONTAINER" 2>/dev/null || true)"
            [[ $st == "true 0" ]] || die 8 "candidate healthy but state is '$st'"
            say "candidate $NEW_CONTAINER healthy on $UP_IP:$NEW_PORT (health, api auth layer, marker '$MARKER')"
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
            R_ASSET_VERSION="$ASSET_VERSION" R_ASSET_STAMP="$ASSET_STAMP" R_PURGE_STATUS="$PURGE_STATUS" R_PURGE_JSON="$PURGE_JSON" \
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
