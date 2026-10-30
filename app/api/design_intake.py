"""Tenant-scoped HTTP adapter for the existing design intake gates."""
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool
from app.services.design_agent import intake

router = APIRouter(prefix="/design/intake", tags=["design-intake"])
VIEW = Depends(require_tenant_role(TenantRole.VIEWER))
WRITE = Depends(require_tenant_role(TenantRole.MEMBER))
SOURCES = {"user": "ceo_choice", "user_other": "ceo_input", "unknown": "recommended_accepted"}
PUBLIC_SOURCES = {value: key for key, value in SOURCES.items()}
PUBLIC_SOURCES["estimate_confirmed"] = "user_other"


class AnswerRequest(BaseModel):
    question_id: UUID
    answer: str
    answer_source: str


class ConfirmRequest(BaseModel):
    brief: dict[str, Any]


def _tenant(context):
    return UUID(str(context["tenant"]["id"]))


def _error(exc):
    message = str(exc)
    status = 404 if message in {"intake session not found", "intake question not found"} else 400
    if message == "all required questions need confirmed answers":
        status = 409
    return HTTPException(status, message)


async def _session(conn, tenant, session_id):
    row = await conn.fetchrow('''SELECT * FROM design_intake_sessions
        WHERE id=$1 AND tenant_id=$2 FOR UPDATE''', session_id, tenant)
    if row is None:
        raise HTTPException(404, "intake session not found")
    return row


def _question(row):
    result = dict(row)
    result["options"] = json.loads(row["options"]) if isinstance(row["options"], str) else row["options"]
    result["answer_source"] = PUBLIC_SOURCES.get(row["answer_source"])
    return result


async def _intake_response(conn, tenant, session):
    if session is None:
        return {"session_id": None, "questions": [], "round": None, "brief": {},
                "brief_confirmed": False, "brief_confirmed_at": None}
    session_id = session["id"]
    rows = await conn.fetch('''SELECT id, area, question, options, recommended,
        answer, answer_source, confirmed_at FROM design_intake_questions
        WHERE session_id=$1 AND tenant_id=$2 ORDER BY id''', session_id, tenant)
    brief = session["brief"]
    return {"session_id": session_id, "questions": [_question(row) for row in rows],
            "round": session["round"],
            "brief": json.loads(brief) if isinstance(brief, str) else brief,
            "brief_confirmed": session["brief_confirmed_at"] is not None,
            "brief_confirmed_at": session["brief_confirmed_at"]}


@router.get("/by-chat/{chat_session_id}")
async def get_intake_by_chat(chat_session_id: UUID, context: dict = VIEW):
    tenant = _tenant(context)
    async with get_pool().acquire() as conn, conn.transaction():
        session = await conn.fetchrow('''SELECT * FROM design_intake_sessions
            WHERE chat_session_id=$1 AND tenant_id=$2
            ORDER BY created_at DESC, id DESC LIMIT 1''', chat_session_id, tenant)
        return await _intake_response(conn, tenant, session)


@router.get("/{session_id}")
async def get_intake(session_id: UUID, context: dict = VIEW):
    tenant = _tenant(context)
    async with get_pool().acquire() as conn, conn.transaction():
        session = await conn.fetchrow('''SELECT * FROM design_intake_sessions
            WHERE id=$1 AND tenant_id=$2''', session_id, tenant)
        if session is None:
            raise HTTPException(404, "intake session not found")
        return await _intake_response(conn, tenant, session)


@router.post("/{session_id}/answers")
async def answer_intake(session_id: UUID, payload: AnswerRequest, context: dict = WRITE):
    tenant = _tenant(context)
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            await _session(conn, tenant, session_id)
            source = SOURCES.get(payload.answer_source, payload.answer_source)
            if payload.answer_source not in SOURCES:
                # Let the intake validator produce its existing rejection.
                source = "invalid_http_source"
            answer = payload.answer
            if payload.answer_source == "unknown":
                row = await conn.fetchrow('''SELECT recommended FROM design_intake_questions
                    WHERE id=$1 AND session_id=$2 AND tenant_id=$3''',
                    payload.question_id, session_id, tenant)
                if row is None:
                    raise HTTPException(404, "intake question not found")
                answer = row["recommended"]
                if not isinstance(answer, str) or not answer.strip():
                    raise HTTPException(409, "question has no recommended answer")
            stamp = datetime.now(timezone.utc)
            await intake.record_answer(conn, tenant, session_id, payload.question_id,
                                       answer, source, confirmed_at=stamp)
            return {"question_id": payload.question_id, "answer": answer,
                    "answer_source": payload.answer_source, "confirmed_at": stamp,
                    "brief_confirmed": False}
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{session_id}/confirm")
async def confirm_intake(session_id: UUID, payload: ConfirmRequest, context: dict = WRITE):
    try:
        async with get_pool().acquire() as conn:
            stamp = await intake.confirm_brief(conn, _tenant(context), session_id, payload.brief)
            return {"session_id": session_id, "brief": payload.brief,
                    "brief_confirmed": True, "brief_confirmed_at": stamp}
    except ValueError as exc:
        raise _error(exc) from exc
