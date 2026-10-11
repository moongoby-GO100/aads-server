"""셸 러너: 리뷰 입력 git_diff 에서 신규(untracked) 파일이 빠지는 결함 회귀.

배경(2026-10-11, NTV2 runner-e4b91c0b/f3b14f84/eba41d50): git_diff 에 수정 파일 2개만 있고
신규 5개(src/tests/bootstrap.php 등)가 없어 AI 리뷰가 "정의가 diff 에 없다"며 3회 반려했다.
원인: cafe24_114 에서 aads-pipeline-runner.service(PID 964890)가 2026-09-21 부터 재시작되지 않고
10-03 한글 파일명 수정(f73f7b25) 이전 스크립트를 메모리에 들고 있었다. 그 코드는
`git ls-files --others | xargs git add -N` 이라, 한글 이름이 하나라도 있으면 배치가 통째로 실패해
ASCII 신규 파일까지 diff 에서 사라진다. 동기화 스크립트는 같은 호스트의 다른 서비스를 보지 않는다.
이 테스트는 i-t-a 가 어떤 이유로 실패해도 공유 블록이 신규 파일 본문을 직접 붙이는지 검사한다.
오류 사전 키: runner.review_diff_newfiles_missing_stale_service
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BEGIN = "# ─── SHARED-BLOCK BEGIN: job_diff_contract"
END = "# ─── SHARED-BLOCK END: job_diff_contract"
KOREAN = "docs/reports/20261011_테스트_격리_실행법.md"


def _shared_block(name: str) -> str:
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    start = text.index(BEGIN)
    return text[start:text.index(END, start)]


def _intent_fn() -> str:
    text = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    start = text.index("\n_intent_to_add_nul() {") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _bash(code: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", code], cwd=cwd, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "wt"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "src/tests").mkdir(parents=True)
    (r / "src/tests/TestCase.php").write_text("<?php\n// base\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _write_new_files(r: Path) -> None:
    (r / "src/tests/bootstrap.php").write_text("<?php\nfunction boot_marker_ASCII() {}\n", encoding="utf-8")
    (r / "scripts").mkdir(exist_ok=True)
    (r / "scripts/test-isolated.sh").write_text("#!/bin/sh\necho iso_marker\n", encoding="utf-8")
    (r / KOREAN).parent.mkdir(parents=True, exist_ok=True)
    (r / KOREAN).write_text("# 한글 본문 마커\n", encoding="utf-8")


def _capture(r: Path, prep: str = "", base: str = "") -> str:
    code = f'{_shared_block("pipeline-runner.sh")}\n{_intent_fn()}\n{prep}\ncapture_job_diff_text "$PWD" "{base}"'
    res = _bash(code, r)
    assert res.returncode == 0, res.stderr
    return res.stdout


# 10-03 이전 코드의 신규 파일 표시 방식(따옴표 처리된 경로가 xargs 로 넘어가 배치 전체 실패)
STALE_ITA = "git ls-files --others --exclude-standard | xargs -d '\\n' -r git add -N -- 2>/dev/null || true"
FIXED_ITA = "git -c core.quotePath=false ls-files -z --others --exclude-standard | _intent_to_add_nul -N"


def test_stale_intent_to_add_loses_every_new_file_without_fallback(repo):
    """원인 재현: 옛 방식은 한글 이름 하나 때문에 ASCII 신규 파일까지 index 에 못 올린다."""
    _write_new_files(repo)
    assert _bash(STALE_ITA, repo).returncode == 0
    assert _git(repo, "diff", "--name-only", "HEAD").strip() == ""


@pytest.mark.parametrize("prep", [STALE_ITA, ""], ids=["stale_ita_failed", "no_ita_at_all"])
def test_new_files_only_are_in_git_diff_even_when_ita_failed(repo, prep):
    _write_new_files(repo)
    out = _capture(repo, prep)
    assert "boot_marker_ASCII" in out
    assert "iso_marker" in out
    assert "한글 본문 마커" in out
    assert "# [untracked-fallback] src/tests/bootstrap.php" in out
    assert f"# [untracked-fallback] {KOREAN}" in out
    assert "new file mode" in out


@pytest.mark.parametrize("prep", [STALE_ITA, ""], ids=["stale_ita_failed", "no_ita_at_all"])
def test_new_plus_modified_keeps_modified_and_adds_new(repo, prep):
    (repo / "src/tests/TestCase.php").write_text("<?php\n// base\n// modified_marker\n", encoding="utf-8")
    _write_new_files(repo)
    out = _capture(repo, prep)
    assert "modified_marker" in out
    assert "boot_marker_ASCII" in out
    assert "한글 본문 마커" in out


def test_ita_success_has_all_files_and_no_fallback_duplicate(repo):
    (repo / "src/tests/TestCase.php").write_text("<?php\n// base\n// modified_marker\n", encoding="utf-8")
    _write_new_files(repo)
    out = _capture(repo, FIXED_ITA)
    assert out.count("boot_marker_ASCII") == 1
    assert out.count("한글 본문 마커") == 1
    assert "modified_marker" in out
    assert "[untracked-fallback]" not in out


def test_new_files_after_runner_commit_plus_untracked_remainder(repo):
    """base..HEAD 경로: AI 가 일부를 커밋하고 나머지 신규 파일이 untracked 로 남은 경우."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "committed.txt").write_text("committed_marker\n", encoding="utf-8")
    _git(repo, "add", "committed.txt")
    _git(repo, "commit", "-q", "-m", "ai commit")
    _write_new_files(repo)
    out = _capture(repo, STALE_ITA, base)
    assert "committed_marker" in out
    assert "boot_marker_ASCII" in out
    assert "한글 본문 마커" in out


def test_runner_artifacts_are_not_pulled_in_by_fallback(repo):
    (repo / "RESULT-X.md").write_text("result_artifact_marker\n", encoding="utf-8")
    (repo / ".runner_full_diff.patch").write_text("patch_artifact_marker\n", encoding="utf-8")
    (repo / "real.txt").write_text("real_marker\n", encoding="utf-8")
    out = _capture(repo)
    assert "real_marker" in out
    assert "result_artifact_marker" not in out
    assert "patch_artifact_marker" not in out


def test_gitignored_untracked_files_are_not_pulled_in(repo):
    (repo / ".gitignore").write_text("*.cache\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    (repo / "x.cache").write_text("ignored_marker\n", encoding="utf-8")
    (repo / "real.txt").write_text("real_marker\n", encoding="utf-8")
    out = _capture(repo)
    assert "real_marker" in out
    assert "ignored_marker" not in out


def test_fallback_respects_byte_cap(repo):
    (repo / "big.txt").write_text(("x" * 99 + "\n") * 2000, encoding="utf-8")
    out = _capture(repo)
    assert "# [untracked-fallback] big.txt" in out
    assert len(out.encode("utf-8")) <= 50000 + 200


def test_clean_worktree_still_yields_empty_diff(repo):
    assert _capture(repo) == ""


def test_shared_blocks_identical_between_runner_and_sweeper():
    assert _shared_block("pipeline-runner.sh") == _shared_block("review-hold-sweeper.sh")


def test_runner_copies_identical_and_logs_fallback_use():
    runner = (ROOT / "scripts/pipeline-runner.sh").read_bytes()
    assert runner == (ROOT / "scripts/pipeline-runner.sh.local").read_bytes()
    assert b"DIFF_UNTRACKED_FALLBACK" in runner


def test_sync_script_warns_about_stale_sibling_runner_read_only():
    sync = (ROOT / "scripts/sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")
    start = sync.index("\nwarn_stale_runner_processes() {") + 1
    fn = sync[start:sync.index("\n}\n", start) + 3]
    assert "(deleted)" in fn and "STALE_RUNNER_PROCESS" in fn
    for forbidden in ("kill", "systemctl restart", "systemctl stop"):
        assert forbidden not in fn
    assert 'warn_stale_runner_processes "$_stale_name" "$_stale_host"' in sync
