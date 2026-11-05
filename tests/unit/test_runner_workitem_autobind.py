"""러너 업무 카드 자동 연결(opt-in 자동 매칭·아카이브 사후 동기화) 단위 테스트."""
import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.goal_work_item_sync import bind_work_items


@pytest.fixture(autouse=True)
def available_schema(monkeypatch):
    monkeypatch.setattr('app.services.goal_work_item_sync.schema_ready', AsyncMock(return_value=True))


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


G, M, T = str(uuid4()), str(uuid4()), str(uuid4())
GOAL_LINE = f'GOAL_ID: {G}\n'


def card(title, key='k', state='draft', kind='task'):
    return dict(id=uuid4(), title=title, idempotency_key=key, status=state, type=kind)


class BindConnection:
    """goal_task_links 한 행·work_items·pipeline_jobs(_archive) 를 흉내 내는 최소 더블."""

    def __init__(self, cards, job=('queued', 'queued'), links=1, archived=None, ids=()):
        self.cards = cards
        self.link = dict(id=uuid4(), goal_id=G, milestone_id=M, tenant_id=T, work_item_ids=list(ids),
                         work_items_synced_status=None)
        self.links = [self.link] * links
        self.job = job
        self.archived = archived
        self.written = None
        self.scoped_updates = 0
        self.reset = 0

    def transaction(self):
        return Transaction()

    async def fetch(self, sql, *args):
        if 'FROM goal_task_links' in sql:
            return self.links
        if 'FROM work_items WHERE id = ANY' in sql:
            return [c | dict(goal_id=G, milestone_id=M, tenant_id=T) for c in self.cards if str(c['id']) in args[0]]
        assert 'FROM work_items w' in sql
        assert args[:3] == (G, M, T) and args[3] == ['task', 'story'] and args[4] == ['draft', 'ready', 'in_progress']
        return [c for c in self.cards if c['type'] in args[3] and c['status'] in args[4]]

    async def fetchrow(self, sql, *args):
        if 'FROM pipeline_jobs_archive' in sql:
            return self.archived
        assert 'FROM pipeline_jobs WHERE' in sql
        return dict(status=self.job[0], phase=self.job[1], tenant_id=T) if self.job else None

    async def execute(self, sql, *args):
        if 'UPDATE goal_task_links l SET work_item_ids = ARRAY(' in sql:
            self.scoped_updates += 1
            # 선언 id 중 이 목표의 task/story 카드만 남긴다(SQL 의 범위 조건을 흉내). ids 가 바뀌면 처리 상태를 비운다.
            kept = list(i for i in args[1]
                         if any(str(c['id']) == i and c['type'] in ('task', 'story') for c in self.cards))
            if set(kept) != {str(i) for i in self.link['work_item_ids']}:
                self.link['work_items_synced_status'] = None
            self.link['work_item_ids'] = kept
        elif 'work_items_synced_status = NULL' in sql and 'SET work_item_ids' in sql:
            self.written = args[2]
            self.link['work_item_ids'] = args[2]
            self.link['work_items_synced_status'] = None
        elif 'work_items_synced_status = NULL' in sql:
            self.reset += 1
            self.link['work_items_synced_status'] = None
        else:
            raise AssertionError(sql)


def bind(conn, instruction, job_id='runner-aaaa1111'):
    return asyncio.run(bind_work_items(conn, job_id, instruction))


def test_directive_single_and_multiple():
    a, b = card('가'), card('나')
    conn = BindConnection([a, b])
    out = bind(conn, f'{GOAL_LINE}WORK_ITEM_ID: {a["id"]}')
    assert out == {'bound': True, 'source': 'directive', 'work_item_ids': [str(a['id'])]}
    out = bind(conn, f'{GOAL_LINE}WORK_ITEM_ID: {a["id"]}, {b["id"]}')
    assert conn.link['work_item_ids'] == [str(a['id']), str(b['id'])] and out['source'] == 'directive'


def test_directive_unknown_and_epic_ids_are_ignored_but_story_is_bound(caplog):
    good, story, epic = card('가'), card('스토리', kind='story'), card('에픽', kind='epic')
    conn = BindConnection([good, story, epic])
    missing = str(uuid4())
    with caplog.at_level('WARNING'):
        out = bind(conn, f'{GOAL_LINE}WORK_ITEM_ID: {missing}, {epic["id"]}, {story["id"]}, {good["id"]}')
    assert out['bound'] and sorted(out['work_item_ids']) == sorted([str(good['id']), str(story['id'])])
    assert {i['id']: i['reason'] for i in out['ignored']} == {
        missing: 'not_found', str(epic['id']): 'type_epic_not_bindable'}
    assert caplog.text.count('work_item_directive_id_ignored') == 2


def test_directive_all_invalid_does_not_fall_back_to_auto_match():
    conn = BindConnection([card('러너 업무 카드 자동 연결 작업')])
    out = bind(conn, f'{GOAL_LINE}TITLE: 러너 업무 카드 자동 연결 작업\nWORK_ITEM_ID: {uuid4()}')
    assert out['bound'] is False and out['reason'] == 'directive_ids_not_found'
    assert conn.written is None


def test_auto_match_single_by_title_and_task_id_and_runner_id():
    target = card('[P1/M] 러너 제출 시 업무 카드 자동 연결', key='x')
    other = card('전혀 다른 작업 카드입니다', key='y')
    for text in ('TITLE: 러너 제출 시 업무 카드 자동 연결',
                 'TASK_ID: AADS-X\nTITLE: [P2/S] 러너 제출 시 업무 카드 자동 연결'):
        conn = BindConnection([target, other])
        out = bind(conn, GOAL_LINE + text)
        assert out == {'bound': True, 'source': 'auto_match', 'work_item_ids': [str(target['id'])]}
        assert conn.written == [str(target['id'])]
    keyed = card('이름이 다른 카드', key='AADS-RUNNER-AUTOBIND-20261106')
    conn = BindConnection([keyed, other])
    assert bind(conn, GOAL_LINE + 'TASK_ID: AADS-RUNNER-AUTOBIND-20261106')['work_item_ids'] == [str(keyed['id'])]
    mention = card('재작업 (runner-aaaa1111 반려 해소)')
    conn = BindConnection([mention, other])
    assert bind(conn, GOAL_LINE + 'nothing')['source'] == 'auto_match'


def test_auto_match_zero_and_two_candidates_do_not_bind():
    cards = [card('러너 업무 카드 자동 연결 1단계'), card('러너 업무 카드 자동 연결 2단계'), card('무관한 카드 이름입니다')]
    conn = BindConnection(cards)
    out = bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    assert out['bound'] is False and out['reason'] == 'ambiguous'
    assert len(out['candidates']) == 2 and conn.written is None
    out = bind(conn, GOAL_LINE + 'TITLE: 존재하지 않는 제목의 지시서입니다')
    assert out['bound'] is False and out['reason'] == 'no_match'
    assert conn.written is None and 1 <= len(out['candidates']) <= 3
    assert set(out['candidates'][0]) == {'id', 'title'}


def test_short_exact_title_matches_but_short_fragment_does_not():
    from app.services.goal_work_item_sync import match_work_items

    short = card('DB 정리')
    other = card('API 서버 정리 작업 전체')
    assert match_work_items([short, other], 'runner-x', '', 'DB 정리') == [short]
    assert match_work_items([short, other], 'runner-x', 'db정리', '') == [short]
    key_card = card('무관', key='AB12')
    assert match_work_items([key_card, other], 'runner-x', 'ab-12', '') == [key_card]
    # 짧은 조각('정리')은 부분 문자열로는 후보가 되지 못한다
    assert match_work_items([short, other], 'runner-x', '', '정리') == []
    assert match_work_items([short], 'runner-x', '', '') == []


def test_candidate_count_is_over_all_matching_cards_not_only_leaf_cards():
    parent = card('러너 업무 카드 자동 연결', kind='story')
    child = card('러너 업무 카드 자동 연결 구현')
    sql_seen = []

    class Spy(BindConnection):
        async def fetch(self, sql, *args):
            sql_seen.append(sql)
            return await super().fetch(sql, *args)

    out = bind(Spy([parent, child]), GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    assert out['bound'] is False and out['reason'] == 'ambiguous' and len(out['candidates']) == 2
    assert not any('parent_id' in sql for sql in sql_seen)
    # story 하나만 맞으면 task 와 같이 연결 대상이다(전이는 transition_plan 이 자식 유무로 가른다)
    out = bind(BindConnection([parent]), GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    assert out == {'bound': True, 'source': 'auto_match', 'work_item_ids': [str(parent['id'])]}


def test_closed_cards_are_not_candidates():
    conn = BindConnection([card('러너 업무 카드 자동 연결', state='completed')])
    assert bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')['reason'] == 'no_match'


def test_existing_binding_is_kept_without_directive():
    keep = uuid4()
    conn = BindConnection([card('러너 업무 카드 자동 연결')], ids=[keep])
    out = bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    assert out == {'bound': True, 'source': 'existing', 'work_item_ids': [str(keep)]}
    assert conn.written is None


def test_no_link_or_no_milestone_reports_reason():
    assert bind(BindConnection([], links=0), GOAL_LINE + 'x')['reason'] == 'no_goal_link'
    conn = BindConnection([])
    conn.link['milestone_id'] = None
    assert bind(conn, GOAL_LINE + 'x')['reason'] == 'no_milestone'


@pytest.mark.parametrize('instruction', [
    'TITLE: 러너 업무 카드 자동 연결',                       # 목표 연결 없음 / 세션으로만 연결(GOAL_ID 줄 없음)
    'MILESTONE_ID: 00000000-0000-0000-0000-000000000000\nTITLE: 러너 업무 카드 자동 연결',
])
def test_without_goal_id_line_no_auto_match_no_sync_no_report(monkeypatch, instruction):
    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    conn = BindConnection([card('러너 업무 카드 자동 연결')], job=('done', 'done'))
    assert bind(conn, instruction) is None
    assert conn.written is None and conn.scoped_updates == 0
    sync.assert_not_awaited()


def test_without_goal_id_line_explicit_directive_keeps_legacy_binding_only(monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    target = card('가')
    conn = BindConnection([target], job=('done', 'done'))
    assert bind(conn, f'WORK_ITEM_ID: {target["id"]}') is None
    assert conn.scoped_updates == 1 and conn.link['work_item_ids'] == [str(target['id'])]
    sync.assert_not_awaited()


@pytest.mark.parametrize('job,expect_sync', [(('queued', 'queued'), False), (('done', 'done'), True),
                                             (('running', 'running'), True)])
def test_late_binding_syncs_once(monkeypatch, job, expect_sync):
    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    target = card('러너 업무 카드 자동 연결')
    conn = BindConnection([target], job=job)
    bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    if expect_sync:
        sync.assert_awaited_once()
        assert sync.await_args.args[1:] == ('runner-aaaa1111', job[0], job[1], T)
        assert conn.reset == 1
    else:
        sync.assert_not_awaited()


def test_late_binding_of_archived_done_runner_syncs_once(monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    target = card('러너 업무 카드 자동 연결')
    conn = BindConnection([target], job=None, archived=dict(status='done', phase='done', tenant_id=T))
    out = bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    assert out['bound'] and out['source'] == 'auto_match'
    sync.assert_awaited_once()
    assert sync.await_args.args[1:] == ('runner-aaaa1111', 'done', 'done', T)


def test_archive_lookup_failure_does_not_fail_binding(monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)

    class NoArchive(BindConnection):
        async def fetchrow(self, sql, *args):
            if 'pipeline_jobs_archive' in sql:
                raise RuntimeError('relation does not exist')
            return await super().fetchrow(sql, *args)

    conn = NoArchive([card('러너 업무 카드 자동 연결')], job=None)
    assert bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')['bound'] is True
    sync.assert_not_awaited()


def test_unbound_does_not_sync(monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    bind(BindConnection([], job=('done', 'done')), GOAL_LINE + 'TITLE: 일치하는 카드가 없는 제목입니다')
    sync.assert_not_awaited()


def test_schema_not_ready_reports_unbound_only_for_opt_in(monkeypatch):
    monkeypatch.setattr('app.services.goal_work_item_sync.schema_ready', AsyncMock(return_value=False))
    out = bind(AsyncMock(), GOAL_LINE + 'x')
    assert out['bound'] is False and out['reason'] == 'schema_not_ready'
    assert bind(AsyncMock(), 'x') is None


def test_reconcile_also_covers_archived_runners():
    from app.services.goal_work_item_sync import reconcile_runner_items

    calls = []

    class Conn:
        def transaction(self):
            return Transaction()

        async def fetchval(self, *args):
            return 'm'

        async def fetch(self, sql, *args):
            if 'FROM pipeline_jobs_archive' in sql:
                assert 'NOT EXISTS (SELECT 1 FROM pipeline_jobs p' in sql
                assert 'work_items_synced_status IS DISTINCT FROM j.status' in sql
                return [dict(job_id='arch', status='done', phase='done')]
            if 'FROM pipeline_jobs' in sql:
                return []
            calls.append(sql)
            return []

        async def execute(self, sql, *args):
            calls.append(sql)

    asyncio.run(reconcile_runner_items(Conn()))
    assert any('FROM goal_task_links' in c for c in calls)


def test_reconcile_survives_missing_archive_table():
    from app.services.goal_work_item_sync import reconcile_runner_items

    class Conn:
        def transaction(self):
            return Transaction()

        async def fetch(self, sql, *args):
            if 'FROM pipeline_jobs_archive' in sql:
                raise RuntimeError('relation "pipeline_jobs_archive" does not exist')
            return []

    asyncio.run(reconcile_runner_items(Conn()))


def _autolink_env(monkeypatch, binding=None, side_effect=None):
    from app.services import goal_autolink

    monkeypatch.setattr(goal_autolink, '_record', AsyncMock())
    rec = AsyncMock()
    monkeypatch.setattr(goal_autolink, '_record_work_item_binding', rec)
    monkeypatch.setattr('app.services.goal_work_item_sync.bind_work_items',
                        AsyncMock(return_value=binding, side_effect=side_effect))
    class Conn:
        def transaction(self):
            return Transaction()

    conn = Conn()

    class Acquire(Transaction):
        async def __aenter__(self):
            return conn
    pool = MagicMock()
    pool.acquire.return_value = Acquire()
    monkeypatch.setattr('app.core.db_pool.get_pool', lambda: pool)
    return goal_autolink, rec


def test_autolink_returns_binding_and_unlinked_has_none(monkeypatch):
    binding = {'bound': True, 'source': 'auto_match', 'work_item_ids': ['x']}
    goal_autolink, rec = _autolink_env(monkeypatch, binding)
    monkeypatch.setattr(goal_autolink, '_resolve_and_link',
                        AsyncMock(return_value=goal_autolink.AutolinkResult('g', 'm', 'directive')))
    out = asyncio.run(goal_autolink.autolink_job('j', 'AADS', instruction='x'))
    assert out.work_item_binding == binding and out.goal_id == 'g'
    rec.assert_awaited_once_with('j', binding)

    rec.reset_mock()
    monkeypatch.setattr(goal_autolink, '_resolve_and_link',
                        AsyncMock(return_value=goal_autolink._none('no_candidate')))
    out = asyncio.run(goal_autolink.autolink_job('j', 'AADS', instruction='x'))
    assert out.work_item_binding is None
    rec.assert_not_awaited()


def test_autolink_non_opt_in_has_no_binding_field(monkeypatch):
    goal_autolink, rec = _autolink_env(monkeypatch, None)
    monkeypatch.setattr(goal_autolink, '_resolve_and_link',
                        AsyncMock(return_value=goal_autolink.AutolinkResult('g', 'm', 'session')))
    out = asyncio.run(goal_autolink.autolink_job('j', 'AADS', instruction='x'))
    assert out.work_item_binding is None
    rec.assert_not_awaited()


class _Boom(Exception):
    pass


def test_autolink_bind_exception_is_reported_with_class_name(monkeypatch):
    goal_autolink, rec = _autolink_env(monkeypatch, side_effect=_Boom('x'))
    monkeypatch.setattr(goal_autolink, '_resolve_and_link',
                        AsyncMock(return_value=goal_autolink.AutolinkResult('g', 'm', 'directive')))
    out = asyncio.run(goal_autolink.autolink_job('j', 'AADS', instruction='x'))
    assert out.work_item_binding['bound'] is False
    assert out.work_item_binding['reason'] == 'bind_error:_Boom'
    rec.assert_awaited_once()


def test_submit_response_includes_binding_only_when_present():
    from app.api.pipeline_runner import JobSubmitResponse, _work_item_binding_needs_warning

    plain = JobSubmitResponse(job_id='j', status='queued', message='m').model_dump()
    assert 'work_item_binding' not in plain
    binding = {'bound': False, 'reason': 'ambiguous', 'candidates': [{'id': 'a', 'title': 't'}]}
    dumped = JobSubmitResponse(job_id='j', status='queued', message='m', work_item_binding=binding).model_dump()
    assert dumped['work_item_binding'] == binding
    assert _work_item_binding_needs_warning(binding)
    assert _work_item_binding_needs_warning({'bound': False, 'reason': 'bind_error:OSError', 'candidates': []})
    assert not _work_item_binding_needs_warning({'bound': True, 'source': 'directive'})
    assert not _work_item_binding_needs_warning({'bound': False, 'reason': 'no_match', 'candidates': []})
    assert not _work_item_binding_needs_warning(None)


def test_childless_story_alone_is_auto_matched_and_changes_state(monkeypatch):
    """R5-1(a): 자식 없는 story 하나만 일치하면 auto_match 로 연결되고 running/done 에서 상태가 바뀐다."""
    from app.services.goal_work_item_sync import transition_plan

    sync = AsyncMock()
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    story = card('러너 업무 카드 자동 연결 스토리', kind='story')
    other = card('전혀 다른 작업 카드입니다')
    conn = BindConnection([story, other], job=('running', 'running'))
    out = bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결 스토리')
    assert out == {'bound': True, 'source': 'auto_match', 'work_item_ids': [str(story['id'])]}
    sync.assert_awaited_once()
    linked = conn.link['work_item_ids']
    rows = [dict(id=story['id'], parent_id=None, type='story', status='draft')]
    assert transition_plan(rows, 'running', linked) == {str(story['id']): 'in_progress'}
    rows[0]['status'] = 'in_progress'
    assert transition_plan(rows, 'done', linked) == {str(story['id']): 'in_review'}


def test_task_and_story_matching_together_is_ambiguous():
    """R5-1(c): task 와 story 가 함께 일치하면 연결하지 않는다."""
    story = card('러너 업무 카드 자동 연결', kind='story')
    task = card('러너 업무 카드 자동 연결 구현')
    conn = BindConnection([story, task])
    out = bind(conn, GOAL_LINE + 'TITLE: 러너 업무 카드 자동 연결')
    assert out['bound'] is False and out['reason'] == 'ambiguous' and len(out['candidates']) == 2
    assert conn.written is None and conn.link['work_item_ids'] == []


def test_reapplying_same_directive_syncs_once_and_keeps_synced_status(monkeypatch):
    """R5-2: 같은 지시서를 2회 적용해도 sync_runner 는 1회, 2회차에 처리 상태가 그대로 남는다."""
    target = card('가')
    conn = BindConnection([target], job=('done', 'done'))

    async def fake_sync(c, job_id, status, phase=None, tenant_id=None):
        conn.link['work_items_synced_status'] = status

    sync = AsyncMock(side_effect=fake_sync)
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    instruction = f'{GOAL_LINE}WORK_ITEM_ID: {target["id"]}'
    first = bind(conn, instruction)
    assert first['bound'] and conn.link['work_items_synced_status'] == 'done'
    resets = conn.reset
    second = bind(conn, instruction)
    assert second['bound'] and second['source'] == 'directive'
    assert sync.await_count == 1
    assert conn.link['work_items_synced_status'] == 'done'
    assert conn.reset == resets


def test_changed_directive_ids_resync(monkeypatch):
    """R5-2: 같은 링크라도 WORK_ITEM_ID 가 바뀌면 처리 상태를 비우고 다시 동기화한다."""
    a, b = card('가'), card('나')
    conn = BindConnection([a, b], job=('done', 'done'))

    async def fake_sync(c, job_id, status, phase=None, tenant_id=None):
        conn.link['work_items_synced_status'] = status

    sync = AsyncMock(side_effect=fake_sync)
    monkeypatch.setattr('app.services.goal_work_item_sync.sync_runner', sync)
    bind(conn, f'{GOAL_LINE}WORK_ITEM_ID: {a["id"]}')
    bind(conn, f'{GOAL_LINE}WORK_ITEM_ID: {a["id"]}, {b["id"]}')
    assert sync.await_count == 2
