"""오비스 창(/ohvis) 콘솔 — 화면 한 벌을 한 번의 호출로 채운다.

목업 `app/static/reports/20260917_ohvis_console.html` 이 레이아웃 정본이고,
이 모듈은 그 세 칸(좌 대화 / 중앙 라이브 / 우 스케줄·승인·보고)에 들어갈
**실데이터만** 돌려준다. 목업에 박혀 있던 예시 주문·금액은 한 줄도 옮기지 않았다 —
데이터가 없으면 빈 배열을 주고 빈 상태 문구는 화면이 그린다.

왜 엔드포인트가 하나인가. 세 칸이 3초마다 각각 폴링하면 한 화면이 초당
1회씩 커넥션 셋을 잡는다. 화면이 한 벌이면 호출도 한 벌이다.

**같은 뜻의 API 를 새로 만들지 않는다** (지시서 2항):
  - 지시 전송  → `POST /ohvis/console/command`. 기록은 `ohvis_task_manager.create_task()`,
    실행은 저장된 레시피가 있으면 `work_recipe.orchestrator.run_directive()`, 없으면
    `chat_service.trigger_ai_reaction()` 으로 이어진다. 2026-09-18 까지는 화면이
    `POST /ohvis/tasks` 를 직접 불렀는데
    그 경로는 INSERT 만 하고 pending 을 소비하는 워커가 저장소에 없어서, 보낸
    지시가 영원히 pending 으로 남았다(`e41f2c94` 가 그 증거다). 그러니 실행
    트리거가 붙은 이 경로만 쓴다 — 그리고 그 트리거는 원격 프롬프트 주입이
    되므로 관리자 게이트 밖에 두지 않는다.
  - 승인 결정  → `app/services/work_recipe/approval.py` 의 `resolve_approval()`.
    이 계약에는 HTTP 표면이 하나도 없었으므로(2026-09-17 실측: `recipe_approvals`
    를 다루는 라우트가 저장소에 없다) 여기서 **얇은 위임 라우트 하나만** 얹는다.
    승인 규칙(IRREVERSIBLE 재확인 문구, 번복 금지)은 전부 서비스 쪽에 있고
    이 모듈은 그대로 호출만 한다.
  - 자동실행 목록 → `app/services/loop_controller.list_active_loops()`.

테넌트 경계. `recipe_runs` / `recipe_approvals` / `browser_task_live_frames` 는
tenant_id 를 가진다 — `IS NOT DISTINCT FROM` 으로 **정확히** 일치하는 행만 본다
(`store.py` 의 `COALESCE(tenant_id, GLOBAL)` 과 같은 뜻이다). tenant_id 가 NULL 인
전역 실행은 테넌트 사용자에게 보이지 않는다. 87개 테넌트가 같은 DB 를 쓰므로
"NULL 이면 다 보여주기" 는 그대로 교차 노출이다.
`ohvis_tasks` / `ohvis_loops` 는 tenant 컬럼이 없는 AADS 내부 운영 테이블이라
이 라우터 전체를 내부 관리자(is_internal_admin)로 막는다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool
from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
from app.services.loop_controller import list_active_loops
from app.services.ohvis_task_manager import complete_task as complete_ohvis_task
from app.services.ohvis_task_manager import create_task as create_ohvis_task
from app.services.work_recipe.approval import (
    ApprovalError,
    ApprovalRequired,
    ConfirmationRequired,
    get_approval,
    list_pending,
    resolve_approval,
)
from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.guard import requires_confirmation
from app.services.work_recipe.orchestrator import run_directive

router = APIRouter(prefix="/ohvis/console", tags=["ohvis-console"])
logger = structlog.get_logger()

TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)

# 화면의 다섯 칸. 응답 최상위 키는 이것으로 고정한다 — 화면이 기대하는 칸이
# 조용히 사라지면 빈 상태와 장애를 구분할 수 없다.
SECTION_KEYS: tuple[str, ...] = (
    "conversation",
    "live",
    "approvals",
    "schedules",
    "reports",
)

# 진행 중으로 볼 run 상태. 'running' 만 라이브다(recipe_runs_status_valid).
LIVE_RUN_STATUS = "running"
# ohvis_loops.status 중 "가동" 으로 보는 값 (loop_controller.VALID_STATUSES)
LOOP_RUNNING_STATUSES = frozenset({"active"})
LOOP_FAILED_STATUSES = frozenset({"failed"})

_MAX_TEXT = 500

# 지시를 채팅으로 흘려보낼 때 붙는 표식. AI 가 "어디서 온 지시인지" 를 알아야
# 오비스 창으로 결과를 돌려보낸다. 문구를 바꾸면 대화 로그 검색이 끊긴다.
COMMAND_MESSAGE_PREFIX = "[오비스 창 지시]"

# 오비스 전용 세션을 찾을 때 쓰는 표식. title 이 아니라 tags 로 찾는다 —
# 대표님이 대화 제목을 바꿔도(챗 UI 는 제목 수정이 자유롭다) 흔들리지 않는다.
# (AADS-OHVIS-CONSOLE-SESSION-ROUTING-P0) 이전에는 폴백이 "테넌트 안에서 가장
# 최근 갱신된 아무 세션" 을 골랐다 — 화면이 session_id 를 안 보내면 다른 담당의
# 워커 세션(예: "라일론 상세페이지 자동생성 담당")으로 지시가 새었다.
OHVIS_SESSION_TAG = "ohvis_console"
OHVIS_WORKSPACE_NAME = "오비스"
OHVIS_SESSION_TITLE = "오비스 창"


async def require_console_admin(
    context: TenantContext = Depends(require_viewer),
) -> TenantContext:
    """오비스 창은 CEO 운영 화면이다 — 내부 관리자만 연다.

    테넌트 컨텍스트를 그대로 들고 나와서 tenant 스코프 조회에 쓴다.
    """
    if not context.get("user", {}).get("is_internal_admin"):
        raise HTTPException(status_code=403, detail="internal_admin_required")
    return context


class ConsoleCommandIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    # 화면이 보내는 세션은 **참고값**이다. 실제 세션은 서버가 고른다
    # (`_resolve_session_id`) — 남의 테넌트 세션 id 를 실어 보내도 내 테넌트
    # 세션으로 떨어진다.
    session_id: UUID | None = None


class ApprovalDecisionIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20, description="approve | reject")
    confirm_text: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=1000)


class RecipeRunIn(BaseModel):
    directive: str = Field(min_length=1, max_length=2000)
    inputs: dict[str, Any] = Field(default_factory=dict)
    browser_session_id: str | None = Field(default=None, max_length=200)
    browser_work_key: str | None = Field(default=None, max_length=120)


# ───────────────────────────────────────────────────────────── 값 다듬기


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str:
    return str(context["membership"]["user_id"])


def _decided_by(context: TenantContext) -> str:
    user = context.get("user") or {}
    return str(user.get("email") or user.get("id") or "unknown")


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    """화면에 실을 자유 문자열 — 시크릿을 지우고 길이를 자른다."""
    masked = mask_secrets(value)
    return masked[:limit]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        # ohvis_loops 는 timestamp(무 timezone) 다. 화면이 시각을 밀어 쓰지
        # 않도록 서버 기준 UTC 를 붙여 준다.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.isoformat()
    return str(value)


def _json_obj(value: Any) -> dict[str, Any]:
    """asyncpg 는 jsonb 를 str 로 준다(풀에 코덱을 걸지 않았다)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    """ohvis_tasks.steps 는 jsonb 배열이다 — str 로 오면 풀어 준다."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _uuid_or_none(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


# ───────────────────────────────────────────────────────── 섹션별 조회


async def _resolve_session_id(
    conn: Any, tenant_id: str, user_id: str, requested: UUID | None
) -> UUID | None:
    """좌측 대화가 매달릴 채팅 세션 하나를 고른다.

    `ohvis_tasks.session_id` 에는 `chat_sessions(id)` 로 가는 외래키가 걸려 있다
    (2026-09-17 실측: `ohvis_tasks_session_id_fkey`). 화면이 만들어 낸 임의의
    UUID 로 지시를 넣으면 FK 위반으로 500 이 난다 — 그래서 **서버가** 실재하는
    세션을 골라 돌려주고, 화면은 그 값으로만 지시를 보낸다.

    요청된 세션이 이 테넌트 것이 아니면 무시한다. 남의 세션 로그를 세션 id
    하나로 열어 볼 수 있으면 `ohvis_tasks` 에는 테넌트 컬럼이 없으므로
    막을 방법이 없다.

    폴백은 **오비스 전용 세션 고정**이다(AADS-OHVIS-CONSOLE-SESSION-ROUTING-P0).
    예전에는 "테넌트 안에서 가장 최근 갱신된 아무 세션" 을 골랐는데, 다른 담당의
    워커 세션이 CEO 채팅보다 자주 갱신되면 그쪽으로 지시가 샜다 — 실측 사고는
    지시가 "라일론 상세페이지 자동생성 담당" 세션으로 들어간 것이었다. 전용
    세션이 없으면 여기서 만든다.
    """
    if requested is not None:
        owned = await conn.fetchval(
            """
            SELECT id FROM chat_sessions
             WHERE id = $1 AND tenant_id IS NOT DISTINCT FROM $2::uuid
            """,
            requested,
            tenant_id,
        )
        if owned is not None:
            return owned

    dedicated = await conn.fetchval(
        """
        SELECT id FROM chat_sessions
         WHERE tenant_id IS NOT DISTINCT FROM $1::uuid
           AND $2 = ANY(tags)
         ORDER BY updated_at DESC NULLS LAST
         LIMIT 1
        """,
        tenant_id,
        OHVIS_SESSION_TAG,
    )
    if dedicated is not None:
        return dedicated

    return await _create_dedicated_session(conn, tenant_id, user_id)


async def _create_dedicated_session(conn: Any, tenant_id: str, user_id: str) -> UUID | None:
    """오비스 전용 세션이 없으면 하나 만든다.

    워크스페이스도 이름으로 get-or-create 한다 — `external_chat_gateway.py` 의
    `resolve_workspace_id()` 와 같은 패턴이다(새 규약을 만들지 않는다).
    """
    workspace_id = await conn.fetchval(
        "SELECT id FROM chat_workspaces WHERE tenant_id = $1::uuid AND name = $2 LIMIT 1",
        tenant_id,
        OHVIS_WORKSPACE_NAME,
    )
    if workspace_id is None:
        workspace_id = await conn.fetchval(
            """
            INSERT INTO chat_workspaces (tenant_id, name, files, settings, color, icon)
            VALUES ($1::uuid, $2, '[]'::jsonb, '{}'::jsonb, '#6366F1', '🛰️')
            RETURNING id
            """,
            tenant_id,
            OHVIS_WORKSPACE_NAME,
        )
    if workspace_id is None:
        return None

    return await conn.fetchval(
        """
        INSERT INTO chat_sessions (tenant_id, user_id, workspace_id, title, tags)
        VALUES ($1::uuid, $2, $3, $4, ARRAY[$5]::text[])
        RETURNING id
        """,
        tenant_id,
        user_id,
        workspace_id,
        OHVIS_SESSION_TITLE,
        OHVIS_SESSION_TAG,
    )


def _conversation_message(row: Any) -> dict[str, Any]:
    """좌측 대화 한 줄. 지시(CEO)와 오비스의 판단·결과를 한 행으로 묶는다."""
    data = dict(row)
    result = _json_obj(data.get("result"))
    summary = result.get("summary") or result.get("message") or ""
    return {
        "id": str(data.get("id")),
        "session_id": str(data["session_id"]) if data.get("session_id") else None,
        "title": _text(data.get("title")),
        "status": str(data.get("status") or ""),
        "task_type": str(data.get("task_type") or ""),
        "judgement": _text(data.get("ohvis_judgement")),
        "result_summary": _text(summary),
        "step_count": len(_json_list(data.get("steps"))),
        "cost_usd": float(data.get("cost_usd") or 0),
        "created_at": _iso(data.get("created_at")),
        "completed_at": _iso(data.get("completed_at")),
        "reported_at": _iso(data.get("reported_at")),
    }


async def _fetch_conversation(
    conn: Any, session_id: UUID | None, limit: int
) -> dict[str, Any]:
    if session_id is not None:
        rows = await conn.fetch(
            """
            SELECT * FROM ohvis_tasks
             WHERE session_id = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            session_id,
            limit,
        )
    else:
        rows = await conn.fetch(
            "SELECT * FROM ohvis_tasks ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    # 오래된 것이 위로 가야 대화처럼 읽힌다.
    messages = [_conversation_message(row) for row in reversed(rows)]

    # 목업의 예시 버튼 3개(쌈장 주문 등)는 데모다. 그 자리에는 **실제로 내렸던
    # 지시** 중 최근 것을 넣는다. 없으면 빈 배열이고 화면은 버튼 줄을 접는다.
    seen: set[str] = set()
    quick: list[str] = []
    for message in reversed(messages):
        title = message["title"].strip()
        if title and title not in seen:
            seen.add(title)
            quick.append(title)
        if len(quick) >= 3:
            break

    return {
        "session_id": str(session_id) if session_id else None,
        "count": len(messages),
        "messages": messages,
        "quick_commands": quick,
    }


async def _fetch_active_run(conn: Any, tenant_id: str) -> dict[str, Any] | None:
    """진행 중 run 하나. 없으면 마지막으로 끝난 run 을 보여준다.

    빈 타임라인보다 "직전 실행" 이 낫다 — 화면의 녹화 dot 은 is_live 로만 켠다.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM recipe_runs
         WHERE tenant_id IS NOT DISTINCT FROM $1::uuid
         ORDER BY (status = $2) DESC, COALESCE(started_at, created_at) DESC
         LIMIT 1
        """,
        tenant_id,
        LIVE_RUN_STATUS,
    )
    if row is None:
        return None
    data = dict(row)
    # inputs 는 싣지 않는다 — 레시피 입력에는 자격증명이 들어온다.
    return {
        "id": str(data["id"]),
        "recipe_name": _text(data.get("recipe_name"), 200),
        "domain": _text(data.get("domain"), 200),
        "recipe_version": int(data.get("recipe_version") or 1),
        "status": str(data.get("status") or ""),
        "llm_calls": int(data.get("llm_calls") or 0),
        "error": _text(data.get("error")),
        "failed_step_seq": data.get("failed_step_seq"),
        "blocked_step_seq": data.get("blocked_step_seq"),
        "blocked_risk": str(data.get("blocked_risk") or ""),
        "duration_ms": int(data.get("duration_ms") or 0),
        "triggered_by": _text(data.get("triggered_by"), 120),
        "task_id": _text(data.get("task_id"), 120) or None,
        "started_at": _iso(data.get("started_at")),
        "finished_at": _iso(data.get("finished_at")),
    }


async def _fetch_timeline(conn: Any, run_id: str, limit: int) -> list[dict[str, Any]]:
    """실행 단계 = 감사 기록(append-only). seq 오름차순으로 그대로 보여준다."""
    rows = await conn.fetch(
        """
        SELECT * FROM recipe_run_steps
         WHERE run_id = $1::uuid
         ORDER BY seq ASC
         LIMIT $2
        """,
        run_id,
        limit,
    )
    timeline = []
    for row in rows:
        data = dict(row)
        timeline.append({
            "seq": int(data.get("seq") or 0),
            "phase": str(data.get("phase") or ""),
            "action": str(data.get("action") or ""),
            "risk": str(data.get("risk") or ""),
            "status": str(data.get("status") or ""),
            "attempts": int(data.get("attempts") or 0),
            "duration_ms": int(data.get("duration_ms") or 0),
            "llm_calls": int(data.get("llm_calls") or 0),
            "error": _text(data.get("error")),
            "url": _text(data.get("url"), 300),
            # 서버 파일 경로는 브라우저로 내보내지 않는다 — 있고 없고만 알린다.
            "has_screenshot": bool(str(data.get("screenshot_path") or "").strip()),
            "approval_id": str(data["approval_id"]) if data.get("approval_id") else None,
            "created_at": _iso(data.get("created_at")),
        })
    return timeline


async def _fetch_live_frame(conn: Any, tenant_id: str) -> dict[str, Any] | None:
    """중앙 화면의 최신 프레임 한 장 — **메타데이터만**.

    frame_base64 는 최대 2.5MB 다. 3초 폴링에 얹으면 화면 하나가 분당 50MB 를
    끌어온다. 이미지는 화면이 기존 `/browser-tasks/{task_id}/live-frame` 으로
    따로 받는다(같은 tenant 검사를 그쪽이 이미 한다).
    """
    row = await conn.fetchrow(
        """
        SELECT task_id, frame_url, media_type, width, height,
               current_url, page_title, current_step, captured_at, updated_at,
               (frame_base64 <> '') AS has_image
          FROM browser_task_live_frames
         WHERE tenant_id = $1::uuid
         ORDER BY captured_at DESC
         LIMIT 1
        """,
        tenant_id,
    )
    if row is None:
        return None
    data = dict(row)
    return {
        "task_id": str(data["task_id"]),
        "frame_url": _text(data.get("frame_url"), 2000),
        "media_type": str(data.get("media_type") or "image/jpeg"),
        "width": data.get("width"),
        "height": data.get("height"),
        "has_image": bool(data.get("has_image")),
        "current_url": _text(data.get("current_url"), 2000),
        "page_title": _text(data.get("page_title"), 300),
        "current_step": _text(data.get("current_step"), 300),
        "captured_at": _iso(data.get("captured_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


def _approval_card(row: dict[str, Any]) -> dict[str, Any]:
    risk = str(row.get("risk") or "")
    return {
        "id": str(row.get("id")),
        "run_id": str(row.get("run_id")) if row.get("run_id") else None,
        "step_seq": row.get("step_seq"),
        "action": _text(row.get("action"), 200),
        "risk": risk,
        "risk_level": risk,
        "status": str(row.get("status") or ""),
        "summary": _text(row.get("summary")),
        "requested_by": _text(row.get("requested_by"), 120),
        "requested_at": _iso(row.get("requested_at")),
        "expires_at": _iso(row.get("expires_at")),
        # 재확인 문구는 승인자가 **그대로 입력해야** 하므로 화면에 보여야 한다
        # (PRD 4절 / 목업 모달). 시크릿이 아니다.
        "requires_confirmation": requires_confirmation(risk),
        "confirm_text": _text(row.get("confirm_text"), 300),
    }


async def _fetch_approvals(tenant_id: str, limit: int) -> dict[str, Any]:
    """승인 대기 목록 — 기존 서비스 계약(list_pending)을 그대로 쓴다.

    `list_pending` 은 테넌트를 가리지 않으므로(전역 운영 도구용) 여기서
    테넌트 경계를 다시 건다. 같은 SQL 을 한 벌 더 쓰지 않는 쪽을 택했다.
    """
    rows = await list_pending(limit=max(limit, 1) * 4)
    items = [
        _approval_card(row)
        for row in rows
        if str(row.get("tenant_id") or "") == tenant_id
    ][:limit]
    return {"count": len(items), "items": items}


def _loop_card(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "")
    last_result = _json_obj(row.get("last_result"))
    success = last_result.get("success")
    if success is None and last_result:
        success = str(last_result.get("status") or "").lower() in {"ok", "success", "done"}
    return {
        "id": row.get("id"),
        "name": _text(row.get("original_command"), 200),
        "loop_type": str(row.get("loop_type") or ""),
        "project": str(row.get("project") or ""),
        "status": status,
        "enabled": status in LOOP_RUNNING_STATUSES,
        "failed": status in LOOP_FAILED_STATUSES,
        "interval_seconds": row.get("interval_seconds"),
        "current_iteration": int(row.get("current_iteration") or 0),
        "max_iterations": row.get("max_iterations"),
        "consecutive_failures": int(row.get("consecutive_failures") or 0),
        "last_success": bool(success) if success is not None else None,
        "last_run_at": _iso(row.get("completed_at") or row.get("started_at")),
        "next_run_at": _iso(row.get("next_run_at")),
        "created_at": _iso(row.get("created_at")),
    }


async def _fetch_schedules(limit: int) -> dict[str, Any]:
    """우측 자동실행 카드 — loop_controller 의 기존 조회를 그대로 쓴다."""
    try:
        rows = await list_active_loops(project=None, status="all", limit=limit * 2)
    except Exception as exc:  # 루프 테이블이 없는 환경에서도 화면은 떠야 한다
        logger.warning("ohvis_console_loops_failed", error=str(exc))
        return {"count": 0, "items": []}
    cards = [_loop_card(row) for row in rows]
    # 가동 중인 것이 위로. 그 다음은 최근 생성 순(list_active_loops 가 이미 정렬).
    cards.sort(key=lambda card: 0 if card["enabled"] else 1)
    cards = cards[:limit]
    return {"count": len(cards), "items": cards}


async def _fetch_reports(conn: Any, session_id: UUID | None, limit: int) -> dict[str, Any]:
    """오늘 보고 — 오늘(KST) 끝난 ohvis_tasks."""
    clause = "AND session_id = $2" if session_id is not None else ""
    args: list[Any] = [limit]
    if session_id is not None:
        args.append(session_id)
    rows = await conn.fetch(
        f"""
        SELECT * FROM ohvis_tasks
         WHERE completed_at IS NOT NULL
           AND (completed_at AT TIME ZONE 'Asia/Seoul')::date
               = (NOW() AT TIME ZONE 'Asia/Seoul')::date
           {clause}
         ORDER BY completed_at DESC
         LIMIT $1
        """,
        *args,
    )
    items = []
    for row in rows:
        data = dict(row)
        result = _json_obj(data.get("result"))
        items.append({
            "id": str(data.get("id")),
            "title": _text(data.get("title"), 200),
            "status": str(data.get("status") or ""),
            "summary": _text(result.get("summary") or result.get("message") or ""),
            "cost_usd": float(data.get("cost_usd") or 0),
            "completed_at": _iso(data.get("completed_at")),
            "reported_at": _iso(data.get("reported_at")),
        })
    return {"count": len(items), "items": items}


def _kpis(run: dict[str, Any] | None, timeline: list[dict[str, Any]], approval_count: int) -> dict[str, int]:
    """목업 상단 KPI 4개 — LLM / 단계 / 승인 / 차단."""
    step_llm = sum(step["llm_calls"] for step in timeline)
    return {
        "llm_calls": int(run["llm_calls"]) if run else step_llm,
        "steps": len(timeline),
        "approvals": approval_count,
        "blocked": sum(1 for step in timeline if step["status"] == "blocked"),
    }


# ───────────────────────────────────────────────────────────── 엔드포인트


@router.get("/summary")
async def get_console_summary(
    session_id: str | None = Query(default=None, description="채팅 세션 ID(좌측 대화·오늘 보고 범위)"),
    limit: int = Query(default=30, ge=1, le=100),
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    """오비스 창 한 벌. 다섯 칸을 한 번에 채운다."""
    tenant_id = _tenant_id(context)
    requested_session = _uuid_or_none(session_id)

    async with get_pool().acquire() as conn:
        # 지시를 넣을 세션은 실재해야 한다(ohvis_tasks → chat_sessions FK).
        session_uuid = await _resolve_session_id(
            conn, tenant_id, _user_id(context), requested_session
        )
        conversation = await _fetch_conversation(conn, session_uuid, limit)
        run = await _fetch_active_run(conn, tenant_id)
        timeline = await _fetch_timeline(conn, run["id"], limit * 4) if run else []
        frame = await _fetch_live_frame(conn, tenant_id)
        reports = await _fetch_reports(conn, session_uuid, limit)

    approvals = await _fetch_approvals(tenant_id, limit)
    schedules = await _fetch_schedules(limit)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": str(session_uuid) if session_uuid else None,
        "conversation": conversation,
        "live": {
            "is_live": bool(run and run["status"] == LIVE_RUN_STATUS),
            "run": run,
            "frame": frame,
            "timeline": timeline,
            "kpis": _kpis(run, timeline, approvals["count"]),
        },
        "approvals": approvals,
        "schedules": schedules,
        "reports": reports,
    }
    return payload


async def _dispatch_ai_reaction(session_id: str, title: str, task_id: str) -> None:
    """지시를 채팅 세션에 넣고 AI 가 도구로 조치하게 만든다.

    `trigger_ai_reaction()` 은 메시지를 넣고 소비 태스크를 띄운 뒤 바로 돌아온다
    (`_consume_stream` 은 별도 태스크다). 그래서 await 해도 요청이 AI 응답을
    기다리지 않는다 — 대신 **띄우는 데 실패한 것**은 여기서 바로 알 수 있다.
    이걸 `create_task()` 로 던져 버리면 실패가 로그에만 남고 행은 running 으로
    굳는다. Pipeline Runner 가 같은 인자로 부르는 그 함수다.

    `ohvis_task_id` 를 넘기면 반응이 끝날 때 `complete_task()` 가 판단·결과를
    그 행에 적는다 — 오비스 창이 결과를 보는 유일한 경로다.

    chat_service 는 여기서 늦게 가져온다. 모듈 최상단에서 가져오면 라우터
    임포트가 채팅 스택 전체를 끌고 온다.
    """
    from app.services.chat_service import trigger_ai_reaction

    await trigger_ai_reaction(
        session_id=session_id,
        system_message=f"{COMMAND_MESSAGE_PREFIX} {title}",
        ohvis_task_id=task_id,
    )


@router.post("/command", status_code=202)
async def run_console_command(
    body: ConsoleCommandIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    """오비스 창에서 내린 지시 한 건 — 기록하고 **실행까지 건다**.

    기록만 하던 것이 이 결함의 전부였다. 그래서 여기서는 두 가지를 한 흐름에
    묶는다: `ohvis_tasks` 행(running) 과 채팅 세션 트리거. 행만 남고 트리거가
    실패하면 그 행을 error 로 닫고 502 를 돌려준다 — pending/running 으로
    방치하면 화면은 "돌고 있다" 고 거짓말한다.

    게이트는 라우터 전체의 `require_console_admin` 이다. 이 엔드포인트는 임의
    문자열을 CEO 채팅 세션에 밀어 넣으므로, 인증이 없으면 그대로 원격 프롬프트
    인젝션이 된다.
    """
    tenant_id = _tenant_id(context)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="empty_title")

    async with get_pool().acquire() as conn:
        session_uuid = await _resolve_session_id(
            conn, tenant_id, _user_id(context), body.session_id
        )
    if session_uuid is None:
        # 전용 세션이 없으면 _resolve_session_id 가 만든다 — 그 생성마저
        # 실패한 경우에만 여기 온다. 세션이 없으면 FK 때문에 행도 못 만든다.
        # 500 으로 터뜨리지 않고 화면이 읽을 수 있는 사유를 준다.
        raise HTTPException(status_code=409, detail="no_chat_session")

    task_id = await create_ohvis_task(
        session_id=str(session_uuid),
        title=title,
        task_type="ceo_directive",
    )
    if not task_id:
        # create_task 는 예외를 삼키고 None 을 준다 — 그 None 이 여기서 끝이다.
        raise HTTPException(status_code=502, detail="ohvis_task_create_failed")

    command_payload = {"directive": title}
    action_intent = ChannelRouter().route_directive(
        directive_from_authenticated_context(
            context,
            session_id=str(session_uuid),
            correlation_id=task_id,
            payload=command_payload,
            capabilities=frozenset({"console.command", "recipe.execute"}),
        ),
        capability="console.command",
    )

    # 저장된 WorkRecipe와 정확히 일치하는 지시는 채팅 LLM을 거치지 않고 바로
    # 재생한다. task_id를 recorder에 넘겨 recipe_runs와 ohvis_tasks를 같은 실행
    # 증거로 묶고, 결과도 이 행에 닫아 오비스 화면이 즉시 읽게 한다.
    try:
        recipe_result = await run_directive(
            title,
            tenant_id,
            triggered_by=_decided_by(context),
            task_id=task_id,
            action_intent=action_intent,
        )
    except ApprovalRequired as exc:
        approval_result = {
            "status": "approval_required",
            "approval_id": exc.approval_id,
            "run_id": str(exc.run_id) if exc.run_id else None,
            "risk": exc.risk_level,
        }
        await complete_ohvis_task(
            task_id,
            status="done",
            result=approval_result,
            ohvis_judgement="승인 대기",
        )
        return {
            "task_id": task_id,
            "session_id": str(session_uuid),
            **approval_result,
            "artifact": {
                "kind": "approval_wait",
                "narration": "승인이 필요한 단계에서 안전하게 대기 중입니다.",
                "evidence": {"approval_id": exc.approval_id, "run_id": approval_result["run_id"]},
            },
        }
    except ValueError as exc:
        reason = _text(str(exc), 300)
        await complete_ohvis_task(
            task_id,
            status="error",
            result={"error": reason, "summary": f"레시피를 실행하지 못했습니다: {reason}"},
            ohvis_judgement="레시피 실행 실패",
        )
        raise HTTPException(status_code=422, detail=reason) from exc
    except Exception as exc:
        reason = _text(str(exc), 300)
        logger.warning(
            "ohvis_console_recipe_dispatch_failed",
            task_id=task_id,
            error=reason,
        )
        await complete_ohvis_task(
            task_id,
            status="error",
            result={"error": reason, "summary": f"레시피 실행 경로가 실패했습니다: {reason}"},
            ohvis_judgement="레시피 실행 경로 실패",
        )
        raise HTTPException(
            status_code=502, detail=f"recipe_dispatch_failed: {reason}"
        ) from exc

    if recipe_result is not None:
        result_payload = recipe_result.to_dict()
        recipe_status = str(result_payload.get("status") or "")
        task_status = "done" if recipe_status == "success" else "error"
        await complete_ohvis_task(
            task_id,
            status=task_status,
            result=result_payload,
            ohvis_judgement=(
                "저장 레시피 재생 완료" if task_status == "done" else "저장 레시피 재생 실패"
            ),
        )
        return {
            "task_id": task_id,
            "session_id": str(session_uuid),
            "status": task_status,
            "execution": "work_recipe",
            "result": result_payload,
        }

    try:
        await _dispatch_ai_reaction(str(session_uuid), title, task_id)
    except Exception as exc:
        reason = _text(str(exc), 300)
        logger.warning(
            "ohvis_console_command_dispatch_failed",
            task_id=task_id,
            session_id=str(session_uuid),
            error=reason,
        )
        await complete_ohvis_task(
            task_id,
            status="error",
            result={"error": reason, "summary": f"지시를 실행하지 못했습니다: {reason}"},
            ohvis_judgement="실행 트리거 실패",
        )
        raise HTTPException(
            status_code=502, detail=f"ai_reaction_dispatch_failed: {reason}"
        ) from exc

    logger.info(
        "ohvis_console_command_dispatched",
        task_id=task_id,
        session_id=str(session_uuid),
    )
    return {"task_id": task_id, "session_id": str(session_uuid), "status": "running"}


@router.post("/approvals/{approval_id}/decision")
async def decide_console_approval(
    approval_id: str,
    body: ApprovalDecisionIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    """승인/거부 — 판단은 전부 `work_recipe.approval` 이 한다.

    여기서 하는 일은 셋뿐이다: 테넌트 확인, 위임, 예외를 HTTP 로 번역.
    재확인 문구 검사·번복 금지 같은 규칙을 이쪽에 복제하면 둘이 갈라진다.
    """
    tenant_id = _tenant_id(context)
    row = await get_approval(approval_id)
    if row is None or str(row.get("tenant_id") or "") != tenant_id:
        raise HTTPException(status_code=404, detail="approval_not_found")

    try:
        updated = await resolve_approval(
            approval_id,
            body.decision,
            _decided_by(context),
            body.reason,
            confirm_text=body.confirm_text,
        )
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"status": "decided", "approval": _approval_card(updated)}


@router.post("/recipes/run")
async def run_console_recipe(
    body: RecipeRunIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    """저장된 레시피와 정확히 연결되는 지시만 실제 브라우저에서 실행한다."""
    try:
        recipe_payload = {"directive": body.directive, "inputs": body.inputs}
        action_intent = ChannelRouter().route_directive(
            directive_from_authenticated_context(
                context,
                session_id=body.browser_session_id or "ohvis-console",
                correlation_id=str(uuid4()),
                payload=recipe_payload,
                capabilities=frozenset({"recipe.execute"}),
            ),
            capability="recipe.execute",
        )
        result = await run_directive(
            body.directive,
            _tenant_id(context),
            inputs=body.inputs,
            browser_session_id=body.browser_session_id,
            browser_work_key=body.browser_work_key,
            triggered_by=_decided_by(context),
            action_intent=action_intent,
        )
    except ApprovalRequired as exc:
        return {
            "status": "approval_required",
            "approval_id": exc.approval_id,
            "run_id": str(exc.run_id) if exc.run_id else None,
            "risk": exc.risk_level,
            "artifact": {
                "kind": "approval_wait",
                "narration": "승인이 필요한 단계에서 안전하게 대기 중입니다.",
                "evidence": {"approval_id": exc.approval_id, "run_id": str(exc.run_id) if exc.run_id else None},
            },
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_text(exc, 300)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="matching_recipe_not_found")
    return result.to_dict()
