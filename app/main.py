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
from app.api.managers import router as managers_router
from app.api.conversations import router as conversations_router
from app.api.project_dashboard import router as project_dashboard_router
# ceo_chat_router 등록 해제 — /chat (chat_v2_router)으로 통합 완료. ceo_chat.py는 pipeline_c에서 call_llm() 참조용으로 유지
# from app.api.ceo_chat import router as ceo_chat_router
from app.api.directives import router as directives_router
from app.api.watchdog import router as watchdog_router
from app.api.approval import router as approval_router
from app.api.briefing import router as briefing_router
from app.api.documents import router as documents_router
from app.api.ops import router as ops_router
from app.api.lessons import router as lessons_router
from app.api.strategy import router as strategy_router
from app.api.plans import router as plans_router
from app.api.debate_logs import router as debate_logs_router
from app.api.artifacts import router as artifacts_router
from app.routers.chat import router as chat_v2_router
from app.config import settings
from app.graph.builder import compile_graph
from app.services.checkpointer import get_checkpointer
from app.mcp.client import MCPClientManager, set_mcp_manager
from app.memory.store import memory_store
from app.core.mcp_server import setup_mcp

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

    # AADS-186C: Langfuse 초기화 (optional — graceful degradation)
    try:
        from app.core.langfuse_config import init_langfuse
        lf_enabled = init_langfuse()
        logger.info("langfuse_status", enabled=lf_enabled)
    except Exception as e:
        logger.warning("langfuse_init_failed", error=str(e))

    # AADS-186C: Telegram 봇 초기화 (optional — graceful degradation)
    try:
        from app.services.telegram_bot import init_telegram_bot
        init_telegram_bot()
    except Exception as e:
        logger.warning("telegram_bot_init_failed", error=str(e))

    # AADS-186C: APScheduler 시작 (2분 주기 알림평가 + 09:00 KST 일일요약)
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from app.services.alert_manager import get_alert_manager
        from app.services.telegram_bot import get_telegram_bot

        async def _run_alert_evaluation():
            try:
                mgr = get_alert_manager()
                alerts = await mgr.evaluate_rules()
                for alert in alerts:
                    await mgr.send_alert(alert)
            except Exception as e:
                logger.warning("scheduler_alert_eval_failed", error=str(e))

        async def _run_daily_summary():
            try:
                bot = get_telegram_bot()
                if bot and bot.is_ready:
                    await bot.send_daily_summary()
            except Exception as e:
                logger.warning("scheduler_daily_summary_failed", error=str(e))

        async def _run_weekly_briefing():
            """AADS-186E-3: 주간 CEO 브리핑 — AutonomousExecutor 기반 자율 생성."""
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                from app.services.autonomous_executor import generate_weekly_briefing

                now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
                logger.info("weekly_briefing_started", date=now_kst.strftime("%Y-%m-%d"))

                briefing = await generate_weekly_briefing()

                bot = get_telegram_bot()
                if bot and bot.is_ready:
                    header = f"📊 *AADS 주간 CEO 브리핑* — {now_kst.strftime('%Y-%m-%d')} (월)\n\n"
                    # Telegram 메시지 최대 4096자
                    msg = header + briefing[:3800] + "\n\n🔗 https://aads.newtalk.kr/"
                    await bot.send_message(msg)
                    logger.info("weekly_briefing_sent")
                else:
                    logger.warning("weekly_briefing_telegram_unavailable")
            except Exception as e:
                logger.warning("weekly_briefing_failed", error=str(e))

        # Unified Healer 초기화
        from app.services.unified_healer import healing_cycle, initialize as healer_init

        async def _run_healing_cycle():
            try:
                await healing_cycle()
            except Exception as e:
                logger.warning("scheduler_healing_cycle_failed", error=str(e))

        scheduler = AsyncIOScheduler()
        # 2분마다 규칙 평가
        scheduler.add_job(_run_alert_evaluation, "interval", minutes=2, id="alert_eval")
        # 30초마다 자율복구 사이클
        scheduler.add_job(_run_healing_cycle, "interval", seconds=30, id="healing_cycle")
        # 매일 09:00 KST (= UTC 00:00)
        scheduler.add_job(_run_daily_summary, CronTrigger(hour=0, minute=0, timezone="UTC"), id="daily_summary")
        # 매주 월요일 09:00 KST (= UTC 00:00, day_of_week=mon) — AADS-186D
        scheduler.add_job(
            _run_weekly_briefing,
            CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC"),
            id="weekly_briefing",
        )
        scheduler.start()
        await healer_init()
        # AADS-190: 스케줄러 인스턴스를 동적 스케줄 도구에 공유
        try:
            from app.api.ceo_chat_tools_scheduler import set_scheduler
            set_scheduler(scheduler)
        except Exception:
            pass
        logger.info("apscheduler_started", jobs=["alert_eval", "healing_cycle", "daily_summary", "weekly_briefing"])
    except Exception as e:
        logger.warning("apscheduler_start_failed_graceful_degradation", error=str(e))
        scheduler = None

    # DB Connection Pool 초기화 (AADS-CRITICAL-FIX #1)
    try:
        from app.core.db_pool import init_pool
        db_pool = await init_pool()
        app_state["db_pool"] = db_pool
    except Exception as e:
        logger.error("db_pool_init_failed", error=str(e))
        app_state["db_pool"] = None

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
    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("apscheduler_stopped")
    try:
        from app.core.langfuse_config import flush_langfuse
        flush_langfuse()
    except Exception:
        pass
    if mcp_manager:
        await mcp_manager.shutdown()
    await memory_store.close()
    # DB Connection Pool 종료 (AADS-CRITICAL-FIX #1)
    try:
        from app.core.db_pool import close_pool
        await close_pool()
    except Exception:
        pass
    app_state["graph"] = None
    app_state["checkpointer"] = None
    app_state["mcp_manager"] = None
    app_state["memory_store"] = None
    app_state["db_pool"] = None
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
# app.include_router(ceo_chat_router, prefix="/api/v1", tags=["ceo-chat"])  # /chat으로 통합
app.include_router(directives_router, prefix="/api/v1", tags=["directives"])
app.include_router(watchdog_router, prefix="/api/v1", tags=["watchdog"])
app.include_router(approval_router, prefix="/api/v1", tags=["approval"])
app.include_router(documents_router, prefix="/api/v1/documents", tags=["documents"])
app.include_router(channels_router, prefix="/api/v1", tags=["channels"])
app.include_router(managers_router, prefix="/api/v1", tags=["managers"])
app.include_router(ops_router, prefix="/api/v1", tags=["ops"])
app.include_router(lessons_router, prefix="/api/v1", tags=["lessons"])
app.include_router(strategy_router, prefix="/api/v1", tags=["strategy"])
app.include_router(plans_router, prefix="/api/v1", tags=["plans"])
app.include_router(debate_logs_router, prefix="/api/v1", tags=["debate-logs"])
app.include_router(artifacts_router, prefix="/api/v1", tags=["artifacts"])
app.include_router(briefing_router, prefix="/api/v1", tags=["briefing"])
app.include_router(chat_v2_router, prefix="/api/v1", tags=["chat-v2"])# AADS-186C: FastAPI-MCP 마운트 (graceful — MCP_ENABLED=false 시 비활성)
setup_mcp(app)