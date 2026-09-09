"""Goal Control Loop — GoalStateMachine.

goals/milestones/goal_task_links 테이블을 조작하여
목표→마일스톤→작업→완료판정→다음단계 자동개시를 구현한다.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


from app.services.goal_binding import (
    BIND_SOURCE_UNKNOWN,
    DONE_JOB_STATUSES,
    FAILED_JOB_STATUSES,
    LINK_STATE_ACTIVE,
    normalize_job_state,
)

# 상태 어휘는 goal_binding 한 곳에서만 정의한다 (러너/재조정기와 판정이 갈리지 않도록).
_DONE_TASK_STATUSES = DONE_JOB_STATUSES
_FAILED_TASK_STATUSES = FAILED_JOB_STATUSES

# 마일스톤 완료/차단 판정에 참여하는 "유효 링크" 조건.
# 재조정기가 orphan/detached 로 표시했거나 재시도 성공으로 승계된 링크는
# 진행을 막지도 않고 완료로 세지도 않는다.
_EFFECTIVE_LINK_SQL = "link_state = 'active' AND superseded_by IS NULL"

# migration 165 적용 여부 캐시 (프로세스당 1회 조회). None = 아직 확인 안 함.
_LINK_PROVENANCE_READY: Optional[bool] = None


def _record_value(row: Any, key: str, default: Any = None) -> Any:
    """asyncpg Record 에 없는 컬럼(마이그레이션 전)을 안전하게 읽는다."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value

# 목표 진행이 기존 구현 보존 정책을 지키는지 스스로 감사하기 위한 기본 intent
_GOAL_POLICY_INTENT = "goal_control"


class GoalStateMachine:

    async def _pool(self):
        from app.core.db_pool import get_pool
        return get_pool()

    async def _trace(
        self,
        run_type: str,
        *,
        input_summary: Any = "",
        output_summary: Any = "",
        project: Optional[str] = None,
        goal_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        """Goal Control Loop 진행 증거를 ohvis_harness_traces에 남긴다.

        추적 실패(테이블 없음/풀 미초기화 등)는 무시하며 목표 상태 전이 동작은
        절대 바뀌지 않는다. 호출부가 이미 커넥션을 점유 중이면 `conn`으로 넘겨
        중첩 acquire 없이 기록한다.
        """
        try:
            from app.services.ohvis_harness_trace import record_trace

            await record_trace(
                conn=conn,
                graph_run_id=f"goal:{goal_id}" if goal_id else f"goal:{run_type}",
                run_type=run_type,
                project=project,
                input_summary=input_summary,
                output_summary=output_summary,
                metadata={
                    **(metadata or {}),
                    "component": "goal_control_loop",
                    "action": run_type,
                    **({"goal_id": goal_id} if goal_id else {}),
                },
                error=error,
            )
        except Exception as exc:  # noqa: BLE001 — 추적은 비치명적
            logger.debug("goal_trace_skipped run_type=%s: %s", run_type, str(exc)[:200])

    async def _trace_policy(
        self,
        goal_id: str,
        project: str,
        title: str,
        *,
        stage: str = "create",
        conn: Any = None,
    ) -> None:
        """기존 구현 보존 정책 체크리스트를 증거로 남긴다 (비치명적).

        생성 시점뿐 아니라 **진행 중인 목표가 새 단계를 개시하는 시점**에도 남긴다.
        마이그레이션/시드로 만들어진 목표는 `create_goal`을 거치지 않으므로,
        개시 시점 기록이 없으면 진행 중 목표에는 자기감사 증거가 전혀 남지 않는다.
        """
        try:
            from app.services.task_policy_compiler import compile_task_policy

            policy = compile_task_policy(
                project=project,
                intent=_GOAL_POLICY_INTENT,
                goal_title=title,
            )
            await self._trace(
                "goal_policy",
                project=project,
                goal_id=goal_id,
                input_summary=policy.get("summary") or title,
                output_summary=(
                    f"stage={stage} "
                    f"risk_tier={policy.get('risk_tier')} "
                    f"approval_required={policy.get('approval_required')} "
                    f"checks={len(policy.get('checklist', []))}"
                ),
                metadata={"stage": stage, "policy": policy},
                conn=conn,
            )
        except Exception as exc:  # noqa: BLE001 — 정책 추적도 비치명적
            logger.debug("goal_policy_trace_skipped goal=%s: %s", goal_id, str(exc)[:200])

    async def create_goal(
        self,
        project: str,
        title: str,
        priority: str = "P2",
        success_criteria: Optional[str] = None,
        parent_goal_id: Optional[str] = None,
    ) -> dict[str, Any]:
        pool = await self._pool()
        goal_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO goals (
                    id, project, title, priority, description, success_criteria,
                    parent_goal_id, status
                )
                VALUES ($1::uuid, $2, $3, $4, $5, $5, $6::uuid, 'draft')
                """,
                goal_id, project, title, priority,
                success_criteria, parent_goal_id,
            )
        await self._trace(
            "goal_create",
            project=project,
            goal_id=goal_id,
            input_summary=f"{project} {priority} {title}",
            output_summary="status=draft",
            metadata={
                "priority": priority,
                "parent_goal_id": parent_goal_id,
                "has_success_criteria": bool(success_criteria),
            },
        )
        await self._trace_policy(goal_id, project, title)
        return {"goal_id": goal_id, "status": "draft"}

    async def activate_goal(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT project, title, status FROM goals WHERE id = $1::uuid", goal_id,
            )
            if not row:
                await self._trace(
                    "goal_activate",
                    goal_id=goal_id,
                    input_summary=f"activate goal {goal_id}",
                    output_summary="rejected",
                    error="goal_not_found",
                    conn=conn,
                )
                return {"error": "goal_not_found"}
            if row["status"] not in ("draft", "paused"):
                rejection = f"cannot_activate_from_{row['status']}"
                await self._trace(
                    "goal_activate",
                    project=row["project"],
                    goal_id=goal_id,
                    input_summary=f"activate goal {goal_id}",
                    output_summary="rejected",
                    error=rejection,
                    conn=conn,
                )
                return {"error": rejection}
            await conn.execute(
                "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1::uuid",
                goal_id,
            )
            first_ms = await conn.fetchrow(
                """SELECT id FROM milestones
                   WHERE goal_id = $1::uuid AND status = 'pending'
                   ORDER BY sequence_order LIMIT 1""",
                goal_id,
            )
            if first_ms:
                await conn.execute(
                    "UPDATE milestones SET status = 'in_progress', started_at = NOW(), updated_at = NOW() WHERE id = $1",
                    first_ms["id"],
                )
        logger.info("goal_activated: %s", goal_id)
        await self._trace(
            "goal_activate",
            project=row["project"],
            goal_id=goal_id,
            input_summary=f"activate goal {goal_id}",
            output_summary="status=active"
            + (f" first_milestone={first_ms['id']}" if first_ms else " first_milestone=none"),
        )
        # 진행 개시 시점의 보존정책 자기감사 증거 (시드/마이그레이션 생성 목표 포함)
        await self._trace_policy(goal_id, row["project"], row["title"], stage="activate")
        return {"goal_id": goal_id, "status": "active"}

    async def add_milestone(
        self,
        goal_id: str,
        title: str,
        sequence: int,
        completion_criteria: Optional[str] = None,
        auto_advance: bool = True,
    ) -> dict[str, Any]:
        pool = await self._pool()
        ms_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO milestones (id, goal_id, title, sequence_order, completion_criteria, auto_advance, status)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'pending')
                """,
                ms_id, goal_id, title, sequence,
                completion_criteria, auto_advance,
            )
        await self._trace(
            "milestone_add",
            goal_id=goal_id,
            input_summary=f"#{sequence} {title}",
            output_summary=f"milestone_id={ms_id} status=pending",
            metadata={
                "milestone_id": ms_id,
                "sequence": sequence,
                "auto_advance": auto_advance,
                "has_completion_criteria": bool(completion_criteria),
            },
        )
        return {"milestone_id": ms_id, "status": "pending"}

    async def _link_provenance_ready(self, conn) -> bool:
        """migration 165 의 계보/승계 컬럼이 적용됐는지 1회만 확인하고 캐시한다.

        배포 파이프라인은 db 자산(마이그레이션)을 먼저 적용하지만, 적용 전 이미지가
        잠깐 뜨더라도 목표 연결이 통째로 실패하지 않도록 레거시 SQL 로 자동 강등한다.
        """
        global _LINK_PROVENANCE_READY
        if _LINK_PROVENANCE_READY is None:
            try:
                _LINK_PROVENANCE_READY = bool(await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'goal_task_links'
                          AND column_name = 'link_state'
                    )
                    """
                ))
            except Exception as exc:  # noqa: BLE001 — 확인 실패 시 레거시 경로 유지
                logger.debug("link_provenance_probe_failed: %s", str(exc)[:200])
                return False
        return _LINK_PROVENANCE_READY

    async def _effective_link_clause(self, conn, *, prefix: str = " AND ") -> str:
        """유효 링크 필터 SQL 조각 (컬럼 미적용 환경에서는 빈 문자열)."""
        return f"{prefix}{_EFFECTIVE_LINK_SQL}" if await self._link_provenance_ready(conn) else ""

    async def link_task(
        self,
        goal_id: str,
        milestone_id: Optional[str],
        task_type: str,
        task_id: str,
        bind_source: str = BIND_SOURCE_UNKNOWN,
    ) -> dict[str, Any]:
        """작업을 목표/마일스톤에 연결한다 (멱등).

        `bind_source` 는 연결 근거(explicit_api / explicit_directive / reconciler /
        legacy_auto_project)를 남긴다. 호출부가 명시적 근거 없이 호출하면 'unknown'
        으로 기록되며, 재조정기가 이후 검증 대상으로 분류한다.

        검증(추가된 부분): 목표가 존재하고 종결되지 않았으며, pipeline_job 의
        프로젝트가 목표 프로젝트와 같아야 한다. 다르면 연결하지 않고 error 를 돌려준다.
        """
        pool = await self._pool()
        link_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            goal = await conn.fetchrow(
                "SELECT id, project, status FROM goals WHERE id = $1::uuid", goal_id,
            )
            if not goal:
                return {"error": "goal_not_found", "goal_id": goal_id}
            if goal["status"] in ("completed", "cancelled", "archived"):
                return {
                    "error": f"goal_not_linkable_{goal['status']}",
                    "goal_id": goal_id,
                    "goal_status": goal["status"],
                }

            if task_type == "pipeline_job":
                job_project = await conn.fetchval(
                    "SELECT project FROM pipeline_jobs WHERE job_id = $1", task_id,
                )
                if job_project and str(job_project).upper() != str(goal["project"]).upper():
                    logger.warning(
                        "goal_link_project_mismatch job=%s job_project=%s goal=%s goal_project=%s",
                        task_id, job_project, goal_id, goal["project"],
                    )
                    return {
                        "error": "project_mismatch",
                        "goal_id": goal_id,
                        "task_id": task_id,
                        "goal_project": goal["project"],
                        "task_project": job_project,
                    }

            if milestone_id is not None:
                owner_goal = await conn.fetchval(
                    "SELECT goal_id::text FROM milestones WHERE id = $1::uuid", milestone_id,
                )
                if not owner_goal:
                    return {"error": "milestone_not_found", "milestone_id": milestone_id}
                if owner_goal != str(goal["id"]):
                    return {
                        "error": "milestone_goal_mismatch",
                        "goal_id": goal_id,
                        "milestone_id": milestone_id,
                    }

            if milestone_id is None:
                milestone_row = await conn.fetchrow(
                    """
                    SELECT id FROM milestones
                    WHERE goal_id = $1::uuid AND status IN ('in_progress', 'pending')
                    ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,
                             sequence_order
                    LIMIT 1
                    """,
                    goal_id,
                )
                milestone_id = str(milestone_row["id"]) if milestone_row else None

            current_status = "pending"
            if task_type == "pipeline_job":
                job = await conn.fetchrow(
                    "SELECT status, phase FROM pipeline_jobs WHERE job_id = $1",
                    task_id,
                )
                if job:
                    current_status = normalize_job_state(job["status"], job["phase"])

            if await self._link_provenance_ready(conn):
                await conn.execute(
                    """
                    INSERT INTO goal_task_links
                        (id, goal_id, milestone_id, task_type, task_id, link_state, bind_source)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7)
                    ON CONFLICT (goal_id, task_type, task_id) WHERE (goal_id IS NOT NULL)
                    DO UPDATE SET milestone_id = COALESCE(EXCLUDED.milestone_id, goal_task_links.milestone_id),
                                  bind_source = CASE
                                      WHEN goal_task_links.bind_source IN ('unknown', 'legacy_auto_project')
                                      THEN EXCLUDED.bind_source
                                      ELSE goal_task_links.bind_source
                                  END,
                                  updated_at = NOW()
                    """,
                    link_id, goal_id,
                    milestone_id,
                    task_type, task_id,
                    LINK_STATE_ACTIVE, bind_source or BIND_SOURCE_UNKNOWN,
                )
                await conn.execute(
                    """
                    UPDATE goal_task_links
                    SET status = $4, updated_at = NOW()
                    WHERE goal_id = $1::uuid AND task_type = $2 AND task_id = $3
                    """,
                    goal_id, task_type, task_id, current_status,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO goal_task_links (id, goal_id, milestone_id, task_type, task_id)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)
                    ON CONFLICT (goal_id, task_type, task_id) WHERE (goal_id IS NOT NULL)
                    DO UPDATE SET milestone_id = COALESCE(EXCLUDED.milestone_id, goal_task_links.milestone_id)
                    """,
                    link_id, goal_id,
                    milestone_id,
                    task_type, task_id,
                )
                await conn.execute(
                    """
                    UPDATE goal_task_links
                    SET status = $4
                    WHERE goal_id = $1::uuid AND task_type = $2 AND task_id = $3
                    """,
                    goal_id, task_type, task_id, current_status,
                )
        if milestone_id:
            await self.check_milestone_completion(milestone_id)
        await self._trace(
            "goal_task_link",
            goal_id=goal_id,
            input_summary=f"{task_type}:{task_id}",
            output_summary=f"milestone_id={milestone_id} status={current_status}",
            metadata={
                "task_type": task_type,
                "task_id": task_id,
                "milestone_id": milestone_id,
                "link_id": link_id,
                "bind_source": bind_source,
            },
        )
        return {
            "link_id": link_id,
            "milestone_id": milestone_id,
            "status": current_status,
            "bind_source": bind_source,
        }

    async def update_task_status(
        self,
        task_type: str,
        task_id: str,
        status: str,
        phase: Optional[str] = None,
    ) -> dict[str, Any]:
        """작업 종료 상태를 링크에 반영한다 (멱등).

        유효(active·미승계) 링크만 갱신한다. 재조정기가 orphan/detached 로 회수했거나
        재시도로 승계된 과거 링크는 다시 살아나 마일스톤을 막지 않는다.
        """
        pool = await self._pool()
        normalized = normalize_job_state(status, phase)
        async with pool.acquire() as conn:
            effective = await self._effective_link_clause(conn)
            timestamp_set = ", updated_at = NOW()" if await self._link_provenance_ready(conn) else ""
            await conn.execute(
                f"UPDATE goal_task_links SET status = $3{timestamp_set} "
                f"WHERE task_type = $1 AND task_id = $2 AND status IS DISTINCT FROM $3{effective}",
                task_type, task_id, normalized,
            )
            links = await conn.fetch(
                f"""
                SELECT DISTINCT milestone_id, goal_id
                FROM goal_task_links
                WHERE task_type = $1 AND task_id = $2{effective}
                """,
                task_type, task_id,
            )
            results = []
            for link in links:
                if normalized == "failed" and link["milestone_id"]:
                    await conn.execute(
                        """
                        UPDATE milestones
                        SET status = 'blocked', updated_at = NOW()
                        WHERE id = $1::uuid AND status IN ('pending', 'in_progress')
                        """,
                        str(link["milestone_id"]),
                    )
                    if link["goal_id"]:
                        await conn.execute(
                            """
                            UPDATE goals
                            SET status = 'blocked', updated_at = NOW()
                            WHERE id = $1::uuid AND status IN ('draft', 'active')
                            """,
                            str(link["goal_id"]),
                        )
                    results.append({
                        "milestone_id": str(link["milestone_id"]),
                        "completed": False,
                        "status": "blocked",
                    })
                elif link["milestone_id"]:
                    r = await self.check_milestone_completion(str(link["milestone_id"]))
                    results.append(r)
                elif link["goal_id"]:
                    r = await self.advance_goal(str(link["goal_id"]))
                    results.append(r)
        await self._trace(
            "goal_task_status",
            goal_id=str(links[0]["goal_id"]) if links and links[0]["goal_id"] else None,
            input_summary=f"{task_type}:{task_id} status={status}",
            output_summary=f"normalized={normalized} links={len(results)}",
            metadata={"task_type": task_type, "task_id": task_id, "results": results},
        )
        return {"updated": len(results), "milestones_checked": results}

    async def check_milestone_completion(self, milestone_id: str) -> dict[str, Any]:
        """마일스톤 완료 판정.

        유효 링크(active·미승계)만 본다. 유효 링크가 하나도 없으면 **완료시키지 않는다** —
        orphan/detached/승계 링크만 남은 마일스톤이 조용히 완료되어 다음 단계를
        선점 개시하는 것을 막는다.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            effective = await self._effective_link_clause(conn)
            links = await conn.fetch(
                f"SELECT task_type, task_id, status FROM goal_task_links "
                f"WHERE milestone_id = $1::uuid{effective}",
                milestone_id,
            )
            if not links:
                total_links = await conn.fetchval(
                    "SELECT COUNT(*) FROM goal_task_links WHERE milestone_id = $1::uuid",
                    milestone_id,
                )
                return {
                    "milestone_id": milestone_id,
                    "completed": False,
                    "reason": "no_effective_tasks" if total_links else "no_linked_tasks",
                }

            all_done = all(
                link["status"] in _DONE_TASK_STATUSES for link in links
            )

            if not all_done:
                for link in links:
                    if link["status"] in _DONE_TASK_STATUSES:
                        continue
                    if link["status"] in _FAILED_TASK_STATUSES:
                        await conn.execute(
                            """
                            UPDATE milestones
                            SET status = 'blocked', updated_at = NOW()
                            WHERE id = $1::uuid AND status IN ('pending', 'in_progress')
                            """,
                            milestone_id,
                        )
                        return {
                            "milestone_id": milestone_id,
                            "completed": False,
                            "status": "blocked",
                            "reason": "linked_task_failed",
                        }
                    if link["task_type"] == "pipeline_job":
                        row = await conn.fetchrow(
                            "SELECT status, phase FROM pipeline_jobs WHERE job_id = $1",
                            link["task_id"],
                        )
                        normalized = normalize_job_state(row["status"], row["phase"]) if row else "pending"
                        if normalized == "completed":
                            await conn.execute(
                                f"UPDATE goal_task_links SET status = 'completed' "
                                f"WHERE milestone_id = $1::uuid AND task_id = $2{effective}",
                                milestone_id, link["task_id"],
                            )
                        elif normalized == "failed":
                            await conn.execute(
                                f"UPDATE goal_task_links SET status = 'failed' "
                                f"WHERE milestone_id = $1::uuid AND task_id = $2{effective}",
                                milestone_id, link["task_id"],
                            )
                            await conn.execute(
                                """
                                UPDATE milestones
                                SET status = 'blocked', updated_at = NOW()
                                WHERE id = $1::uuid AND status IN ('pending', 'in_progress')
                                """,
                                milestone_id,
                            )
                            return {
                                "milestone_id": milestone_id,
                                "completed": False,
                                "status": "blocked",
                                "reason": "pipeline_job_failed",
                            }
                        else:
                            all_done = False
                            break
                    else:
                        all_done = False
                        break
                else:
                    all_done = True

            if all_done:
                await conn.execute(
                    """
                    UPDATE milestones SET status = 'completed', completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1::uuid AND status != 'completed'
                    """,
                    milestone_id,
                )
                ms = await conn.fetchrow(
                    "SELECT goal_id FROM milestones WHERE id = $1::uuid",
                    milestone_id,
                )
                if ms:
                    await self._advance_after_milestone(str(ms["goal_id"]), milestone_id)

            return {"milestone_id": milestone_id, "completed": all_done}

    async def advance_goal(self, goal_id: str) -> dict[str, Any]:
        """Advance one goal along the goal -> milestone -> task timeline."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            goal = await conn.fetchrow(
                "SELECT id, project, title, status FROM goals WHERE id = $1::uuid",
                goal_id,
            )
            if not goal:
                await self._trace(
                    "goal_advance",
                    goal_id=goal_id,
                    input_summary=f"advance goal {goal_id}",
                    conn=conn,
                    output_summary="skipped",
                    error="goal_not_found",
                )
                return {"error": "goal_not_found"}
            if goal["status"] in ("completed", "blocked", "cancelled"):
                await self._trace(
                    "goal_advance",
                    project=goal["project"],
                    goal_id=goal_id,
                    input_summary=f"advance goal {goal_id}",
                    conn=conn,
                    output_summary=f"advanced=False status={goal['status']}",
                )
                return {"goal_id": goal_id, "status": goal["status"], "advanced": False}

            current = await conn.fetchrow(
                """
                SELECT id FROM milestones
                WHERE goal_id = $1::uuid AND status = 'in_progress'
                ORDER BY sequence_order
                LIMIT 1
                """,
                goal_id,
            )
            if current:
                checked = await self.check_milestone_completion(str(current["id"]))
                await self._update_goal_progress(goal_id)
                await self._trace(
                    "goal_advance",
                    project=goal["project"],
                    goal_id=goal_id,
                    input_summary=f"advance goal {goal_id}",
                    conn=conn,
                    output_summary=(
                        f"advanced={bool(checked.get('completed'))} "
                        f"milestone={current['id']} state={checked.get('status') or 'in_progress'}"
                    ),
                    metadata={"current": checked},
                )
                return {"goal_id": goal_id, "advanced": checked.get("completed", False), "current": checked}

            pending = await conn.fetchrow(
                """
                SELECT id FROM milestones
                WHERE goal_id = $1::uuid AND status = 'pending'
                ORDER BY sequence_order
                LIMIT 1
                """,
                goal_id,
            )
            if pending:
                if goal["status"] == "draft":
                    await conn.execute(
                        "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1::uuid",
                        goal_id,
                    )
                await conn.execute(
                    """
                    UPDATE milestones
                    SET status = 'in_progress', started_at = COALESCE(started_at, NOW()), updated_at = NOW()
                    WHERE id = $1::uuid
                    """,
                    str(pending["id"]),
                )
                await self._update_goal_progress(goal_id)
                await self._trace(
                    "goal_advance",
                    project=goal["project"],
                    goal_id=goal_id,
                    input_summary=f"advance goal {goal_id}",
                    conn=conn,
                    output_summary=f"advanced=True started_milestone={pending['id']}",
                    metadata={"started_milestone_id": str(pending["id"])},
                )
                # 새 단계를 개시하는 진행 중 목표에도 보존정책 증거를 남긴다
                await self._trace_policy(
                    goal_id, goal["project"], goal["title"],
                    stage="advance", conn=conn,
                )
                return {
                    "goal_id": goal_id,
                    "advanced": True,
                    "started_milestone_id": str(pending["id"]),
                }

            await self._update_goal_progress(goal_id)
            status = await conn.fetchval("SELECT status FROM goals WHERE id = $1::uuid", goal_id)
            await self._trace(
                "goal_advance",
                project=goal["project"],
                goal_id=goal_id,
                input_summary=f"advance goal {goal_id}",
                output_summary=f"advanced={status == 'completed'} status={status} no_open_milestone",
                conn=conn,
            )
            return {"goal_id": goal_id, "status": status, "advanced": status == "completed"}

    async def advance_active_goals(self, project: Optional[str] = None) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    "SELECT id FROM goals WHERE project = $1 AND status IN ('draft', 'active') ORDER BY created_at",
                    project,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id FROM goals WHERE status IN ('draft', 'active') ORDER BY project, created_at"
                )
        results = [await self.advance_goal(str(row["id"])) for row in rows]
        advanced = sum(1 for item in results if item.get("advanced"))
        await self._trace(
            "goals_advance_sweep",
            project=project,
            input_summary=f"advance_active_goals project={project or 'ALL'}",
            output_summary=f"checked={len(results)} advanced={advanced}",
            metadata={"checked": len(results), "advanced": advanced},
        )
        return {"checked": len(results), "results": results}

    async def _advance_after_milestone(self, goal_id: str, completed_milestone_id: str) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            completed_ms = await conn.fetchrow(
                "SELECT sequence_order FROM milestones WHERE id = $1::uuid",
                completed_milestone_id,
            )
            seq = completed_ms["sequence_order"] if completed_ms else -1

            # 선점 개시 방지: 직전 마일스톤이 DB 상 실제로 completed 여야 다음 단계를 연다.
            actually_completed = await conn.fetchval(
                "SELECT status = 'completed' FROM milestones WHERE id = $1::uuid",
                completed_milestone_id,
            )
            if not actually_completed:
                logger.info(
                    "milestone_advance_skipped_not_completed: %s", completed_milestone_id,
                )
                await self._update_goal_progress(goal_id)
                return

            next_ms = await conn.fetchrow(
                """SELECT id, auto_advance FROM milestones
                   WHERE goal_id = $1::uuid AND sequence_order > $2 AND status = 'pending'
                   ORDER BY sequence_order LIMIT 1""",
                goal_id, seq,
            )
            if next_ms and next_ms["auto_advance"]:
                await conn.execute(
                    "UPDATE milestones SET status = 'in_progress', started_at = NOW(), updated_at = NOW() WHERE id = $1",
                    next_ms["id"],
                )
                logger.info("milestone_auto_advanced: %s → %s", completed_milestone_id, next_ms["id"])

            await self._update_goal_progress(goal_id)

    async def _partial_milestone_progress(self, conn, goal_id: str) -> float:
        """완료되지 않은 마일스톤들이 이미 달성한 부분 진행분 (마일스톤 개수 환산).

        기존에는 "완료 마일스톤 수 / 전체"만 셌기 때문에, 진행 중 마일스톤에 붙은
        작업이 전부 끝나도 progress 가 0.0 에 머물렀다. 유효 링크 기준으로
        마일스톤 하나 미만의 부분 진행을 더해준다 (완료 판정 자체는 바꾸지 않는다).
        """
        if not await self._link_provenance_ready(conn):
            effective = ""
        else:
            effective = f" AND {_EFFECTIVE_LINK_SQL}"
        rows = await conn.fetch(
            f"""
            SELECT m.id,
                   COUNT(l.id) AS total,
                   COUNT(l.id) FILTER (WHERE l.status = 'completed') AS done
            FROM milestones m
            JOIN goal_task_links l ON l.milestone_id = m.id{effective}
            WHERE m.goal_id = $1::uuid AND m.status <> 'completed'
            GROUP BY m.id
            """,
            goal_id,
        )
        partial = 0.0
        for row in rows:
            total = row["total"] or 0
            if total > 0:
                partial += (row["done"] or 0) / total
        return partial

    async def _update_goal_progress(self, goal_id: str) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            stats = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'completed') AS completed
                FROM milestones WHERE goal_id = $1::uuid
                """,
                goal_id,
            )
            total = stats["total"] if stats else 0
            completed = stats["completed"] if stats else 0
            if total > 0:
                partial = await self._partial_milestone_progress(conn, goal_id)
                # 부분 진행분은 1.0 을 만들 수 없다 — 완료는 "모든 마일스톤 completed"로만.
                progress = min(round((completed + partial) / total, 2), 0.99)
            else:
                # 마일스톤이 없는 목표는 목표 직결 링크 기준으로 진행률을 낸다.
                progress = await self._goal_link_progress(conn, goal_id)

            if total > 0 and total == completed:
                await conn.execute(
                    """UPDATE goals SET status = 'completed', progress = 1.0,
                       completed_at = NOW(), updated_at = NOW() WHERE id = $1::uuid AND status != 'completed'""",
                    goal_id,
                )
                goal = await conn.fetchrow(
                    "SELECT project FROM goals WHERE id = $1::uuid", goal_id,
                )
                if goal:
                    next_goal = await conn.fetchrow(
                        """SELECT id FROM goals
                           WHERE project = $1 AND status = 'draft'
                           ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                                                   WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 9 END,
                                    created_at ASC
                           LIMIT 1""",
                        goal["project"],
                    )
                    if next_goal:
                        await conn.execute(
                        "UPDATE goals SET status = 'active', updated_at = NOW() WHERE id = $1",
                            next_goal["id"],
                        )
                        logger.info("goal_auto_activated: %s (after %s completed)", next_goal["id"], goal_id)
            else:
                await conn.execute(
                    "UPDATE goals SET progress = $2, updated_at = NOW() WHERE id = $1::uuid",
                    goal_id, progress,
                )

    async def _goal_link_progress(self, conn, goal_id: str) -> float:
        """마일스톤이 없는 목표의 진행률 = 완료된 유효 링크 / 전체 유효 링크."""
        effective = await self._effective_link_clause(conn)
        row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'completed') AS done
            FROM goal_task_links
            WHERE goal_id = $1::uuid{effective}
            """,
            goal_id,
        )
        total = (row["total"] if row else 0) or 0
        if total <= 0:
            return 0.0
        return min(round((row["done"] or 0) / total, 2), 0.99)

    async def check_goal_completion(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            stats = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'completed') AS completed
                FROM milestones WHERE goal_id = $1::uuid
                """,
                goal_id,
            )
            total = stats["total"] if stats else 0
            completed = stats["completed"] if stats else 0
            return {"goal_id": goal_id, "completed": total > 0 and total == completed, "total": total, "completed_count": completed}

    async def get_goal_status(self, goal_id: str) -> dict[str, Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            goal = await conn.fetchrow(
                """
                SELECT id, project, title, priority, status, description,
                       success_criteria, progress, created_at, completed_at
                FROM goals WHERE id = $1::uuid
                """,
                goal_id,
            )
            if not goal:
                return {"error": "goal_not_found"}

            milestones = await conn.fetch(
                """
                SELECT id, title, sequence_order, status, auto_advance, completion_criteria, started_at, completed_at
                FROM milestones WHERE goal_id = $1::uuid ORDER BY sequence_order
                """,
                goal_id,
            )
            if await self._link_provenance_ready(conn):
                tasks = await conn.fetch(
                    "SELECT id, milestone_id, task_type, task_id, status, link_state, "
                    "bind_source, superseded_by FROM goal_task_links WHERE goal_id = $1::uuid",
                    goal_id,
                )
            else:
                tasks = await conn.fetch(
                    "SELECT id, milestone_id, task_type, task_id, status FROM goal_task_links WHERE goal_id = $1::uuid",
                    goal_id,
                )
            task_map: dict[str, list] = {}
            for t in tasks:
                ms_key = str(t["milestone_id"]) if t["milestone_id"] else "unlinked"
                link_state = _record_value(t, "link_state", LINK_STATE_ACTIVE)
                superseded_by = _record_value(t, "superseded_by", None)
                task_map.setdefault(ms_key, []).append({
                    "task_type": t["task_type"],
                    "task_id": t["task_id"],
                    "status": t["status"],
                    # 추가 필드 (기존 3개 필드는 그대로 유지 — 하위호환)
                    "link_state": link_state,
                    "bind_source": _record_value(t, "bind_source", BIND_SOURCE_UNKNOWN),
                    "superseded_by": superseded_by,
                    "effective": link_state == LINK_STATE_ACTIVE and not superseded_by,
                })

            total = len(milestones)
            completed = sum(1 for m in milestones if m["status"] == "completed")

            return {
                "goal_id": str(goal["id"]),
                "project": goal["project"],
                "title": goal["title"],
                "priority": goal["priority"],
                "status": goal["status"],
                "description": goal["description"],
                "success_criteria": goal["success_criteria"] or goal["description"],
                "progress": float(goal["progress"]) if goal["progress"] else (completed / total if total > 0 else 0),
                "milestones_total": total,
                "milestones_completed": completed,
                "milestones": [
                    {
                        "id": str(m["id"]),
                        "title": m["title"],
                        "sequence": m["sequence_order"],
                        "status": m["status"],
                        "auto_advance": m["auto_advance"],
                        "completion_criteria": m["completion_criteria"],
                        "started_at": m["started_at"].isoformat() if m["started_at"] else None,
                        "completed_at": m["completed_at"].isoformat() if m["completed_at"] else None,
                        "tasks": task_map.get(str(m["id"]), []),
                    }
                    for m in milestones
                ],
            }

    async def list_goals(self, project: Optional[str] = None) -> list[dict[str, Any]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    """SELECT id, project, title, priority, status, progress, created_at, completed_at
                       FROM goals WHERE project = $1 ORDER BY
                       CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 WHEN 'paused' THEN 2 WHEN 'blocked' THEN 3 WHEN 'completed' THEN 4 ELSE 9 END,
                       created_at DESC""",
                    project,
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, project, title, priority, status, progress, created_at, completed_at
                       FROM goals ORDER BY
                       CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 WHEN 'paused' THEN 2 WHEN 'blocked' THEN 3 WHEN 'completed' THEN 4 ELSE 9 END,
                       created_at DESC"""
                )
        return [
            {
                "goal_id": str(r["id"]),
                "project": r["project"],
                "title": r["title"],
                "priority": r["priority"],
                "status": r["status"],
                "progress": float(r["progress"]) if r["progress"] else 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ]

    async def update_goal(self, goal_id: str, **kwargs) -> dict[str, Any]:
        pool = await self._pool()
        allowed = {"title", "priority", "description", "success_criteria", "status", "deadline"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return {"error": "no_valid_fields"}
        set_parts = [f"{k} = ${i+2}" for i, k in enumerate(updates)]
        set_parts.append("updated_at = NOW()")
        sql = f"UPDATE goals SET {', '.join(set_parts)} WHERE id = $1::uuid"
        async with pool.acquire() as conn:
            await conn.execute(sql, goal_id, *updates.values())
        return {"goal_id": goal_id, "updated": list(updates.keys())}

    def _normalize_task_status(self, status: str, phase: Optional[str] = None) -> str:
        """하위호환 래퍼 — 정규화 규칙은 goal_binding.normalize_job_state 한 곳에만 있다."""
        return normalize_job_state(status, phase)


goal_state_machine = GoalStateMachine()
