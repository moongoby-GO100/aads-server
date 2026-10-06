"""러너 서비스: 알림·DB 용 git_diff(50,000자)와 별개로 보존·범위 게이트용 전체 diff 를 보존한다.

배경: GO100 runner-36797f75 의 128,134B diff 가 상류에서 표시 없이 잘려 보존 게이트가
`def main` 반환형 변경을 삭제로 오판했다. 오류 사전 키: reviewer.upstream_diff_truncation_false_symbol_delete
"""

import asyncio
from unittest.mock import AsyncMock

from app.services.pipeline_runner_service import (
    PipelineCJob,
    _MAX_DIFF_CHARS,
    _MAX_REVIEW_DIFF_CHARS,
)


def _job(**attrs) -> PipelineCJob:
    job = PipelineCJob(project="AADS", instruction="허용 파일: app/a.py", chat_session_id="")
    for key, value in attrs.items():
        setattr(job, key, value)
    return job


def _big_diff(total: int) -> str:
    head = "diff --git a/app/a.py b/app/a.py\n--- a/app/a.py\n+++ b/app/a.py\n@@ -1 +1 @@\n"
    tail = "\ndiff --git a/app/z_tail.py b/app/z_tail.py\n+x\n"
    return head + "+" + "y" * (total - len(head) - len(tail) - 1) + tail


def test_full_review_diff_keeps_everything_above_git_diff_cap_and_reports_not_truncated():
    big = _big_diff(_MAX_DIFF_CHARS * 3)
    job = _job()
    job._ssh_command = AsyncMock(side_effect=[big, ""])
    full, truncated = asyncio.run(job._collect_full_review_diff("abc123"))
    assert full == big and truncated is False
    assert "app/z_tail.py" in full[_MAX_DIFF_CHARS:]
    cmds = [c.args[0] for c in job._ssh_command.await_args_list]
    assert cmds[0].startswith("git diff abc123 | head -c ")
    assert all(c.kwargs["max_output_chars"] >= _MAX_REVIEW_DIFF_CHARS for c in job._ssh_command.await_args_list)


def test_full_review_diff_over_cap_is_flagged_truncated():
    job = _job()
    job._ssh_command = AsyncMock(side_effect=[_big_diff(_MAX_REVIEW_DIFF_CHARS + 5000), ""])
    full, truncated = asyncio.run(job._collect_full_review_diff(""))
    assert truncated is True and len(full) == _MAX_REVIEW_DIFF_CHARS


def test_multibyte_diff_reaching_byte_cap_is_still_flagged_truncated():
    # 한글 3바이트 문자: 문자 수는 상한 미만이어도 head -c 가 바이트에서 잘랐으면 절단이다.
    text = "diff --git a/app/a.py b/app/a.py\n+" + "가" * (_MAX_REVIEW_DIFF_CHARS // 3 + 10)
    job = _job()
    job._ssh_command = AsyncMock(side_effect=[text, ""])
    full, truncated = asyncio.run(job._collect_full_review_diff(""))
    assert len(full) < _MAX_REVIEW_DIFF_CHARS and truncated is True


def test_refresh_keeps_git_diff_meaning_and_records_full_diff():
    big = _big_diff(_MAX_DIFF_CHARS * 2)
    job = _job(git_diff=big[:_MAX_DIFF_CHARS])
    job._ssh_command = AsyncMock(side_effect=[big, ""])
    asyncio.run(job._refresh_review_diff("abc"))
    assert len(job.git_diff) == _MAX_DIFF_CHARS
    assert job.review_diff == big and job.review_diff_truncated is False
    assert job.review_diff_original_chars == len(big)


def test_refresh_failure_falls_back_without_blocking_and_reports_cap_hit_honestly():
    job = _job(git_diff="d" * _MAX_DIFF_CHARS)
    job._ssh_command = AsyncMock(side_effect=RuntimeError("ssh down"))
    asyncio.run(job._refresh_review_diff(""))
    assert job.review_diff == ""
    assert job.review_diff_truncated is True


def test_preservation_audit_scope_uses_full_diff_not_the_cut_one():
    big = _big_diff(_MAX_DIFF_CHARS * 2)
    job = _job(git_diff=big[:_MAX_DIFF_CHARS], review_diff=big, review_diff_original_chars=len(big))
    job._trace_runner = AsyncMock()
    asyncio.run(job._trace_preservation_audit({"verdict": "PASS", "summary": "ok"}))
    metadata = job._trace_runner.await_args.kwargs["metadata"]
    assert "app/z_tail.py" in metadata["changed_files"]
    assert metadata["out_of_scope_files"] == ["app/z_tail.py"]
    assert metadata["diff_original_chars"] == len(big)
