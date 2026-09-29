"""Regression coverage for rejecting a Pipeline Runner job without cleaning its shared workdir."""

import asyncio

import pytest

from app.services.pipeline_runner_service import PipelineCJob


def _job(job_id="runner-test1234"):
    job = PipelineCJob.__new__(PipelineCJob)
    job.job_id = job_id
    job.status = "awaiting_approval"
    job.review_feedback = ""
    job.logs = []
    job.phase = "awaiting_approval"
    job.cycle = 0
    job.workdir = "/root/kis-autotrade-v4"
    return job


def test_reject_without_worktree_skips_git_mutation(monkeypatch):
    job = _job()
    commands = []
    posted = []

    async def ssh(command, **kwargs):
        commands.append(command)
        return "REJECT_NO_WORKTREE"

    async def save():
        return None

    async def post(content, **kwargs):
        posted.append(content)

    monkeypatch.setattr(job, "_ssh_command", ssh)
    monkeypatch.setattr(job, "_save_to_db", save)
    monkeypatch.setattr(job, "_post_to_chat", post)

    result = asyncio.run(job._reject_inner("no"))

    joined = "\n".join(commands)
    assert "git checkout" not in joined
    assert "git clean" not in joined
    assert job.status == "done"
    assert job.review_feedback == "REJECTED: no"
    assert result["status"] == "rejected"
    assert "공유 작업트리는 건드리지 않았습니다" in posted[0]


def test_reject_with_worktree_targets_only_job_worktree(monkeypatch):
    job = _job("runner-own123")
    commands = []

    async def ssh(command, **kwargs):
        commands.append(command)
        return "REJECT_WORKTREE_REMOVED"

    async def save():
        return None

    async def post(content, **kwargs):
        return None

    monkeypatch.setattr(job, "_ssh_command", ssh)
    monkeypatch.setattr(job, "_save_to_db", save)
    monkeypatch.setattr(job, "_post_to_chat", post)

    result = asyncio.run(job._reject_inner("no"))

    joined = "\n".join(commands)
    assert "git worktree remove --force /tmp/aads-wt-runner-own123" in joined
    assert "git clean -fd" not in joined
    assert "git checkout" not in joined
    assert result["message"] == "변경사항이 원복되었습니다."


def test_reject_cleanup_failure_does_not_mark_job_done(monkeypatch):
    job = _job()

    async def ssh(command, **kwargs):
        return "REJECT_CLEANUP_ERROR"

    async def forbidden_save():
        pytest.fail("cleanup failure must not persist a successful rejection")

    monkeypatch.setattr(job, "_ssh_command", ssh)
    monkeypatch.setattr(job, "_save_to_db", forbidden_save)
    monkeypatch.setattr(job, "_post_to_chat", lambda *args, **kwargs: None)

    result = asyncio.run(job._reject_inner("no"))

    assert "error" in result
    assert job.status == "awaiting_approval"
    assert job.review_feedback == ""
