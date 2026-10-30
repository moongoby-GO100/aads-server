"""인증된 테넌트의 수집 결과 읽기 전용 API."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user
from app.core.db_pool import get_pool

router = APIRouter(prefix='/collection-datasets', tags=['collection-datasets'])


async def tenant_scope(user: Annotated[dict, Depends(get_current_user)]):
    membership = user.get('current_membership') or {}
    tenant = user.get('tenant_id')
    if not tenant or str(membership.get('tenant_id')) != str(tenant) or membership.get('status') != 'active' or membership.get('role') not in {'owner', 'admin', 'member', 'viewer'}:
        raise HTTPException(403, 'tenant_membership_required')
    return UUID(str(tenant))


@router.get('')
async def datasets(tenant: Annotated[UUID, Depends(tenant_scope)]):
    rows = await get_pool().fetch('SELECT * FROM collection_datasets WHERE tenant_id=$1 ORDER BY dataset_key', tenant)
    return {'datasets': [dict(r) for r in rows]}


async def history(tenant, dataset_key, table, limit):
    if table not in {'collection_runs', 'collection_records'}:
        raise ValueError('invalid table')
    if not await get_pool().fetchval('SELECT id FROM collection_datasets WHERE tenant_id=$1 AND dataset_key=$2', tenant, dataset_key):
        raise HTTPException(404, 'dataset_not_found')
    order = 'r.started_at DESC,r.id' if table == 'collection_runs' else 'r.last_seen_at DESC,r.record_key'
    rows = await get_pool().fetch(
        f'SELECT r.* FROM {table} r JOIN collection_datasets d ON d.id=r.dataset_id '
        f'WHERE d.tenant_id=$1 AND d.dataset_key=$2 ORDER BY {order} LIMIT $3', tenant, dataset_key, limit)
    return [dict(r) for r in rows]


@router.get('/{dataset_key}/runs')
async def runs(dataset_key: str, tenant: Annotated[UUID, Depends(tenant_scope)], limit: int = Query(100, ge=1, le=1000)):
    return {'runs': await history(tenant, dataset_key, 'collection_runs', limit)}


@router.get('/{dataset_key}/records')
async def records(dataset_key: str, tenant: Annotated[UUID, Depends(tenant_scope)], limit: int = Query(100, ge=1, le=1000)):
    return {'records': await history(tenant, dataset_key, 'collection_records', limit)}
