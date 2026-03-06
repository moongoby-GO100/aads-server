"""
AADS FastAPI 서버.
lifespan으로 그래프 컴파일 + checkpointer + MCP 초기화.
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.logging_config import configure_logging

from app.api import health, projects, checkpoints, stream, auth, context, chat, visual_qa, mobile_qa, memory
from app.api.channels import router as channels_router
from app.api.conversations import router as conversations_router
from app.api.project_dashboard import router as project_dashboard_router
from app.api.ceo_chat import router as ceo_chat_router
from app.api.watchdog import router as watchdog_router
from app.api.approval import router as approval_router
from app.api.documents import router as documents_router
from app.config import settings
from app.graph.builder import compile_graph
from app.services.checkpointer import get_checkpointer
from app.mcp.client import MCPClientManager, set_mcp_manager
from app.memory.store import memory_store

logger = structlog.get_logger()

# 전역 그래프 (lifespan에서 초기화)
app_state: dict = {"graph": None, "checkpointer": None, "mcp_manager": None, "memory_store": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 그래프 + checkpointer + MCP 초기화."""
    # 로깅 설정 초기화
    import os
    json_logs = os.getenv("ENVIRONMENT", "development") == "production"
    configure_logging(log_level=settings.LOG_LEVEL, json_format=json_logs)
    logger.info("aads_server_starting", env=settings.ENVIRONMENT, json_logs=json_logs)

    # Docker 샌드박스 이미지 사전 풀 (T-015, D-011)
    try:
        from app.services.sandbox import pull_images
        await pull_images()
        logger.info("sandbox_images_pulled")
    except Exception as e:
        logger.warning("sandbox_image_pull_failed_graceful_degradation", error=str(e))

    # Memory Store 초기화 (T-011)
    try:
        await memory_store.initialize()
        app_state["memory_store"] = memory_store
        logger.info("memory_store_initialized")
    except Exception as e:
        logger.warning("memory_store_init_failed_graceful_degradation", error=str(e))

    # MCP 매니저 초기화 (graceful degradation — MCP 없이도 동작)
    mcp_manager = MCPClientManager()
    try:
        await mcp_manager.initialize()
        set_mcp_manager(mcp_manager)
        app_state["mcp_manager"] = mcp_manager
        logger.info(
            "mcp_initialized",
            available_servers=mcp_manager.available_servers,
        )
    except Exception as e:
        logger.warning("mcp_init_failed_graceful_degradation", error=str(e))

    async with get_checkpointer() as checkpointer:
        graph = await compile_graph(checkpointer=checkpointer)
        app_state["graph"] = graph
        app_state["checkpointer"] = checkpointer
        logger.info(
            "graph_compiled",
            nodes=list(graph.get_graph().nodes.keys()),
        )
        yield

    # 종료 정리
    if mcp_manager:
        await mcp_manager.shutdown()
    await memory_store.close()
    app_state["graph"] = None
    app_state["checkpointer"] = None
    app_state["mcp_manager"] = None
    app_state["memory_store"] = None
    logger.info("aads_server_shutdown")


app = FastAPI(
    title="AADS API",
    version="0.2.0",
    description="Autonomous AI Development System — Phase 2 Dashboard",
    lifespan=lifespan,
)

# 글로벌 예외 핸들러
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "예기치 않은 오류가 발생했습니다",
            "type": type(exc).__name__,
        },
    )


# 라우터 등록
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(project_dashboard_router, prefix="/api/v1", tags=["project-dashboard"])
app.include_router(projects.router, prefix="/api/v1", tags=["projects"])
app.include_router(checkpoints.router, prefix="/api/v1", tags=["checkpoints"])
app.include_router(stream.router, prefix="/api/v1", tags=["stream"])
app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(context.router, prefix="/api/v1", tags=["context"])
app.include_router(chat.router, prefix="/api/v1", tags=["chat"])
app.include_router(visual_qa.router, prefix="/api/v1", tags=["visual-qa"])
app.include_router(mobile_qa.router, prefix="/api/v1", tags=["mobile-qa"])
app.include_router(memory.router, prefix="/api/v1", tags=["memory"])
app.include_router(conversations_router, prefix="/api/v1", tags=["conversations"])
app.include_router(ceo_chat_router, prefix="/api/v1", tags=["ceo-chat"])
app.include_router(watchdog_router, prefix="/api/v1", tags=["watchdog"])
app.include_router(approval_router, prefix="/api/v1", tags=["approval"])
app.include_router(documents_router, prefix="/api/v1/documents", tags=["documents"])
app.include_router(channels_router, prefix="/api/v1", tags=["channels"])
