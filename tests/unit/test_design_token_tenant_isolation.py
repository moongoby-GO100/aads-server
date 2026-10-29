"""Phase 1 tenant isolation: SQL contract and tenant-bound reads."""
from pathlib import Path
from uuid import UUID

import pytest

from tests.unit.test_design_context_builder import _BuilderConn, _Pool, REQUEST_ID
from app.services import design_context_builder as builder

TENANT = UUID('2d701a8c-9596-4757-8588-faa4f7837112')
OTHER = UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
ROOT = Path(__file__).resolve().parents[2]


def test_migration_contract():
    sql = (ROOT / 'migrations/20261029_design_token_tenant_isolation.sql').read_text()
    down = (ROOT / 'migrations/rollback/20261029_design_token_tenant_isolation.down.sql').read_text()
    for fragment in (
        'ADD COLUMN IF NOT EXISTS tenant_id UUID',
        'UNIQUE (tenant_id, project_key)',
        'UNIQUE (tenant_id, project_key, version, mode)',
        'FOREIGN KEY (tenant_id, project_key) REFERENCES design_projects(tenant_id, project_key)',
        "CHECK (status IN ('draft','approved','superseded','archived'))",
        "CHECK (source IN ('manual','extracted','imported_genspark','generated'))",
        "WHERE tenant_id IS NULL AND project_key = 'AADS'",
    ):
        assert fragment in sql
    assert 'ALTER TABLE design_projects DROP CONSTRAINT' not in sql
    assert "'2d701a8c-9596-4757-8588-faa4f7837112'" in sql
    assert 'design_token_sets_project_key_version_mode_key' in sql
    for text in (sql, down):
        assert 'BEGIN;' in text and text.rstrip().endswith('COMMIT;')
        assert 'IF NOT EXISTS' in text
    assert 'UNIQUE (project_key, version, mode)' in down
    assert 'REFERENCES design_projects(project_key)' in down
    assert 'DROP COLUMN IF EXISTS tenant_id' in down
    for table in ('design_screens', 'design_decisions', 'design_audit_runs', 'design_modification_requests'):
        assert f'ALTER TABLE {table}' not in sql + down


class TenantConn(_BuilderConn):
    def __init__(self, repo_path, approved=True):
        super().__init__(repo_path)
        self.approved = approved

    async def fetchrow(self, query, *args):
        if 'FROM design_modification_requests r' in query:
            assert 'JOIN design_projects p' in query
            assert 'p.tenant_id = $2::uuid' in query
            if args[1] != TENANT:
                return None
        if 'FROM design_token_sets' in query:
            assert 'tenant_id = $2::uuid' in query
            assert "status = 'approved'" in query
            assert args == ('AADS', TENANT)
            if not self.approved:
                return None
        return await super().fetchrow(query, *args)


@pytest.mark.asyncio
async def test_other_tenant_rejected_before_context_write(monkeypatch, tmp_path):
    conn = TenantConn(str(tmp_path))
    monkeypatch.setattr(builder, 'get_pool', lambda: _Pool(conn))
    with pytest.raises(builder.DesignContextRequestNotFound):
        await builder.build_context_pack(UUID(REQUEST_ID), OTHER)
    assert conn.inserted_context is None


@pytest.mark.asyncio
async def test_missing_tenant_rejected_before_db(monkeypatch):
    monkeypatch.setattr(builder, 'get_pool', lambda: pytest.fail('must not open DB'))
    with pytest.raises(TypeError):
        await builder.build_context_pack(UUID(REQUEST_ID))
    for tenant in (None, '', 'invalid'):
        with pytest.raises(ValueError):
            await builder.build_context_pack(UUID(REQUEST_ID), tenant)


@pytest.mark.asyncio
@pytest.mark.parametrize('approved', [True, False])
async def test_approved_tokens_only(monkeypatch, tmp_path, approved):
    conn = TenantConn(str(tmp_path), approved=approved)
    monkeypatch.setattr(builder, 'get_pool', lambda: _Pool(conn))
    result = await builder.build_context_pack(UUID(REQUEST_ID), TENANT)
    pack = result['context_pack']
    tokens = pack['context']['design_contract']['design_tokens']
    if approved:
        assert tokens['tenant_id'] == str(TENANT)
        assert tokens['project_key'] == 'AADS'
        assert tokens['version'] == '2026.05'
        assert tokens['mode'] == 'project'
    else:
        assert tokens is None
        assert any(item['message'] == 'no approved design_token_sets' for item in pack['missing_context'])


@pytest.mark.asyncio
async def test_admin_missing_tenant_rejected():
    from fastapi import HTTPException
    from app.api.admin import list_design_projects
    with pytest.raises(HTTPException) as exc:
        await list_design_projects(current_user={})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_lists_only_authenticated_tenant(monkeypatch):
    from app.api.admin import list_design_projects
    from app.core import db_pool

    class Conn:
        async def fetch(self, query, *args):
            assert 'WHERE tenant_id = $1::uuid' in query
            assert args == (TENANT,)
            return [{'project_key': 'AADS', 'display_name': 'AADS', 'repo_path': '/unused', 'status': 'active'}]

    monkeypatch.setattr(db_pool, 'get_pool', lambda: _Pool(Conn()))
    result = await list_design_projects(current_user={'tenant_id': str(TENANT)})
    assert result['count'] == 1
    assert result['projects'][0]['project_key'] == 'AADS'


@pytest.mark.asyncio
async def test_context_api_missing_tenant_rejected(monkeypatch):
    from fastapi import HTTPException
    from app.api import design_modifications

    async def forbidden(*args):
        pytest.fail('builder must not run without tenant')

    monkeypatch.setattr(design_modifications, 'build_context_pack', forbidden)
    with pytest.raises(HTTPException) as exc:
        await design_modifications.build_design_modification_context_pack(
            UUID(REQUEST_ID), current_user={'user_id': 'test'},
        )
    assert exc.value.status_code == 403
