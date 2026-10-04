"""사용자 예약 작업(user_*) 영속화 — sync URL, persistent 등록, 실패 폴백."""
import asyncio
import datetime as dt

import pytest

from app.api import ceo_chat_tools_scheduler as sched_tools
from app.core.db_urls import to_sync_psycopg_url


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://u:p@db:5432/aads",
        "postgresql+asyncpg://u:p@db:5432/aads",
        "postgresql+psycopg2://u:p@db:5432/aads",
        "postgres://u:p@db:5432/aads",
        "postgresql+psycopg://u:p@db:5432/aads",
    ],
)
def test_sync_url_forced_to_psycopg(raw):
    assert to_sync_psycopg_url(raw) == "postgresql+psycopg://u:p@db:5432/aads"


def test_sync_url_keeps_query_string():
    out = to_sync_psycopg_url("postgresql+asyncpg://u:p@db/aads?sslmode=require")
    assert out == "postgresql+psycopg://u:p@db/aads?sslmode=require"


@pytest.mark.parametrize("bad", ["", "   ", "mysql://u:p@db/x", "sqlite:///x.db"])
def test_sync_url_rejects_without_leaking_value(bad):
    with pytest.raises(ValueError) as exc:
        to_sync_psycopg_url(bad)
    if bad.strip():
        assert bad not in str(exc.value)


class _FakeJob:
    def __init__(self, job_id):
        self.id = job_id
        self.trigger = "fake"
        self.next_run_time = dt.datetime(2026, 10, 8, 0, 0, tzinfo=dt.timezone.utc)


class _FakeScheduler:
    """add_job 호출 인자를 기록하는 모의 스케줄러."""

    def __init__(self, fail_persistent=False):
        self.calls = []
        self.jobs = {}
        self.fail_persistent = fail_persistent

    def add_job(self, func, *trigger_args, **kwargs):
        self.calls.append({"func": func, "trigger_args": trigger_args, **kwargs})
        if kwargs.get("jobstore") == "persistent" and self.fail_persistent:
            raise RuntimeError("db down")
        self.jobs[kwargs["id"]] = kwargs.get("jobstore", "default")

    def get_job(self, job_id, jobstore=None):
        return _FakeJob(job_id) if job_id in self.jobs else None

    def get_jobs(self, jobstore=None):
        return [
            _FakeJob(j) for j, store in self.jobs.items()
            if jobstore is None or store == jobstore
        ]


@pytest.fixture
def fake_sched(monkeypatch):
    def _make(**kw):
        sched = _FakeScheduler(**kw)
        monkeypatch.setattr(sched_tools, "_scheduler", sched)
        return sched
    yield _make
    monkeypatch.setattr(sched_tools, "_scheduler", None)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "schedule_type,schedule_config",
    [
        ("cron", {"hour": 9, "minute": 0}),
        ("interval", {"minutes": 5}),
        ("once", {"delay_minutes": 3}),
    ],
)
def test_schedule_task_uses_persistent_jobstore(fake_sched, schedule_type, schedule_config):
    sched = fake_sched()
    result = _run(sched_tools.schedule_task(
        name="canary revert",
        schedule_type=schedule_type,
        action_type="url_check",
        action_config={"url": "http://x"},
        schedule_config=schedule_config,
    ))

    assert result["status"] == "registered"
    assert result["persisted"] is True
    assert len(sched.calls) == 1
    call = sched.calls[0]
    assert call["jobstore"] == "persistent"
    assert call["replace_existing"] is False
    assert call["misfire_grace_time"] == 3600
    assert call["id"] == "user_canary_revert"
    assert call["func"] is sched_tools._execute_scheduled_job
    assert call["args"][0] == "user_canary_revert"


def test_schedule_task_normalizes_action_config_to_plain_values(fake_sched):
    sched = fake_sched()
    when = dt.datetime(2026, 10, 8)
    _run(sched_tools.schedule_task(
        name="norm",
        schedule_type="interval",
        action_type="url_check",
        action_config={"url": "http://x", "at": when},
        schedule_config={"minutes": 1},
    ))
    cfg = sched.calls[0]["args"][2]
    assert cfg["at"] == str(when)


def test_persistent_failure_falls_back_to_memory_and_reports(fake_sched):
    sched = fake_sched(fail_persistent=True)
    result = _run(sched_tools.schedule_task(
        name="fallback",
        schedule_type="interval",
        action_type="url_check",
        action_config={"url": "http://x"},
        schedule_config={"minutes": 1},
    ))

    assert result["status"] == "registered"
    assert result["persisted"] is False
    assert "db down" in result["persisted_reason"]
    assert [c.get("jobstore") for c in sched.calls] == ["persistent", None]
    assert sched.jobs["user_fallback"] == "default"


def test_no_scheduler_never_creates_memory_only_local_scheduler(monkeypatch):
    """브리지(스케줄러 없음)에서 메모리 BackgroundScheduler 를 만들지 않는다 — 위임만 한다."""
    import sys
    import types

    monkeypatch.setattr(sched_tools, "_scheduler", None)
    monkeypatch.setitem(
        sys.modules, "app.main", types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    )
    assert sched_tools._ensure_scheduler() is None
    assert sched_tools._scheduler is None


def test_list_reports_persisted_flag(fake_sched):
    sched = fake_sched()
    sched.jobs["user_a"] = "persistent"
    sched.jobs["user_b"] = "default"
    sched.jobs["alert_eval"] = "default"
    out = _run(sched_tools.list_scheduled_tasks())
    flags = {j["job_id"]: j["persisted"] for j in out["jobs"]}
    assert flags == {"user_a": True, "user_b": False, "alert_eval": False}


def test_persistent_jobs_survive_scheduler_restart_and_unschedule_deletes_row(tmp_path):
    pytest.importorskip("sqlalchemy")
    from apscheduler.schedulers.background import BackgroundScheduler
    from sqlalchemy import create_engine, text

    from app.core.persistent_jobstore import SlotGatedJobStore

    url = f"sqlite:///{tmp_path / 'jobs.db'}"

    def _make(active=True):
        store = SlotGatedJobStore(url=url, tablename="apscheduler_jobs", is_active=lambda: active)
        return BackgroundScheduler(jobstores={"persistent": store}), store

    first, _ = _make()
    first.start(paused=True)
    first.add_job(
        sched_tools._execute_scheduled_job_sync,
        "date",
        run_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
        args=["user_x", "url_check", {"url": "http://x"}],
        id="user_x",
        jobstore="persistent",
        replace_existing=False,
        misfire_grace_time=3600,
    )
    first.shutdown(wait=False)

    second, store = _make()
    second.start(paused=True)
    try:
        assert [j.id for j in second.get_jobs(jobstore="persistent")] == ["user_x"]
        assert store.get_next_run_time() is not None
        second.remove_job("user_x")
    finally:
        second.shutdown(wait=False)

    with create_engine(url).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM apscheduler_jobs")).scalar() == 0


def test_standby_slot_does_not_fire_persistent_jobs(tmp_path):
    pytest.importorskip("sqlalchemy")
    from apscheduler.job import Job
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.date import DateTrigger

    from app.core.persistent_jobstore import SlotGatedJobStore

    state = {"active": False}
    store = SlotGatedJobStore(
        url=f"sqlite:///{tmp_path / 'jobs.db'}",
        tablename="apscheduler_jobs",
        is_active=lambda: state["active"],
    )
    sched = BackgroundScheduler()
    store.start(sched, "persistent")
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    store.add_job(Job(
        sched,
        id="user_y",
        func=sched_tools._execute_scheduled_job_sync,
        trigger=DateTrigger(run_date=past),
        args=["user_y", "url_check", {}],
        kwargs={},
        executor="default",
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
        next_run_time=past,
    ))
    now = dt.datetime.now(dt.timezone.utc)
    assert store.get_due_jobs(now) == []
    assert store.get_next_run_time() is None
    state["active"] = True
    assert [j.id for j in store.get_due_jobs(now)] == ["user_y"]
    assert store.get_next_run_time() is not None
    store.shutdown()
