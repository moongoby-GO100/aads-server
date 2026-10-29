"""D1 boundaries tested without a database or model service."""
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.design_agent import queue, runner
from app.services.design_agent.budget import BudgetExceeded, RunBudget, check_run
from app.services.design_agent.config import Config
from app.services.design_agent.writer import DesignAgentPermissionError, DesignAgentWriter


class Connection:
    def __init__(self):
        self.execute = AsyncMock()
        self.fetchval = AsyncMock(return_value='row-id')
        self.fetchrow = AsyncMock()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_disabled_does_not_even_acquire_connection():
    pool = SimpleNamespace(acquire=AsyncMock())
    assert await runner.process_one(pool, None, Config()) == {'status': 'blocked_disabled'}
    pool.acquire.assert_not_called()


def test_environment_limits(monkeypatch):
    for key in ('DESIGN_AGENT_ENABLED', 'DESIGN_AGENT_MAX_LLM_CALLS_PER_RUN',
                'DESIGN_AGENT_DAILY_USD_CAP'):
        monkeypatch.delenv(key, raising=False)
    assert Config.from_env() == Config()
    monkeypatch.setenv('DESIGN_AGENT_MAX_LLM_CALLS_PER_RUN', '99')
    assert Config.from_env().max_llm_calls == 15
    with pytest.raises(ValueError):
        Config(max_llm_calls=16)


@pytest.mark.asyncio
async def test_enqueue_idempotent():
    conn = Connection()
    rows = {}

    async def insert(sql, tenant, project, event, dedupe, payload):
        assert 'ON CONFLICT (tenant_id, dedupe_key)' in sql
        assert 'RETURNING id' in sql
        return rows.setdefault((tenant, dedupe), len(rows) + 1)

    conn.fetchval.side_effect = insert
    assert await queue.enqueue_trigger(conn, 't', 'p', 'manual', 'd') == 1
    assert await queue.enqueue_trigger(conn, 't', 'p', 'manual', 'd') == 1
    assert await queue.enqueue_trigger(conn, 'other', 'p', 'manual', 'd') == 2


@pytest.mark.asyncio
async def test_claim_and_finish_tenant_fenced():
    conn = Connection()
    await queue.claim_next(conn, 't')
    sql = conn.fetchrow.call_args.args[0]
    assert 'FOR UPDATE SKIP LOCKED' in sql and "tenant_id=$1" in sql
    assert 'attempts=attempts+1' in sql
    await queue.mark_done(conn, 't', 'id')
    await queue.mark_failed(conn, 't', 'id')
    for call in conn.execute.call_args_list:
        assert 'tenant_id=$2' in call.args[0]
        assert call.args[1:3] == ('id', 't')


@pytest.mark.asyncio
@pytest.mark.parametrize('table,operation,patch', [
    ('design_token_sets', 'UPDATE', {}),
    ('design_token_sets', 'DELETE', {}),
    ('project_document_heads', 'INSERT', {}),
    ('design_token_sets', 'INSERT', {'status': 'approved'}),
    ('design_token_sets', 'INSERT', {'tenant_id': None}),
    ('design_token_sets', 'INSERT', {'tenant_id': 'other'}),
    ('design_token_sets', 'INSERT', {'source': 'manual'}),
    ('design_token_sets', 'approve_document', {}),
    ('design_token_sets', 'INSERT', {'project_key': 'other'}),
    ('design_token_sets', 'INSERT', {'unexpected': True}),
])
async def test_writer_denials_audited(table, operation, patch):
    conn = Connection()
    writer = DesignAgentWriter(conn, 't', 'p', 'run')
    values = dict(tenant_id='t', project_key='p', version='1', source='generated')
    values.update(patch)
    with pytest.raises(DesignAgentPermissionError):
        await writer.write(table, values, operation)
    conn.fetchval.assert_not_called()
    sql, run, tenant, writes, error = conn.execute.call_args.args
    assert 'UPDATE design_agent_runs' in sql and run == 'run' and tenant == 't'
    assert json.loads(writes)[0]['denied'] == error


@pytest.mark.asyncio
@pytest.mark.parametrize('table', ['design_token_sets', 'design_audit_runs',
                                  'design_modification_requests'])
async def test_writer_forces_draft_and_checks_project_tenant(table):
    conn = Connection()
    writer = DesignAgentWriter(conn, 't', 'p', 'run')
    values = dict(tenant_id='t', project_key='p')
    if table == 'design_token_sets':
        values.update(version='1', source='generated', tokens={})
    await writer.write(table, values)
    check, insert = conn.fetchval.call_args_list
    assert 'tenant_id=$1 AND project_key=$2' in check.args[0]
    assert check.args[1:] == ('t', 'p')
    assert insert.args[0].startswith(f'INSERT INTO {table}')
    assert 'draft' in insert.args
    assert ('tenant_id' in insert.args[0]) == (table == 'design_token_sets')
    assert values.get('status') is None


@pytest.mark.asyncio
async def test_writer_rejects_unowned_project_and_screen():
    conn = Connection()
    writer = DesignAgentWriter(conn, 't', 'p', 'run')
    conn.fetchval.return_value = None
    with pytest.raises(DesignAgentPermissionError):
        await writer.write('design_audit_runs', dict(tenant_id='t', project_key='p'))
    conn.fetchval.side_effect = [1, None]
    with pytest.raises(DesignAgentPermissionError):
        await writer.write('design_modification_requests',
                           dict(tenant_id='t', project_key='p', screen_id='foreign'))


def test_sixteenth_call_blocked():
    budget = RunBudget()
    for _ in range(15):
        budget.record_call(Decimal('0.01'))
    with pytest.raises(BudgetExceeded):
        budget.record_call()
    assert budget.llm_calls == 15 and budget.cost_usd == Decimal('0.15')


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate,cost,expected', [
    (True, 0, 'skipped_duplicate'), (False, 2, 'blocked_budget'),
    (False, '1.9999', None),
])
async def test_budget_and_duplicate(duplicate, cost, expected):
    conn = Connection()
    conn.fetchval.side_effect = [duplicate, Decimal(str(cost))]
    assert await check_run(conn, 't', 'p', 'manual', 'hash', Decimal('2')) == expected
    if not duplicate:
        sql = conn.fetchval.call_args.args[0]
        assert "Asia/Seoul" in sql and 'tenant_id=$1' in sql


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', [None, 'blocked_budget', 'skipped_duplicate'])
async def test_runner_records_terminal_run(monkeypatch, blocked):
    conn = Connection()
    conn.fetchrow.return_value = dict(id='trigger', event_type='manual',
                                     project_key='p', payload={'a': 1})
    monkeypatch.setattr(runner, 'check_run', AsyncMock(return_value=blocked))
    handler = AsyncMock()
    monkeypatch.setitem(runner.HANDLERS, 'manual', handler)
    pool = SimpleNamespace(acquire=lambda: conn)
    result = await runner.process_one(pool, 't', Config(enabled=True))
    assert result['status'] == (blocked or 'succeeded')
    assert handler.await_count == (0 if blocked else 1)
    assert 'pg_advisory_xact_lock' in conn.execute.call_args_list[0].args[0]
    assert any('finished_at=now()' in c.args[0] for c in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_handler_denial_retained_in_run(monkeypatch):
    conn = Connection()
    conn.fetchrow.return_value = dict(id='trigger', event_type='manual', project_key='p', payload={})
    monkeypatch.setattr(runner, 'check_run', AsyncMock(return_value=None))

    async def denied(trigger, writer, budget):
        await writer.write('project_document_heads', {})

    monkeypatch.setitem(runner.HANDLERS, 'manual', denied)
    result = await runner.process_one(SimpleNamespace(acquire=lambda: conn), 't', Config(enabled=True))
    assert result['status'] == 'failed'
    audits = [c.args[3] for c in conn.execute.call_args_list if 'SET writes=' in c.args[0]]
    assert json.loads(audits[-1])[0]['denied']
    assert json.loads(audits[-1])[0]['rolled_back']


def test_migration_static_contract():
    root = Path(__file__).resolve().parents[2] / 'migrations'
    name = '20261031_design_agent_skeleton.sql'
    assert name > '20261030_design_projects_backfill.sql'
    sql = (root / name).read_text()
    down = (root / 'rollback' / name.replace('.sql', '.down.sql')).read_text()
    assert sql.count('CREATE TABLE IF NOT EXISTS') == 2
    assert 'UNIQUE (tenant_id, dedupe_key)' in sql
    assert 'REFERENCES design_agent_triggers(id)' in sql
    for state in ('project_created', 'deploy_succeeded', 'manual', 'screen_change_requested',
                  'new_screen_requested', 'queued', 'claimed', 'done', 'failed', 'skipped',
                  'running', 'succeeded', 'skipped_duplicate', 'blocked_budget', 'blocked_disabled'):
        assert f"'{state}'" in sql
    assert sql.count('CHECK') >= 5
    assert '(tenant_id, started_at)' in sql
    assert re.findall(r'DROP TABLE IF EXISTS (\w+)', down) == [
        'design_agent_runs', 'design_agent_triggers']
    for content in (sql, down):
        assert 'BEGIN;' in content and content.rstrip().endswith('COMMIT;')
    assert 'ALTER TABLE' not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate,cost,expected', [
    (True, 0, 'skipped_duplicate'), (False, 2, 'blocked_budget'),
    (False, 0, 'succeeded'),
])
async def test_runner_real_budget_gate(monkeypatch, duplicate, cost, expected):
    conn = Connection()
    conn.fetchrow.return_value = dict(id='trigger', event_type='manual',
                                     project_key='p', payload='{"a":1}')
    conn.fetchval.side_effect = ([True, 'run'] if duplicate else
                                [False, Decimal(cost), 'run'])
    handler = AsyncMock()
    monkeypatch.setitem(runner.HANDLERS, 'manual', handler)
    result = await runner.process_one(SimpleNamespace(acquire=lambda: conn),
                                     't', Config(enabled=True))
    assert result == {'id': 'run', 'status': expected}
    assert handler.await_count == (1 if expected == 'succeeded' else 0)


@pytest.mark.asyncio
async def test_no_queued_trigger_creates_no_run():
    conn = Connection()
    conn.fetchrow.return_value = None
    assert await runner.process_one(SimpleNamespace(acquire=lambda: conn),
                                    't', Config(enabled=True)) is None
    conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_runner_budget_exception_records_call_count(monkeypatch):
    conn = Connection()
    conn.fetchrow.return_value = dict(id='trigger', event_type='manual', project_key='p', payload={})
    monkeypatch.setattr(runner, 'check_run', AsyncMock(return_value=None))

    async def over_limit(trigger, writer, budget):
        for _ in range(16):
            budget.record_call()

    monkeypatch.setitem(runner.HANDLERS, 'manual', over_limit)
    result = await runner.process_one(SimpleNamespace(acquire=lambda: conn), 't', Config(enabled=True))
    assert result['status'] == 'blocked_budget'
    update = next(c for c in conn.execute.call_args_list if 'llm_calls=$4' in c.args[0])
    assert update.args[4] == 15
