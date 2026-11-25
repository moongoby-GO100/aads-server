"""운영 변경과 승인 게이트 변경은 기존 포괄 승인으로 재주입하지 않는다."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import next_step_proposals as nsp


@pytest.fixture
def setup_gate(monkeypatch):
    from app.core import db_pool

    conn = SimpleNamespace(fetchval=AsyncMock(side_effect=lambda sql, *a:
        'card-id' if 'INSERT INTO' in sql else None))

    class Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(db_pool, 'get_pool', lambda: SimpleNamespace(acquire=Acquire))
    monkeypatch.setattr(nsp, '_current_bubble_id', AsyncMock(return_value=''))
    monkeypatch.setattr(nsp, '_session_project', AsyncMock(return_value='GO100'))
    grant = AsyncMock(return_value='grant-12345678')
    goal = AsyncMock(return_value='goal-12345678')  # auto_approve_high=true 相当
    enqueue = AsyncMock(return_value={'queue_id': 'q', 'queue_status': 'pending', 'created': True})
    monkeypatch.setattr(nsp, '_covered_by_existing_grant', grant)
    monkeypatch.setattr(nsp, '_goal_policy_covers', goal)
    monkeypatch.setitem(sys.modules, 'app.services.chat_service',
                        SimpleNamespace(enqueue_next_step_reaction=enqueue))
    return conn, grant, goal, enqueue


@pytest.mark.parametrize('grant_present', [True, False])
@pytest.mark.parametrize('risk', ['low', 'medium'])
def test_live_restart_requires_card(setup_gate, grant_present, risk):
    conn, grant, goal, enqueue = setup_gate
    grant.return_value = 'grant-12345678' if grant_present else None
    result = asyncio.run(nsp.propose('session', [{
        'title': 'go100-card310 drop-in을 새 폴더로 교체하고 재기동',
        'tool': 'run_remote_command', 'risk': risk, 'rollback': '',
    }], tenant_id='tenant'))
    assert result['cards'] and not result['auto']
    grant.assert_not_awaited()
    goal.assert_not_awaited()
    enqueue.assert_not_awaited()
    insert = next(c for c in conn.fetchval.call_args_list if 'INSERT INTO' in c.args[0])
    assert insert.args[5] == 'high'
    assert '되돌리는 법 미기재 — 실행 전 확인 필요' in insert.args[4]
    assert "'approve'" in insert.args[0]


@pytest.mark.parametrize('grant_present', [True, False])
def test_readonly_query_still_auto(setup_gate, grant_present):
    _, grant, goal, enqueue = setup_gate
    grant.return_value = 'grant-12345678' if grant_present else None
    result = asyncio.run(nsp.propose('session', [{
        'title': 'DB에서 주문 건수 조회', 'tool': 'query_database',
    }], tenant_id='tenant'))
    assert result['auto'] and not result['cards']
    enqueue.assert_awaited_once()
    assert '되돌릴 필요 없음(읽기/조사)' in enqueue.call_args.args[1]
    assert goal.await_count == (0 if grant_present else 1)


@pytest.mark.parametrize('title', [
    '승인 게이트 구현 수정', 'live_trading_guard 수정',
    'next_step_proposals 수정', 'app/api/project_docs.py 승인 로직 변경',
])
def test_gate_change_requires_card(setup_gate, title):
    _, grant, goal, enqueue = setup_gate
    result = asyncio.run(nsp.propose('session', [{'title': title}], tenant_id='tenant'))
    assert result['cards'] and not result['auto']
    grant.assert_not_awaited()
    goal.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.parametrize('marker', [
    'restart', 'systemctl', 'daemon-reload', '배포', 'deploy', '릴리스 교체',
    '주문 실행', '실매매', 'card119', 'live_engine',
])
def test_operational_markers(marker):
    assert nsp.is_operational_change('', marker, '')


@pytest.mark.parametrize('tool', [
    'run_remote_command', 'write_remote_file', 'patch_remote_file', 'db_safe_write', 'deploy_safe',
])
def test_unproven_readonly_tools(tool):
    assert nsp.is_operational_change('점검', '', tool)


def test_explicit_rollback_preserved():
    step = nsp._normalize({'title': 'card310 재기동', 'rollback': '이전 설정 복구'}, 0)
    assert '이전 설정 복구' in nsp._summary_of(step, '')


def test_operational_marker_survives_display_truncation(setup_gate):
    _, grant, goal, _ = setup_gate
    result = asyncio.run(nsp.propose('session', [{
        'title': '조회 ' + '가' * 210 + ' restart', 'tool': 'query_database',
    }], tenant_id='tenant'))
    assert result['cards'] and not result['auto']
    grant.assert_not_awaited()
    goal.assert_not_awaited()
