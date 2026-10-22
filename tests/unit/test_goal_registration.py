"""Chat hierarchy validation, atomic registration and SQL bypass regression tests."""

from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services.db_write_sql_guard import (
    SqlGuardError,
    enforce_chat_goal_registration,
    validate_single_write,
)
from app.services.goal_owner_resolver import _SESSION_SQL
from app.services.goal_registration import register_goal

TENANT, SESSION = str(uuid4()), str(uuid4())


def payload():
    return {
        "goal": {
            "project": "AADS",
            "title": "목표 등록",
            "description": "상세 설명",
            "success_criteria": "검증 통과",
            "priority": "P1",
        },
        "milestones": [
            {
                "title": f"단계 {i}",
                "description": "상세 단계",
                "completion_criteria": "완료 확인",
                "sequence_order": i,
                "auto_advance": False,
                "epics": [
                    {
                        "title": "에픽",
                        "acceptance_criteria": ["검증"],
                        "stories": [
                            {
                                "title": "스토리",
                                "acceptance_criteria": ["검증"],
                                "tasks": [{"title": "업무", "acceptance_criteria": ["검증"]}],
                            }
                        ],
                    }
                ],
            }
            for i in range(3)
        ],
    }


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda p: p["milestones"].pop(), "milestones"),
        (lambda p: p["milestones"][0]["epics"][0].update(stories=[]), "stories"),
        (
            lambda p: p["milestones"][0]["epics"][0].update(acceptance_criteria=[]),
            "acceptance_criteria",
        ),
        (lambda p: p["milestones"][0].update(completion_criteria="  "), "completion_criteria"),
        (lambda p: p["milestones"][0]["epics"][0]["stories"][0].update(tasks=[]), "tasks"),
        (lambda p: p["goal"].update(title="English only"), "title"),
    ],
)
@pytest.mark.asyncio
async def test_missing_hierarchy_never_opens_transaction(mutation, expected):
    p = payload()
    mutation(p)
    conn = AsyncMock()
    result = await register_goal(conn, p, tenant_id=TENANT, session_id=SESSION)
    assert result["error"] == "invalid_goal_hierarchy"
    assert any(expected in e for e in result["errors"])
    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()


class MemoryConnection:
    """Transactional SQL adapter exercising the real create_work_item service."""

    def __init__(self, fail_at=0):
        self.goals, self.milestones, self.items, self.links = {}, {}, {}, []
        self.fail_at = fail_at
        self.keys = []

    @asynccontextmanager
    async def transaction(self):
        snapshot = deepcopy((self.goals, self.milestones, self.items, self.links))
        try:
            yield
        except Exception:
            self.goals, self.milestones, self.items, self.links = snapshot
            raise

    async def execute(self, sql, *a):
        if "INSERT INTO goals" in sql:
            self.goals[a[0]] = {
                "id": a[0],
                "tenant_id": a[1],
                "project": a[2],
                "title": a[3],
                "status": a[10],
                "owner_role_key": a[8],
                "owner_session_id": a[9],
                "version": 1,
            }
        elif "INSERT INTO milestones" in sql:
            self.milestones[a[0]] = {
                "id": a[0],
                "status": "pending",
                "started_at": None,
                "owner_role_key": a[9],
                "owner_session_id": a[10],
            }
        elif "UPDATE milestones SET status = 'in_progress'" in sql:
            self.milestones[a[0]].update(status="in_progress", started_at="now")
        elif "INSERT INTO goal_task_links" in sql:
            self.links.append(a)

    async def fetch(self, sql, *a):
        return []

    async def fetchrow(self, sql, *a):
        if sql == _SESSION_SQL:
            return {"session_id": a[0], "role_key": "dev", "title": "t", "project_key": "AADS"}
        if "workspace_name" in sql:
            return {
                "session_id": SESSION,
                "role_key": "dev",
                "project": "AADS",
                "workspace_name": "AADS",
                "display_name": "AADS",
                "user_id": "user",
            }
        if "FROM chat_sessions" in sql:
            return {"user_id": "user"}
        if "FROM goals" in sql:
            return self.goals.get(a[0])
        if "FROM milestones" in sql:
            return self.milestones.get(a[0])
        if "FROM work_items" in sql:
            return self.items.get(a[0]) if "WHERE id=" in sql else None
        if "INSERT INTO work_items" in sql:
            if self.fail_at and len(self.items) + 1 == self.fail_at:
                raise RuntimeError("injected work item failure")
            item = {"id": str(uuid4()), "type": a[5], "milestone_id": a[3], "parent_id": a[4]}
            self.items[item["id"]] = item
            self.keys.append(a[11])
            return item
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        assert "FROM pipeline_jobs" in sql
        return 1


@pytest.mark.asyncio
async def test_registration_counts_and_links():
    conn = MemoryConnection()
    p = payload()
    p["linked_jobs"] = [{"milestone_index": 1, "job_id": "runner-job"}]
    result = await register_goal(conn, p, tenant_id=TENANT, session_id=SESSION)
    assert {k: v for k, v in result.items() if k != "goal_id"} == {
        "milestone_count": 3,
        "epic_count": 3,
        "story_count": 3,
        "task_count": 3,
    }
    assert len(conn.goals) == 1 and len(conn.items) == 9
    assert len(set(conn.keys)) == 9
    assert all(key.startswith(f"goal-register:{result['goal_id']}:") for key in conn.keys)
    assert conn.links[0][3:] == ("chat_session", SESSION)
    assert conn.links[1][1] == list(conn.milestones)[1]
    assert conn.links[1][3:] == ("pipeline_job", "runner-job")


@pytest.mark.asyncio
async def test_registration_records_owner_role_and_executor_session():
    conn = MemoryConnection()
    p = payload()
    p["goal"]["owner_role_key"] = "dev"
    p["goal"]["owner_session_id"] = str(uuid4())
    result = await register_goal(conn, p, tenant_id=TENANT, session_id=SESSION)
    goal = conn.goals[result["goal_id"]]
    assert goal["owner_role_key"] == "dev"
    assert goal["owner_session_id"] == SESSION
    assert len(conn.milestones) == 3
    assert all(ms["owner_role_key"] == "dev" for ms in conn.milestones.values())
    assert all(ms["owner_session_id"] == SESSION for ms in conn.milestones.values())


@pytest.mark.asyncio
async def test_middle_failure_rolls_back_every_table():
    conn = MemoryConnection(fail_at=5)
    with pytest.raises(RuntimeError, match="injected"):
        await register_goal(conn, payload(), tenant_id=TENANT, session_id=SESSION)
    assert not conn.goals and not conn.milestones and not conn.items and not conn.links


@pytest.mark.asyncio
async def test_p0_cap_no_writes(monkeypatch):
    from app.services import goal_signals

    monkeypatch.setattr(goal_signals, "active_p0_goals", AsyncMock(return_value=[{}] * 3))
    p = payload()
    p["goal"]["priority"] = "P0-CRITICAL"
    conn = MemoryConnection()
    result = await register_goal(conn, p, tenant_id=TENANT, session_id=SESSION)
    assert result["error"] == "p0_cap_exceeded"
    assert not conn.goals


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO goals (title) VALUES ($1)",
        'INSERT INTO public."goals" (title) VALUES ($1)',
        'INSERT /* comment */ INTO "public"."goals" (title) VALUES ($1)',
    ],
)
def test_goal_insert_blocked(sql):
    with pytest.raises(SqlGuardError, match="goal_register"):
        enforce_chat_goal_registration(
            validate_single_write(sql), chat_session=True, project="AADS"
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE goals SET title=$1 WHERE id=$2",
        "INSERT INTO milestones (title) VALUES ($1)",
        "INSERT INTO work_items (title) VALUES ($1)",
    ],
)
def test_existing_goal_repairs_allowed(sql):
    enforce_chat_goal_registration(validate_single_write(sql), chat_session=True, project="AADS")


def test_non_chat_insert_unchanged():
    enforce_chat_goal_registration(
        validate_single_write("INSERT INTO goals (title) VALUES ($1)"),
        chat_session=False,
        project="AADS",
    )


@pytest.mark.asyncio
async def test_executor_blocks_before_connection(monkeypatch):
    from app.services import tool_executor as executor

    token = executor.current_chat_session_id.set(SESSION)
    try:
        result = await executor.ToolExecutor()._db_safe_write(
            {"sql": "INSERT INTO goals (title) VALUES ($1)", "params": ["목표"]}
        )
        assert result["blocked"] is True
        assert "goal_register" in result["error"]
    finally:
        executor.current_chat_session_id.reset(token)


@pytest.mark.asyncio
async def test_executor_goal_update_reaches_database(monkeypatch):
    from app.services import tool_executor as executor

    conn = AsyncMock()
    conn.fetchrow.return_value = {"cnt": 1}
    statement = Mock()
    statement.fetch = AsyncMock(return_value=[])
    statement.get_statusmsg.return_value = "UPDATE 1"
    conn.prepare.return_value = statement

    @asynccontextmanager
    async def transaction():
        yield

    conn.transaction = transaction

    @asynccontextmanager
    async def connection(project):
        yield conn

    monkeypatch.setattr(executor, "_db_safe_write_conn", connection)
    monkeypatch.setattr("app.services.vault_cleanup_guard.assert_target_not_view", AsyncMock())
    token = executor.current_chat_session_id.set(SESSION)
    try:
        result = await executor.ToolExecutor()._db_safe_write(
            {"sql": "UPDATE goals SET title=$1 WHERE id=$2", "params": ["목표 수정", str(uuid4())]}
        )
        assert result.get("success") is True, result
        conn.prepare.assert_awaited_once_with("UPDATE goals SET title=$1 WHERE id=$2")
        statement.fetch.assert_awaited_once()
    finally:
        executor.current_chat_session_id.reset(token)


def test_tool_available_to_chat_and_schema_is_complete():
    from app.services.tool_registry import _CORE_TOOLS, _DEFER_LOADING, _TOOLS

    assert "goal_register" in _CORE_TOOLS
    assert _DEFER_LOADING["goal_register"] is False
    schema = _TOOLS["goal_register"]["input_schema"]
    milestone = schema["properties"]["milestones"]
    assert milestone["minItems"] == 3
    work = milestone["items"]["properties"]["epics"]["items"]
    for children in ("stories", "tasks", None):
        assert "acceptance_criteria" in work["required"]
        if children:
            work = work["properties"][children]["items"]
