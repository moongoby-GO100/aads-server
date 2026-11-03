"""상태 변경 fixture로 월간 패턴 교체 검증."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.api import obys_workspaces as api
from tests.unit import test_obys_work_schedules as _schedules

client = _schedules.client


@pytest_asyncio.fixture
async def stateful(client, monkeypatch):
    http, user, db = client
    user['current_membership']['role'] = 'owner'
    db.rows = []
    original = db.fetchrow
    # Transactions do not serialize themselves. Only production advisory-lock
    # statements acquire the matching employee lock, held until transaction exit.
    locks = {}
    current = ContextVar('schedule_transaction')
    db.lock_contended = asyncio.Event()
    original_fetch = db.fetch
    original_execute = db.execute

    def require_locked(business, emails):
        tx = current.get()
        assert all(f'obys-schedule:{business}:{email}' in tx['held'] for email in emails)

    @asynccontextmanager
    async def transaction():
        tx = {'held': {}, 'before': {}}
        token = current.set(tx)
        try:
            yield
        except Exception:
            for (business, email), before in tx['before'].items():
                db.rows[:] = [r for r in db.rows if
                              (r['business_id'], r['employee_email']) != (business, email)]
                db.rows.extend(before)
            raise
        finally:
            for lock in reversed(list(tx['held'].values())):
                lock.release()
            current.reset(token)

    async def execute(sql, *args):
        if 'pg_advisory_xact_lock' not in sql:
            return await original_execute(sql, *args)
        tx = current.get()
        key = args[0]
        if key not in tx['held']:
            lock = locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                db.lock_contended.set()
            await lock.acquire()
            tx['held'][key] = lock
            _, business, email = key.split(':', 2)
            tx['before'][(business, email)] = deepcopy([
                r for r in db.rows if (r['business_id'], r['employee_email']) == (business, email)])
        return 'SELECT 1'

    async def fetch(sql, business, *args):
        if 'UPDATE yeoljeong_work_schedules SET deleted_at' in sql:
            require_locked(business, args[0])
        return await original_fetch(sql, business, *args)

    async def insert(sql, *args):
        if not sql.lstrip().startswith('INSERT'):
            return await original(sql, *args)
        require_locked(args[1], [args[2]])
        incoming = await original(sql, *args)
        row = next((r for r in db.rows if not r.get('deleted_at') and
                    all(r[k] == incoming[k] for k in
                        ('business_id', 'employee_email', 'work_date'))), None)
        if row is not None:
            if 'DO NOTHING' in sql:
                return None
            row.update({k: v for k, v in incoming.items() if k != 'id'})
        else:
            row = incoming
            db.rows.append(row)
        return dict(row)

    monkeypatch.setattr(db, 'execute', execute)
    monkeypatch.setattr(db, 'fetch', fetch)
    monkeypatch.setattr(db, 'fetchrow', insert)
    monkeypatch.setattr(db, 'transaction', transaction)
    return http, db


def pattern(**updates):
    return {'employees': [{'employee_email': 'fixture@example.test'}],
            'date_from': '2026-11-01', 'date_to': '2026-11-30',
            'weekdays': [0], 'start_time': '21:00', 'end_time': '09:00', **updates}


def active(db):
    return [r for r in db.rows if not r.get('deleted_at')]


@pytest.mark.asyncio
async def test_month_replace_is_idempotent_and_scoped(stateful):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    assert (await http.post(path, json=pattern(weekdays=list(range(7))))).status_code == 200
    sentinel = deepcopy(db.rows[0])
    for changes in ({'business_id': 'other'}, {'employee_email': 'other@example.test'},
                    {'work_date': date(2026, 12, 1)}, {'memo': 'manual exception'},
                    {'kind': 'leave'}, {'kind': 'off'}):
        db.rows.append({**sentinel, **changes, 'id': str(changes)})
    protected = deepcopy(db.rows[30:])
    actual = {'check_in': 'unchanged', 'confirmed_payroll': 123}
    db.rows[1]['actual_record'] = deepcopy(actual)
    responses = await asyncio.gather(*(http.post(path, json=pattern()) for _ in range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert db.rows[30:] == protected
    plans = [r for r in active(db) if r not in db.rows[30:]]
    assert [r['work_date'] for r in plans] == [date(2026, 11, d) for d in (2, 9, 16, 23, 30)]
    assert all(str(r['start_time']) == '21:00:00' and str(r['end_time']) == '09:00:00' for r in plans)
    assert db.rows[1]['actual_record'] == actual
    assert len({(r['business_id'], r['employee_email'], r['work_date']) for r in plans}) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['preserve', 'cells'])
async def test_selected_cells_and_keep_do_not_retire_other_days(stateful, mode):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    await http.post(path, json=pattern(weekdays=list(range(7))))
    before = deepcopy(db.rows)
    extra = {'preserve_existing': True} if mode == 'preserve' else {
        'cells': [{'employee_email': 'fixture@example.test', 'work_date': '2026-11-02',
                   'start_time': '21:00', 'end_time': '09:00'}]}
    response = await http.post(path, json=pattern(**extra))
    assert response.status_code == 200
    assert len(active(db)) == 30
    if mode == 'preserve':
        assert db.rows == before


@pytest.mark.asyncio
async def test_wednesday_to_sunday_keeps_original_times(stateful):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    await http.post(path, json=pattern(weekdays=list(range(7)), start_time='10:00', end_time='19:00'))
    response = await http.post(path, json=pattern(weekdays=[2, 3, 4, 5, 6], start_time='10:00', end_time='19:00'))
    assert response.status_code == 200
    assert len(active(db)) == 21
    assert all(r['work_date'].weekday() >= 2 and str(r['start_time']) == '10:00:00' for r in active(db))


def test_lock_is_employee_scoped_and_ordered():
    db = AsyncMock()
    asyncio.run(api._lock_schedule_employees(db, 'business', [' B@test ', 'a@test', 'a@test']))
    assert [c.args[1] for c in db.execute.await_args_list] == [
        'obys-schedule:business:a@test', 'obys-schedule:business:b@test']
    assert all('pg_advisory_xact_lock' in c.args[0] for c in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_failed_save_rolls_back_retired_days(stateful, monkeypatch):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    await http.post(path, json=pattern(weekdays=list(range(7))))
    before = deepcopy(db.rows)
    monkeypatch.setattr(api, '_save_work_schedule', AsyncMock(side_effect=RuntimeError('fixture failure')))
    with pytest.raises(RuntimeError, match='fixture failure'):
        await http.post(path, json=pattern())
    assert db.rows == before


@pytest.mark.asyncio
async def test_duplicate_employees_do_not_duplicate_active_plans(stateful):
    http, db = stateful
    response = await http.post(_schedules.BASE + '/bulk', json=pattern(
        employees=[{'employee_email': 'fixture@example.test'},
                   {'employee_email': ' FIXTURE@example.test '}]))
    assert response.status_code == 200
    assert len(active(db)) == 5


@pytest.mark.asyncio
async def test_excluded_manual_days_and_same_name_employee_are_preserved(stateful):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    assert (await http.post(path, json=pattern(weekdays=list(range(7)), employees=[{
        'employee_email': 'fixture@example.test', 'employee_name': '동명이인'}]))).status_code == 200
    # Modify real, unique cells rather than adding impossible duplicate active keys.
    for day, changes in [(3, {'memo': 'manual exception'}),
                         (4, {'kind': 'off'}), (5, {'kind': 'leave'})]:
        cell = {'employee_email': 'fixture@example.test', 'work_date': f'2026-11-{day:02}',
                'start_time': '10:00', 'end_time': '19:00', **changes}
        assert (await http.post(path, json=pattern(cells=[cell]))).status_code == 200
    other = pattern(employees=[{'employee_email': 'other@example.test',
                                'employee_name': '동명이인'}], weekdays=list(range(7)))
    assert (await http.post(path, json=other)).status_code == 200
    protected = deepcopy([r for r in db.rows if r['memo'] or r['kind'] != 'work'
                          or r['employee_email'] == 'other@example.test'])
    assert (await http.post(path, json=pattern())).status_code == 200
    by_id = {r['id']: r for r in db.rows}
    assert all(by_id[r['id']] == r for r in protected)
    assert len(active(db)) == 38  # five Mondays, three exceptions, thirty other plans


@pytest.mark.asyncio
async def test_retirement_audit_failure_rolls_back_entire_pattern(stateful, monkeypatch):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    assert (await http.post(path, json=pattern(weekdays=list(range(7))))).status_code == 200
    before = deepcopy(db.rows)
    monkeypatch.setattr(api, '_attendance_audit', AsyncMock(side_effect=RuntimeError('audit failure')))
    with pytest.raises(RuntimeError, match='audit failure'):
        await http.post(path, json=pattern())
    assert db.rows == before


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['off', 'leave'])
async def test_non_work_pattern_preserves_all_other_days(stateful, kind):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    assert (await http.post(path, json=pattern(weekdays=list(range(7))))).status_code == 200
    before = deepcopy([r for r in db.rows if r['work_date'].weekday() != 6])
    response = await http.post(path, json=pattern(weekdays=[6], kind=kind))
    assert response.status_code == 200
    assert len(active(db)) == 30
    assert [r for r in db.rows if r['work_date'].weekday() != 6] == before
    assert all(r['kind'] == kind for r in active(db) if r['work_date'].weekday() == 6)


@pytest.mark.asyncio
async def test_different_patterns_wait_for_employee_transaction(stateful, monkeypatch):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    entered, release = asyncio.Event(), asyncio.Event()
    original = api._retire_excluded_schedule_days

    async def pause_first(connection, business, payload, user):
        await original(connection, business, payload, user)
        if payload.weekdays == [0]:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)

    monkeypatch.setattr(api, '_retire_excluded_schedule_days', pause_first)
    first = asyncio.create_task(http.post(path, json=pattern()))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(http.post(path, json=pattern(weekdays=[2, 3, 4, 5, 6])))
        await asyncio.wait_for(db.lock_contended.wait(), 3)
        assert not second.done()
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second else []))
    assert all(r.status_code == 200 for r in results)
    assert len(active(db)) == 21
    assert all(r['work_date'].weekday() >= 2 for r in active(db))


@pytest.mark.asyncio
async def test_fixture_rejects_missing_production_lock(stateful, monkeypatch):
    http, _db = stateful
    monkeypatch.setattr(api, '_lock_schedule_employees', AsyncMock())
    with pytest.raises(AssertionError):
        await http.post(_schedules.BASE + '/bulk', json=pattern())


@pytest.mark.asyncio
async def test_other_employee_does_not_wait_for_busy_employee(stateful, monkeypatch):
    http, db = stateful
    path = _schedules.BASE + '/bulk'
    entered, release = asyncio.Event(), asyncio.Event()
    original = api._retire_excluded_schedule_days

    async def pause_employee(connection, business, payload, user):
        await original(connection, business, payload, user)
        if payload.employees[0].employee_email == 'fixture@example.test':
            entered.set()
            await asyncio.wait_for(release.wait(), 3)

    monkeypatch.setattr(api, '_retire_excluded_schedule_days', pause_employee)
    first = asyncio.create_task(http.post(path, json=pattern()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        other = await asyncio.wait_for(http.post(path, json=pattern(
            employees=[{'employee_email': 'other@example.test'}])), 2)
        assert other.status_code == 200
        assert not first.done()
    finally:
        release.set()
        result = await first
    assert result.status_code == 200
    assert len(active(db)) == 10


@pytest.mark.asyncio
async def test_postgres_concurrent_pattern_replacement(client, monkeypatch):
    """Use independent real connections and observe the blocked advisory lock."""
    import os
    from pathlib import Path
    from uuid import uuid4

    import asyncpg

    dsn = os.getenv('AADS_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('AADS_TEST_POSTGRES_DSN not configured')
    http, user, _db = client
    user['current_membership']['role'] = 'owner'
    schema = 'schedule_fixture_' + uuid4().hex
    admin = await asyncpg.connect(dsn, timeout=5)
    entered, release = asyncio.Event(), asyncio.Event()
    connections, tasks = [], []
    original = api._retire_excluded_schedule_days

    async def connect():
        conn = await asyncpg.connect(dsn, timeout=5)
        await conn.execute(f'SET search_path TO "{schema}"')
        connections.append(conn)
        return conn

    async def pause_first(conn, business, payload, actor):
        await original(conn, business, payload, actor)
        if payload.weekdays == [0]:
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        migration = Path('migrations/20261030_obys_work_schedules.sql').read_text()
        ddl = migration[migration.index('CREATE TABLE'):migration.index('CREATE INDEX')]
        await admin.execute(ddl.replace(' REFERENCES yeoljeong_businesses(id)', ''))
        monkeypatch.setattr(api.upload_svc, '_connect', connect)
        monkeypatch.setattr(api, '_attendance_audit', AsyncMock())
        monkeypatch.setattr(api, '_retire_excluded_schedule_days', pause_first)
        path = _schedules.BASE + '/bulk'
        tasks.append(asyncio.create_task(http.post(path, json=pattern())))
        await asyncio.wait_for(entered.wait(), 5)
        tasks.append(asyncio.create_task(http.post(path, json=pattern(weekdays=[2, 3, 4, 5, 6]))))

        async def wait_for_database_lock():
            while True:
                if len(connections) == 2 and await admin.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=$1 "
                    "AND locktype='advisory' AND NOT granted)", connections[1].get_server_pid()
                ):
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_database_lock(), 5)
        assert not tasks[1].done()
        release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
        rows = await admin.fetch('SELECT * FROM yeoljeong_work_schedules WHERE deleted_at IS NULL')
        assert len(rows) == 21
        assert all(r['work_date'].weekday() >= 2 for r in rows)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for conn in connections:
            if not conn.is_closed():
                await conn.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
