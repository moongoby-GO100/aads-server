"""db_safe_write project 라우팅 + 승인 게이트 엄격도 유지 회귀 테스트.

2026-10-01: GO100 accounts UPDATE 가 AADS DB 에 고정돼 `relation "accounts" does not exist`
로 실패했다. project 라우팅을 추가하되, (1) 지원 외 project 는 AADS 로 폴백하지 않고 차단,
(2) 가드 체인은 AADS 경로와 동일, (3) live_trading_guard 는 db_safe_write 를 계속
프로젝트 미상으로 취급해 goal/project 자동승인이 걸리지 않아야 한다.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import live_trading_guard as guard
from app.services.tool_executor import ToolExecutor

GO100_CONFIG = {"host": "h", "port": "5432", "database": "kisautotrade", "user": "u", "password": "p"}
UPDATE_SQL = "UPDATE accounts SET active = false WHERE id = 1"


class _Row:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Stmt:
    def __init__(self, conn, sql):
        self._conn, self._sql = conn, sql

    async def fetch(self, *args):
        self._conn.executed.append((self._sql, args))
        return []

    def get_statusmsg(self):
        return "UPDATE 8"


class _Conn:
    def __init__(self, pre_count=8):
        self._pre = pre_count
        self.executed: list = []
        self.tx_entered = 0

    async def fetchrow(self, query, *args):
        return _Row({"cnt": self._pre})

    async def fetch(self, query, *args):
        return []  # 뷰 조회 — 뷰 없음

    def transaction(self):
        self.tx_entered += 1
        return _Tx()

    async def execute(self, query, *args):
        return "OK"

    async def prepare(self, sql):
        return _Stmt(self, sql)


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _borrow_factory(conn, calls):
    @asynccontextmanager
    async def _borrow(project):
        calls.append(project)
        yield _Pool(conn)

    return _borrow


def _aads_pool_must_not_be_used():
    return patch("app.core.db_pool.get_pool", side_effect=AssertionError("AADS 풀로 폴백됨"))


@pytest.mark.asyncio
async def test_no_project_uses_aads_pool():
    conn = _Conn()
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)) as get_pool:
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL})
    get_pool.assert_called_once()
    assert result["success"] is True
    assert result["project"] == "AADS"


@pytest.mark.asyncio
async def test_explicit_aads_uses_aads_pool():
    conn = _Conn()
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)) as get_pool:
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": "aads"})
    get_pool.assert_called_once()
    assert result["project"] == "AADS"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expected_alias", [("GO100", "GO100"), ("go100", "GO100"), ("KIS", "GO100")])
async def test_go100_and_kis_route_to_project_pool(name, expected_alias):
    conn = _Conn()
    calls: list = []
    with (
        patch("app.api.ceo_chat_tools_db._get_project_db_config", return_value=GO100_CONFIG),
        patch("app.api.ceo_chat_tools_db._borrow_pg_pool", _borrow_factory(conn, calls)),
        _aads_pool_must_not_be_used() as get_pool,
    ):
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": name})
    assert calls == [expected_alias]  # KIS 는 _PROJECT_ALIAS 로 GO100 해석
    get_pool.assert_not_called()
    assert result["success"] is True
    assert result["project"] == "GO100"
    assert result["table"] == "accounts"
    assert conn.tx_entered == 1  # 트랜잭션 강제
    assert conn.executed and conn.executed[0][0] == UPDATE_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["SF", "NTV2", "ACCT", "NOPE", "sf", "AADS2"])
async def test_unsupported_project_is_blocked_without_aads_fallback(name):
    borrow = MagicMock()
    with (
        patch("app.api.ceo_chat_tools_db._borrow_pg_pool", borrow),
        _aads_pool_must_not_be_used() as get_pool,
    ):
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": name})
    get_pool.assert_not_called()
    borrow.assert_not_called()
    assert result["blocked"] is True
    assert result["project"] == name
    assert "error" in result and "success" not in result


@pytest.mark.asyncio
async def test_non_string_project_is_blocked():
    with _aads_pool_must_not_be_used() as get_pool:
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": ["GO100"]})
    get_pool.assert_not_called()
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_missing_project_config_is_blocked_without_fallback():
    with (
        patch("app.api.ceo_chat_tools_db._get_project_db_config", return_value=None),
        _aads_pool_must_not_be_used() as get_pool,
    ):
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": "GO100"})
    get_pool.assert_not_called()
    assert result["blocked"] is True
    assert result["project"] == "GO100"


@pytest.mark.asyncio
async def test_pool_failure_does_not_fall_back_to_aads():
    @asynccontextmanager
    async def _boom(project):
        raise ValueError("프로젝트 GO100 DB 설정 없음")
        yield  # pragma: no cover

    with (
        patch("app.api.ceo_chat_tools_db._get_project_db_config", return_value=GO100_CONFIG),
        patch("app.api.ceo_chat_tools_db._borrow_pg_pool", _boom),
        _aads_pool_must_not_be_used() as get_pool,
    ):
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": "GO100"})
    get_pool.assert_not_called()
    assert "error" in result and "success" not in result
    assert result["project"] == "GO100"


@pytest.mark.asyncio
async def test_go100_dry_run_does_not_execute():
    conn = _Conn(pre_count=8)
    with (
        patch("app.api.ceo_chat_tools_db._get_project_db_config", return_value=GO100_CONFIG),
        patch("app.api.ceo_chat_tools_db._borrow_pg_pool", _borrow_factory(conn, [])),
        _aads_pool_must_not_be_used(),
    ):
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": "GO100", "dry_run": True})
    assert result["dry_run"] is True
    assert result["project"] == "GO100"
    assert result["pre_count"] == 8
    assert conn.executed == []
    assert conn.tx_entered == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [
    "DROP TABLE accounts",
    "UPDATE accounts SET a = 1; DELETE FROM accounts",
    "UPDATE accounts SET a = 1; DROP TABLE x",
    "TRUNCATE accounts",
])
async def test_go100_guards_still_block_ddl_and_multi_statement(sql):
    conn = _Conn()
    with (
        patch("app.api.ceo_chat_tools_db._get_project_db_config", return_value=GO100_CONFIG),
        patch("app.api.ceo_chat_tools_db._borrow_pg_pool", _borrow_factory(conn, [])),
        _aads_pool_must_not_be_used(),
    ):
        result = await ToolExecutor()._db_safe_write({"sql": sql, "project": "GO100"})
    assert "error" in result and "success" not in result
    assert conn.executed == []


@pytest.mark.asyncio
async def test_go100_still_runs_vault_view_guard():
    """가드 체인 보존: 쓰기 대상이 뷰이면 GO100 경로에서도 차단된다."""
    from app.services.vault_cleanup_guard import VaultLoginProtected

    conn = _Conn()
    guard_mock = AsyncMock(side_effect=VaultLoginProtected("view blocked"))
    with (
        patch("app.api.ceo_chat_tools_db._get_project_db_config", return_value=GO100_CONFIG),
        patch("app.api.ceo_chat_tools_db._borrow_pg_pool", _borrow_factory(conn, [])),
        patch("app.services.vault_cleanup_guard.assert_target_not_view", guard_mock),
        _aads_pool_must_not_be_used(),
    ):
        result = await ToolExecutor()._db_safe_write({"sql": UPDATE_SQL, "project": "GO100"})
    guard_mock.assert_awaited_once()
    assert result["blocked"] is True
    assert result["project"] == "GO100"
    assert conn.executed == []


def test_registry_schema_has_optional_project():
    from app.services.tool_registry import _TOOLS

    schema = _TOOLS["db_safe_write"]["input_schema"]
    assert schema["properties"]["project"]["type"] == "string"
    assert "project" not in schema["required"]


# ── 승인 게이트 엄격도 유지 ────────────────────────────────────────────────


def test_approval_project_ignores_db_safe_write_project():
    for proj in ("GO100", "KIS", "AADS", ""):
        assert guard._approval_project("db_safe_write", {"project": proj, "sql": "x"}) == ""
    # 다른 도구는 그대로 project 를 본다.
    assert guard._approval_project("patch_remote_file", {"project": "go100"}) == "GO100"


class _RecPool:
    def __init__(self):
        self.fetchrow_args = None

    async def fetchrow(self, query, *args):
        self.fetchrow_args = args
        return None

    async def execute(self, *a, **k):
        return "OK"


@pytest.mark.asyncio
async def test_goal_policy_never_gets_project_for_db_safe_write():
    pool = _RecPool()
    with patch("app.core.db_pool.get_pool", return_value=pool):
        out = await guard.goal_policy_allows(
            "sess-1", "db_safe_write", {"project": "GO100", "sql": UPDATE_SQL}, "high",
        )
    assert out is None
    assert pool.fetchrow_args[2] == ""  # $3 = '' → `$3 <> ''` 로 자동승인 불가


@pytest.mark.asyncio
async def test_goal_policy_still_uses_project_for_other_tools():
    pool = _RecPool()
    with patch("app.core.db_pool.get_pool", return_value=pool):
        await guard.goal_policy_allows("sess-1", "patch_remote_file", {"project": "go100"}, "high")
    assert pool.fetchrow_args[2] == "GO100"


@pytest.mark.asyncio
async def test_is_approved_project_scope_and_wide_scopes_stay_closed_for_go100_write():
    pool = _RecPool()
    with (
        patch("app.core.db_pool.get_pool", return_value=pool),
        patch.object(guard, "_active_goal_ids", AsyncMock(return_value=["goal-1"])),
    ):
        ok = await guard.is_approved(
            "db_safe_write", {"project": "GO100", "sql": UPDATE_SQL}, "sess-1", "high",
        )
    assert ok is False
    work_key, session_id, tool, goal_ids, target_key, wide_ok, target_project, _fp = pool.fetchrow_args
    assert target_project == ""  # $7 = '' → project 범위 불가
    assert wide_ok is False      # session 범위 불가
    assert goal_ids == [""]      # goal 범위 불가


@pytest.mark.asyncio
async def test_is_approved_aads_default_path_unchanged():
    pool = _RecPool()
    with (
        patch("app.core.db_pool.get_pool", return_value=pool),
        patch.object(guard, "_active_goal_ids", AsyncMock(return_value=["goal-1"])),
    ):
        await guard.is_approved("db_safe_write", {"sql": UPDATE_SQL}, "sess-1", "high")
    _wk, _s, _t, goal_ids, _tk, wide_ok, target_project, _fp = pool.fetchrow_args
    assert wide_ok is True and goal_ids == ["goal-1"] and target_project == ""


@pytest.mark.asyncio
async def test_request_approval_records_empty_project_for_db_safe_write():
    import json

    class _P:
        def __init__(self):
            self.scope_json = None

        async def fetchval(self, query, *args):
            if "INSERT INTO agent_permission_requests" in query:
                self.scope_json = args[5]
                return "req-1"
            if "chat_sessions" in query:
                return "00000000-0000-0000-0000-000000000001"
            return None

    pool = _P()
    with (
        patch("app.core.db_pool.get_pool", return_value=pool),
        patch("app.services.next_step_proposals._current_bubble_id", AsyncMock(return_value=None)),
    ):
        rid = await guard.request_approval(
            "db_safe_write", {"project": "GO100", "sql": UPDATE_SQL}, "reason",
            session_id="00000000-0000-0000-0000-0000000000aa",
            tenant_id="00000000-0000-0000-0000-000000000001",
        )
    assert rid == "req-1"
    assert json.loads(pool.scope_json)["project"] == ""
