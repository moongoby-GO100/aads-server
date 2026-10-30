"""러너 연결과 마일스톤 판정에 따른 업무 항목 상태를 동기화한다."""
from __future__ import annotations

import re
from uuid import UUID


def parse_work_item_ids(instruction):
    """지시문에서 명시한 업무 항목 UUID를 중복 없이 추출한다."""
    ids = []
    for value in re.findall(r"(?im)^\s*WORK_ITEM_ID:\s*([^\r\n]+)", instruction or ""):
        for token in re.split(r"[,\s]+", value.strip()):
            try:
                item = str(UUID(token))
            except ValueError:
                continue
            if item not in ids:
                ids.append(item)
    return ids


async def schema_ready(conn):
    """동기화에 필요한 선택 컬럼이 모두 있는지 확인한다."""
    from app.services.goal_manager import link_optional_columns

    return {'work_item_ids', 'work_items_synced_status'} <= await link_optional_columns(conn)


async def bind_work_items(conn, job_id, instruction):
    """같은 테넌트·목표·마일스톤에 속한 업무만 러너에 연결한다."""
    if not await schema_ready(conn):
        return
    ids = parse_work_item_ids(instruction)
    if not ids:
        return
    await conn.execute("""
        UPDATE goal_task_links l SET work_item_ids = ARRAY(
            SELECT w.id FROM work_items w
            WHERE w.id = ANY($2::uuid[]) AND w.type = 'task'
              AND w.goal_id = l.goal_id AND w.milestone_id = l.milestone_id
              AND w.tenant_id = l.tenant_id
        ), updated_at = NOW()
        WHERE l.task_type = 'pipeline_job' AND l.task_id = $1
          AND l.link_state = 'active' AND l.superseded_by IS NULL
    """, job_id, ids)


def transition_plan(rows, event, linked_ids=()):
    """사람의 차단·취소·반려를 보존하면서 실제 상태 변경만 계산한다."""
    states = {str(r['id']): r['status'] for r in rows}
    original = dict(states)
    linked = set(map(str, linked_ids))
    for row in rows:
        key = str(row['id'])
        state = states[key]
        if event in ('completed', 'rejected') and state == 'in_review':
            states[key] = 'completed' if event == 'completed' else 'changes_requested'
        elif row['type'] == 'task' and key in linked:
            if event == 'running' and state in ('draft', 'ready', 'changes_requested'):
                states[key] = 'in_progress'
            elif event == 'done' and state in ('draft', 'ready', 'in_progress'):
                states[key] = 'in_review'
    if event == 'done':
        for kind in ('story', 'epic'):
            for row in rows:
                key = str(row['id'])
                if row['type'] != kind or states[key] not in ('draft', 'ready', 'in_progress', 'in_review'):
                    continue
                children = [states[str(c['id'])] for c in rows if str(c['parent_id']) == key]
                if children and all(s in ('in_review', 'completed') for s in children):
                    states[key] = 'completed' if all(s == 'completed' for s in children) else 'in_review'
    return {key: state for key, state in states.items() if state != original[key]}


async def sync_milestone(conn, milestone_id, event, linked_ids=()):
    """마일스톤 잠금 아래 업무와 상위 항목의 상태를 반영한다."""
    if not await schema_ready(conn):
        return
    async with conn.transaction():
        await conn.fetchval('SELECT id FROM milestones WHERE id=$1::uuid FOR UPDATE', milestone_id)
        rows = await conn.fetch("""
            SELECT id, parent_id, type, status FROM work_items
            WHERE milestone_id=$1::uuid ORDER BY id FOR UPDATE
        """, milestone_id)
        for item_id, state in transition_plan(rows, event, linked_ids).items():
            await conn.execute("""UPDATE work_items SET status=$2, version=version+1,
                updated_at=NOW(), completed_at=CASE WHEN $2='completed' THEN NOW() ELSE NULL END
                WHERE id=$1::uuid AND status IS DISTINCT FROM $2
                  AND status NOT IN ('cancelled','blocked')""", item_id, state)


async def sync_runner(conn, job_id, status, phase=None, tenant_id=None):
    """새 러너 상태를 한 번만 반영하고 처리 상태를 연결에 기록한다."""
    if status not in ('running', 'in_progress', 'done'):
        return
    if status == 'done' and phase not in (
        'done', 'push_only_by_directive', 'deploy_already_present', 'deploy_skip_docs_only',
    ):
        return
    if not await schema_ready(conn):
        return
    async with conn.transaction():
        links = await conn.fetch("""
            SELECT milestone_id, work_item_ids FROM goal_task_links
            WHERE task_type='pipeline_job' AND task_id=$1 AND link_state='active'
              AND superseded_by IS NULL AND cardinality(work_item_ids)>0
              AND work_items_synced_status IS DISTINCT FROM $3
              AND ($2::uuid IS NULL OR tenant_id=$2::uuid)
            ORDER BY milestone_id FOR UPDATE
        """, job_id, tenant_id, status)
        for link in links:
            await sync_milestone(
                conn, str(link['milestone_id']),
                'done' if status == 'done' else 'running', link['work_item_ids'],
            )
        await conn.execute("""
            UPDATE goal_task_links SET work_items_synced_status=$3
            WHERE task_type='pipeline_job' AND task_id=$1 AND link_state='active'
              AND superseded_by IS NULL AND cardinality(work_item_ids)>0
              AND work_items_synced_status IS DISTINCT FROM $3
              AND ($2::uuid IS NULL OR tenant_id=$2::uuid)
        """, job_id, tenant_id, status)


async def reconcile_runner_items(conn, project=None):
    """아직 반영하지 않은 러너 상태만 조회하여 누락된 업무 상태를 보정한다."""
    if not await schema_ready(conn):
        return
    jobs = await conn.fetch("""
        SELECT DISTINCT j.job_id, j.status, j.phase FROM pipeline_jobs j
        JOIN goal_task_links l ON l.task_id=j.job_id AND l.task_type='pipeline_job'
        WHERE cardinality(l.work_item_ids)>0 AND l.link_state='active'
          AND l.superseded_by IS NULL AND ($1::text IS NULL OR j.project=$1)
          AND j.status IN ('running','in_progress','done')
          AND l.work_items_synced_status IS DISTINCT FROM j.status
        ORDER BY j.job_id
    """, project)
    for job in jobs:
        await sync_runner(conn, job['job_id'], job['status'], job['phase'])
