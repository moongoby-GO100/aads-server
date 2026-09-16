"""Pipeline Runner stale_base 자동 rebase(attempt_stale_base_rebase) 테스트.

배경: 2026-09-16 하루에 같은 원인으로 세 건이 push 에서 멈췄다
(runner-cd394808 외). 승인과 push 사이에 origin/main 이 전진하면 승인 SHA 는
non-fast-forward 가 되고, force push 는 금지라 러너가 그대로 죽었다.
"그때그때 사람이 rebase 한다" 는 규칙으로만 남아 세 번 다 어겨졌다(R-ERRBOOK).

여기서 고정하는 계약은 하나다. **겹친 파일이 0 일 때만 자동으로 옮겨 붙인다.**
한 파일이라도 겹치면 하지 않는다 — 텍스트로 안 겹쳐도 같은 파일이면 의미가
충돌할 수 있고 그 판단은 사람 몫이다.
"""

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


# ── 1) 스크립트 계약 ────────────────────────────────────────────────────


def test_auto_rebase_present_in_both_runner_scripts():
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        assert "attempt_stale_base_rebase() {" in script, f"{script_name}: 함수 없음"
        assert 'rebased_sha=$(attempt_stale_base_rebase "$worktree_dir" "$current_sha" "$job_id")' in script


def test_auto_rebase_runs_before_the_terminal_stale_base_branch():
    """자동 rebase 가 실패했을 때만 기존 종료 경로로 가야 한다."""
    script = _read_script("pipeline-runner.sh")
    call = script.index('rebased_sha=$(attempt_stale_base_rebase')
    terminal = script.index("push_stale_base: 승인 SHA")
    assert call < terminal, "자동 rebase 시도가 종료 분기보다 뒤에 있다"
    assert 'push_state="fast_forward"' in script
    assert "push_stale_base_rebased" in script


def test_auto_rebase_never_force_pushes():
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        assert "push --force" not in script
        assert "push -f " not in script
        assert "--force-with-lease" not in script


def test_auto_rebase_guards_are_present():
    """격리 워크트리 제한·커밋수 상한·킬스위치가 사라지면 안 된다."""
    script = _read_script("pipeline-runner.sh")
    fn = _extract_function(script, "attempt_stale_base_rebase")
    assert '[[ "$repo" == "/tmp/aads-wt-${job_id}" ]] || return 1' in fn, "라이브 저장소 보호 가드 소실"
    assert "AUTO_REBASE_STALE_BASE" in fn, "킬스위치 소실"
    assert "rebase --abort" in fn, "실패 시 원상복귀 경로 소실"
    assert "comm -12" in fn, "파일 겹침 검사 소실"


def test_runner_scripts_stay_byte_identical():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")


# ── 2) 실제 동작 ────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    if shutil.which("git") is None or shutil.which("comm") is None:
        pytest.skip("git/comm 미설치 환경 — 동작 테스트 생략")
    script = _read_script("pipeline-runner.sh")
    path = tmp_path_factory.mktemp("auto_rebase_fn") / "fn.sh"
    path.write_text(
        "set -eo pipefail\n"
        'log() { echo "$*" >&2; }\n'
        + _extract_function(script, "classify_push_state")
        + _extract_function(script, "attempt_stale_base_rebase"),
        encoding="utf-8",
    )
    return path


def _call(fn_file: Path, repo: Path, sha: str, job_id: str, env_kill: bool = False):
    prefix = "AUTO_REBASE_STALE_BASE=0 " if env_kill else ""
    return subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; {prefix}attempt_stale_base_rebase "{repo}" "{sha}" "{job_id}"'],
        capture_output=True,
        text=True,
    )


def _build_case(tmp_path: Path, job_id: str, job_file: str, incoming_file: str):
    """워크트리 커밋과 origin/main 전진을 만들어 stale_base 상황을 재현한다."""
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "runner@aads.local")
    _git(clone, "config", "user.name", "AADS Runner Test")

    (clone / "base.txt").write_text("base\n", encoding="utf-8")
    (clone / job_file).write_text("v0\n", encoding="utf-8")
    (clone / incoming_file).write_text("v0\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "main")

    worktree = Path(f"/tmp/aads-wt-{job_id}")
    _git(clone, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / job_file).write_text("job change\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "job commit")
    job_sha = _git(worktree, "rev-parse", "HEAD")

    # 승인 이후 origin/main 이 전진한다 — 여기서 stale_base 가 된다
    (clone / incoming_file).write_text("someone else\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "incoming")
    _git(clone, "push", "origin", "main")

    return clone, worktree, job_sha


@pytest.fixture
def case_disjoint(tmp_path):
    job_id = f"test{uuid.uuid4().hex[:10]}"
    clone, worktree, job_sha = _build_case(tmp_path, job_id, "job_only.txt", "other_only.txt")
    yield job_id, clone, worktree, job_sha
    shutil.rmtree(worktree, ignore_errors=True)
    _git(clone, "worktree", "prune")


@pytest.fixture
def case_overlap(tmp_path):
    job_id = f"test{uuid.uuid4().hex[:10]}"
    clone, worktree, job_sha = _build_case(tmp_path, job_id, "shared.txt", "shared.txt")
    yield job_id, clone, worktree, job_sha
    shutil.rmtree(worktree, ignore_errors=True)
    _git(clone, "worktree", "prune")


def test_disjoint_files_are_rebased_onto_latest_origin(fn_file, case_disjoint):
    job_id, _clone, worktree, job_sha = case_disjoint

    proc = _call(fn_file, worktree, job_sha, job_id)

    assert proc.returncode == 0, f"rebase 했어야 한다: {proc.stderr}"
    new_sha = proc.stdout.strip()
    assert SHA_RE.match(new_sha), f"stdout 이 SHA 가 아니다: {new_sha!r}"
    assert new_sha != job_sha, "SHA 가 그대로면 옮겨 붙이지 않은 것이다"

    # 옮겨 붙인 뒤에는 fast-forward 여야 push 가 가능하다
    state = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; classify_push_state "{worktree}" "{new_sha}"'],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert state == "fast_forward"

    # 작업 내용이 보존돼야 한다
    assert (worktree / "job_only.txt").read_text(encoding="utf-8") == "job change\n"
    # 다른 사람 변경도 함께 올라와 있어야 한다
    assert (worktree / "other_only.txt").read_text(encoding="utf-8") == "someone else\n"


def test_overlapping_file_is_left_to_a_human(fn_file, case_overlap):
    job_id, _clone, worktree, job_sha = case_overlap

    proc = _call(fn_file, worktree, job_sha, job_id)

    assert proc.returncode == 1, "같은 파일을 양쪽이 건드렸으면 자동으로 하면 안 된다"
    assert "AUTO_REBASE_SKIP" in proc.stderr
    assert _git(worktree, "rev-parse", "HEAD") == job_sha, "실패했는데 워크트리가 움직였다"


def test_refuses_outside_isolated_job_worktree(fn_file, case_disjoint):
    """라이브 저장소를 rebase 하면 다른 세션의 작업이 날아간다."""
    job_id, clone, _worktree, job_sha = case_disjoint

    proc = _call(fn_file, clone, job_sha, job_id)

    assert proc.returncode == 1


def test_kill_switch_disables_auto_rebase(fn_file, case_disjoint):
    job_id, _clone, worktree, job_sha = case_disjoint

    proc = _call(fn_file, worktree, job_sha, job_id, env_kill=True)

    assert proc.returncode == 1
    assert _git(worktree, "rev-parse", "HEAD") == job_sha
