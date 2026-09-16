"""Pipeline Runner push 사전판별(classify_push_state) 계약 + 동작 테스트.

배경: 2026-09-16 runner-3c82de0b(NTV2) 가 `! [rejected] ... (non-fast-forward)` 로
배포 중단됐다. 승인 SHA 의 base 가 origin/main 보다 낡았을 때 러너는 원인을 분류하지
못하고 push_fail 만 남겼고, 운영자가 매번 수동으로 로컬/원격 HEAD 를 대조해야 했다.

이 테스트는 두 가지를 고정한다.
  1) 스크립트 계약 — classify_push_state 존재, 3상태 처리, force push 부재.
  2) 실제 동작 — 임시 git 저장소로 already_present / fast_forward / stale_base 판별.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


# ── 1) 스크립트 계약 ────────────────────────────────────────────────────


def test_push_precheck_contract_present_in_both_runner_scripts():
    for script_name in SCRIPTS:
        script = _read_script(script_name)

        assert "classify_push_state() {" in script
        assert "PUSH_PRECHECK job=$job_id" in script
        assert "push_state=$(classify_push_state \"$worktree_dir\" \"$current_sha\")" in script

        # 3상태 + 판별불가가 모두 처리돼야 한다
        for state in ("already_present", "fast_forward", "stale_base", "fetch_fail"):
            assert state in script, f"{script_name}: {state} 미처리"


def test_already_present_skips_push_instead_of_failing():
    script = _read_script("pipeline-runner.sh")

    precheck = script.index("PUSH_PRECHECK job=$job_id")
    push_cmd = script.index('git -C "$worktree_dir" push origin "${current_sha}:refs/heads/main"')

    # 사전판별은 반드시 실제 push 앞에 온다
    assert precheck < push_cmd
    assert 'push_skipped="true"' in script
    assert "PUSH_ALREADY_PRESENT job=$job_id" in script
    assert 'if [[ "$push_skipped" != "true" ]]; then' in script


def test_stale_base_is_terminal_and_never_force_pushes():
    script = _read_script("pipeline-runner.sh")

    assert "phase='push_stale_base'" in script
    assert "push_stale_base: 승인 SHA" in script
    # force push 는 어떤 형태로도 존재하면 안 된다
    assert "push --force" not in script
    assert "push -f " not in script
    assert "--force-with-lease" not in script


def test_push_failure_rechecks_once_for_concurrent_push_race():
    script = _read_script("pipeline-runner.sh")

    assert "push_recheck=$(classify_push_state" in script
    assert "PUSH_RACE_RESOLVED job=$job_id" in script
    # 실패 진단에 판별 상태가 함께 남아야 원인 추적이 가능하다
    assert 'push_fail(state=${push_state}/recheck=${push_recheck:-none})' in script


def test_runner_scripts_stay_byte_identical():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")


# ── 2) 실제 동작 ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def classify_fn(tmp_path_factory):
    if shutil.which("git") is None or shutil.which("awk") is None:
        pytest.skip("git/awk 미설치 환경 — 동작 테스트 생략")
    fn_dir = tmp_path_factory.mktemp("classify_fn")
    fn_file = fn_dir / "fn.sh"
    fn_file.write_text(
        "set -eo pipefail\n" + _extract_function(_read_script("pipeline-runner.sh"), "classify_push_state"),
        encoding="utf-8",
    )
    return fn_file


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _classify(fn_file: Path, repo: Path, sha: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; classify_push_state "{repo}" "{sha}"'],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def repo_pair(tmp_path_factory):
    base = tmp_path_factory.mktemp("push_state")
    remote = base / "remote.git"
    clone = base / "clone"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "runner@aads.local")
    _git(clone, "config", "user.name", "AADS Runner Test")

    (clone / "a.txt").write_text("A\n", encoding="utf-8")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-m", "A")
    _git(clone, "push", "origin", "HEAD:refs/heads/main")
    sha_a = _git(clone, "rev-parse", "HEAD")

    (clone / "b.txt").write_text("B\n", encoding="utf-8")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-m", "B")
    sha_b = _git(clone, "rev-parse", "HEAD")
    # B 도 원격에 올려 둔다 — 테스트가 update-ref 로 원격 main 을 A/B 로 자유롭게 옮기려면
    # 해당 객체가 bare 저장소에 존재해야 한다.
    _git(clone, "push", "origin", f"{sha_b}:refs/heads/main")

    # A 를 base 로 한 별개 커밋 C — origin/main 이 B 로 전진하면 stale_base 가 된다
    _git(clone, "checkout", "-q", "-b", "side", sha_a)
    (clone / "c.txt").write_text("C\n", encoding="utf-8")
    _git(clone, "add", "c.txt")
    _git(clone, "commit", "-m", "C")
    sha_c = _git(clone, "rev-parse", "HEAD")

    return {"remote": remote, "clone": clone, "a": sha_a, "b": sha_b, "c": sha_c}


def _set_remote_main(repo_pair, sha: str) -> None:
    """원격 main 을 명시적으로 고정한다 — 테스트 실행 순서에 의존하지 않기 위함."""
    _git(repo_pair["remote"], "update-ref", "refs/heads/main", sha)


def test_classify_fast_forward_when_remote_is_ancestor(classify_fn, repo_pair):
    # origin/main = A, 밀려는 커밋 = B(A 위) → fast_forward
    _set_remote_main(repo_pair, repo_pair["a"])
    assert _classify(classify_fn, repo_pair["clone"], repo_pair["b"]) == "fast_forward"


def test_classify_stale_base_when_histories_diverge(classify_fn, repo_pair):
    # origin/main = B, 밀려는 커밋 = C(A 위) → 서로 조상이 아님
    _set_remote_main(repo_pair, repo_pair["b"])
    assert _classify(classify_fn, repo_pair["clone"], repo_pair["c"]) == "stale_base"


def test_classify_already_present_when_commit_is_in_remote(classify_fn, repo_pair):
    # origin/main = B 이므로 A 와 B 는 이미 원격에 포함 → push 불필요
    _set_remote_main(repo_pair, repo_pair["b"])
    assert _classify(classify_fn, repo_pair["clone"], repo_pair["b"]) == "already_present"
    assert _classify(classify_fn, repo_pair["clone"], repo_pair["a"]) == "already_present"


def test_classify_fetch_fail_on_invalid_sha(classify_fn, repo_pair):
    assert _classify(classify_fn, repo_pair["clone"], "not-a-sha") == "fetch_fail"
