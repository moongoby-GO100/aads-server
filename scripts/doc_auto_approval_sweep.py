#!/usr/bin/env python3
"""승인 필요 정본 문서를 훑어 자동승인 판정을 점검한다.

기본은 dry-run(쓰기 없음). --apply 를 줘야 쓴다 — 단, DOC_AUTO_APPROVE 가 on 일 때만이다.

    doc_auto_approval_sweep.py                   판정 요약 출력
    doc_auto_approval_sweep.py --json            JSON 으로 출력
    doc_auto_approval_sweep.py --apply           조건을 채운 문서는 자동승인,
                                                 옛 형식 리뷰만 있는 문서는 재검토,
                                                 한 번도 검토받지 못한 회사가 있는 문서는 첫 검토
                                                 (재검토·첫 검토 합쳐 최대 --limit 건, 일일 상한 준수)

교차검토는 판본 등록 직후에만 예약되므로, 그때 예약이 빠졌거나 자동승인이 켜지기 전에 등록된 문서는
아무도 다시 집어 들지 않아 검토→보류→자동 보완→자동승인 순환이 시작되지 않는다(2026-11-06 실측 25건 보류 중
대부분 review_state_pending). 첫 검토(would_review)가 그 순환의 시작점을 채운다. cron 으로 매시 --apply 한다.

migration-apply-* 문서는 대상에서 뺀다(마이그레이션 자동화 경로 소관).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_LIMIT = 500

_SELECT = (
    "SELECT h.id,h.tenant_id::text AS tenant_id,h.project_key,h.document_key FROM project_document_heads h "
    "JOIN project_document_revisions latest ON latest.id=h.latest_revision_id "
    "WHERE {required} AND h.document_key NOT LIKE 'migration-apply-%' "
    "AND ($1::uuid IS NULL OR h.tenant_id=$1::uuid) AND ($2::text IS NULL OR h.project_key=$2) "
    "ORDER BY h.updated_at DESC,h.id LIMIT $3"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='승인 필요 정본 문서의 자동승인 판정을 점검한다(기본 dry-run).')
    parser.add_argument('--apply', action='store_true', help='실제로 자동승인·재검토 예약을 수행한다(기본: 쓰기 없음)')
    parser.add_argument('--json', action='store_true', help='JSON 으로 출력한다')
    parser.add_argument('--limit', type=int, default=10, help='--apply 시 재검토 최대 건수(기본 10)')
    parser.add_argument('--tenant', default=None, help='테넌트 UUID 로 제한')
    parser.add_argument('--project', default=None, help='프로젝트 키로 제한')
    parser.add_argument('--timeout', type=int, default=3600, help='전체 제한 시간(초, 기본 3600)')
    return parser


def untried_providers(rows) -> set:
    """필수 검토 회사 중 이 판본에 리뷰 기록(실패 시도 포함)이 하나도 없는 회사."""
    from app.services import doc_auto_approval
    return set(doc_auto_approval.REQUIRED_PROVIDERS) - {r.get('reviewer_provider') for r in rows or []}


def classify(decision, review_state=None, untried=()) -> str:
    if decision.action == 'approve':
        return 'would_approve'
    if decision.action == 'hold' and 'reject_present' not in decision.reasons:
        if 'legacy_review_no_score' in decision.reasons:
            return 'would_rereview'
        # 시도한 적 없는 회사가 있을 때만 첫 검토한다. 이미 시도해 실패한 회사는 매시 다시 돌리지 않는다.
        # D2 는 타사 1곳 통과로 review_state=passed 가 되지만 자동승인은 두 회사를 요구하므로(needs_second_company)
        # 그 경우도 남은 회사의 검토를 채운다.
        if untried and review_state != 'rejected' and (
                review_state == 'pending' or 'needs_second_company' in decision.reasons):
            return 'would_review'
    return decision.action


async def collect(conn, tenant, project):
    from app.api.canonical_documents import APPROVAL_REQUIRED_SQL
    from app.services import doc_auto_approval
    rows = await conn.fetch(_SELECT.format(required=APPROVAL_REQUIRED_SQL), tenant, project, SCAN_LIMIT)
    items = []
    for row in rows:
        ctx = await doc_auto_approval.assess(conn, row['tenant_id'], row['project_key'], row['id'])
        if ctx is None:
            continue
        decision = ctx['decision']
        klass = classify(decision, ctx['summary'].get('review_state'), untried_providers(ctx['rows']))
        items.append({'tenant_id': row['tenant_id'], 'head_id': row['id'], 'project': row['project_key'],
                      'document_key': row['document_key'], 'class': klass, 'action': decision.action,
                      'risk': decision.risk, 'score': decision.score, 'reasons': decision.reasons,
                      'review_state': ctx['summary']['review_state'], 'head': ctx['head'], 'revision': ctx['revision']})
    return items


async def daily_budget_left(conn, tenant) -> int:
    from app.services.doc_tier import settings
    used = await conn.fetchval('SELECT count(*) FROM document_cross_review_budget '
                               'WHERE tenant_id=$1::uuid AND review_day=CURRENT_DATE', tenant)
    return settings()['d3_daily_limit'] - int(used or 0)


async def apply_items(conn, items, limit):
    from app.services import doc_auto_approval, doc_cross_review
    rereviews = 0
    for item in items:
        if item['class'] == 'would_approve':
            decision = await doc_auto_approval.maybe_auto_approve(
                conn, item['tenant_id'], item['project'], item['head_id'], item['revision']['id'])
            item['applied'] = 'approved' if decision.action == 'approve' else f'not_approved:{decision.action}'
        elif item['class'] in ('would_rereview', 'would_review'):
            if rereviews >= limit:
                item['applied'] = 'skipped_limit'
            elif await daily_budget_left(conn, item['tenant_id']) <= 0:
                item['applied'] = 'skipped_daily_limit'
            else:
                rereviews += 1
                await doc_cross_review.execute_review(
                    item['tenant_id'], item['project'], dict(item['head']), dict(item['revision']), follow_up=True)
                item['applied'] = 'rereviewed' if item['class'] == 'would_rereview' else 'reviewed'
    return items


def render(items, applied, mode) -> dict:
    counts: dict = {}
    for item in items:
        counts[item['class']] = counts.get(item['class'], 0) + 1
    documents = [{k: v for k, v in item.items() if k not in ('head', 'revision', 'head_id', 'tenant_id')} for item in items]
    return {'mode': mode, 'apply': applied, 'total': len(items), 'counts': counts, 'documents': documents}


async def run(args) -> int:
    from app.core.db_pool import close_pool, get_pool, init_pool
    from app.services import doc_auto_approval
    mode = doc_auto_approval.auto_approve_mode()
    applied = bool(args.apply) and mode == 'on'
    await init_pool()
    try:
        async with get_pool().acquire() as conn:
            items = await collect(conn, args.tenant, args.project.upper() if args.project else None)
        if applied:
            async with get_pool().acquire() as conn:
                await apply_items(conn, items, args.limit)
    finally:
        await close_pool()
    report = render(items, applied, mode)
    if args.apply and not applied:
        report['note'] = f'DOC_AUTO_APPROVE={mode} 이므로 --apply 를 무시하고 dry-run 으로 끝냈다'
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"mode={mode} apply={applied} total={report['total']} counts={report['counts']}")
        if report.get('note'):
            print(report['note'])
        for doc in report['documents']:
            print(f"- [{doc['class']}] {doc['project']}/{doc['document_key']} risk={doc['risk']} "
                  f"score={doc['score']} reasons={','.join(doc['reasons']) or '-'}"
                  + (f" applied={doc['applied']}" if 'applied' in doc else ''))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        print('--limit 는 1 이상이어야 한다', file=sys.stderr)
        return 2
    sys.path.insert(0, str(REPO))
    try:
        return asyncio.run(asyncio.wait_for(run(args), timeout=args.timeout))
    except asyncio.TimeoutError:
        print(f'제한 시간 {args.timeout}초 초과', file=sys.stderr)
        return 124


if __name__ == '__main__':
    sys.exit(main())
