"""브레인스토밍 시각화 API."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.braming_service import (
    create_braming_session,
    expand_node,
    generate_counter,
    generate_ideas,
    generate_perspectives,
    get_session_graph,
    list_sessions,
    synthesize_session,
)

router = APIRouter(prefix="/api/v1/braming", tags=["braming"])


class BramingSessionCreateRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="브레인스토밍 주제")
    config: Optional[dict[str, Any]] = Field(default=None, description="세션 설정")


class PerspectiveGenerateRequest(BaseModel):
    topic: Optional[str] = Field(default=None, description="세션 주제를 덮어쓸 선택 값")


class IdeaGenerateRequest(BaseModel):
    perspective_node_id: str = Field(..., description="아이디어 생성 대상 perspective 노드 ID")


class CounterGenerateRequest(BaseModel):
    target_node_id: str = Field(..., description="반박 생성 대상 노드 ID")


class ExpandNodeRequest(BaseModel):
    node_id: str = Field(..., description="확장 대상 노드 ID")


@router.post("/sessions")
async def create_session(req: BramingSessionCreateRequest):
    try:
        return await create_braming_session(topic=req.topic, config=req.config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions")
async def get_sessions(limit: int = Query(20, ge=1, le=100)):
    try:
        return {"items": await list_sessions(limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{session_id}")
async def get_graph(session_id: str):
    try:
        return await get_session_graph(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/perspectives")
async def create_perspectives(session_id: str, req: PerspectiveGenerateRequest):
    try:
        topic = (req.topic or "").strip()
        graph = await get_session_graph(session_id)
        session_topic = graph["session"]["topic"]
        items = await generate_perspectives(session_id=session_id, topic=topic or session_topic)
        return {"items": items}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/ideas")
async def create_ideas(session_id: str, req: IdeaGenerateRequest):
    try:
        return {"items": await generate_ideas(session_id, req.perspective_node_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/counter")
async def create_counter(session_id: str, req: CounterGenerateRequest):
    try:
        return await generate_counter(session_id, req.target_node_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/expand")
async def expand_selected_node(session_id: str, req: ExpandNodeRequest):
    try:
        return {"items": await expand_node(session_id, req.node_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/synthesize")
async def synthesize(session_id: str):
    try:
        return await synthesize_session(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
