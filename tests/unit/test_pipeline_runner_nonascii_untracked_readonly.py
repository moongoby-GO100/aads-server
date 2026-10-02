"""셸 러너: 한글 파일명 intent-to-add 일괄 실패 + 본문 자연어 읽기전용 오판 회귀.

배경(2026-10-03, runner-4bc5f67e / runner-cb6d75c2): `git ls-files --others` 가 core.quotePath 때문에
한글 파일명을 따옴표·8진수로 출력 → `xargs git add -N` 이 pathspec 불일치로 전체 실패(rc=123) →
ASCII 파일까지 diff 에 안 잡혀 NO_CHANGES_READ_ONLY done 처리, worktree 정리로 산출물 소실.
지시문 본문의 "수정하지/read-only" 문구가 읽기전용으로 판정된 것도 원인이었다.
Python 쪽 규칙은 tests/unit/test_runner_untracked_readonly.py 가 검사한다 (같은 규칙 — 한쪽만 고치지 마라).
오류 사전 키: runner.untracked_new_files_discarded_as_read_only
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _fn(script: str, name: str) -> str:
    start = script.index(f"\n{name}() {{") + 1
    return script[start:script.index("\n}\n", start) + 3]


def _intent_block(script: str) -> str:
    start = script.index('    local _new_untracked=""')
    end = script.index('    local git_diff=""', start)
    return script[start:end]


def _bash(code: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", code], cwd=cwd, capture_output=True, text=True)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")


def test_scripts_are_byte_identical():
    assert (ROOT / "scripts/pipeline-runner.sh").read_bytes() == (
        ROOT / "scripts/pipeline-runner.sh.local"
    ).read_bytes()


# ── 읽기전용 판정 ─────────────────────────────────────────────────────


def _is_read_only(script_name: str, instruction: str) -> bool:
    fn = _fn(_read(script_name), "is_read_only_instruction")
    return subprocess.run(
        ["bash", "-c", f'{fn}\nis_read_only_instruction "$INSTR"'],
        env={**os.environ, "INSTR": instruction},
        capture_output=True,
    ).returncode == 0


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_body_natural_language_only_is_not_read_only(script_name):
    text = (
        "TASK_ID: X\nTITLE: 새 파일 추가\n\n기존 파일은 수정하지 말고 새 파일만 만들어라. 기존 코드를 변경하지 말 것.\n"
        "read-only 로 두라, do not modify, no file changes, 읽기 전용, 파일 수정 금지"
    )
    assert _is_read_only(script_name, text) is False


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_header_marker_is_read_only(script_name):
    assert _is_read_only(script_name, "MODE: READ_ONLY\nTASK_ID: X\n본문") is True
    assert _is_read_only(script_name, "TASK_ID: X\n   mode :  read_only   \n본문") is True


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_marker_after_line_20_is_not_read_only(script_name):
    late = "TASK_ID: X\n" + "\n".join(f"line {i}" for i in range(19)) + "\nMODE: READ_ONLY\n"
    assert late.splitlines()[20] == "MODE: READ_ONLY"  # 21번째 줄
    assert _is_read_only(script_name, late) is False
    inside = "TASK_ID: X\n" + "\n".join(f"line {i}" for i in range(18)) + "\nMODE: READ_ONLY\n"
    assert inside.splitlines()[19] == "MODE: READ_ONLY"  # 20번째 줄
    assert _is_read_only(script_name, inside) is True


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_marker_must_be_whole_line(script_name):
    assert _is_read_only(script_name, "이 작업은 MODE: READ_ONLY 가 아니다") is False
    assert _is_read_only(script_name, "MODE: READ_ONLY_PLUS") is False
    assert _is_read_only(script_name, "") is False


# ── 한글 파일명 intent-to-add ─────────────────────────────────────────


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_nonascii_and_ascii_untracked_files_both_reach_git_diff(tmp_path, script_name):
    script = _read(script_name)
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "d").mkdir()
    (repo / "d" / "PRD.md").write_text("prd\n", encoding="utf-8")
    (repo / "d" / "기획서.md").write_text("기획\n", encoding="utf-8")

    harness = (
        "set -u\nlog() { echo \"$*\" >> \"$LOGF\"; }\n"
        f"worktree_dir=\"\"; job_id=j\n{_fn(script, '_intent_to_add_nul')}\n{_intent_block(script)}\n"
    )
    env = {**os.environ, "LOGF": str(tmp_path / "log.txt")}
    proc = subprocess.run(["bash", "-c", harness], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    names = _git(repo, "-c", "core.quotePath=false", "diff", "--name-only", "HEAD").splitlines()
    assert "d/PRD.md" in names and "d/기획서.md" in names
    diff = _git(repo, "-c", "core.quotePath=false", "diff", "HEAD")
    assert "+prd" in diff and "+기획" in diff


def test_old_pattern_reproduces_rc123_and_empty_diff(tmp_path):
    """수정 전 패턴이 실제로 실패함을 고정한다 — 이 재현이 깨지면 위 테스트가 무의미해진다."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "d").mkdir()
    (repo / "d" / "PRD.md").write_text("prd\n", encoding="utf-8")
    (repo / "d" / "기획서.md").write_text("기획\n", encoding="utf-8")
    proc = _bash("git ls-files --others --exclude-standard | xargs -d '\\n' -r git add -N --", cwd=repo)
    assert proc.returncode == 123
    assert _git(repo, "diff", "HEAD").strip() == ""


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_one_bad_path_does_not_block_other_files_and_is_logged(tmp_path, script_name):
    script = _read(script_name)
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "good.txt").write_text("g\n", encoding="utf-8")
    (repo / "한글.txt").write_text("h\n", encoding="utf-8")
    logf = tmp_path / "log.txt"
    harness = (
        "log() { echo \"$*\" >> \"$LOGF\"; }\n"
        f"{_fn(script, '_intent_to_add_nul')}\n"
        "printf 'nonexistent.txt\\0good.txt\\0한글.txt\\0' | _intent_to_add_nul -N\n"
    )
    proc = subprocess.run(
        ["bash", "-c", harness], cwd=repo, env={**os.environ, "LOGF": str(logf)},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    names = _git(repo, "-c", "core.quotePath=false", "diff", "--name-only", "HEAD").splitlines()
    assert "good.txt" in names and "한글.txt" in names
    logged = logf.read_text(encoding="utf-8")
    assert "INTENT_TO_ADD_BATCH_FAILED" in logged and "files=3" in logged and "failed=1" in logged


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_ignored_rescue_path_handles_nonascii_in_whitelist_repo(tmp_path, script_name):
    """.gitignore 가 '*' 인 저장소(rescue 경로)에서도 한글 신규 파일이 diff 에 잡힌다."""
    script = _read(script_name)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "seed")
    os.utime(repo / ".git", (1_000_000_000, 1_000_000_000))
    (repo / "산출물.md").write_text("내용\n", encoding="utf-8")
    (repo / "out.md").write_text("out\n", encoding="utf-8")

    harness = (
        "set -u\nlog() { echo \"$*\" >> \"$LOGF\"; }\n"
        f"worktree_dir=\"{repo}\"; job_id=j\n{_fn(script, '_intent_to_add_nul')}\n{_intent_block(script)}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", harness], cwd=repo, env={**os.environ, "LOGF": str(tmp_path / "log.txt")},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    names = _git(repo, "-c", "core.quotePath=false", "diff", "--name-only", "HEAD").splitlines()
    assert "산출물.md" in names and "out.md" in names
