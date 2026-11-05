"""러너 연결과 마일스톤 판정에 따른 업무 항목 상태를 동기화한다."""
from __future__ import annotations

import logging
import re
from uuid import UUID, NAMESPACE_URL, uuid5

logger = logging.getLogger(__name__)


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


_MIN_SUBSTRING_LEN = 8
_MAX_CANDIDATES = 5
_OPEN_STATES = ('draft', 'ready', 'in_progress')
# 러너에 묶을 수 있는 카드 유형이자 자동 매칭 후보 유형. task 와 story 가 같이 맞으면 모호(ambiguous)다.
# 자식 없는 story 는 transition_plan 이 task 와 같은 규칙으로 직접 전이하고, 자식 있는 story 는 롤업만 한다.
_CANDIDATE_TYPES = ('task', 'story')
_RUNNING_STATES = ('running', 'in_progress', 'done')


def _norm(text):
    """대소문자·구두점·공백을 지운 비교용 문자열."""
    return re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())


def parse_directive_field(instruction, key):
    """지시서에서 `KEY: 값` 첫 줄의 값을 돌려준다. 없으면 빈 문자열."""
    match = re.search(rf"(?im)^\s*{re.escape(key)}:\s*([^\r\n]+)", instruction or "")
    return match.group(1).strip() if match else ""


def _strip_leading_tag(title):
    return re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", title or "")


def _text_matches(key, text):
    """정규화한 두 문자열이 같거나, 둘 다 충분히 길 때 한쪽이 다른 쪽에 들어 있으면 참."""
    if not key or not text:
        return False
    if key == text:
        return True  # 정확 일치는 길이와 무관하게 인정한다(짧은 카드 제목 'DB 정리' 등).
    # 부분 문자열 일치에만 최소 길이를 둔다: 'api' 같은 짧은 조각은 무관한 카드 제목에 우연히
    # 들어가므로 오탐(엉뚱한 카드에 연결)이 누락(미연결, 경고로 드러남)보다 해롭다.
    return min(len(key), len(text)) >= _MIN_SUBSTRING_LEN and (key in text or text in key)


def match_work_items(cards, job_id, task_id, title):
    """TASK_ID·TITLE·러너 id 와 일치하는 카드를 모두 고른다. 개수 판단은 호출부가 한다."""
    keys = {k for k in (_norm(task_id), _norm(title), _norm(_strip_leading_tag(title))) if k}
    runner = (job_id or "").strip().lower()
    hits = []
    for card in cards:
        texts = (_norm(card['title']), _norm(card['idempotency_key']))
        hit = bool(runner) and runner in (card['title'] or "").lower()
        hit = hit or any(_text_matches(key, text) for key in keys for text in texts)
        if hit:
            hits.append(card)
    return hits


def _brief(cards):
    return [{'id': str(c['id']), 'title': c['title']} for c in cards[:_MAX_CANDIDATES]]


def _unbound(reason, detail, candidates=(), ignored=()):
    out = {'bound': False, 'reason': reason, 'detail': detail, 'candidates': list(candidates)}
    if ignored:
        out['ignored'] = list(ignored)
    return out


async def _classify_ignored(conn, job_id, requested, bound, scope):
    """WORK_ITEM_ID 로 선언했지만 묶이지 않은 id 와 그 이유."""
    rejected = [i for i in requested if i not in bound]
    if not rejected:
        return []
    rows = {str(r['id']): r for r in await conn.fetch("""
        SELECT id, type, goal_id, milestone_id, tenant_id FROM work_items WHERE id = ANY($1::uuid[])
    """, rejected)}
    ignored = []
    for item in rejected:
        row = rows.get(item)
        if row is None:
            reason = 'not_found'
        elif (str(row['goal_id']), str(row['milestone_id']), str(row['tenant_id'])) != scope:
            reason = 'not_in_goal_milestone_tenant'
        elif row['type'] not in _CANDIDATE_TYPES:
            reason = f"type_{row['type']}_not_bindable"
        else:
            reason = 'not_bindable'
        ignored.append({'id': item, 'reason': reason})
        logger.warning("work_item_directive_id_ignored job=%s id=%s reason=%s", job_id, item, reason)
    return ignored


async def _auto_match(conn, job_id, link, instruction, scope):
    """같은 마일스톤의 열린 카드 중 TASK_ID/TITLE/러너 id 와 맞는 카드가 정확히 1개일 때만 고른다."""
    cards = await conn.fetch("""
        SELECT w.id, w.type, w.title, w.idempotency_key FROM work_items w
        WHERE w.goal_id = $1::uuid AND w.milestone_id = $2::uuid AND w.tenant_id = $3::uuid
          AND w.type = ANY($4::text[]) AND w.status = ANY($5::text[]) ORDER BY w.created_at, w.id
    """, *scope, list(_CANDIDATE_TYPES), list(_OPEN_STATES))
    hits = match_work_items(cards, job_id, parse_directive_field(instruction, 'TASK_ID'),
                            parse_directive_field(instruction, 'TITLE'))
    if len(hits) > 1:
        return None, _unbound('ambiguous', f'일치하는 카드가 {len(hits)}개라 연결하지 않았다', _brief(hits))
    if not hits:
        return None, _unbound('no_match', '같은 마일스톤에 TASK_ID/TITLE 과 일치하는 열린 카드가 없다',
                              _brief(cards))
    return [str(hits[0]['id'])], None


async def _bind_one_link(conn, job_id, link, instruction, requested):
    """opt-in 제출의 링크 하나를 지시서 → 자동 매칭 순서로 채운다. (결과, 새로 묶였는지)"""
    if link['milestone_id'] is None or link['goal_id'] is None:
        return _unbound('no_milestone', '목표만 연결되고 마일스톤이 없어 카드 범위를 정할 수 없다'), False
    scope = (str(link['goal_id']), str(link['milestone_id']), str(link['tenant_id']))
    if requested:
        # 선언한 id 가 하나라도 있으면 자동 매칭으로 덮지 않는다(선언이 틀렸다는 사실이 드러나야 한다).
        bound = [str(i) for i in link['work_item_ids'] or ()]
        ignored = await _classify_ignored(conn, job_id, requested, set(bound), scope)
        if not bound:
            return _unbound('directive_ids_not_found',
                            'WORK_ITEM_ID 가 같은 목표·마일스톤·테넌트의 task/story 카드가 아니라 모두 무시했다',
                            ignored=ignored), False
        out = {'bound': True, 'source': 'directive', 'work_item_ids': bound}
        if ignored:
            out['ignored'] = ignored
        # 같은 ids 로 이미 동기화된 링크를 재적용해도 다시 묶인 것이 아니다. ids 가 바뀌었으면 UPDATE 가
        # 처리 상태를 비웠으므로 NULL 여부 하나로 "아직 반영하지 않음"을 판단한다.
        return out, link['work_items_synced_status'] is None
    if link['work_item_ids']:
        return {'bound': True, 'source': 'existing',
                'work_item_ids': [str(i) for i in link['work_item_ids']]}, False
    ids, failure = await _auto_match(conn, job_id, link, instruction, scope)
    if failure:
        return failure, False
    await conn.execute("""
        UPDATE goal_task_links SET work_item_ids = $3::uuid[], work_items_synced_status = NULL,
               updated_at = NOW()
        WHERE id = $2::uuid AND task_type = 'pipeline_job' AND task_id = $1
    """, job_id, str(link['id']), ids)
    return {'bound': True, 'source': 'auto_match', 'work_item_ids': ids}, True


async def _job_state(conn, job_id):
    """러너의 (status, phase, tenant_id). 큐에서 아카이브로 옮겨졌으면 아카이브에서 읽는다."""
    job = await conn.fetchrow(
        "SELECT status, phase, tenant_id::text AS tenant_id FROM pipeline_jobs WHERE job_id = $1", job_id)
    if job:
        return job
    try:
        async with conn.transaction():  # 세이브포인트: 아카이브 조회 실패가 바깥 트랜잭션을 오염시키지 않는다
            return await conn.fetchrow("""
                SELECT status, row_data->>'phase' AS phase, row_data->>'tenant_id' AS tenant_id
                FROM pipeline_jobs_archive WHERE job_id = $1
            """, job_id)
    except Exception as exc:  # noqa: BLE001 — 아카이브 미설치 환경에서는 사후 동기화만 건너뛴다
        logger.warning("work_item_archive_lookup_failed job=%s: %s", job_id, str(exc)[:200])
        return None


async def bind_work_items(conn, job_id, instruction):
    """같은 테넌트·목표·마일스톤에 속한 task/story 카드만 러너 링크에 연결한다.

    * `WORK_ITEM_ID:` 선언은 모든 목표 연결 제출에 적용한다(기존 동작).
    * 자동 매칭·연결 결과 보고·즉시 동기화는 지시서에 `GOAL_ID:` 줄이 있는 제출(opt-in)에만 적용한다.
      세션으로만 목표에 붙은 제출이나 목표 없는 제출은 기존 동작 그대로이며 None 을 돌려준다.

    반환: opt-in 제출이면 ``{"bound": bool, "source"|"reason": ..., "candidates": [...]}``.
    새로 연결했는데 러너가 이미 실행 중이거나 끝났으면(아카이브 포함) 카드 상태를 한 번 동기화한다.
    """
    from app.services.goal_binding import resolve_declared_ids

    opt_in = bool(resolve_declared_ids(instruction).goal_id)
    if not await schema_ready(conn):
        return _unbound('schema_not_ready', 'work_item_ids 컬럼이 없어 연결할 수 없다') if opt_in else None
    requested = parse_work_item_ids(instruction)
    if requested:
        await conn.execute("""
            UPDATE goal_task_links l SET work_item_ids = ARRAY(
                SELECT w.id FROM work_items w
                WHERE w.id = ANY($2::uuid[]) AND w.type IN ('task', 'story')
                  AND w.goal_id = l.goal_id AND w.milestone_id = l.milestone_id
                  AND w.tenant_id = l.tenant_id
                ORDER BY w.id
            ), work_items_synced_status = CASE WHEN l.work_item_ids IS DISTINCT FROM ARRAY(
                SELECT w.id FROM work_items w
                WHERE w.id = ANY($2::uuid[]) AND w.type IN ('task', 'story')
                  AND w.goal_id = l.goal_id AND w.milestone_id = l.milestone_id
                  AND w.tenant_id = l.tenant_id
                ORDER BY w.id
            ) THEN NULL ELSE l.work_items_synced_status END, updated_at = NOW()
            WHERE l.task_type = 'pipeline_job' AND l.task_id = $1
              AND l.link_state = 'active' AND l.superseded_by IS NULL
        """, job_id, requested)
    if not opt_in:
        return None
    links = await conn.fetch("""
        SELECT id, goal_id, milestone_id, tenant_id, work_item_ids, work_items_synced_status
        FROM goal_task_links
        WHERE task_type = 'pipeline_job' AND task_id = $1
          AND link_state = 'active' AND superseded_by IS NULL
        ORDER BY updated_at DESC NULLS LAST, id FOR UPDATE
    """, job_id)
    if not links:
        return _unbound('no_goal_link', '러너가 목표에 연결되지 않아 업무 카드를 연결하지 않았다')
    results, fresh = [], []
    for link in links:
        result, is_new = await _bind_one_link(conn, job_id, link, instruction, requested)
        results.append(result)
        if is_new:
            fresh.append(link)
    if fresh:
        # 방금 묶은 연결은 아직 반영한 상태가 없으니 처리 상태를 비우고 1회 동기화한다.
        for link in fresh:
            await conn.execute(
                "UPDATE goal_task_links SET work_items_synced_status = NULL WHERE id = $1::uuid", str(link['id']))
        job = await _job_state(conn, job_id)
        if job and job['status'] in _RUNNING_STATES:
            await sync_runner(conn, job_id, job['status'], job['phase'], job['tenant_id'])
    bound = [r for r in results if r['bound']]
    return bound[0] if bound else results[0]


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
        elif key in linked and (row['type'] == 'task' or (
                row['type'] == 'story' and not any(str(c['parent_id']) == key for c in rows))):
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
    from app.services.goal_workflow_approval import append_event

    changed = 0
    async with conn.transaction():
        await conn.fetchval('SELECT id FROM milestones WHERE id=$1::uuid FOR UPDATE', milestone_id)
        rows = await conn.fetch("""
            SELECT id, parent_id, type, status, tenant_id, project FROM work_items
            WHERE milestone_id=$1::uuid ORDER BY id FOR UPDATE
        """, milestone_id)
        originals = {str(row["id"]): dict(row) for row in rows}
        for item_id, state in transition_plan(rows, event, linked_ids).items():
            result = await conn.execute("""UPDATE work_items SET status=$2, version=version+1,
                updated_at=NOW(), completed_at=CASE WHEN $2='completed' THEN NOW() ELSE NULL END
                WHERE id=$1::uuid AND status IS DISTINCT FROM $2
                  AND status NOT IN ('cancelled','blocked')""", item_id, state)
            if result != 'UPDATE 1':
                continue
            row = originals[item_id]
            await append_event(
                conn, tenant_id=str(row['tenant_id']), project=row['project'],
                aggregate_type='work_item', aggregate_id=item_id,
                event_type='status_synced_from_milestone', actor=None,
                payload={'from': row['status'], 'to': state,
                         'milestone_id': str(milestone_id), 'event': event},
                correlation_id=str(uuid5(NAMESPACE_URL, f'milestone:{milestone_id}')),
            )
            changed += 1
    return changed


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


_RECONCILE_WHERE = """
        WHERE cardinality(l.work_item_ids)>0 AND l.link_state='active'
          AND l.superseded_by IS NULL AND ($1::text IS NULL OR j.project=$1)
          AND j.status IN ('running','in_progress','done')
          AND l.work_items_synced_status IS DISTINCT FROM j.status
        ORDER BY j.job_id
"""


async def reconcile_runner_items(conn, project=None):
    """아직 반영하지 않은 러너 상태만 조회하여 누락된 업무 상태를 보정한다.

    pipeline_jobs 에서 pipeline_jobs_archive 로 옮겨진 러너도 같은 기준으로 훑는다.
    """
    if not await schema_ready(conn):
        return
    jobs = list(await conn.fetch("""
        SELECT DISTINCT j.job_id, j.status, j.phase FROM pipeline_jobs j
        JOIN goal_task_links l ON l.task_id=j.job_id AND l.task_type='pipeline_job'
""" + _RECONCILE_WHERE, project))
    try:
        async with conn.transaction():  # 세이브포인트: 아카이브 조회 실패가 보정 전체를 되돌리지 않게 한다
            jobs += await conn.fetch("""
                SELECT DISTINCT j.job_id, j.status, j.phase FROM (
                    SELECT a.job_id, a.project, a.status, a.row_data->>'phase' AS phase
                    FROM pipeline_jobs_archive a
                    WHERE NOT EXISTS (SELECT 1 FROM pipeline_jobs p WHERE p.job_id=a.job_id)
                ) j
                JOIN goal_task_links l ON l.task_id=j.job_id AND l.task_type='pipeline_job'
""" + _RECONCILE_WHERE, project)
    except Exception as exc:  # noqa: BLE001
        logger.warning("work_item_reconcile_archive_failed: %s", str(exc)[:200])
    for job in jobs:
        await sync_runner(conn, job['job_id'], job['status'], job['phase'])
