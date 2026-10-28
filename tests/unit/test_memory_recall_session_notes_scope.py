"""현재 세션 요약의 우선순위와 tenant 경계를 검증한다."""
import asyncio
from datetime import datetime, timedelta
import re
from uuid import UUID

from app.core import memory_recall

SESSION = UUID('8bf0405a-0000-0000-0000-000000000000')
OTHER = UUID('009efbfa-0000-0000-0000-000000000000')
TENANT = '11111111-1111-1111-1111-111111111111'
FOREIGN = '22222222-2222-2222-2222-222222222222'


def note(summary, kind='compaction', session=SESSION, tenant=TENANT, age=0, projects=None):
    return dict(summary=summary, note_type=kind, session_id=session, tenant_id=tenant,
                user_id='ceo', created_at=datetime(2026, 10, 29) - timedelta(hours=age),
                key_decisions=[], projects_discussed=projects)


class Conn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetchrow(self, query, session):
        return dict(tenant_id=TENANT, user_id='ceo')

    async def fetch(self, query, *params):
        self.queries.append(query)
        # Assert the security/filter clauses before emulating PostgreSQL selection.
        assert 'JOIN chat_sessions cs ON sn.session_id = cs.id' in query
        assert 'cs.tenant_id = $1::uuid' in query
        assert "sn.note_type IS DISTINCT FROM 'compaction_hash'" in query
        assert "!~ '^[0-9A-Fa-f]{32}$'" in query
        assert 'cs.user_id = $3::text' in query
        assert query.index('CASE WHEN sn.session_id = $2::uuid THEN 0') < query.index("sn.note_type = 'compaction'")
        tenant, session, user, *project = params
        rows = [r for r in self.rows if isinstance(r['session_id'], UUID)
                and r['tenant_id'] == tenant and r['user_id'] == user
                and r['note_type'] != 'compaction_hash'
                and not re.fullmatch('[0-9a-fA-F]{32}', r['summary'].strip())
                and (not project or r['session_id'] == session
                     or r['projects_discussed'] is None or project[0] in r['projects_discussed'])]
        return sorted(rows, key=lambda r: (
            r['session_id'] != session,
            not (r['session_id'] == session and r['note_type'] == 'compaction'),
            bool(project and r['session_id'] != session and project[0] not in (r['projects_discussed'] or [])),
            -r['created_at'].timestamp(),
        ))[:3]


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


def recall(monkeypatch, rows, **kwargs):
    conn = Conn(rows)
    monkeypatch.setattr(memory_recall, '_get_pool', lambda: Pool(conn))
    return asyncio.run(memory_recall._build_session_notes(
        session_id=str(SESSION), tenant_id=TENANT, user_id='ceo', **kwargs))


def test_current_session_and_compaction_precede_recent_foreign_notes(monkeypatch):
    text = recall(monkeypatch, [note('다른 세션', session=OTHER),
                               note('질문 조각', kind='auto_save'),
                               note('현재 목표 CEO 지시 미완료', age=100)])
    assert text.index('현재 목표') < text.index('질문 조각') < text.index('다른 세션')


def test_hashes_filtered_before_limit(monkeypatch):
    text = recall(monkeypatch, [note('변경 감지', kind='compaction_hash'),
                               note('a' * 32), note(' ABCDEF0123456789ABCDEF0123456789 '),
                               note('유효한 요약', age=10)])
    assert '유효한 요약' in text
    assert '변경 감지' not in text and 'a' * 32 not in text and 'ABCDEF' not in text


def test_fallback_when_current_session_empty(monkeypatch):
    assert '보충 요약' in recall(monkeypatch, [note('보충 요약', session=OTHER)])


def test_other_tenant_and_string_notes_excluded(monkeypatch):
    assert recall(monkeypatch, [note('유출 금지', tenant=FOREIGN),
                               note('저장 노트', session='note_123')]) == ''


def test_full_current_session_does_not_include_fallback(monkeypatch):
    text = recall(monkeypatch, [note(f'현재 요약 {i}', age=i) for i in range(3)]
                  + [note('보충 금지', session=OTHER)])
    assert '보충 금지' not in text
    assert all(f'현재 요약 {i}' in text for i in range(3))


def test_long_first_line_keeps_prefix_and_budget(monkeypatch):
    prefix = '현재 목표·CEO 지시·미완료: '
    text = recall(monkeypatch, [note(prefix + '가' * 5759, age=100), note('최근 질문', kind='auto_save')])
    assert prefix in text and len(text) == 750
    assert '최근 질문' not in text


def test_current_project_mismatch_still_precedes_project_fallback(monkeypatch):
    text = recall(monkeypatch, [note('현재 요약', projects=['OTHER']),
                               note('프로젝트 보충', session=OTHER, projects=['AADS'])], project_id='AADS')
    assert text.index('현재 요약') < text.index('프로젝트 보충')


def test_missing_tenant_fails_closed(monkeypatch):
    conn = Conn([note('전역 유출 금지')])
    monkeypatch.setattr(memory_recall, '_get_pool', lambda: Pool(conn))
    assert asyncio.run(memory_recall._build_session_notes()) == ''
    assert not conn.queries


def test_scope_inferred_from_session(monkeypatch):
    conn = Conn([note('현재 세션 범위')])
    monkeypatch.setattr(memory_recall, '_get_pool', lambda: Pool(conn))
    assert '현재 세션 범위' in asyncio.run(memory_recall._build_session_notes(session_id=str(SESSION)))


def test_current_auto_saves_use_recency_across_project_tags(monkeypatch):
    text = recall(monkeypatch, [note('오래된 질문', kind='auto_save', projects=['AADS'], age=2),
                               note('최신 질문', kind='auto_save', projects=['OTHER'])], project_id='AADS')
    assert text.index('최신 질문') < text.index('오래된 질문')


def test_explicit_tenant_cannot_recall_foreign_current_session(monkeypatch):
    text = recall(monkeypatch, [note('외부 현재 세션', tenant=FOREIGN),
                               note('같은 tenant 보충', session=OTHER)])
    assert '외부 현재 세션' not in text
    assert '같은 tenant 보충' in text
