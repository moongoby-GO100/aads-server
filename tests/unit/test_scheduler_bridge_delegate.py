"""MCP 브리지의 schedule_task/unschedule_task/list_scheduled_tasks 는 API 프로세스에 위임한다.

브리지가 만든 메모리 스케줄러는 세션 종료와 함께 예약을 잃었다(persisted=false 로 "등록됨" 처럼 보임).
"""
import asyncio
import sys
import types

import httpx
import pytest
from fastapi import FastAPI

from app.api import ceo_chat_tools_scheduler as sched_tools
from app.api import internal_scheduler
from app.auth import require_internal_admin

SESSION = "11111111-1111-1111-1111-111111111111"
OTHER_SESSION = "22222222-2222-2222-2222-222222222222"


class _Job:
    def __init__(self, job_id, args, store):
        self.id = job_id
        self.args = args
        self.trigger = "fake"
        self.next_run_time = None
        self._jobstore_alias = store


class _ApiScheduler:
    """API 프로세스의 AsyncIOScheduler 대역. add_job 호출을 기록한다."""

    def __init__(self):
        self.calls = []
        self.jobs = {}

    def add_job(self, func, *trigger_args, **kwargs):
        self.calls.append({"func": func, **kwargs})
        self.jobs[kwargs["id"]] = _Job(kwargs["id"], kwargs["args"], kwargs.get("jobstore", "default"))

    def get_job(self, job_id, jobstore=None):
        return self.jobs.get(job_id)

    def get_jobs(self, jobstore=None):
        return [j for j in self.jobs.values() if jobstore is None or j._jobstore_alias == jobstore]

    def remove_job(self, job_id, jobstore=None):
        del self.jobs[job_id]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api_app():
    app = FastAPI()
    app.include_router(internal_scheduler.router, prefix="/api/v1")
    app.dependency_overrides[require_internal_admin] = lambda: {"is_internal_admin": True}
    app.state.scheduler = _ApiScheduler()
    return app


@pytest.fixture
def bridge(monkeypatch, api_app):
    """스케줄러 없는 브리지 컨텍스트 + 위임 호출을 in-process API 앱으로 연결."""
    monkeypatch.setattr(sched_tools, "_scheduler", None)
    monkeypatch.setitem(
        sys.modules, "app.main", types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
    )
    monkeypatch.setenv("AADS_MONITOR_KEY", "test-monitor-key")
    monkeypatch.setenv("AADS_API_BASE", "http://api.test")
    monkeypatch.delenv("AADS_SESSION_ID", raising=False)

    seen = []
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        async def _record(request: httpx.Request):
            seen.append(request)

        return real_client(
            transport=httpx.ASGITransport(app=api_app),
            timeout=kwargs.get("timeout"),
            event_hooks={"request": [_record]},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return types.SimpleNamespace(app=api_app, scheduler=api_app.state.scheduler, seen=seen)


def _schedule(**over):
    kwargs = dict(
        name="strategy card report",
        schedule_type="cron",
        action_type="url_check",
        action_config={"url": "http://x", "project": "GO100"},
        schedule_config={"hour": 9, "minute": 0},
        report_session_id=SESSION,
    )
    kwargs.update(over)
    return _run(sched_tools.schedule_task(**kwargs))


def test_bridge_schedule_delegates_to_api_persistent_store(bridge):
    result = _schedule()

    assert result["status"] == "registered"
    assert result["persisted"] is True
    assert result["delegated"] is True
    assert result["job_id"] == "user_strategy_card_report"
    assert len(bridge.scheduler.calls) == 1
    call = bridge.scheduler.calls[0]
    assert call["jobstore"] == "persistent"
    assert call["id"] == "user_strategy_card_report"
    assert call["func"] is sched_tools._execute_scheduled_job
    assert call["replace_existing"] is False
    assert call["args"][2]["report_session_id"] == SESSION
    assert call["args"][2]["project"] == "GO100"
    req = bridge.seen[0]
    assert req.url.path == "/api/v1/internal/scheduler/schedule"
    assert req.headers["x-monitor-key"] == "test-monitor-key"


def test_bridge_never_registers_in_a_local_scheduler(bridge):
    _schedule()
    assert sched_tools._scheduler is None


def test_bridge_session_falls_back_to_env_session(bridge, monkeypatch):
    monkeypatch.setenv("AADS_SESSION_ID", SESSION)
    result = _schedule(report_session_id="", action_config={"url": "http://x"})
    assert result["report_session_id"] == SESSION
    assert bridge.scheduler.calls[0]["args"][2]["report_session_id"] == SESSION


def test_reregistering_same_request_from_same_session_replaces(bridge):
    assert _schedule()["persisted"] is True
    again = _schedule()

    assert again["status"] == "registered"
    assert [c["replace_existing"] for c in bridge.scheduler.calls] == [False, True]
    assert len(bridge.scheduler.jobs) == 1


def test_same_name_from_other_session_is_not_overwritten(bridge):
    _schedule()
    other = _schedule(report_session_id=OTHER_SESSION)

    assert "error" in other
    assert len(bridge.scheduler.calls) == 1
    assert bridge.scheduler.jobs["user_strategy_card_report"].args[2]["report_session_id"] == SESSION


def test_sessionless_duplicate_is_not_replaced(bridge):
    _schedule(report_session_id="")
    again = _schedule(report_session_id="")
    assert "error" in again
    assert len(bridge.scheduler.calls) == 1


def test_bridge_unschedule_and_list_delegate(bridge):
    _schedule()

    listed = _run(sched_tools.list_scheduled_tasks())
    assert listed["delegated"] is True
    assert [j["job_id"] for j in listed["jobs"]] == ["user_strategy_card_report"]
    assert listed["jobs"][0]["persisted"] is True

    removed = _run(sched_tools.unschedule_task("strategy card report"))
    assert removed["status"] == "removed"
    assert removed["delegated"] is True
    assert bridge.scheduler.jobs == {}

    missing = _run(sched_tools.unschedule_task("strategy card report"))
    assert "error" in missing


def test_delegate_unreachable_reports_not_persisted_and_does_not_fall_back(bridge, monkeypatch):
    def _boom(*a, **k):
        class _C:
            async def __aenter__(self):
                raise httpx.ConnectError("connection refused")

            async def __aexit__(self, *exc):
                return False

        return _C()

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    result = _schedule()

    assert result["persisted"] is False
    assert "error" in result
    assert "status" not in result
    assert "scheduler_delegate_failed" in result["persisted_reason"]
    assert sched_tools._scheduler is None
    assert bridge.scheduler.calls == []


def test_delegate_http_error_reports_not_persisted(bridge):
    bridge.app.state.scheduler = None
    result = _schedule()
    assert result["persisted"] is False
    assert "http_503" in result["persisted_reason"]

    assert "error" in _run(sched_tools.list_scheduled_tasks())
    assert "error" in _run(sched_tools.unschedule_task("x"))


def test_delegate_without_monitor_key_fails_loudly(bridge, monkeypatch):
    monkeypatch.delenv("AADS_MONITOR_KEY", raising=False)
    result = _schedule()
    assert result["persisted"] is False
    assert "AADS_MONITOR_KEY_missing" in result["persisted_reason"]
    assert bridge.seen == []


def test_api_context_still_runs_in_process_without_http(monkeypatch, bridge):
    """API 프로세스(스케줄러 있음): 위임 없이 기존 동작 — replace_existing=False, 중복은 오류."""
    api_sched = _ApiScheduler()
    monkeypatch.setattr(sched_tools, "_scheduler", api_sched)

    first = _schedule()
    assert first["persisted"] is True
    assert "delegated" not in first
    assert api_sched.calls[0]["replace_existing"] is False
    assert api_sched.calls[0]["jobstore"] == "persistent"
    assert bridge.seen == []

    dup = _schedule()
    assert "error" in dup
    assert len(api_sched.calls) == 1

    assert _run(sched_tools.list_scheduled_tasks())["user_jobs"] == 1
    assert _run(sched_tools.unschedule_task("strategy card report"))["status"] == "removed"
    assert bridge.seen == []


def test_endpoint_requires_internal_admin():
    assert any(d.dependency is require_internal_admin for d in internal_scheduler.router.dependencies)


def test_public_pipeline_constant_does_not_grant_scheduler_access():
    """internal-pipeline-call 상수는 /pipeline/ 경로에서만 통한다 — 이 경로는 열리지 않아야 한다."""
    import inspect

    import app.auth as auth_mod

    src = inspect.getsource(auth_mod.get_current_user)
    assert "monitor_key == 'internal-pipeline-call'" in src
    assert "/pipeline/" in src
