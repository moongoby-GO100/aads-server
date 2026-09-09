"""goal_task_links ↔ pipeline_jobs 재조정기 (멱등, dry-run 우선).

배경 (AADS Goal Control P0, 2026-09-09 운영 실측):
  - pipeline_jobs 가 종료돼도 링크가 queued/pending/running 으로 남아 있었다.
  - 프로젝트만 보고 붙인 암묵적 자동연결 때문에 무관한 작업이 활성 목표에 붙었다.
  - 링크가 가리키는 pipeline_jobs 행이 아예 없는 고아 링크가 다수 존재한다.

원칙:
  1. **행을 지우지 않는다.** 회수는 link_state='detached'/'orphan' 표시로만 한다
     (되돌리려면 link_state='active' 로 되돌리면 끝이다).
  2. **결정적 근거가 있을 때만 고친다.** 프로젝트 불일치, 작업 행 부재,
     같은 instruction_hash 재시도 성공 — 전부 DB 로 확인 가능한 사실이다.
  3. 근거가 약한 과거 암묵 연결(bind_source 미상)은 **보고만** 하고,
     운영자가 include_unverified=True 로 명시 요청할 때만 회수한다.
  4. dry_run=True 가 기본. 같은 입력으로 두 번 돌리면 두 번째는 0건이어야 한다.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.goal_binding import (
    LINK_STATUS_COMPLETED,
    LINK_STATE_ACTIVE,
    LINK_STATE_DETACHED,
    LINK_STATE_ORPHAN,
    LINK_STATUS_FAILED,
    UNVERIFIED_BIND_SOURCES,
    decide_supersession,
    is_terminal_job_state,
    normalize_job_state,
    parse_goal_binding,
)

logger = logging.getLogger(__name__)

DEFAULT_SCAN_LIMIT = 2000
DEFAULT_REPAIR_LIMIT = 500

# 재조정 액션 종류
ACTION_STALE_STATUS = "stale_status"
ACTION_ORPHAN = "orphan_link"
ACTION_MISBOUND_PROJECT = "misbound_project"
ACTION_UNVERIFIED_BIND = "unverified_bind"
ACTION_SUPERSEDE = "supersede_failed_retry"


async def _pool():
    from app.core.db_pool import get_pool

    return get_pool()


async def _provenance_ready(conn) -> bool:
    from app.services.goal_manager import goal_state_machine

    return await goal_state_machine._link_provenance_ready(conn)


# ─── 단일 작업 상태 전파 (durable write 직후 호출되는 중앙 진입점) ───────────
async def sync_job_status(
    job_id: str,
    status: Optional[str] = None,
    phase: Optional[str] = None,
    *,
    source: str = "",
) -> dict[str, Any]:
    """pipeline_jobs 종료 상태를 연결된 목표에 반영한다 (best-effort, 멱등).

    - status 가 없으면 DB 에서 읽는다 (호출부가 방금 쓴 durable 값).
    - 연결된 링크가 없으면 아무 것도 하지 않는다 (명시적 연결이 없는 작업은
      어떤 목표도 건드리지 않는다).
    - 같은 상태로 여러 번 불러도 결과가 같다 — 호출부는 중복 호출을 걱정하지 않아도 된다.
    실패는 절대 상위 흐름을 막지 않는다.
    """
    if not job_id:
        return {"synced": False, "reason": "missing_job_id"}
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            if status is None or phase is None:
                row = await conn.fetchrow(
                    "SELECT status, phase FROM pipeline_jobs WHERE job_id = $1", job_id,
                )
                if not row:
                    return {"synced": False, "reason": "job_not_found", "job_id": job_id}
                status = status if status is not None else row["status"]
                phase = phase if phase is not None else row["phase"]
            has_link = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM goal_task_links "
                "WHERE task_type = 'pipeline_job' AND task_id = $1)",
                job_id,
            )
        if not has_link:
            return {"synced": False, "reason": "no_linked_goal", "job_id": job_id}

        from app.services.goal_manager import goal_state_machine

        result = await goal_state_machine.update_task_status(
            "pipeline_job", job_id, status or "", phase,
        )
        logger.info(
            "goal_link_synced job=%s status=%s phase=%s normalized=%s source=%s links=%s",
            job_id, status, phase, normalize_job_state(status, phase), source or "-",
            result.get("updated"),
        )
        return {"synced": True, "job_id": job_id, "result": result}
    except Exception as exc:  # noqa: BLE001 — 목표 반영 실패가 작업 흐름을 막지 않는다
        logger.warning("goal_link_sync_failed job=%s source=%s: %s", job_id, source or "-", exc)
        return {"synced": False, "reason": "error", "error": str(exc)[:300], "job_id": job_id}


async def mark_superseded(job_id: str, superseded_by: str, reason: str) -> dict[str, Any]:
    """대체 관계가 **호출 시점에 이미 확정된** 경우 링크에 승계를 바로 기록한다.

    예: 중복 approved 병합에서 최신 작업만 남기고 나머지를 cancelled 로 만들 때,
    "누가 누구를 대체했는지"가 그 자리에서 결정적이므로 재조정 스윕을 기다릴 필요가 없다.
    승계된 링크는 마일스톤을 막지 않는다 (진행 판정에서 제외).
    """
    if not job_id or not superseded_by or job_id == superseded_by:
        return {"marked": 0, "reason": "invalid_arguments"}
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            if not await _provenance_ready(conn):
                return {"marked": 0, "reason": "migration_pending"}
            rows = await conn.fetch(
                """
                UPDATE goal_task_links
                SET superseded_by = $2, superseded_at = NOW(), supersede_reason = $3,
                    reconciled_at = NOW(), updated_at = NOW()
                WHERE task_type = 'pipeline_job' AND task_id = $1
                  AND link_state = 'active' AND superseded_by IS NULL
                RETURNING milestone_id::text AS milestone_id
                """,
                job_id, superseded_by, reason,
            )
            unblocked = []
            for row in rows:
                if row["milestone_id"] and await _unblock_if_clear(conn, row["milestone_id"]):
                    unblocked.append(row["milestone_id"])
        return {"marked": len(rows), "unblocked_milestones": unblocked}
    except Exception as exc:  # noqa: BLE001
        logger.warning("goal_link_supersede_failed job=%s: %s", job_id, exc)
        return {"marked": 0, "reason": "error", "error": str(exc)[:300]}


async def sync_job_status_if_terminal(
    job_id: str,
    status: Optional[str] = None,
    phase: Optional[str] = None,
    *,
    source: str = "",
) -> dict[str, Any]:
    """종료 상태일 때만 전파한다 (진행 중 상태 write 에서 부담 없이 호출 가능)."""
    if status is not None and not is_terminal_job_state(status, phase):
        return {"synced": False, "reason": "not_terminal", "job_id": job_id}
    return await sync_job_status(job_id, status, phase, source=source)


# ─── 전수 재조정 ────────────────────────────────────────────────────────────
def _job_has_explicit_binding(row: Any, link_goal_id: str) -> bool:
    """작업 자신이 이 목표를 명시적으로 가리키는가 (결정적 근거)."""
    job_goal_id = row.get("job_goal_id")
    if job_goal_id and str(job_goal_id) == link_goal_id:
        return True
    binding = parse_goal_binding(row.get("instruction") or "")
    return bool(binding.goal_id and binding.goal_id == link_goal_id.lower())


async def _load_links(conn, project: Optional[str], limit: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT l.id::text          AS link_id,
               l.goal_id::text     AS goal_id,
               l.milestone_id::text AS milestone_id,
               l.task_id           AS task_id,
               l.status            AS link_status,
               l.link_state        AS link_state,
               l.bind_source       AS bind_source,
               l.superseded_by     AS superseded_by,
               j.job_id            AS job_id,
               j.status            AS job_status,
               j.phase             AS job_phase,
               j.project           AS job_project,
               j.instruction_hash  AS instruction_hash,
               j.created_at        AS job_created_at,
               j.goal_id::text     AS job_goal_id,
               LEFT(COALESCE(j.instruction, ''), 4000) AS instruction,
               g.project           AS goal_project,
               g.status            AS goal_status
        FROM goal_task_links l
        LEFT JOIN pipeline_jobs j
               ON j.job_id = l.task_id AND l.task_type = 'pipeline_job'
        LEFT JOIN goals g ON g.id = l.goal_id
        WHERE l.task_type = 'pipeline_job'
          AND ($1::text IS NULL OR g.project = $1)
        ORDER BY l.created_at
        LIMIT $2
        """,
        project,
        limit,
    )
    return [dict(row) for row in rows]


async def _load_retry_candidates(conn, hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not hashes:
        return {}
    rows = await conn.fetch(
        """
        SELECT job_id, instruction_hash, status, phase, created_at
        FROM pipeline_jobs
        WHERE instruction_hash = ANY($1::text[])
        """,
        hashes,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["instruction_hash"], []).append(dict(row))
    return grouped


def _plan_actions(
    links: list[dict[str, Any]],
    retry_candidates: dict[str, list[dict[str, Any]]],
    *,
    include_unverified: bool,
) -> list[dict[str, Any]]:
    """링크별로 필요한 조치를 결정한다 (순수 판정 — DB 를 건드리지 않는다)."""
    actions: list[dict[str, Any]] = []
    for link in links:
        if link.get("link_state") != LINK_STATE_ACTIVE:
            continue  # 이미 회수된 링크는 다시 손대지 않는다 (멱등)
        goal_id = link.get("goal_id") or ""

        # 1) 고아: 링크가 가리키는 pipeline_jobs 행이 없다.
        #    pipeline_cleanup.cleanup_stale_jobs 가 1시간 지난 종료 작업을 지우므로
        #    고아 자체는 정상적으로 생긴다. 이미 종료 판정이 링크에 남아 있으면
        #    그 판정을 **그대로 존중**하고(완료 기여분을 잃지 않는다), 판정이 없는
        #    링크만 orphan 으로 표시해 진행 판정에서 제외한다.
        if not link.get("job_id"):
            if (link.get("link_status") or "") in (LINK_STATUS_COMPLETED, LINK_STATUS_FAILED):
                continue
            actions.append({
                "action": ACTION_ORPHAN,
                "link_id": link["link_id"],
                "task_id": link["task_id"],
                "goal_id": goal_id,
                "milestone_id": link.get("milestone_id"),
                "reason": "pipeline_jobs row missing and link never reached a terminal status",
            })
            continue

        # 2) 오연결: 작업 프로젝트 ≠ 목표 프로젝트 (결정적)
        job_project = str(link.get("job_project") or "").upper()
        goal_project = str(link.get("goal_project") or "").upper()
        if job_project and goal_project and job_project != goal_project:
            actions.append({
                "action": ACTION_MISBOUND_PROJECT,
                "link_id": link["link_id"],
                "task_id": link["task_id"],
                "goal_id": goal_id,
                "milestone_id": link.get("milestone_id"),
                "reason": f"job_project={job_project} goal_project={goal_project}",
            })
            continue

        # 3) 근거 미상 연결: 계보가 unknown/legacy 이고 작업 자신도 이 목표를 가리키지 않는다
        unverified = (
            (link.get("bind_source") or "") in UNVERIFIED_BIND_SOURCES
            and not _job_has_explicit_binding(link, goal_id)
        )
        if unverified:
            actions.append({
                "action": ACTION_UNVERIFIED_BIND,
                "link_id": link["link_id"],
                "task_id": link["task_id"],
                "goal_id": goal_id,
                "milestone_id": link.get("milestone_id"),
                "reason": f"bind_source={link.get('bind_source') or 'unknown'} and job has no GOAL_ID",
                "applied": include_unverified,
            })
            if include_unverified:
                continue
            # 회수하지 않기로 했으면 보고만 하고, 상태 정합(4)은 그대로 이어서 맞춘다.

        # 4) 상태 불일치: 링크 상태가 작업의 현재 종료 상태와 다르다
        normalized = normalize_job_state(link.get("job_status"), link.get("job_phase"))
        if normalized != (link.get("link_status") or ""):
            actions.append({
                "action": ACTION_STALE_STATUS,
                "link_id": link["link_id"],
                "task_id": link["task_id"],
                "goal_id": goal_id,
                "milestone_id": link.get("milestone_id"),
                "from": link.get("link_status"),
                "to": normalized,
                "reason": f"job_status={link.get('job_status')} phase={link.get('job_phase')}",
            })

        # 5) 승계: 실패 링크가 같은 instruction_hash 의 후속 성공으로 대체됐다
        effective_status = normalized if normalized else link.get("link_status")
        if effective_status == LINK_STATUS_FAILED and not link.get("superseded_by"):
            decision = decide_supersession(
                link_status=LINK_STATUS_FAILED,
                job_id=str(link.get("job_id") or ""),
                instruction_hash=link.get("instruction_hash"),
                job_created_at=link.get("job_created_at"),
                candidates=retry_candidates.get(link.get("instruction_hash") or "", []),
            )
            if decision.superseded:
                actions.append({
                    "action": ACTION_SUPERSEDE,
                    "link_id": link["link_id"],
                    "task_id": link["task_id"],
                    "goal_id": goal_id,
                    "milestone_id": link.get("milestone_id"),
                    "superseded_by": decision.superseded_by,
                    "reason": decision.reason,
                })
    return actions


async def _apply_action(conn, action: dict[str, Any], note: str) -> None:
    kind = action["action"]
    link_id = action["link_id"]
    if kind == ACTION_STALE_STATUS:
        await conn.execute(
            "UPDATE goal_task_links SET status = $2, reconciled_at = NOW(), "
            "reconcile_note = $3, updated_at = NOW() WHERE id = $1::uuid",
            link_id, action["to"], note,
        )
    elif kind == ACTION_ORPHAN:
        await conn.execute(
            "UPDATE goal_task_links SET link_state = $2, reconciled_at = NOW(), "
            "reconcile_note = $3, updated_at = NOW() WHERE id = $1::uuid",
            link_id, LINK_STATE_ORPHAN, note,
        )
    elif kind in (ACTION_MISBOUND_PROJECT, ACTION_UNVERIFIED_BIND):
        await conn.execute(
            "UPDATE goal_task_links SET link_state = $2, reconciled_at = NOW(), "
            "reconcile_note = $3, updated_at = NOW() WHERE id = $1::uuid",
            link_id, LINK_STATE_DETACHED, note,
        )
    elif kind == ACTION_SUPERSEDE:
        await conn.execute(
            "UPDATE goal_task_links SET superseded_by = $2, superseded_at = NOW(), "
            "supersede_reason = $3, reconciled_at = NOW(), reconcile_note = $4, "
            "updated_at = NOW() WHERE id = $1::uuid",
            link_id, action["superseded_by"], action.get("reason"), note,
        )


async def _unblock_if_clear(conn, milestone_id: str) -> bool:
    """유효한 실패 링크가 남아 있지 않은 blocked 마일스톤을 진행 중으로 되돌린다."""
    blocking = await conn.fetchval(
        """
        SELECT COUNT(*) FROM goal_task_links
        WHERE milestone_id = $1::uuid
          AND link_state = 'active' AND superseded_by IS NULL
          AND status = 'failed'
        """,
        milestone_id,
    )
    if blocking:
        return False
    updated = await conn.fetchval(
        """
        UPDATE milestones SET status = 'in_progress',
               started_at = COALESCE(started_at, NOW()), updated_at = NOW()
        WHERE id = $1::uuid AND status = 'blocked'
        RETURNING id::text
        """,
        milestone_id,
    )
    if not updated:
        return False
    await conn.execute(
        """
        UPDATE goals SET status = 'active', updated_at = NOW()
        WHERE id = (SELECT goal_id FROM milestones WHERE id = $1::uuid)
          AND status = 'blocked'
          AND NOT EXISTS (
            SELECT 1 FROM milestones m
            WHERE m.goal_id = goals.id AND m.status = 'blocked'
          )
        """,
        milestone_id,
    )
    return True


async def reconcile_goal_links(
    project: Optional[str] = None,
    *,
    dry_run: bool = True,
    limit: int = DEFAULT_REPAIR_LIMIT,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    include_unverified: bool = False,
) -> dict[str, Any]:
    """goal_task_links 를 pipeline_jobs 기준으로 재조정한다.

    dry_run=True(기본)면 아무 것도 쓰지 않고 계획만 돌려준다.
    dry_run=False 면 최대 `limit` 건까지만 적용하고, 영향받은 마일스톤/목표의
    완료 판정과 진행률을 다시 계산한다.
    """
    pool = await _pool()
    async with pool.acquire() as conn:
        if not await _provenance_ready(conn):
            return {
                "ok": False,
                "reason": "migration_pending",
                "detail": "migrations/165_goal_link_binding_and_supersession.sql 적용 후 사용 가능",
            }
        links = await _load_links(conn, project, scan_limit)
        hashes = sorted({
            link["instruction_hash"] for link in links
            if link.get("instruction_hash")
            and normalize_job_state(link.get("job_status"), link.get("job_phase")) == LINK_STATUS_FAILED
        })
        retry_candidates = await _load_retry_candidates(conn, hashes)

    actions = _plan_actions(links, retry_candidates, include_unverified=include_unverified)
    planned = [a for a in actions if a.get("applied") is not False]
    reported_only = [a for a in actions if a.get("applied") is False]

    summary: dict[str, int] = {}
    for action in actions:
        key = action["action"]
        summary[key] = summary.get(key, 0) + 1

    report: dict[str, Any] = {
        "ok": True,
        "project": project or "ALL",
        "dry_run": dry_run,
        "scanned_links": len(links),
        "summary": summary,
        "planned": len(planned),
        "reported_only": len(reported_only),
        "include_unverified": include_unverified,
        "samples": planned[:20],
        "reported_only_samples": reported_only[:20],
        "applied": 0,
        "milestones_rechecked": [],
        "goals_recomputed": [],
        "unblocked_milestones": [],
    }
    if dry_run or not planned:
        return report

    note = f"goal_link_reconciler project={project or 'ALL'}"
    applied = 0
    touched_milestones: set[str] = set()
    touched_goals: set[str] = set()
    async with pool.acquire() as conn:
        for action in planned[:limit]:
            async with conn.transaction():
                await _apply_action(conn, action, note)
            applied += 1
            if action.get("milestone_id"):
                touched_milestones.add(str(action["milestone_id"]))
            if action.get("goal_id"):
                touched_goals.add(str(action["goal_id"]))
        unblocked = []
        for milestone_id in sorted(touched_milestones):
            if await _unblock_if_clear(conn, milestone_id):
                unblocked.append(milestone_id)

    from app.services.goal_manager import goal_state_machine

    rechecked = []
    for milestone_id in sorted(touched_milestones):
        rechecked.append(await goal_state_machine.check_milestone_completion(milestone_id))
    for goal_id in sorted(touched_goals):
        await goal_state_machine._update_goal_progress(goal_id)

    report["applied"] = applied
    report["milestones_rechecked"] = rechecked
    report["goals_recomputed"] = sorted(touched_goals)
    report["unblocked_milestones"] = unblocked
    logger.info(
        "goal_link_reconcile project=%s applied=%d milestones=%d goals=%d",
        project or "ALL", applied, len(touched_milestones), len(touched_goals),
    )
    return report
