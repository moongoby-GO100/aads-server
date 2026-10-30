import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.services.goal_work_item_sync import parse_work_item_ids, transition_plan, sync_runner, bind_work_items


def tree(a='draft', b='draft', story='draft', epic='draft'):
    return [dict(id='e', parent_id=None, type='epic', status=epic),
            dict(id='s', parent_id='e', type='story', status=story),
            dict(id='a', parent_id='s', type='task', status=a),
            dict(id='b', parent_id='s', type='task', status=b)]


def test_explicit_multiple_ids():
    a, b = str(uuid4()), str(uuid4())
    assert parse_work_item_ids(f'WORK_ITEM_ID: {a}\nWORK_ITEM_ID: {b}, {a}\nWORK_ITEM_ID: invalid') == [a, b]


@pytest.mark.parametrize('state', ['draft', 'ready'])
def test_start(state):
    assert transition_plan(tree(a=state), 'running', ['a']) == {'a': 'in_progress'}


def test_done_partial_parent_not_promoted():
    assert transition_plan(tree(), 'done', ['a']) == {'a': 'in_review'}


def test_done_promotes_story_and_epic():
    assert transition_plan(tree(b='completed'), 'done', ['a']) == {'a': 'in_review', 's': 'in_review', 'e': 'in_review'}


@pytest.mark.parametrize('event,target', [('completed', 'completed'), ('rejected', 'changes_requested')])
def test_milestone(event, target):
    assert transition_plan(tree('in_review', 'in_review', 'in_review', 'in_review'), event) == dict.fromkeys(['a', 'b', 's', 'e'], target)


@pytest.mark.parametrize('event', ['running', 'done', 'completed', 'rejected'])
def test_human_states_preserved(event):
    assert transition_plan(tree('cancelled', 'blocked', 'cancelled', 'blocked'), event, ['a', 'b']) == {}


@pytest.mark.parametrize('event', ['running', 'done', 'completed', 'rejected'])
def test_idempotent(event):
    rows = tree('in_review', 'completed') if event in ('completed', 'rejected') else tree(b='in_review')
    plan = transition_plan(rows, event, ['a'])
    for row in rows:
        row['status'] = plan.get(row['id'], row['status'])
    assert transition_plan(rows, event, ['a']) == {}


def test_pre_push_done_is_ignored():
    conn = AsyncMock()
    asyncio.run(sync_runner(conn, 'runner-test', 'done', 'awaiting_approval'))
    conn.fetch.assert_not_called()


def test_binding_scope_and_no_implicit_binding():
    conn = AsyncMock()
    asyncio.run(bind_work_items(conn, 'j', 'nothing'))
    conn.execute.assert_not_called()
    asyncio.run(bind_work_items(conn, 'j', f'WORK_ITEM_ID: {uuid4()}'))
    sql = conn.execute.call_args.args[0]
    for scope in ('w.tenant_id = l.tenant_id', 'w.goal_id = l.goal_id', 'w.milestone_id = l.milestone_id'):
        assert scope in sql


@pytest.fixture(autouse=True)
def available_schema(monkeypatch):
    monkeypatch.setattr('app.services.goal_work_item_sync.schema_ready', AsyncMock(return_value=True))


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, state='in_review', synced=None):
        self.rows = tree(state, 'completed')
        self.synced = synced
        self.status = 'done'
        self.locks = 0

    def transaction(self):
        return Transaction()

    async def fetchval(self, *args):
        self.locks += 1
        return 'm'

    async def fetch(self, sql, *args):
        if 'FROM pipeline_jobs' in sql:
            assert 'work_items_synced_status IS DISTINCT FROM j.status' in sql
            return [] if self.synced == self.status else [dict(job_id='j', status=self.status, phase='done')]
        if 'FROM goal_task_links' in sql:
            return [] if self.synced == args[2] else [dict(milestone_id='m', work_item_ids=['a'])]
        return self.rows

    async def execute(self, sql, *args):
        if 'UPDATE goal_task_links' in sql:
            self.synced = args[2]
        else:
            for row in self.rows:
                if row['id'] == args[0]:
                    row['status'] = args[1]


def test_rejected_reconcile_and_new_job_running():
    from app.services.goal_work_item_sync import reconcile_runner_items, sync_milestone

    conn = FakeConnection()
    asyncio.run(sync_milestone(conn, 'm', 'rejected'))
    asyncio.run(reconcile_runner_items(conn))
    assert conn.rows[2]['status'] == 'changes_requested'
    locks = conn.locks
    asyncio.run(reconcile_runner_items(conn))
    assert conn.locks == locks
    # 새 연결(새 잡)은 아직 반영한 상태가 없다.
    conn.synced = None
    asyncio.run(sync_runner(conn, 'new-job', 'running'))
    assert conn.rows[2]['status'] == 'in_progress'


def test_missing_schema_noops(monkeypatch):
    from app.services.goal_work_item_sync import reconcile_runner_items, sync_milestone

    monkeypatch.setattr('app.services.goal_work_item_sync.schema_ready', AsyncMock(return_value=False))
    conn = AsyncMock()
    asyncio.run(bind_work_items(conn, 'j', f'WORK_ITEM_ID: {uuid4()}'))
    asyncio.run(sync_runner(conn, 'j', 'running'))
    asyncio.run(sync_milestone(conn, 'm', 'rejected'))
    asyncio.run(reconcile_runner_items(conn))
    conn.fetch.assert_not_called()
    conn.execute.assert_not_called()
    conn.transaction.assert_not_called()


@pytest.mark.parametrize('status,phase', [('done', 'awaiting_approval'), ('cancelled', None), ('blocked', None)])
def test_runner_skip(status, phase):
    conn = FakeConnection()
    asyncio.run(sync_runner(conn, 'j', status, phase))
    assert conn.locks == 0
    assert conn.synced is None


def test_running_does_not_update_goal_link():
    import ast
    from pathlib import Path

    module = ast.parse(Path('app/services/pipeline_runner_service.py').read_text())
    save = next(n for n in ast.walk(module) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_save_to_db')
    running = next(n for n in ast.walk(save) if isinstance(n, ast.If) and ast.unparse(n.test) == "self.status in ('running', 'in_progress')")
    calls = [n.func.id for n in ast.walk(running) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert '_sync_work_items_running' in calls
    assert '_update_linked_goal_state_with_phase' not in calls


def test_bind_failure_still_records(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app.services import goal_autolink

    result = SimpleNamespace(linked=True, source='test', goal_id='g', milestone_id='m', reason='test')
    monkeypatch.setattr(goal_autolink, '_resolve_and_link', AsyncMock(return_value=result))
    record = AsyncMock()
    monkeypatch.setattr(goal_autolink, '_record', record)
    pool = MagicMock()
    conn = FakeConnection()
    class Acquire(Transaction):
        async def __aenter__(self):
            return conn
    pool.acquire.return_value = Acquire()
    monkeypatch.setattr('app.core.db_pool.get_pool', lambda: pool)
    monkeypatch.setattr('app.services.goal_work_item_sync.bind_work_items', AsyncMock(side_effect=RuntimeError('bind failed')))
    asyncio.run(goal_autolink.autolink_job('j', 'AADS', instruction='test'))
    record.assert_awaited_once_with('j', result)


def test_schema_probe_requires_both_columns(monkeypatch):
    from app.services import goal_manager

    monkeypatch.setattr(goal_manager, '_link_columns_cache', None)
    conn = AsyncMock()
    conn.fetch.return_value = [{'column_name': 'work_item_ids'}]
    # fixture가 덮은 함수를 대신하여 실제 스키마 검사 함수를 독립 로드한다.
    import runpy
    probe = runpy.run_path('app/services/goal_work_item_sync.py')['schema_ready']
    assert asyncio.run(probe(conn)) is False
    monkeypatch.setattr(goal_manager, '_link_columns_cache', None)
    conn.fetch.return_value.append({'column_name': 'work_items_synced_status'})
    assert asyncio.run(probe(conn)) is True
