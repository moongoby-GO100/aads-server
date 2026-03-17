"""
AADS-1864: QA 검증 보고 + 매니저 AI 승인 프로세스

- POST /qa/report — 작업 완료 보고 (매니저 AI에 전달)
- GET /qa/status/{task_id} — QA 상태 조회
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── 모델 ─────────────────────────────────────────────────────────────────────

class QAReportRequest(BaseModel):
    """작업 완료 보고 요청."""
    job_id: str
    project: str = "AADS"
    task_id: Optional[str] = None
    checklist_results: Optional[dict] = None  # 체크리스트 항목별 결과
    summary: str = ""


class QAReportResponse(BaseModel):
    status: str
    qa_id: str
    message: str
    manager_notified: bool


class QAStatusResponse(BaseModel):
    job_id: str
    qa_status: str  # pending_review | manager_approved | manager_rejected | ceo_approved | ceo_rejected
    checklist_results: Optional[dict] = None
    manager_feedback: Optional[str] = None
    reported_at: Optional[str] = None


# ─── 인메모리 QA 상태 저장소 ──────────────────────────────────────────────────
# (향후 DB 테이블로 전환 가능)
_qa_store: dict[str, dict] = {}


# ─── 엔드포인트 ──────────────────────────────────────────────────────────────

@router.post("/qa/report", response_model=QAReportResponse)
async def report_qa(req: QAReportRequest):
    """
    작업 완료 보고 → 매니저 AI 세션에 자동 메시지 전송.

    1. 체크리스트 결과 저장
    2. 해당 프로젝트 매니저 세션에 보고 메시지 삽입
    3. 매니저 AI 응답 후 '승인' 키워드 감지 시 pipeline_jobs 상태 전환
    """
    qa_id = f"qa-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    # QA 상태 저장
    _qa_store[req.job_id] = {
        "qa_id": qa_id,
        "job_id": req.job_id,
        "project": req.project,
        "task_id": req.task_id,
        "qa_status": "pending_review",
        "checklist_results": req.checklist_results or {},
        "summary": req.summary,
        "manager_feedback": None,
        "reported_at": now,
    }

    # 매니저 AI 세션에 보고 메시지 전송
    manager_notified = await _notify_manager(req)

    logger.info(f"qa_report_submitted qa_id={qa_id} job_id={req.job_id} project={req.project} manager_notified={manager_notified}")

    return QAReportResponse(
        status="ok",
        qa_id=qa_id,
        message="QA 보고 접수 완료. 매니저 AI 검토 대기 중."
        if manager_notified
        else "QA 보고 접수 완료. 매니저 세션 없음 — 수동 검토 필요.",
        manager_notified=manager_notified,
    )


@router.get("/qa/status/{job_id}", response_model=QAStatusResponse)
async def get_qa_status(job_id: str):
    """QA 상태 조회."""
    entry = _qa_store.get(job_id)
    if not entry:
        # DB에서 pipeline_jobs 상태로 폴백
        fallback = await _get_pipeline_job_status(job_id)
        if fallback:
            return QAStatusResponse(
                job_id=job_id,
                qa_status=fallback.get("status", "unknown"),
                checklist_results=None,
                manager_feedback=None,
                reported_at=None,
            )
        raise HTTPException(404, f"QA 보고를 찾을 수 없음: {job_id}")

    return QAStatusResponse(
        job_id=entry["job_id"],
        qa_status=entry["qa_status"],
        checklist_results=entry.get("checklist_results"),
        manager_feedback=entry.get("manager_feedback"),
        reported_at=entry.get("reported_at"),
    )


@router.post("/qa/manager-approve/{job_id}")
async def manager_approve(job_id: str, feedback: str = ""):
    """매니저 AI 승인 처리 → pipeline_jobs 상태를 pending_ceo_approval로 전환."""
    entry = _qa_store.get(job_id)
    if not entry:
        raise HTTPException(404, f"QA 보고를 찾을 수 없음: {job_id}")

    entry["qa_status"] = "manager_approved"
    entry["manager_feedback"] = feedback or "매니저 AI 승인 완료"

    # pipeline_jobs 상태 전환
    await _update_pipeline_status(job_id, "awaiting_approval")

    logger.info(f"qa_manager_approved job_id={job_id}")
    return {"status": "ok", "job_id": job_id, "qa_status": "manager_approved"}


@router.post("/qa/manager-reject/{job_id}")
async def manager_reject(job_id: str, feedback: str = "검증 실패"):
    """매니저 AI 거부 처리."""
    entry = _qa_store.get(job_id)
    if not entry:
        raise HTTPException(404, f"QA 보고를 찾을 수 없음: {job_id}")

    entry["qa_status"] = "manager_rejected"
    entry["manager_feedback"] = feedback

    logger.info(f"qa_manager_rejected job_id={job_id} feedback={feedback}")
    return {"status": "ok", "job_id": job_id, "qa_status": "manager_rejected", "feedback": feedback}


# ─── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

async def _notify_manager(req: QAReportRequest) -> bool:
    """해당 프로젝트 매니저 세션에 QA 보고 메시지 삽입."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if not pool:
            logger.warning("qa_notify_manager: DB pool 없음")
            return False

        # 프로젝트 매니저 워크스페이스에서 최근 세션 조회
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT cs.id as session_id
                FROM chat_sessions cs
                JOIN chat_workspaces cw ON cs.workspace_id = cw.id
                WHERE UPPER(cw.name) LIKE $1
                ORDER BY cs.updated_at DESC
                LIMIT 1
                """,
                f"%{req.project.upper()}%",
            )

            if not row:
                logger.warning(f"qa_notify_manager: {req.project} 매니저 세션 없음")
                return False

            session_id = str(row["session_id"])

            # 체크리스트 결과 포맷
            checklist_text = ""
            if req.checklist_results:
                for key, val in req.checklist_results.items():
                    icon = "✅" if val else "❌"
                    checklist_text += f"  {icon} {key}: {'PASS' if val else 'FAIL'}\n"

            # 매니저 세션에 보고 메시지 삽입
            report_msg = (
                f"📋 **[QA 완료 보고]** `{req.job_id}`\n"
                f"프로젝트: **{req.project}**\n"
                f"Task: {req.task_id or 'N/A'}\n\n"
                f"**요약:** {req.summary}\n\n"
                f"**검증 체크리스트:**\n{checklist_text or '(체크리스트 미제출)'}\n\n"
                f"---\n"
                f"승인하려면: POST /api/v1/qa/manager-approve/{req.job_id}\n"
                f"거부하려면: POST /api/v1/qa/manager-reject/{req.job_id}"
            )

            await conn.execute(
                """
                INSERT INTO chat_messages
                    (session_id, role, content, model_used, intent, cost,
                     tokens_in, tokens_out, attachments, sources, tools_called)
                VALUES ($1::uuid, 'system', $2, NULL, 'qa_report', 0, 0, 0,
                        '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                """,
                session_id,
                report_msg,
            )
            await conn.execute(
                "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                session_id,
            )

            logger.info(f"qa_notify_manager: 보고 전송 완료 session={session_id[:8]}...")
            return True

    except Exception as e:
        logger.warning(f"qa_notify_manager error: {e}")
        return False


async def _update_pipeline_status(job_id: str, new_status: str) -> None:
    """pipeline_jobs 테이블 상태 업데이트."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE pipeline_jobs SET status = $1, updated_at = NOW() WHERE job_id = $2",
                new_status,
                job_id,
            )
    except Exception as e:
        logger.warning(f"qa_update_pipeline_status error: {e}")


async def _get_pipeline_job_status(job_id: str) -> Optional[dict]:
    """pipeline_jobs 에서 상태 조회."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        if not pool:
            return None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT job_id, status, phase FROM pipeline_jobs WHERE job_id = $1",
                job_id,
            )
            if row:
                return {"job_id": row["job_id"], "status": row["status"], "phase": row["phase"]}
    except Exception as e:
        logger.warning(f"qa_get_pipeline_status error: {e}")
    return None


# ─── Pipeline C 완료 시 자동 보고 헬퍼 ─────────────────────────────────────────

async def auto_report_on_completion(job_id: str, project: str, task_id: str = "",
                                     summary: str = "", checklist_results: dict = None) -> bool:
    """Pipeline C 완료 시 자동으로 매니저에게 QA 보고. pipeline_c.py에서 호출."""
    try:
        req = QAReportRequest(
            job_id=job_id,
            project=project,
            task_id=task_id,
            checklist_results=checklist_results or {},
            summary=summary or f"Pipeline C 작업 {job_id} 완료",
        )
        resp = await report_qa(req)
        return resp.manager_notified
    except Exception as e:
        logger.warning(f"auto_report_on_completion error: {e}")
        return False
