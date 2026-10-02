"""리뷰 입력 diff 절단 고지 + 전체 diffstat 동봉, .runner_full_diff.patch 커밋 유입 차단.

(R2) 이번 라운드는 파일 추적 해제(git rm --cached)를 하지 않는다. 스테이징 제외
pathspec 과 .gitignore 로 유입만 막는다.

배경(2026-09-30): capture_job_diff_text 가 45000B 에서 조용히 자르고, 추적 중이던
.runner_full_diff.patch(64KB+)가 diff 앞머리를 다 먹어 실제 소스 변경이 리뷰어에게
보이지 않았다. 리뷰어는 "테스트 파일이 없다"며 세 번 연속 반려했다.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATCH_NAME = ".runner_full_diff.patch"
SHARED_BLOCK_BEGIN = "# ─── SHARED-BLOCK BEGIN: job_diff_contract"
SHARED_BLOCK_END = "# ─── SHARED-BLOCK END: job_diff_contract"
FUNCS = (
    "review_diff_stat_section",
    "review_diff_truncation_notice",
    "build_review_diff_prefix",
)


def _runner() -> str:
    return (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def _func(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


# ── 정적 검사 ──────────────────────────────────────────────────────────


def test_runner_has_truncation_notice_and_stat_attachment():
    script = _runner()
    assert "[DIFF TRUNCATED]" in script
    assert "[DIFFSTAT TRUNCATED" in script
    assert "diff --stat" in _func(script, "review_diff_stat_section")
    assert "--name-only" in _func(script, "review_diff_stat_section")
    assert "head -c 6000" in _func(script, "review_diff_stat_section")


def test_prefix_caps_match_shared_capture_caps():
    """절단 판정 상한이 capture_job_diff_text 의 head -c 값과 갈라지면 고지가 거짓이 된다."""
    script = _runner()
    shared = script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]
    capture_caps = set(re.findall(r"head -c (\d+)", _func(shared, "capture_job_diff_text")))
    prefix = _func(script, "build_review_diff_prefix")
    prefix_caps = set(re.findall(r"cap_\w+=(\d+)", prefix))
    assert capture_caps == prefix_caps == {"45000", "5000", "50000"}


def test_review_request_uses_prefixed_diff_but_stores_raw_git_diff():
    script = _runner()
    start = script.index('local review_diff="$git_diff"')
    end = script.index("while [[ $review_attempt -lt $review_max_attempts ]]", start)
    region = script[start:end]
    assert region.count('--arg diff "$review_diff"') == 2
    assert '--arg diff "$git_diff"' not in region
    # DB 저장값·스위퍼 비교 기준은 앞머리 없는 원본 그대로여야 한다.
    assert 'git_diff=$(sql_escape "$git_diff")' in script
    assert "review_diff_prefix" not in script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]


def test_every_git_add_all_excludes_full_diff_patch():
    script = _runner()
    lines = [ln for ln in script.splitlines() if "add -A" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) >= 2
    for ln in lines:
        assert PATCH_NAME in ln, ln


def test_gitignore_lists_full_diff_patch():
    entries = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert PATCH_NAME in entries


def test_full_diff_patch_still_tracked_this_round():
    """추적 해제는 보존게이트 교정이 배포된 뒤 별도 job 이다 — 이번 라운드에는 추적 상태가 정상."""
    if shutil.which("git") is None:
        pytest.skip("git 미설치")
    res = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        pytest.skip("git 저장소로 읽을 수 없는 환경(마운트된 워크트리)")
    if PATCH_NAME not in res.stdout.splitlines():
        pytest.skip("추적 해제 라운드 이후 — 이 검사는 더 이상 해당 없음")


def test_full_diff_patch_is_excluded_from_intent_to_add_candidates():
    script = _runner()
    assert "coverage\\.xml|\\.runner_full_diff\\.patch" in script


# ── 동작 검사 — 실제 git 저장소에서 bash 함수를 직접 호출 ─────────────────


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    res = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash 미설치")
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _write(r, "README.md", "base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _prefix(repo: Path, base: str) -> str:
    script = _runner()
    body = "\n".join(_func(script, n) for n in FUNCS)
    res = subprocess.run(
        ["bash", "-c", f'set -eo pipefail\n{body}\nbuild_review_diff_prefix "$1" "$2"', "bash", str(repo), base],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def _commit_big_change(repo: Path, extra_files: int = 0) -> str:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "app/big.py", "".join(f"value_{i} = {i}\n" for i in range(6000)))
    for i in range(extra_files):
        _write(repo, f"tests/unit/generated_case_directory_number_{i:04d}/test_case_{i:04d}.py", "x = 1\n")
    _write(repo, "tests/unit/test_late.py", "def test_late():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return base


def test_truncated_diff_gets_notice_and_full_diffstat(repo):
    base = _commit_big_change(repo)
    head = _git(repo, "rev-parse", "HEAD")
    full = len(subprocess.run(
        ["git", "-C", str(repo), "diff", f"{base}..{head}"], capture_output=True).stdout)
    assert full > 45000

    out = _prefix(repo, base)

    assert out.startswith(f"[DIFF TRUNCATED] 전체 {full}B 중 앞 45000B 만 아래에 포함됨.")
    assert "DIFFSTAT (전체 변경 파일 — 절단 없음)" in out
    # 45KB 뒤에 밀려 잘려 나가는 파일도 목록에는 있어야 한다.
    assert "tests/unit/test_late.py" in out
    assert "app/big.py" in out
    assert "[DIFFSTAT TRUNCATED" not in out


def test_small_diff_gets_no_prefix_at_all(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "a.txt", "hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "small")
    assert _prefix(repo, base) == ""


def test_oversized_diffstat_reports_untruncated_file_count(repo):
    base = _commit_big_change(repo, extra_files=150)
    names = _git(repo, "diff", "--name-only", f"{base}..HEAD").splitlines()
    assert len(names) == 152

    out = _prefix(repo, base)

    assert "[DIFFSTAT TRUNCATED — 파일 152개]" in out
    assert "절단 없음" not in out
    stat_part = out.split("=== DIFF")[0]
    assert len(stat_part.encode("utf-8")) < 6000 + 1500


def test_uncommitted_changes_get_their_own_diffstat_section(repo):
    base = _commit_big_change(repo)
    _write(repo, "README.md", "base\nedited after commit\n")

    out = _prefix(repo, base)

    assert "DIFFSTAT (미커밋 변경 — git diff HEAD --stat" in out
    assert "README.md" in out


def test_no_base_branch_uses_single_cap(repo):
    _write(repo, "app/big.py", "".join(f"value_{i} = {i}\n" for i in range(6000)))
    _git(repo, "add", "-N", "app/big.py")

    out = _prefix(repo, "")

    assert re.match(r"\[DIFF TRUNCATED\] 전체 \d+B 중 앞 50000B 만", out)
    assert "app/big.py" in out


def test_git_add_all_does_not_use_exclude_pathspec_for_ignored_patch():
    """.gitignore 에 오른 파일을 ':(exclude)' 로 지정하면 git add 가 rc=1 을 낸다.

    2026-10-02 15:19 KST runner-805e8cb8 이 그렇게 approval_commit_stage_failed 로 죽었다
    (5cb1f84e 가 패치 파일을 .gitignore 에 올린 직후). add -A 후 reset 으로 빼야 한다.
    """
    script = _runner()
    lines = [ln for ln in script.splitlines() if "add -A" in ln and not ln.lstrip().startswith("#")]
    for ln in lines:
        assert ":(exclude)" not in ln, ln
        assert f"reset -q -- {PATCH_NAME}" in ln, ln
