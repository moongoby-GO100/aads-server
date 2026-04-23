"""LLM 모델 레지스트리 조회/동기화 API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.services.model_registry import list_provider_summaries, list_registered_models, sync_model_registry

router = APIRouter(prefix="/llm-models", tags=["llm-models"])


@router.get("")
async def list_llm_models(
    provider: str | None = Query(None),
    active_only: bool = Query(False),
) -> dict[str, Any]:
    models = await list_registered_models(provider=provider, active_only=active_only)
    return {"models": models, "total": len(models)}


@router.get("/providers/summary")
async def get_provider_summary() -> dict[str, Any]:
    summaries = await list_provider_summaries()
    return {"providers": summaries, "total": len(summaries)}


@router.post("/sync")
async def sync_llm_models() -> dict[str, Any]:
    return await sync_model_registry(triggered_by="llm_models_api", reason="manual_api")
