"""OHVIS internal LangSmith-compatible LLMOps API.

Routes (PRD §5.6):
    GET  /ohvis/llmops/status
    GET  /ohvis/llmops/traces
    GET  /ohvis/llmops/traces/{trace_id}
    GET  /ohvis/llmops/eval-candidates
    POST /ohvis/llmops/datasets/from-trace
    POST /ohvis/llmops/evals/run
    GET  /ohvis/llmops/evals/{experiment_id}

Auth follows the existing OHVIS surface: no per-route dependency, the global
`jwt_auth_middleware` in `app/main.py` gates `/api/v1/ohvis/*`.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.llmops_eval import (
    DatasetPromotionError,
    evaluate_trace,
    get_experiment,
    promote_trace_to_dataset,
    run_experiment,
)
from app.services.llmops_store import (
    get_status,
    get_trace,
    list_eval_candidates,
    list_traces,
)

router = APIRouter()


class DatasetFromTraceRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=200)
    dataset_slug: Optional[str] = Field(None, max_length=80)
    dataset_title: Optional[str] = Field(None, max_length=200)
    project: Optional[str] = Field(None, max_length=40)
    purpose: str = Field("", max_length=500)
    expected: str = Field("", max_length=4000)
    rubric: Optional[dict[str, Any]] = None


class EvalRunRequest(BaseModel):
    dataset_slug: str = Field(..., min_length=1, max_length=80)
    evaluator: str = Field("rule_v1", max_length=40)
    candidate_sha: Optional[str] = Field(None, max_length=64)
    model_id: Optional[str] = Field(None, max_length=80)
    name: str = Field("", max_length=200)
    limit: int = Field(200, ge=1, le=500)


@router.get("/ohvis/llmops/status", tags=["ohvis-llmops"])
async def ohvis_llmops_status(project: Optional[str] = Query(None, max_length=40)):
    return await get_status(project=project)


@router.get("/ohvis/llmops/traces", tags=["ohvis-llmops"])
async def ohvis_llmops_traces(
    project: Optional[str] = Query(None, max_length=40),
    status: Optional[str] = Query(None, max_length=20),
    graph_run_id: Optional[str] = Query(None, max_length=200),
    session_id: Optional[str] = Query(None, max_length=64),
    since_hours: Optional[int] = Query(None, ge=1, le=8760),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await list_traces(
        project=project,
        status=status,
        graph_run_id=graph_run_id,
        session_id=session_id,
        since_hours=since_hours,
        limit=limit,
        offset=offset,
    )


@router.get("/ohvis/llmops/eval-candidates", tags=["ohvis-llmops"])
async def ohvis_llmops_eval_candidates(
    project: Optional[str] = Query(None, max_length=40),
    quality_threshold: float = Query(0.4, ge=0.0, le=1.0),
    since_hours: int = Query(168, ge=1, le=8760),
    limit: int = Query(20, ge=1, le=100),
):
    """Failed / low-quality traces worth promoting into an eval dataset (FR-004)."""
    candidates = await list_eval_candidates(
        project=project,
        quality_threshold=quality_threshold,
        since_hours=since_hours,
        limit=limit,
    )
    return {"candidates": candidates, "count": len(candidates)}


@router.get("/ohvis/llmops/traces/{trace_id:path}", tags=["ohvis-llmops"])
async def ohvis_llmops_trace_detail(trace_id: str):
    trace = await get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
    trace["evaluation"] = evaluate_trace(trace)
    return trace


@router.post("/ohvis/llmops/datasets/from-trace", tags=["ohvis-llmops"])
async def ohvis_llmops_dataset_from_trace(req: DatasetFromTraceRequest):
    try:
        return await promote_trace_to_dataset(
            trace_id=req.trace_id,
            dataset_slug=req.dataset_slug,
            dataset_title=req.dataset_title,
            project=req.project,
            purpose=req.purpose,
            expected=req.expected,
            rubric=req.rubric,
        )
    except DatasetPromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ohvis/llmops/evals/run", tags=["ohvis-llmops"])
async def ohvis_llmops_eval_run(req: EvalRunRequest):
    try:
        return await run_experiment(
            dataset_slug=req.dataset_slug,
            evaluator=req.evaluator,
            candidate_sha=req.candidate_sha,
            model_id=req.model_id,
            name=req.name,
            limit=req.limit,
        )
    except DatasetPromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ohvis/llmops/evals/{experiment_id}", tags=["ohvis-llmops"])
async def ohvis_llmops_eval_detail(experiment_id: str):
    experiment = await get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail=f"experiment not found: {experiment_id}")
    return experiment
