"""러너 변경 판정: untracked 새 파일 포함 + 읽기전용은 헤더 MODE 마커로만 판정.

배경(2026-10-03, runner-4bc5f67e): 새 파일 5개만 만든 작업이 `git diff` 에 안 잡혀 diff 가 비었고,
지시문 본문의 "수정하지/변경하지" 문구가 읽기전용으로 오판돼 done 처리 → worktree 정리로 산출물이 소실됐다.
오류 사전 키: runner.untracked_new_files_discarded_as_read_only
"""

import asyncio
import subprocess
from unittest.mock import AsyncMock

from app.services.pipeline_runner_service import (
    PipelineCJob,
    _MAX_DIFF_CHARS,
    _is_read_only_done,
    _is_read_only_instruction,
)


def _job(**attrs) -> PipelineCJob:
    job = PipelineCJob(project="AADS", instruction="x", chat_session_id="")
    for k, v in attrs.items():
        setattr(job, k, v)
    return job


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


# ── 읽기전용 판정 ─────────────────────────────────────────────────────


def test_korean_negative_phrases_in_body_without_marker_are_not_read_only():
    text = "TASK_ID: X\nTITLE: 새 파일 추가\n\n기존 파일은 수정하지 말고 새 파일만 만들어라. 기존 코드를 변경하지 말 것.\nread-only 로 두라, do not modify, no file changes"
    assert _is_read_only_instruction(text) is False


def test_header_marker_is_read_only_case_and_space_insensitive():
    assert _is_read_only_instruction("MODE: READ_ONLY\nTASK_ID: X\n본문") is True
    assert _is_read_only_instruction("TASK_ID: X\n   mode :  read_only   \n본문") is True


def test_marker_only_in_middle_of_body_is_not_read_only():
    body = "TASK_ID: X\n" + "\n".join(f"line {i}" for i in range(40)) + "\nMODE: READ_ONLY\n"
    assert _is_read_only_instruction(body) is False


def test_marker_must_be_whole_line():
    assert _is_read_only_instruction("이 작업은 MODE: READ_ONLY 가 아니다") is False
    assert _is_read_only_instruction("MODE: READ_ONLY_PLUS") is False
    assert _is_read_only_instruction("") is False
    assert _is_read_only_instruction(None) is False


# ── 변경 diff 헬퍼 ────────────────────────────────────────────────────


def test_helper_includes_untracked_file_diff_when_tracked_diff_is_empty(tmp_path):
    job = _job()
    job._ssh_command = AsyncMock(side_effect=["", "diff --git a/new.py b/new.py\n+print(1)\n"])
    diff = asyncio.run(job._collect_change_diff("abc123"))
    assert "new.py" in diff and diff.strip()
    cmds = [c.args[0] for c in job._ssh_command.await_args_list]
    assert cmds[0] == "git diff abc123"
    assert "ls-files --others --exclude-standard" in cmds[1]
    assert "git add" not in " ".join(cmds)


def test_helper_keeps_tracked_diff_and_caps_length():
    job = _job()
    job._ssh_command = AsyncMock(side_effect=["T" * (_MAX_DIFF_CHARS + 10), "U" * 100])
    diff = asyncio.run(job._collect_change_diff(""))
    assert len(diff) == _MAX_DIFF_CHARS
    assert job._ssh_command.await_args_list[0].args[0] == "git diff HEAD"


def test_helper_real_git_untracked_only_and_index_untouched(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "old.txt").write_text("old\n")
    _git(tmp_path, "add", "old.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "ignored.log").write_text("x\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-q", "-m", "ignore")
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "new dir").mkdir()
    (tmp_path / "new dir" / "new file.py").write_text("print('new')\n")

    job = _job(server="localhost", workdir=str(tmp_path))
    diff = asyncio.run(job._collect_change_diff(sha))
    assert "new file.py" in diff and "+print('new')" in diff
    assert "ignored.log" not in diff
    status = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert "?? " in status and "A " not in status.replace("?? ", "")


def test_helper_real_git_no_changes_is_empty(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    job = _job(server="localhost", workdir=str(tmp_path))
    assert asyncio.run(job._collect_change_diff("")).strip() == ""


# ── read_only_done 분기 ───────────────────────────────────────────────


def test_marker_plus_untracked_file_does_not_take_read_only_done_branch():
    instruction = "MODE: READ_ONLY\nTASK_ID: X\n"
    job = _job()
    job._ssh_command = AsyncMock(side_effect=["", "diff --git a/n.py b/n.py\n+1\n"])
    diff = asyncio.run(job._collect_change_diff("abc"))
    assert _is_read_only_done(instruction, diff, "some output") is False


def test_marker_with_empty_diff_and_output_takes_read_only_done_branch():
    assert _is_read_only_done("MODE: READ_ONLY\nx", "", "output") is True
    assert _is_read_only_done("MODE: READ_ONLY\nx", "  \n", "output") is True
    assert _is_read_only_done("MODE: READ_ONLY\nx", "", "  ") is False


def test_no_marker_and_empty_diff_never_read_only_done():
    assert _is_read_only_done("기존 파일은 수정하지 말라", "", "output") is False
