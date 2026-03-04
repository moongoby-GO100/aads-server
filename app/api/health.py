from fastapi import APIRouter
import os

router = APIRouter()


@router.get("/health")
async def health_check():
    from app.main import app_state
    graph_ready = app_state.get("graph") is not None
    # BUG FIX T-016: sandbox 상태 포함 (E2E 테스트 호환)
    sandbox_status = "ok" if os.path.exists("/var/run/docker.sock") else "unavailable"
    return {
        "status": "ok" if graph_ready else "initializing",
        "graph_ready": graph_ready,
        "version": "0.1.0",
        "sandbox": {"status": sandbox_status},
    }
