"""aads_git_ssh_retry.sh: 인증 단계 거부만 다시 접속하고 나머지는 그대로 돌려준다."""
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aads_git_ssh_retry.sh"


def _fake_ssh(tmp_path: Path, outcomes: list[tuple[int, str, str]]) -> Path:
    """호출마다 outcomes 를 차례로 재현하는 가짜 ssh."""
    state = tmp_path / "n"
    state.write_text("0")
    lines = ["#!/usr/bin/env bash", f'n=$(cat "{state}"); echo $((n+1)) > "{state}"', 'stdin=$(cat)']
    for i, (rc, out, err) in enumerate(outcomes):
        lines.append(f'if [ "$n" = "{i}" ]; then printf %s "{out}"; printf %s "{err}" >&2; exit {rc}; fi')
    lines.append("exit 99")
    p = tmp_path / "ssh"
    p.write_text("\n".join(lines) + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _run(tmp_path, outcomes, max_retries="5"):
    fake = _fake_ssh(tmp_path, outcomes)
    env = dict(os.environ, AADS_GIT_SSH_BIN=str(fake), AADS_GIT_SSH_RETRY_SLEEP="0",
               AADS_GIT_SSH_RETRIES=max_retries, AADS_GIT_SSH_RETRY_LOG=str(tmp_path / "log"))
    r = subprocess.run(["bash", str(SCRIPT), "git@github.com", "git-upload-pack 'x.git'"],
                       input="", capture_output=True, text=True, env=env, timeout=30)
    return r, int((tmp_path / "n").read_text())


DENIED = "git@github.com: Permission denied (publickey)."


def test_retries_publickey_denial_then_succeeds(tmp_path):
    r, calls = _run(tmp_path, [(255, "", DENIED), (255, "", DENIED), (0, "PACK", "")])
    assert r.returncode == 0 and r.stdout == "PACK" and calls == 3
    assert "retry 1/5" in (tmp_path / "log").read_text()


def test_gives_up_after_max_and_returns_last_error(tmp_path):
    r, calls = _run(tmp_path, [(255, "", DENIED)] * 3, max_retries="3")
    assert r.returncode == 255 and calls == 3 and "publickey" in r.stderr


def test_host_key_failure_not_retried(tmp_path):
    r, calls = _run(tmp_path, [(255, "", "Host key verification failed."), (0, "", "")])
    assert r.returncode == 255 and calls == 1


def test_non_auth_error_not_retried(tmp_path):
    r, calls = _run(tmp_path, [(128, "", "ERROR: Repository not found."), (0, "", "")])
    assert r.returncode == 128 and calls == 1 and "not found" in r.stderr
