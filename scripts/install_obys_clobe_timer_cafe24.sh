#!/bin/bash
# contabo116 → cafe24(server-114) 에 클로브 수집 1시간 타이머를 설치한다. 지시 세션이 승인 후 직접 실행한다.
#
#   install_obys_clobe_timer_cafe24.sh [--dry-run]
#
# --dry-run: ssh 를 한 번도 부르지 않고, 할 일과 사전 검사 결과만 출력한다.
# 설치 대상(cafe24):
#   /srv/acct/bin/obys_clobe_collect_tick.sh           (755)
#   /etc/systemd/system/obys-clobe-collect.service     (644)
#   /etc/systemd/system/obys-clobe-collect.timer       (644)
# 접속 방식은 deploy_acct_app_cafe24.sh 와 같다(CAFE24_SSH=server-114, BatchMode, 호출마다 timeout).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${OBYS_TIMER_SRC_DIR:-$SCRIPT_DIR/../deploy/acct}"
CAFE24_SSH="${CAFE24_SSH:-server-114}"
SSH_DEFAULT_TIMEOUT="${ACCT_SSH_TIMEOUT:-60}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

WRAPPER_DEST="/srv/acct/bin/obys_clobe_collect_tick.sh"
UNIT_DIR="/etc/systemd/system"
SERVICE_NAME="obys-clobe-collect.service"
TIMER_NAME="obys-clobe-collect.timer"

DRY_RUN=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "usage: $0 [--dry-run]" >&2; exit 64 ;;
esac

say() { echo "[install_obys_clobe_timer] $*"; }
die() { say "FAIL: $*" >&2; exit 1; }

ssh_c() { # ssh_c <timeout-seconds> <remote command...>
    local t=$1
    shift
    timeout -k 10 "$t" ssh "${SSH_OPTS[@]}" "$CAFE24_SSH" "$@"
}

push_file() { # push_file <local> <mode> <remote-path>
    local src=$1 mode=$2 dest=$3
    if ((DRY_RUN)); then
        say "dry-run: install -m $mode $(basename "$src") -> $CAFE24_SSH:$dest"
        return 0
    fi
    ssh_c "$SSH_DEFAULT_TIMEOUT" "t=\$(mktemp /tmp/obys-install.XXXXXX) && cat >\"\$t\" && install -m $mode \"\$t\" $dest; rc=\$?; rm -f \"\$t\"; exit \$rc" \
        <"$src" || die "install of $dest failed"
}

remote() { # remote <timeout-seconds> <command string>
    if ((DRY_RUN)); then
        say "dry-run: ssh $CAFE24_SSH: $2"
        return 0
    fi
    ssh_c "$1" "$2" || die "remote command failed: $2"
}

# ---- 사전 검사 (로컬, 실패하면 아무것도 보내지 않는다) ----
for f in obys_clobe_collect_tick.sh "systemd/$SERVICE_NAME" "systemd/$TIMER_NAME"; do
    [[ -f $SRC_DIR/$f ]] || die "missing source $SRC_DIR/$f"
done
bash -n "$SRC_DIR/obys_clobe_collect_tick.sh" || die "wrapper has a syntax error"
exec_start=$(sed -n 's/^ExecStart=//p' "$SRC_DIR/systemd/$SERVICE_NAME")
[[ $exec_start == "$WRAPPER_DEST" ]] || die "unit ExecStart '$exec_start' != install path '$WRAPPER_DEST'"
[[ $exec_start != /root/aads* ]] || die "unit ExecStart must not point into /root/aads (absent on $CAFE24_SSH)"
say "preflight ok: ExecStart=$exec_start (dry_run=$DRY_RUN, target=$CAFE24_SSH)"

# ---- 설치 ----
remote 30 "install -d -m 755 /srv/acct/bin"
push_file "$SRC_DIR/obys_clobe_collect_tick.sh" 755 "$WRAPPER_DEST"
push_file "$SRC_DIR/systemd/$SERVICE_NAME" 644 "$UNIT_DIR/$SERVICE_NAME"
push_file "$SRC_DIR/systemd/$TIMER_NAME" 644 "$UNIT_DIR/$TIMER_NAME"
remote 30 "systemctl daemon-reload"
remote 30 "systemctl enable --now $TIMER_NAME"
remote 30 "systemctl list-timers $TIMER_NAME --no-pager"
say "done"
