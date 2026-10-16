"""러너 보존 패치(logs/runner-diff/*.patch)는 개행으로 끝나야 git apply 가 받는다.

배경(2026-10-16): _persist_full_diff_if_truncated 가 full_diff=$(git diff ...) 로 받은 뒤
printf '%s' 로 썼다. 명령 치환이 끝 개행을 지우고 '%s' 는 붙이지 않아 마지막 줄 개행이 없었고,
git apply 가 "corrupt patch at line N" 으로 거부했다(runner-4cfa24b2.patch).
오류 사전: runner.full_diff_patch_missing_trailing_newline
"""

import subprocess
from pathlib import Path

import pytest

SCRIPTS = ["pipeline-runner.sh", "pipeline-runner.sh.local"]
SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
LOG_DIR_LITERAL = "/root/aads/aads-server/logs/runner-diff"


def _read(name: str) -> str:
    return (SCRIPT_DIR / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _assert_applies_to_base(patch: Path, tmp_path: Path) -> None:
    base_dir = tmp_path / "base-copy"
    base_dir.mkdir()
    (base_dir / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    proc = subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=base_dir, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def _run_persist(script_name: str, repo: Path, tmp_path: Path, base_sha: str, head_sha: str):
    fn = _extract_function(_read(script_name), "_persist_full_diff_if_truncated")
    assert LOG_DIR_LITERAL in fn
    log_dir = tmp_path / "runner-diff"
    fn = fn.replace(LOG_DIR_LITERAL, str(log_dir))
    harness = tmp_path / "harness.sh"
    harness.write_text(
        'log() { echo "$*" >> "$LOG_OUT"; }\n'
        f"ARTIFACT_DIR={tmp_path}/artifacts\n"
        f"{fn}\n"
        '_persist_full_diff_if_truncated "job-x" "$1" "$2" "$3" ""\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(harness), str(repo), base_sha, head_sha],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin:/usr/local/bin", "LOG_OUT": str(tmp_path / "log.txt")},
    )
    assert proc.returncode == 0, proc.stderr
    return log_dir / "job-x.patch"


def test_scripts_stay_identical_and_use_newline_printf():
    assert _read("pipeline-runner.sh") == _read("pipeline-runner.sh.local")
    for name in SCRIPTS:
        fn = _extract_function(_read(name), "_persist_full_diff_if_truncated")
        assert "printf '%s\\n' \"$full_diff\" > \"$log_patch\"" in fn
        assert "printf '%s' \"$full_diff\"" not in fn


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_committed_diff_patch_ends_with_newline_and_applies(script_name, repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    head = _git(repo, "rev-parse", "HEAD").strip()

    patch = _run_persist(script_name, repo, tmp_path, base, head)
    data = patch.read_bytes()
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")

    _assert_applies_to_base(patch, tmp_path)


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_uncommitted_only_diff_patch_ends_with_newline_and_applies(script_name, repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    patch = _run_persist(script_name, repo, tmp_path, head, head)
    assert patch.read_bytes().endswith(b"\n")
    _assert_applies_to_base(patch, tmp_path)
