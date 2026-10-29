"""Tenant-bound, non-destructive registration of design project targets."""
import json
import logging
from uuid import UUID

from app.core.db_pool import get_pool
from app.core.project_config import PROJECT_MAP
from app.services.server_registry import PROJECT_TO_SERVER, normalize_project

logger = logging.getLogger(__name__)


async def ensure_design_project(tenant_id, project_key, display_name, repo_path,
                                frontend_stack, adapter_key, server_id):
    """Insert once; preserve all existing human edits, including metadata.

    The global project_key constraint remains in Phase 1. DO NOTHING covers both
    uniqueness constraints (including concurrent registrations by other tenants).
    """
    tenant = UUID(str(tenant_id))
    if not project_key:
        raise ValueError("project_key required")
    path = repo_path if repo_path and repo_path != '/' else 'unknown'
    metadata = {"server_id": server_id}
    if path == 'unknown':
        metadata['reason'] = 'Repository path is not confirmed in project configuration'
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO design_projects
               (tenant_id, project_key, display_name, repo_path, frontend_stack,
                adapter_key, status, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
               ON CONFLICT DO NOTHING RETURNING tenant_id""",
            tenant, project_key, display_name or project_key, path,
            frontend_stack or 'unknown', adapter_key or 'legacy-css',
            'pending_repo' if path == 'unknown' else 'active', json.dumps(metadata),
        )
        if row is None:
            row = await conn.fetchrow(
                'SELECT tenant_id FROM design_projects WHERE project_key = $1', project_key)
        if row is None:
            raise RuntimeError('Design registration disappeared during registration')
        return 'registered' if UUID(str(row['tenant_id'])) == tenant else 'conflict'


async def register_design_project(tenant_id, project_key, display_name=None):
    """Best effort boundary: never fail the primary project/workspace creation."""
    try:
        key = normalize_project(project_key)
        if not tenant_id or key not in PROJECT_MAP:
            return "skipped"
        config = PROJECT_MAP[key]
        return await ensure_design_project(
            tenant_id, key, display_name or config.get('display_name', key),
            config.get('workdir', 'unknown'), 'unknown', 'legacy-css',
            PROJECT_TO_SERVER.get(key),
        )
    except Exception:
        logger.warning('Design project registration failed', exc_info=True)
        return 'failed'
