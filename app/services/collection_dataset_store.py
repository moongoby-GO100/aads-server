"""테넌트별 수집 저장과 실행 직렬화. 원문/비밀값은 저장 경계에서 거절한다."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from app.core.db_pool import get_pool
from app.services.delivery_collection_gate import user_action_kind

_FORBIDDEN = re.compile(r'password|passwd|token|cookie|secret|html|주민|계좌번호', re.IGNORECASE)
_SECRET = re.compile(r'<[^>]+>|sk-ant-|Bearer\s+|(?:password|token|cookie|secret)\s*[=:]', re.IGNORECASE)


def record_key(row, key_fields):
    if not key_fields or any(row.get(k) is None or row.get(k) == '' for k in key_fields):
        raise ValueError('missing_record_key')
    raw = json.dumps([row[k] for k in key_fields], ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def prepare_rows(rows, schema, keys):
    if not isinstance(rows, list):
        raise TypeError('invalid_rows')
    unique = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) - set(schema):
            raise ValueError('undeclared_fields')
        for name, value in row.items():
            if _FORBIDDEN.search(name) or isinstance(value, (dict, list)):
                raise ValueError('unsafe_field')
            if isinstance(value, str) and _SECRET.search(value):
                raise ValueError('unsafe_value')
        unique[record_key(row, keys)] = row
    return unique


@asynccontextmanager
async def execution_scope(tenant_id, dataset_keys, domain, *, automatic=False):
    """잠금은 브라우저 실행 전부터 저장 종료까지 같은 세션에서 보유한다."""
    tenant = UUID(str(tenant_id))
    from app.services.collection_pause import is_collection_paused
    async with get_pool().acquire() as conn:
        locks = []
        try:
            names = [f'dataset:{tenant}:{key}' for key in dataset_keys]
            host = urlsplit(str(domain) if '://' in str(domain) else '//' + str(domain)).hostname
            if not host:
                raise ValueError('invalid_domain')
            names.append(f'domain:{host.lower().rstrip(chr(46))}')
            for name in sorted(set(names)):
                lock = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], 'big', signed=True)
                if not await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock):
                    raise RuntimeError('COLLECTION_ALREADY_RUNNING')
                locks.append(lock)
            for key in dataset_keys:
                if await is_collection_paused('smart_browser', dataset_key=key, tenant_id=str(tenant)):
                    raise RuntimeError('COLLECTION_PAUSED')
                dataset = await conn.fetchrow(
                    'SELECT * FROM collection_datasets WHERE tenant_id=$1 AND dataset_key=$2', tenant, key)
                if not dataset:
                    raise ValueError('dataset_not_found')
                if automatic:
                    latest = await conn.fetchval(
                        'SELECT r.status FROM collection_runs r JOIN collection_datasets d ON d.id=r.dataset_id '
                        'WHERE d.tenant_id=$1 AND d.dataset_key=$2 ORDER BY r.started_at DESC LIMIT 1', tenant, key)
                    if not dataset['enabled'] or latest == 'action_required':
                        raise RuntimeError('COLLECTION_ACTION_REQUIRED_OR_DISABLED')
            yield conn
        finally:
            for lock in reversed(locks):
                await conn.fetchval('SELECT pg_advisory_unlock($1)', lock)


async def upsert(conn, tenant_id, dataset_key, output, *, recipe_run_id=None, error_code=''):
    tenant = UUID(str(tenant_id))
    status = output.get('status', 'failed')
    if user_action_kind(error_code) or error_code.lower() in {'auth_required', 'login_required', 'captcha', 'portal_blocked'}:
        status = 'action_required'
    if status not in {'success', 'no_records', 'partial', 'extraction_failed', 'action_required', 'failed'}:
        status = 'failed'
    # 오류 메시지에는 URL/쿠키가 섞일 수 있다. 알려진 코드만 보존한다.
    from app.services.delivery_collection_gate import USER_ACTION_KIND_BY_CODE, error_codes
    allowed = set(USER_ACTION_KIND_BY_CODE) | {'AUTH_REQUIRED', 'LOGIN_REQUIRED', 'CAPTCHA', 'PORTAL_BLOCKED', 'EXTRACTION_FAILED'}
    error_code = next((code for code in error_codes(error_code) if code in allowed), 'COLLECTION_FAILED' if error_code else '')
    async with conn.transaction():
        dataset = await conn.fetchrow(
            'SELECT * FROM collection_datasets WHERE tenant_id=$1 AND dataset_key=$2 FOR UPDATE', tenant, dataset_key)
        if not dataset:
            raise ValueError('dataset_not_found')
        schema = dataset['field_schema']
        if isinstance(schema, str):
            schema = json.loads(schema)
        rows = output.get('rows', [])
        if not isinstance(rows, list):
            raise TypeError('invalid_rows')
        prepared = prepare_rows(rows, schema, dataset['key_fields']) if status in {'success', 'partial'} else {}
        evidence_path = output.get('evidence_path')
        if evidence_path is not None and (
            not isinstance(evidence_path, str)
            or not re.fullmatch(r'[a-zA-Z0-9_./-]{1,512}', evidence_path)
            or '..' in evidence_path.split('/') or evidence_path.startswith('/')
        ):
            raise ValueError('unsafe_evidence_path')
        run_id = uuid4()
        await conn.execute(
            'INSERT INTO collection_runs(id,dataset_id,recipe_run_id,status,rows_seen,required_fill_rate,error_code,evidence_path) '
            'SELECT $1,id,$4,$5,$6,$7,$8,$9 FROM collection_datasets WHERE tenant_id=$2 AND dataset_key=$3',
            run_id, tenant, dataset_key, UUID(str(recipe_run_id)) if recipe_run_id else None,
            status, len(rows), output.get('required_fill_rate'), error_code[:100] or None, evidence_path)
        new = 0
        for key, fields in prepared.items():
            inserted = await conn.fetchval(
                'INSERT INTO collection_records(dataset_id,record_key,fields,first_seen_run_id,last_seen_run_id) '
                'SELECT id,$3,$4::jsonb,$5,$5 FROM collection_datasets WHERE tenant_id=$1 AND dataset_key=$2 '
                'ON CONFLICT(dataset_id,record_key) DO NOTHING RETURNING record_key',
                tenant, dataset_key, key, json.dumps(fields, ensure_ascii=False), run_id)
            new += int(inserted is not None)
            if inserted is None:
                await conn.execute(
                    'UPDATE collection_records r SET fields=$4::jsonb,last_seen_run_id=$5,last_seen_at=now() '
                    'FROM collection_datasets d WHERE r.dataset_id=d.id AND d.tenant_id=$1 AND d.dataset_key=$2 AND r.record_key=$3',
                    tenant, dataset_key, key, json.dumps(fields, ensure_ascii=False), run_id)
        await conn.execute(
            'UPDATE collection_runs r SET rows_new=$3,finished_at=now() FROM collection_datasets d '
            'WHERE r.dataset_id=d.id AND d.tenant_id=$1 AND r.id=$2', tenant, run_id, new)
    return {'run_id': str(run_id), 'rows_new': new, 'status': status}


async def register_schedule(tenant_id, dataset_key, cron):
    """예약 선언만 등록한다. 활성화/스케줄러 실행은 별도 승인 경로다."""
    from apscheduler.triggers.cron import CronTrigger
    CronTrigger.from_crontab(cron)
    return await get_pool().execute(
        'UPDATE collection_datasets SET schedule_cron=$3,enabled=false,updated_at=now() '
        'WHERE tenant_id=$1 AND dataset_key=$2', UUID(str(tenant_id)), dataset_key, cron)


async def register_dataset(tenant_id, project_key, dataset_key, recipe_name, field_schema, key_fields):
    """신뢰된 레시피 등록 경로용. 예약은 항상 비활성 상태로 생성한다."""
    if not dataset_key or not field_schema or not key_fields or set(key_fields) - set(field_schema):
        raise ValueError('invalid_dataset_schema')
    if any(_FORBIDDEN.search(name) for name in field_schema):
        raise ValueError('unsafe_field')
    return await get_pool().fetchval(
        'INSERT INTO collection_datasets(tenant_id,project_key,dataset_key,recipe_name,field_schema,key_fields) '
        'VALUES($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT(tenant_id,dataset_key) DO NOTHING RETURNING id',
        UUID(str(tenant_id)), project_key, dataset_key, recipe_name, json.dumps(field_schema), key_fields)
