"""승인 카드 동기화와 증거의 원자성, 검토 대기 카드의 정리 요청 제외."""
import ast
import asyncio
import copy
import importlib.util
import hashlib
import json
import re
import sqlite3
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def review(monkeypatch):
    log = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    # 최소 호스트 환경에서도 실행하되 런타임 의존성이 있으면 실제 모듈을 쓴다.
    for name, fallback in [
        ('structlog', types.SimpleNamespace(get_logger=lambda *a: log)),
        ('asyncpg', types.SimpleNamespace(UndefinedColumnError=type('UndefinedColumnError', (Exception,), {}))),
    ]:
        try:
            __import__(name)
        except ModuleNotFoundError:
            monkeypatch.setitem(sys.modules, name, fallback)
    import app.services as services
    manager = types.SimpleNamespace(goal_state_machine=types.SimpleNamespace(_update_goal_progress=AsyncMock()))
    monkeypatch.setitem(sys.modules, 'app.services.goal_manager', manager)
    monkeypatch.setattr(services, 'goal_manager', manager, raising=False)
    monkeypatch.setitem(sys.modules, 'app.services.goal_workflow_approval', types.SimpleNamespace(append_event=AsyncMock()))
    dispatch = types.SimpleNamespace(note_record_reset=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, 'app.services.goal_dispatch', dispatch)
    monkeypatch.setattr(services, 'goal_dispatch', dispatch, raising=False)
    spec = importlib.util.spec_from_file_location('review_under_test', ROOT / 'app/services/milestone_review.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Conn:
    def __init__(self, evidence, fail=False):
        self.milestone = dict(status='review', title='M1', goal_id='g', evidence=evidence,
                              dispatched_at=None, dispatch_count=0)
        self.cards = [dict(id=k, parent_id=None, type=t, status='in_review', tenant_id='tenant',
                           project='AADS', goal_id='g', version=1) for k, t in [('e', 'epic'), ('s', 'story')]]
        self.evidence = []
        self.fail = fail

    @asynccontextmanager
    async def transaction(self):
        saved = copy.deepcopy((self.cards, self.evidence))
        try:
            yield
        except Exception:
            self.cards, self.evidence = saved
            raise

    async def fetchrow(self, sql, *args):
        return dict(self.milestone)

    async def fetchval(self, sql, *args):
        return 'm'

    async def fetch(self, sql, *args):
        if "status = 'in_review'" in sql:
            return [dict(c) for c in self.cards if c['status'] == 'in_review']
        return [dict(c) for c in self.cards]

    async def execute(self, sql, *args):
        if 'INSERT INTO work_item_evidence' in sql:
            if self.fail and len(self.evidence) >= self.fail - 1:
                raise RuntimeError('evidence insert failed')
            assert 'work_item_version' in sql and 'criterion_key' in sql
            assert 'sha256:' in sql and "'verified'" in sql and "'audit'" in sql
            assert 'NOT EXISTS' in sql and 'IS NOT DISTINCT FROM' in sql
            item, mid, kind, uri, payload = args
            key = (item, mid, uri)
            if not any(e['key'] == key for e in self.evidence):
                self.evidence.append(dict(key=key, kind=kind, payload=json.loads(payload)))
            return 'INSERT 0 1'
        if 'UPDATE work_items' in sql:
            card = next(c for c in self.cards if c['id'] == args[0])
            card.update(status=args[1], version=card['version'] + 1)
            return 'UPDATE 1'
        if 'UPDATE milestones' in sql:
            self.milestone['status'] = args[1] if 'status = $2' in sql else 'in_progress'
        return 'UPDATE 1'


@pytest.mark.parametrize('jobs', [[], ['runner-1234abcd'], ['runner-1234abcd', 'runner-8765abcd']])
def test_confirm_evidence_and_retry(review, monkeypatch, jobs):
    conn = Conn(dict(summary='검증한 결과 요약', refs=jobs + jobs))
    install_pool(monkeypatch, conn)
    result = asyncio.run(review.confirm('m', True, confirmer='CEO'))
    assert result['status'] == 'completed'
    assert all(c['status'] == 'completed' for c in conn.cards)
    assert len(conn.evidence) == 2 * max(1, len(jobs))
    assert {e['kind'] for e in conn.evidence} == ({'job'} if jobs else {'rollup'})
    for e in conn.evidence:
        assert e['payload']['summary'] == '검증한 결과 요약'
        assert e['payload']['milestone_id'] == 'm'
        assert e['payload']['confirmer'] == 'CEO'
        assert e['payload']['approved_at']
    # 정상 재시도는 review 게이트로 거절되고, 재신고·재승인도 증거를 복제하지 않는다.
    assert asyncio.run(review.confirm('m', True))['error'] == 'not_in_review'
    conn.milestone['status'] = 'review'
    for c in conn.cards:
        c['status'] = 'in_review'
    asyncio.run(review.confirm('m', True))
    assert len(conn.evidence) == 2 * max(1, len(jobs))


def install_pool(monkeypatch, conn):
    @asynccontextmanager
    async def acquire():
        yield conn
    pool = types.SimpleNamespace(acquire=acquire)
    import app.core as core
    module = types.SimpleNamespace(get_pool=lambda: pool)
    monkeypatch.setitem(sys.modules, 'app.core.db_pool', module)
    monkeypatch.setattr(core, 'db_pool', module, raising=False)


@pytest.mark.parametrize('fail_at', [1, 2])
def test_insert_failure_rolls_back_cards_but_approves(review, monkeypatch, fail_at):
    conn = Conn({'summary': '검증 결과'}, fail=fail_at)
    install_pool(monkeypatch, conn)
    assert asyncio.run(review.confirm('m', True))['status'] == 'completed'
    assert conn.milestone['status'] == 'completed'
    assert all(c['status'] == 'in_review' for c in conn.cards)
    assert conn.evidence == []


def test_reject_has_no_evidence(review, monkeypatch):
    conn = Conn({'summary': '검증 결과'})
    install_pool(monkeypatch, conn)
    assert asyncio.run(review.confirm('m', False))['status'] == 'in_progress'
    assert all(c['status'] == 'changes_requested' for c in conn.cards)
    assert conn.evidence == []


@pytest.mark.parametrize('milestone,state,expected', [
    ('review', 'in_review', 0), ('review', 'ready', 1),
    ('review', 'draft', 1), ('in_progress', 'in_review', 1),
    ('blocked', 'in_review', 1), ('failed', 'in_review', 1),
    ('review', 'completed', 0), ('review', 'cancelled', 0),
])
def test_triage_sql_predicates(milestone, state, expected):
    tree = ast.parse((ROOT / 'app/services/goal_dispatch.py').read_text())
    sqls = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in ('_TRIAGE_CANDIDATE_SQL', '_TRIAGE_ITEMS_SQL')}
    db = sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE milestones(id TEXT,status TEXT); CREATE TABLE work_items(id TEXT,milestone_id TEXT,assignment_id TEXT,status TEXT,type TEXT,title TEXT,created_at TEXT);')
    db.execute('INSERT INTO milestones VALUES (?,?)', ('m', milestone))
    db.execute('INSERT INTO work_items VALUES (?, ?, NULL, ?, ?, ?, ?)', ('w', 'm', state, 'story', 'card', 'now'))
    candidate = sqls['_TRIAGE_CANDIDATE_SQL']
    subquery = candidate[candidate.index('(SELECT count(*)') + 1:candidate.index(' AS unassigned_count') - 1]
    assert db.execute('SELECT (' + subquery + ') FROM milestones m').fetchone()[0] == expected
    items = re.sub(r'\$1::uuid', '?', sqls['_TRIAGE_ITEMS_SQL'])
    assert len(db.execute(items, ('m',)).fetchall()) == expected


@pytest.mark.parametrize('refs,kind,count', [([], 'rollup', 1), (['runner-1234abcd'] * 2, 'job', 1),
                                           (['runner-1234abcd', 'runner-8765abcd'], 'job', 2)])
def test_evidence_insert_sql_is_idempotent_and_satisfies_foundation(review, refs, kind, count):
    # PostgreSQL 캐스트만 제거하고 실제 INSERT/NOT EXISTS를 SQLite에서 실행한다.
    db = sqlite3.connect(':memory:')
    db.executescript("""
        CREATE TABLE work_items(id TEXT, milestone_id TEXT, tenant_id TEXT, project TEXT,
                                goal_id TEXT, version INTEGER, status TEXT);
        INSERT INTO work_items VALUES ('w', 'm', 't', 'AADS', 'g', 2, 'completed');
        CREATE TABLE work_item_evidence(tenant_id TEXT, project TEXT, goal_id TEXT,
            work_item_id TEXT, evidence_type TEXT, uri TEXT, payload TEXT, verified BOOLEAN,
            work_item_version INTEGER, criterion_key TEXT, content_hash TEXT,
            verification_state TEXT, verified_at TEXT, retention_class TEXT);
    """)
    db.create_function('convert_to', 2, lambda value, encoding: value.encode(encoding))
    db.create_function('sha256', 1, lambda value: hashlib.sha256(value).digest())
    db.create_function('encode', 2, lambda value, encoding: value.hex())
    db.create_function('NOW', 0, lambda: '2026-11-06T00:00:00Z')

    class SqlConn:
        async def execute(self, sql, *args):
            sql = re.sub(r'::(?:text|jsonb|uuid)', '', sql)
            db.execute(sql, {str(i): arg for i, arg in enumerate(args, 1)})

    for _ in range(2):
        asyncio.run(review._record_confirmation_evidence(
            SqlConn(), 'm', [{'id': 'w'}], {'summary': '검증 결과', 'refs': refs}, 'CEO'))
    rows = db.execute('SELECT evidence_type, work_item_version, criterion_key, content_hash, '
                      'verified, verification_state, verified_at, retention_class, payload '
                      'FROM work_item_evidence').fetchall()
    assert len(rows) == count
    for row in rows:
        assert row[:3] == (kind, 2, 'milestone:m')
        assert row[3] == 'sha256:' + hashlib.sha256(row[8].encode()).hexdigest()
        assert row[4:8] == (1, 'verified', '2026-11-06T00:00:00Z', 'audit')
