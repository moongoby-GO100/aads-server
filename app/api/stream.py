"""
SSE 스트리밍 엔드포인트.
POST /api/v1/projects/{id}/stream — 8-agent 실행 상태를 SSE로 실시간 전송.
"""
import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()

KEEPALIVE_INTERVAL = 20  # 초 (Nginx 60s timeout 대비)


def _sse_event(event: str, data: dict) -> str:
    """SSE 포맷으로 변환."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_project_execution(project_id: str):
    """프로젝트 실행 상태를 SSE로 스트리밍."""
    from app.main import app_state

    graph = app_state.get("graph")
    if not graph:
        yield _sse_event("error", {"message": "Graph not ready", "project_id": project_id})
        return

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    state = await graph.aget_state(config)
    if not state or not state.values:
        yield _sse_event("error", {"message": "Project not found", "project_id": project_id})
        return

    # 현재 상태 브로드캐스트
    current_stage = state.values.get("checkpoint_stage", "unknown")
    agents_completed = state.values.get("active_agents", [])
    llm_calls = state.values.get("llm_calls_count", 0)
    total_cost = state.values.get("total_cost_usd", 0.0)

    # 에이전트 완료 상태 전송
    for agent in agents_completed:
        yield _sse_event("agent_complete", {
            "agent": agent,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "completed",
        })
        await asyncio.sleep(0.05)

    # 현재 체크포인트 전송
    yield _sse_event("checkpoint", {
        "stage": current_stage,
        "auto_approved": True,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # 파이프라인 상태 전송
    is_completed = current_stage in ("completed", "cancelled")
    yield _sse_event("pipeline_status", {
        "project_id": project_id,
        "status": "completed" if is_completed else "in_progress",
        "checkpoint_stage": current_stage,
        "llm_calls_count": llm_calls,
        "total_cost_usd": total_cost,
        "generated_files": state.values.get("generated_files", []),
    })

    if is_completed:
        yield _sse_event("pipeline_complete", {
            "project_id": project_id,
            "total_cost_usd": total_cost,
            "llm_calls_count": llm_calls,
            "timestamp": datetime.utcnow().isoformat(),
        })


@router.post("/projects/{project_id}/stream")
async def stream_project(project_id: str):
    """프로젝트 실행 상태를 SSE로 스트리밍.

    Server-Sent Events 형식:
      event: agent_start | agent_complete | checkpoint | pipeline_status | pipeline_complete | error
      data: JSON 객체
    """
    async def event_generator():
        try:
            # keepalive ping
            yield ": keepalive\n\n"

            async for chunk in _stream_project_execution(project_id):
                yield chunk

            # 종료 신호
            yield _sse_event("done", {"project_id": project_id})
        except Exception as e:
            yield _sse_event("error", {"message": str(e), "project_id": project_id})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx proxy_buffering off
        },
    )
