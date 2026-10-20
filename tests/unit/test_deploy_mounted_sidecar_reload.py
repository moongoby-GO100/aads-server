"""Mounted-sidecar topology contract for deploy.sh blue/green releases.

Static tests pin ordering/wiring; behavioral tests run the real shell functions
extracted from deploy.sh against stubbed docker/curl/ss.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

DEPLOY = (Path(__file__).parents[2] / "deploy.sh").read_text(encoding="utf-8")

LOCAL_ROUTE = "127.0.0.1:8110"
REMOTE_ROUTE = "127.0.0.1:8210"
SSH_LISTENER = 'LISTEN 0 128 127.0.0.1:8210 0.0.0.0:* users:(("ssh",pid=753,fd=4))'
APPROVED_HOST = "114.207.244.86"  # ACCT cafe24_114
OLD244_HOST = "5.104.85.244"  # retired jinah244, still the live tunnel on 2026-10-20
TUNNEL_BASE = ["/usr/bin/ssh", "-NT", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes"]
PROXY_LISTENER = 'LISTEN 0 4096 127.0.0.1:8210 0.0.0.0:* users:(("docker-proxy",pid=1,fd=8))'


def _function_body(name: str) -> str:
    return DEPLOY.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]


def _topology_source() -> str:
    start = DEPLOY.index('FINANCE_ROUTE_REMOTE_TUNNEL="')
    end = DEPLOY.index("schedule_standby_sync_retry() {")
    return DEPLOY[start:end]


def _tunnel(dest, fwd="127.0.0.1:8210:127.0.0.1:8110"):
    return [*TUNNEL_BASE, "-L", fwd, dest]


def _run(
    tmp_path,
    *,
    routes,
    present=(),
    healthy=(),
    public="401",
    ss_out="",
    env=None,
    cmdline=None,
    aliases=None,
    ssh_g_fail=False,
    ssh_g_extra="",
    ssh_g_style="bracket",
    real_ssh_g=False,
    extra_shell="",
):
    conf = tmp_path / "upstream.conf"
    servers = "\n".join(f"    server {r} max_fails=3;" for r in routes)
    conf.write_text(
        "upstream other {\n    server 127.0.0.1:9999;\n}\n"
        f"upstream yeoljeong_finance_api {{\n    zone y 64k;\n{servers}\n"
        "    # server 127.0.0.1:1111; commented\n    keepalive 16;\n}\n",
        encoding="utf-8",
    )
    calls = tmp_path / "calls.log"
    script = tmp_path / "harness.sh"
    # real_ssh_g keeps the production finance_ssh_resolve (timeout 15 ssh -G).
    resolve_override = "" if real_ssh_g else 'finance_ssh_resolve() { ssh -G "$@"; }'
    script.write_text(
        textwrap.dedent(
            f"""\
            set -uo pipefail
            UPSTREAM_CONF={conf}
            CALLS={calls}
            PRESENT=" {' '.join(present)} "
            HEALTHY=" {' '.join(healthy)} "
            PUBLIC_CODE={public}
            SS_OUT='{ss_out}'
            CMDLINE=({' '.join(repr(a) for a in (cmdline if cmdline is not None else _tunnel(APPROVED_HOST)))})
            ALIASES=({' '.join(repr(f'{k}={v}') for k, v in (aliases or {}).items())})
            SSH_G_FAIL={'1' if ssh_g_fail else ''}
            SSH_G_EXTRA='{ssh_g_extra}'
            SSH_G_STYLE='{ssh_g_style}'
            deploy_phase_start() {{ echo "start $1" >> "$CALLS"; }}
            deploy_phase_end() {{ echo "end $1 $2" >> "$CALLS"; }}
            notify() {{ :; }}
            sleep() {{ :; }}
            ss() {{ printf '%s\\n' "$SS_OUT"; }}
            finance_proc_cmdline_stub() {{ printf '%s\\n' "${{CMDLINE[@]}}"; }}
            fmt_ep() {{
                if [[ "$SSH_G_STYLE" == bare ]]; then printf '%s:%s' "$1" "$2"; else printf '[%s]:%s' "$1" "$2"; fi
            }}
            ssh() {{
                if [[ "$1" != -G ]]; then echo "ssh-REAL-CONNECT $*" >> "$CALLS"; return 99; fi
                shift
                echo "ssh -G $*" >> "$CALLS"
                [[ -n "$SSH_G_FAIL" ]] && return 255
                local a prev="" dest="" fwds=() host kv colons rest bind_a lport thost tport
                for a in "$@"; do
                    [[ "$prev" == -L ]] && fwds+=("$a")
                    prev="$a"
                    dest="$a"
                done
                host="${{dest##*@}}"
                for kv in "${{ALIASES[@]}}"; do [[ "${{kv%%=*}}" == "$host" ]] && host="${{kv#*=}}"; done
                printf 'user partner\nhostname %s\nport 22\nproxyusefdpass no\n' "$host"
                for a in "${{fwds[@]}}"; do
                    colons="${{a//[^:]/}}"
                    if (( ${{#colons}} == 3 )); then
                        bind_a="${{a%%:*}}"; rest="${{a#*:}}"
                        lport="${{rest%%:*}}"; rest="${{rest#*:}}"
                        thost="${{rest%%:*}}"; tport="${{rest#*:}}"
                        printf 'localforward %s %s\n' "$(fmt_ep "$bind_a" "$lport")" "$(fmt_ep "$thost" "$tport")"
                    else
                        lport="${{a%%:*}}"; rest="${{a#*:}}"
                        thost="${{rest%%:*}}"; tport="${{rest#*:}}"
                        printf 'localforward %s %s\n' "$lport" "$(fmt_ep "$thost" "$tport")"
                    fi
                done
                [[ -n "$SSH_G_EXTRA" ]] && printf '%b\n' "$SSH_G_EXTRA"
                return 0
            }}
            docker() {{
                echo "docker $*" >> "$CALLS"
                if [[ "$1" == inspect ]]; then [[ "$PRESENT" == *" $2 "* ]]; return; fi
                return 0
            }}
            curl() {{
                local a url="" want_code=false
                for a in "$@"; do
                    [[ "$a" == "-w" ]] && want_code=true
                    [[ "$a" == http* ]] && url="$a"
                done
                echo "curl $url" >> "$CALLS"
                if $want_code; then printf '%s' "$PUBLIC_CODE"; return 0; fi
                [[ "$HEALTHY" == *" $url "* ]]
            }}
            {_topology_source()}
            finance_proc_cmdline() {{ finance_proc_cmdline_stub "$@"; }}
            {resolve_override}
            {extra_shell}
            "$@"
            """
        ),
        encoding="utf-8",
    )
    import os

    proc_env = {**os.environ, "DEPLOY_SIDECAR_HEALTH_WAIT": "2", "DEPLOY_SIDECAR_PREFLIGHT_WAIT": "2"}
    proc_env.pop("DEPLOY_MOUNTED_SIDECARS", None)
    proc_env.update(env or {})
    return script, calls, proc_env


_FRESH = iter(range(10**6))


def _fresh(tmp_path):
    d = tmp_path / f"run{next(_FRESH)}"
    d.mkdir()
    return d


def _call(tmp_path, fn, **kw):
    tmp_path = _fresh(tmp_path)
    script, calls, env = _run(tmp_path, **kw)
    proc = subprocess.run(
        ["bash", str(script), fn], capture_output=True, text=True, env=env, timeout=30
    )
    log = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return proc, log


LOCAL_OK = dict(
    routes=[LOCAL_ROUTE],
    present=["yeoljeong-finance"],
    healthy=["http://127.0.0.1:8110/health/live"],
)
REMOTE_OK = dict(
    routes=[REMOTE_ROUTE],
    healthy=["http://127.0.0.1:8210/health/live"],
    ss_out=SSH_LISTENER,
)


# ── static wiring ───────────────────────────────────────────────────────────
def test_preflight_runs_before_build_and_cutover():
    pre = DEPLOY.index("if ! preflight_mounted_sidecar_topology; then")
    build = DEPLOY.index("build_release_image\n", pre)
    cutover = DEPLOY.index('deploy_phase_start "nginx_cutover"')
    assert pre < build < cutover
    assert 'record_deploy "blocked"' in DEPLOY[pre : pre + 400]


def test_nginx_lock_is_released_before_sidecar_restart_wait():
    cutover_success = DEPLOY.index('deploy_phase_end "nginx_cutover" "success"')
    reload_call = DEPLOY.index("reload_mounted_sidecars", cutover_success)
    release_lock = DEPLOY.index("release_nginx_switch_lock", cutover_success)
    standby_sync = DEPLOY.index("sync_standby_slot_after_drain", reload_call)
    assert release_lock < reload_call < standby_sync


def test_failed_sidecar_reload_rolls_back_routing_and_never_reports_success():
    start = DEPLOY.index("if ! reload_mounted_sidecars; then")
    branch = DEPLOY[start : DEPLOY.index("# Phase 7 기준선", start)]
    assert "rollback_cutover_after_sidecar_failure" in branch
    ok, failed = branch.split("else", 1)
    assert "DEPLOY_UPSTREAM_SWITCHED=false" in ok
    assert "DEPLOY_UPSTREAM_SWITCHED=false" not in failed
    assert "routing rollback FAILED" in failed
    assert branch.count('record_deploy "failed"') == 2
    assert branch.index("exit 1") > branch.index("else")
    assert "standby" not in branch.lower()


def test_no_env_switch_can_skip_the_required_gate():
    assert "DEPLOY_MOUNTED_SIDECAR_OPTIONAL" not in DEPLOY
    assert "DEPLOY_FINANCE_TOPOLOGY" not in DEPLOY
    assert 'DEPLOY_MOUNTED_SIDECARS:-yeoljeong-finance' not in DEPLOY


def test_warmup_and_auth_contract_preserved_in_source():
    body = _function_body("reload_mounted_sidecars")
    assert "DEPLOY_SIDECAR_PUBLIC_CHECK_URL:-http://127.0.0.1/api/v1/yeoljeong-finance/health/live" in body
    assert "DEPLOY_SIDECAR_HEALTH_WAIT:-60" in body
    assert 'notify "❌ Blue-Green 인증 실패: ${failure}"' in body
    assert "200|401|403" in _function_body("finance_public_route_check")
    assert "curl -sf --max-time 5" in _function_body("finance_route_health_wait")


# ── behavior: local topology ────────────────────────────────────────────────
def test_local_absent_required_sidecar_blocks_preflight(tmp_path):
    proc, log = _call(tmp_path, "preflight_mounted_sidecar_topology", **{**LOCAL_OK, "present": []})
    assert proc.returncode == 1
    assert "end sidecar_topology_preflight blocked" in log
    assert "docker restart" not in log


def test_local_absent_required_sidecar_blocks_reload_without_restart(tmp_path):
    proc, log = _call(tmp_path, "reload_mounted_sidecars", **{**LOCAL_OK, "present": []})
    assert proc.returncode == 1
    assert "end mounted_sidecar_reload error" in log
    assert "docker restart" not in log


def test_local_present_sidecar_is_restarted_and_verified(tmp_path):
    proc, log = _call(tmp_path, "preflight_mounted_sidecar_topology", **LOCAL_OK)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "docker restart" not in log
    proc, log = _call(tmp_path, "reload_mounted_sidecars", **LOCAL_OK)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "docker restart yeoljeong-finance" in log
    assert "end mounted_sidecar_reload success" in log


def test_local_internal_warmup_failure_fails(tmp_path):
    proc, log = _call(tmp_path, "reload_mounted_sidecars", **{**LOCAL_OK, "healthy": []})
    assert proc.returncode == 1
    assert "end mounted_sidecar_reload error" in log
    assert "success" not in log


def test_public_auth_codes_pass_and_5xx_fails(tmp_path):
    for code in ("200", "401", "403"):
        proc, _ = _call(tmp_path, "reload_mounted_sidecars", **{**LOCAL_OK, "public": code})
        assert proc.returncode == 0, code
    for code in ("500", "502", "000", "404"):
        proc, log = _call(tmp_path, "reload_mounted_sidecars", **{**LOCAL_OK, "public": code})
        assert proc.returncode == 1, code
        assert "end mounted_sidecar_reload error" in log


# ── behavior: remote topology ───────────────────────────────────────────────
def test_remote_topology_never_touches_local_docker(tmp_path):
    for fn in ("preflight_mounted_sidecar_topology", "reload_mounted_sidecars"):
        proc, log = _call(tmp_path, fn, **REMOTE_OK)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "docker" not in log
        assert "curl http://127.0.0.1:8210/health/live" in log


def test_remote_bad_health_fails_both_gates(tmp_path):
    for fn, phase in (
        ("preflight_mounted_sidecar_topology", "sidecar_topology_preflight blocked"),
        ("reload_mounted_sidecars", "mounted_sidecar_reload error"),
    ):
        proc, log = _call(tmp_path, fn, **{**REMOTE_OK, "healthy": []})
        assert proc.returncode == 1
        assert f"end {phase}" in log
        assert "docker" not in log


def test_remote_public_route_5xx_fails(tmp_path):
    proc, log = _call(tmp_path, "reload_mounted_sidecars", **{**REMOTE_OK, "public": "502"})
    assert proc.returncode == 1
    assert "end mounted_sidecar_reload error" in log


def test_remote_route_owned_by_docker_proxy_is_rejected(tmp_path):
    proc, _ = _call(tmp_path, "preflight_mounted_sidecar_topology", **{**REMOTE_OK, "ss_out": PROXY_LISTENER})
    assert proc.returncode == 1


def test_remote_route_without_listener_is_rejected(tmp_path):
    proc, _ = _call(tmp_path, "preflight_mounted_sidecar_topology", **{**REMOTE_OK, "ss_out": ""})
    assert proc.returncode == 1


# ── behavior: unknown topology / forbidden sidecar ──────────────────────────
def test_unknown_route_blocks(tmp_path):
    for routes in (["127.0.0.1:9000"], [], [LOCAL_ROUTE, REMOTE_ROUTE]):
        for fn in ("preflight_mounted_sidecar_topology", "reload_mounted_sidecars"):
            proc, log = _call(tmp_path, fn, **{**LOCAL_OK, "routes": routes})
            assert proc.returncode == 1, (routes, fn)
            assert "docker restart" not in log


def test_api_container_is_rejected_as_sidecar(tmp_path):
    for fn in ("preflight_mounted_sidecar_topology", "reload_mounted_sidecars"):
        proc, log = _call(
            tmp_path,
            fn,
            **{**LOCAL_OK, "present": ["yeoljeong-finance", "aads-server-green"]},
            env={"DEPLOY_MOUNTED_SIDECARS": "aads-server-green"},
        )
        assert proc.returncode == 1
        assert "docker restart" not in log


def test_explicit_extra_sidecar_adds_a_required_gate_never_removes_one(tmp_path):
    proc, _ = _call(
        tmp_path,
        "preflight_mounted_sidecar_topology",
        **REMOTE_OK,
        env={"DEPLOY_MOUNTED_SIDECARS": "yeoljeong-finance"},
    )
    assert proc.returncode == 1


# ── behavior: destination verification (blocking defect 1) ──────────────────
GATES = ("preflight_mounted_sidecar_topology", "reload_mounted_sidecars")


def _assert_blocked_before_health(proc, log):
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "curl http://127.0.0.1:8210" not in log
    assert "ssh-REAL-CONNECT" not in log
    assert "docker" not in log


def test_approved_cafe24_tunnel_is_allowed_and_resolved_locally_only(tmp_path):
    for fn in GATES:
        proc, log = _call(tmp_path, fn, **REMOTE_OK)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ssh -G" in log
        assert "ssh-REAL-CONNECT" not in log


def test_old244_tunnel_is_blocked_even_when_health_is_200(tmp_path):
    for fn in GATES:
        proc, log = _call(tmp_path, fn, **REMOTE_OK, cmdline=_tunnel(f"partner@{OLD244_HOST}"))
        _assert_blocked_before_health(proc, log)
        assert OLD244_HOST in proc.stdout
        assert APPROVED_HOST in proc.stdout


def test_ssh_alias_is_resolved_through_ssh_g_not_by_name(tmp_path):
    good = dict(**REMOTE_OK, cmdline=_tunnel("acct-fin"), aliases={"acct-fin": APPROVED_HOST})
    bad = dict(**REMOTE_OK, cmdline=_tunnel("acct-fin"), aliases={"acct-fin": OLD244_HOST})
    for fn in GATES:
        proc, log = _call(tmp_path, fn, **good)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ssh -G -NT" in log and "acct-fin" in log
        proc, log = _call(tmp_path, fn, **bad)
        _assert_blocked_before_health(proc, log)


def test_unresolvable_or_unknown_forward_fails_closed(tmp_path):
    cases = {
        "ssh -G fails": dict(ssh_g_fail=True),
        "wrong remote port": dict(cmdline=_tunnel(APPROVED_HOST, "127.0.0.1:8210:127.0.0.1:9999")),
        "forward on another local port": dict(cmdline=_tunnel(APPROVED_HOST, "127.0.0.1:8211:127.0.0.1:8110")),
        "forward to a third host": dict(cmdline=_tunnel(APPROVED_HOST, "127.0.0.1:8210:10.0.0.9:8110")),
        "no forward at all": dict(cmdline=[*TUNNEL_BASE, APPROVED_HOST]),
        "jump host hides destination": dict(ssh_g_extra="proxyjump bastion.example"),
        "proxycommand hides destination": dict(ssh_g_extra="proxycommand nc 5.104.85.244 22"),
        "cmdline unreadable": dict(cmdline=[]),
        "listener is not ssh binary": dict(cmdline=["/usr/bin/socat", "TCP-LISTEN:8210", "TCP:114.207.244.86:8110"]),
    }
    for name, override in cases.items():
        for fn in GATES:
            proc, log = _call(tmp_path, fn, **{**REMOTE_OK, **override})
            assert proc.returncode == 1, (name, fn, proc.stdout)
            assert "curl http://127.0.0.1:8210" not in log, name
            assert "ssh-REAL-CONNECT" not in log, name


def test_listener_shape_must_be_single_ssh_pid_on_loopback(tmp_path):
    wildcard = 'LISTEN 0 128 0.0.0.0:8210 0.0.0.0:* users:(("ssh",pid=753,fd=4))'
    two_pids = 'LISTEN 0 128 127.0.0.1:8210 0.0.0.0:* users:(("ssh",pid=753,fd=4),("ssh",pid=900,fd=4))'
    split = SSH_LISTENER + "\n" + SSH_LISTENER.replace("pid=753", "pid=901")
    for out in (wildcard, two_pids, split, PROXY_LISTENER, ""):
        proc, log = _call(tmp_path, "preflight_mounted_sidecar_topology", **{**REMOTE_OK, "ss_out": out})
        assert proc.returncode == 1, out
        assert "curl http://127.0.0.1:8210" not in log


def test_remote_mode_refuses_obsolete_local_finance_container(tmp_path):
    for fn in GATES:
        proc, log = _call(
            tmp_path,
            fn,
            **{**REMOTE_OK, "present": ["yeoljeong-finance"]},
            env={"DEPLOY_MOUNTED_SIDECARS": "yeoljeong-finance"},
        )
        assert proc.returncode == 1
        assert "docker inspect" not in log and "docker restart" not in log


def test_local_route_owned_by_ssh_forward_is_rejected(tmp_path):
    ssh8110 = SSH_LISTENER.replace("8210", "8110")
    for fn in GATES:
        proc, log = _call(tmp_path, fn, **{**LOCAL_OK, "ss_out": ssh8110})
        assert proc.returncode == 1
        assert "docker restart" not in log


def test_local_required_sidecar_absent_blocks_before_any_restart(tmp_path):
    proc, log = _call(tmp_path, "preflight_mounted_sidecar_topology", **{**LOCAL_OK, "present": []})
    assert proc.returncode == 1
    assert "docker inspect yeoljeong-finance" in log
    assert "docker restart" not in log
    assert "required local sidecar absent: yeoljeong-finance" in proc.stdout


# ── behavior: real `ssh -G` output (no connection is ever made) ─────────────
REAL_BASE = [*TUNNEL_BASE, "-F", "/dev/null"]


def _real_tunnel(dest, fwd="127.0.0.1:8210:127.0.0.1:8110", *extra):
    return [*REAL_BASE, *extra, "-L", fwd, dest]


needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH client not installed")


def _real(tmp_path, fn="preflight_mounted_sidecar_topology", **kw):
    return _call(tmp_path, fn, **{**REMOTE_OK, "real_ssh_g": True, **kw})


@needs_ssh
def test_real_openssh_bracketed_forward_of_approved_host_is_allowed(tmp_path):
    out = subprocess.run(
        ["ssh", "-G", *_real_tunnel(f"partner@{APPROVED_HOST}")[1:]], capture_output=True, text=True, timeout=15
    ).stdout
    assert "localforward [127.0.0.1]:8210 [127.0.0.1]:8110" in out  # the format that was missed
    for fn in GATES:
        proc, log = _real(tmp_path, fn, cmdline=_real_tunnel(f"partner@{APPROVED_HOST}"))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ssh-REAL-CONNECT" not in log


@needs_ssh
def test_real_openssh_old244_and_bad_forwards_are_blocked_before_health(tmp_path):
    cases = {
        "old244": _real_tunnel(f"partner@{OLD244_HOST}"),
        "wrong remote port": _real_tunnel(f"partner@{APPROVED_HOST}", "127.0.0.1:8210:127.0.0.1:9999"),
        "other local port": _real_tunnel(f"partner@{APPROVED_HOST}", "127.0.0.1:8211:127.0.0.1:8110"),
        "third host": _real_tunnel(f"partner@{APPROVED_HOST}", "127.0.0.1:8210:10.0.0.9:8110"),
        "hostname target": _real_tunnel(f"partner@{APPROVED_HOST}", "127.0.0.1:8210:localhost:8110"),
        "wildcard bind": _real_tunnel(f"partner@{APPROVED_HOST}", "0.0.0.0:8210:127.0.0.1:8110"),
        "star bind": _real_tunnel(f"partner@{APPROVED_HOST}", "*:8210:127.0.0.1:8110"),
        "port-only bind": _real_tunnel(f"partner@{APPROVED_HOST}", "8210:127.0.0.1:8110"),
        "ipv6 bind": _real_tunnel(f"partner@{APPROVED_HOST}", "[::1]:8210:127.0.0.1:8110"),
        "jump host": _real_tunnel(f"partner@{APPROVED_HOST}", "127.0.0.1:8210:127.0.0.1:8110", "-J", "bastion"),
        "no forward": [*REAL_BASE, f"partner@{APPROVED_HOST}"],
        "second forward on same bind": [
            *REAL_BASE, "-L", "127.0.0.1:8210:127.0.0.1:8110", "-L", "127.0.0.1:8210:127.0.0.1:22",
            f"partner@{APPROVED_HOST}",
        ],
    }
    for name, cmdline in cases.items():
        for fn in GATES:
            proc, log = _real(tmp_path, fn, cmdline=cmdline)
            assert proc.returncode == 1, (name, fn, proc.stdout)
            assert "curl http://127.0.0.1:8210" not in log, name
            assert "ssh-REAL-CONNECT" not in log, name


def test_ssh_g_output_forms_both_normalize_but_only_explicit_loopback_matches(tmp_path):
    ok_forms = ({}, {"ssh_g_style": "bare"})
    for kw in ok_forms:
        proc, _ = _call(tmp_path, "preflight_mounted_sidecar_topology", **REMOTE_OK, **kw)
        assert proc.returncode == 0, (kw, proc.stdout)
    for fwd in ("8210:127.0.0.1:8110", "127.0.0.1:8210:localhost:8110"):
        proc, _ = _call(
            tmp_path, "preflight_mounted_sidecar_topology", **REMOTE_OK, cmdline=_tunnel(APPROVED_HOST, fwd)
        )
        assert proc.returncode == 1, fwd


def test_malformed_or_duplicate_localforward_lines_fail_closed(tmp_path):
    cases = {
        "duplicate bracketed": "localforward [127.0.0.1]:8210 [127.0.0.1]:8110",
        "same bind other target": "localforward [127.0.0.1]:8210 [10.0.0.9]:8110",
        "extra field": "localforward [127.0.0.1]:8211 [127.0.0.1]:1 junk",
    }
    for name, extra in cases.items():
        proc, log = _call(tmp_path, "preflight_mounted_sidecar_topology", **REMOTE_OK, ssh_g_extra=extra)
        assert proc.returncode == 1, name
        assert "curl http://127.0.0.1:8210" not in log, name


def test_ssh_g_resolution_is_time_bounded_and_never_connects():
    assert "timeout 15 ssh -G" in _function_body("finance_ssh_resolve")
    body = _function_body("verify_finance_tunnel_destination")
    assert "finance_ssh_resolve" in body
    assert "ssh -N" not in body and "ssh -f" not in body


# ── behavior: post-cutover routing rollback (blocking defect 2) ─────────────
ROLLBACK_SHELL = r"""
GREEN_PORT=8101
DOWNTIME_PROBE_HOST=aads.newtalk.kr
RUN_DIR="$(dirname "$CALLS")"
cat >> "$UPSTREAM_CONF" <<'CONF'
# Active API slot: green (8101). Blue (8100) is kept as rollback backup.
upstream aads_api {
    server 127.0.0.1:8100 max_fails=1 fail_timeout=10s backup;
    server 127.0.0.1:8101 max_fails=1 fail_timeout=10s;
}
CONF
if [[ -n "${STALE_ROUTING:-}" ]]; then
    sed -i -E -e 's/(8100 max_fails=1 fail_timeout=10s) backup;/\1;/' -e 's/(8101 max_fails=1 fail_timeout=10s);/\1 backup;/' "$UPSTREAM_CONF"
fi
cp "$UPSTREAM_CONF" "$UPSTREAM_CONF.orig"
AADS_RELEASE_SHA=abc123def456
DEPLOY_GENERATION=gen-1
DEPLOY_GENERATION_FILE="$RUN_DIR/.deploy_generation"
printf '%s\n' "${GEN_FILE_NOW:-gen-1}" > "$DEPLOY_GENERATION_FILE"
ACTIVE_PORT_FILE="$RUN_DIR/.active_port"
ACTIVE_CONTAINER_FILE="$RUN_DIR/.active_container"
printf '%s\n' "${ACTIVE_PORT_NOW:-8101}" > "$ACTIVE_PORT_FILE"
printf '%s\n' "${ACTIVE_CONTAINER_NOW:-aads-server-green}" > "$ACTIVE_CONTAINER_FILE"
MARKERS="$RUN_DIR/owner"; mkdir -p "$MARKERS"
printf true > "$MARKERS/aads-server-green"; printf false > "$MARKERS/aads-server-blue"
acquire_nginx_switch_lock() { [[ -n "${LOCK_FAIL:-}" ]] && return 1; echo "lock acquire" >> "$CALLS"; }
release_nginx_switch_lock() { echo "lock release" >> "$CALLS"; }
nginx_config_test() { echo "nginx -t" >> "$CALLS"; return "${NGINX_T_RC:-0}"; }
nginx_reload() {
    echo "nginx reload" >> "$CALLS"
    [[ "${NGINX_RELOAD_FAIL_ONCE:-}" == 1 ]] && { NGINX_RELOAD_FAIL_ONCE=0; return 1; }
    [[ "${NGINX_RELOAD_FAIL_ALWAYS:-}" == 1 ]] && return 1
    return 0
}
write_active_slot_state() {
    echo "state $1 $2" >> "$CALLS"
    [[ "${STATE_RC:-0}" != 0 ]] && return "$STATE_RC"
    printf '%s\n' "$1" > "$ACTIVE_PORT_FILE"; printf '%s\n' "$2" > "$ACTIVE_CONTAINER_FILE"
}
verify_container_slot_markers() { echo "markers $1 $2" >> "$CALLS"; return "${MARKER_RC:-0}"; }
audit_control() { echo "audit $1 $3" >> "$CALLS"; }
# Stateful docker: owner markers are real files, so write AND read-back are exercised.
docker() {
    echo "docker $*" >> "$CALLS"
    case "$1" in
    exec)
        local c="$2" cmd="$5" v
        if [[ "$cmd" == printf* ]]; then
            v="${cmd#printf }"; v="${v%% *}"
            [[ " ${EXEC_FAIL_WRITE:-} " == *" $c:$v "* ]] && return 1
            printf '%s' "$v" > "$MARKERS/$c"; return 0
        fi
        [[ " ${EXEC_FAIL_READ:-} " == *" $c "* ]] && return 1
        if [[ " ${EXEC_READ_WRONG:-} " == *" $c "* ]]; then printf 'garbage'; return 0; fi
        cat "$MARKERS/$c"; return 0 ;;
    inspect)
        case "$*" in
        *'{{.Config.Image}}'*) echo "${NEW_IMAGE:-aads-server:abc123def456}" ;;
        *'{{.Image}}'*) echo "${NEW_IMAGE_ID:-sha256:aaa}" ;;
        esac
        return 0 ;;
    image)
        echo "${TAG_IMAGE_ID:-sha256:aaa}"; return 0 ;;
    esac
    return 0
}
"""
OLD_HEALTH = "http://127.0.0.1:8100/api/v1/health"
ROUTED_HEALTH = "http://127.0.0.1/api/v1/health"
ROLLBACK_OK = {**REMOTE_OK, "healthy": [OLD_HEALTH, ROUTED_HEALTH], "extra_shell": ROLLBACK_SHELL}
ROLLBACK_ARGS = "8101 aads-server-green 8100 aads-server-blue"
FINAL_OWNER_OK = {"aads-server-green": "true", "aads-server-blue": "false"}


def _rollback(tmp_path, env=None, via_if=False, **kw):
    tmp_path = _fresh(tmp_path)
    script, calls, penv = _run(tmp_path, **{**ROLLBACK_OK, **kw}, env=env)
    if via_if:
        # Production calls the function as an `if` condition, where `set -e` is
        # suspended inside it: only explicit return-code checks can fail it.
        cmd = (f'source "{script}"; set -e; '
               f'if rollback_cutover_after_sidecar_failure {ROLLBACK_ARGS}; then echo IF_TRUE; rc=0; '
               f'else echo IF_FALSE; rc=1; fi; echo "ERR=${{SIDECAR_ROLLBACK_ERROR:-}}"; exit $rc')
    else:
        cmd = (f'source "{script}" rollback_cutover_after_sidecar_failure {ROLLBACK_ARGS}; '
               f'rc=$?; echo "ERR=${{SIDECAR_ROLLBACK_ERROR:-}}"; exit $rc')
    proc = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True, env=penv, timeout=30,
    )
    return proc, calls.read_text(encoding="utf-8"), tmp_path / "upstream.conf"


def _owner(conf):
    d = conf.parent / "owner"
    return {c: (d / c).read_text() for c in ("aads-server-green", "aads-server-blue")}


def _api_servers(conf):
    out = {}
    for line in conf.read_text(encoding="utf-8").splitlines():
        if "server 127.0.0.1:810" in line:
            out[line.split(":")[1].split()[0]] = "backup" if "backup" in line else "active"
    return out


def _lines(log):
    return log.splitlines()


def _err(proc):
    return proc.stdout.rsplit("ERR=", 1)[1].strip()


def test_rollback_restores_previous_slot_under_one_short_lock(tmp_path):
    proc, log, conf = _rollback(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _api_servers(conf) == {"8100": "active", "8101": "backup"}
    assert _owner(conf) == {"aads-server-blue": "true", "aads-server-green": "false"}
    text = conf.read_text(encoding="utf-8")
    assert "# Active API slot: blue (8100). Green (8101) is kept as rollback backup." in text
    assert "server 127.0.0.1:9999;" in text and "upstream yeoljeong_finance_api" in text
    lines = _lines(log)
    idx = lambda needle: next(i for i, l in enumerate(lines) if l.startswith(needle))  # noqa: E731
    lock, test, reload_, state, markers, release = (
        idx("lock acquire"), idx("nginx -t"), idx("nginx reload"),
        idx("state 8100 aads-server-blue"), idx("markers aads-server-blue 8100"), idx("lock release"),
    )
    assert lock < test < reload_ < state < markers < release
    own_old = idx("docker exec aads-server-blue sh -c printf true")
    own_new = idx("docker exec aads-server-green sh -c printf false")
    read_old = idx("docker exec aads-server-blue sh -c cat /tmp/aads_execution_resume_owner")
    read_new = idx("docker exec aads-server-green sh -c cat /tmp/aads_execution_resume_owner")
    routed = max(i for i, l in enumerate(lines) if l == f"curl {ROUTED_HEALTH}")
    assert state < own_old < read_old < own_new < read_new < routed < release
    assert "end sidecar_failure_rollback success" in log
    assert not any(l.startswith(("docker build", "docker stop", "docker restart")) for l in lines)
    assert sum(l == "lock acquire" for l in lines) == sum(l == "lock release" for l in lines) == 1


def test_rollback_refuses_unhealthy_previous_slot_without_touching_nginx(tmp_path):
    proc, log, conf = _rollback(tmp_path, healthy=[ROUTED_HEALTH])
    assert proc.returncode == 1
    assert "previous slot aads-server-blue:8100 is not healthy" in proc.stdout
    assert "lock acquire" not in log and "nginx reload" not in log and "state " not in log
    assert conf.read_text() == (conf.parent / "upstream.conf.orig").read_text()
    assert "end sidecar_failure_rollback failed" in log


def test_rollback_lock_unavailable_fails_without_editing_nginx(tmp_path):
    proc, log, conf = _rollback(tmp_path, env={"LOCK_FAIL": "1"})
    assert proc.returncode == 1
    assert "nginx reload" not in log
    assert conf.read_text() == (conf.parent / "upstream.conf.orig").read_text()


def test_rollback_failures_never_false_success_and_leave_routing_consistent(tmp_path):
    cases = {
        "nginx -t fails": dict(env={"NGINX_T_RC": "1"}),
        "markers disagree": dict(env={"MARKER_RC": "1"}),
        "routed health fails after reload": dict(healthy=[OLD_HEALTH]),
    }
    for name, kw in cases.items():
        proc, log, conf = _rollback(tmp_path, **kw)
        assert proc.returncode == 1, (name, proc.stdout)
        assert _err(proc), name
        assert "end sidecar_failure_rollback success" not in log, name
        assert "end sidecar_failure_rollback failed" in log, name
        assert "audit sidecar-failure-rollback failed" in log, name
        assert "audit sidecar-failure-rollback success" not in log, name
        assert log.count("lock acquire") == log.count("lock release") == 1, name


def test_rollback_failure_after_flip_restores_state_and_ownership_to_live_slot(tmp_path):
    proc, log, conf = _rollback(tmp_path, healthy=[OLD_HEALTH])
    assert proc.returncode == 1
    assert "COMPENSATION FAILED" not in proc.stdout
    assert _api_servers(conf) == {"8100": "backup", "8101": "active"}
    assert "state 8101 aads-server-green" in _lines(log)
    assert _owner(conf) == FINAL_OWNER_OK
    assert (conf.parent / ".active_port").read_text().strip() == "8101"


def test_rollback_reload_failure_restores_file_and_reports_failure(tmp_path):
    proc, log, conf = _rollback(tmp_path, env={"NGINX_RELOAD_FAIL_ONCE": "1"})
    assert proc.returncode == 1
    assert "nginx reload failed while restoring" in proc.stdout
    assert _api_servers(conf) == {"8100": "backup", "8101": "active"}
    assert "state " not in log
    assert _owner(conf) == FINAL_OWNER_OK


# ── rollback ownership-marker writes are checked, read back, fail closed ────
def test_review_repro_all_docker_exec_failing_is_no_longer_success(tmp_path):
    shell = ROLLBACK_SHELL + '\ndocker() { echo "docker $*" >> "$CALLS"; [[ "$1" == exec ]] && return 1; return 0; }\n'
    proc, log, conf = _rollback(tmp_path, extra_shell=shell)
    assert proc.returncode == 1, proc.stdout
    assert _err(proc)
    assert "audit sidecar-failure-rollback success" not in log
    assert "end sidecar_failure_rollback success" not in log


def test_owner_marker_write_or_readback_failure_fails_closed_and_compensates(tmp_path):
    cases = {
        "old slot write fails": (
            {"EXEC_FAIL_WRITE": "aads-server-blue:true"},
            "resume-owner marker write/read-back failed on aads-server-blue",
        ),
        "new slot write fails": (
            {"EXEC_FAIL_WRITE": "aads-server-green:false"},
            "resume-owner marker write/read-back failed on aads-server-green",
        ),
        "old slot read fails": (
            {"EXEC_FAIL_READ": "aads-server-blue"},
            "resume-owner marker write/read-back failed on aads-server-blue",
        ),
        "old slot read-back mismatch": (
            {"EXEC_READ_WRONG": "aads-server-blue"},
            "resume-owner marker write/read-back failed on aads-server-blue",
        ),
        "new slot read-back mismatch": (
            {"EXEC_READ_WRONG": "aads-server-green"},
            "resume-owner marker write/read-back failed on aads-server-green",
        ),
    }
    for name, (env, reason) in cases.items():
        proc, log, conf = _rollback(tmp_path, env=env)
        assert proc.returncode == 1, (name, proc.stdout)
        assert reason in _err(proc), name
        assert "audit sidecar-failure-rollback failed" in log, name
        assert "audit sidecar-failure-rollback success" not in log, name
        assert "end sidecar_failure_rollback success" not in log, name
        assert "routed health" not in _err(proc), name
        if "READ" not in "".join(env) and "WRONG" not in "".join(env):
            assert _owner(conf) == FINAL_OWNER_OK, name
        assert _api_servers(conf) == {"8100": "backup", "8101": "active"}, name
        assert "state 8101 aads-server-green" in _lines(log), name
        if "COMPENSATION" not in _err(proc):
            assert (conf.parent / ".active_port").read_text().strip() == "8101", name


def test_compensation_failures_are_reported_never_called_consistent(tmp_path):
    cases = {
        "owner marker restore fails": (
            {"EXEC_FAIL_WRITE": "aads-server-blue:true aads-server-green:true"},
            "resume-owner marker restore on aads-server-green",
        ),
        "state restore fails": ({"STATE_RC": "1"}, "active-slot state restore"),
        "marker re-verification fails": ({"MARKER_RC": "1"}, "slot marker re-verification"),
        "nginx reload of restored file fails": (
            {"EXEC_FAIL_WRITE": "aads-server-green:false", "NGINX_RELOAD_FAIL_AFTER_FLIP": "1"},
            "nginx reload of restored upstream",
        ),
    }
    for name, (env, reason) in cases.items():
        kw = {}
        if "NGINX_RELOAD_FAIL_AFTER_FLIP" in env:
            env = {k: v for k, v in env.items() if k != "NGINX_RELOAD_FAIL_AFTER_FLIP"}
            kw["extra_shell"] = ROLLBACK_SHELL + (
                '\n_n=0; nginx_reload() { echo "nginx reload" >> "$CALLS"; _n=$((_n+1)); (( _n == 1 )); }\n'
            )
        proc, log, conf = _rollback(tmp_path, env=env, **kw)
        err = _err(proc)
        assert proc.returncode == 1, (name, proc.stdout)
        assert "COMPENSATION FAILED" in err and reason in err, (name, err)
        assert "manual verification required" in err, name
        assert "audit sidecar-failure-rollback failed" in log, name
        assert "audit sidecar-failure-rollback success" not in log, name
        assert "end sidecar_failure_rollback success" not in log, name
        assert log.count("lock acquire") == log.count("lock release") == 1, name


# ── a superseded rollback must not undo a newer release ─────────────────────
def test_stale_rollback_is_aborted_inside_the_lock_without_touching_anything(tmp_path):
    cases = {
        "newer deploy generation": ({"GEN_FILE_NOW": "gen-2"}, "deploy generation"),
        "slot became standby again (routing flipped)": ({"STALE_ROUTING": "1"}, "nginx aads_api routing"),
        "slot reused by another release sha": ({"NEW_IMAGE": "aads-server:feedfacecafe"}, "not this release"),
        "image generation changed under same tag": (
            {"NEW_IMAGE_ID": "sha256:bbb", "TAG_IMAGE_ID": "sha256:ccc"},
            "image generation",
        ),
        "active-slot state moved": ({"ACTIVE_PORT_NOW": "8100", "ACTIVE_CONTAINER_NOW": "aads-server-blue"}, "active-slot state"),
    }
    for name, (env, reason) in cases.items():
        proc, log, conf = _rollback(tmp_path, env=env)
        err = _err(proc)
        assert proc.returncode == 1, (name, proc.stdout)
        assert "stale rollback refused" in err and reason in err, (name, err)
        lines = _lines(log)
        assert "lock acquire" in lines and "lock release" in lines, name
        assert not any(l.startswith(("nginx -t", "nginx reload", "state ", "markers ")) for l in lines), name
        assert not any("printf" in l for l in lines), name
        assert conf.read_text() == (conf.parent / "upstream.conf.orig").read_text(), name
        assert _owner(conf) == FINAL_OWNER_OK, name
        assert "audit sidecar-failure-rollback failed" in log, name
        assert "audit sidecar-failure-rollback success" not in log, name


def test_rollback_function_never_builds_or_restarts_or_uses_stale_snapshot():
    body = _function_body("rollback_cutover_after_sidecar_failure")
    for forbidden in ("docker build", "docker compose", "docker restart", "docker stop", "--build", ".pre_deploy"):
        assert forbidden not in body
    assert body.index("acquire_nginx_switch_lock") < body.index("nginx_reload") < body.index("release_nginx_switch_lock")
    assert body.index("rollback_expected_state_ok") < body.index("sed -i") < body.index("release_nginx_switch_lock")


def test_rollback_never_ignores_owner_marker_results_nor_touches_db_fence():
    body = _function_body("rollback_cutover_after_sidecar_failure")
    assert "|| true" not in body
    assert "aads_execution_resume_owner" not in body  # only via the checked helper
    assert body.count("set_resume_owner_marker") >= 4
    marker = _function_body("set_resume_owner_marker")
    assert "cat /tmp/aads_execution_resume_owner" in marker and '[[ "$observed" == "$value" ]]' in marker
    for text in (body, marker, _function_body("rollback_expected_state_ok")):
        assert "owner_epoch" not in text.replace("DB lease fencing", "")
        assert "psql" not in text and "UPDATE " not in text
    assert "DB lease fencing" in body


def test_stale_guard_checks_generation_routing_sha_and_image_identity():
    guard = _function_body("rollback_expected_state_ok")
    for needle in ("DEPLOY_GENERATION_FILE", "aads_api_routing_snapshot", "AADS_RELEASE_SHA",
                   "{{.Image}}", "image inspect", "ACTIVE_PORT_FILE", "ACTIVE_CONTAINER_FILE"):
        assert needle in guard, needle
    assert "upstream[[:space:]]+aads_api" in _function_body("aads_api_routing_snapshot")


# ── upstream edit result is verified, never assumed (sed ignored => false success) ──
SED_NOOP = '\nsed() { echo "sed noop" >> "$CALLS"; return 0; }\n'
SED_FAIL_NO_WRITE = '\nsed() { echo "sed failure injected" >> "$CALLS"; return 1; }\n'
SED_FAIL_PARTIAL = (
    '\nsed() { echo "sed partial" >> "$CALLS"; '
    'command sed -i -E -e "s/(8101 max_fails=1 fail_timeout=10s);/\\1 backup;/" "$UPSTREAM_CONF"; return 1; }\n'
)
SED_BOTH_ACTIVE = '\nsed() { command sed -i -E -e "s/ backup;/;/" "$UPSTREAM_CONF"; return 0; }\n'
SED_BOTH_BACKUP = (
    '\nsed() { command sed -i -E -e "s/(8101 max_fails=1 fail_timeout=10s);/\\1 backup;/" "$UPSTREAM_CONF"; return 0; }\n'
)
ORIG_ROUTING = {"8100": "backup", "8101": "active"}


def test_review_repro_sed_failure_is_no_longer_false_success_in_production_if_context(tmp_path):
    cases = {
        "sed rc1, nothing written": (SED_FAIL_NO_WRITE, "sed rc=1"),
        "sed rc1 after partial write": (SED_FAIL_PARTIAL, "sed rc=1"),
        "sed rc0 but nothing changed": (SED_NOOP, "did not produce active=8100 backup=8101"),
        "sed rc0 but both servers active": (SED_BOTH_ACTIVE, "did not produce active=8100 backup=8101"),
        "sed rc0 but both servers backup": (SED_BOTH_BACKUP, "did not produce active=8100 backup=8101"),
    }
    for via_if in (False, True):
        for name, (override, reason) in cases.items():
            label = (name, via_if)
            proc, log, conf = _rollback(tmp_path, via_if=via_if, extra_shell=ROLLBACK_SHELL + override)
            err = _err(proc)
            lines = _lines(log)
            assert proc.returncode == 1, (label, proc.stdout)
            assert ("IF_FALSE" in proc.stdout) if via_if else True, label
            assert "IF_TRUE" not in proc.stdout, label
            assert reason in err, (label, err)
            assert "COMPENSATION FAILED" not in err, (label, err)
            # nothing past the edit may run: no state flip, no marker or owner writes
            assert not any(l.startswith(("state ", "markers ")) for l in lines), label
            assert not any("printf" in l for l in lines), label
            assert f"curl {ROUTED_HEALTH}" not in lines, label
            assert "audit sidecar-failure-rollback failed" in log, label
            assert "audit sidecar-failure-rollback success" not in log, label
            assert "end sidecar_failure_rollback failed" in log, label
            assert "end sidecar_failure_rollback success" not in log, label
            # the file is back on the live cutover routing, state and ownership untouched
            assert _api_servers(conf) == ORIG_ROUTING, label
            assert conf.read_text() == (conf.parent / "upstream.conf.orig").read_text(), label
            assert _owner(conf) == FINAL_OWNER_OK, label
            assert (conf.parent / ".active_port").read_text().strip() == "8101", label
            assert lines.count("lock acquire") == lines.count("lock release") == 1, label


def test_edit_failure_never_reloads_the_edited_file(tmp_path):
    for override in (SED_FAIL_PARTIAL, SED_NOOP, SED_BOTH_ACTIVE):
        proc, log, conf = _rollback(tmp_path, extra_shell=ROLLBACK_SHELL + override)
        lines = _lines(log)
        # only the compensation may test/reload, and only after the file is restored
        assert lines.count("nginx reload") <= 1
        assert _api_servers(conf) == ORIG_ROUTING


def test_edit_failure_with_failed_restore_is_reported_as_unverified(tmp_path):
    shell = ROLLBACK_SHELL + SED_FAIL_PARTIAL + (
        '\ncp() { if [[ "$2" == "$UPSTREAM_CONF" ]]; then echo "cp restore failed" >> "$CALLS"; return 1; fi; command cp "$@"; }\n'
    )
    for via_if in (False, True):
        proc, log, conf = _rollback(tmp_path, via_if=via_if, extra_shell=shell)
        err = _err(proc)
        assert proc.returncode == 1, proc.stdout
        assert "sed rc=1" in err
        assert "COMPENSATION FAILED" in err and "upstream file restore" in err, err
        assert "upstream routing re-verification" in err, err
        assert "manual verification required" in err, err
        assert "audit sidecar-failure-rollback success" not in log
        assert "end sidecar_failure_rollback success" not in log
        assert not any(l.startswith("state ") for l in _lines(log))


def test_rollback_edit_is_scoped_to_aads_api_and_leaves_unrelated_upstreams_alone(tmp_path):
    decoy = (
        "\nprintf 'upstream decoy_api {\\n    server 127.0.0.1:8100 weight=2;\\n"
        "    server 127.0.0.1:8101 weight=2 backup;\\n}\\n' >> \"$UPSTREAM_CONF\"\n"
        'cp "$UPSTREAM_CONF" "$UPSTREAM_CONF.orig"\n'
    )
    proc, log, conf = _rollback(tmp_path, via_if=True, extra_shell=ROLLBACK_SHELL + decoy)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "IF_TRUE" in proc.stdout
    text = conf.read_text(encoding="utf-8")
    assert "    server 127.0.0.1:8100 weight=2;" in text
    assert "    server 127.0.0.1:8101 weight=2 backup;" in text
    block = text.split("upstream aads_api {", 1)[1].split("}", 1)[0]
    assert "server 127.0.0.1:8100 max_fails=1 fail_timeout=10s;" in block
    assert "server 127.0.0.1:8101 max_fails=1 fail_timeout=10s backup;" in block
    assert "end sidecar_failure_rollback success" in log


def test_normal_rollback_still_succeeds_when_called_as_an_if_condition(tmp_path):
    proc, log, conf = _rollback(tmp_path, via_if=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "IF_TRUE" in proc.stdout and _err(proc) == ""
    assert _api_servers(conf) == {"8100": "active", "8101": "backup"}
    assert _owner(conf) == {"aads-server-blue": "true", "aads-server-green": "false"}
    assert "audit sidecar-failure-rollback success" in log


def test_rollback_edit_verification_is_wired_before_reload_in_source():
    body = _function_body("rollback_cutover_after_sidecar_failure")
    assert "|| sed_rc=$?" in body and "(( sed_rc != 0 ))" in body
    assert body.count("aads_api_routing_snapshot") >= 2  # post-edit check and compensation re-check
    assert body.index("sed -i") < body.index("aads_api_routing_snapshot") < body.index("nginx_config_test") < body.index("nginx_reload")
    assert "upstream[[:space:]]+aads_api" in body  # sed range is scoped to the aads_api block


# ── compensation is fail-closed: nothing is reloaded/written until the restore is proven ──
CP_RESTORE_RC1 = (
    '\ncp() { if [[ "$2" == "$UPSTREAM_CONF" ]]; then echo "cp restore failed" >> "$CALLS"; return 1; fi; command cp "$@"; }\n'
)
CP_RESTORE_NOOP = (
    '\ncp() { if [[ "$2" == "$UPSTREAM_CONF" ]]; then echo "cp restore noop" >> "$CALLS"; return 0; fi; command cp "$@"; }\n'
)
CP_RESTORE_WRONG = (
    '\ncp() { command cp "$@"; if [[ "$2" == "$UPSTREAM_CONF" ]]; then '
    'command sed -i -E -e "s/ backup;/;/" "$UPSTREAM_CONF"; fi; return 0; }\n'
)
CP_RESTORE_TAMPER = (
    '\ncp() { command cp "$@"; if [[ "$2" == "$UPSTREAM_CONF" ]]; then echo "# tampered" >> "$UPSTREAM_CONF"; fi; return 0; }\n'
)
# Mock nginx runtime: every successful reload copies the conf nginx would have
# read into runtime_N.conf, so tests judge what was loaded, not what deploy.sh claims.
RUNTIME_OBSERVER = r"""
_bump() { local n; n=$(( $(cat "$RUN_DIR/$1" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$RUN_DIR/$1"; echo "$n"; }
nginx_config_test() {
    local n; n="$(_bump t_n)"; echo "nginx -t" >> "$CALLS"
    [[ -n "${NGINX_T_FAIL_FROM:-}" ]] && (( n >= NGINX_T_FAIL_FROM )) && return 1
    return 0
}
nginx_reload() {
    local n; n="$(_bump reload_n)"; echo "nginx reload" >> "$CALLS"
    [[ -n "${RELOAD_FAIL_FROM:-}" ]] && (( n >= RELOAD_FAIL_FROM )) && { echo "reload failed" >> "$CALLS"; return 1; }
    command cp "$UPSTREAM_CONF" "$RUN_DIR/runtime_${n}.conf"
}
"""
SNAPSHOT_UNPARSEABLE_AFTER_EDIT = r"""
eval "orig_$(declare -f aads_api_routing_snapshot)"
aads_api_routing_snapshot() {
    local n; n=$(( $(cat "$RUN_DIR/snap_n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$RUN_DIR/snap_n"
    (( n >= 3 )) && return 0
    orig_aads_api_routing_snapshot
}
"""
GOOD_ROUTINGS = ({"8100": "active", "8101": "backup"}, {"8100": "backup", "8101": "active"})


def _runtime_loads(conf):
    return [_api_servers(p) for p in sorted(conf.parent.glob("runtime_*.conf"))]


def _comp_run(tmp_path, via_if, overrides, env=None, routed=False):
    extra = ROLLBACK_SHELL + overrides + RUNTIME_OBSERVER
    kw = {"extra_shell": extra}
    if routed:
        kw["healthy"] = [OLD_HEALTH]  # edit+reload succeed, routed health fails afterwards
    return _rollback(tmp_path, env=env, via_if=via_if, **kw)


def _assert_failed_closed(label, proc, log, conf, via_if):
    lines = _lines(log)
    err = _err(proc)
    assert proc.returncode == 1, (label, proc.stdout)
    assert ("IF_FALSE" in proc.stdout) if via_if else True, label
    assert "IF_TRUE" not in proc.stdout, label
    assert "COMPENSATION FAILED" in err and "manual verification required" in err, (label, err)
    assert "audit sidecar-failure-rollback failed" in log, label
    assert "audit sidecar-failure-rollback success" not in log, label
    assert "end sidecar_failure_rollback failed" in log, label
    assert "end sidecar_failure_rollback success" not in log, label
    assert lines.count("lock acquire") == lines.count("lock release") == 1, label
    for loaded in _runtime_loads(conf):  # nginx never loaded an ambiguous/all-active upstream
        assert loaded in GOOD_ROUTINGS, (label, loaded)


def test_review_repro_failed_restore_never_reloads_both_active_upstream(tmp_path):
    for via_if in (False, True):
        proc, log, conf = _comp_run(tmp_path, via_if, SED_BOTH_ACTIVE + CP_RESTORE_RC1)
        label = ("repro", via_if)
        _assert_failed_closed(label, proc, log, conf, via_if)
        lines = _lines(log)
        assert "upstream file restore" in _err(proc), label
        assert "nginx not reloaded" in _err(proc), label
        assert "cp restore failed" in lines, label
        assert "nginx reload" not in lines and "nginx -t" not in lines, label
        assert _runtime_loads(conf) == [], label
        assert not any(l.startswith(("state ", "markers ")) or "printf" in l for l in lines), label
        assert (conf.parent / ".active_port").read_text().strip() == "8101", label
        assert _owner(conf) == FINAL_OWNER_OK, label


def test_unproven_restore_blocks_test_reload_and_every_dependent_write(tmp_path):
    cases = {
        "partial sed write + restore rc1": (SED_FAIL_PARTIAL + CP_RESTORE_RC1, "upstream file restore (cp failed)"),
        "both-active + restore rc0 no-op": (SED_BOTH_ACTIVE + CP_RESTORE_NOOP, "content differs from snapshot"),
        "both-active + restore rc0 wrong content": (SED_BOTH_ACTIVE + CP_RESTORE_WRONG, "content differs from snapshot"),
        "restore rc0 tampered but still cutover-routed": (
            SED_BOTH_ACTIVE + CP_RESTORE_TAMPER, "content differs from snapshot"),
        "restored file routing cannot be re-parsed": (
            SED_BOTH_ACTIVE + SNAPSHOT_UNPARSEABLE_AFTER_EDIT, "upstream routing re-verification"),
    }
    for via_if in (False, True):
        for name, (overrides, reason) in cases.items():
            label = (name, via_if)
            proc, log, conf = _comp_run(tmp_path, via_if, overrides)
            _assert_failed_closed(label, proc, log, conf, via_if)
            lines = _lines(log)
            assert reason in _err(proc), (label, _err(proc))
            assert "nginx not reloaded" in _err(proc), label
            assert "nginx reload" not in lines and "nginx -t" not in lines, label
            assert _runtime_loads(conf) == [], label
            assert not any(l.startswith(("state ", "markers ")) or "printf" in l for l in lines), label
            assert f"curl {ROUTED_HEALTH}" not in lines, label
            assert (conf.parent / ".active_port").read_text().strip() == "8101", label
            assert _owner(conf) == FINAL_OWNER_OK, label


def test_compensation_config_test_failure_blocks_reload(tmp_path):
    for via_if in (False, True):
        proc, log, conf = _comp_run(tmp_path, via_if, SED_BOTH_ACTIVE, env={"NGINX_T_FAIL_FROM": "1"})
        label = ("nginx -t fails", via_if)
        _assert_failed_closed(label, proc, log, conf, via_if)
        lines = _lines(log)
        assert "nginx config test of restored upstream" in _err(proc), label
        assert "nginx reload" not in lines and _runtime_loads(conf) == [], label
        assert _api_servers(conf) == ORIG_ROUTING, label  # file itself was restored
        assert not any(l.startswith(("state ", "markers ")) or "printf" in l for l in lines), label


def test_routed_rollback_writes_nothing_more_when_compensation_is_unproven(tmp_path):
    cases = {
        "restore rc1": (CP_RESTORE_RC1, {}, "upstream file restore (cp failed)"),
        "restore rc0 no-op": (CP_RESTORE_NOOP, {}, "content differs from snapshot"),
        "restore rc0 wrong content (both active)": (CP_RESTORE_WRONG, {}, "content differs from snapshot"),
        "compensation nginx -t fails": ("", {"NGINX_T_FAIL_FROM": "2"}, "nginx config test of restored upstream"),
        "compensation reload fails": ("", {"RELOAD_FAIL_FROM": "2"}, "nginx reload of restored upstream"),
    }
    for via_if in (False, True):
        for name, (overrides, env, reason) in cases.items():
            label = (name, via_if)
            proc, log, conf = _comp_run(tmp_path, via_if, overrides, env=env, routed=True)
            _assert_failed_closed(label, proc, log, conf, via_if)
            lines = _lines(log)
            err = _err(proc)
            assert reason in err, (label, err)
            assert "active-slot state/marker restore skipped" in err, (label, err)
            assert "resume-owner marker restore skipped" in err, (label, err)
            # only the rollback's own forward writes happened; the compensation added none
            assert [l for l in lines if l.startswith("state ")] == ["state 8100 aads-server-blue"], label
            assert [l for l in lines if l.startswith("markers ")] == ["markers aads-server-blue 8100"], label
            assert sum("printf" in l for l in lines) == 2, label
            assert lines.count("nginx reload") <= 2, label
            if name in ("compensation nginx -t fails", "compensation reload fails", "restore rc1", "restore rc0 no-op",
                        "restore rc0 wrong content (both active)"):
                # the single successful reload is the rollback edit; nothing else was loaded
                assert _runtime_loads(conf) == [{"8100": "active", "8101": "backup"}], label
            assert (conf.parent / ".active_port").read_text().strip() == "8100", label
            assert _owner(conf) == {"aads-server-blue": "true", "aads-server-green": "false"}, label


def test_proven_compensation_reloads_then_restores_state_and_owner_in_order(tmp_path):
    for via_if in (False, True):
        proc, log, conf = _comp_run(tmp_path, via_if, "", routed=True)
        label = ("proven compensation", via_if)
        lines = _lines(log)
        err = _err(proc)
        assert proc.returncode == 1 and "COMPENSATION FAILED" not in err, (label, err)
        assert "routed health failed" in err, label
        assert _runtime_loads(conf) == [{"8100": "active", "8101": "backup"}, ORIG_ROUTING], label
        second_reload = [i for i, l in enumerate(lines) if l == "nginx reload"][1]
        assert lines.index("state 8101 aads-server-green") > second_reload, label
        later_printf = [i for i, l in enumerate(lines) if "printf" in l and i > second_reload]
        assert len(later_printf) == 2, label
        assert _api_servers(conf) == ORIG_ROUTING and _owner(conf) == FINAL_OWNER_OK, label
        assert (conf.parent / ".active_port").read_text().strip() == "8101", label


def test_compensation_gates_are_explicit_conditions_in_source():
    body = _function_body("rollback_cutover_after_sidecar_failure")
    comp = body[body.index("comp_restored=true"):]
    assert 'cp "$backup" "$UPSTREAM_CONF" 2>/dev/null ||' not in body  # no `||` chaining that a caller's `if` can bypass
    assert comp.index("cmp -s") < comp.index("aads_api_routing_snapshot") < comp.index("nginx_config_test") < comp.index("nginx_reload")
    assert comp.index('"$comp_restored" == "true"') < comp.index("nginx_config_test")
    assert comp.index("nginx_reload") < comp.index("comp_reloaded=true") < comp.index("write_active_slot_state")
    assert comp.index("comp_reloaded") < comp.index("set_resume_owner_marker")
    assert 'comp_reloaded" != "true"' in comp
