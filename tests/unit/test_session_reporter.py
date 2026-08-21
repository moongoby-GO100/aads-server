from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid


if "structlog" not in sys.modules:
    sys.modules["structlog"] = SimpleNamespace(get_logger=lambda *args, **kwargs: logging.getLogger("test"))
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = SimpleNamespace(Connection=object, Pool=object)


def _load_session_reporter():
    module_name = "_test_session_reporter_module"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).resolve().parents[2] / "app" / "services" / "session_reporter.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, *, session_exists: bool = True) -> None:
        self.session_exists = session_exists
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.message_id = uuid.uuid4()

    def transaction(self):
        return _Tx()

    async def fetchval(self, query: str, *args):
        self.queries.append((query, args))
        if "FROM chat_sessions" in query:
            return 1 if self.session_exists else None
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args):
        self.queries.append((query, args))
        if "INSERT INTO chat_messages" in query:
            return {"id": self.message_id}
        raise AssertionError(query)

    async def execute(self, query: str, *args):
        self.queries.append((query, args))
        return "UPDATE 1"


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    def get_job(self, job_id):
        return None

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


def test_build_session_report_content_sanitizes_tool_blocks():
    build_session_report_content = _load_session_reporter().build_session_report_content

    content = build_session_report_content(
        title="예약 점검",
        body="<function_results>secret</function_results>정상",
        status="done",
        source="schedule_task",
        project="GO100",
    )

    assert "[세션 자동보고]" in content
    assert "GO100" in content
    assert "정상" in content
    assert "secret" not in content
    assert "function_results" not in content


def test_post_session_report_inserts_assistant_message_and_updates_session():
    post_session_report = _load_session_reporter().post_session_report

    session_id = str(uuid.uuid4())
    conn = _FakeConn()

    result = asyncio.run(
        post_session_report(
            session_id=session_id,
            title="테스트",
            body="완료",
            source="unit_test",
            conn=conn,
        )
    )

    assert result.posted is True
    assert result.session_id == session_id
    assert result.message_id == str(conn.message_id)
    assert any("INSERT INTO chat_messages" in query for query, _ in conn.queries)
    assert any("UPDATE chat_sessions" in query for query, _ in conn.queries)


def test_post_session_report_skips_missing_session():
    post_session_report = _load_session_reporter().post_session_report

    result = asyncio.run(
        post_session_report(
            session_id=str(uuid.uuid4()),
            title="테스트",
            body="완료",
            conn=_FakeConn(session_exists=False),
        )
    )

    assert result.posted is False
    assert result.skipped_reason == "session_not_found"


def test_schedule_task_binds_report_session_id():
    from app.api import ceo_chat_tools_scheduler as scheduler_mod

    fake_scheduler = _FakeScheduler()
    scheduler_mod.set_scheduler(fake_scheduler)
    session_id = str(uuid.uuid4())

    result = asyncio.run(
        scheduler_mod.schedule_task(
            name="unit once",
            schedule_type="once",
            action_type="url_check",
            action_config={"url": "https://example.test/health"},
            schedule_config={"delay_minutes": 1},
            report_session_id=session_id,
        )
    )

    assert result["status"] == "registered"
    assert result["report_session_id"] == session_id
    assert fake_scheduler.jobs
    args = fake_scheduler.jobs[0]["args"]
    assert args[2]["report_session_id"] == session_id
    assert args[2]["report_to_session"] is True
