"""goal_task_links ↔ pipeline_jobs 재조정기 (dry-run 우선, 멱등, 제한적 복구).

무엇을 고치나
-------------
* **stale**   : pipeline_jobs 는 종료됐는데 링크가 queued/pending/running 으로 남은 행
                → 정규화된 상태로 갱신한다.
* **orphan**  : task_id 에 해당하는 pipeline_jobs 행이 없는 링크
                → 지우지 않고 link_state='orphan' 으로 격리한다.
* **misbound**: 링크가 걸린 목표의 project 와 작업의 project 가 다른 행
                → link_state='detached' + detach_reason. **결정적 근거가 있을 때만**.
* **superseded**: 같은 instruction_hash 로 나중에 성공한 작업이 있는 실패 링크
                → superseded_by 기록 → 마일스톤 차단 해제.
* **legacy_unverified**: bind_source 가 legacy_auto_project/unknown 인 행
                → **보고만** 한다. "프로젝트만 보고 붙었다"는 사실은 그 링크가
                  틀렸다는 결정적 근거가 아니므로 자동 분리하지 않는다.
                  운영자가 detach_legacy=True 로 명시할 때만 제한 수량으로 회수한다.

안전 규칙
---------
* 기본은 dry_run=True — 아무것도 쓰지 않고 계획만 돌려준다.
* 행을 DELETE 하지 않는다. 회수는 link_state 전환으로만 표현한다.
* limit 으로 한 번에 건드리는 행 수를 제한한다(기본 200).
* migration 166 미적용 스키마에서는 읽기 전용 보고로 자동 격하된다.
* 같은 입력으로 두 번 돌리면 두 번째는 repaired=0 이다(멱등).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.goal_binding import (
    BIND_SOURCE_LEGACY_AUTO,
    BIND_SOURCE_UNKNOWN,
    LINK_STATE_DETACHED,
    LINK_STATE_ORPHAN,
    normalize_job_state,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

# 재조정이 손대지 않는 링크 상태 — 이미 회수/격리된 행은 다시 건드리지 않는다.
_UNTOUCHED_STATES = (LINK_STATE_DETACHED, LINK_STATE_ORPHAN)


async def _has_schema(conn) -> bool:
    from app.services.goal_manager import link_optional_columns

    columns = await link_optional_columns(conn)
    return {"link_state", "bind_source", "superseded_by"}.issubset(columns)


async def _load_candidates(conn, project: Optional[str], limit: int) -> list[dict[str, Any]]:
    """링크 + 연결된 작업 + 목표 프로젝트를 한 번에 읽는다 (읽기 전용)."""
    rows = await conn.fetch(
        """
        SELECT l.id::text          AS link_id,
               l.goal_id::text     AS goal_id,
               l.milestone_id::text AS milestone_id,
               l.task_type,
               l.task_id,
               l.status            AS link_status,
               COALESCE(l.link_state, 'active')   AS link_state,
               COALESCE(l.bind_source, 'unknown') AS bind_source,
               l.superseded_by,
               j.status            AS job_status,
               j.phase             AS job_phase,
               j.project           AS job_project,
               g.project           AS goal_project,
               g.status            AS goal_status,
               m.status            AS milestone_status
        FROM goal_task_links l
        LEFT JOIN pipeline_jobs j
               ON l.task_type = 'pipeline_job' AND j.job_id = l.task_id
        LEFT JOIN goals g ON g.id = l.goal_id
        LEFT JOIN milestones m ON m.id = l.milestone_id
        WHERE ($1::text IS NULL OR g.project = $1::text OR j.project = $1::text)
        ORDER BY l.created_at DESC
        LIMIT $2
        """,
        project,
        min(max(limit, 1), MAX_LIMIT) * 4,  # 후보는 넉넉히 읽고, 수정 건수만 limit 으로 제한
    )
    return [dict(r) for r in rows]


def plan_actions(candidates: list[dict[str, Any]], *, detach_legacy: bool = False) -> list[dict[str, Any]]:
    """읽어온 행에서 수행할 조치를 계산한다 (순수 함수 — DB 접근 없음).

    한 링크당 조치는 최대 하나다. 우선순위: orphan > misbound > stale.
    """
    actions: list[dict[str, Any]] = []
    for row in candidates:
        if row.get("link_state") in _UNTOUCHED_STATES:
            continue

        # 1) 작업 행이 사라진 링크 → 격리 (삭제하지 않음)
        if row.get("task_type") == "pipeline_job" and not row.get("job_status"):
            actions.append({
                "kind": "orphan",
                "link_id": row["link_id"],
                "task_id": row["task_id"],
                "goal_id": row.get("goal_id"),
                "milestone_id": row.get("milestone_id"),
                "new_link_state": LINK_STATE_ORPHAN,
                "reason": "pipeline_jobs row not found",
            })
            continue

        # 2) 목표 프로젝트 ≠ 작업 프로젝트 → 결정적 오연결
        goal_project = row.get("goal_project")
        job_project = row.get("job_project")
        if goal_project and job_project and goal_project != job_project:
            actions.append({
                "kind": "misbound",
                "link_id": row["link_id"],
                "task_id": row["task_id"],
                "goal_id": row.get("goal_id"),
                "milestone_id": row.get("milestone_id"),
                "new_link_state": LINK_STATE_DETACHED,
                "reason": f"cross_project: goal={goal_project} job={job_project}",
            })
            continue

        # 3) 명시적 근거 없이 붙은 레거시 링크 → 기본은 보고만
        if row.get("bind_source") in (BIND_SOURCE_LEGACY_AUTO, BIND_SOURCE_UNKNOWN):
            if detach_legacy:
                actions.append({
                    "kind": "legacy_detach",
                    "link_id": row["link_id"],
                    "task_id": row["task_id"],
                    "goal_id": row.get("goal_id"),
                    "milestone_id": row.get("milestone_id"),
                    "new_link_state": LINK_STATE_DETACHED,
                    "reason": f"legacy_auto_project binding revoked by operator (bind_source={row.get('bind_source')})",
                })
                continue
            actions.append({
                "kind": "legacy_unverified",
                "link_id": row["link_id"],
                "task_id": row["task_id"],
                "goal_id": row.get("goal_id"),
                "milestone_id": row.get("milestone_id"),
                "reason": "project-only auto link; needs operator confirmation",
                "report_only": True,
            })
            # 레거시라도 상태가 밀려 있으면 아래 stale 판정을 계속 받는다.

        # 4) 링크 상태가 작업 실제 상태와 어긋남
        expected = normalize_job_state(row.get("job_status"), row.get("job_phase"))
        if expected != (row.get("link_status") or ""):
            actions.append({
                "kind": "stale",
                "link_id": row["link_id"],
                "task_id": row["task_id"],
                "goal_id": row.get("goal_id"),
                "milestone_id": row.get("milestone_id"),
                "from_status": row.get("link_status"),
                "to_status": expected,
                "reason": f"job={row.get('job_status')}/{row.get('job_phase')}",
            })
    return actions


def summarize(actions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action["kind"]] = counts.get(action["kind"], 0) + 1
    return counts


async def reconcile(
    project: Optional[str] = None,
    *,
    dry_run: bool = True,
    limit: int = DEFAULT_LIMIT,
    detach_legacy: bool = False,
    actor: str = "goal_link_reconciler",
) -> dict[str, Any]:
    """goal_task_links 를 pipeline_jobs 기준으로 재조정한다.

    dry_run=True (기본) 이면 계획만 반환하고 DB 를 쓰지 않는다.
    """
    from app.core.db_pool import get_pool
    from app.services.goal_manager import goal_state_machine

    limit = min(max(int(limit), 1), MAX_LIMIT)
    pool = get_pool()
    async with pool.acquire() as conn:
        schema_ready = await _has_schema(conn)
        candidates = await _load_candidates(conn, project, limit)
        actions = plan_actions(candidates, detach_legacy=detach_legacy)
        counts = summarize(actions)

        writable = [a for a in actions if not a.get("report_only")]
        bounded = writable[:limit]
        result: dict[str, Any] = {
            "project": project or "ALL",
            "dry_run": dry_run,
            "schema_ready": schema_ready,
            "scanned": len(candidates),
            "counts": counts,
            "planned": len(writable),
            "bounded_to": len(bounded),
            "repaired": 0,
            "blocked_milestones_to_recheck": len({
                row["milestone_id"]
                for row in candidates
                if row.get("milestone_id") and row.get("milestone_status") == "blocked"
            }),
            "actions": bounded[:50],  # 응답 크기 제한 — 전체 건수는 counts 로 본다
        }
        if dry_run:
            return result
        if not schema_ready:
            result["error"] = "migration_166_required"
            return result

        repaired = 0
        # Even an idempotent second pass must re-evaluate milestones that were
        # blocked by links already quarantined in an earlier pass.  Otherwise
        # there is no new row action to trigger lifecycle recovery.
        touched_milestones: set[str] = {
            row["milestone_id"]
            for row in candidates
            if row.get("milestone_id") and row.get("milestone_status") == "blocked"
        }
        touched_goals: set[str] = {
            row["goal_id"]
            for row in candidates
            if row.get("goal_id") and row.get("milestone_status") == "blocked"
        }
        for action in bounded:
            try:
                if action["kind"] == "stale":
                    await conn.execute(
                        """
                        UPDATE goal_task_links
                        SET status = $2, last_job_status = $2,
                            reconciled_at = NOW(), updated_at = NOW()
                        WHERE id = $1::uuid AND status IS DISTINCT FROM $2
                        """,
                        action["link_id"], action["to_status"],
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE goal_task_links
                        SET link_state = $2, detach_reason = $3,
                            reconciled_at = NOW(), updated_at = NOW()
                        WHERE id = $1::uuid AND COALESCE(link_state, 'active') <> $2
                        """,
                        action["link_id"], action["new_link_state"],
                        f"[{actor}] {action['reason']}"[:500],
                    )
                repaired += 1
                if action.get("milestone_id"):
                    touched_milestones.add(action["milestone_id"])
                if action.get("goal_id"):
                    touched_goals.add(action["goal_id"])
            except Exception as exc:  # noqa: BLE001 — 한 행 실패가 전체를 막지 않는다
                logger.warning("goal_reconcile_row_failed link=%s: %s", action["link_id"], str(exc)[:200])

        # 승계 판정은 마일스톤 단위로 한 번씩만 돌린다.
        for milestone_id in touched_milestones:
            await goal_state_machine._mark_superseded_failures(conn, milestone_id)

        result["repaired"] = repaired
        result["milestones_recomputed"] = len(touched_milestones)
        result["goals_recomputed"] = len(touched_goals)

    # E) 재조정 후 마일스톤/목표 진행률을 다시 계산한다.
    #    check_milestone_completion / _update_goal_progress 가 기존 완료 기준을
    #    그대로 쓰므로, 진짜로 기준을 통과할 때만 다음 단계가 열린다.
    for milestone_id in sorted(touched_milestones):
        try:
            await goal_state_machine.check_milestone_completion(milestone_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("goal_reconcile_milestone_failed %s: %s", milestone_id, str(exc)[:200])
    for goal_id in sorted(touched_goals):
        try:
            await goal_state_machine._update_goal_progress(goal_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("goal_reconcile_goal_failed %s: %s", goal_id, str(exc)[:200])

    logger.info(
        "goal_reconcile_done project=%s dry_run=%s repaired=%s counts=%s",
        project or "ALL", dry_run, result["repaired"], counts,
    )
    return result
