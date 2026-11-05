"""정본 문서 자동승인 — 엄격 판정(evaluate)과 승인 실행(maybe_auto_approve).

승인 핵심 로직은 canonical_documents.approve_revision_in_tx 한 곳이고, 사람 승인(HTTP)과 이 모듈이 함께 쓴다.
사람 승인 경로(_require_human_approver)는 바꾸지 않으며 이 모듈은 HTTP 로 노출되지 않는다.

DOC_AUTO_APPROVE=off|shadow|on — 미설정·이상한 값은 on. off 는 DB 를 읽지도 않고 즉시 멈추고,
shadow 는 판정만 구조화 로그(doc_auto_approval_decision)로 남긴다.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional

from fastapi import HTTPException

from app.services import doc_cross_review as cross
from app.services import doc_review_rubric as rubric

logger = logging.getLogger(__name__)

ACTOR = 'system:doc-auto-approver'
MODES = ('off', 'shadow', 'on')
DEFAULT_MODE = 'on'
DEFAULT_MIN_SCORE = 0.85
DEFAULT_MIN_CRITERION = 0.70
DEFAULT_HIGH_RISK_MIN_SCORE = 0.90
HIGH_RISK_MAX_MINOR = 2
REQUIRED_PROVIDERS = frozenset({'anthropic', 'openai'})
ELIGIBLE_TIERS = frozenset({'D2', 'D3'})

MIGRATION_KEY_PREFIX = 'migration-apply-'
MIGRATION_WORDS = ('migration', '마이그레이션')

# 애매하면 high. 키워드를 넓게 잡는 쪽이 안전하다(high 는 더 높은 점수와 minor 상한을 요구할 뿐 승인을 막지는 않는다).
HIGH_RISK_PROJECTS = frozenset({'GO100', 'KIS'})
TRADING_WORDS_KO = ('주문', '매매', '체결', '자금', '계좌', '입출금')
TRADING_WORDS_EN = re.compile(r'\b(?:orders?|trad(?:e|es|ed|ing)|funds?|accounts?)\b', re.IGNORECASE)
SECURITY_WORDS = ('보안', '인증', '권한', '시크릿', '토큰', 'security', 'auth', 'oauth', 'secret', 'token',
                  'credential', 'permission', 'r-auth', 'r-key')
PROTECTED_FILES = ('auth_provider.py', 'model_selector.py', 'claude_relay_server.py', 'docker-compose.yml', '.env')

_HEAD_SQL = (
    'SELECT h.*,EXISTS(SELECT 1 FROM project_document_goal_links gl WHERE gl.head_id=h.id) AS goal_linked '
    'FROM project_document_heads h WHERE h.id=$1 AND h.tenant_id=$2::uuid AND h.project_key=$3'
)
_LAST_DECISION_SQL = ("SELECT action FROM project_document_events WHERE head_id=$1 AND revision_id=$2 "
                      "AND action IN ('review','approved','archived') ORDER BY id DESC LIMIT 1")


@dataclass(frozen=True)
class Decision:
    action: str
    risk: str = 'normal'
    reasons: list = field(default_factory=list)
    score: Optional[float] = None


def auto_approve_mode() -> str:
    value = (os.getenv('DOC_AUTO_APPROVE') or '').strip().lower()
    return value if value in MODES else DEFAULT_MODE


def _threshold(name: str, default: float) -> float:
    try:
        value = float((os.getenv(name) or '').strip())
    except ValueError:
        return default
    return value if math.isfinite(value) and 0 < value <= 1 else default


def min_score() -> float:
    return _threshold('DOC_AUTO_APPROVE_MIN_SCORE', DEFAULT_MIN_SCORE)


def min_criterion() -> float:
    return _threshold('DOC_AUTO_APPROVE_MIN_CRITERION', DEFAULT_MIN_CRITERION)


def high_risk_min_score() -> float:
    return _threshold('DOC_AUTO_APPROVE_HIGH_RISK_MIN_SCORE', DEFAULT_HIGH_RISK_MIN_SCORE)


def _text_of(head: Mapping, revision: Mapping) -> tuple[str, str, str]:
    return (str(revision.get('title') or '').lower(), str(head.get('document_key') or '').lower(),
            str(revision.get('content') or ''))


def classify_risk(head: Mapping, revision: Mapping) -> str:
    title, key, content = _text_of(head, revision)
    meta = title + '\n' + key
    if any(word in meta for word in SECURITY_WORDS):
        return 'high'
    body = content.lower()
    if any(name in body for name in PROTECTED_FILES):
        return 'high'
    if str(head.get('project_key') or '').upper() in HIGH_RISK_PROJECTS:
        everything = meta + '\n' + content
        if any(word in everything for word in TRADING_WORDS_KO) or TRADING_WORDS_EN.search(everything):
            return 'high'
    return 'normal'


def _skip_reasons(head: Mapping, revision: Mapping) -> list[str]:
    reasons = []
    rev_id = str(revision.get('id'))
    if str(head.get('latest_revision_id')) != rev_id:
        reasons.append('not_latest_revision')
    if str(head.get('approved_revision_id')) == rev_id:
        reasons.append('already_approved')
    tier = revision.get('doc_tier')
    if tier not in ELIGIBLE_TIERS:
        reasons.append(f'tier_not_eligible:{tier}')
    kind = str(head.get('kind') or '').lower()
    if kind == 'report':
        reasons.append('kind_report')
    if not head.get('goal_linked'):
        reasons.append('not_goal_linked')
    title, key, _ = _text_of(head, revision)
    if key.startswith(MIGRATION_KEY_PREFIX) or (
            kind == 'contract' and any(word in title or word in key for word in MIGRATION_WORDS)):
        reasons.append('migration_document')
    return reasons


def _has_nonfinite(data: Any) -> bool:
    return cross._scrub_nonfinite(data)[1] > 0


def review_problem(row: Mapping) -> tuple[Optional[dict], Optional[str]]:
    """저장된 리뷰 -> (구조 데이터, 문제 코드). 문제 코드: legacy | non_finite | invalid | truncated | None."""
    text, data = rubric.parse_review_reason(row.get('reason'))
    if data is None:
        return None, 'legacy'
    if _has_nonfinite(data):
        return data, 'non_finite'
    if _count_positive(data.get('dropped_issues')) or _count_positive(data.get('dropped_blockers')):
        return data, 'truncated'
    summary = (text.split('\n', 1)[0] if text else '').strip()[:600]
    candidate = {'verdict': row.get('verdict'), 'summary': summary, 'ceo_brief': data.get('ceo_brief'),
                 'scores': data.get('scores'), 'score': data.get('score'), 'issues': data.get('issues')}
    ok, _ = rubric.validate_structured(candidate)
    return (data, None) if ok else (data, 'invalid')


def _count_positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def providers_needing_review(rows: Iterable[Mapping]) -> set[str]:
    """유효한 구조화 통과 리뷰가 아직 없는 회사. 후속 검토가 채워야 할 대상."""
    satisfied = {r.get('reviewer_provider') for r in cross._latest_by_model(list(rows)).values()
                 if r.get('verdict') == 'pass' and review_problem(r)[1] is None}
    return set(REQUIRED_PROVIDERS) - satisfied


def issue_id(model: str, index: int) -> str:
    return f'{model}:{index}'


def _issue_counts(latest: Mapping[str, Mapping], excluded: frozenset) -> dict:
    counts = {'blocker': 0, 'major': 0, 'minor': 0}
    for model, row in latest.items():
        _, data = rubric.parse_review_reason(row.get('reason'))
        if not isinstance(data, dict):
            continue
        data = cross._scrub_nonfinite(data)[0]
        issues = data.get('issues')
        for index, issue in enumerate(issues if isinstance(issues, list) else []):
            if isinstance(issue, dict) and issue.get('severity') in counts and issue_id(model, index) not in excluded:
                counts[issue['severity']] += 1
        counts['blocker'] += data.get('dropped_blockers') if _count_positive(data.get('dropped_blockers')) else 0
    return counts


def evaluate(head: Mapping, revision: Mapping, reviews: Iterable[Mapping], review_summary: Optional[Mapping],
             checks: Optional[Iterable[Mapping]], excluded_issue_ids: Optional[Iterable[str]] = None) -> Decision:
    """순수 판정. excluded_issue_ids 는 issue_id(모델, 순번) 목록이며 blocker/major/minor 집계에서만 뺀다(기본값 없음)."""
    risk = classify_risk(head, revision)
    skips = _skip_reasons(head, revision)
    if skips:
        return Decision('skip', risk, skips)

    excluded = frozenset(excluded_issue_ids or ())
    holds: list[str] = []

    checks = list(checks or [])
    if not checks:
        holds.append('checks_missing')
    else:
        failed = [str(c.get('id')) if isinstance(c, Mapping) else '?' for c in checks
                  if not (isinstance(c, Mapping) and c.get('ok') is True)]
        if failed:
            holds.append('checks_failed:' + ','.join(failed))

    state = (review_summary or {}).get('review_state')
    if state != 'passed':
        holds.append(f'review_state_{state}')

    latest = cross._latest_by_model(list(reviews or []))
    if any(r.get('verdict') == 'reject' for r in latest.values()):
        holds.append('reject_present')

    passes, valid, problems = [], [], set()
    for row in latest.values():
        if row.get('verdict') != 'pass':
            continue
        data, problem = review_problem(row)
        if problem == 'non_finite':
            problems.add('non_finite_score')
            continue
        passes.append(row)
        if problem == 'legacy':
            problems.add('legacy_review_no_score')
        elif problem == 'invalid':
            problems.add('review_json_invalid')
        elif problem == 'truncated':
            problems.add('review_issues_truncated')
        else:
            valid.append(data)
    holds.extend(sorted(problems))

    providers = {r.get('reviewer_provider') for r in passes}
    if len(passes) < 2 or not REQUIRED_PROVIDERS <= providers:
        holds.append('needs_second_company')

    counts = _issue_counts(latest, excluded)
    if counts['blocker']:
        holds.append('blocker_present')
    if counts['major']:
        holds.append('major_present')

    scores = [d['score'] for d in valid]
    lowest = min(scores) if scores else None
    threshold = high_risk_min_score() if risk == 'high' else min_score()
    if lowest is not None and lowest < threshold:
        holds.append('high_risk_score_below_min' if risk == 'high' else 'score_below_min')
    if any(v < min_criterion() for d in valid for v in d['scores'].values()):
        holds.append('criterion_below_min')
    if risk == 'high' and counts['minor'] > HIGH_RISK_MAX_MINOR:
        holds.append('high_risk_minor_exceeded')

    reasons = list(dict.fromkeys(holds))
    return Decision('hold' if reasons else 'approve', risk, reasons, lowest)


async def assess(conn: Any, tenant: str, project: str, head_id: Any, revision_id: Any = None, *, lock: bool = False):
    """DB 에서 다시 읽어 판정한다. lock=True 면 head 를 FOR UPDATE 로 잠근다. 대상이 없으면 None."""
    head = await conn.fetchrow(_HEAD_SQL + (' FOR UPDATE OF h' if lock else ''), head_id, tenant, project)
    if not head:
        return None
    head = dict(head)
    revision = await conn.fetchrow('SELECT * FROM project_document_revisions WHERE head_id=$1 AND id=$2',
                                   head['id'], revision_id or head['latest_revision_id'])
    if not revision:
        return None
    revision = dict(revision)
    rows = await conn.fetch('SELECT * FROM project_document_reviews WHERE revision_id=$1 ORDER BY id', revision['id'])
    tier = cross.tier_of(revision, head)
    summary = cross.summarize(tier, revision.get('author_model'), rows)
    try:
        checks = rubric.run_checks(head.get('kind') or '', revision.get('title'), revision.get('content'), tier)
    except Exception:
        logger.warning('doc_auto_approval_checks_failed', exc_info=True)
        checks = [{'id': 'checks_unavailable', 'ok': False, 'detail': '결정적 검사를 실행하지 못함'}]
    decision = evaluate(head, revision, rows, summary, checks)
    return {'head': head, 'revision': revision, 'rows': rows, 'summary': summary, 'checks': checks, 'decision': decision}


def _log(event: str, mode: str, head: Mapping, decision: Decision, level: int = logging.WARNING, **extra: Any) -> None:
    payload = {'mode': mode, 'project': head.get('project_key'), 'document_key': head.get('document_key'),
               'action': decision.action, 'risk': decision.risk, 'score': decision.score,
               'reasons': decision.reasons, **extra}
    logger.log(level, '%s %s', event, json.dumps(payload, ensure_ascii=False, default=str))


def _error_code(exc: HTTPException) -> str:
    detail = exc.detail
    return str(detail.get('code') if isinstance(detail, dict) else detail)


async def maybe_auto_approve(conn: Any, tenant: str, project: str, head_id: Any, revision_id: Any) -> Decision:
    """head 를 잠그고 다시 읽어 판정한다. on 모드의 approve 만 DB 에 쓴다. hold·skip·shadow 는 로그만."""
    mode = auto_approve_mode()
    if mode == 'off':
        return Decision('skip', 'normal', ['mode_off'])
    from app.api.canonical_documents import approve_revision_in_tx, review_revision_in_tx

    try:
        async with conn.transaction():
            ctx = await assess(conn, tenant, project, head_id, revision_id, lock=True)
            if ctx is None:
                return Decision('skip', 'normal', ['head_or_revision_missing'])
            head, revision, decision = ctx['head'], ctx['revision'], ctx['decision']
            _log('doc_auto_approval_decision', mode, head, decision,
                 logging.INFO if decision.action == 'skip' else logging.WARNING)
            if mode != 'on' or decision.action != 'approve':
                return decision
            last = await conn.fetchval(_LAST_DECISION_SQL, head['id'], revision['id'])
            if last in ('approved', 'archived'):
                return replace(decision, action='hold', reasons=[f'already_{last}'])
            if last != 'review':
                await review_revision_in_tx(conn, tenant, project, head, revision['id'], head['generation'], ACTOR)
            result = await approve_revision_in_tx(conn, tenant, project, head, revision['id'], ACTOR)
    except HTTPException as exc:
        held = Decision('hold', 'normal', [f'approve_blocked:{_error_code(exc)}'])
        logger.warning('doc_auto_approval_decision %s', json.dumps(
            {'mode': mode, 'document_key': None, 'action': 'hold', 'reasons': held.reasons}, ensure_ascii=False))
        return held
    if not result.get('idempotent'):
        _log('doc_auto_approval_approved', mode, head, decision, actor=ACTOR)
    return decision


_FOLLOW_UP_REQUESTED: set = set()


async def request_follow_up_review(conn: Any, tenant: str, project: str, head: Mapping, revision: Mapping,
                                   *, rereview_legacy: bool = False) -> str:
    """부족한 회사의 리뷰를 기존 schedule 로 1회 예약한다. 일일 상한·budget 은 execute_review 가 지킨다."""
    if auto_approve_mode() != 'on':
        return 'mode_not_on'
    rows = await conn.fetch('SELECT * FROM project_document_reviews WHERE revision_id=$1 ORDER BY id', revision['id'])
    needed = providers_needing_review(rows)
    tried = {r.get('reviewer_provider') for r in rows}
    targets = {p for p in needed if p not in tried}
    if rereview_legacy:
        targets |= {r.get('reviewer_provider') for r in cross._latest_by_model(list(rows)).values()
                    if r.get('verdict') == 'pass' and review_problem(r)[1] == 'legacy'} & needed
    key = str(revision['id'])
    if not targets:
        return 'already_attempted'
    if key in _FOLLOW_UP_REQUESTED:
        return 'already_requested'
    if len(_FOLLOW_UP_REQUESTED) > 1000:
        _FOLLOW_UP_REQUESTED.clear()
    _FOLLOW_UP_REQUESTED.add(key)
    cross.schedule(tenant, project, dict(head), dict(revision), follow_up=True)
    logger.warning('doc_auto_approval_follow_up_scheduled %s', json.dumps(
        {'project': project, 'document_key': head.get('document_key'), 'providers': sorted(targets)}, ensure_ascii=False))
    return 'scheduled'
