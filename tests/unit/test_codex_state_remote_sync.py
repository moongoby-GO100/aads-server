"""codex state.json 원격 배포 + 낡은 state.json 무시 회귀 테스트 (2026-10-20, codex.remote_state_json_stale).

ssh/scp 는 PATH 의 가짜 실행 파일로 대신한다 — 실제 서버·토큰·DB 를 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
RUNNER = (SCRIPTS / "pipeline-runner.sh").read_text(encoding="utf-8")
SYNC_SH = SCRIPTS / "codex_auth_sync.sh"
KST = timezone(timedelta(hours=9))


def _function(name: str) -> str:
    start = RUNNER.index(f"{name}() {{")
    return RUNNER[start: RUNNER.index("\n}\n", start) + 3]


@pytest.fixture(scope="module")
def picker(tmp_path_factory):
    if shutil.which("bash") is None or shutil.which("python3") is None:
        pytest.skip("bash/python3 미설치 환경")
    path = tmp_path_factory.mktemp("fn") / "fn.sh"
    path.write_text('log() { echo "LOG $*" >&2; }\n' + _function("codex_pick_account_home"),
                    encoding="utf-8")
    return path


def _write_state(tmp_path: Path, accounts: list[dict], updated_at="__now__") -> Path:
    for a in accounts:
        (tmp_path / a["key_name"]).mkdir(exist_ok=True)
    payload = {"accounts": accounts}
    if updated_at == "__now__":
        updated_at = datetime.now(KST).isoformat()
    if updated_at is not None:
        payload["updated_at"] = updated_at
    state = tmp_path / "state.json"
    state.write_text(json.dumps(payload))
    return state


def _acct(name, priority, **kw):
    row = {"key_name": name, "priority": priority, "is_active": True, "has_auth": True,
           "rate_limited_until_epoch": None}
    row.update(kw)
    return row


def _pick(picker, tmp_path, state, **env):
    e = {"PATH": "/usr/bin:/bin:/usr/local/bin",
         "AADS_CODEX_REVOKED_MARKER_PREFIX": str(tmp_path / "revoked-"),
         "AADS_CODEX_ACCOUNTS_STATE": str(state)}
    e.update(env)
    return subprocess.run(["bash", "-c", f"source {picker}; codex_pick_account_home"],
                          capture_output=True, text=True, env=e, timeout=30)


# (a) 낡은 state.json → rc=1 + CODEX_STATE_STALE 로그
def test_stale_state_is_not_used(picker, tmp_path):
    old = (datetime.now(KST) - timedelta(days=30)).isoformat()
    state = _write_state(tmp_path, [_acct("CODEX_OAUTH_JINAH", 1)], updated_at=old)
    out = _pick(picker, tmp_path, state)
    assert out.returncode == 1 and out.stdout == ""
    assert "CODEX_STATE_STALE age=" in out.stderr


@pytest.mark.parametrize("stamp", [None, "not-a-date", "", 12345, "2026-10-20T13:54:00"])
def test_missing_or_unparseable_updated_at_counts_as_stale(picker, tmp_path, stamp):
    state = _write_state(tmp_path, [_acct("CODEX_OAUTH_MAIN", 1)], updated_at=stamp)
    out = _pick(picker, tmp_path, state)
    assert out.returncode == 1 and "CODEX_STATE_STALE age=unknown" in out.stderr


def test_max_age_env_overrides_default(picker, tmp_path):
    ten_min_ago = (datetime.now(KST) - timedelta(minutes=10)).isoformat()
    state = _write_state(tmp_path, [_acct("CODEX_OAUTH_MAIN", 1)], updated_at=ten_min_ago)
    assert _pick(picker, tmp_path, state).returncode == 0
    assert _pick(picker, tmp_path, state, AADS_CODEX_STATE_MAX_AGE_SEC="60").returncode == 1
    assert _pick(picker, tmp_path, state, AADS_CODEX_STATE_MAX_AGE_SEC="junk").returncode == 0


# (b) 최신 state.json + 꺼 둔 계정은 고르지 않는다
def test_fresh_state_skips_inactive_account(picker, tmp_path):
    state = _write_state(tmp_path, [
        _acct("CODEX_OAUTH_JINAH", 1, is_active=False),
        _acct("CODEX_OAUTH_MAIN", 2),
    ])
    out = _pick(picker, tmp_path, state)
    assert out.returncode == 0
    assert out.stdout.strip() == str(tmp_path / "CODEX_OAUTH_MAIN")


def test_fresh_state_with_only_inactive_account_returns_1(picker, tmp_path):
    state = _write_state(tmp_path, [_acct("CODEX_OAUTH_JINAH", 1, is_active=False)])
    assert _pick(picker, tmp_path, state).returncode == 1


# (c) 원격 배포 함수: 한 서버가 실패해도 0 으로 끝나고 다른 서버는 배포된다
@pytest.fixture()
def fake_ssh(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("ssh", "scp"):
        exe = bindir / name
        exe.write_text(
            "#!/bin/bash\n"
            f'echo "{name} $*" >> "{calls}"\n'
            'if [[ -n "${FAIL_HOST:-}" && "$*" == *"$FAIL_HOST"* ]]; then exit 255; fi\n'
            "exit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return bindir, calls


def _run_push(tmp_path, bindir, fail_host="", sync_enabled="1", state_exists=True):
    state = tmp_path / "accounts" / "state.json"
    state.parent.mkdir(exist_ok=True)
    state.unlink(missing_ok=True)
    if state_exists:
        state.write_text('{"updated_at": "x", "accounts": []}')
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "CODEX_SYNC_LOG": str(tmp_path / "sync.log"),
           "CODEX_STATE_FILE": str(state), "FAIL_HOST": fail_host,
           "SYNC_ENABLED": sync_enabled}
    r = subprocess.run(["bash", str(SYNC_SH), "--push-state"], capture_output=True, text=True,
                       env=env, timeout=60)
    log = (tmp_path / "sync.log").read_text() if (tmp_path / "sync.log").exists() else ""
    return r, log


def test_push_state_survives_one_server_failure(tmp_path, fake_ssh):
    bindir, calls = fake_ssh
    r, log = _run_push(tmp_path, bindir, fail_host="5.104.86.14")
    assert r.returncode == 0
    assert "contabo14 (5.104.86.14:22) ← state.json 배포 실패" in log
    assert "cafe24_114 (114.207.244.86:7916) ← state.json 배포 성공" in log
    text = calls.read_text()
    assert "scp" in text and "-P 7916" in text and "114.207.244.86:" in text
    assert "mv -f" in text and "chmod 600" in text


def test_push_state_all_servers_failing_still_exits_0(tmp_path, fake_ssh):
    bindir, _ = fake_ssh
    for host in ("5.104.86.14", "114.207.244.86"):
        r, _log = _run_push(tmp_path, bindir, fail_host=host)
        assert r.returncode == 0


def test_push_state_never_touches_auth_json(tmp_path, fake_ssh):
    bindir, calls = fake_ssh
    _run_push(tmp_path, bindir)
    assert "auth.json" not in calls.read_text()


def test_push_state_skipped_on_independent_node_and_missing_file(tmp_path, fake_ssh):
    bindir, calls = fake_ssh
    r, log = _run_push(tmp_path, bindir, sync_enabled="0")
    assert r.returncode == 0 and "비활성 노드" in log and not calls.exists()
    r, log = _run_push(tmp_path, bindir, state_exists=False)
    assert r.returncode == 0 and not calls.exists()


def test_codex_usage_sync_calls_push_after_write_state():
    src = (SCRIPTS / "codex_usage.py").read_text(encoding="utf-8")
    assert src.index("write_state(accounts)\n        push_state_remote()") > 0
    assert "--push-state" in src and "start_new_session=True" in src and "os.killpg" in src


# 리뷰 지적 2: 연결 뒤 멈춘 서버가 다음 서버 배포를 막지 않는다
@pytest.fixture()
def hanging_ssh(tmp_path):
    bindir = tmp_path / "hbin"
    bindir.mkdir()
    calls = tmp_path / "hcalls.log"
    for name in ("ssh", "scp"):
        exe = bindir / name
        exe.write_text(
            "#!/bin/bash\n"
            f'echo "{name} $*" >> "{calls}"\n'
            'if [[ -n "${HANG_HOST:-}" && "$*" == *"$HANG_HOST"* ]]; then echo $$ >> "${PIDS_FILE}"; exec sleep 300; fi\n'
            "exit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return bindir, calls


def test_push_state_hanging_first_server_does_not_block_second(tmp_path, hanging_ssh):
    bindir, calls = hanging_ssh
    state = tmp_path / "state.json"
    state.write_text('{"updated_at": "x", "accounts": []}')
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "CODEX_SYNC_LOG": str(tmp_path / "sync.log"), "CODEX_STATE_FILE": str(state),
           "HANG_HOST": "5.104.86.14", "PIDS_FILE": f"{calls}.pids", "SYNC_ENABLED": "1",
           "STATE_SYNC_CMD_TIMEOUT": "1", "STATE_SYNC_KILL_AFTER": "1"}
    started = time.monotonic()
    r = subprocess.run(["bash", str(SYNC_SH), "--push-state"], capture_output=True, text=True,
                       env=env, timeout=60)
    assert time.monotonic() - started < 30
    log = (tmp_path / "sync.log").read_text()
    assert r.returncode == 0
    assert "contabo14 (5.104.86.14:22) ← state.json 배포 실패 또는 시간 초과" in log
    assert "cafe24_114 (114.207.244.86:7916) ← state.json 배포 성공" in log
    assert "114.207.244.86:" in calls.read_text()
    pids = [int(x) for x in Path(f"{calls}.pids").read_text().split()]
    assert pids
    time.sleep(0.5)
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_every_state_ssh_call_is_wrapped_in_timeout():
    body = SYNC_SH.read_text(encoding="utf-8")
    fn = body[body.index("sync_state_to_remote() {"): body.index("push_state_all() {")]
    for line in fn.splitlines():
        if line.strip().startswith(("#",)):
            continue
        if " ssh $" in line or " scp $" in line or line.strip().startswith(("ssh ", "scp ")):
            assert "timeout -k" in line, line


# 리뷰 지적 1: 피커가 실패하면 이전 시도에서 export 한 CODEX_HOME 을 되돌린다
@pytest.fixture(scope="module")
def selector(tmp_path_factory):
    path = tmp_path_factory.mktemp("sel") / "sel.sh"
    path.write_text('log() { echo "LOG $*" >&2; }\n' + _function("codex_pick_account_home")
                    + _function("codex_restore_home") + _function("codex_select_account_home"),
                    encoding="utf-8")
    return path


def _select_twice(selector, tmp_path, fresh_state, stale_state, pre_home=None):
    e = {"PATH": "/usr/bin:/bin:/usr/local/bin",
         "AADS_CODEX_REVOKED_MARKER_PREFIX": str(tmp_path / "revoked-")}
    if pre_home is not None:
        e["CODEX_HOME"] = pre_home
    script = (f"source {selector}\n"
              f"AADS_CODEX_ACCOUNTS_STATE={fresh_state} codex_select_account_home\n"
              'echo "1:${CODEX_PICKED_HOME}|${CODEX_HOME-UNSET}"\n'
              f"AADS_CODEX_ACCOUNTS_STATE={stale_state} codex_select_account_home\n"
              'echo "2:${CODEX_PICKED_HOME}|${CODEX_HOME-UNSET}"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=30)
    lines = dict(line.split(":", 1) for line in r.stdout.splitlines())
    return r, lines


def test_stale_pick_after_successful_pick_unsets_exported_home(selector, tmp_path):
    fresh = _write_state(tmp_path, [_acct("CODEX_OAUTH_JINAH", 1)])
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    old = (datetime.now(KST) - timedelta(days=30)).isoformat()
    stale = _write_state(stale_dir, [_acct("CODEX_OAUTH_JINAH", 1)], updated_at=old)
    r, lines = _select_twice(selector, tmp_path, fresh, stale)
    assert lines["1"] == f"{tmp_path}/CODEX_OAUTH_JINAH|{tmp_path}/CODEX_OAUTH_JINAH"
    assert lines["2"] == "|UNSET", r.stderr
    assert "CODEX_STATE_STALE" in r.stderr


def test_stale_pick_restores_preexisting_codex_home(selector, tmp_path):
    fresh = _write_state(tmp_path, [_acct("CODEX_OAUTH_MAIN", 1)])
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    stale = _write_state(stale_dir, [_acct("CODEX_OAUTH_MAIN", 1)], updated_at=None)
    _r, lines = _select_twice(selector, tmp_path, fresh, stale, pre_home="/custom/home")
    assert lines["2"] == "|/custom/home"


def test_call_site_uses_selector_not_bare_export():
    start = RUNNER.index("codex_select_account_home\n            _codex_home=")
    assert 'export CODEX_HOME="$_codex_home"' not in RUNNER[start: start + 400]


def test_runner_local_copy_is_in_sync():
    assert (SCRIPTS / "pipeline-runner.sh").read_bytes() == (SCRIPTS / "pipeline-runner.sh.local").read_bytes()
    assert os.access(SYNC_SH, os.R_OK)
