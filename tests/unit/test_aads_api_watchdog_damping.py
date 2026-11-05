"""호스트 감시 스크립트의 hard/soft 실패 판정, 카운터 2개, 전환 쿨다운 행동 검증.

docker/curl/nginx/systemctl/flock/logger 스텁을 PATH 앞에 두고 실제 스크립트를 실행한다.
모든 경로는 AADS_* 환경변수로 tmp 에 돌린다. 쿨다운은 dry-run 이 아닌 실제 switch_to_peer 로도 확인한다.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WATCHDOG = ROOT / "scripts/aads_api_watchdog.sh"

ACTIVE = "aads-server"
PEER = "aads-server-green"

# 컨테이너별 상태는 STUB_RUNNING_<name>, STUB_SUP_<name> (하이픈은 _) 로 바꾼다.
# curl 은 STUB_LIVE_FAIL_PORTS 에 든 포트의 /health/live 만 시간 초과(28)로 실패시킨다.
DOCKER_STUB = r"""#!/usr/bin/env bash
echo "docker $*" >> "${STUB_CALL_LOG:-/dev/null}"
name() { echo "${1//-/_}"; }
case "$1" in
  inspect)
    c="${@: -1}"
    case "$*" in
      *State.Running*) v="STUB_RUNNING_$(name "$c")"; echo "${!v:-true}" ;;
      *Health*) v="STUB_HEALTH_$(name "$c")"; echo "${!v:-healthy}" ;;
      *OOMKilled*) echo false ;;
    esac ;;
  exec)
    if [[ "$*" == *"supervisorctl status"* ]]; then
      v="STUB_SUP_$(name "$2")"
      echo "aads-api ${!v:-RUNNING} pid 1, uptime 0:10:00"
    fi ;;
  stats) echo "10%" ;;
esac
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
for port in ${STUB_LIVE_FAIL_PORTS:-}; do
  if [[ "$*" == *":${port}/health/live"* ]]; then
    printf '000'
    exit 28
  fi
done
printf '200'
exit 0
"""

NGINX_STUB = r"""#!/usr/bin/env bash
echo "nginx $*" >> "${STUB_CALL_LOG:-/dev/null}"
exit 0
"""

SYSTEMCTL_STUB = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "${STUB_CALL_LOG:-/dev/null}"
exit 0
"""

FLOCK_STUB = "#!/usr/bin/env bash\nexit 0\n"
LOGGER_STUB = "#!/usr/bin/env bash\nexit 0\n"

WRITER_STUB = r"""#!/usr/bin/env bash
echo "writer $*" >> "${STUB_CALL_LOG:-/dev/null}"
exit 0
"""

UPSTREAM_CONF = (
    "upstream aads_api {\n"
    "    server 127.0.0.1:8100 max_fails=1 fail_timeout=10s;\n"
    "    server 127.0.0.1:8102 max_fails=1 fail_timeout=10s backup;\n"
    "}\n"
)

REAL_PATHS = [
    Path("/etc/nginx/conf.d/aads-upstream.conf"),
    Path("/etc/nginx/conf.d/aads-upstream.conf.watchdog.previous"),
    Path("/run/aads-api-watchdog"),
    Path("/var/log/aads-control-audit.jsonl"),
    ROOT / ".active_port",
    ROOT / ".active_container",
    ROOT / ".active_slot_authorization",
]


def _snapshot(path: Path):
    if not path.exists():
        return None
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


@pytest.fixture
def env(tmp_path: Path):
    before = {p: _snapshot(p) for p in REAL_PATHS}

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (
        ("docker", DOCKER_STUB),
        ("curl", CURL_STUB),
        ("nginx", NGINX_STUB),
        ("systemctl", SYSTEMCTL_STUB),
        ("flock", FLOCK_STUB),
        ("logger", LOGGER_STUB),
    ):
        stub = fake_bin / name
        stub.write_text(body)
        stub.chmod(0o755)
    writer = tmp_path / "scripts" / "aads_active_slot_state.sh"
    writer.parent.mkdir()
    writer.write_text(WRITER_STUB)
    writer.chmod(0o755)
    upstream = tmp_path / "aads-upstream.conf"
    upstream.write_text(UPSTREAM_CONF)

    base = {k: v for k, v in os.environ.items() if not k.startswith(("AADS_", "STUB_"))}
    paths = {
        "AADS_COMPOSE_DIR": tmp_path / "compose",
        "AADS_UPSTREAM_CONF": upstream,
        "AADS_ACTIVE_SLOT_STATE_WRITER": writer,
        "AADS_WATCHDOG_STATE_DIR": tmp_path / "state",
        "AADS_DEPLOY_LOCK": tmp_path / "no-deploy.lock",
        "AADS_NGINX_LOCK": tmp_path / "nginx.lock",
        "AADS_CONTROL_AUDIT_LOG": tmp_path / "audit.jsonl",
        "STUB_CALL_LOG": tmp_path / "calls.log",
    }
    env = {
        **base,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        **{k: str(v) for k, v in paths.items()},
    }
    yield env

    for key, value in env.items():
        if key.startswith(("AADS_", "STUB_")) and key.endswith(("DIR", "CONF", "LOCK", "LOG", "WRITER")):
            assert Path(value).is_relative_to(tmp_path), f"{key} 가 tmp 밖: {value}"
    assert {p: _snapshot(p) for p in REAL_PATHS} == before, "실제 시스템 경로가 변경됨"


def _run(env: dict[str, str], *, dry: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(WATCHDOG)] + (["--dry-run"] if dry else [])
    return subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=60)


def _audit(env: dict[str, str]) -> list[dict]:
    path = Path(env["AADS_CONTROL_AUDIT_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _state(env: dict[str, str]) -> Path:
    return Path(env["AADS_WATCHDOG_STATE_DIR"])


def _counter(env: dict[str, str], name: str) -> str:
    return (_state(env) / name).read_text().strip()


def _calls(env: dict[str, str]) -> list[str]:
    path = Path(env["STUB_CALL_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def _set_last_failover(env: dict[str, str], seconds_ago: int) -> None:
    _state(env).mkdir(parents=True, exist_ok=True)
    (_state(env) / "last_failover_epoch").write_text(f"{int(time.time()) - seconds_ago}\n")


def _soft(env: dict[str, str], **extra: str) -> dict[str, str]:
    return {**env, "STUB_LIVE_FAIL_PORTS": "8100", **extra}


def _hard(env: dict[str, str], **extra: str) -> dict[str, str]:
    return {**env, "STUB_LIVE_FAIL_PORTS": "8100", "STUB_SUP_aads_server": "STOPPED", **extra}


FAILOVER_DRY = "DRY-RUN failover aads-server:8100 -> aads-server-green:8102"


def test_a_docker_unhealthy_with_slow_live_is_soft_until_fifth(env):
    senv = _soft(env, STUB_HEALTH_aads_server="unhealthy")
    for attempt in range(1, 5):
        result = _run(senv)
        assert result.returncode == 0, result.stderr
        assert FAILOVER_DRY not in result.stdout
        assert f"kind=soft fail={attempt}/5 hard=0/2 rule=none" in result.stdout
    result = _run(senv)
    assert result.returncode == 0, result.stderr
    assert "kind=soft fail=5/5 hard=0/2 rule=soft_streak" in result.stdout
    assert FAILOVER_DRY in result.stdout
    checks = [a for a in _audit(env) if a["action"] == "check"]
    assert len(checks) == 5
    assert all("kind=soft" in c["detail"] for c in checks)
    assert "health=unhealthy" in checks[-1]["detail"]
    assert _counter(env, "consecutive_failures") == "0"
    assert _counter(env, "consecutive_hard_failures") == "0"


def test_b_supervisor_stopped_twice_fails_over_even_in_cooldown(env):
    _set_last_failover(env, 60)
    henv = _hard(env)
    first = _run(henv)
    assert first.returncode == 0, first.stderr
    assert "kind=hard fail=1/5 hard=1/2 rule=none" in first.stdout
    assert FAILOVER_DRY not in first.stdout
    second = _run(henv)
    assert second.returncode == 0, second.stderr
    assert "kind=hard fail=2/5 hard=2/2 rule=hard_streak" in second.stdout
    assert FAILOVER_DRY in second.stdout
    assert not [a for a in _audit(env) if a["action"] == "failover"]


@pytest.mark.parametrize("state", ["FATAL", "EXITED", "BACKOFF", "unknown"])
def test_b2_other_non_running_supervisor_states_are_hard(env, state):
    henv = _soft(env, STUB_SUP_aads_server=state)
    _run(henv)
    second = _run(henv)
    assert "kind=hard fail=2/5 hard=2/2 rule=hard_streak" in second.stdout
    assert FAILOVER_DRY in second.stdout


def test_b3_container_not_running_is_hard(env):
    henv = _soft(env, STUB_RUNNING_aads_server="false")
    _run(henv)
    second = _run(henv)
    assert "kind=hard" in second.stdout
    assert FAILOVER_DRY in second.stdout


def _soft4_then_hard(env):
    senv = _soft(env)
    for _ in range(4):
        result = _run(senv)
        assert "kind=soft" in result.stdout
        assert FAILOVER_DRY not in result.stdout
    return _run(_hard(env))


def test_c_soft4_then_hard1_outside_cooldown_is_soft_streak(env):
    _set_last_failover(env, 700)
    fifth = _soft4_then_hard(env)
    assert fifth.returncode == 0, fifth.stderr
    assert "kind=hard fail=5/5 hard=1/2 rule=soft_streak" in fifth.stdout
    assert FAILOVER_DRY in fifth.stdout


def test_c_soft4_then_hard1_inside_cooldown_is_blocked(env):
    _set_last_failover(env, 60)
    fifth = _soft4_then_hard(env)
    assert fifth.returncode == 0, fifth.stderr
    assert "kind=hard fail=5/5 hard=1/2 rule=soft_streak" in fifth.stdout
    assert FAILOVER_DRY not in fifth.stdout
    blocked = [a for a in _audit(env) if a["action"] == "failover"]
    assert len(blocked) == 1
    assert blocked[0]["result"] == "blocked"
    assert blocked[0]["detail"].startswith("cooldown remaining=")
    remaining = int(blocked[0]["detail"].split("remaining=")[1].split("s")[0])
    assert 0 < remaining <= 540
    assert "rule=soft_streak" in blocked[0]["detail"]


def test_d_hard_soft_hard_does_not_fail_over(env):
    assert "hard=1/2" in _run(_hard(env)).stdout
    assert _counter(env, "consecutive_hard_failures") == "1"
    assert "hard=0/2" in _run(_soft(env)).stdout
    assert _counter(env, "consecutive_hard_failures") == "0"
    third = _run(_hard(env))
    assert third.returncode == 0, third.stderr
    assert "kind=hard fail=3/5 hard=1/2 rule=none" in third.stdout
    assert FAILOVER_DRY not in third.stdout
    assert _counter(env, "consecutive_failures") == "3"
    assert _counter(env, "consecutive_hard_failures") == "1"
    assert not [a for a in _audit(env) if a["action"] == "failover"]


def test_e_real_switch_starts_cooldown_and_blocks_following_soft_failures(env, tmp_path):
    upstream = Path(env["AADS_UPSTREAM_CONF"])
    calls_before = len(_calls(env))
    senv = _soft(env)
    for _ in range(4):
        assert _run(senv, dry=False).returncode == 0
    assert not (_state(env) / "last_failover_epoch").exists()
    assert upstream.read_text() == UPSTREAM_CONF

    fifth = _run(senv, dry=False)
    assert fifth.returncode == 0, fifth.stderr
    assert "FAILOVER complete aads-server:8100 -> aads-server-green:8102" in fifth.stdout
    switched = upstream.read_text()
    assert "server 127.0.0.1:8102 max_fails=1 fail_timeout=10s;" in switched
    assert "server 127.0.0.1:8100 max_fails=1 fail_timeout=10s backup;" in switched
    epoch = int((_state(env) / "last_failover_epoch").read_text())
    assert abs(epoch - time.time()) < 30
    assert _counter(env, "consecutive_failures") == "0"
    assert _counter(env, "consecutive_hard_failures") == "0"
    calls = _calls(env)[calls_before:]
    assert "nginx -t" in calls
    assert "systemctl reload nginx" in calls
    assert any(c.startswith("writer write 8102 aads-server-green host-watchdog") for c in calls)
    success = [a for a in _audit(env) if a["action"] == "failover" and a["result"] == "success"]
    assert len(success) == 1
    assert "kind=soft fail=5/5 hard=0/2 rule=soft_streak" in success[0]["detail"]

    reloads_after_switch = _calls(env).count("systemctl reload nginx")
    new_active_fails = _soft(env, STUB_LIVE_FAIL_PORTS="8102")
    for _ in range(4):
        assert _run(new_active_fails, dry=False).returncode == 0
    blocked_run = _run(new_active_fails, dry=False)
    assert blocked_run.returncode == 0, blocked_run.stderr
    assert "FAILOVER complete" not in blocked_run.stdout
    assert "failover blocked by cooldown remaining=" in blocked_run.stdout
    assert upstream.read_text() == switched
    assert _calls(env).count("systemctl reload nginx") == reloads_after_switch
    blocked = [a for a in _audit(env) if a["action"] == "failover" and a["result"] == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["detail"].startswith("cooldown remaining=")

    _set_last_failover(env, 700)
    after = _run(new_active_fails, dry=False)
    assert after.returncode == 0, after.stderr
    assert "FAILOVER complete aads-server-green:8102 -> aads-server:8100" in after.stdout
    assert upstream.read_text() == UPSTREAM_CONF
    assert _calls(env).count("systemctl reload nginx") == reloads_after_switch + 1


def test_hard_streak_real_switch_ignores_cooldown(env):
    _set_last_failover(env, 30)
    henv = _hard(env)
    _run(henv, dry=False)
    second = _run(henv, dry=False)
    assert second.returncode == 0, second.stderr
    assert "FAILOVER complete aads-server:8100 -> aads-server-green:8102" in second.stdout
    success = [a for a in _audit(env) if a["action"] == "failover" and a["result"] == "success"]
    assert "rule=hard_streak" in success[0]["detail"]


def test_both_unhealthy_only_stopped_child_is_started_in_place(env):
    both = _hard(env, STUB_LIVE_FAIL_PORTS="8100 8102", STUB_SUP_aads_server_green="STOPPED")
    _run(both)
    second = _run(both)
    assert second.returncode == 0, second.stderr
    assert "DRY-RUN start stopped child aads-server:aads-api" in second.stdout
    assert "DRY-RUN failover" not in second.stdout

    running_slow = _soft(env, STUB_LIVE_FAIL_PORTS="8100 8102")
    state = _state(env)
    (state / "consecutive_failures").write_text("4\n")
    result = _run(running_slow)
    assert result.returncode == 1
    assert "both API slots unhealthy" in result.stdout
    assert "start stopped child" not in result.stdout


def test_peer_unhealthy_blocks_switch_on_hard_streak(env):
    henv = _hard(env, STUB_SUP_aads_server="FATAL", STUB_LIVE_FAIL_PORTS="8100 8102")
    _run(henv)
    second = _run(henv)
    assert second.returncode == 1
    assert "DRY-RUN failover" not in second.stdout


def test_healthy_resets_both_counters(env):
    _run(_hard(env))
    _run(_soft(env))
    assert _counter(env, "consecutive_failures") == "2"
    healthy = _run(env)
    assert "OK active=aads-server:8100" in healthy.stdout
    assert _counter(env, "consecutive_failures") == "0"
    assert _counter(env, "consecutive_hard_failures") == "0"


def test_deployment_lock_defers_without_touching_counters(env):
    lock = Path(env["AADS_DEPLOY_LOCK"])
    lock.write_text(f"{os.getpid()}\n")
    result = _run(_hard(env))
    assert result.returncode == 0
    assert "recovery deferred" in result.stdout
    assert not (_state(env) / "consecutive_hard_failures").exists()


def test_custom_thresholds_via_env(env):
    senv = _soft(env, AADS_WATCHDOG_SOFT_FAIL_THRESHOLD="3")
    _run(senv)
    _run(senv)
    third = _run(senv)
    assert "kind=soft fail=3/3 hard=0/2 rule=soft_streak" in third.stdout
    assert FAILOVER_DRY in third.stdout
