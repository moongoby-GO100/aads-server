"""API surface for CEO PC local model install/test queue."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.local_model_manager import local_model_manager

router = APIRouter(prefix="/local-models", tags=["local-models"])


class LocalModelRunRequest(BaseModel):
    item_id: str = Field(..., description="Canonical queue item_id")
    action: str = Field("prepare", description="status, prepare, install, test, or install_test")
    agent_id: str = ""
    allow_install: bool = False
    allow_download: bool = False
    timeout_seconds: float = Field(900.0, ge=30.0, le=1800.0)
    wait_for_turn: bool = True


@router.get("/queue")
async def list_local_model_queue(
    bridge: str = Query(""),
    category: str = Query(""),
    priority: int | None = Query(None),
    include_ollama: bool = Query(True),
) -> dict[str, Any]:
    items = local_model_manager.list_queue(
        bridge=bridge,
        category=category,
        priority=priority,
        include_ollama=include_ollama,
    )
    return {"queue": items, "count": len(items)}


@router.get("/status")
async def local_model_queue_status(
    agent_id: str = Query(""),
    include_items: bool = Query(True),
) -> dict[str, Any]:
    return await local_model_manager.queue_status(
        agent_id=agent_id,
        include_items=include_items,
    )


@router.post("/run")
async def run_local_model_install_test(req: LocalModelRunRequest) -> dict[str, Any]:
    return await local_model_manager.run_install_test(
        item_id=req.item_id,
        action=req.action,
        agent_id=req.agent_id,
        allow_install=req.allow_install,
        allow_download=req.allow_download,
        timeout_seconds=req.timeout_seconds,
        wait_for_turn=req.wait_for_turn,
    )
