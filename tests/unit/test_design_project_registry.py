"""Registration ownership, preservation, and creation failure boundaries."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.services import design_project_registry as registry

TENANT = UUID('2d701a8c-9596-4757-8588-faa4f7837112')
OTHER = UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')


class Connection:
    def __init__(self):
        self.rows = {}

    async def fetchrow(self, query, *args):
        if query.startswith('SELECT'):
            return self.rows.get(args[0])
        assert 'ON CONFLICT DO NOTHING' in query
        tenant, key, name, path, stack, adapter, status, metadata = args
        if key in self.rows:
            return None
        self.rows[key] = dict(tenant_id=tenant, display_name=name, repo_path=path,
                              frontend_stack=stack, adapter_key=adapter,
                              status=status, metadata=metadata)
        return self.rows[key]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def connection(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(registry, 'get_pool', lambda: SimpleNamespace(acquire=lambda: conn))
    return conn


async def ensure(tenant=TENANT, key='AADS', name='Original'):
    return await registry.ensure_design_project(
        tenant, key, name, '/repo', 'react', 'legacy-css', 'contabo116')


@pytest.mark.asyncio
async def test_idempotent_preserves_manual_edits(connection):
    assert await ensure() == 'registered'
    connection.rows['AADS']['display_name'] = 'Human edit'
    before = dict(connection.rows['AADS'])
    assert await ensure(name='New automatic value') == 'registered'
    assert len(connection.rows) == 1
    assert connection.rows['AADS'] == before


@pytest.mark.asyncio
async def test_tenant_separation_and_global_key_conflict(connection):
    assert await ensure() == 'registered'
    before = dict(connection.rows['AADS'])
    assert await ensure(OTHER) == 'conflict'
    assert connection.rows['AADS'] == before
    assert await ensure(OTHER, 'OTHER') == 'registered'
    assert connection.rows['OTHER']['tenant_id'] == OTHER


@pytest.mark.asyncio
async def test_unknown_repository_and_failure(connection, monkeypatch):
    assert await registry.register_design_project(TENANT, 'SF') == 'registered'
    assert connection.rows['SF']['repo_path'] == 'unknown'
    assert connection.rows['SF']['status'] == 'pending_repo'
    monkeypatch.setattr(registry, 'ensure_design_project', AsyncMock(side_effect=RuntimeError('DB down')))
    assert await registry.register_design_project(TENANT, 'AADS') == 'failed'


@pytest.mark.asyncio
async def test_create_project_without_tenant_skips_random_id(monkeypatch):
    from app.api import projects
    graph = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
    monkeypatch.setitem(sys.modules, 'app.main', SimpleNamespace(app_state={'graph': graph}))
    monkeypatch.setattr(projects, '_get_healing_engine', AsyncMock(return_value=None))
    monkeypatch.setattr(registry, 'ensure_design_project', AsyncMock(side_effect=RuntimeError('DB down')))
    register = AsyncMock(side_effect=AssertionError('random project id must not register'))
    monkeypatch.setattr(registry, 'register_design_project', register)
    result = await projects.create_project(projects.CreateProjectRequest(description='AADS'))
    register.assert_not_awaited()
    assert result.status == 'checkpoint_pending'
    assert result.design_registration == 'skipped'
    graph.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspace_failure_isolation(monkeypatch):
    from app.services import chat_service
    row = {'id': 'workspace', 'name': 'AADS', 'project_key': 'AADS'}
    conn = Connection()
    conn.fetchrow = AsyncMock(return_value=row)
    monkeypatch.setattr(chat_service, 'get_pool', lambda: SimpleNamespace(acquire=lambda: conn))
    monkeypatch.setattr(chat_service, '_row_to_dict', dict)
    monkeypatch.setattr(registry, 'ensure_design_project', AsyncMock(side_effect=RuntimeError('DB down')))
    result = await chat_service.create_workspace({'name': 'AADS'}, tenant_id=str(TENANT))
    assert result['id'] == 'workspace'
    assert result['design_registration'] == 'failed'


def test_backfill_and_scoped_rollback():
    root = Path(__file__).resolve().parents[2]
    sql = (root / 'migrations/20261030_design_projects_backfill.sql').read_text()
    down = (root / 'migrations/rollback/20261030_design_projects_backfill.down.sql').read_text()
    for key in ('AADS', 'GO100', 'KIS', 'SF', 'NTV2', 'NAS'):
        assert key in sql
    assert sql.count("('2d701a8c-9596-4757-8588-faa4f7837112'") == 5
    assert 'ON CONFLICT DO NOTHING' in sql
    assert "metadata->>'backfill_task'" in down
    assert "'AADS'" not in down
    assert 'design_token_sets' not in sql + down


@pytest.mark.asyncio
@pytest.mark.parametrize('tenant,key', [(TENANT, 'ab12cd34'), (None, 'AADS'), ('', 'AADS'), (TENANT, 'NAS')])
async def test_invalid_registration_skipped(monkeypatch, tenant, key):
    mock = AsyncMock(side_effect=AssertionError('must not register'))
    monkeypatch.setattr(registry, 'ensure_design_project', mock)
    assert await registry.register_design_project(tenant, key) == 'skipped'
    mock.assert_not_awaited()


def test_create_project_auth_signature_unchanged():
    import inspect
    from app.api import projects
    assert list(inspect.signature(projects.create_project).parameters) == ['req']


@pytest.mark.asyncio
async def test_workspace_missing_resolved_tenant_skipped(monkeypatch):
    from app.services import chat_service
    conn = Connection()
    row = {'id': 'workspace', 'name': 'AADS', 'project_key': 'AADS'}
    conn.fetchrow = AsyncMock(return_value=row)
    monkeypatch.setattr(chat_service, 'get_pool', lambda: SimpleNamespace(acquire=lambda: conn))
    monkeypatch.setattr(chat_service, '_require_tenant_uuid', lambda *args: None)
    monkeypatch.setattr(chat_service, '_row_to_dict', dict)
    mock = AsyncMock(side_effect=AssertionError('must not register'))
    monkeypatch.setattr(registry, 'register_design_project', mock)
    result = await chat_service.create_workspace({'name': 'AADS'})
    assert result == dict(row, design_registration='skipped')
    mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('key,expected', [('AADS', 'registered'), ('ab12cd34', 'skipped')])
async def test_workspace_registration_key_guard(monkeypatch, connection, key, expected):
    from app.services import chat_service
    workspace_conn = Connection()
    row = {'id': 'workspace', 'name': key, 'project_key': key}
    workspace_conn.fetchrow = AsyncMock(return_value=row)
    monkeypatch.setattr(chat_service, 'get_pool', lambda: SimpleNamespace(acquire=lambda: workspace_conn))
    monkeypatch.setattr(chat_service, '_row_to_dict', dict)
    result = await chat_service.create_workspace({'name': key}, tenant_id=str(TENANT))
    assert result == dict(row, design_registration=expected)
    assert list(connection.rows) == (['AADS'] if expected == 'registered' else [])


@pytest.mark.asyncio
async def test_workspace_unexpected_registration_exception(monkeypatch):
    from app.services import chat_service
    conn = Connection()
    conn.fetchrow = AsyncMock(return_value={'id': 'workspace', 'project_key': 'AADS'})
    monkeypatch.setattr(chat_service, 'get_pool', lambda: SimpleNamespace(acquire=lambda: conn))
    monkeypatch.setattr(chat_service, '_row_to_dict', dict)
    monkeypatch.setattr(registry, 'register_design_project', AsyncMock(side_effect=RuntimeError('unexpected')))
    result = await chat_service.create_workspace({'name': 'AADS'}, tenant_id=str(TENANT))
    assert result['id'] == 'workspace'
    assert result['design_registration'] == 'failed'


def test_backfill_follows_tenant_isolation_in_release_order():
    """Match the release runner's LC_ALL=C byte ordering of top-level SQL."""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    names = [path.name for path in (root / 'migrations').glob('*.sql')
             if not path.name.endswith(('.down.sql', '.verify.sql'))
             and not path.name.startswith('verify')]
    ordered = subprocess.run(
        ['sort'], input='\n'.join(names) + '\n', text=True,
        capture_output=True, check=True, env={'LC_ALL': 'C'},
    ).stdout.splitlines()
    assert ordered.index('20261029_design_token_tenant_isolation.sql') < ordered.index(
        '20261030_design_projects_backfill.sql')
    assert '20261029_design_projects_backfill.sql' not in names
