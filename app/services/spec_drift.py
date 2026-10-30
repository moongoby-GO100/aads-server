"""기준 문서 대조와 승인 대기 초안 등록. 모든 I/O 실패는 경고로 격리한다."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.spec_ref import _EXPLICIT_SQL, extract_acceptance, parse_spec_refs

logger = logging.getLogger(__name__)


async def review_spec_prompt(instruction, job_id, project):
    refs = parse_spec_refs(instruction)
    if not refs:
        return ""
    try:
        from app.core.db_pool import get_pool
        async with get_pool().acquire() as conn:
            tenant = await conn.fetchval('SELECT tenant_id FROM pipeline_jobs WHERE job_id=$1', job_id)
            excerpts = []
            for ref in refs:
                row = await conn.fetchrow(_EXPLICIT_SQL, tenant, project.upper(), ref['document_key'], ref['revision'])
                if row:
                    excerpts.append(f"{row['document_key']}@r{row['revision']}\n{extract_acceptance(row['content'])}")
        return ('\n[기준 문서 대조 — 경고 전용, 점수·판정에 반영 금지]\n'
                + '\n'.join(excerpts)[:6000]
                + '\nJSON에 spec_alignment(aligned|deviates|spec_update_needed|not_applicable), '
                  'spec_delta(문서와 달라진 점, 600자 이내)를 포함하세요.\n')
    except Exception:
        logger.warning('spec_prompt_failed_open', exc_info=True)
        return ""


async def register_spec_drift(instruction, job_id, project, details):
    """문서별 트랜잭션/헤드 잠금으로 재검수와 병행 수정에 안전한 초안만 생성한다."""
    if details.get('spec_alignment') not in {'deviates', 'spec_update_needed'}:
        return []
    delta = details.get('spec_delta', '')
    if not delta:
        return []
    results = []
    for key in dict.fromkeys(r['document_key'] for r in parse_spec_refs(instruction)):
        try:
            from app.core.db_pool import get_pool
            from app.api.canonical_documents import RevisionInput, create_revision_in_tx
            async with get_pool().acquire() as conn, conn.transaction():
                tenant = await conn.fetchval('SELECT tenant_id FROM pipeline_jobs WHERE job_id=$1', job_id)
                head = await conn.fetchrow(
                    'SELECT * FROM project_document_heads WHERE tenant_id=$1::uuid '
                    'AND project_key=$2 AND document_key=$3 FOR UPDATE', tenant, project.upper(), key)
                if not head:
                    continue
                idem = f'spec-drift:{job_id}:{key}'
                existing = await conn.fetchrow(
                    'SELECT revision FROM project_document_revisions WHERE head_id=$1 AND idempotency_key=$2',
                    head['id'], idem)
                if existing:
                    results.append({'document_key': key, 'revision': existing['revision']})
                    continue
                latest = await conn.fetchrow('SELECT * FROM project_document_revisions WHERE id=$1', head['latest_revision_id'])
                date = datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d')
                major, minor, patch = map(int, latest['version'].split('.'))
                body = RevisionInput(
                    document_key=key, kind=head['kind'], title=latest['title'],
                    version=f'{major}.{minor}.{patch + 1}',
                    content=latest['content'] + f'\n\n## 구현 중 변경 ({job_id}, {date})\n\n' + delta,
                    source_task_id=job_id, change_summary=f'구현 문서 대조 {job_id}: {delta}'[:1000],
                    idempotency_key=idem, expected_generation=head['generation'])
                await create_revision_in_tx(conn, tenant, 'system:spec-drift', project.upper(), body)
                revision = await conn.fetchval('SELECT revision FROM project_document_revisions '
                                               'WHERE head_id=$1 AND idempotency_key=$2', head['id'], idem)
            results.append({'document_key': key, 'revision': revision})
        except Exception:
            logger.warning('spec_drift_failed_open job_id=%s key=%s', job_id, key, exc_info=True)
    return results


async def apply_spec_review(instruction, job_id, project, details):
    """검수 결과 메시지에 실등록 결과만 반영한다."""
    try:
        if not parse_spec_refs(instruction):
            return
        drafts = await asyncio.wait_for(register_spec_drift(instruction, job_id, project, details), 10)
        details['spec_drafts'] = drafts
        lines = [f"⚠️ 문서 수정 필요 — {d['document_key']} r{d['revision']} 초안 등록(승인 대기)" for d in drafts]
        if not lines:
            lines = [f"문서 대조: {details.get('spec_alignment', 'not_applicable')}"]
        details['summary'] = str(details.get('summary') or '리뷰 완료') + '\n' + '\n'.join(lines)
    except Exception:
        logger.warning('spec_drift_failed_open', exc_info=True)
