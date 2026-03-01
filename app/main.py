"""
AADS FastAPI 서버.
lifespan으로 그래프 컴파일 + checkpointer 초기화.
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api import health, projects, checkpoints
from app.config import settings
from app.graph.builder import compile_graph
from app.services.checkpointer import get_checkpointer

logger = structlog.get_logger()

# 전역 그래프 (lifespan에서 초기화)
app_state: dict = {"graph": None, "checkpointer": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 그래프 + checkpointer 초기화."""
    logger.info("aads_server_starting", env=settings.ENVIRONMENT)

    async with get_checkpointer() as checkpointer:
        graph = await compile_graph(checkpointer=checkpointer)
        app_state["graph"] = graph
        app_state["checkpointer"] = checkpointer
        logger.info(
            "graph_compiled",
            nodes=list(graph.get_graph().nodes.keys()),
        )
        yield

    app_state["graph"] = None
    app_state["checkpointer"] = None
    logger.info("aads_server_shutdown")


app = FastAPI(
    title="AADS API",
    version="0.1.0",
    description="Autonomous AI Development System",
    lifespan=lifespan,
)

# 라우터 등록
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(projects.router, prefix="/api/v1", tags=["projects"])
app.include_router(checkpoints.router, prefix="/api/v1", tags=["checkpoints"])
