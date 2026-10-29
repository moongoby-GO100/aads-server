"""같은 쌍 추가지시의 등록, 제한, 순차 배달 회귀시험."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.core import db_pool
from app.services import session_relay as relay

ORIGIN = '00000000-0000-0000-0000-000000000001'
TARGET = '00000000-0000-0000-0000-000000000002'


class Pool:
    def __init__(self):
        self.rows = []
        self.queries = []
        self.locks = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    transaction = acquire

    async def execute(self, sql, *args):
        self.queries.append(sql)
        if 'pg_advisory_xact_lock' in sql:
            self.locks += 1
        if 'INSERT INTO session_relay' in sql:
            self.rows.append(dict(id=args[0], origin=args[1], target=args[2],
                                  question=args[4], goal_id=args[5],
                                  status='queued' if "'queued'" in sql else 'pending'))
        if "SET status='pending'" in sql:
            row = next(r for r in self.rows if r['id'] == args[0])
            if row['status'] != 'queued':
                return 'UPDATE 0'
            row['status'] = 'pending'
            return 'UPDATE 1'
        return 'UPDATE 0'

    async def fetchval(self, sql, *args):
        self.queries.append(sql)
        if 'FROM session_relay' not in sql:
            return '담당'
        pair = [r for r in self.rows if {r['origin'], r['target']} == set(args[:2])]
        if 'md5(question)' in sql:
            digest = lambda q: hashlib.md5(q.encode()).digest()
            return sum(r['status'] in ('pending', 'queued') and not r.get('old') and digest(r['question']) == digest(args[2])
                       for r in pair)
        if '(created_at, id)' in sql:
            assert "status='pending'" in sql and "status='queued'" in sql
            i = next(i for i, r in enumerate(pair) if r['id'] == args[2])
            return sum((r['status'] == 'pending' and not r.get('old')) or (r['status'] == 'queued' and j < i)
                       for j, r in enumerate(pair))
        if "status='queued'" in sql:
            return sum(r['status'] == 'queued' for r in pair)
        assert relay._PAIR_RECENT_SQL in sql
        return sum(r['status'] in ('pending', 'queued') and not r.get('old') for r in pair)

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if "WHERE status = 'queued'" in sql:
            assert 'created_at ASC, id ASC' in sql
            return [r.copy() for r in self.rows if r['status'] == 'queued'][:1]
        return []

    async def fetchrow(self, *args):
        return {'title': '담당', 'role_key': '담당'}


@pytest.fixture
def harness(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(db_pool, 'get_pool', lambda: pool)
    monkeypatch.setattr(relay, '_hop_basis', AsyncMock(return_value=(0, 'direct_turn')))
    monkeypatch.setattr(relay, '_resolve_target', AsyncMock(return_value={
        'id': TARGET, 'title': '담당', 'role_key': 'DataEngineOwner'}))
    monkeypatch.setattr(relay, '_relay_goal', AsyncMock(return_value=None))
    monkeypatch.setattr(relay, '_relay_paused', AsyncMock(return_value=False))
    monkeypatch.setattr(relay, '_target_is_busy', AsyncMock(return_value=False))
    monkeypatch.setattr(relay, '_run_relay', AsyncMock())
    monkeypatch.setattr(relay, 'reconcile_orphan_relays', AsyncMock(return_value={
        'answered': 0, 'failed': 0}))
    pool.rows.append(dict(id='active', origin=TARGET, target=ORIGIN,
                          question='기존 질문', status='pending', goal_id=None))
    return pool


@pytest.mark.asyncio
async def test_pair_inflight_queues_followup(harness):
    out = await relay.ask(ORIGIN, TARGET, '추가 지시')
    assert out['sent'] and out['queued'] and out['reason'] == 'pair_in_flight'
    assert harness.rows[-1]['status'] == 'queued'
    assert harness.rows[-1]['id'] == out['relay_id']
    assert '추가지시' in out['message'] and harness.locks == 1
    relay._run_relay.assert_not_called()


@pytest.mark.asyncio
async def test_queue_limit_and_duplicate_question(harness):
    for i in range(5):
        assert (await relay.ask(ORIGIN, TARGET, f'추가지시 {i}'))['queued']
    out = await relay.ask(ORIGIN, TARGET, '여섯째 지시')
    assert out['sent'] is False and out['error'] == 'pair_queue_full'
    out = await relay.ask(ORIGIN, TARGET, '추가지시 0')
    assert out['sent'] is False and out['error'] == 'duplicate_question'
    out = await relay.ask(ORIGIN, TARGET, '기존 질문')
    assert out['error'] == 'duplicate_question'
    assert len(harness.rows) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['answered', 'failed', 'expired'])
async def test_pending_blocks_then_terminal_releases_fifo(harness, terminal):
    first = await relay.ask(ORIGIN, TARGET, '먼저')
    second = await relay.ask(ORIGIN, TARGET, '나중')
    assert (await relay.dispatch_queued_relays())['sent'] == 0
    relay._run_relay.assert_not_called()
    harness.rows[0]['status'] = terminal
    assert (await relay.dispatch_queued_relays())['sent'] == 1
    await asyncio.sleep(0)
    assert relay._run_relay.call_args.args[0] == first['relay_id']
    assert (await relay.dispatch_queued_relays())['sent'] == 0
    harness.rows[1]['status'] = 'answered'
    assert (await relay.dispatch_queued_relays())['sent'] == 1
    await asyncio.sleep(0)
    assert relay._run_relay.call_args.args[0] == second['relay_id']


@pytest.mark.asyncio
async def test_old_pending_does_not_block_ask(harness):
    harness.rows[0]['old'] = True
    out = await relay.ask(ORIGIN, TARGET, '기존 질문')
    assert out['sent'] and not out.get('queued')
    assert harness.rows[-1]['status'] == 'pending'
    await asyncio.sleep(0)
    relay._run_relay.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_pending_does_not_block_drainer(harness):
    out = await relay.ask(ORIGIN, TARGET, '추가지시')
    harness.rows[0]['old'] = True
    assert (await relay.dispatch_queued_relays())['sent'] == 1
    await asyncio.sleep(0)
    assert relay._run_relay.call_args.args[0] == out['relay_id']
    queries = [q for q in harness.queries if '(created_at, id)' in q]
    assert queries and all(relay._PAIR_RECENT_SQL in q for q in queries)
