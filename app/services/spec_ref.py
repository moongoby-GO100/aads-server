"""러너 기준 문서 판본 고정: 경고 전용, 스키마 변경 없음."""
from __future__ import annotations

import asyncio
import json
import re

from app.services import canonical_gate as gate

MARKER = "[기준 문서 — SPEC_REF]"
_REF = re.compile(r"SPEC_REF:\s*([A-Za-z0-9_.:/-]+)(?:@r([1-9][0-9]{0,9}))?")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BASE_SQL = """
SELECT h.document_key, r.revision, r.title, r.content,
       COALESCE(r.id = h.approved_revision_id, false) AS approved
FROM project_document_heads h
JOIN project_document_revisions r ON r.head_id = h.id
 AND r.tenant_id = h.tenant_id AND r.project_key = h.project_key
WHERE h.tenant_id = $1::uuid AND h.project_key = $2
"""
_GOAL_SQL = _BASE_SQL + """
 AND h.kind IN ('prd','spec','design','plan','contract')
 AND r.id = COALESCE(h.approved_revision_id, h.latest_revision_id)
 AND (EXISTS (SELECT 1 FROM project_document_goal_links l
              WHERE l.head_id = h.id AND l.goal_id = $3::uuid)
      OR EXISTS (SELECT 1 FROM project_document_revisions linked
                 WHERE linked.head_id = h.id AND linked.goal_id = $3::uuid))
 ORDER BY h.document_key
"""
_EXPLICIT_SQL = _BASE_SQL + """
 AND h.document_key = $3
 AND (($4::integer IS NOT NULL AND r.revision = $4)
      OR ($4::integer IS NULL AND r.id = COALESCE(h.approved_revision_id, h.latest_revision_id)))
"""


def parse_spec_refs(instruction):
    refs = []
    for line in instruction.splitlines():
        match = _REF.fullmatch(line.strip())
        if match:
            refs.append({"document_key": match[1], "revision": int(match[2]) if match[2] else None})
    return refs


async def resolve_goal_specs(conn, tenant_id, project, goal_id):
    return [dict(row) for row in await conn.fetch(_GOAL_SQL, tenant_id, project, goal_id)]


def extract_acceptance(content):
    selected = []
    level = None
    for line in (content or "").splitlines():
        heading = _HEADING.fullmatch(line.strip())
        title = heading[2].strip() if heading else line.strip().rstrip(":：")
        is_target = title.casefold() in {"완료 기준", "수용 기준", "성공 기준", "acceptance"}
        if level is not None and heading and len(heading[1]) <= level:
            level = None
        if is_target:
            level = len(heading[1]) if heading else 6
        if level is not None:
            selected.append(line)
    return "\n".join(selected)[:1500]


def build_spec_block(refs):
    lines = [MARKER]
    remaining = 4000
    for ref in refs:
        excerpt = extract_acceptance(ref.get("content", ""))[:remaining]
        remaining -= len(excerpt)
        lines.extend([f"SPEC_REF: {ref['document_key']}@r{ref['revision']}",
                      f"제목: {ref.get('title', '')} | 승인: {'예' if ref['approved'] else '아니오'}"])
        if excerpt:
            lines.append(excerpt)
    return "\n".join(lines)


async def bind_runner_specs(*, instruction, goal_id, tenant_id, project, job_id, pool=None):
    """목표 연결 후 저장된 지시서 보강. 모든 I/O 합산 예산, 실패 시 원래 제출 유지."""
    if gate.get_mode() == gate.MODE_OFF:
        return None
    explicit = parse_spec_refs(instruction)
    declared = any(line.strip().startswith("SPEC_REF:") for line in instruction.splitlines())
    if not declared and not goal_id:
        return None

    async def run():
        nonlocal pool
        if pool is None:
            from app.core.db_pool import get_pool
            pool = get_pool()
        async with pool.acquire() as conn:
            refs = []
            invalid = declared and (not explicit or sum(
                line.strip().startswith("SPEC_REF:") for line in instruction.splitlines()
            ) != len(explicit))
            if declared:
                for ref in explicit:
                    # Avoid PostgreSQL int overflow for malformed revision numbers.
                    if ref['revision'] is not None and ref['revision'] > 2147483647:
                        invalid = True
                        continue
                    row = await conn.fetchrow(_EXPLICIT_SQL, tenant_id, project,
                                              ref['document_key'], ref['revision'])
                    if row is None:
                        invalid = True
                    else:
                        refs.append(dict(row))
            else:
                refs = await resolve_goal_specs(conn, tenant_id, project, goal_id)
            verdict = ('spec_ref_invalid' if invalid else 'spec_missing' if not refs else
                       'spec_unapproved' if any(not r['approved'] for r in refs) else 'spec_bound')
            if refs and not invalid and MARKER not in instruction:
                block = build_spec_block(refs)
                # Append to the persisted instruction, preserving other submission enrichment.
                await conn.execute(
                    "UPDATE pipeline_jobs SET instruction = instruction || E'\\n\\n' || $3 "
                    "WHERE job_id=$1 AND tenant_id=$2::uuid AND position($4 in instruction)=0",
                    job_id, tenant_id, block, MARKER)
            detail = {'goal_id': goal_id, 'refs': [
                {k: r[k] for k in ('document_key', 'revision', 'approved')} for r in refs],
                'source': 'explicit' if declared else 'goal_auto'}
            # Separate transaction: an unavailable audit table cannot abort job updates.
        await gate._record(pool, 'runner_spec_ref', project, job_id, verdict, detail,
                           gate.get_mode(), gate.get_timeout_seconds())
        if verdict != 'spec_bound':
            return {'verdict': verdict, 'entrypoint': 'runner_spec_ref', 'ref': job_id,
                    'mode': gate.get_mode(), 'detail': detail,
                    'message': '기준 문서 확인 경고 — 제출은 계속 진행합니다.'}
        return None

    first = await check_spec_warning(instruction=instruction, goal_id=goal_id,
        tenant_id=tenant_id, project=project, job_id=job_id, pool=pool)
    try:
        return merge_spec_warning(await asyncio.wait_for(run(), gate.get_timeout_seconds()), first)
    except Exception as exc:
        gate.logger.warning('runner_spec_ref.failed_open', job_id=job_id, error=type(exc).__name__)
        return first


def merge_spec_warning(existing, warning):
    if not warning:
        return existing
    if not existing:
        return warning
    return {**existing, 'spec_ref': warning}


async def check_spec_warning(*, instruction, tenant_id, project, job_id,
                             goal_id=None, approval=False, pool=None):
    """최신 판본 경고만 반환한다. 조회·감사는 작업 트랜잭션과 분리한다."""
    change = re.search(r"(?m)^\s*SPEC_CHANGE:|범위\s*변경|요구\s*변경|사양\s*변경|기획\s*변경", instruction)
    if not approval and (not goal_id or not change):
        return None
    refs = parse_spec_refs(instruction)
    if approval and not any(r['revision'] is not None for r in refs):
        return None

    async def run():
        nonlocal pool
        if pool is None:
            from app.core.db_pool import get_pool
            pool = get_pool()
        messages = []
        async with pool.acquire() as conn:
            if approval:
                for ref in refs:
                    if ref['revision'] is None:
                        continue
                    row = await conn.fetchrow(
                        _EXPLICIT_SQL.replace('COALESCE(h.approved_revision_id, h.latest_revision_id)',
                                              'h.latest_revision_id'),
                        tenant_id, project, ref['document_key'], None)
                    if row and row['revision'] > ref['revision']:
                        messages.append(f"⚠️ 낡은 문서 기준: 작업은 {ref['document_key']} r{ref['revision']}, 현재 r{row['revision']}")
            else:
                rows = await conn.fetch(_GOAL_SQL.replace(
                    'COALESCE(h.approved_revision_id, h.latest_revision_id)', 'h.latest_revision_id'),
                    tenant_id, project, goal_id)
                for row in rows:
                    if not any(r['document_key'] == row['document_key'] and
                               r['revision'] == row['revision'] for r in refs):
                        messages.append('변경 지시입니다. 먼저 canonical_document_register 로 '
                            f"{row['document_key']} 새 판본을 올리고 SPEC_REF: {row['document_key']}@r{row['revision'] + 1} "
                            '을 넣어 다시 제출하십시오')
        if not messages:
            return None
        entrypoint = 'runner_approve_stale' if approval else 'chat_spec_first'
        warning = dict(verdict='stale_spec' if approval else 'spec_first',
                       entrypoint=entrypoint, message='\n'.join(messages))
        await gate._record(pool, entrypoint, project, job_id, warning['verdict'],
                           warning, gate.get_mode(), gate.get_timeout_seconds())
        return warning

    try:
        return await asyncio.wait_for(run(), gate.get_timeout_seconds() * 2)
    except Exception as exc:
        gate.logger.warning('spec_warning.failed_open', job_id=job_id, error=type(exc).__name__)
        return None
