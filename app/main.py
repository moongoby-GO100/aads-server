"""
AADS FastAPI 서버.
lifespan으로 그래프 컴파일 + checkpointer + MCP 초기화.
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
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
from app.api.task_monitor import router as task_monitor_router
from app.api.qa import router as qa_router
from app.api.image import router as image_router
from app.api.fact_check import router as fact_check_router
from app.api.pipeline_runner import router as pipeline_runner_router
from app.api.code_review import router as code_review_router
from app.api.quality import router as quality_router
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

    # -- 서버 재시작 시 중단된 스트리밍: DB pool 초기화 후 resume (아래 참조) --
    # resume_interrupted_streams는 DB pool 이후에 실행됨 (line ~290)



    # -- [P1-Fix] restart recovery: check done files, set interrupted not error --
    try:
        import os as _os
        from app.core.db_pool import get_pool as _gp2
        _pool2 = _gp2()
        async with _pool2.acquire() as _c2:
            _running_jobs = await _c2.fetch(
                "SELECT job_id FROM pipeline_c_jobs WHERE status='running'"
            )
            _recovered = 0
            _interrupted = 0
            for _row in _running_jobs:
                _jid = _row["job_id"]
                _done_file = "/tmp/pipeline_c_" + _jid + ".done"
                try:
                    if _os.path.exists(_done_file):
                        _exit_code = open(_done_file).read().strip()
                        if _exit_code == "0":
                            await _c2.execute(
                                "UPDATE pipeline_c_jobs SET status='awaiting_approval',"
                                "phase='awaiting_approval',error_msg='recovered after restart'"
                                " WHERE job_id=$1", _jid
                            )
                            _recovered += 1
                        else:
                            await _c2.execute(
                                "UPDATE pipeline_c_jobs SET status='error',"
                                "error_msg='exit=" + str(_exit_code) + "' WHERE job_id=$1", _jid
                            )
                    else:
                        await _c2.execute(
                            "UPDATE pipeline_c_jobs SET status='interrupted',"
                            "error_msg='server restarted, nohup may still run'"
                            " WHERE job_id=$1", _jid
                        )
                        _interrupted += 1
                except Exception as _je:
                    logger.warning("startup_recovery_job_error job=" + _jid + ": " + str(_je))
            if _running_jobs:
                logger.info("startup_recovery total=" + str(len(_running_jobs)) + " recovered=" + str(_recovered) + " interrupted=" + str(_interrupted))
    except Exception as _e:
        logger.warning("startup_pipeline_c_cleanup_failed: " + str(_e))

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
        # F11: 매일 03:00 UTC — ai_observations GC (confidence 감쇠 + 삭제)
        async def _run_memory_gc():
            try:
                from app.core.memory_gc import gc_observations
                from app.core.db_pool import get_pool
                await gc_observations(get_pool())
            except Exception as e:
                logger.warning(f"memory_gc_job_error: {e}")
        scheduler.add_job(_run_memory_gc, CronTrigger(hour=3, minute=0, timezone="UTC"), id="memory_gc")
        # F4: Memory Consolidation — 매일 04:00 UTC (중복 병합, confidence 강화/감쇠)
        async def _run_memory_consolidation():
            try:
                from app.core.memory_gc import consolidate_memory_facts
                from app.core.db_pool import get_pool
                await consolidate_memory_facts(get_pool())
            except Exception as e:
                logger.warning(f"memory_consolidation_job_error: {e}")
        scheduler.add_job(_run_memory_consolidation, CronTrigger(hour=4, minute=0, timezone="UTC"), id="memory_consolidation")
        # C1: Sleep-Time Agent — 매일 05:00 UTC (인사이트 생성 + 프롬프트 최적화)
        async def _run_sleep_time_agent():
            try:
                from app.core.memory_gc import sleep_time_consolidation
                from app.core.db_pool import get_pool
                await sleep_time_consolidation(get_pool())
            except Exception as e:
                logger.warning(f"sleep_time_agent_job_error: {e}")
        scheduler.add_job(_run_sleep_time_agent, CronTrigger(hour=5, minute=0, timezone="UTC"), id="sleep_time_agent")
        # Layer C: Background Session Compaction — 2시간마다 (200건 이상 미압축 세션 자동 압축)
        async def _run_background_compaction():
            try:
                from app.core.memory_gc import background_session_compaction
                await background_session_compaction()
            except Exception as e:
                logger.warning(f"background_compaction_job_error: {e}")
        scheduler.add_job(_run_background_compaction, 'interval', hours=2, id='background_compaction')
        # P2: eval_pipeline — 품질 대시보드 집계 (매일 06:00 UTC, sleep-time 이후)
        async def _run_quality_stats():
            try:
                from app.services.eval_pipeline import aggregate_quality_stats
                from app.core.db_pool import get_pool
                result = await aggregate_quality_stats(get_pool())
                logger.info("eval_pipeline_quality_stats_done", total=result.get("overall", {}).get("total_scored", 0))
            except Exception as e:
                logger.warning(f"eval_pipeline_quality_stats_error: {e}")
        scheduler.add_job(_run_quality_stats, CronTrigger(hour=6, minute=0, timezone="UTC"), id="eval_quality_stats")
        # P2: eval_pipeline — 품질 회귀 감지 (매일 06:30 UTC)
        async def _run_quality_regression():
            try:
                from app.services.eval_pipeline import detect_quality_regression
                from app.core.db_pool import get_pool
                regressions = await detect_quality_regression(get_pool())
                if regressions:
                    logger.warning("eval_pipeline_regression_detected", count=len(regressions))
            except Exception as e:
                logger.warning(f"eval_pipeline_quality_regression_error: {e}")
        scheduler.add_job(_run_quality_regression, CronTrigger(hour=6, minute=30, timezone="UTC"), id="eval_quality_regression")
        # Phase 1: Quality Feedback Loop — 매일 06:45 UTC (eval_pipeline 이후)
        async def _run_quality_feedback():
            try:
                from app.services.quality_feedback_loop import analyze_quality_weaknesses
                from app.core.db_pool import get_pool
                result = await analyze_quality_weaknesses(get_pool())
                if result.get("directives_created", 0) > 0:
                    logger.info("quality_feedback_directives_created", count=result["directives_created"])
            except Exception as e:
                logger.warning(f"quality_feedback_job_error: {e}")
        scheduler.add_job(_run_quality_feedback, CronTrigger(hour=6, minute=45, timezone="UTC"), id="quality_feedback")
        # Phase 2: Autonomous Research Agent — 매일 07:00 UTC (16:00 KST)
        async def _run_research_agent():
            try:
                from app.services.research_agent import run_daily_research
                from app.core.db_pool import get_pool
                result = await run_daily_research(get_pool())
                logger.info("research_agent_done", findings=len(result.get("findings", [])))
            except Exception as e:
                logger.warning(f"research_agent_job_error: {e}")
        scheduler.add_job(_run_research_agent, CronTrigger(hour=7, minute=0, timezone="UTC"), id="research_agent")
        # Phase 3: Experience Learner — 매일 07:30 UTC (연구 에이전트 이후)
        async def _run_experience_learner():
            try:
                from app.services.experience_learner import process_completed_jobs
                from app.core.db_pool import get_pool
                result = await process_completed_jobs(get_pool())
                if result.get("processed", 0) > 0:
                    logger.info("experience_learner_done", processed=result["processed"])
            except Exception as e:
                logger.warning(f"experience_learner_job_error: {e}")
        scheduler.add_job(_run_experience_learner, CronTrigger(hour=7, minute=30, timezone="UTC"), id="experience_learner")
        # P2: eval_pipeline — 주간 품질 리포트 (매주 월요일 07:00 UTC)
        async def _run_weekly_quality_report():
            try:
                from app.services.eval_pipeline import generate_weekly_report
                from app.core.db_pool import get_pool
                report = await generate_weekly_report(get_pool())
                logger.info("eval_pipeline_weekly_report_done", length=len(report))
            except Exception as e:
                logger.warning(f"eval_pipeline_weekly_report_error: {e}")
        scheduler.add_job(
            _run_weekly_quality_report,
            CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="UTC"),
            id="eval_weekly_report",
        )
        # task_logs GC: 매일 03:30 UTC — 7일 이상 된 로그 삭제
        async def _run_task_logs_gc():
            try:
                from app.services.task_logger import gc_old_logs
                await gc_old_logs(7)
            except Exception as e:
                logger.warning(f"task_logs_gc_error: {e}")
        scheduler.add_job(_run_task_logs_gc, CronTrigger(hour=3, minute=30, timezone="UTC"), id="task_logs_gc")
        # P5: 주간 품질 분석 -- 매주 월요일 09:30 KST (= UTC 00:30)
        async def _run_weekly_quality_analysis():
            try:
                from app.services.self_evaluator import weekly_quality_analysis
                from app.core.db_pool import get_pool
                await weekly_quality_analysis(get_pool())
                logger.info("weekly_quality_analysis_done")
            except Exception as e:
                logger.warning(f"weekly_quality_analysis_error: {e}")
        scheduler.add_job(
            _run_weekly_quality_analysis,
            CronTrigger(day_of_week="mon", hour=0, minute=30, timezone="UTC"),
            id="weekly_quality_analysis"
        )
        # Auto-Fix Dispatcher: 5분마다 error_log 스캔 → Pipeline Runner 자동 수정 작업 제출
        async def _run_auto_fix():
            try:
                from app.services.auto_fix_dispatcher import scan_and_dispatch
                result = await scan_and_dispatch()
                if result.get("dispatched", 0) > 0:
                    logger.info(f"auto_fix_dispatched: {result}")
            except Exception as e:
                logger.warning(f"auto_fix_error: {e}")
        scheduler.add_job(_run_auto_fix, 'interval', minutes=5, id='auto_fix_dispatcher')

        scheduler.start()
        app.state.scheduler = scheduler  # fallback: MCP 도구 경로에서 참조 가능
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
    # ★ Pipeline Runner 복구보다 먼저 초기화해야 DB 조회 가능
    try:
        from app.core.db_pool import init_pool
        db_pool = await init_pool()
        app_state["db_pool"] = db_pool
        # B3: tool_results_archive is_error 컬럼 마이그레이션
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "ALTER TABLE tool_results_archive ADD COLUMN IF NOT EXISTS is_error BOOLEAN DEFAULT FALSE"
                )
            logger.info("b3_is_error_column_ensured")
        except Exception as e:
            logger.warning("b3_is_error_column_migration_failed", error=str(e))
    except Exception as e:
        logger.error("db_pool_init_failed", error=str(e))
        app_state["db_pool"] = None

    # 서버 시작 시 stale placeholder 즉시 정리 — 경고 메시지 추가 후 intent 변경
    # resume_interrupted_streams 제거: resume가 새 placeholder를 만들어 문제 재발시킴
    try:
        async with db_pool.acquire() as _c:
            _cleaned = await _c.fetchval(
                "WITH d AS (UPDATE chat_messages SET intent = 'bg_partial', "
                "content = content || E'\\n\\n⚠️ _서버 재시작으로 응답이 중단되었습니다. 다시 질문해주세요._' "
                "WHERE intent = 'streaming_placeholder' RETURNING id) SELECT COUNT(*) FROM d"
            )
            if _cleaned and _cleaned > 0:
                logger.info(f"startup_placeholder_cleanup: {_cleaned} stale placeholder(s) → bg_partial")
    except Exception as _e:
        logger.warning(f"startup_placeholder_cleanup_failed: {_e}")

    # 주기적 stale placeholder 자동 정리 (2분마다, 2분 초과분)
    async def _periodic_placeholder_cleanup():
        import asyncio as _pc_asyncio
        while True:
            await _pc_asyncio.sleep(120)  # 2분
            try:
                from app.core.db_pool import get_pool as _gp_pc
                _pool = _gp_pc()
                async with _pool.acquire() as _c:
                    _n = await _c.fetchval(
                        "WITH d AS (UPDATE chat_messages SET intent = 'bg_partial', "
                        "content = content || E'\\n\\n⚠️ _응답 생성이 중단되었습니다._' "
                        "WHERE intent = 'streaming_placeholder' AND created_at < NOW() - interval '2 minutes' "
                        "RETURNING id) SELECT COUNT(*) FROM d"
                    )
                    if _n and _n > 0:
                        logger.info(f"periodic_placeholder_cleanup: {_n} stale placeholder(s) → bg_partial")
            except Exception:
                pass

    import asyncio as _startup_asyncio
    _startup_asyncio.create_task(_periodic_placeholder_cleanup())

    # Pipeline Runner: 재시작 복구 + Watchdog 시작 (DB 풀 초기화 이후)
    try:
        from app.services.pipeline_c import recover_interrupted_jobs, start_watchdog
        await recover_interrupted_jobs()
        await start_watchdog(interval=120)
    except Exception as e:
        logger.warning("pipeline_c_init_failed", error=str(e))

    # 누락 임베딩 백필 (memory_facts에서 embedding IS NULL인 항목)
    async def _backfill_missing_embeddings():
        try:
            from app.services.chat_embedding_service import embed_texts
            from app.core.db_pool import get_pool
            import uuid as _uuid
            pool = get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, category, subject FROM memory_facts WHERE embedding IS NULL AND superseded_by IS NULL LIMIT 50"
                )
            if not rows:
                return
            texts = [f"{r['category']}: {r['subject']}" for r in rows]
            embeddings = await embed_texts(texts)
            async with pool.acquire() as conn:
                updated = 0
                for row, emb in zip(rows, embeddings):
                    if emb:
                        await conn.execute("UPDATE memory_facts SET embedding = $1 WHERE id = $2", str(emb), row["id"])
                        updated += 1
                logger.info(f"startup_embedding_backfill: {updated}/{len(rows)} facts embedded")
        except Exception as e:
            logger.warning(f"startup_embedding_backfill_failed: {e}")

    import asyncio as _startup_asyncio
    _startup_asyncio.create_task(_backfill_missing_embeddings())

    # missed sleep-time agent 체크 — 24시간 이상 인사이트 미생성 시 즉시 실행
    async def _check_missed_sleep_time():
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                latest = await conn.fetchval(
                    "SELECT MAX(created_at) FROM memory_facts WHERE category = 'project_insight'"
                )
            from datetime import datetime, timezone
            if not latest or (datetime.now(timezone.utc) - latest).total_seconds() > 86400:
                logger.info("startup_missed_sleep_time: running catch-up consolidation")
                from app.core.memory_gc import sleep_time_consolidation
                await sleep_time_consolidation(get_pool())
                logger.info("startup_missed_sleep_time: done")
        except Exception as e:
            logger.warning(f"startup_missed_sleep_time_failed: {e}")

    _startup_asyncio.create_task(_check_missed_sleep_time())

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

# H-07: CORS middleware — restrict to AADS dashboard origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://aads.newtalk.kr"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# C-01: JWT 인증 미들웨어 — 인증 없는 외부 접근 차단
import app.auth as _auth_mod

# 인증 불필요 경로 (prefix match)
_AUTH_EXEMPT_PREFIXES = (
    "/api/v1/health",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/mcp",
)
# 내부 모니터링 (verify_monitor_key로 별도 인증)
_MONITOR_KEY_PATHS = (
    "/api/v1/context",
    "/api/v1/watchdog",
    "/api/v1/approval",
)


@app.middleware("http")
async def jwt_auth_middleware(request: Request, call_next):
    path = request.url.path

    # 1) 면제 경로
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    # 2) 모니터 키 인증 경로 (별도 인증 체계)
    if any(path.startswith(p) for p in _MONITOR_KEY_PATHS):
        return await call_next(request)

    # 3) OPTIONS (CORS preflight)
    if request.method == "OPTIONS":
        return await call_next(request)

    # 4) JWT Bearer 토큰 검증
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = _auth_mod.verify_token(token)
        if payload:
            request.state.user = payload
            return await call_next(request)

    # 5) X-Monitor-Key 헤더가 있으면 통과 (내부 서비스 간 호출)
    if request.headers.get("x-monitor-key"):
        return await call_next(request)

    # 인증 실패
    return JSONResponse(status_code=401, content={"detail": "인증이 필요합니다. Bearer 토큰을 제공하세요."})

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
app.include_router(task_monitor_router, prefix="/api/v1", tags=["task-monitor"])
app.include_router(qa_router, prefix="/api/v1", tags=["qa"])
app.include_router(chat_v2_router, prefix="/api/v1", tags=["chat-v2"])
app.include_router(image_router, prefix="/api/v1/image", tags=["image"])
app.include_router(fact_check_router, prefix="/api/v1/fact-check", tags=["fact-check"])
app.include_router(pipeline_runner_router, prefix="/api/v1", tags=["pipeline-runner"])
app.include_router(code_review_router)
app.include_router(quality_router, prefix="/api/v1", tags=["quality"])
# 정적 파일 서빙
import pathlib as _pathlib
_static_dir = _pathlib.Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")# AADS-186C: FastAPI-MCP 마운트 (graceful — MCP_ENABLED=false 시 비활성)
setup_mcp(app)