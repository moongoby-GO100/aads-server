"""정본 문서 자동승인: 모드, 엄격 판정, 승인 실행 순서, 사람 승인 경로 불변, sweep 점검 스크립트."""
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as canonical
from app.services import doc_auto_approval as auto
from app.services import doc_cross_review as cross
from app.services import doc_review_rubric as rubric

CLAUDE = 'claude-sonnet-5-5'
OPUS = 'claude-opus-5-5'
CODEX = 'codex:gpt-6.1-sol'
ASTRA = 'codex:gpt-6-astra'
SWEEP_PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'doc_auto_approval_sweep.py'


def payload(score=0.9, scores=None, issues=(), verdict='pass'):
    return {'verdict': verdict, 'summary': '문제 없음', 'ceo_brief': '이 문서는 목표를 정합니다. 승인되면 개발이 시작됩니다.',
            'scores': scores or {k: 0.9 for k in rubric.SCORE_KEYS}, 'score': score, 'issues': list(issues)}


def issue(severity='minor', **override):
    data = {'severity': severity, 'criterion': 'completeness', 'location': '완료기준', 'problem': '근거가 부족합니다', 'fix': '수치를 적으세요'}
    data.update(override)
    return data


def row(model, verdict='pass', **kw):
    reason = rubric.compose_reason(payload(verdict=verdict, **kw), [])
    provider = cross.provider_of(model)
    return {'reviewer_model': model, 'reviewer_provider': provider, 'verdict': verdict, 'reason': reason,
            'cross_company': provider == 'openai', 'created_at': 'now'}


def legacy_row(model, verdict='pass'):
    provider = cross.provider_of(model)
    return {'reviewer_model': model, 'reviewer_provider': provider, 'verdict': verdict, 'reason': '정합성 확인',
            'cross_company': provider == 'openai', 'created_at': 'now'}


def head(**kw):
    data = {'id': 'h1', 'project_key': 'AADS', 'document_key': 'doc-a', 'kind': 'plan', 'goal_linked': True,
            'latest_revision_id': 'r1', 'approved_revision_id': None, 'generation': 3}
    data.update(kw)
    return data


def revision(**kw):
    data = {'id': 'r1', 'doc_tier': 'D2', 'title': '일반 계획', 'content': '# 계획\n본문', 'author_model': CLAUDE}
    data.update(kw)
    return data


OK_CHECKS = [{'id': 'content_size', 'ok': True, 'detail': ''}, {'id': 'paths_exist', 'ok': True, 'detail': ''}]


def decide(rows, *, h=None, r=None, checks=OK_CHECKS, excluded=None):
    h, r = h or head(), r or revision()
    summary = cross.summarize(cross.tier_of(r, h), r.get('author_model'), rows)
    return auto.evaluate(h, r, rows, summary, checks, excluded)


GOOD_ROWS = [row(CODEX, score=0.9), row(OPUS, score=0.9)]


# ── 모드·임계값 ──

@pytest.mark.parametrize('value,expected', [
    (None, 'on'), ('', 'on'), ('on', 'on'), ('ON', 'on'), (' shadow ', 'shadow'), ('Shadow', 'shadow'),
    ('off', 'off'), ('OFF', 'off'), ('maybe', 'on'), ('0', 'on'), ('false', 'on')])
def test_mode_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('DOC_AUTO_APPROVE', raising=False)
    else:
        monkeypatch.setenv('DOC_AUTO_APPROVE', value)
    assert auto.auto_approve_mode() == expected


def test_thresholds_default_and_override(monkeypatch):
    for name in ('DOC_AUTO_APPROVE_MIN_SCORE', 'DOC_AUTO_APPROVE_MIN_CRITERION', 'DOC_AUTO_APPROVE_HIGH_RISK_MIN_SCORE'):
        monkeypatch.delenv(name, raising=False)
    assert (auto.min_score(), auto.min_criterion(), auto.high_risk_min_score()) == (0.85, 0.70, 0.90)
    monkeypatch.setenv('DOC_AUTO_APPROVE_MIN_SCORE', '0.95')
    assert auto.min_score() == 0.95
    for bad in ('nan', 'inf', '-1', '0', '2', 'abc'):
        monkeypatch.setenv('DOC_AUTO_APPROVE_MIN_SCORE', bad)
        assert auto.min_score() == 0.85


# ── skip ──

@pytest.mark.parametrize('h,r,reason', [
    (head(document_key='migration-apply-20261106'), revision(), 'migration_document'),
    (head(kind='contract', document_key='db-change'), revision(title='DB 마이그레이션 계약'), 'migration_document'),
    (head(kind='contract', document_key='schema-migration-plan'), revision(), 'migration_document'),
    (head(kind='report'), revision(), 'kind_report'),
    (head(), revision(doc_tier='D1'), 'tier_not_eligible:D1'),
    (head(), revision(doc_tier=None), 'tier_not_eligible:None'),
    (head(), revision(doc_tier='SPLIT'), 'tier_not_eligible:SPLIT'),
    (head(goal_linked=False), revision(), 'not_goal_linked'),
    (head(latest_revision_id='r2'), revision(), 'not_latest_revision'),
    (head(approved_revision_id='r1'), revision(), 'already_approved'),
])
def test_skip_conditions(h, r, reason):
    decision = decide(GOOD_ROWS, h=h, r=r)
    assert decision.action == 'skip' and reason in decision.reasons


def test_plan_with_migration_word_is_not_skipped_unless_contract():
    assert decide(GOOD_ROWS, r=revision(title='마이그레이션 일정 계획')).action == 'approve'


# ── approve / hold ──

def test_normal_document_with_two_companies_is_approved():
    decision = decide([row(CODEX, score=0.86), row(OPUS, score=0.9)])
    assert (decision.action, decision.risk, decision.reasons, decision.score) == ('approve', 'normal', [], 0.86)


def test_score_below_minimum_holds():
    decision = decide([row(CODEX, score=0.84), row(OPUS)])
    assert decision.action == 'hold' and 'score_below_min' in decision.reasons


def test_criterion_below_minimum_holds():
    low = {k: 0.9 for k in rubric.SCORE_KEYS}
    low['risk_rollback'] = 0.69
    decision = decide([row(CODEX, scores=low), row(OPUS)])
    assert decision.action == 'hold' and 'criterion_below_min' in decision.reasons


@pytest.mark.parametrize('severity,reason', [('blocker', 'blocker_present'), ('major', 'major_present')])
def test_blocker_or_major_holds(severity, reason):
    decision = decide([row(CODEX, issues=[issue(severity)]), row(OPUS)])
    assert decision.action == 'hold' and reason in decision.reasons


def test_reject_holds():
    decision = decide([row(CODEX), row(OPUS), row(OPUS, 'reject')])
    assert decision.action == 'hold' and 'reject_present' in decision.reasons


def test_single_company_holds_with_needs_second_company():
    decision = decide([row(CODEX)])
    assert decision.action == 'hold' and 'needs_second_company' in decision.reasons
    two_openai = decide([row(CODEX), row(ASTRA)])
    assert 'needs_second_company' in two_openai.reasons


def test_legacy_reviews_hold_without_score():
    decision = decide([legacy_row(CODEX), legacy_row(OPUS)])
    assert decision.action == 'hold' and 'legacy_review_no_score' in decision.reasons
    mixed = decide([row(CODEX), legacy_row(OPUS)])
    assert mixed.action == 'hold' and 'legacy_review_no_score' in mixed.reasons


def test_failed_deterministic_check_or_missing_checks_hold():
    failed = decide(GOOD_ROWS, checks=OK_CHECKS + [{'id': 'secret_patterns', 'ok': False, 'detail': ''}])
    assert failed.action == 'hold' and 'checks_failed:secret_patterns' in failed.reasons
    assert 'checks_missing' in decide(GOOD_ROWS, checks=[]).reasons
    assert decide(GOOD_ROWS, checks=[{'id': 'x'}]).action == 'hold'


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-Infinity'])
def test_non_finite_score_is_never_approved(bad):
    data = payload()
    reason = rubric.compose_reason(data, []).replace('"score":0.9', f'"score":{bad}', 1)
    assert bad in reason
    nan_row = dict(row(CODEX), reason=reason)
    decision = decide([nan_row, row(OPUS)])
    assert decision.action == 'hold'
    assert 'non_finite_score' in decision.reasons


def test_non_finite_criterion_score_is_never_approved():
    reason = rubric.compose_reason(payload(), []).replace('"completeness":0.9', '"completeness":NaN', 1)
    decision = decide([dict(row(CODEX), reason=reason), row(OPUS)])
    assert decision.action == 'hold' and 'non_finite_score' in decision.reasons


def test_truncated_issue_list_holds():
    reason = rubric.compose_reason(payload(), [])
    reason = reason.replace('"issues":[]', '"issues":[],"dropped_issues":2', 1)
    decision = decide([dict(row(CODEX), reason=reason), row(OPUS)])
    assert decision.action == 'hold' and 'review_issues_truncated' in decision.reasons


def test_review_state_not_passed_holds():
    decision = auto.evaluate(head(), revision(), GOOD_ROWS, {'review_state': 'pending'}, OK_CHECKS)
    assert decision.action == 'hold' and 'review_state_pending' in decision.reasons


# ── 고위험 ──

def test_high_risk_requires_higher_score():
    low = decide([row(CODEX, score=0.89), row(OPUS, score=0.95)], r=revision(title='인증 흐름 설계'))
    assert low.risk == 'high' and low.action == 'hold' and 'high_risk_score_below_min' in low.reasons
    ok = decide([row(CODEX, score=0.92), row(OPUS, score=0.95, issues=[issue(), issue()])], r=revision(title='인증 흐름 설계'))
    assert ok.risk == 'high' and ok.action == 'approve'


def test_high_risk_minor_limit():
    rows = [row(CODEX, score=0.95, issues=[issue(), issue()]), row(OPUS, score=0.95, issues=[issue()])]
    decision = decide(rows, r=revision(title='토큰 정책'))
    assert decision.action == 'hold' and 'high_risk_minor_exceeded' in decision.reasons
    assert decide(rows).action == 'approve'


@pytest.mark.parametrize('h,r', [
    (head(project_key='GO100'), revision(title='GO100 주문 체결 흐름')),
    (head(project_key='KIS'), revision(content='계좌 잔고를 조회한다')),
    (head(project_key='KIS'), revision(content='place an order through the broker')),
    (head(), revision(title='OAuth 연동 설계')),
    (head(document_key='r-auth-update'), revision()),
    (head(), revision(title='시크릿 보관 규칙')),
    (head(), revision(title='권한 모델')),
    (head(), revision(content='auth_provider.py 를 고친다')),
    (head(), revision(content='docker-compose.yml 의 포트를 바꾼다')),
    (head(), revision(content='.env 에 값을 둔다')),
    (head(), revision(content='model_selector.py, claude_relay_server.py')),
])
def test_high_risk_classification(h, r):
    assert auto.classify_risk(h, r) == 'high'


@pytest.mark.parametrize('h,r', [
    (head(), revision()),
    (head(project_key='AADS'), revision(title='주문 목록 화면')),
    (head(project_key='SF'), revision(content='account order fund')),
])
def test_normal_risk_classification(h, r):
    assert auto.classify_risk(h, r) == 'normal'


# ── excluded_issue_ids ──

def test_excluded_issue_ids_default_none_and_opt_in():
    rows = [row(CODEX, issues=[issue('major')]), row(OPUS)]
    assert decide(rows).action == 'hold'
    assert decide(rows, excluded=[]).action == 'hold'
    assert decide(rows, excluded=[auto.issue_id(CODEX, 0)]).action == 'approve'
    assert decide(rows, excluded=[auto.issue_id(OPUS, 0)]).action == 'hold'


def test_excluded_issue_ids_cannot_hide_reject():
    rows = [row(CODEX), row(OPUS), row(OPUS, 'reject', issues=[issue('major')])]
    assert decide(rows, excluded=[auto.issue_id(OPUS, 0)]).action == 'hold'


# ── 실행: maybe_auto_approve ──

class Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class FakeConn:
    def __init__(self, h, r, rows):
        self.head, self.revision, self.rows = h, r, rows
        self.executed, self.events, self.fetches = [], [], 0

    def transaction(self):
        return Tx(self)

    def acquire(self):
        return Tx(self)

    async def fetchrow(self, sql, *args):
        self.fetches += 1
        if 'FROM project_document_heads' in sql:
            return dict(self.head)
        if 'FROM project_document_revisions' in sql:
            return dict(self.revision)

    async def fetch(self, sql, *args):
        self.fetches += 1
        return list(self.rows)

    async def fetchval(self, sql, *args):
        self.fetches += 1
        if 'project_document_events' in sql:
            return self.events[-1][0] if self.events else None
        return 0

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if sql.startswith('INSERT INTO project_document_events'):
            action = 'approved' if "'approved'" in sql else 'review' if "'review'" in sql else 'other'
            self.events.append((action, args[-1]))
        elif 'approved_revision_id=$2' in sql:
            self.head['approved_revision_id'] = args[1]
            self.head['generation'] += 1
        elif sql.startswith('UPDATE project_document_heads'):
            self.head['generation'] += 1


@pytest.fixture
def on_mode(monkeypatch):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'on')
    monkeypatch.setattr(rubric, 'run_checks', lambda *a, **k: list(OK_CHECKS))
    monkeypatch.setattr(cross, 'enforce', AsyncMock(return_value={'doc_tier': 'D2', 'review_state': 'passed'}))


def run(coro):
    return asyncio.run(coro)


def test_on_mode_records_review_then_approved_with_system_actor(on_mode):
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    decision = run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'approve'
    assert conn.events == [('review', 'system:doc-auto-approver'), ('approved', 'system:doc-auto-approver')]
    assert conn.head['approved_revision_id'] == 'r1'


def test_on_mode_skips_review_event_when_already_in_review(on_mode):
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    conn.events.append(('review', 'someone'))
    run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert [e[0] for e in conn.events] == ['review', 'approved']
    assert conn.events[-1][1] == auto.ACTOR


def test_on_mode_is_idempotent(on_mode):
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    writes = len(conn.executed)
    again = run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert again.action == 'skip' and 'already_approved' in again.reasons
    assert len(conn.executed) == writes and len(conn.events) == 2


def test_on_mode_does_not_reapprove_human_archived_revision(on_mode):
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    conn.events.append(('archived', 'human'))
    decision = run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'hold' and conn.executed == []


def test_hold_writes_nothing_and_logs_decision(on_mode, caplog):
    conn = FakeConn(head(), revision(), [row(CODEX)])
    with caplog.at_level(logging.INFO, logger=auto.logger.name):
        decision = run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'hold' and conn.executed == []
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith('doc_auto_approval_decision')]
    assert len(logged) == 1 and 'needs_second_company' in logged[0] and 'doc-a' in logged[0]


def test_shadow_mode_judges_but_never_executes(monkeypatch, on_mode, caplog):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'shadow')
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    with caplog.at_level(logging.INFO, logger=auto.logger.name):
        decision = run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'approve'
    assert conn.executed == [] and conn.events == []
    assert any('"mode": "shadow"' in r.getMessage() for r in caplog.records)


def test_off_mode_touches_nothing(monkeypatch):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'off')
    decision = run(auto.maybe_auto_approve(object(), 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'skip' and decision.reasons == ['mode_off']


def test_approve_blocked_by_gate_is_held_not_raised(on_mode):
    cross.enforce.side_effect = HTTPException(409, {'code': 'cross_review_required'})
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    decision = run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'hold' and decision.reasons == ['approve_blocked:cross_review_required']


def test_approved_log_has_key_risk_score(on_mode, caplog):
    conn = FakeConn(head(), revision(), GOOD_ROWS)
    with caplog.at_level(logging.INFO, logger=auto.logger.name):
        run(auto.maybe_auto_approve(conn, 't1', 'AADS', 'h1', 'r1'))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('doc_auto_approval_approved'))
    data = json.loads(line.split(' ', 1)[1])
    assert (data['document_key'], data['risk'], data['score']) == ('doc-a', 'normal', 0.9)


def test_missing_head_is_skipped(on_mode):
    class Empty(FakeConn):
        async def fetchrow(self, sql, *args):
            return None
    decision = run(auto.maybe_auto_approve(Empty(head(), revision(), []), 't1', 'AADS', 'h1', 'r1'))
    assert decision.action == 'skip'


# ── 후속 검토 예약 ──

@pytest.fixture(autouse=True)
def clear_follow_up():
    auto._FOLLOW_UP_REQUESTED.clear()


def test_follow_up_is_scheduled_once(monkeypatch):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'on')
    schedule = Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    conn = FakeConn(head(), revision(), [row(CODEX)])
    assert run(auto.request_follow_up_review(conn, 't1', 'AADS', head(), revision())) == 'scheduled'
    assert run(auto.request_follow_up_review(conn, 't1', 'AADS', head(), revision())) == 'already_requested'
    schedule.assert_called_once()
    assert schedule.call_args.kwargs == {'follow_up': True}


def test_follow_up_not_repeated_when_other_company_already_tried(monkeypatch):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'on')
    schedule = Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    failed = dict(legacy_row(OPUS, 'attempt_failed'))
    conn = FakeConn(head(), revision(), [row(CODEX), failed])
    assert run(auto.request_follow_up_review(conn, 't1', 'AADS', head(), revision())) == 'already_attempted'
    schedule.assert_not_called()


def test_follow_up_not_scheduled_in_shadow_or_off(monkeypatch):
    schedule = Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    for mode in ('shadow', 'off'):
        monkeypatch.setenv('DOC_AUTO_APPROVE', mode)
        assert run(auto.request_follow_up_review(FakeConn(head(), revision(), [row(CODEX)]), 't', 'AADS', head(), revision())) == 'mode_not_on'
    schedule.assert_not_called()


def test_legacy_rereview_only_when_requested(monkeypatch):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'on')
    schedule = Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    rows = [legacy_row(CODEX), row(OPUS)]
    conn = FakeConn(head(), revision(), rows)
    assert run(auto.request_follow_up_review(conn, 't', 'AADS', head(), revision())) == 'already_attempted'
    assert run(auto.request_follow_up_review(conn, 't', 'AADS', head(), revision(), rereview_legacy=True)) == 'scheduled'


def test_providers_needing_review():
    assert auto.providers_needing_review([row(CODEX)]) == {'anthropic'}
    assert auto.providers_needing_review([legacy_row(CODEX), row(OPUS)]) == {'openai'}
    assert auto.providers_needing_review([row(CODEX), row(OPUS)]) == set()
    assert auto.providers_needing_review([row(CODEX), row(OPUS, 'reject')]) == {'anthropic'}


def test_hook_requests_follow_up_only_on_needs_second_company(monkeypatch):
    conn = FakeConn(head(), revision(), [])
    monkeypatch.setattr(cross, 'get_pool', lambda: conn)
    monkeypatch.setattr(auto, 'maybe_auto_approve', AsyncMock(
        return_value=auto.Decision('hold', 'normal', ['needs_second_company'])))
    follow = AsyncMock()
    monkeypatch.setattr(auto, 'request_follow_up_review', follow)
    outcome = {'review_state': 'passed'}
    run(cross._auto_approve_after_review('t', 'AADS', head(), revision(), outcome, allow_follow_up=True))
    follow.assert_awaited_once()
    follow.reset_mock()
    run(cross._auto_approve_after_review('t', 'AADS', head(), revision(), outcome, allow_follow_up=False))
    follow.assert_not_awaited()
    run(cross._auto_approve_after_review('t', 'AADS', head(), revision(), {'review_state': 'pending'}, allow_follow_up=True))
    follow.assert_not_awaited()


def test_hook_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(cross, 'get_pool', lambda: FakeConn(head(), revision(), []))
    monkeypatch.setattr(auto, 'maybe_auto_approve', AsyncMock(side_effect=RuntimeError('boom')))
    with caplog.at_level(logging.ERROR, logger=cross.logger.name):
        run(cross._auto_approve_after_review('t', 'AADS', head(), revision(), {'review_state': 'passed'}, allow_follow_up=True))
    assert any(r.getMessage().startswith('doc_auto_approval_failed') for r in caplog.records)


class ReviewDB(FakeConn):
    def __init__(self, models, rows):
        super().__init__(head(), revision(), rows)
        self.models = models

    async def fetch(self, sql, *args):
        return self.rows

    async def fetchval(self, sql, *args):
        if 'document_cross_review_leases' in sql:
            return True
        if 'count(*)' in sql:
            return 0
        if 'runner_model_config' in sql:
            return self.models
        return None

    async def execute(self, sql, *args):
        if sql.startswith('INSERT INTO project_document_reviews'):
            self.rows.append({'reviewer_model': args[4], 'reviewer_provider': args[5], 'role': args[6],
                              'cross_company': args[7], 'verdict': args[8], 'reason': args[9], 'created_at': 'now'})


def _follow_up(monkeypatch, db, call):
    from app.services import code_reviewer as code
    monkeypatch.setattr(cross, 'get_pool', lambda: db)
    monkeypatch.setattr(code, '_call_review_model', call)
    monkeypatch.setattr(cross, 'notify_blocked', AsyncMock())
    monkeypatch.setattr(auto, 'maybe_auto_approve', AsyncMock(return_value=auto.Decision('hold', 'normal', ['x'])))
    r = {'id': 'r1', 'doc_tier': 'D2', 'author_model': CLAUDE, 'title': '문서', 'content': '# 문서'}
    return asyncio.run(cross.execute_review('t', 'AADS', {'id': 'h1', 'kind': 'plan', 'document_key': 'doc-a'}, r, follow_up=True))


def test_execute_review_follow_up_calls_only_missing_company(monkeypatch):
    db = ReviewDB([CLAUDE, OPUS, CODEX], [row(CODEX)])
    call = AsyncMock(return_value=json.dumps(payload(), ensure_ascii=False))
    result = _follow_up(monkeypatch, db, call)
    assert [c.kwargs['model'] for c in call.await_args_list] == [OPUS]
    assert result['review_state'] == 'passed'
    assert [(r['reviewer_model'], r['verdict']) for r in db.rows] == [(CODEX, 'pass'), (OPUS, 'pass')]
    cross.notify_blocked.assert_not_awaited()
    auto.maybe_auto_approve.assert_awaited_once()


def test_execute_review_follow_up_without_candidate_records_nothing(monkeypatch):
    db = ReviewDB([CLAUDE, CODEX], [row(CODEX)])
    call = AsyncMock()
    _follow_up(monkeypatch, db, call)
    call.assert_not_awaited()
    assert len(db.rows) == 1
    cross.notify_blocked.assert_not_awaited()


def test_execute_review_follow_up_noop_when_all_satisfied(monkeypatch):
    db = ReviewDB([CLAUDE, OPUS, CODEX], [row(CODEX), row(OPUS)])
    call = AsyncMock()
    _follow_up(monkeypatch, db, call)
    call.assert_not_awaited()


def test_execute_review_follow_up_respects_daily_limit(monkeypatch):
    monkeypatch.setenv('AADS_DOC_TIER_D3_DAILY_LIMIT', '1')

    class Full(ReviewDB):
        async def fetchval(self, sql, *args):
            return 1 if 'count(*)' in sql else await super().fetchval(sql, *args)
    db = Full([CLAUDE, OPUS, CODEX], [row(CODEX)])
    call = AsyncMock()
    _follow_up(monkeypatch, db, call)
    call.assert_not_awaited()
    assert len(db.rows) == 1
    cross.notify_blocked.assert_not_awaited()


def test_schedule_passes_follow_up_flag(monkeypatch):
    monkeypatch.delenv('AADS_MCP_BRIDGE', raising=False)
    execute = AsyncMock(return_value={})
    monkeypatch.setattr(cross, 'execute_review', execute)

    async def go():
        cross.schedule('t', 'AADS', {'id': 'h'}, {'id': 'r'}, follow_up=True)
        await asyncio.gather(*list(cross._TASKS), return_exceptions=True)
    asyncio.run(go())
    assert execute.await_args.kwargs == {'follow_up': True}


def test_detached_worker_gets_follow_up_flag(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setenv('AADS_MCP_BRIDGE', '1')
    monkeypatch.setenv('DOC_CROSS_REVIEW_WORKER_LOG_DIR', str(tmp_path))
    popen = Mock()
    popen.return_value.wait.return_value = 0
    monkeypatch.setattr(subprocess, 'Popen', popen)
    cross.schedule('t', 'AADS', {'id': 'h', 'document_key': 'k'}, {'id': 'r'}, follow_up=True)
    assert '--follow-up' in popen.call_args.args[0]
    popen.reset_mock()
    cross.schedule('t', 'AADS', {'id': 'h', 'document_key': 'k'}, {'id': 'r'})
    assert '--follow-up' not in popen.call_args.args[0]


# ── 사람 승인 경로 불변 ──

class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return Tx(self.conn)


def _context(auth_type='human_session', uid='u1', role='owner'):
    return {'tenant': {'id': 't1'}, 'user': {'user_id': uid, 'auth_type': auth_type},
            'membership': {'role': role, 'status': 'active'}}


def _body():
    import uuid
    return canonical.DecisionInput(revision_id=uuid.uuid4(), expected_generation=1)


@pytest.mark.parametrize('context', [_context('api_key'), _context('service'), _context(uid='system:doc-auto-approver')])
def test_http_approve_requires_human_login(monkeypatch, context):
    conn = FakeConn(head(), revision(), [])
    monkeypatch.setattr(canonical, 'get_pool', lambda: Pool(conn))
    with pytest.raises(HTTPException) as exc:
        run(canonical.approve_document('AADS', 'doc-a', _body(), context))
    assert exc.value.status_code == 403 and exc.value.detail == 'human_login_required'
    assert conn.executed == [] and conn.fetches == 0


def test_http_approve_delegates_after_human_checks(monkeypatch):
    conn = FakeConn(head(), revision(), [])
    body = _body()
    h = {'id': 'h1', 'approved_revision_id': None, 'generation': 1, 'latest_revision_id': body.revision_id}
    monkeypatch.setattr(canonical, 'get_pool', lambda: Pool(conn))
    monkeypatch.setattr(canonical, '_authorize', AsyncMock(return_value=('t1', 'u1')))
    monkeypatch.setattr(canonical, '_head', AsyncMock(return_value=h))
    delegate = AsyncMock(return_value={'approved_revision_id': body.revision_id, 'idempotent': False})
    monkeypatch.setattr(canonical, 'approve_revision_in_tx', delegate)
    assert run(canonical.approve_document('AADS', 'doc-a', body, _context()))['idempotent'] is False
    delegate.assert_awaited_once_with(conn, 't1', 'AADS', h, body.revision_id, 'u1')


def test_http_approve_generation_conflict_precedes_delegate(monkeypatch):
    conn = FakeConn(head(), revision(), [])
    body = _body()
    h = {'id': 'h1', 'approved_revision_id': None, 'generation': 9, 'latest_revision_id': body.revision_id}
    monkeypatch.setattr(canonical, 'get_pool', lambda: Pool(conn))
    monkeypatch.setattr(canonical, '_authorize', AsyncMock(return_value=('t1', 'u1')))
    monkeypatch.setattr(canonical, '_head', AsyncMock(return_value=h))
    delegate = AsyncMock()
    monkeypatch.setattr(canonical, 'approve_revision_in_tx', delegate)
    with pytest.raises(HTTPException) as exc:
        run(canonical.approve_document('AADS', 'doc-a', body, _context()))
    assert exc.value.detail == 'generation_conflict'
    delegate.assert_not_awaited()


def test_approve_revision_in_tx_requires_review_state(monkeypatch):
    monkeypatch.setattr(cross, 'enforce', AsyncMock(return_value={}))
    conn = FakeConn(head(), revision(), [])
    with pytest.raises(HTTPException) as exc:
        run(canonical.approve_revision_in_tx(conn, 't1', 'AADS', head(), 'r1', 'u1'))
    assert exc.value.detail == 'revision_not_in_review' and conn.executed == []


def test_approve_revision_in_tx_is_idempotent(monkeypatch):
    enforce = AsyncMock()
    monkeypatch.setattr(cross, 'enforce', enforce)
    conn = FakeConn(head(), revision(), [])
    result = run(canonical.approve_revision_in_tx(conn, 't1', 'AADS', head(approved_revision_id='r1'), 'r1', 'u1'))
    assert result['idempotent'] is True and conn.executed == []
    enforce.assert_not_awaited()


def test_approval_endpoint_is_not_exposed_for_auto_approver():
    # 승인 실행 경로는 노출하지 않는다. 읽기 전용 참고검토 조회(GET /auto-approval-digest)만 예외다.
    routes = [route for route in canonical.router.routes if 'auto' in getattr(route, 'path', '')]
    assert all(route.path.endswith('/auto-approval-digest') and route.methods == {'GET'} for route in routes)
    assert not hasattr(canonical, 'maybe_auto_approve')


# ── sweep ──

def load_sweep():
    spec = importlib.util.spec_from_file_location('doc_auto_approval_sweep', SWEEP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SweepConn(FakeConn):
    def __init__(self, heads, reviews):
        super().__init__(head(), revision(), reviews)
        self.heads = heads

    async def fetch(self, sql, *args):
        self.fetches += 1
        if 'JOIN project_document_revisions latest' in sql:
            return list(self.heads)
        return list(self.rows)

    async def fetchval(self, sql, *args):
        return 0


SWEEP_ROWS = [{'id': 'h1', 'tenant_id': 't1', 'project_key': 'AADS', 'document_key': 'doc-a'}]


def test_sweep_help_succeeds(capsys):
    sweep = load_sweep()
    with pytest.raises(SystemExit) as exc:
        sweep.main(['--help'])
    assert exc.value.code == 0
    assert '--apply' in capsys.readouterr().out


def test_sweep_rejects_bad_limit():
    assert load_sweep().main(['--limit', '0']) == 2


def test_sweep_query_excludes_migration_apply():
    sweep = load_sweep()
    assert "NOT LIKE 'migration-apply-%'" in sweep._SELECT
    assert 'latest.doc_tier' in sweep._SELECT.format(required=canonical.APPROVAL_REQUIRED_SQL)


def test_sweep_dry_run_makes_no_writes(on_mode):
    sweep = load_sweep()
    conn = SweepConn(SWEEP_ROWS, GOOD_ROWS)
    items = run(sweep.collect(conn, None, None))
    assert conn.executed == [] and conn.events == []
    assert [i['class'] for i in items] == ['would_approve']
    report = sweep.render(items, False, 'on')
    assert report['counts'] == {'would_approve': 1}
    assert 'head' not in report['documents'][0] and 'tenant_id' not in report['documents'][0]
    json.dumps(report, default=str)


def test_sweep_classifies_legacy_for_rereview(on_mode):
    sweep = load_sweep()
    conn = SweepConn(SWEEP_ROWS, [legacy_row(CODEX), legacy_row(OPUS)])
    assert run(sweep.collect(conn, None, None))[0]['class'] == 'would_rereview'
    conn = SweepConn(SWEEP_ROWS, [legacy_row(CODEX), legacy_row(OPUS), legacy_row(OPUS, 'reject')])
    assert run(sweep.collect(conn, None, None))[0]['class'] == 'hold'


def _item(klass, key):
    return {'tenant_id': 't1', 'head_id': 'h1', 'project': 'AADS', 'document_key': key, 'class': klass,
            'head': head(), 'revision': revision()}


def test_sweep_apply_respects_limit_and_budget(monkeypatch):
    sweep = load_sweep()
    approve = AsyncMock(return_value=auto.Decision('approve'))
    execute = AsyncMock(return_value={})
    monkeypatch.setattr(auto, 'maybe_auto_approve', approve)
    monkeypatch.setattr(cross, 'execute_review', execute)
    items = [_item('would_approve', 'a'), _item('would_rereview', 'b'), _item('would_rereview', 'c'), _item('hold', 'd')]
    conn = SweepConn([], [])
    run(sweep.apply_items(conn, items, 1))
    assert [i.get('applied') for i in items] == ['approved', 'rereviewed', 'skipped_limit', None]
    approve.assert_awaited_once()
    execute.assert_awaited_once()
    assert execute.await_args.kwargs == {'follow_up': True}

    class Spent(SweepConn):
        async def fetchval(self, sql, *args):
            return 99
    items = [_item('would_rereview', 'e')]
    run(sweep.apply_items(Spent([], []), items, 5))
    assert items[0]['applied'] == 'skipped_daily_limit'
    assert execute.await_count == 1


def test_sweep_apply_is_ignored_unless_mode_on(monkeypatch, capsys):
    import app.core.db_pool as db_pool
    sweep = load_sweep()
    conn = SweepConn(SWEEP_ROWS, GOOD_ROWS)
    monkeypatch.setattr(db_pool, 'init_pool', AsyncMock())
    monkeypatch.setattr(db_pool, 'close_pool', AsyncMock())
    monkeypatch.setattr(db_pool, 'get_pool', lambda: Pool(conn))
    monkeypatch.setattr(rubric, 'run_checks', lambda *a, **k: list(OK_CHECKS))
    maybe = AsyncMock()
    monkeypatch.setattr(auto, 'maybe_auto_approve', maybe)
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'shadow')
    assert sweep.main(['--apply', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['apply'] is False and 'note' in report
    maybe.assert_not_awaited()
    assert conn.executed == []


def test_sweep_classifies_never_reviewed_for_first_review(on_mode):
    # 2026-11-06: 등록 때 검토 예약이 빠진 문서는 아무도 집어 들지 않아 자동승인 순환이 시작되지 않았다.
    sweep = load_sweep()
    assert run(sweep.collect(SweepConn(SWEEP_ROWS, []), None, None))[0]['class'] == 'would_review'
    # D2 에서 한 회사만 통과(review_state=passed)했고 다른 회사는 시도한 적 없음 -> 남은 회사 첫 검토
    assert run(sweep.collect(SweepConn(SWEEP_ROWS, [row(CODEX)]), None, None))[0]['class'] == 'would_review'


def test_sweep_does_not_review_tried_or_rejected(on_mode):
    sweep = load_sweep()
    hold = auto.Decision('hold', reasons=['needs_second_company'])
    # 두 회사 모두 시도한 기록이 있으면 매시 다시 돌리지 않는다.
    assert sweep.classify(hold, 'pending', set()) == 'hold'
    # 반려가 있으면 검토가 아니라 자동 보완 경로 소관이다.
    rejected = auto.Decision('hold', reasons=['reject_present', 'needs_second_company'])
    assert sweep.classify(rejected, 'pending', {'openai'}) == 'hold'
    assert sweep.classify(hold, 'rejected', {'openai'}) == 'hold'
    # 검토 상태와 무관한 보류(점수 미달 등)는 첫 검토 대상이 아니다.
    low = auto.Decision('hold', reasons=['score_below_min'])
    assert sweep.classify(low, 'passed', {'anthropic'}) == 'hold'
    # 기존 호출 형태(인자 1개)는 그대로 동작한다.
    assert sweep.classify(auto.Decision('approve')) == 'would_approve'


def test_sweep_apply_first_review_shares_limit(monkeypatch):
    sweep = load_sweep()
    execute = AsyncMock(return_value={})
    monkeypatch.setattr(cross, 'execute_review', execute)
    items = [_item('would_review', 'a'), _item('would_rereview', 'b'), _item('would_review', 'c')]
    run(sweep.apply_items(SweepConn([], []), items, 2))
    assert [i.get('applied') for i in items] == ['reviewed', 'rereviewed', 'skipped_limit']
    assert execute.await_count == 2
    assert all(c.kwargs == {'follow_up': True} for c in execute.await_args_list)


def test_follow_up_review_role_fits_db_check():
    # 2026-11-06: follow_up 역할이 그대로 INSERT 돼 role CHECK 위반으로 후속 검토가 전부 실패했다.
    sql = (Path(__file__).resolve().parents[2] / 'migrations' / '20261029_document_tier.sql').read_text()
    for name in cross.STORED_ROLES:
        assert f"'{name}'" in sql
    assert cross.stored_role('follow_up', True) == 'cross'
    assert cross.stored_role('follow_up', False) == 'same_company_other_model'
    assert cross.stored_role('follow_up_rerun', True) == 'cross'
    assert cross.stored_role('same_company_other_model', False) == 'same_company_other_model'
    assert cross.stored_role('cross', True) == 'cross'


def test_record_writes_allowed_role_for_follow_up():
    conn = FakeConn(head(), revision(), [])
    seen = []

    async def execute(sql, *args):
        seen.append(args)
    conn.execute = execute
    pick = cross.ReviewerPick(CODEX, 'follow_up')
    run(cross._record(conn, 't1', 'AADS', head(), revision(), pick, 'pass', 'ok'))
    assert seen and seen[0][6] in cross.STORED_ROLES and seen[0][6] == 'cross' and seen[0][7] is True
    pick = cross.ReviewerPick(OPUS, 'follow_up_rerun')
    run(cross._record(conn, 't1', 'AADS', head(), revision(), pick, 'pass', 'ok'))
    assert seen[1][6] == 'same_company_other_model' and seen[1][7] is False


# ── runner-e2f0c41e 반려 지적 4건 회귀 ──

def test_high_risk_still_requires_base_min_score(monkeypatch):
    # [P1] MIN_SCORE 가 HIGH_RISK_MIN_SCORE 보다 높으면 고위험 문서도 기본 최소점수를 지켜야 한다.
    monkeypatch.setenv('DOC_AUTO_APPROVE_MIN_SCORE', '0.95')
    monkeypatch.setenv('DOC_AUTO_APPROVE_HIGH_RISK_MIN_SCORE', '0.90')
    decision = decide([row(CODEX, score=0.92), row(OPUS, score=0.96)], r=revision(title='인증 흐름 설계'))
    assert decision.risk == 'high' and decision.action == 'hold'
    assert 'score_below_min' in decision.reasons
    normal = decide([row(CODEX, score=0.92), row(OPUS, score=0.96)])
    assert normal.action == 'hold' and 'score_below_min' in normal.reasons


@pytest.mark.parametrize('key', ['order_execution', 'account_balance', 'fund_transfer', 'trade-log', 'kis_orders_v2'])
def test_trading_keywords_split_on_underscore_and_hyphen(key):
    # [P1] 밑줄·하이픈으로 이어진 영어 거래 키워드도 고위험이다.
    assert auto.classify_risk(head(project_key='KIS', document_key=key), revision(title='plan')) == 'high'


@pytest.mark.parametrize('key', ['reorder-sections', 'tradeoff-notes', 'refunds-policy-draft'])
def test_trading_keywords_do_not_match_inside_other_words(key):
    assert auto.classify_risk(head(project_key='KIS', document_key=key), revision(title='plan')) == 'normal'


class SizedReviewDB(ReviewDB):
    def __init__(self, by_size, rows):
        super().__init__([], rows)
        self.by_size = by_size

    async def fetchval(self, sql, *args):
        if 'runner_model_config' in sql:
            return self.by_size.get(args[0] if args else 'AI_REVIEW', [])
        return await super().fetchval(sql, *args)


def test_follow_up_finds_needed_company_in_other_size_list(monkeypatch):
    # [P1] STD 에 작성 모델+OpenAI, HEAVY 에만 다른 Anthropic 모델이 있어도 Anthropic 리뷰를 채운다.
    db = SizedReviewDB({'AI_REVIEW_STD': [CLAUDE, CODEX], 'AI_REVIEW_HEAVY': [OPUS]}, [row(CODEX)])
    db.by_size['AI_REVIEW_STD'] = [CLAUDE, CODEX]
    call = AsyncMock(return_value=json.dumps(payload(), ensure_ascii=False))
    monkeypatch.setattr(cross, 'tier_of', lambda r, h: 'D2')
    _follow_up(monkeypatch, db, call)
    assert [c.kwargs['model'] for c in call.await_args_list] == [OPUS]


def test_pick_follow_up_never_picks_author_and_covers_rerun_first():
    picks = cross.pick_follow_up(CLAUDE, [[CLAUDE, CODEX], [OPUS, ASTRA]], available={CLAUDE, CODEX, OPUS, ASTRA},
                                 needed={'anthropic', 'openai'}, rerun={ASTRA})
    assert [p.model for p in picks] == [ASTRA, OPUS]
    assert all(p.model != CLAUDE for p in picks)


def test_legacy_pass_beside_valid_same_company_review_still_needs_follow_up():
    # [P2] 같은 회사의 유효 리뷰가 있어도 다른 모델의 legacy pass 가 남으면 evaluate 는 보류하므로,
    # 후속 검토도 그 회사를 '필요' 로 보고 legacy 모델을 다시 돌려야 한다.
    rows = [row(CODEX), legacy_row(ASTRA), row(OPUS)]
    assert decide(rows).action == 'hold'
    assert auto.providers_needing_review(rows) == {'openai'}
    assert auto.follow_up_targets(rows) == ({'openai'}, {ASTRA})


def test_sweep_style_legacy_rereview_schedules_when_valid_same_company_exists(monkeypatch):
    monkeypatch.setenv('DOC_AUTO_APPROVE', 'on')
    auto._FOLLOW_UP_REQUESTED.clear()
    schedule = Mock()
    monkeypatch.setattr(cross, 'schedule', schedule)
    rows = [row(CODEX), legacy_row(ASTRA), row(OPUS)]
    conn = FakeConn(head(), revision(), rows)
    assert run(auto.request_follow_up_review(conn, 't', 'AADS', head(), revision(), rereview_legacy=True)) == 'scheduled'
    schedule.assert_called_once()
