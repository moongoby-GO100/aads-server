"""OHVIS 내부 LangSmith-compatible LLMOps API.

GET  /api/v1/ohvis/llmops/status               — foundation 상태 + export 게이트
GET  /api/v1/ohvis/llmops/traces               — trace 검색 (v1 폴백 포함)
GET  /api/v1/ohvis/llmops/traces/{trace_id}    — trace 상세 (span/tool call/feedback)
GET  /api/v1/ohvis/llmops/candidates           — 실패/저품질 승격 후보
POST /api/v1/ohvis/llmops/datasets/from-trace  — trace를 eval example로 승격
POST /api/v1/ohvis/llmops/evals/run            — rule evaluator 오프라인 평가 실행
GET  /api/v1/ohvis/llmops/evals/{experiment_id}— 평가 결과 조회
POST /api/v1/ohvis/llmops/feedback             — trace 피드백 기록

인증은 기존 OHVIS API(`ohvis_harness`, `ohvis_tasks`)와 동일하게 라우터 수준
의존성 없이 앱 미들웨어에 맡긴다.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import llmops_evaluator, llmops_store

router = APIRouter()


class DatasetFromTraceRequest(BaseModel):
    trace_id: str = Field(..., min_length=8, max_length=64)
    dataset_slug: str = Field(llmops_store.DEFAULT_FAILURE_DATASET, min_length=1, max_length=120)
    project: Optional[str] = Field(None, max_length=40)
    force: bool = Field(False, description="성공/고품질 trace도 강제로 승격한다")


class EvalRunRequest(BaseModel):
    dataset_slug: Optional[str] = Field(None, max_length=120)
    dataset_id: Optional[str] = Field(None, max_length=64)
    name: str = Field("", max_length=200)
    candidate_sha: Optional[str] = Field(None, max_length=64)
    model_id: Optional[str] = Field(None, max_length=120)
    limit: int = Field(200, ge=1, le=1000)
    created_by: Optional[str] = Field(None, max_length=120)


class FeedbackRequest(BaseModel):
    trace_id: Optional[str] = Field(None, max_length=64)
    source_ref: Optional[str] = Field(None, max_length=200)
    rating: Optional[int] = Field(None, ge=-1, le=5)
    label: Optional[str] = Field(None, max_length=60)
    comment: str = Field("", max_length=2000)
    created_by: Optional[str] = Field(None, max_length=120)


@router.get("/ohvis/llmops/status", tags=["ohvis-llmops"])
async def llmops_status(project: Optional[str] = Query(None, max_length=40)):
    return await llmops_store.get_status(project=project)


@router.get("/ohvis/llmops/traces", tags=["ohvis-llmops"])
async def llmops_list_traces(
    project: Optional[str] = Query(None, max_length=40),
    status: Optional[str] = Query(None, max_length=30),
    graph_run_id: Optional[str] = Query(None, max_length=200),
    session_id: Optional[str] = Query(None, max_length=64),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(50, ge=1, le=llmops_store.MAX_LIMIT),
    include_legacy: bool = Query(True),
):
    return await llmops_store.list_traces(
        project=project,
        status=status,
        graph_run_id=graph_run_id,
        session_id=session_id,
        hours=hours,
        limit=limit,
        include_legacy=include_legacy,
    )


@router.get("/ohvis/llmops/candidates", tags=["ohvis-llmops"])
async def llmops_promotion_candidates(
    project: Optional[str] = Query(None, max_length=40),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    candidates = await llmops_store.find_promotion_candidates(
        project=project, hours=hours, limit=limit
    )
    return {"candidates": candidates, "count": len(candidates), "quality_floor": llmops_store.QUALITY_FLOOR}


@router.get("/ohvis/llmops/traces/{trace_id}", tags=["ohvis-llmops"])
async def llmops_get_trace(trace_id: str):
    trace = await llmops_store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


@router.post("/ohvis/llmops/datasets/from-trace", tags=["ohvis-llmops"])
async def llmops_promote_trace(req: DatasetFromTraceRequest):
    result = await llmops_store.promote_trace_to_dataset(
        req.trace_id,
        dataset_slug=req.dataset_slug,
        project=req.project,
        force=req.force,
    )
    if not result.get("promoted") and result.get("reason") == "trace_not_found":
        raise HTTPException(status_code=404, detail="trace not found")
    return result


@router.post("/ohvis/llmops/evals/run", tags=["ohvis-llmops"])
async def llmops_run_eval(req: EvalRunRequest):
    if not req.dataset_slug and not req.dataset_id:
        raise HTTPException(status_code=400, detail="dataset_slug 또는 dataset_id가 필요합니다")
    try:
        result = await llmops_evaluator.run_experiment(
            dataset_slug=req.dataset_slug,
            dataset_id=req.dataset_id,
            name=req.name,
            candidate_sha=req.candidate_sha,
            model_id=req.model_id,
            limit=req.limit,
            created_by=req.created_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"eval failed: {str(exc)[:200]}") from exc
    if not result.get("ok") and result.get("reason") == "dataset_not_found":
        raise HTTPException(status_code=404, detail="dataset not found")
    return result


@router.get("/ohvis/llmops/evals/{experiment_id}", tags=["ohvis-llmops"])
async def llmops_get_eval(experiment_id: str):
    experiment = await llmops_evaluator.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@router.post("/ohvis/llmops/feedback", tags=["ohvis-llmops"])
async def llmops_record_feedback(req: FeedbackRequest):
    ok = await llmops_store.record_feedback(
        trace_id=req.trace_id,
        source_ref=req.source_ref,
        rating=req.rating,
        label=req.label,
        comment=req.comment,
        created_by=req.created_by,
    )
    return {"recorded": ok}
