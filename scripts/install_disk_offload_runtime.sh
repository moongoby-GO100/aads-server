#!/usr/bin/env bash
# disk_offload_cafe24.sh 를 dirty 메인 작업트리와 분리된 "SHA 고정 실행본"으로 설치한다.
#
#   <runtime-root>/<SHA>/   disk_offload_cafe24.sh (git 객체에서 추출) + run.sh + MANIFEST
#   <runtime-root>/current  -> <SHA>   (원자적 symlink 교체)
#   <runtime-root>/backups  crontab 백업,  <runtime-root>/state/records  활성화별 rollback 기록(쌓임)
#
# 기본은 plan 이다. --install / --activate / --rollback 을 명시할 때만 디스크·crontab 이 바뀐다.
# 자동 fetch 없음(base-ref 는 이미 가져온 로컬 ref), 작업트리 파일 복사 없음(git 객체만 사용).
# 크론은 aads-disk-offload 표지가 붙은 단 한 줄의 명령 경로만 바꾼다(스케줄·주석·다른 줄 보존).
set -uo pipefail

SCRIPT_REL='scripts/disk_offload_cafe24.sh'
MARKER='# aads-disk-offload'
SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
SELF_REPO_DEFAULT="$(dirname -- "$SELF")/.."

REPO="${AADS_OFFLOAD_RUNTIME_REPO:-$SELF_REPO_DEFAULT}"
BASE_REF="${AADS_OFFLOAD_RUNTIME_BASE_REF:-origin/main}"
RUNTIME_ROOT="${AADS_OFFLOAD_RUNTIME_ROOT:-/root/aads/runtime/disk-offload}"
CRONTAB_BIN="${AADS_CRONTAB_BIN:-crontab}"
LOG_DEFAULT='/root/aads/logs/disk_offload_cafe24.log'

SHA=''
EXPECT_SHA256=''
DO_INSTALL=0
DO_ACTIVATE=0
DO_ROLLBACK=0
SKIP_CRON=0

usage() {
    cat >&2 <<'EOF'
usage: install_disk_offload_runtime.sh --sha <40hex> [options]
       install_disk_offload_runtime.sh --rollback [--runtime-root DIR]

  (no action flag)   plan only: prints pinned SHA, paths, the one cron line, rollback. Changes nothing.
  --install          stage <runtime-root>/<SHA>/ (verified). Does not touch current or crontab.
  --activate         switch runtime current -> <SHA> and repoint the aads-disk-offload cron line.
                     Needs a staged release; combine with --install to do both.
  --skip-cron        with --activate: switch current only, leave crontab alone.
  --rollback         undo the most recent --activate only (its own pre-state record); repeat to step further back.
  --expect-sha256 H  require the extracted script's sha256 to equal H (out-of-band pin).
  --repo DIR         git repository holding the approved commit (default: this script's repo).
  --base-ref REF     approved ref the SHA must be contained in (default: origin/main; never fetched).
  --runtime-root DIR default /root/aads/runtime/disk-offload (must be outside the repo worktree).
EOF
    exit 2
}

while (( $# )); do
    case "$1" in
        --sha) SHA="${2:-}"; shift 2 || usage ;;
        --expect-sha256) EXPECT_SHA256="${2:-}"; shift 2 || usage ;;
        --repo) REPO="${2:-}"; shift 2 || usage ;;
        --base-ref) BASE_REF="${2:-}"; shift 2 || usage ;;
        --runtime-root) RUNTIME_ROOT="${2:-}"; shift 2 || usage ;;
        --install) DO_INSTALL=1; shift ;;
        --activate) DO_ACTIVATE=1; shift ;;
        --skip-cron) SKIP_CRON=1; shift ;;
        --rollback) DO_ROLLBACK=1; shift ;;
        --plan) shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

say() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$RUNTIME_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "runtime root must be an absolute path without spaces: $RUNTIME_ROOT"
RUNTIME_ROOT="$(realpath -m -- "$RUNTIME_ROOT")"
TARGET_CMD="$RUNTIME_ROOT/current/run.sh"
CURRENT="$RUNTIME_ROOT/current"
STATE_DIR="$RUNTIME_ROOT/state"
BACKUP_DIR="$RUNTIME_ROOT/backups"

if (( DO_ROLLBACK )) && (( DO_INSTALL || DO_ACTIVATE )); then
    die '--rollback cannot be combined with --install/--activate'
fi
if (( ! DO_ROLLBACK )); then
    [[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die '--sha must be a full 40-hex commit SHA (no refs, no "latest")'
fi
if [[ -n "$EXPECT_SHA256" && ! "$EXPECT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    die '--expect-sha256 must be 64 lowercase hex'
fi

# ---------------------------------------------------------------- recovery state
IN_FLIGHT=0
PREP_DIR=''
STAGE_DIR=''
RECOVER_CRON_BACKUP=''
RECOVER_CURRENT=0
RECOVER_PREV_SHA=''
RECOVER_STATE_FILES=()

recover_partial() {
    local f
    warn 'recovering from a partial activation'
    if [[ -n "$RECOVER_CRON_BACKUP" && -f "$RECOVER_CRON_BACKUP" ]]; then
        timeout 30 "$CRONTAB_BIN" "$RECOVER_CRON_BACKUP" \
            && warn "crontab restored from $RECOVER_CRON_BACKUP" \
            || warn "CRONTAB RESTORE FAILED; restore manually: $CRONTAB_BIN $RECOVER_CRON_BACKUP"
    fi
    if (( RECOVER_CURRENT )); then
        if [[ -n "$RECOVER_PREV_SHA" ]]; then
            switch_current "$RECOVER_PREV_SHA" && warn "current restored to $RECOVER_PREV_SHA" \
                || warn "CURRENT RESTORE FAILED; run: ln -sfn $RECOVER_PREV_SHA $CURRENT"
        elif [[ -L "$CURRENT" ]]; then
            rm -f -- "$CURRENT" && warn 'current pointer removed (there was none before)'
        fi
    fi
    for f in "${RECOVER_STATE_FILES[@]}"; do rm -f -- "$f"; done
}

cleanup() {
    local rc=$?
    if (( IN_FLIGHT && rc != 0 )); then recover_partial; fi
    [[ -n "$PREP_DIR" ]] && rm -rf -- "$PREP_DIR"
    [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]] && rm -rf -- "$STAGE_DIR"
    return 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------- git side
verify_commit() {
    local resolved
    command -v git >/dev/null || die 'git not found'
    git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository: $REPO"
    resolved="$(timeout 30 git -C "$REPO" rev-parse --verify --quiet "${SHA}^{commit}")" \
        || die "commit not found in $REPO: $SHA"
    [[ "$resolved" == "$SHA" ]] || die "SHA did not resolve to itself ($resolved)"
    timeout 30 git -C "$REPO" rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null \
        || die "base ref $BASE_REF not found locally (this tool never fetches; fetch explicitly first)"
    timeout 30 git -C "$REPO" merge-base --is-ancestor "$SHA" "$BASE_REF" \
        || die "$SHA is not contained in $BASE_REF; only approved origin/main commits can be pinned"
    REPO_TOP="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null)" || REPO_TOP=''
    if [[ -n "$REPO_TOP" ]]; then
        REPO_TOP="$(realpath -m -- "$REPO_TOP")"
        if [[ "$RUNTIME_ROOT" == "$REPO_TOP" || "$RUNTIME_ROOT" == "$REPO_TOP"/* ]]; then
            die "runtime root $RUNTIME_ROOT is inside the git worktree $REPO_TOP"
        fi
    fi
}

# 커밋된 blob 만 dest 에 추출하고 blob id·bash -n·의존성을 검증한다. 작업트리 파일은 읽지 않는다.
prepare_script() {
    local dest="$1" mode blob_expect blob_actual
    mode="$(timeout 30 git -C "$REPO" ls-tree "$SHA" -- "$SCRIPT_REL" | awk '{print $1}')"
    [[ "$mode" == 100644 || "$mode" == 100755 ]] || die "$SCRIPT_REL is missing or not a regular file at $SHA (mode=${mode:-none})"
    blob_expect="$(git -C "$REPO" rev-parse "$SHA:$SCRIPT_REL")" || die "cannot resolve blob for $SCRIPT_REL"
    timeout 60 git -C "$REPO" cat-file blob "$blob_expect" >"$dest" || die 'git cat-file failed'
    blob_actual="$(git -C "$REPO" hash-object -- "$dest")"
    [[ "$blob_actual" == "$blob_expect" ]] || die "extracted content hash ($blob_actual) != committed blob ($blob_expect)"
    bash -n "$dest" || die "bash -n failed for $SCRIPT_REL at $SHA"
    if grep -Eq '^[[:space:]]*(source|\.)[[:space:]]+[^[:space:]]' "$dest"; then
        die "$SCRIPT_REL sources another file; that dependency is not pinned by this installer"
    fi
    BLOB_ID="$blob_expect"
    SCRIPT_SHA256="$(sha256sum -- "$dest" | awk '{print $1}')"
    if [[ -n "$EXPECT_SHA256" && "$EXPECT_SHA256" != "$SCRIPT_SHA256" ]]; then
        die "script sha256 $SCRIPT_SHA256 != --expect-sha256 $EXPECT_SHA256"
    fi
}

# ---------------------------------------------------------------- release dir
manifest_get() { sed -n "s/^$2=//p" "$1/MANIFEST" 2>/dev/null | head -n 1; }

verify_release() {
    local rel="$1" want_sha="$2" got
    [[ -d "$rel" && ! -L "$rel" ]] || { warn "release dir missing: $rel"; return 1; }
    [[ -f "$rel/disk_offload_cafe24.sh" && -f "$rel/run.sh" && -f "$rel/MANIFEST" ]] || { warn "release incomplete: $rel"; return 1; }
    [[ "$(manifest_get "$rel" source_sha)" == "$want_sha" ]] || { warn "MANIFEST source_sha mismatch in $rel"; return 1; }
    got="$(sha256sum -- "$rel/disk_offload_cafe24.sh" | awk '{print $1}')"
    [[ "$got" == "$(manifest_get "$rel" script_sha256)" ]] || { warn "script sha256 differs from MANIFEST in $rel"; return 1; }
    got="$(sha256sum -- "$rel/run.sh" | awk '{print $1}')"
    [[ "$got" == "$(manifest_get "$rel" launcher_sha256)" ]] || { warn "launcher sha256 differs from MANIFEST in $rel"; return 1; }
    bash -n "$rel/disk_offload_cafe24.sh" || { warn "bash -n failed in $rel"; return 1; }
    bash -n "$rel/run.sh" || { warn "bash -n failed for launcher in $rel"; return 1; }
    return 0
}

write_launcher() {
    cat >"$1" <<'LAUNCHER'
#!/usr/bin/env bash
# SHA-pinned launcher for disk_offload_cafe24.sh (generated by install_disk_offload_runtime.sh).
# Verifies the pinned script against MANIFEST, logs the version, then execs it. Never fetches or updates.
set -uo pipefail
here="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
log="${AADS_OFFLOAD_LOG:-/root/aads/logs/disk_offload_cafe24.log}"
mkdir -p -- "$(dirname -- "$log")" 2>/dev/null
sha="$(sed -n 's/^source_sha=//p' "$here/MANIFEST" | head -n 1)"
want="$(sed -n 's/^script_sha256=//p' "$here/MANIFEST" | head -n 1)"
got="$(sha256sum -- "$here/disk_offload_cafe24.sh" 2>/dev/null | awk '{print $1}')"
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ || ! "$want" =~ ^[0-9a-f]{64}$ || "$got" != "$want" ]]; then
    printf '%s ERROR pinned runtime verification failed sha=%s want=%s got=%s dir=%s\n' \
        "$(date '+%F %T')" "${sha:-none}" "${want:-none}" "${got:-none}" "$here" >>"$log"
    exit 1
fi
printf '%s RUNTIME pinned sha=%s script_sha256=%s dir=%s\n' "$(date '+%F %T')" "$sha" "$got" "$here" >>"$log"
exec bash "$here/disk_offload_cafe24.sh" "$@"
LAUNCHER
}

stage_release() {
    local rel="$RUNTIME_ROOT/$SHA"
    mkdir -p -- "$RUNTIME_ROOT" || die "cannot create $RUNTIME_ROOT"
    if [[ -e "$rel" ]]; then
        verify_release "$rel" "$SHA" || die "existing release $rel failed verification; it is immutable and was not overwritten"
        [[ "$(manifest_get "$rel" script_sha256)" == "$SCRIPT_SHA256" ]] \
            || die "existing release $rel has a different script hash than the committed blob"
        say "release already staged and verified: $rel"
        return 0
    fi
    STAGE_DIR="$(mktemp -d "$RUNTIME_ROOT/.stage.XXXXXX")" || die 'cannot create stage dir'
    prepare_script "$STAGE_DIR/disk_offload_cafe24.sh"
    write_launcher "$STAGE_DIR/run.sh"
    chmod 555 "$STAGE_DIR/disk_offload_cafe24.sh" "$STAGE_DIR/run.sh"
    {
        printf 'source_sha=%s\n' "$SHA"
        printf 'script_path=%s\n' "$SCRIPT_REL"
        printf 'script_blob=%s\n' "$BLOB_ID"
        printf 'script_sha256=%s\n' "$SCRIPT_SHA256"
        printf 'launcher_sha256=%s\n' "$(sha256sum -- "$STAGE_DIR/run.sh" | awk '{print $1}')"
        printf 'installed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$STAGE_DIR/MANIFEST"
    chmod 444 "$STAGE_DIR/MANIFEST"
    verify_release "$STAGE_DIR" "$SHA" || die 'staged release failed verification'
    mv -T -- "$STAGE_DIR" "$rel" || die "cannot publish release dir $rel"
    STAGE_DIR=''
    say "staged release: $rel (script_sha256=$SCRIPT_SHA256)"
}

# rc 0: 설치기가 만드는 정규 형태(<40hex> 또는 $RUNTIME_ROOT/<40hex>)이며 그 SHA 를 출력.
# rc 1: current 없음.  rc 2: 있지만 정규 형태가 아님(일반 파일/디렉터리/외부 경로 링크) — 건드리지 않는다.
current_sha() {
    local t
    if [[ ! -L "$CURRENT" ]]; then
        [[ -e "$CURRENT" ]] && return 2
        return 1
    fi
    t="$(readlink -- "$CURRENT")" || return 2
    [[ "$t" == "$RUNTIME_ROOT"/* ]] && t="${t#"$RUNTIME_ROOT"/}"
    [[ "$t" =~ ^[0-9a-f]{40}$ ]] || return 2
    printf '%s\n' "$t"
}

switch_current() {
    local tmp="$RUNTIME_ROOT/.current.new.$$"
    if [[ -e "$CURRENT" && ! -L "$CURRENT" ]]; then
        warn "$CURRENT exists and is not a symlink; refusing to replace"
        return 1
    fi
    rm -f -- "$tmp"
    ln -s -- "$1" "$tmp" && mv -T -- "$tmp" "$CURRENT"
}

# ---------------------------------------------------------------- crontab side
CRON_ERR=''
CRON_ACTION=''
CRON_OLD_LINE=''
CRON_NEW_LINE=''

is_active_line() {
    local t="${1#"${1%%[![:space:]]*}"}"
    [[ -n "$t" && "$t" != \#* ]]
}

# mode=target: 표지 줄의 명령 토큰만 $4 로 교체. mode=restore: 표지 줄 전체를 $4 로 교체.
build_cron() {
    local src="$1" out="$2" mode="$3" arg="$4" line n_marker=0 n_other=0
    local re='^([[:space:]]*([^[:space:]]+[[:space:]]+){5})([^[:space:]]+)(.*)$'
    CRON_ERR=''; CRON_ACTION=''; CRON_OLD_LINE=''; CRON_NEW_LINE=''
    while IFS= read -r line || [[ -n "$line" ]]; do
        is_active_line "$line" || continue
        if [[ "$line" == *"$MARKER"* ]]; then
            n_marker=$((n_marker + 1)); CRON_OLD_LINE="$line"
        elif [[ "$line" == *disk_offload_cafe24.sh* ]]; then
            n_other=$((n_other + 1))
        fi
    done <"$src"
    if (( n_marker != 1 )); then
        CRON_ERR="expected exactly one active '$MARKER' cron line, found $n_marker (nothing changed)"
        return 1
    fi
    if (( n_other != 0 )); then
        CRON_ERR="$n_other other active cron line(s) reference disk_offload_cafe24.sh without the marker (nothing changed)"
        return 1
    fi
    if [[ "$mode" == target ]]; then
        [[ "$CRON_OLD_LINE" =~ $re ]] || { CRON_ERR='aads-disk-offload line is not a 5-field cron entry'; return 1; }
        local prefix="${BASH_REMATCH[1]}" cmd="${BASH_REMATCH[3]}" rest="${BASH_REMATCH[4]}"
        if [[ "$cmd" == "$arg" ]]; then
            CRON_ACTION=noop; CRON_NEW_LINE="$CRON_OLD_LINE"
        elif [[ "$cmd" == */disk_offload_cafe24.sh ]]; then
            CRON_ACTION=replace; CRON_NEW_LINE="${prefix}${arg}${rest}"
        else
            CRON_ERR="unexpected command '$cmd' on the aads-disk-offload line (nothing changed)"
            return 1
        fi
    else
        if [[ "$CRON_OLD_LINE" == "$arg" ]]; then CRON_ACTION=noop; else CRON_ACTION=replace; fi
        CRON_NEW_LINE="$arg"
    fi
    : >"$out"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if is_active_line "$line" && [[ "$line" == *"$MARKER"* ]]; then
            printf '%s\n' "$CRON_NEW_LINE" >>"$out"
        else
            printf '%s\n' "$line" >>"$out"
        fi
    done <"$src"
    return 0
}

# 현재 crontab 을 $1 파일로 읽는다. crontab 이 없으면 빈 파일. 읽기 실패는 1.
read_crontab() {
    local dest="$1" out rc
    out="$(timeout 30 "$CRONTAB_BIN" -l 2>&1)"; rc=$?
    if (( rc != 0 )); then
        if [[ "$out" == *'no crontab for'* ]]; then : >"$dest"; return 0; fi
        CRON_ERR="cannot read crontab: ${out:0:200}"
        return 1
    fi
    timeout 30 "$CRONTAB_BIN" -l >"$dest" 2>/dev/null
}

write_crontab_verified() {
    local file="$1" readback
    readback="$(mktemp "$PREP_DIR/readback.XXXXXX")" || return 1
    timeout 30 "$CRONTAB_BIN" "$file" || return 1
    timeout 30 "$CRONTAB_BIN" -l >"$readback" 2>/dev/null || return 1
    cmp -s -- "$file" "$readback"
}

# ---------------------------------------------------------------- plan
print_plan() {
    local cur tz_lines
    say '== aads-disk-offload pinned runtime PLAN (no changes made) =='
    say "pinned_sha:       $SHA"
    say "base_ref:         $BASE_REF (contains pinned sha; not fetched)"
    say "script:           $SCRIPT_REL blob=$BLOB_ID"
    say "script_sha256:    $SCRIPT_SHA256"
    say "bash -n:          ok;  external sourced dependencies: none"
    say "runtime_root:     $RUNTIME_ROOT  (outside git worktree ${REPO_TOP:-?})"
    say "release_dir:      $RUNTIME_ROOT/$SHA  $([[ -d "$RUNTIME_ROOT/$SHA" ]] && echo '[already staged]' || echo '[to be created]')"
    cur="$(current_sha 2>/dev/null)"
    case $? in
        0) ;;
        1) cur='(none)' ;;
        *) cur='(NOT a canonical pointer; --activate would refuse)' ;;
    esac
    say "current:          $cur -> $SHA"
    say "log/lock (unchanged): ${AADS_OFFLOAD_LOG:-$LOG_DEFAULT} , /tmp/aads-disk-offload.lock"
    say "host_tz:          $(date +%Z%z) $(cat /etc/timezone 2>/dev/null)"
    if read_crontab "$PREP_DIR/cron.src"; then
        tz_lines="$(grep -E '^[[:space:]]*(CRON_)?TZ=' "$PREP_DIR/cron.src" || true)"
        say "crontab_tz_vars:  ${tz_lines:-none (schedule runs in host time; fields preserved verbatim)}"
        if build_cron "$PREP_DIR/cron.src" "$PREP_DIR/cron.new" target "$TARGET_CMD"; then
            say "cron_action:      $CRON_ACTION"
            say "cron.old:         $CRON_OLD_LINE"
            say "cron.new:         $CRON_NEW_LINE"
        else
            say "cron_action:      BLOCKED - $CRON_ERR"
        fi
    else
        say "cron_action:      BLOCKED - $CRON_ERR"
    fi
    say "backups:          $BACKUP_DIR/crontab.<UTC>.<pid>.bak (full crontab), $STATE_DIR/records/<seq>.rec (one per activation: previous current + cron line)"
    say 'install:'
    say "  bash $SELF --sha $SHA --expect-sha256 $SCRIPT_SHA256 --runtime-root $RUNTIME_ROOT --install --activate"
    say 'rollback:'
    say "  bash $SELF --rollback --runtime-root $RUNTIME_ROOT"
    say "  (manual, cron line only) restore the cron.old line above in 'crontab -e'; full copy: $CRONTAB_BIN $BACKUP_DIR/crontab.<UTC>.<pid>.bak"
}

# ---------------------------------------------------------------- activate / rollback
# 활성화 한 번 = records/<seq>.rec 한 개(활성화 "직전" 상태). 기록은 덮어쓰지 않고 쌓으며,
# --rollback 은 가장 최근 기록 하나만 되돌린 뒤 rolled_back/ 으로 옮긴다(여러 번 호출하면 차례로 거슬러 올라감).
rec_get() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -n 1; }

next_seq() {
    local f last='' n
    for f in "$1"/*.rec; do [[ -f "$f" ]] && last="$f"; done
    n="${last##*/}"; n="${n%.rec}"
    printf '%08d' "$(( 10#${n:-0} + 1 ))"
}

do_activate() {
    local rel="$RUNTIME_ROOT/$SHA" prev_sha='' had_current=0 ts backup='' cron_changes=0 cur_changes=0
    local rec_dir="$STATE_DIR/records" rec seq cur_rc
    verify_release "$rel" "$SHA" || die "release $rel is not staged or failed verification (use --install first)"
    [[ "$(manifest_get "$rel" script_sha256)" == "$SCRIPT_SHA256" ]] || die 'staged script hash differs from committed blob'
    mkdir -p -- "$rec_dir" "$BACKUP_DIR" || die 'cannot create state/backup dirs'
    ts="$(date -u +%Y%m%dT%H%M%SZ).$$"

    prev_sha="$(current_sha)"; cur_rc=$?
    case $cur_rc in
        0) had_current=1 ;;
        1) prev_sha='' ;;
        *) die "$CURRENT exists but is not a canonical pointer to $RUNTIME_ROOT/<sha> ($(readlink -- "$CURRENT" 2>/dev/null || echo 'not a symlink')); refusing to touch it (nothing changed)" ;;
    esac
    [[ "$prev_sha" == "$SHA" ]] || cur_changes=1

    if (( ! SKIP_CRON )); then
        read_crontab "$PREP_DIR/cron.src" || die "$CRON_ERR"
        build_cron "$PREP_DIR/cron.src" "$PREP_DIR/cron.new" target "$TARGET_CMD" || die "$CRON_ERR"
        [[ "$CRON_ACTION" == replace ]] && cron_changes=1
        if (( cron_changes )); then
            [[ "$(diff <(sed -e '$a\' "$PREP_DIR/cron.src") "$PREP_DIR/cron.new" | grep -c '^[<>]')" == 2 ]] \
                || die 'internal check failed: more than one crontab line would change'
        fi
    fi

    if (( ! cur_changes && ! cron_changes )); then
        say "already active: current -> $SHA and cron already points at $TARGET_CMD (no change)"
        return 0
    fi

    IN_FLIGHT=1
    if (( cron_changes )); then
        backup="$BACKUP_DIR/crontab.$ts.bak"
        cp -- "$PREP_DIR/cron.src" "$backup" || die "cannot write crontab backup $backup"
        say "crontab backup: $backup"
    fi

    # 되돌릴 정보는 어떤 것도 바꾸기 전에, 이번 활성화 전용 파일로 저장한다. 하나라도 실패하면 아직 아무것도 안 바뀐 상태로 중단.
    seq="$(next_seq "$rec_dir")" || die 'cannot compute rollback record sequence'
    rec="$rec_dir/$seq.rec"
    RECOVER_STATE_FILES+=("$rec.new" "$rec")
    {
        printf 'new_sha=%s\n' "$SHA"
        printf 'prev_current=%s\n' "$( (( had_current )) && printf '%s' "$prev_sha" || printf 'NONE' )"
        printf 'cron_changed=%s\n' "$cron_changes"
        printf 'prev_cron_line=%s\n' "$( (( cron_changes )) && printf '%s' "$CRON_OLD_LINE" )"
        printf 'backup=%s\n' "$backup"
        printf 'recorded_at=%s\n' "$ts"
    } >"$rec.new" || die "cannot save rollback record $rec.new (nothing was changed)"
    [[ "$(rec_get "$rec.new" new_sha)" == "$SHA" && "$(rec_get "$rec.new" recorded_at)" == "$ts" ]] \
        || die "rollback record $rec.new was not written completely (nothing was changed)"
    mv -T -- "$rec.new" "$rec" || die "cannot publish rollback record $rec (nothing was changed)"

    if (( cur_changes )); then
        (( had_current )) && RECOVER_PREV_SHA="$prev_sha"
        switch_current "$SHA" || die 'atomic current switch failed'
        RECOVER_CURRENT=1
        [[ "$(current_sha)" == "$SHA" ]] || die 'current did not point at the pinned SHA after switch'
        say "current: ${prev_sha:-(none)} -> $SHA"
    fi
    if (( cron_changes )); then
        RECOVER_CRON_BACKUP="$backup"
        write_crontab_verified "$PREP_DIR/cron.new" || die 'crontab write or read-back verification failed'
        say "cron.old: $CRON_OLD_LINE"
        say "cron.new: $CRON_NEW_LINE"
    fi
    IN_FLIGHT=0
    say "rollback record: $rec"
    say "activated pinned sha=$SHA script_sha256=$SCRIPT_SHA256"
    say "rollback: bash $SELF --rollback --runtime-root $RUNTIME_ROOT"
}

do_rollback() {
    local rec_dir="$STATE_DIR/records" archive_dir="$STATE_DIR/rolled_back"
    local rec='' f prev saved_line='' cron_changed new_sha ts backup
    for f in "$rec_dir"/*.rec; do [[ -f "$f" ]] && rec="$f"; done
    [[ -n "$rec" ]] || { say 'nothing to roll back (no saved state)'; return 0; }

    prev="$(rec_get "$rec" prev_current)"
    cron_changed="$(rec_get "$rec" cron_changed)"
    new_sha="$(rec_get "$rec" new_sha)"
    [[ "$prev" == NONE || "$prev" =~ ^[0-9a-f]{40}$ ]] || die "invalid prev_current in $rec"
    [[ "$cron_changed" == 0 || "$cron_changed" == 1 ]] || die "invalid cron_changed in $rec"

    # 바꾸기 전에 모든 선행조건을 확인한다.
    mkdir -p -- "$BACKUP_DIR" "$archive_dir" || die 'cannot create backup/archive dir (nothing was changed)'
    ts="$(date -u +%Y%m%dT%H%M%SZ).$$"
    if [[ "$prev" != NONE ]]; then
        verify_release "$RUNTIME_ROOT/$prev" "$prev" || die "previous release $prev failed verification (nothing was changed)"
    fi
    current_sha >/dev/null 2>&1
    if [[ $? -eq 2 ]]; then
        die "$CURRENT exists but is not a canonical pointer to $RUNTIME_ROOT/<sha>; refusing to touch it (nothing was changed)"
    fi
    if [[ "$(current_sha 2>/dev/null)" != "$new_sha" ]]; then
        warn "current is '$(current_sha 2>/dev/null || echo none)', not the '$new_sha' this record activated; rolling back its saved pre-state anyway"
    fi
    if [[ "$cron_changed" == 1 ]]; then
        saved_line="$(rec_get "$rec" prev_cron_line)"
        [[ -n "$saved_line" ]] || die "saved cron line is empty in $rec"
        read_crontab "$PREP_DIR/cron.src" || die "$CRON_ERR"
        build_cron "$PREP_DIR/cron.src" "$PREP_DIR/cron.new" restore "$saved_line" || die "$CRON_ERR"
        if [[ "$CRON_ACTION" == replace ]]; then
            backup="$BACKUP_DIR/crontab.$ts.rollback.bak"
            cp -- "$PREP_DIR/cron.src" "$backup" || die 'cannot write crontab backup (nothing was changed)'
            RECOVER_CRON_BACKUP="$backup"; IN_FLIGHT=1
            write_crontab_verified "$PREP_DIR/cron.new" || die 'crontab write or read-back verification failed'
            say "cron restored: $saved_line"
        else
            say 'cron line already equals the saved line (no change)'
        fi
    else
        say 'this activation did not change the cron line; cron left as is'
    fi
    if [[ "$prev" == NONE ]]; then
        [[ -L "$CURRENT" ]] && { rm -f -- "$CURRENT" || die 'cannot remove current'; }
        say 'current removed (there was none before the activation)'
    else
        switch_current "$prev" || die 'atomic current switch failed'
        say "current restored -> $prev"
    fi
    IN_FLIGHT=0
    mv -T -- "$rec" "$archive_dir/${rec##*/}.$ts" \
        || die "ROLLED BACK, but could not archive $rec; move it away manually (mv $rec $archive_dir/) or the next --rollback will undo an older activation too"
    say "rollback complete (record archived in $archive_dir)"
}

# ---------------------------------------------------------------- main
PREP_DIR="$(mktemp -d /tmp/aads-offload-runtime.XXXXXX)" || die 'mktemp failed'

if (( DO_ROLLBACK )); then
    [[ -d "$RUNTIME_ROOT" ]] || die "runtime root does not exist: $RUNTIME_ROOT"
    exec 7>"$RUNTIME_ROOT/.install.lock"
    flock -n 7 || die 'another installer run holds the lock'
    do_rollback
    exit 0
fi

verify_commit
prepare_script "$PREP_DIR/disk_offload_cafe24.sh"

if (( ! DO_INSTALL && ! DO_ACTIVATE )); then
    print_plan
    exit 0
fi

mkdir -p -- "$RUNTIME_ROOT" || die "cannot create $RUNTIME_ROOT"
exec 7>"$RUNTIME_ROOT/.install.lock"
flock -n 7 || die 'another installer run holds the lock'
(( DO_INSTALL )) && stage_release
(( DO_ACTIVATE )) && do_activate
exit 0
