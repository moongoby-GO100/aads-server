"""계보 스위퍼는 호스트 checkout 이 아니라 origin/main 의 record-release-provenance.sh 를 실행한다.

가짜 git/docker 를 PATH 앞에 세워 (a) origin/main 사본 실행 (b) show 실패 시 로컬 폴백+WARN
(c) AADS_PROVENANCE_SCRIPT 최우선 을 검증한다. 실제 DB·네트워크는 쓰지 않는다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTOHEAL_LIB = REPO_ROOT / "scripts" / "deploy_autoheal.sh"


def _script(tag: str, calls: Path) -> str:
    return f'#!/bin/bash\necho "{tag} $*" >> "{calls}"\nexit 0\n'


def _setup(tmp_path, *, show_ok=True, fetch_ok=True, show_body=None):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".git").mkdir()
    calls = tmp_path / "calls.log"
    local = repo / "scripts" / "record-release-provenance.sh"
    local.write_text(_script("LOCAL", calls), encoding="utf-8")
    local.chmod(0o755)

    origin_script = show_body if show_body is not None else _script("ORIGIN", calls)
    origin_file = tmp_path / "origin_script.sh"
    origin_file.write_text(origin_script, encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    git_log = tmp_path / "git.log"
    (bindir / "git").write_text(
        '#!/bin/bash\n'
        f'echo "$*" >> "{git_log}"\n'
        'if [[ "$*" == *" fetch "* ]]; then exit ' + ("0" if fetch_ok else "1") + '; fi\n'
        'if [[ "$*" == *" show origin/main:scripts/record-release-provenance.sh"* ]]; then\n'
        + (f'  cat "{origin_file}"; exit 0\n' if show_ok else '  exit 128\n')
        + 'fi\n'
        'printf "%040d\\n" 1\n',
        encoding="utf-8",
    )
    (bindir / "docker").write_text(
        '#!/bin/bash\n'
        'if [[ "$*" == *"FROM deploy_runs d"* ]]; then printf "7001|aaaaaaa1\\n"\n'
        'elif [[ "$*" == *"count(*)"* ]]; then echo 1\nfi\n',
        encoding="utf-8",
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = f'export PATH="{bindir}:$PATH"\nexport STATE_DIR="{repo}"\nexport TMPDIR="{tmpdir}"\n'
    return env, calls, git_log, tmpdir


def _sweep(env: str):
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{env}source "{AUTOHEAL_LIB}"\nautoheal_provenance_sweep --since-run-id 7000\n'],
        capture_output=True, text=True, timeout=30,
    )


def test_sweep_runs_origin_main_copy_and_removes_temp_file(tmp_path):
    env, calls, git_log, tmpdir = _setup(tmp_path)
    proc = _sweep(env)
    assert proc.returncode == 0, proc.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("ORIGIN ")
    assert "--repo " in lines[0] and "--deploy-run-id 7001" in lines[0]
    assert "WARN" not in proc.stderr
    git_calls = git_log.read_text(encoding="utf-8")
    assert "fetch -q origin" in git_calls
    assert "show origin/main:scripts/record-release-provenance.sh" in git_calls
    assert list(tmpdir.iterdir()) == []


def test_sweep_falls_back_to_local_script_with_warn_when_show_fails(tmp_path):
    env, calls, _, tmpdir = _setup(tmp_path, show_ok=False)
    proc = _sweep(env)
    assert proc.returncode == 0, proc.stderr
    assert "[provenance-sweep] WARN: origin/main 스크립트 사용 불가 — 로컬 사본 사용" in proc.stderr
    assert calls.read_text(encoding="utf-8").startswith("LOCAL ")
    assert list(tmpdir.iterdir()) == []


def test_sweep_falls_back_when_fetch_fails_or_content_is_not_a_script(tmp_path):
    env, calls, _, _ = _setup(tmp_path, fetch_ok=False)
    proc = _sweep(env)
    assert proc.returncode == 0 and "로컬 사본 사용" in proc.stderr
    assert calls.read_text(encoding="utf-8").startswith("LOCAL ")

    sub = tmp_path / "second"
    sub.mkdir()
    env, calls, _, _ = _setup(sub, show_body="not a script\n")
    proc = _sweep(env)
    assert proc.returncode == 0 and "로컬 사본 사용" in proc.stderr
    assert calls.read_text(encoding="utf-8").startswith("LOCAL ")


def test_env_override_wins_without_touching_git(tmp_path):
    env, calls, git_log, _ = _setup(tmp_path)
    override = tmp_path / "override.sh"
    override.write_text(_script("OVERRIDE", calls), encoding="utf-8")
    override.chmod(0o755)
    proc = _sweep(env + f'export AADS_PROVENANCE_SCRIPT="{override}"\n')
    assert proc.returncode == 0, proc.stderr
    assert calls.read_text(encoding="utf-8").startswith("OVERRIDE ")
    assert "WARN" not in proc.stderr
    git_calls = git_log.read_text(encoding="utf-8") if git_log.exists() else ""
    assert " fetch " not in git_calls and "show origin/main" not in git_calls


def test_dry_run_does_not_fetch(tmp_path):
    env, _, git_log, _ = _setup(tmp_path)
    proc = subprocess.run(
        ["bash", "-c", f'set -uo pipefail\n{env}source "{AUTOHEAL_LIB}"\nautoheal_provenance_sweep --since-run-id 7000 --dry-run\n'],
        capture_output=True, text=True, timeout=30,
    )
    assert "DRY-RUN deploy_run#7001" in proc.stderr
    assert " fetch " not in (git_log.read_text(encoding="utf-8") if git_log.exists() else "")
