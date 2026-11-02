"""정정 서비스의 상태 전이 및 업무 DB 트랜잭션 회귀."""
from contextlib import asynccontextmanager
from datetime import date, time, datetime
from unittest.mock import AsyncMock
import json
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.services import obys_attendance_corrections as svc


def payload(**kw):
    return svc.CorrectionIn(**{ 'work_date':'2026-11-02', 'start_at':'22:00', 'end_at':'06:00', 'break_minutes':0, 'reason':'누락', **kw})


@pytest.mark.parametrize('minutes,expected', [(0,480),(60,420)])
def test_overnight_zero_break(minutes, expected):
    p = payload(break_minutes=minutes)
    assert svc.ws.attendance_worked_minutes(p.start_at,p.end_at,p.break_minutes) == expected
    assert svc.ws.attendance_night_minutes(p.start_at,p.end_at,p.break_minutes) == expected


@pytest.mark.parametrize('values', [{'reason':' '},{'employee_email':'spoof@example.com'},{'break_minutes':-1},{'start_at':'22:00:01'}])
def test_invalid_payload(values):
    with pytest.raises((ValidationError,HTTPException)):
        payload(**values)


@pytest.mark.parametrize('action,status', [('approve','approved'),('reject','rejected'),('withdraw','withdrawn')])
def test_transition(action,status):
    assert svc.transition({'status':'pending','version':1},action,1)
    assert not svc.transition({'status':status,'version':2},action,1)
    with pytest.raises(HTTPException) as e:
        svc.transition({'status':status,'version':2},action,2)
    assert e.value.status_code == 409


class DB:
    def __init__(self):
        self.record = dict(id='r',employee_email='me@example.com',work_date=date(2026,11,1),start_at=time(9),end_at=time(18),break_minutes=60,updated_at=datetime(2026,11,1))
        self.request = dict(id='c',record_id='r',employee_email='me@example.com',status='pending',version=1,original=json.dumps(svc.snapshot(self.record)),requested=payload().model_dump_json(exclude={'reason','supersedes_id'}),reason='누락')
        self.execute = AsyncMock()
        self.close = AsyncMock()
        self.fetchval = AsyncMock(return_value=False)
        self.fetch = AsyncMock(return_value=[])
        self.committed = False
    @asynccontextmanager
    async def transaction(self):
        yield
        self.committed = True
    async def fetchrow(self, sql, *args):
        if sql.startswith('SELECT * FROM obys_'): return self.request
        if sql.startswith('SELECT * FROM yeoljeong_'): return self.record
        if sql.startswith('INSERT'): return {**self.request,'status':'pending','supersedes_id':args[8]}
        if sql.startswith('UPDATE obys_'):
            return {**self.request,'status':args[3],'version':2}
        raise AssertionError(sql)


@pytest.fixture
def db(monkeypatch):
    db=DB()
    monkeypatch.setattr(svc.ws,'_clock_identity',lambda u:{'email':'me@example.com'})
    monkeypatch.setattr(svc.ws,'_clock_business',AsyncMock())
    monkeypatch.setattr(svc.ws,'_require_attendance_admin',lambda u:None)
    monkeypatch.setattr(svc.ws.upload_svc,'_tenant',lambda u:'tenant')
    monkeypatch.setattr(svc.ws.upload_svc,'tenant_session_for_user',lambda u:{'permissions':{'can_manage_settings':True}})
    monkeypatch.setattr(svc.ws.upload_svc,'_connect',AsyncMock(return_value=db))
    monkeypatch.setattr(svc.ws.upload_svc,'_require_business',AsyncMock())
    monkeypatch.setattr(svc.ws,'_attendance_audit',AsyncMock())
    monkeypatch.setattr(svc.ws,'_recompute_attendance_week',AsyncMock())
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create','reject','withdraw','approve'])
async def test_transaction_effects(db,operation):
    result=await svc.run({},'b',operation,record_id='r',correction_id=None if operation=='create' else 'c',payload=payload() if operation=='create' else svc.DecisionIn(version=1,reason='확인'))
    assert db.committed
    assert result['id']=='c'
    mutations=[c for c in db.execute.call_args_list if c.args[0].startswith('UPDATE yeoljeong_attendance_records')]
    assert len(mutations)==(1 if operation=='approve' else 0)
    assert svc.ws._recompute_attendance_week.await_count==(2 if operation=='approve' else 0)
    db.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['payroll','stale','other_employee','pending','resubmit'])
async def test_conflicts_rollback(db,failure):
    op='approve'
    if failure=='payroll': db.fetch.return_value=[{'id':'p','payroll_month':'2026-11','status':'paid'}]
    if failure=='stale': db.record['break_minutes']=30
    if failure=='other_employee':
        op='create'; db.record['employee_email']='other@example.com'
    if failure=='pending':
        op='create'; db.fetchval.return_value=True
    if failure=='resubmit': op='create'
    with pytest.raises(HTTPException) as e:
        await svc.run({},'b',op,record_id='r',correction_id='c' if op=='approve' else None,payload=svc.DecisionIn(version=1,reason='확인') if op=='approve' else payload(supersedes_id='c' if failure=='resubmit' else None))
    assert e.value.status_code==(404 if failure=='other_employee' else 409)
    assert not db.committed
    assert not any(c.args[0].startswith('UPDATE yeoljeong_attendance_records') for c in db.execute.call_args_list)


@pytest.mark.asyncio
async def test_resubmit_preserves_previous_version(db):
    db.request['status']='rejected'
    result=await svc.run({},'b','create',record_id='r',payload=payload(supersedes_id='c'))
    assert result['status']=='pending'
    assert result['supersedes_id']=='c'
    assert db.request['status']=='rejected'
    assert not any(c.args[0].startswith('UPDATE') for c in db.execute.call_args_list)
    assert db.committed


@pytest.mark.asyncio
async def test_api_uses_login_and_rejects_forged_identity(monkeypatch):
    from fastapi import FastAPI
    import httpx
    from app.api.obys_attendance_corrections import router
    from app.auth import get_current_user
    app=FastAPI()
    app.include_router(router,prefix='/api/v1')
    async def current_user():
        return {'email':'me@example.com'}
    app.dependency_overrides[get_current_user]=current_user
    run=AsyncMock(return_value={'id':'c'})
    monkeypatch.setattr(svc,'run',run)
    path='/api/v1/workspaces/b/attendance/records/r/corrections'
    body=json.loads(payload().model_dump_json())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post(path,json={**body,'employee_email':'other@example.com'})).status_code==422
        run.assert_not_awaited()
        assert (await client.post(path,json=body)).status_code==201
    assert run.await_args.args[0]['email']=='me@example.com'


@pytest.mark.asyncio
async def test_tenant_gate_prevents_connection(db,monkeypatch):
    async def denied(*args): raise HTTPException(403,'다른 테넌트')
    monkeypatch.setattr(svc.ws,'_clock_business',denied)
    with pytest.raises(HTTPException) as e:
        await svc.run({},'foreign','create',record_id='r',payload=payload())
    assert e.value.status_code==403
    svc.ws.upload_svc._connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_retry_is_idempotent(db):
    db.request.update(status='approved',version=2,decided_by='me@example.com',decision_reason='확인')
    result=await svc.run({},'b','approve',correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert result['version']==2
    assert not any(c.args[0].startswith('UPDATE') for c in db.execute.call_args_list)
    svc.ws._attendance_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unique_collision_is_409(db):
    original = db.fetchrow
    async def conflict(sql, *args):
        if sql.startswith('INSERT INTO obys_'):
            raise svc.asyncpg.UniqueViolationError('concurrent insert')
        return await original(sql, *args)
    db.fetchrow = conflict
    with pytest.raises(HTTPException) as e:
        await svc.run({},'b','create',record_id='r',payload=payload())
    assert e.value.status_code==409
    assert not db.committed


@pytest.mark.asyncio
async def test_employee_list_is_scoped(db,monkeypatch):
    monkeypatch.setattr(svc.ws.upload_svc,'tenant_session_for_user',lambda u:{'permissions':{'can_manage_settings':False}})
    assert await svc.run({},'b','list')=={'items':[]}
    assert db.fetch.await_args.args[1:]==('tenant','b',False,'me@example.com')


@pytest.mark.asyncio
async def test_metadata_refresh_does_not_block_approval(db):
    db.record['updated_at'] = datetime(2026,11,2)
    await svc.run({},'b','approve',correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert db.committed


def test_snapshot_normalizes_json_types():
    from decimal import Decimal
    row = DB().record
    row['hourly_wage'] = Decimal('10000.00')
    original = json.loads(json.dumps(svc.snapshot(row)))
    original['hourly_wage'] = '10000'
    original['start_at'] = '09:00'
    assert svc.attendance_revision(row) == svc.attendance_revision(original)


@pytest.mark.asyncio
async def test_date_collision_prevents_mutation(db):
    db.fetchval.return_value = True
    with pytest.raises(HTTPException) as error:
        await svc.run({},'b','approve',correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert error.value.status_code == 409
    assert not db.committed
    assert not any(c.args[0].startswith('UPDATE') for c in db.execute.call_args_list)


@pytest.mark.asyncio
async def test_resubmit_cannot_branch(db):
    db.request['status'] = 'withdrawn'
    db.fetchval.side_effect = [False, True]
    with pytest.raises(HTTPException) as error:
        await svc.run({},'b','create',record_id='r',payload=payload(supersedes_id='c'))
    assert error.value.status_code == 409
    assert not db.committed


@pytest.mark.asyncio
async def test_case_insensitive_withdraw(db,monkeypatch):
    db.request['employee_email'] = 'Me@Example.COM'
    monkeypatch.setattr(svc.ws,'_clock_identity',lambda u:{'email':'ME@example.com'})
    result = await svc.run({},'b','withdraw',correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert result['status'] == 'withdrawn'


@pytest.mark.parametrize('changes', [dict(decided_by='other@example.com'), dict(decision_reason='다름')])
def test_retry_requires_same_actor_and_reason(changes):
    row = dict(status='approved',version=2,decided_by='me@example.com',decision_reason='확인')
    row.update(changes)
    with pytest.raises(HTTPException) as error:
        svc.transition(row,'approve',1,actor='me@example.com',reason='확인')
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_retry_still_requires_admin(db,monkeypatch):
    db.request.update(status='approved',version=2)
    def denied(user):
        raise HTTPException(403,'관리자만 허용')
    monkeypatch.setattr(svc.ws,'_require_attendance_admin',denied)
    with pytest.raises(HTTPException) as error:
        await svc.run({},'b','approve',correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert error.value.status_code == 403
    svc.ws.upload_svc._connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_fences_and_unique_insert_concurrency():
    """Opt-in disposable DB only; never uses application/production DB configuration.

    Apply the actual migration in a unique fixture schema. The database-name guard
    alone is adapted to the disposable database; all indexes/triggers are original.
    """
    import os
    import asyncio
    from pathlib import Path
    from uuid import uuid4
    dsn = os.environ.get('OBYS_CORRECTIONS_TEST_DSN')
    if not dsn:
        pytest.skip('격리 PostgreSQL DSN 미제공: 실제 SQL/동시성 미검증')
    schema = 'correction_test_' + uuid4().hex
    first = await svc.asyncpg.connect(dsn)
    second = await svc.asyncpg.connect(dsn)
    try:
        await first.execute(f'CREATE SCHEMA {schema}')
        for connection in (first, second):
            await connection.execute(f'SET search_path TO {schema}, pg_catalog')
        await first.execute('CREATE TABLE yeoljeong_businesses(id text PRIMARY KEY); '
                            'CREATE TABLE yeoljeong_payroll_statements(id text PRIMARY KEY,business_id text,status text)')
        migration = (Path(__file__).resolve().parents[2] / 'migrations/20261102_obys_attendance_corrections.sql').read_text()
        database_literal = await first.fetchval('SELECT quote_literal(current_database())')
        migration = migration.replace("current_database() <> 'obys'", 'current_database() <> ' + database_literal)
        migration = migration.replace('public.', schema + '.').replace('pg_catalog, public', 'pg_catalog, ' + schema)
        await first.execute(migration)
        await first.execute("INSERT INTO yeoljeong_businesses VALUES ('a'),('b')")
        async with first.transaction():
            await first.execute("SELECT pg_advisory_xact_lock(hashtextextended('correction:a',0))")
            # Another business proceeds while a is locked; a writer times out.
            await second.execute("INSERT INTO yeoljeong_payroll_statements VALUES ('b','b','paid')")
            async with second.transaction():
                await second.execute("SET LOCAL lock_timeout='150ms'")
                with pytest.raises(svc.asyncpg.LockNotAvailableError):
                    await second.execute("INSERT INTO yeoljeong_payroll_statements VALUES ('a','a','paid')")
        await second.execute("INSERT INTO yeoljeong_payroll_statements VALUES ('a','a','paid')")
        insert = """INSERT INTO obys_attendance_corrections
            (id,tenant_id,business_id,record_id,employee_email,original,requested,reason,supersedes_id,status)
            VALUES ($1,'00000000-0000-0000-0000-000000000001','a',$2,'me@example.com','{}','{}','사유',$3,$4)"""
        await first.execute(insert,'old','r',None,'rejected')
        for supersedes in (None, 'old'):
            # Pending unique index and one-successor index each arbitrate real INSERTs.
            tx = first.transaction()
            await tx.start()
            contender = None
            try:
                await first.execute(insert,'winner'+str(supersedes),'r',supersedes,'pending' if supersedes is None else 'rejected')
                contender = asyncio.create_task(second.execute(insert,'loser'+str(supersedes),'r' if supersedes is None else 'r2',supersedes,'pending' if supersedes is None else 'rejected'))
                await asyncio.sleep(0.05)
                assert not contender.done()
                await tx.commit()
                with pytest.raises(svc.asyncpg.UniqueViolationError):
                    await contender
            finally:
                if contender is not None and not contender.done():
                    await tx.rollback()
                    await contender
    finally:
        await second.close()
        await first.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await first.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,status', [('approve','approved'),('reject','rejected'),('withdraw','withdrawn')])
async def test_same_decision_replay_has_one_effect(db,operation,status):
    db.request.update(status=status,version=2,decided_by='me@example.com',decision_reason='확인')
    result = await svc.run({},'b',operation,correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert result['status'] == status
    svc.ws._attendance_audit.assert_not_awaited()
    assert not any(c.args[0].startswith('UPDATE') for c in db.execute.call_args_list)


@pytest.mark.asyncio
async def test_withdraw_replay_still_checks_owner(db):
    db.request.update(employee_email='other@example.com',status='withdrawn',version=2,
                      decided_by='me@example.com',decision_reason='확인')
    with pytest.raises(HTTPException) as error:
        await svc.run({},'b','withdraw',correction_id='c',payload=svc.DecisionIn(version=1,reason='확인'))
    assert error.value.status_code == 404
    assert not db.committed


@pytest.mark.asyncio
async def test_list_normalizes_login_case(db,monkeypatch):
    monkeypatch.setattr(svc.ws,'_clock_identity',lambda u:{'email':'Me@Example.COM'})
    monkeypatch.setattr(svc.ws.upload_svc,'tenant_session_for_user',lambda u:{'permissions':{'can_manage_settings':False}})
    await svc.run({},'b','list')
    assert 'lower(employee_email)' in db.fetch.await_args.args[0]
    assert db.fetch.await_args.args[-1] == 'me@example.com'
