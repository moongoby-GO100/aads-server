"""원복성 예약을 SELECT 전용 db_query 로 등록하는 것을 등록 단계에서 거부한다."""
import asyncio

import pytest

from app.api import ceo_chat_tools_scheduler as sched_tools


class _FakeJob:
    def __init__(self, job_id):
        self.id = job_id
        self.trigger = "fake"
        self.next_run_time = None


class _FakeScheduler:
    def __init__(self):
        self.calls = []
        self.jobs = {}

    def add_job(self, func, *trigger_args, **kwargs):
        self.calls.append(kwargs)
        self.jobs[kwargs["id"]] = kwargs.get("jobstore", "default")

    def get_job(self, job_id, jobstore=None):
        return _FakeJob(job_id) if job_id in self.jobs else None

    def get_jobs(self, jobstore=None):
        return [_FakeJob(j) for j in self.jobs]


@pytest.fixture
def sched(monkeypatch):
    s = _FakeScheduler()
    monkeypatch.setattr(sched_tools, "_scheduler", s)
    yield s
    monkeypatch.setattr(sched_tools, "_scheduler", None)


def _register(name, action_type, action_config):
    return asyncio.run(sched_tools.schedule_task(
        name=name,
        schedule_type="once",
        action_type=action_type,
        action_config=action_config,
        schedule_config={"delay_minutes": 5},
    ))


SELECT_Q = "SELECT * FROM runner_host_policy"


def test_rejects_restore_named_db_query(sched):
    r = _register("go100-ops-runner-hold-release-1531", "db_query",
                  {"project": "AADS", "query": SELECT_Q})
    assert "error" in r
    assert "SELECT 전용" in r["error"]
    assert "ops.scheduled_restore_is_readonly_db_query" in r["error"]
    assert sched.calls == []


def test_rejects_restore_keyword_in_query(sched):
    r = _register("contabo14-later", "db_query",
                  {"project": "AADS", "query": "SELECT 1 -- 원복"})
    assert "error" in r
    assert sched.calls == []


@pytest.mark.parametrize(
    "kw", ["restore", "RELEASE", "revert", "rollback", "unpin", "unhold", "복구", "해제"]
)
def test_rejects_each_keyword_case_insensitive(sched, kw):
    r = _register(f"job-{kw}-x", "db_query", {"query": SELECT_Q})
    assert "error" in r


def test_same_name_remote_command_allowed(sched):
    r = _register("go100-ops-runner-hold-release-1531", "remote_command",
                  {"project": "AADS", "command": "echo ok"})
    assert r["status"] == "registered"


def test_recheck_select_allowed(sched):
    r = _register("go100-ops-runner-cap-release-psi-recheck", "db_query",
                  {"project": "AADS", "query": SELECT_Q})
    assert r["status"] == "registered"


def test_recheck_with_write_still_rejected(sched):
    r = _register("go100-ops-runner-cap-release-psi-recheck", "db_query",
                  {"project": "AADS", "query": "DELETE FROM runner_host_policy"})
    assert "error" in r


def test_plain_db_query_allowed(sched):
    r = _register("daily-user-count", "db_query",
                  {"project": "AADS", "query": "SELECT count(*) FROM users"})
    assert r["status"] == "registered"
