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


class AddOwnerRequest(BaseModel):
    session_id: str
    role_key: Optional[str] = None
    as_lead: bool = False


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
                   ms.dispatch_note, ms.id::text AS milestone_id, ms.variant,
                   ms.status AS milestone_status,
                   op.reason AS paused_reason,
                   (SELECT count(*) FROM milestone_notes n
                     WHERE n.milestone_id = ms.id AND n.answered_at IS NULL) AS open_notes
            FROM bound b
            LEFT JOIN last_msg lm ON lm.session_id = b.id
            LEFT JOIN owner_pause op
                   ON op.goal_id = $1::uuid AND op.session_id = b.id
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
        if r["paused_reason"] is not None:
            state = "멈춤"
        elif r["milestone_status"] == "review":
            state = "확인 대기"
        elif r["dispatch_note"]:
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
            "milestone_id": r["milestone_id"],
            "variant": r["variant"],
            "paused": r["paused_reason"] is not None,
            "paused_reason": r["paused_reason"],
            "open_notes": r["open_notes"] or 0,
            "is_lead": (r["role_key"] or "").endswith("Lead"),
        })

    from app.services.direction_guard import is_halted

    docs = await goal_documents(goal_id)
    return {
        "goal": dict(goal),
        "halted": await is_halted(),
        "owners": owners,
        "has_lead": any(o["is_lead"] for o in owners),
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


@router.get("/goals/{goal_id}/candidates")
async def goal_owner_candidates(goal_id: str):
    """붙일 수 있는 세션 — **같은 워크스페이스**의 아직 안 묶인 것만.

    프로젝트를 넘어 붙이면 맥락이 섞인다. `ask_session` 이 워크스페이스
    안으로 제한한 것과 같은 이유다.
    """
    from app.core.db_pool import get_pool

    rows = await get_pool().fetch(
        """
        SELECT s.id::text AS session_id, s.title,
               COALESCE(s.role_key, '') AS role_key,
               s.message_count,
               EXISTS (
                   SELECT 1 FROM prompt_assets a
                   WHERE a.enabled AND s.role_key IS NOT NULL
                     AND a.role_scope @> ARRAY[s.role_key]::text[]
               ) AS has_prompt
        FROM chat_sessions s
        WHERE s.workspace_id = (
                SELECT s2.workspace_id FROM goal_task_links l2
                JOIN chat_sessions s2 ON s2.id = l2.task_id::uuid
                WHERE l2.goal_id = $1::uuid AND l2.task_type = 'chat_session'
                LIMIT 1
              )
          AND NOT EXISTS (
                SELECT 1 FROM goal_task_links l
                WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
                  AND l.task_id = s.id::text
                  AND COALESCE(l.link_state,'active') = 'active'
              )
        ORDER BY s.updated_at DESC
        LIMIT 40
        """,
        goal_id,
    )
    return {"candidates": [dict(r) for r in rows]}


@router.post("/goals/{goal_id}/owners")
async def add_goal_owner(goal_id: str, req: AddOwnerRequest):
    """이미 있는 세션을 담당으로 붙인다. **세션을 만들지는 않는다.**

    채팅창이 늘어나는 것을 대표님이 모르시는 상태가 되면 안 된다.

    역할 프롬프트를 만들지도 않는다 — 그 담당이 무엇을 하는 사람인지는
    사람이 쓴다. 다만 없으면 **응답에 담아 알린다.** 프롬프트가 없으면
    그 담당은 자기가 누구인지 모르는 채로 시작한다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            "SELECT id::text, title, COALESCE(role_key,'') AS role_key "
            "FROM chat_sessions WHERE id = $1::uuid",
            req.session_id,
        )
        if not sess:
            raise HTTPException(status_code=404, detail="session_not_found")

        role = (req.role_key or sess["role_key"] or "").strip()
        if role and role != sess["role_key"]:
            await conn.execute(
                "UPDATE chat_sessions SET role_key = $2, updated_at = NOW() WHERE id = $1::uuid",
                req.session_id, role,
            )

        await conn.execute(
            "INSERT INTO goal_task_links (goal_id, task_type, task_id, status, "
            "       bind_source, bound_by, link_state) "
            "VALUES ($1::uuid, 'chat_session', $2, 'active', 'manual', 'ceo', 'active') "
            "ON CONFLICT DO NOTHING",
            goal_id, req.session_id,
        )

        if req.as_lead:
            await conn.execute(
                "UPDATE goals SET owner_role_key = NULLIF($2,''), "
                "owner_session_id = $3::uuid, updated_at = NOW() WHERE id = $1::uuid",
                goal_id, role, req.session_id,
            )

        has_prompt = bool(role) and bool(await conn.fetchval(
            "SELECT 1 FROM prompt_assets WHERE enabled "
            "  AND role_scope @> ARRAY[$1]::text[] LIMIT 1",
            role,
        ))

    return {
        "added": True, "session": sess["title"], "role_key": role,
        "as_lead": req.as_lead, "has_prompt": has_prompt,
        "warning": (
            None if has_prompt else
            f"'{role or '(역할 없음)'}' 역할 프롬프트가 없습니다. 이 담당은 "
            "자기가 무엇을 하는 사람인지 모르는 채로 시작합니다."
        ),
    }


@router.delete("/goals/{goal_id}/owners/{session_id}")
async def remove_goal_owner(goal_id: str, session_id: str):
    """목표에서 뗀다. **세션은 지우지 않는다** — 링크만 끊는다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        holding = await conn.fetchval(
            "SELECT title FROM milestones WHERE goal_id = $1::uuid "
            "  AND status IN ('in_progress','review') "
            "  AND (owner_session_id = $2::uuid "
            "       OR owner_role_key = (SELECT role_key FROM chat_sessions WHERE id = $2::uuid)) "
            "LIMIT 1",
            goal_id, session_id,
        )
        await conn.execute(
            "UPDATE goal_task_links SET link_state = 'detached', "
            "       detach_reason = 'ceo_removed', updated_at = NOW() "
            "WHERE goal_id = $1::uuid AND task_type = 'chat_session' AND task_id = $2",
            goal_id, session_id,
        )
        await conn.execute(
            "DELETE FROM owner_pause WHERE goal_id = $1::uuid AND session_id = $2::uuid",
            goal_id, session_id,
        )
    return {
        "removed": True,
        "was_holding": holding,
        "warning": (
            f"이 담당이 맡고 있던 '{holding}' 은 담당이 없어져 지시가 "
            "나가지 않습니다." if holding else None
        ),
    }


@router.post("/goals/{goal_id}/owners/{session_id}/pause")
async def pause_owner(goal_id: str, session_id: str, req: InterveneRequest):
    """이 담당에게 **새 지시를 보내지 않는다.** 진행 중 응답은 끝까지 둔다.

    끊으면 지금까지 한 것이 사라진다. 2026-09-14 배포로 두 번 끊어서
    운영인프라담당의 조사가 두 번 날아갔다. 지금 끊어야 하면
    `/chat/sessions/{id}/stop` 을 따로 부른다.
    """
    from app.core.db_pool import get_pool

    reason = (req.reason or "").strip()
    if not reason:
        # 이유 없이 멈춘 카드는 사흘 뒤에 왜 멈췄는지 아무도 모른다.
        raise HTTPException(status_code=400, detail="reason required")
    await get_pool().execute(
        "INSERT INTO owner_pause (goal_id, session_id, reason) "
        "VALUES ($1::uuid, $2::uuid, $3) "
        "ON CONFLICT (goal_id, session_id) DO UPDATE "
        "   SET reason = EXCLUDED.reason, paused_at = NOW()",
        goal_id, session_id, reason,
    )
    return {"paused": True, "reason": reason}


@router.delete("/goals/{goal_id}/owners/{session_id}/pause")
async def resume_owner(goal_id: str, session_id: str):
    """멈춤을 푼다. 밀린 지시가 있으면 다음 주기에 나간다."""
    from app.core.db_pool import get_pool

    await get_pool().execute(
        "DELETE FROM owner_pause WHERE goal_id = $1::uuid AND session_id = $2::uuid",
        goal_id, session_id,
    )
    return {"paused": False}


@router.post("/goals/{goal_id}/owners/{session_id}/restart")
async def restart_owner(goal_id: str, session_id: str):
    """발송 기록을 지우고 처음부터 다시 지시한다.

    멈춰 있었다면 같이 푼다 — 재시작을 눌렀는데 멈춘 채로 있으면
    아무 일도 안 일어난다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            UPDATE milestones SET dispatched_at = NULL, dispatch_count = 0,
                   dispatch_note = NULL, updated_at = NOW()
            WHERE goal_id = $1::uuid AND status = 'in_progress'
              AND (owner_session_id = $2::uuid
                   OR owner_role_key = (SELECT role_key FROM chat_sessions WHERE id = $2::uuid))
            RETURNING 1
            """,
            goal_id, session_id,
        )
        await conn.execute(
            "DELETE FROM owner_pause WHERE goal_id = $1::uuid AND session_id = $2::uuid",
            goal_id, session_id,
        )
    return {"restarted": bool(n), "resumed": True}


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
