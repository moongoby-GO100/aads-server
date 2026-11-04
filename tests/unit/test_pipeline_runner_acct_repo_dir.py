"""ACCT 저장소 경로는 호스트 설정값(AADS_ACCT_REPO_DIR)으로 받는다 — 구 서버 고정경로 제거."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts/pipeline-runner.sh"
LOCAL = ROOT / "scripts/pipeline-runner.sh.local"


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i) + len(end)]


def _map_block(text: str) -> str:
    return _block(text, "declare -A PROJECT_WORKDIR=(", "\n)\n")


def _func(text: str) -> str:
    return _block(text, "acct_repo_ready() {", "\n}\n")


def _run(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )


def _acct_dir(env: dict) -> str:
    text = SH.read_text(encoding="utf-8")
    out = _run(f'{_map_block(text)}\nprintf %s "${{PROJECT_WORKDIR[ACCT]}}"', env)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_acct_workdir_empty_when_unset():
    assert _acct_dir({}) == ""


def test_acct_workdir_uses_configured_path(tmp_path):
    assert _acct_dir({"AADS_ACCT_REPO_DIR": str(tmp_path)}) == str(tmp_path)


def test_acct_repo_ready_rejects_empty_and_non_git(tmp_path):
    func = _func(SH.read_text(encoding="utf-8"))
    assert _run(f'{func}\nacct_repo_ready ACCT ""').returncode == 1
    assert _run(f'{func}\nacct_repo_ready ACCT "{tmp_path}"').returncode == 1
    assert _run(f'{func}\nacct_repo_ready ACCT "{tmp_path}/missing"').returncode == 1


def test_acct_repo_ready_accepts_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    func = _func(SH.read_text(encoding="utf-8"))
    assert _run(f'{func}\nacct_repo_ready ACCT "{tmp_path}"').returncode == 0


def test_acct_repo_ready_ignores_other_projects():
    func = _func(SH.read_text(encoding="utf-8"))
    assert _run(f'{func}\nacct_repo_ready AADS ""').returncode == 0


def test_run_job_fails_acct_before_lock_with_expected_error():
    text = SH.read_text(encoding="utf-8")
    body = text[text.index("\nrun_job() {") :]
    guard = body.index("acct_repo_ready")
    assert guard < body.index("/ops/locks/work/acquire")
    snippet = body[guard : guard + 400]
    assert "acct_repo_not_configured" in snippet
    assert "AADS_ACCT_REPO_DIR" in snippet


def test_sh_and_local_are_identical_for_acct_blocks():
    a, b = SH.read_text(encoding="utf-8"), LOCAL.read_text(encoding="utf-8")
    assert _map_block(a) == _map_block(b)
    assert _func(a) == _func(b)


def test_no_legacy_acct_server_strings():
    for path in (SH, LOCAL, ROOT / "app/api/pipeline_runner.py", ROOT / "app/services/tool_registry.py"):
        text = path.read_text(encoding="utf-8")
        assert "jinah244" not in text, path
        assert "회계비서" not in text, path


def test_acct_host_fallback_and_tool_description():
    api = (ROOT / "app/api/pipeline_runner.py").read_text(encoding="utf-8")
    assert re.search(r'_PROJECT_HOST_FALLBACK = \{[^}]*"ACCT": "cafe24_114"', api, re.S)
    reg = (ROOT / "app/services/tool_registry.py").read_text(encoding="utf-8")
    assert "ACCT→cafe24_114 (aads-server 저장소, AADS_ACCT_REPO_DIR)" in reg
