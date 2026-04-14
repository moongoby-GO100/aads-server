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
from app.api.memory_monitor import router as memory_monitor_router
from app.api.pc_agent import router as pc_agent_router
from app.api.kakao_bot import router as kakao_bot_router
from app.api.agenda import router as agenda_router
from app.api.hot_reload import router as hot_reload_router
from app.api.credential_vault import router as credential_vault_router
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

        # Learning Health Monitor: 3시간마다 대화 vs 학습 비율 체크 → 학습 없으면 자동 재스캔
        async def _run_learning_health_check():
            try:
                from app.core.memory_recall import check_learning_health, rescan_recent_conversations
                health = await check_learning_health(hours=6)
                if health.get("action_needed") == "rescan":
                    logger.info("learning_health_rescan_triggered", messages=health["messages"], learnings=health["learnings"])
                    result = await rescan_recent_conversations(hours=6)
                    logger.info("learning_health_rescan_done", scanned=result["scanned"], extracted=result["extracted"])
            except Exception as e:
                logger.warning(f"learning_health_check_error: {e}")
        scheduler.add_job(_run_learning_health_check, 'interval', hours=3, id='learning_health_check')

        # ──────────────────────────────────────────────
        # AUTH-001: 일일 인증 상태 체크 (매일 09:05 KST)
        # ──────────────────────────────────────────────
        async def _auth_daily_check():
            """인증 토큰 유효성 일일 자동 점검 + 텔레그램 보고"""
            import os, httpx, asyncio
            from datetime import datetime
            from zoneinfo import ZoneInfo
            kst = ZoneInfo("Asia/Seoul")
            now_kst = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
            results = []

            # 1) 토큰 환경변수 존재 확인
            token1 = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
            token2 = os.environ.get("ANTHROPIC_AUTH_TOKEN_2", "")
            token_status = []
            if token1:
                token_status.append(f"TOKEN_1: {'✅' if 'sk-ant-oat01-' in token1 else '⚠️형식이상'}")
            else:
                token_status.append("TOKEN_1: ❌없음")
            if token2:
                token_status.append(f"TOKEN_2: {'✅' if 'sk-ant-oat01-' in token2 else '⚠️형식이상'}")
            else:
                token_status.append("TOKEN_2: ❌없음")
            results.extend(token_status)

            # 2) LiteLLM 연결 확인
            litellm_url = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(f"{litellm_url}/health")
                    results.append(f"LiteLLM: {'✅OK' if r.status_code == 200 else f'⚠️{r.status_code}'}")
            except Exception as e:
                results.append(f"LiteLLM: ❌{str(e)[:30]}")

            # 3) 텔레그램 보고
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            msg = f"🔐 [AADS 일일 인증 체크] {now_kst}\n" + "\n".join(f"  {r}" for r in results)
            logger.info(f"auth_daily_check: {results}")
            if bot_token and chat_id:
                try:
                    async with httpx.AsyncClient(timeout=10) as c:
                        await c.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg}
                        )
                except Exception as e:
                    logger.warning(f"auth_daily_check telegram failed: {e}")

        scheduler.add_job(_auth_daily_check, "cron", hour=9, minute=5, timezone="Asia/Seoul", id="auth_daily_check", replace_existing=True)

        # AADS-191: Pipeline Jobs 자동 정리 (1시간 주기)
        async def _run_pipeline_cleanup():
            try:
                from app.services.pipeline_cleanup import run_pipeline_cleanup
                await run_pipeline_cleanup()
            except Exception as e:
                logger.warning(f"pipeline_cleanup failed: {e}")
        scheduler.add_job(_run_pipeline_cleanup, "interval", hours=1, id="pipeline_cleanup", replace_existing=True)

        # AADS-241: awaiting_approval 자동 notify 폴러 (60초 주기)
        # 211서버 러너의 NOTIFY_AI http=fail 보정 — 채팅 AI가 반드시 검수 트리거되도록
        async def _trigger_pending_approvals():
            try:
                pool = app_state.get("db_pool")
                if not pool:
                    return
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT job_id FROM pipeline_jobs "
                        "WHERE status='awaiting_approval' "
                        "AND chat_session_id IS NOT NULL "
                        "AND updated_at < NOW() - INTERVAL '90 seconds' "
                        "ORDER BY updated_at ASC LIMIT 5"
                    )
                for row in rows:
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"http://localhost:8080/api/v1/pipeline/jobs/{row['job_id']}/notify",
                                headers={"x-monitor-key": "internal-pipeline-call"},
                            )
                    except Exception as _ne:
                        logger.debug(f"pending_approval_notify_skip job={row['job_id']}: {_ne}")
            except Exception as e:
                logger.warning(f"pending_approval_trigger_failed: {e}")
        scheduler.add_job(_trigger_pending_approvals, "interval", seconds=60, id="pending_approval_trigger", replace_existing=True)

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
        # ── 스키마 자동 검증 + 자동 마이그레이션 ──
        try:
            async with db_pool.acquire() as conn:
                # chat_messages 필수 컬럼 자동 생성
                _auto_columns = [
                    ("chat_messages", "branch_id", "UUID DEFAULT NULL"),
                    ("chat_messages", "intent", "TEXT DEFAULT NULL"),
                    ("tool_results_archive", "is_error", "BOOLEAN DEFAULT FALSE"),
                ]
                for _tbl, _col, _type in _auto_columns:
                    await conn.execute(
                        f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS {_col} {_type}"
                    )
                # INSERT 기능 테스트
                _test_ok = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='chat_messages' AND column_name='content')"
                )
                if not _test_ok:
                    logger.error("startup_schema_validation_FAILED: chat_messages.content missing")
                else:
                    logger.info("startup_schema_validation_ok")
        except Exception as e:
            logger.error("startup_schema_migration_failed", error=str(e))

        # ── 필수 환경변수 검증 ──
        _env_warnings = []
        _budget = float(os.environ.get("AGENT_SDK_MAX_BUDGET_USD", "10"))
        if _budget <= 0:
            _env_warnings.append(f"AGENT_SDK_MAX_BUDGET_USD={_budget} (must be >0, defaulting to 10)")
            os.environ["AGENT_SDK_MAX_BUDGET_USD"] = "10"
        if not os.environ.get("JWT_SECRET_KEY"):
            _env_warnings.append("JWT_SECRET_KEY not set")
        if not os.environ.get("DATABASE_URL"):
            _env_warnings.append("DATABASE_URL not set")
        if _env_warnings:
            logger.warning("startup_env_warnings", warnings=_env_warnings)
        else:
            logger.info("startup_env_validation_ok")
    except Exception as e:
        logger.error("db_pool_init_failed", error=str(e))
        app_state["db_pool"] = None

    # Autonomy Gate 스키마 초기화 (T-009)
    try:
        from app.services.autonomy_gate import init_autonomy_schema
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await init_autonomy_schema(conn)
        logger.info("autonomy_gate_schema_initialized")
    except Exception as e:
        logger.warning(f"autonomy_gate_init_failed: {e}")

    # WoL: 네트워크 정보 테이블 사전 생성 (DB pool 초기화 후)
    try:
        from app.services.wol_service import ensure_network_table
        await ensure_network_table()
        logger.info("wol_network_table_ensured")
    except Exception as e:
        logger.warning("wol_network_table_ensure_failed", error=str(e))

    # 서버 시작 시 stale placeholder → 내용 있으면 보존(promote), 없으면 삭제
    try:
        async with db_pool.acquire() as _c:
            # ── startup placeholder cleanup (중복 버블 방지 강화) ──
            _placeholders = await _c.fetch(
                "SELECT id, session_id, content FROM chat_messages WHERE intent = 'streaming_placeholder'"
            )
            _promoted = 0
            _cleaned = 0
            for _ph in _placeholders:
                _ph_content = (_ph["content"] or "").strip()
                if _ph_content:
                    # 동일 세션에 placeholder 이후 최종 응답이 이미 있으면 삭제 (중복 방지)
                    _has_final = await _c.fetchval(
                        "SELECT count(*) FROM chat_messages WHERE session_id = $1 AND role = 'assistant' "
                        "AND intent IS DISTINCT FROM 'streaming_placeholder' "
                        "AND created_at >= (SELECT created_at FROM chat_messages WHERE id = $2)",
                        _ph["session_id"], _ph["id"],
                    )
                    if _has_final and _has_final > 0:
                        await _c.execute("DELETE FROM chat_messages WHERE id = $1", _ph["id"])
                        _cleaned += 1
                    else:
                        import re as _re
                        _clean_content = _re.sub(r'\n*⏳ _(?:생성 중|AI가 응답을 생성 중).*?_\s*$', '', _ph_content).rstrip()
                        if _clean_content:
                            # Stage 2: promote 전 동일 내용 recovered 중복 검사 (앞 50자 비교)
                            _prefix = _clean_content[:50]
                            _dup_recovered = await _c.fetchval(
                                "SELECT count(*) FROM chat_messages WHERE session_id = $1 AND role = 'assistant' "
                                "AND model_used = 'recovered' AND LEFT(content, 50) = $2 AND id != $3",
                                _ph["session_id"], _prefix, _ph["id"],
                            )
                            if _dup_recovered and _dup_recovered > 0:
                                await _c.execute("DELETE FROM chat_messages WHERE id = $1", _ph["id"])
                                _cleaned += 1
                            else:
                                await _c.execute(
                                    "UPDATE chat_messages SET content = $2, intent = NULL, model_used = 'recovered' WHERE id = $1",
                                    _ph["id"], _clean_content,
                                )
                                _promoted += 1
                        else:
                            await _c.execute("DELETE FROM chat_messages WHERE id = $1", _ph["id"])
                            _cleaned += 1
                else:
                    await _c.execute("DELETE FROM chat_messages WHERE id = $1", _ph["id"])
                    _cleaned += 1
            if (_promoted and _promoted > 0) or (_cleaned and _cleaned > 0):
                logger.info(f"startup_placeholder_cleanup: promoted={_promoted or 0} deleted={_cleaned or 0}")
                await _c.execute(
                    "UPDATE chat_sessions s SET message_count = "
                    "(SELECT count(*) FROM chat_messages m WHERE m.session_id = s.id)"
                )
    except Exception as _e:
        logger.warning(f"startup_placeholder_cleanup_failed: {_e}")

    # 주기적 stale placeholder 처리 (15초마다, 1분 초과분 — 중복 버블 방지 강화)
    async def _periodic_placeholder_cleanup():
        import asyncio as _pc_asyncio
        while True:
            await _pc_asyncio.sleep(15)  # 15초
            try:
                from app.core.db_pool import get_pool as _gp_pc
                from app.services.chat_service import _streaming_state
                _pool = _gp_pc()
                async with _pool.acquire() as _c:
                    # 현재 스트리밍 중인 세션은 제외
                    _active_sids = [k for k, v in _streaming_state.items() if not v.get("completed")]
                    # 1분 초과 + 스트리밍 아닌 placeholder만 대상 (2분→1분 단축: 고아 placeholder 빠른 정리)
                    _stale = await _c.fetch(
                        "SELECT id, session_id, content FROM chat_messages "
                        "WHERE intent = 'streaming_placeholder' AND created_at < NOW() - interval '1 minute'"
                    )
                    _promoted = 0
                    _deleted = 0
                    for row in _stale:
                        _sid_str = str(row["session_id"])
                        if _sid_str in _active_sids:
                            continue  # 아직 스트리밍 중 → 건드리지 않음
                        content = row["content"] or ""
                        if content.strip():
                            # 최종 응답이 이미 있으면 중복 방지를 위해 삭제
                            _has_final = await _c.fetchval(
                                "SELECT count(*) FROM chat_messages WHERE session_id = $1 AND role = 'assistant' "
                                "AND intent IS DISTINCT FROM 'streaming_placeholder' "
                                "AND created_at >= (SELECT created_at FROM chat_messages WHERE id = $2)",
                                row["session_id"], row["id"],
                            )
                            if _has_final and _has_final > 0:
                                await _c.execute("DELETE FROM chat_messages WHERE id = $1", row["id"])
                                _deleted += 1
                            else:
                                import re as _re
                                _clean = _re.sub(r'\n*⏳ _(?:생성 중|AI가 응답을 생성 중).*?_\s*$', '', content).rstrip()
                                if _clean:
                                    # Stage 2: promote 전 동일 내용 recovered 중복 검사
                                    _prefix = _clean[:50]
                                    _dup_rec = await _c.fetchval(
                                        "SELECT count(*) FROM chat_messages WHERE session_id = $1 AND role = 'assistant' "
                                        "AND model_used = 'recovered' AND LEFT(content, 50) = $2 AND id != $3",
                                        row["session_id"], _prefix, row["id"],
                                    )
                                    if _dup_rec and _dup_rec > 0:
                                        await _c.execute("DELETE FROM chat_messages WHERE id = $1", row["id"])
                                        _deleted += 1
                                    else:
                                        await _c.execute(
                                            "UPDATE chat_messages SET content = $2, intent = NULL, model_used = 'recovered' WHERE id = $1",
                                            row["id"], _clean,
                                        )
                                        _promoted += 1
                                else:
                                    await _c.execute("DELETE FROM chat_messages WHERE id = $1", row["id"])
                                    _deleted += 1
                        else:
                            await _c.execute("DELETE FROM chat_messages WHERE id = $1", row["id"])
                            _deleted += 1
                    if _promoted or _deleted:
                        logger.info(f"periodic_placeholder_cleanup: promoted={_promoted} deleted={_deleted}")
                        await _c.execute(
                            "UPDATE chat_sessions s SET message_count = "
                            "(SELECT count(*) FROM chat_messages m WHERE m.session_id = s.id)"
                        )
            except Exception:
                pass

    import asyncio as _startup_asyncio
    _startup_asyncio.create_task(_periodic_placeholder_cleanup())

    # 서버 재시작 후 미완료 대화 자동 재실행
    async def _resume_incomplete_conversations():
        """서버 재시작 시 마지막 메시지가 user인 최근 세션을 감지하여 자동 재실행.

        조건:
        1. 최근 10분 이내 사용자 메시지가 마지막인 세션
        2. 배포 목적 재시작 구분: /tmp/aads_deploy_restart 플래그 파일이 있으면 스킵
        """
        import asyncio as _resume_asyncio
        await _resume_asyncio.sleep(5)  # 서버 완전 기동 대기

        _deploy_flag = "/tmp/aads_deploy_restart"
        _is_deploy_restart = False
        if os.path.exists(_deploy_flag):
            os.remove(_deploy_flag)
            _is_deploy_restart = True
            logger.info("resume_incomplete: deploy restart detected — recovered 세션만 이어쓰기")

        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                # 최근 10분 이내, 미완료 세션 찾기:
                # 1) 마지막 메시지가 user인 세션 (응답 없음) — 배포 재시작 시 스킵
                # 2) 마지막 assistant가 recovered인 세션 (중단된 부분 응답) — 항상 이어쓰기
                _case1 = "" if not _is_deploy_restart else "FALSE AND"
                incomplete = await conn.fetch("""
                    WITH last_msgs AS (
                        SELECT DISTINCT ON (session_id)
                            session_id, role, content, created_at,
                            intent, model_used
                        FROM chat_messages
                        WHERE created_at > NOW() - INTERVAL '10 minutes'
                          AND intent IS DISTINCT FROM 'system_trigger'
                          AND intent IS DISTINCT FROM 'streaming_placeholder'
                        ORDER BY session_id, created_at DESC
                    )
                    SELECT lm.session_id::text,
                           COALESCE(
                               (SELECT content FROM chat_messages
                                WHERE session_id = lm.session_id AND role = 'user'
                                ORDER BY created_at DESC LIMIT 1),
                               lm.content
                           ) AS content,
                           lm.created_at,
                           lm.model_used
                    FROM last_msgs lm
                    WHERE (
                        ({case1} lm.role = 'user' AND lm.content IS NOT NULL AND length(lm.content) > 0)
                        OR
                        (lm.role = 'assistant' AND lm.model_used = 'recovered')
                    )
                    LIMIT 3
                """.format(case1=_case1))

                if not incomplete:
                    logger.info("resume_incomplete: no incomplete conversations found")
                    return

                logger.info(f"resume_incomplete: found {len(incomplete)} incomplete conversation(s)")

                for row in incomplete:
                    sid = row["session_id"]
                    user_content = row["content"]
                    is_recovered = row.get("model_used") == "recovered"
                    try:
                        from app.services.chat_service import send_message_stream, with_background_completion
                        import asyncio as _ri_asyncio

                        # recovered 세션: 마지막 user 메시지 + 이어쓰기 지시
                        if is_recovered:
                            _last_user = await conn.fetchval(
                                "SELECT content FROM chat_messages "
                                "WHERE session_id = $1::uuid AND role = 'user' "
                                "ORDER BY created_at DESC LIMIT 1",
                                sid,
                            )
                            if _last_user:
                                user_content = (
                                    f"{_last_user}\n\n"
                                    "[시스템] 이전 응답이 서버 재시작으로 중단되었습니다. "
                                    "이전 응답 내용을 참고하여 이어서 완성해 주세요."
                                )
                            else:
                                logger.info(f"resume_incomplete: session={sid[:8]} recovered but no user msg, skip")
                                continue

                        async def _resume_session(_sid, _content):
                            try:
                                stream = send_message_stream(
                                    session_id=_sid,
                                    content=_content,
                                )
                                bg = with_background_completion(stream, session_id=_sid)
                                async for _ in bg:
                                    pass
                                logger.info(f"resume_incomplete: session={_sid[:8]} completed")
                            except Exception as _e:
                                logger.warning(f"resume_incomplete: session={_sid[:8]} stream_error={_e}")

                        _ri_asyncio.create_task(_resume_session(sid, user_content))
                        _mode = "recovered-resume" if is_recovered else "direct"
                        logger.info(f"resume_incomplete: session={sid[:8]} re-triggered ({_mode})")
                    except Exception as e:
                        logger.warning(f"resume_incomplete: session={sid[:8]} error={e}")
        except Exception as e:
            logger.warning(f"resume_incomplete_failed: {e}")

    _startup_asyncio.create_task(_resume_incomplete_conversations())

    # ── 주기적 recovered 세션 자동이어쓰기 스캐너 (30초 주기) ──
    _recovered_resume_attempts: dict[str, int] = {}  # sid → 재시도 횟수

    async def _periodic_recovered_scanner():
        """서버 재시작 없이도 recovered 세션을 감지하여 자동 이어쓰기."""
        import asyncio as _prs_asyncio
        await _prs_asyncio.sleep(15)  # 초기 대기
        while True:
            try:
                await _prs_asyncio.sleep(30)
                from app.core.db_pool import get_pool as _gp_prs
                from app.services.chat_service import _streaming_state
                _pool = _gp_prs()
                async with _pool.acquire() as conn:
                    rows = await conn.fetch("""
                        WITH last_msgs AS (
                            SELECT DISTINCT ON (session_id)
                                session_id, role, model_used, created_at
                            FROM chat_messages
                            WHERE created_at > NOW() - INTERVAL '10 minutes'
                              AND intent IS DISTINCT FROM 'system_trigger'
                              AND intent IS DISTINCT FROM 'streaming_placeholder'
                            ORDER BY session_id, created_at DESC
                        )
                        SELECT lm.session_id::text
                        FROM last_msgs lm
                        WHERE lm.role = 'assistant' AND lm.model_used = 'recovered'
                        LIMIT 5
                    """)
                    if not rows:
                        continue

                    for row in rows:
                        sid = row["session_id"]
                        # 이미 스트리밍 중이면 스킵
                        if sid in _streaming_state and not _streaming_state[sid].get("completed"):
                            continue
                        # 재시도 횟수 초과(최대 2회) 스킵
                        if _recovered_resume_attempts.get(sid, 0) >= 2:
                            continue

                        # 중복 방지: 이미 이 세션에 백그라운드 작업 진행 중이면 스킵
                        from app.services.chat_service import _active_bg_tasks
                        if sid in _active_bg_tasks and not _active_bg_tasks[sid].done():
                            logger.info(f"recovered_scanner: session={sid[:8]} bg_task active, skip")
                            continue

                        _recovered_resume_attempts[sid] = _recovered_resume_attempts.get(sid, 0) + 1

                        # 마지막 user 메시지 조회
                        # 마지막 user 메시지 조회
                        _last_user = await conn.fetchval(
                            "SELECT content FROM chat_messages "
                            "WHERE session_id = $1::uuid AND role = 'user' "
                            "ORDER BY created_at DESC LIMIT 1",
                            sid,
                        )
                        if not _last_user:
                            continue

                        # P0-1: 이미 복구 메시지가 붙어있으면 루프 방지
                        if "[시스템] 이전 응답이 중단되었습니다" in _last_user:
                            logger.info(f"recovered_scanner: session={sid[:8]} already has recovery suffix, skip loop")
                            continue

                        _recovery_suffix = "[시스템] 이전 응답이 중단되었습니다. 이전 응답 내용을 참고하여 이어서 완성해 주세요."
                        user_content = f"{_last_user}\n\n{_recovery_suffix}"

                        # P0-2: idempotency_key로 중복 삽입 방지
                        import hashlib as _hs
                        _idem_key = "recovery_" + _hs.md5(f"{sid}:{_last_user[:100]}".encode()).hexdigest()

                        from app.services.chat_service import send_message_stream, with_background_completion

                        async def _resume_recovered(_sid, _content, _ikey=_idem_key):
                            try:
                                stream = send_message_stream(session_id=_sid, content=_content, idempotency_key=_ikey)
                                bg = with_background_completion(stream, session_id=_sid)
                                async for _ in bg:
                                    pass
                                logger.info(f"recovered_scanner: session={_sid[:8]} resumed OK")
                            except Exception as _e:
                                logger.warning(f"recovered_scanner: session={_sid[:8]} error={_e}")

                        _prs_asyncio.create_task(_resume_recovered(sid, user_content))
                        logger.info(f"recovered_scanner: session={sid[:8]} auto-resume triggered (attempt {_recovered_resume_attempts[sid]})")
            except Exception as _e:
                logger.warning(f"recovered_scanner_error: {_e}")

    _startup_asyncio.create_task(_periodic_recovered_scanner())

    # Pipeline Runner: 재시작 복구 + Watchdog 시작 (DB 풀 초기화 이후)
    try:
        from app.services.pipeline_runner_service import recover_interrupted_jobs, start_watchdog
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

    # KakaoBot SaaS 스케줄러 시작
    try:
        from app.services.kakaobot_scheduler import start_scheduler_tasks
        start_scheduler_tasks()
        logger.info("kakaobot_scheduler_started")
    except Exception as e:
        logger.warning(f"kakaobot_scheduler_start_failed: {e}")

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
    allow_origins=["https://aads.newtalk.kr", "https://kakaobot.newtalk.kr"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# C-01: JWT 인증 미들웨어 — 인증 없는 외부 접근 차단
import app.auth as _auth_mod

# 인증 불필요 경로 (prefix match)
_AUTH_EXEMPT_PREFIXES = (
    "/health",
    "/api/v1/health",
    "/api/v1/ops/health-check",  # 운영 헬스체크 (정정: -check 포함)
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/me",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/mcp",
    "/api/v1/pc-agent",
    "/api/v1/review",
    "/api/v1/kakao-bot/msgbot/webhook",
    "/api/v1/kakao-bot/respond",
    "/api/v1/kakao-bot/agent",
    "/api/v1/ops/hot-reload",  # 내부 hot-reload (127.0.0.1 전용)
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
app.include_router(memory_monitor_router, prefix="/api/v1", tags=["memory-monitor"])
app.include_router(pc_agent_router, prefix="/api/v1", tags=["pc-agent"])
app.include_router(kakao_bot_router, prefix="/api/v1", tags=["kakao-bot"])
app.include_router(agenda_router, prefix="/api/v1/agenda", tags=["agenda"])
app.include_router(hot_reload_router, prefix="/api/v1", tags=["hot-reload"])
app.include_router(credential_vault_router, prefix="/api/v1", tags=["credential-vault"])

# 루트 /health — 모니터링 도구 호환 (인증 면제)
from fastapi.responses import JSONResponse as _JSONResponse

@app.get("/health", tags=["health"], include_in_schema=False)
async def root_health_check():
    """루트 /health — /api/v1/health 와 동일한 응답. 인증 불필요."""
    from app.main import app_state
    from app.services.sandbox import check_sandbox_health
    graph_ready = app_state.get("graph") is not None
    sandbox_health = await check_sandbox_health()
    return _JSONResponse({
        "status": "ok" if graph_ready else "initializing",
        "graph_ready": graph_ready,
        "version": "0.1.0",
        "sandbox": sandbox_health,
    })# 정적 파일 서빙
import pathlib as _pathlib
_static_dir = _pathlib.Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")# AADS-186C: FastAPI-MCP 마운트 (graceful — MCP_ENABLED=false 시 비활성)
setup_mcp(app)
