"""Goals API — 목표 Control Loop 엔드포인트."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()


class MilestoneCreateRequest(BaseModel):
    title: str
    sequence: int
    completion_criteria: Optional[str] = None
    auto_advance: bool = True


class GoalCreateRequest(BaseModel):
    project: str
    title: str
    priority: str = "P2"
    success_criteria: Optional[str] = None
    parent_goal_id: Optional[str] = None
    milestones: Optional[list[MilestoneCreateRequest]] = None
    activate: bool = False


class LinkTaskRequest(BaseModel):
    milestone_id: Optional[str] = None
    task_type: str
    task_id: str


class GoalUpdateRequest(BaseModel):
    title: Optional[str] = None
    priority: Optional[str] = None
    description: Optional[str] = None
    success_criteria: Optional[str] = None
    status: Optional[str] = None
    deadline: Optional[str] = None


class TaskStatusRequest(BaseModel):
    task_type: str
    task_id: str
    status: str
    # phase 는 선택 — terminated/review_failed/blocked_dependency 같은 별칭을
    # status 와 함께 넘기면 정규화가 실패 상태를 놓치지 않는다 (하위호환).
    phase: Optional[str] = None


@router.get("/goals")
async def list_goals(project: Optional[str] = Query(None)):
    from app.services.goal_manager import goal_state_machine
    return await goal_state_machine.list_goals(project)


@router.post("/goals")
async def create_goal(req: GoalCreateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.create_goal(
        project=req.project,
        title=req.title,
        priority=req.priority,
        success_criteria=req.success_criteria,
        parent_goal_id=req.parent_goal_id,
    )
    if req.milestones:
        for ms in req.milestones:
            await goal_state_machine.add_milestone(
                goal_id=result["goal_id"],
                title=ms.title,
                sequence=ms.sequence,
                completion_criteria=ms.completion_criteria,
                auto_advance=ms.auto_advance,
            )
    if req.activate:
        result = await goal_state_machine.activate_goal(result["goal_id"])
    return result


class GoalDocRequest(BaseModel):
    kind: str = "reference"          # plan | prd | report | reference
    doc_path: str
    title: Optional[str] = None
    note: Optional[str] = None


class InterveneRequest(BaseModel):
    message: Optional[str] = None
    roles: Optional[list[str]] = None
    reason: Optional[str] = None
    on: Optional[bool] = None


@router.get("/goals/{goal_id}/board")
async def goal_board(goal_id: str):
    """담당별 현재 상태. 창 8개를 열지 않아도 되게.

    **활동 판정을 DB 로만 한다.** `is_streaming()` 은 프로세스 메모리라
    블루/그린에서 다른 슬롯이 물으면 못 읽는다. 마지막 메시지와 그 모양
    (진행중 표시인지)으로 판단하면 어느 슬롯에서든 같은 답이 나온다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        goal = await conn.fetchrow(
            "SELECT id::text, project, title, status, progress FROM goals WHERE id = $1::uuid",
            goal_id,
        )
        if not goal:
            raise HTTPException(status_code=404, detail="goal_not_found")

        rows = await conn.fetch(
            """
            WITH bound AS (
                SELECT s.id, COALESCE(s.role_key, '') AS role_key, s.title
                FROM goal_task_links l
                JOIN chat_sessions s ON s.id = l.task_id::uuid
                WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
                  AND COALESCE(l.link_state, 'active') = 'active'
            ),
            last_msg AS (
                SELECT DISTINCT ON (m.session_id)
                       m.session_id, m.role, m.content, m.created_at,
                       COALESCE(jsonb_array_length(m.tools_called), 0) AS tool_calls
                FROM chat_messages m
                JOIN bound b ON b.id = m.session_id
                WHERE m.deleted_at IS NULL
                ORDER BY m.session_id, m.created_at DESC
            )
            SELECT b.id::text AS session_id, b.role_key, b.title,
                   lm.role AS last_role, lm.created_at AS last_at,
                   lm.tool_calls,
                   (lm.content LIKE '⏳%') AS working,
                   (lm.content LIKE '⚠️ _응답 생성이%') AS interrupted,
                   ms.title AS milestone, ms.dispatched_at, ms.dispatch_count,
                   ms.dispatch_note
            FROM bound b
            LEFT JOIN last_msg lm ON lm.session_id = b.id
            LEFT JOIN milestones ms
                   ON ms.goal_id = $1::uuid AND ms.status = 'in_progress'
                  AND (ms.owner_session_id = b.id
                       OR (ms.owner_session_id IS NULL AND ms.owner_role_key = b.role_key))
            ORDER BY (b.role_key LIKE '%Lead') DESC, b.role_key
            """,
            goal_id,
        )

    owners = []
    for r in rows:
        if r["dispatch_note"]:
            state = "막힘"
        elif r["interrupted"]:
            state = "응답 끊김"
        elif r["working"]:
            state = "작업 중"
        elif r["dispatched_at"] and r["last_role"] == "user":
            state = "응답 대기"
        elif r["milestone"]:
            state = "진행 중"
        else:
            state = "지시 없음"
        owners.append({
            "session_id": r["session_id"],
            "role_key": r["role_key"],
            "title": r["title"],
            "state": state,
            "milestone": r["milestone"],
            "last_at": r["last_at"].isoformat() if r["last_at"] else None,
            "tool_calls": r["tool_calls"] or 0,
            "dispatch_count": r["dispatch_count"] or 0,
            "note": r["dispatch_note"],
        })

    from app.services.direction_guard import is_halted

    docs = await goal_documents(goal_id)
    return {
        "goal": dict(goal),
        "halted": await is_halted(),
        "owners": owners,
        "documents": docs["documents"],
        "has_design": docs["has_design"],
        "missing_design": docs["missing"],
    }


@router.get("/goals/{goal_id}/documents")
async def goal_documents(goal_id: str):
    """목표의 설계 문서. 없으면 없다고 답한다 — 빈 것과 안 쓴 것은 다르다."""
    from app.core.db_pool import get_pool

    rows = await get_pool().fetch(
        "SELECT kind, doc_path, title, note, created_at FROM goal_documents "
        "WHERE goal_id = $1::uuid "
        "ORDER BY CASE kind WHEN 'plan' THEN 0 WHEN 'prd' THEN 1 "
        "                   WHEN 'report' THEN 2 ELSE 3 END, created_at",
        goal_id,
    )
    docs = [dict(r) for r in rows]
    kinds = {d["kind"] for d in docs}
    return {
        "documents": docs,
        # 기획서와 PRD 가 둘 다 있어야 "설계가 있다" 고 본다. 하나만 있으면
        # 왜(기획서)나 어떻게(PRD) 중 한쪽이 비어 있다는 뜻이다.
        "has_design": "plan" in kinds and "prd" in kinds,
        "missing": [k for k in ("plan", "prd") if k not in kinds],
    }


@router.post("/goals/{goal_id}/documents")
async def add_goal_document(goal_id: str, req: GoalDocRequest):
    """문서를 목표에 잇는다. `doc_path` 는 doc_chunks 와 같은 규격이다."""
    from app.core.db_pool import get_pool

    path = (req.doc_path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="doc_path required")
    if req.kind not in ("plan", "prd", "report", "reference"):
        raise HTTPException(status_code=400, detail="kind must be plan|prd|report|reference")

    row = await get_pool().fetchrow(
        """
        INSERT INTO goal_documents (goal_id, kind, doc_path, title, note, created_by)
        VALUES ($1::uuid, $2, $3, $4, $5, 'ceo')
        ON CONFLICT (goal_id, doc_path) DO UPDATE
           SET kind = EXCLUDED.kind, title = EXCLUDED.title, note = EXCLUDED.note
        RETURNING kind, doc_path, title
        """,
        goal_id, req.kind, path, req.title, req.note,
    )
    return dict(row)


@router.get("/goals/for-session/{session_id}")
async def goals_for_session(session_id: str):
    """이 세션이 참여 중인 목표와 맡은 마일스톤.

    담당이 자기 창에서 "내가 무슨 목표에 묶여 있는지" 를 봐야 한다.
    지금은 그걸 알 길이 없어서, 지시를 받아도 무엇의 일부인지 모른다.
    """
    from app.core.db_pool import get_pool

    rows = await get_pool().fetch(
        """
        SELECT DISTINCT ON (g.id)
               g.id::text AS goal_id, g.title, g.status, g.project,
               COALESCE(g.progress, 0) AS progress,
               s.role_key,
               ms.id::text AS milestone_id, ms.title AS milestone,
               ms.dispatch_note
        FROM goal_task_links l
        JOIN goals g ON g.id = l.goal_id
        JOIN chat_sessions s ON s.id = l.task_id::uuid
        LEFT JOIN milestones ms
               ON ms.goal_id = g.id AND ms.status = 'in_progress'
              AND (ms.owner_session_id = s.id
                   OR (ms.owner_session_id IS NULL AND ms.owner_role_key = s.role_key))
        WHERE l.task_type = 'chat_session'
          AND l.task_id = $1
          AND COALESCE(l.link_state, 'active') = 'active'
          AND g.status IN ('draft', 'active', 'blocked')
        ORDER BY g.id, ms.sequence_order NULLS LAST
        """,
        session_id,
    )

    from app.services.direction_guard import is_halted

    return {"halted": await is_halted(), "goals": [dict(r) for r in rows]}


@router.post("/goals/halt")
async def goal_halt(req: InterveneRequest):
    """전체 정지를 걸거나 푼다. 실제로 막는 것은 direction_guard 다."""
    from app.services.goal_intervene import halt

    if req.on is None:
        raise HTTPException(status_code=400, detail="on required")
    return await halt(bool(req.on), req.reason or "")


@router.post("/goals/{goal_id}/direct")
async def goal_direct(goal_id: str, req: InterveneRequest):
    """대표님 지시를 담당 전원에게 동시에. 주도를 거치지 않는다."""
    from app.services.goal_intervene import direct

    if not (req.message or "").strip():
        raise HTTPException(status_code=400, detail="message required")
    return await direct(goal_id, req.message, req.roles)


@router.post("/goals/milestones/{milestone_id}/rewind")
async def goal_rewind(milestone_id: str, req: InterveneRequest):
    """마일스톤을 되돌리고 발송 기록을 지운다 — 그래야 다시 지시가 나간다."""
    from app.services.goal_intervene import rewind

    result = await rewind(milestone_id, req.reason or "")
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/goals/{goal_id}/status")
async def goal_status(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.get_goal_status(goal_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/goals/{goal_id}/activate")
async def activate_goal(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.activate_goal(goal_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/goals/{goal_id}/milestones")
async def add_milestone(goal_id: str, req: MilestoneCreateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.add_milestone(
        goal_id=goal_id,
        title=req.title,
        sequence=req.sequence,
        completion_criteria=req.completion_criteria,
        auto_advance=req.auto_advance,
    )
    return result


@router.post("/goals/{goal_id}/link-task")
async def link_task(goal_id: str, req: LinkTaskRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.link_task(
        goal_id=goal_id,
        milestone_id=req.milestone_id,
        task_type=req.task_type,
        task_id=req.task_id,
    )
    return result


@router.post("/goals/{goal_id}/check-completion")
async def check_completion(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.check_goal_completion(goal_id)
    return result


@router.post("/goals/{goal_id}/advance")
async def advance_goal(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.advance_goal(goal_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/goals/advance")
async def advance_active_goals(project: Optional[str] = Query(None)):
    from app.services.goal_manager import goal_state_machine
    return await goal_state_machine.advance_active_goals(project)


@router.post("/goals/task-status")
async def update_task_status(req: TaskStatusRequest):
    from app.services.goal_manager import goal_state_machine
    return await goal_state_machine.update_task_status_with_phase(
        task_type=req.task_type,
        task_id=req.task_id,
        status=req.status,
        phase=req.phase,
    )


@router.post("/goals/reconcile")
async def reconcile_goal_links(
    project: Optional[str] = Query(None, description="프로젝트 코드 (미지정 시 전체)"),
    dry_run: bool = Query(True, description="true(기본)면 계획만 반환하고 DB 를 쓰지 않는다"),
    limit: int = Query(200, ge=1, le=2000, description="한 번에 수정할 최대 링크 수"),
    detach_legacy: bool = Query(
        False,
        description="프로젝트만 보고 붙은 레거시 링크까지 회수 — 운영자가 명시할 때만",
    ),
):
    """goal_task_links 를 pipeline_jobs 기준으로 재조정한다.

    stale / misbound / orphan / legacy_unverified 건수를 보고하고,
    dry_run=false 일 때만 limit 범위 안에서 복구한다. 행을 삭제하지 않으며,
    같은 입력으로 다시 돌리면 repaired=0 이다(멱등).
    """
    from app.services.goal_link_reconciler import reconcile

    return await reconcile(
        project,
        dry_run=dry_run,
        limit=limit,
        detach_legacy=detach_legacy,
    )


@router.post("/goals/release-evidence")
async def reconcile_release_evidence(
    project: Optional[str] = Query(None, description="프로젝트 코드 (미지정 시 전체)"),
    dry_run: bool = Query(True, description="true(기본)면 계획만 반환하고 DB 를 쓰지 않는다"),
    limit: int = Query(200, ge=1, le=2000, description="한 번에 검사할 최대 링크 수"),
):
    """인증된 배포에 포함된 작업 커밋으로 goal_task_links 를 완료 승격한다.

    증거는 deploy_release_provenance(배포 시점에 Git 으로 해석한 40자 full SHA)와
    deploy_runs 인증 조건(success/completed + image_digest=standby_digest)뿐이다.
    런타임은 Git 을 쓰지 않는다 — 컨테이너에 /app/.git 이 없기 때문이다.
    마일스톤/목표 완료는 GoalStateMachine 이 판정하며, 같은 입력으로 다시 돌리면
    completed=0 이다(멱등).
    """
    from app.services.release_evidence import reconcile_release_links

    return await reconcile_release_links(project, dry_run=dry_run, limit=limit)


@router.put("/goals/{goal_id}")
async def update_goal(goal_id: str, req: GoalUpdateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.update_goal(
        goal_id, **req.model_dump(exclude_none=True),
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
