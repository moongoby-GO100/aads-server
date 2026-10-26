"""계획 모드의 제한 조회 및 MCP 표기 회귀 검사."""
import json
from unittest.mock import AsyncMock
import pytest
from mcp_servers import plan_read_tools as read, plan_mode_guard as guard


def test_source_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(read, 'ROOT', tmp_path)
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app/ok.py').write_text('answer = 42')
    assert read.read_source('app/ok.py')['content'] == 'answer = 42'
    (tmp_path / 'app/link.py').symlink_to(tmp_path / 'app/ok.py')
    for path in ('app/../ok.py', '/etc/passwd', 'app/.env', 'app/link.py', 'app/secrets.py', 'app/$(echo x).env'):
        with pytest.raises((ValueError, OSError)):
            read.read_source(path)
    (tmp_path / 'app/inline.py').write_text('password = "supersecret"')
    with pytest.raises(ValueError):
        read.read_source('app/inline.py')


@pytest.mark.asyncio
async def test_db_rejects_arbitrary_sql():
    for sql in ('SELECT 1', 'SELECT dangerous_udf()', 'SELECT 1; DELETE FROM x', 'WITH x AS (DELETE FROM x RETURNING *) SELECT * FROM x'):
        with pytest.raises(ValueError):
            await read.execute('plan_database_status', {'sql': sql})


@pytest.mark.asyncio
async def test_tools_list_annotations_and_execution(monkeypatch):
    from mcp_servers import aads_tools_bridge as bridge
    monkeypatch.setenv('AADS_RESPONSE_MODE', 'plan')
    monkeypatch.setattr(bridge, '_get_tool_definitions', lambda: [{'name': n} for n in ('handover_search', 'pipeline_runner_status', 'pipeline_runner_submit', 'query_db')])
    tools = await bridge.list_tools()
    assert {t.name for t in tools} == {'handover_search', 'pipeline_runner_status', *read.NAMES}
    assert all(t.annotations.readOnlyHint is True for t in tools)
    monkeypatch.setenv('AADS_RESPONSE_MODE', 'quality')
    tools = await bridge.list_tools()
    assert all(t.annotations.readOnlyHint is False for t in tools if t.name in {'query_db', 'pipeline_runner_submit'})
    monkeypatch.setattr(bridge, '_ensure_db', AsyncMock())
    monkeypatch.setenv('AADS_RESPONSE_MODE', 'plan')
    assert json.loads(await bridge._call_tool('pipeline_runner_submit', {}))['blocked']


@pytest.mark.parametrize('module', ['app.services.plan_mode_guard', 'mcp_servers.plan_mode_guard'])
def test_unknown_mode_and_switch(module):
    import importlib
    policy = importlib.import_module(module)
    for mode, blocked in [('unknown', True), (' PLAN ', True), ('quality', False), ('fast', False), ('plan', True)]:
        token = policy.set_response_mode(mode)
        try:
            assert bool(policy.check('pipeline_runner_submit', {})) is blocked
        finally:
            policy.reset_response_mode(token)


def test_policy_parity():
    from app.services import plan_mode_guard as api
    assert api._READ_ONLY_TOOLS == guard._READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_probe_transaction_is_readonly(monkeypatch):
    from app.core import db_pool
    class Context:
        def __init__(self, value):
            self.value = value
        async def __aenter__(self):
            return self.value
        async def __aexit__(self, *args):
            return False
    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {'readonly': True}
            return Context(self)
        execute = AsyncMock()
        fetchrow = AsyncMock(return_value={'read_probe': 1, 'read_only': 'on'})
    conn = Connection()
    class Pool:
        def acquire(self):
            return Context(conn)
    monkeypatch.setattr(db_pool, 'get_pool', lambda: Pool())
    assert await read.execute('plan_database_status', {}) == {'read_probe': 1, 'read_only': 'on'}
    assert conn.execute.await_count == 2
    assert conn.fetchrow.await_args.args == ("SELECT 1 AS read_probe, current_setting('transaction_read_only') AS read_only",)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("sensitive"), OSError("sensitive"), UnicodeError("sensitive")])
async def test_bridge_returns_safe_read_error(monkeypatch, error):
    from mcp_servers import aads_tools_bridge as bridge
    monkeypatch.setattr(bridge, "_ensure_db", AsyncMock())
    monkeypatch.setattr(bridge, "_session_in_plan_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(read, "execute", AsyncMock(side_effect=error))
    result = await bridge._call_tool("plan_read_source", {"path": "app/main.py"})
    assert json.loads(result)["error"] == "plan_read_denied"
    assert "sensitive" not in result
    assert not guard.plan_mode_active()


@pytest.mark.parametrize("path", ["outside/main.py", "app/main.json", "app//main.py", "app/./main.py"])
def test_source_rejects_unapproved_roots_and_types(path):
    with pytest.raises(ValueError):
        read.read_source(path)


def test_source_rejects_oversize_and_hardlink(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(read, "ROOT", tmp_path)
    (tmp_path / "app").mkdir()
    source = tmp_path / "app/main.py"
    source.write_text("x" * 131073)
    with pytest.raises(ValueError):
        read.read_source("app/main.py")
    source.write_text("x = 1")
    os.link(source, tmp_path / "app/alias.py")
    with pytest.raises(ValueError):
        read.read_source("app/alias.py")


@pytest.mark.asyncio
async def test_schema_parameter_binding_and_readonly(monkeypatch):
    from app.core import db_pool
    class Context:
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self.value
        async def __aexit__(self, *args): return False
    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {"readonly": True}
            return Context(self)
        execute = AsyncMock()
        fetch = AsyncMock(return_value=[{"column_name": "id", "data_type": "uuid", "is_nullable": "NO"}])
    conn = Connection()
    class Pool:
        def acquire(self): return Context(conn)
    monkeypatch.setattr(db_pool, "get_pool", lambda: Pool())
    result = await read.execute("plan_database_schema", {"table": "project_handover_entries"})
    assert result["columns"][0]["column_name"] == "id"
    assert conn.fetch.await_args.args[1] == "project_handover_entries"
    assert "table_name = $1" in conn.fetch.await_args.args[0]
    for params in ({"table": "x; DELETE FROM x"}, {"table": "x", "sql": "SELECT 1"}, {"table": "pg_catalog.pg_authid"}):
        with pytest.raises(ValueError):
            await read.execute("plan_database_schema", params)
    assert conn.fetch.await_count == 1


def test_verified_read_metadata_is_guarded():
    assert read.VERIFIED_READ_ONLY_TOOLS <= guard._READ_ONLY_TOOLS
