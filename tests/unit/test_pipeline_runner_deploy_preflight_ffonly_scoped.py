"""deploy_git_preflight 자동 fast-forward — dirty 를 배포 대상 파일 기준으로 완화한다.

배경: runner-62d088ac(R2)가 AI 리뷰(score=0.88) 통과 후 CEO 승인까지 받았으나,
approval commit 단계에서 `deploy_preflight_git_state: behind=1` 로 차단됐다.
당시 자동 FF 조건이 `dirty == 0` 을 전역으로 요구했기 때문에, main workdir 에
이 job 과 무관한 상시 dirty 파일(goals.py, acct-purchase-mockup.html 등, 2026-09-18
실측 4파일)이 있으면 behind>0 인 상황에서 FF 자체를 시도하지 않고 그대로
막혔다.

여기서 고정하는 계약은 [[AADS-DEPLOY-PREFLIGHT-FFONLY-DIRTY-SCOPED-20260918]]다.
자동 FF 는 dirty 파일 집합과 이 job 의 배포 대상 파일 집합이 겹치지 않을
때에만 시도한다. 겹치면(대상 파일이 미커밋 상태) 기존처럼 차단한다. FF 시도
자체가 실패(충돌)하면 `git merge --abort` 로 원복하고 `deploy_preflight_ff_conflict`
로 차단한다 — behind/ahead 상태가 애매한 채로 다음 단계로 넘어가면 안 된다.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

FUNCS = (
    "is_remote_project",
    "is_git_workdir",
    "git_dirty_count",
    "git_ahead_behind_counts",
    "git_dirty_paths",
    "deploy_git_preflight",
)

STUBS = """
set -eo pipefail
log() { echo "$*" >&2; }
sql_escape() { printf '%s' "$1"; }
db_update() { :; }
job_target_files() { printf '%s\\n' "$JOB_TARGET_FILES"; }
_fail_job() {
    echo "FAIL_JOB reason=$3 detail=$4" >&2
    echo "$3" > "$FAIL_JOB_OUT"
}
"""


def _read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


# ── 1) 스크립트 계약 ────────────────────────────────────────────────────


def test_ff_condition_no_longer_requires_global_clean_dirty():
    """자동 FF 조건에서 전역 dirty==0 요구가 사라져야 한다."""
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        fn = _extract_function(script, "deploy_git_preflight")
        assert 'behind:-999}" -gt 0 && "${ahead:-999}" -eq 0 && "${dirty:-999}" -eq 0' not in fn, (
            f"{script_name}: 여전히 dirty==0 전역 조건이 남아있다"
        )
        assert '"${behind:-999}" -gt 0 && "${ahead:-999}" -eq 0' in fn


def test_ff_scoped_by_overlap_with_job_target_files():
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        fn = _extract_function(script, "deploy_git_preflight")
        assert "ff_dirty_paths=$(git_dirty_paths" in fn
        assert "ff_target_files=$(job_target_files" in fn
        assert "comm -12" in fn


def test_ff_conflict_aborts_and_blocks():
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        fn = _extract_function(script, "deploy_git_preflight")
        assert "merge --abort" in fn
        assert "deploy_preflight_ff_conflict" in fn


def test_never_force_pushes_or_force_merges():
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        fn = _extract_function(script, "deploy_git_preflight")
        assert "--force" not in fn
        assert "-f " not in fn


def test_runner_scripts_stay_byte_identical():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")


def test_isolated_static_preserves_legacy_preflight_and_routes_aads_only():
    script = _read_script("pipeline-runner.sh")
    legacy = _extract_function(script, "deploy_git_preflight")
    isolated = _extract_function(script, "deploy_isolated_git_preflight")
    assert 'local job_id="$1" project="$2" session_id="$3" main_workdir="$4"' in legacy
    assert "merge --ff-only origin/main" in legacy
    assert "deploy_preflight_file_conflict" in legacy
    assert 'if [[ "$project" == "AADS" ]]; then\n        if ! deploy_isolated_git_preflight' in script
    assert 'elif ! deploy_git_preflight "$job_id" "$project" "$session_id" "$main_workdir"' in script
    assert "worktree list --porcelain" in isolated
    assert "status --porcelain --untracked-files=all" in isolated
    assert '"$head_sha" != "$approved_sha"' in isolated
    assert "merge-base --is-ancestor" in isolated
    assert "deploy_isolated_stale_approval" in isolated


def test_isolated_static_requires_reviewed_sha_at_remote_before_release():
    script = _read_script("pipeline-runner.sh")
    assert '"$project" == "AADS" && "$push_state" != "fast_forward"' in script
    assert "deploy_isolated_push_state" in script
    assert '"$pushed_remote_sha" != "$expected_sha"' in script
    assert "deploy_isolated_remote_changed" in script
    assert 'if [[ "$project" != "AADS" ]] && ! ensure_approved_job_worktree' in script


# ── 2) 실제 동작 ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    if shutil.which("git") is None or shutil.which("comm") is None:
        pytest.skip("git/comm 미설치 환경 — 동작 테스트 생략")
    script = _read_script("pipeline-runner.sh")
    body = STUBS + "\n".join(_extract_function(script, name) for name in FUNCS)
    path = tmp_path_factory.mktemp("deploy_preflight_fn") / "fn.sh"
    path.write_text(body, encoding="utf-8")
    return path


def _call(fn_file: Path, main_workdir: Path, job_id: str, job_target_files: str, fail_out: Path):
    env_prefix = (
        f'JOB_TARGET_FILES={job_target_files!r} '
        f'FAIL_JOB_OUT={str(fail_out)!r} '
    )
    cmd = (
        f'source "{fn_file}"; '
        f'{env_prefix}deploy_git_preflight "{job_id}" "AADS" "sess-1" "{main_workdir}"'
    )
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def _build_remote_and_main(tmp_path: Path):
    remote = tmp_path / "remote.git"
    main = tmp_path / "main"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(main)], check=True, capture_output=True)
    _git(main, "config", "user.email", "runner@aads.local")
    _git(main, "config", "user.name", "AADS Runner Test")

    (main / "base.txt").write_text("base\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "base")
    _git(main, "push", "origin", "main")
    return remote, main


def _advance_origin(remote: Path, tmp_path: Path, changed_file: str, content: str):
    """다른 클론에서 origin/main 을 전진시킨다 (main workdir 은 건드리지 않음)."""
    other = tmp_path / f"other-{uuid.uuid4().hex[:8]}"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "other@aads.local")
    _git(other, "config", "user.name", "Other Session")
    (other / changed_file).write_text(content, encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", f"advance {changed_file}")
    _git(other, "push", "origin", "main")
    shutil.rmtree(other, ignore_errors=True)


def test_behind_with_no_dirty_is_fast_forwarded(fn_file, tmp_path):
    remote, main = _build_remote_and_main(tmp_path)
    _advance_origin(remote, tmp_path, "unrelated.txt", "incoming\n")

    fail_out = tmp_path / "fail1.txt"
    proc = _call(fn_file, main, "runner-behind-clean", "", fail_out)

    assert proc.returncode == 0, proc.stderr
    assert "DEPLOY_PREFLIGHT_FFONLY_SYNC" in proc.stderr
    assert not fail_out.exists()
    assert (main / "unrelated.txt").read_text(encoding="utf-8") == "incoming\n"


def test_behind_with_dirty_disjoint_from_target_is_fast_forwarded(fn_file, tmp_path):
    remote, main = _build_remote_and_main(tmp_path)
    _advance_origin(remote, tmp_path, "unrelated.txt", "incoming\n")

    # main workdir 에 이 job 과 무관한 상시 dirty 파일이 있다 (예: goals.py)
    (main / "goals.py").write_text("local edit\n", encoding="utf-8")

    fail_out = tmp_path / "fail2.txt"
    proc = _call(fn_file, main, "runner-behind-unrelated-dirty", "app/target.py", fail_out)

    assert proc.returncode == 0, proc.stderr
    assert "DEPLOY_PREFLIGHT_FFONLY_SYNC" in proc.stderr
    assert not fail_out.exists()
    assert (main / "unrelated.txt").read_text(encoding="utf-8") == "incoming\n"
    # 무관한 dirty 파일은 그대로 남아있어야 한다 (건드리지 않음)
    assert (main / "goals.py").read_text(encoding="utf-8") == "local edit\n"


def test_behind_with_dirty_overlapping_target_is_blocked(fn_file, tmp_path):
    remote, main = _build_remote_and_main(tmp_path)
    _advance_origin(remote, tmp_path, "unrelated.txt", "incoming\n")

    # 이 job 의 배포 대상 파일이 미커밋 상태로 dirty (git status --porcelain 이
    # 새 untracked 디렉토리는 개별 파일이 아니라 디렉토리 자체로 접어서 보고하므로,
    # 경로 매칭을 확실히 하려면 최상위 파일로 둔다)
    (main / "target.py").write_text("uncommitted job change\n", encoding="utf-8")

    fail_out = tmp_path / "fail3.txt"
    proc = _call(fn_file, main, "runner-behind-overlap-dirty", "target.py", fail_out)

    assert proc.returncode == 1, proc.stderr
    assert fail_out.exists()
    assert fail_out.read_text().strip() == "deploy_preflight_git_state"
    # FF 를 시도하지 않았으므로 origin 변경분이 들어오면 안 된다
    assert not (main / "unrelated.txt").exists()


def test_ff_conflict_aborts_and_blocks_with_ff_conflict_reason(fn_file, tmp_path):
    remote, main = _build_remote_and_main(tmp_path)
    # origin 이 main workdir 의 dirty 파일과 "같은 파일"을 전진시킨다 → merge --ff-only 가
    # 로컬 미커밋 변경을 덮어써야 하므로 충돌로 실패한다.
    _advance_origin(remote, tmp_path, "conflict.txt", "incoming change\n")
    (main / "conflict.txt").write_text("local uncommitted change\n", encoding="utf-8")

    fail_out = tmp_path / "fail4.txt"
    # 대상 파일은 이 dirty 파일과 무관하므로 FF 시도 자체는 진행된다
    proc = _call(fn_file, main, "runner-ff-conflict", "app/other_target.py", fail_out)

    assert proc.returncode == 1, proc.stderr
    assert fail_out.exists()
    assert fail_out.read_text().strip() == "deploy_preflight_ff_conflict"
    merge_head = subprocess.run(
        ["git", "-C", str(main), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        capture_output=True, text=True,
    )
    assert merge_head.returncode != 0, "merge --abort 후에도 MERGE_HEAD 가 남아있다"
    # 원래 로컬 변경은 보존돼야 한다 (abort 로 원복)
    assert (main / "conflict.txt").read_text(encoding="utf-8") == "local uncommitted change\n"


def test_behind_zero_skips_ff_entirely(fn_file, tmp_path):
    _remote, main = _build_remote_and_main(tmp_path)

    fail_out = tmp_path / "fail5.txt"
    proc = _call(fn_file, main, "runner-uptodate", "", fail_out)

    assert proc.returncode == 0, proc.stderr
    assert "DEPLOY_PREFLIGHT_FFONLY_SYNC" not in proc.stderr
    assert "DEPLOY_PREFLIGHT_OK" in proc.stderr


@pytest.fixture
def isolated_case(tmp_path):
    remote, main = _build_remote_and_main(tmp_path)
    job_id = f"runner-isolated-{uuid.uuid4().hex[:12]}"
    worktree = Path(f"/tmp/aads-wt-{job_id}")
    _git(main, "worktree", "add", "--detach", str(worktree), "HEAD")
    _git(worktree, "config", "user.email", "runner@aads.local")
    _git(worktree, "config", "user.name", "AADS Runner Test")
    (worktree / "release.txt").write_text("approved\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "approved release")
    approved_sha = _git(worktree, "rev-parse", "HEAD")
    try:
        yield remote, main, worktree, job_id, approved_sha
    finally:
        _git(main, "worktree", "remove", "--force", str(worktree))


@pytest.fixture(scope="module")
def isolated_fn_file(tmp_path_factory):
    script = _read_script("pipeline-runner.sh")
    body = STUBS + _extract_function(script, "deploy_isolated_git_preflight")
    path = tmp_path_factory.mktemp("isolated_preflight_fn") / "fn.sh"
    path.write_text(body, encoding="utf-8")
    return path


def _call_isolated(fn_file, main, worktree, job_id, approved_sha, fail_out):
    cmd = (
        f'source "{fn_file}"; '
        f'FAIL_JOB_OUT={str(fail_out)!r} '
        f'deploy_isolated_git_preflight "{job_id}" "AADS" "sess-1" '
        f'"{main}" "{worktree}" "{approved_sha}"'
    )
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_isolated_clean_approved_worktree_ignores_dirty_shared_main(isolated_case, isolated_fn_file, tmp_path):
    _remote, main, worktree, job_id, sha = isolated_case
    (main / "base.txt").write_text("shared dirty\n", encoding="utf-8")
    fail_out = tmp_path / "fail.txt"
    proc = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, fail_out)
    assert proc.returncode == 0, proc.stderr
    assert not fail_out.exists()
    assert (main / "base.txt").read_text() == "shared dirty\n"


@pytest.mark.parametrize("case", ["wrong_sha", "dirty_release", "remote_changed", "fetch_failure"])
def test_isolated_fails_closed(isolated_case, isolated_fn_file, tmp_path, case):
    remote, main, worktree, job_id, sha = isolated_case
    if case == "wrong_sha":
        sha = _git(main, "rev-parse", "HEAD")
    elif case == "dirty_release":
        (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    elif case == "remote_changed":
        _advance_origin(remote, tmp_path, "release.txt", "conflicting\n")
    else:
        _git(main, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    fail_out = tmp_path / "fail.txt"
    proc = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, fail_out)
    assert proc.returncode == 1, proc.stderr
    assert fail_out.exists()
    assert fail_out.read_text().strip() == {
        "wrong_sha": "deploy_isolated_sha_or_dirty",
        "dirty_release": "deploy_isolated_sha_or_dirty",
        "remote_changed": "deploy_isolated_stale_approval",
        "fetch_failure": "deploy_fetch_failed",
    }[case]


def test_isolated_rejects_unregistered_clone(isolated_case, isolated_fn_file, tmp_path):
    remote, main, _worktree, _job_id, _sha = isolated_case
    fake_job_id = f"runner-fake-{uuid.uuid4().hex[:12]}"
    fake_path = Path(f"/tmp/aads-wt-{fake_job_id}")
    subprocess.run(["git", "clone", str(remote), str(fake_path)], check=True, capture_output=True)
    try:
        fail_out = tmp_path / "fake_fail.txt"
        fake_sha = _git(fake_path, "rev-parse", "HEAD")
        proc = _call_isolated(isolated_fn_file, main, fake_path, fake_job_id, fake_sha, fail_out)
        assert proc.returncode == 1, proc.stderr
        assert fail_out.read_text().strip() == "deploy_worktree_not_isolated"
    finally:
        shutil.rmtree(fake_path)
