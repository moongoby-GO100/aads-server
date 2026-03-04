from fastapi import APIRouter
from app.services.sandbox import check_sandbox_health

router = APIRouter()


@router.get("/health")
async def health_check():
    from app.main import app_state
    graph_ready = app_state.get("graph") is not None
    sandbox_health = await check_sandbox_health()
    return {
        "status": "ok" if graph_ready else "initializing",
        "graph_ready": graph_ready,
        "version": "0.1.0",
        "sandbox": sandbox_health,
    }
