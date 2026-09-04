import pytest


@pytest.mark.asyncio
async def test_query_db_alias_dispatches_to_query_database(monkeypatch):
    from app.services.tool_executor import ToolExecutor

    async def fake_query_database(self, inp):
        return {"query": inp.get("sql") or inp.get("query")}

    monkeypatch.setattr(ToolExecutor, "_query_database", fake_query_database)

    result = await ToolExecutor()._dispatch("query_db", {"sql": "SELECT 1"})

    assert result == {"query": "SELECT 1"}


def test_database_tools_use_database_timeout_bucket(monkeypatch):
    from app.services import tool_executor

    monkeypatch.setattr(tool_executor, "_TOOL_TIMEOUT", 20.0)
    monkeypatch.setattr(tool_executor, "_LONG_TOOL_TIMEOUT", 55.0)
    monkeypatch.setattr(tool_executor, "_DATABASE_TOOL_TIMEOUT", 28.0)

    assert tool_executor._timeout_for_tool("query_database") == 28.0
    assert tool_executor._timeout_for_tool("query_db") == 28.0
    assert tool_executor._timeout_for_tool("query_project_database") == 28.0
    assert tool_executor._timeout_for_tool("run_remote_command") == 55.0
    assert tool_executor._timeout_for_tool("task_history") == 20.0


@pytest.mark.asyncio
async def test_tool_executor_timeout_payload_identifies_wrapper_layer(monkeypatch):
    from app.services import tool_executor
    from app.services.tool_executor import ToolExecutor

    async def slow_dispatch(self, tool_name, tool_input):
        await tool_executor.asyncio.sleep(0.03)
        return {"ok": True}

    monkeypatch.setattr(tool_executor, "_TOOL_TIMEOUT", 0.01)
    monkeypatch.setattr(ToolExecutor, "_dispatch", slow_dispatch)

    result = await ToolExecutor().execute("task_history", {})

    assert '"error_code": "tool_executor_timeout"' in result
    assert '"timeout_seconds": 0.01' in result


@pytest.mark.asyncio
async def test_query_database_allows_created_at_column(monkeypatch):
    from app.services.tool_executor import ToolExecutor

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Acquire:
        def __init__(self, conn):
            self.conn = conn
            self.timeout = None

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        def __init__(self):
            self.query = ""

        def transaction(self):
            return _Tx()

        async def execute(self, *args):
            return None

        async def fetch(self, query, *args, **kwargs):
            self.query = query
            return [{"created_at": "2026-09-04T00:00:00+09:00"}]

    class _Pool:
        def __init__(self):
            self.conn = _Conn()

        def acquire(self, timeout=None):
            acq = _Acquire(self.conn)
            acq.timeout = timeout
            return acq

    pool = _Pool()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)

    result = await ToolExecutor()._query_database({"query": "SELECT created_at FROM chat_messages", "limit": 1})

    assert result == [{"created_at": "2026-09-04T00:00:00+09:00"}]
    assert "LIMIT 1" in pool.conn.query


@pytest.mark.asyncio
async def test_query_database_falls_back_to_direct_connection_when_pool_unavailable(monkeypatch):
    from app.services import tool_executor
    from app.services.tool_executor import ToolExecutor

    captured = {}

    async def fake_direct(clean_query, connect_timeout, statement_timeout):
        captured["query"] = clean_query
        captured["connect_timeout"] = connect_timeout
        captured["statement_timeout"] = statement_timeout
        return [{"ok": 1}]

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: (_ for _ in ()).throw(RuntimeError("pool missing")))
    monkeypatch.setattr(tool_executor, "_fetch_internal_db_rows_direct", fake_direct)

    result = await ToolExecutor()._query_database({"query": "SELECT 1", "limit": 1})

    assert result == [{"ok": 1}]
    assert captured["query"] == "SELECT 1 LIMIT 1"
    assert captured["connect_timeout"] <= 4.0
    assert captured["statement_timeout"] <= 22.0
