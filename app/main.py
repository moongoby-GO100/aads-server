"""
AADS FastAPI 서버.
lifespan으로 그래프 컴파일 + checkpointer + MCP 초기화.
"""
from contextlib import asynccontextmanager
import os

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from app.logging_config import configure_logging

from app.api import health, projects, checkpoints, stream, auth, context, chat, visual_qa, mobile_qa, memory, terminal, browser_bridge, design_modifications, google_sheets, yeoljeong_finance, notifications
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
from app.api.governance import router as governance_router
from app.api.ops import router as ops_router
from app.api.admin import router as admin_router
from app.api.admin_users import router as admin_users_router
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
from app.api.pc_ollama_bridge import router as pc_ollama_bridge_router
from app.api.voice import router as voice_router
from app.api.local_models import router as local_models_router
from app.api.local_media_router import router as local_media_router
from app.api.device import router as device_router
from app.api.kakao_bot import router as kakao_bot_router
from app.api.agenda import router as agenda_router
from app.api.assistant import router as assistant_router
from app.api.hot_reload import router as hot_reload_router
from app.api.credential_vault import router as credential_vault_router
from app.api.llm_keys import router as llm_keys_router
from app.api.llm_models import router as llm_models_router
from app.api.braming import router as braming_router
from app.api.project_docs import router as project_docs_router
from app.api.files import router as files_router
from app.api.external_chat import router as external_chat_router
from app.api.ohvis_tasks import router as ohvis_tasks_router
from app.api.loops import router as loops_router
from app.api.browser_tasks import router as browser_tasks_router
from app.routers.chat import router as chat_v2_router
from app.routers.agent_vault import router as agent_vault_router
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

        async def _run_periodic_model_registry_sync():
            try:
                from app.services.model_registry import sync_model_registry

                result = await sync_model_registry(
                    triggered_by="scheduler",
                    reason="periodic_refresh",
                )
                if result.get("ok"):
                    logger.info(
                        "model_registry_periodic_sync_done",
                        models_synced=result.get("models_synced", 0),
                        normalized_providers=result.get("normalized_providers", {}),
                        review_required_providers=result.get("review_required_providers", []),
                    )
                else:
                    logger.warning(
                        "model_registry_periodic_sync_failed",
                        error=result.get("error", "unknown"),
                    )
            except Exception as e:
                logger.warning("model_registry_periodic_sync_failed", error=str(e))

        async def _run_rate_limit_recovery():
            """만료된 rate_limited_until 자동 클리어 + 비활성 키 재활성화 + 모델 재활성화."""
            try:
                from app.core.db_pool import get_pool
                pool = get_pool()
                needs_sync = False
                async with pool.acquire() as conn:
                    cleared = await conn.fetch(
                        """
                        UPDATE llm_api_keys
                        SET rate_limited_until = NULL, updated_at = NOW()
                        WHERE rate_limited_until IS NOT NULL
                          AND rate_limited_until <= NOW()
                        RETURNING id, provider, key_name
                        """
                    )
                    if cleared:
                        key_names = [r["key_name"] for r in cleared]
                        logger.info("rate_limit_recovery: cleared %d expired rate limits: %s", len(cleared), key_names)
                        needs_sync = True

                    reactivated = await conn.fetch(
                        """
                        UPDATE llm_api_keys
                        SET is_active = TRUE, updated_at = NOW()
                        WHERE is_active = FALSE
                          AND (rate_limited_until IS NULL OR rate_limited_until <= NOW())
                          AND encrypted_value IS NOT NULL
                          AND key_name NOT IN ('OPENAI_API_KEY')
                        RETURNING id, provider, key_name
                        """
                    )
                    if reactivated:
                        reactivated_names = [r["key_name"] for r in reactivated]
                        logger.info("rate_limit_recovery: reactivated %d keys: %s", len(reactivated), reactivated_names)
                        needs_sync = True

                    stale_models = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM llm_models m
                        WHERE m.provider = 'anthropic'
                          AND m.is_active = FALSE
                          AND m.discovery_source = 'template'
                          AND EXISTS (
                            SELECT 1 FROM llm_api_keys k
                            WHERE k.provider = 'anthropic'
                              AND k.is_active = TRUE
                              AND (k.rate_limited_until IS NULL OR k.rate_limited_until <= NOW())
                          )
                        """
                    )
                    if stale_models and stale_models > 0:
                        logger.info("rate_limit_recovery: %d stale anthropic template models detected, forcing sync", stale_models)
                        needs_sync = True

                if needs_sync:
                    from app.services.model_registry import invalidate_registry_cache, sync_model_registry
                    from app.core.llm_key_provider import invalidate_key_cache
                    invalidate_key_cache()
                    invalidate_registry_cache()
                    await sync_model_registry(
                        triggered_by="rate_limit_recovery",
                        reason="expired_keys_or_stale_models",
                    )
            except Exception as e:
                logger.warning("rate_limit_recovery_failed: %s", str(e)[:200])

        async def _run_stale_execution_cleanup():
            """Auto-retry or settle stale running executions that block sessions."""
            try:
                from app.core.db_pool import get_pool as _sep
                from app.services.chat_service import (
                    _resume_single_stream as _watchdog_resume_single_stream,
                    get_active_bg_tasks as _get_active_bg_tasks,
                    _has_meaningful_partial_content as _has_meaningful_partial_content_watchdog,
                    _strip_streaming_progress_markers as _strip_streaming_progress_markers_watchdog,
                )
                import asyncio as _watchdog_asyncio

                _pool = _sep()
                _active_sids = {
                    sid for sid, is_active in (_get_active_bg_tasks() or {}).items()
                    if is_active
                }
                async with _pool.acquire() as conn:
                    candidates = await conn.fetch(
                        """
                        SELECT te.id,
                               te.session_id,
                               te.requested_model,
                               te.retry_count,
                               te.error_message,
                               COALESCE(ph.id, te.assistant_message_id) AS assistant_message_id,
                               COALESCE(ph.content, am.content, '') AS partial_content,
                               COALESCE(um.content, '') AS last_user_msg,
                               pj.job_id AS pipeline_job_id,
                               pj.status AS pipeline_job_status,
                               w.name AS workspace_name
                        FROM chat_turn_executions te
                        JOIN chat_sessions s
                          ON s.id = te.session_id
                        JOIN chat_workspaces w
                          ON w.id = s.workspace_id
                        LEFT JOIN chat_messages am
                          ON am.id = te.assistant_message_id
                        LEFT JOIN chat_messages um
                          ON um.id = te.user_message_id
                        LEFT JOIN LATERAL (
                            SELECT job_id, status
                            FROM pipeline_jobs
                            WHERE COALESCE(um.content, '') LIKE '%' || job_id || '%'
                            ORDER BY updated_at DESC
                            LIMIT 1
                        ) pj ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT id, content
                            FROM chat_messages
                            WHERE execution_id = te.id
                              AND intent = 'streaming_placeholder'
                            ORDER BY created_at DESC
                            LIMIT 1
                        ) ph ON TRUE
                        WHERE te.status IN ('running', 'retrying')
                          AND (
                            (
                              COALESCE(te.last_event_id, '') = ''
                              AND te.started_at < NOW() - INTERVAL '20 minutes'
                              AND te.updated_at < NOW() - INTERVAL '10 minutes'
                            )
                            OR (
                              COALESCE(te.last_event_id, '') <> ''
                              AND te.started_at < NOW() - INTERVAL '45 minutes'
                              AND te.updated_at < NOW() - INTERVAL '20 minutes'
                            )
                            OR (
                              te.started_at < NOW() - INTERVAL '30 minutes'
                              AND COALESCE(te.actual_model, '') = ''
                            )
                            OR (
                              te.started_at < NOW() - INTERVAL '90 minutes'
                            )
                          )
                        """
                    )
                    _claimable = [
                        r for r in candidates
                        if str(r["session_id"]) not in _active_sids
                    ]
                    def _should_settle_without_retry(row) -> bool:
                        """Do not replay abandoned turns that already preserved useful output."""
                        _err = str(row["error_message"] or "")
                        _user_msg = str(row["last_user_msg"] or "")
                        _pipeline_status = str(row["pipeline_job_status"] or "")
                        _clean = _strip_streaming_progress_markers_watchdog(row["partial_content"] or "")
                        _terminal_pipeline_review = (
                            _user_msg.startswith("[시스템] Pipeline Runner 작업 AI 검수 요청")
                            and _pipeline_status
                            and _pipeline_status not in {
                                "queued",
                                "claimed",
                                "running",
                                "awaiting_approval",
                                "approved",
                                "deploying",
                                "restarting",
                            }
                        )
                        return (
                            (
                                "missing_done_event" in _err
                                and "client_gone=True" in _err
                                and _has_meaningful_partial_content_watchdog(_clean)
                            )
                            or _terminal_pipeline_review
                        )

                    _watchdog_retry_enabled = os.getenv("AADS_WATCHDOG_AUTO_RETRY", "0") == "1"
                    _retry_rows = [
                        r for r in _claimable
                        if (
                            _watchdog_retry_enabled
                            and (r["retry_count"] or 0) < 2
                            and (r["last_user_msg"] or "").strip()
                            and not _should_settle_without_retry(r)
                        )
                    ]
                    _retry_ids = [r["id"] for r in _retry_rows]
                    _retry_claimed = []
                    if _retry_ids:
                        _retry_claimed = await conn.fetch(
                            """
                            UPDATE chat_turn_executions
                            SET status = 'retrying',
                                retry_count = retry_count + 1,
                                completed_at = NULL,
                                updated_at = NOW(),
                                error_message = 'watchdog_auto_retry_scheduled policy=20m+10m_or_45m+20m'
                            WHERE id = ANY($1::uuid[])
                              AND status IN ('running', 'retrying')
                              AND (
                                (
                                  COALESCE(last_event_id, '') = ''
                                  AND started_at < NOW() - INTERVAL '20 minutes'
                                  AND updated_at < NOW() - INTERVAL '10 minutes'
                                )
                                OR (
                                  COALESCE(last_event_id, '') <> ''
                                  AND started_at < NOW() - INTERVAL '45 minutes'
                                  AND updated_at < NOW() - INTERVAL '20 minutes'
                                )
                                OR (
                                  started_at < NOW() - INTERVAL '30 minutes'
                                  AND COALESCE(actual_model, '') = ''
                                )
                                OR (
                                  started_at < NOW() - INTERVAL '90 minutes'
                                )
                              )
                            RETURNING id
                            """,
                            _retry_ids,
                        )
                    _claimed_ids = {r["id"] for r in _retry_claimed}
                    for row in _retry_rows:
                        if row["id"] not in _claimed_ids:
                            continue
                        _sid = str(row["session_id"])
                        _eid = str(row["id"])
                        _placeholder_id = row["assistant_message_id"]
                        if not _placeholder_id:
                            _placeholder_id = await conn.fetchval(
                                """
                                INSERT INTO chat_messages (
                                    session_id, execution_id, role, content, intent, model_used, tools_called
                                )
                                VALUES ($1::uuid, $2::uuid, 'assistant', $3, 'streaming_placeholder', 'streaming', '[]'::jsonb)
                                ON CONFLICT (execution_id) WHERE intent = 'streaming_placeholder' AND execution_id IS NOT NULL
                                DO UPDATE SET content = COALESCE(chat_messages.content, EXCLUDED.content), edited_at = NOW()
                                RETURNING id
                                """,
                                _sid,
                                _eid,
                                row["partial_content"] or "",
                            )
                        await conn.execute(
                            """
                            UPDATE chat_turn_executions
                            SET assistant_message_id = $2,
                                updated_at = NOW()
                            WHERE id = $1::uuid
                            """,
                            _eid,
                            _placeholder_id,
                        )
                        await conn.execute(
                            """
                            UPDATE chat_sessions
                            SET current_execution_id = $2,
                                updated_at = NOW()
                            WHERE id = $1
                            """,
                            row["session_id"],
                            row["id"],
                        )
                        _resume_task = _watchdog_asyncio.create_task(
                            _watchdog_resume_single_stream(
                                _sid,
                                _placeholder_id,
                                row["partial_content"] or "",
                                row["last_user_msg"] or "",
                                row["workspace_name"] or "CEO",
                                execution_id=_eid,
                                requested_model=row["requested_model"],
                            )
                        )

                        def _on_watchdog_resume_done(
                            _task,
                            _session_id=_sid,
                            _execution_id=_eid,
                        ):
                            if _task.cancelled():
                                logger.warning(
                                    "watchdog_auto_retry_cancelled session=%s execution=%s",
                                    _session_id[:8],
                                    _execution_id[:8],
                                )
                                return
                            _exc = _task.exception()
                            if _exc:
                                logger.error(
                                    "watchdog_auto_retry_escaped session=%s execution=%s error=%s",
                                    _session_id[:8],
                                    _execution_id[:8],
                                    _exc,
                                )

                        _resume_task.add_done_callback(_on_watchdog_resume_done)
                    _settle_ids = [
                        r["id"] for r in _claimable
                        if r["id"] not in set(_retry_ids)
                    ]
                    if not _settle_ids:
                        if _claimed_ids:
                            logger.info(
                                "stale_execution_watchdog: auto-retry scheduled %d executions",
                                len(_claimed_ids),
                            )
                        return
                    settled = await conn.fetch(
                        """
                        UPDATE chat_turn_executions
                        SET status = 'interrupted',
                            completed_at = COALESCE(completed_at, NOW()),
                            updated_at = NOW(),
                            error_message = COALESCE(error_message, 'auto-settled by stale execution watchdog')
                        WHERE id = ANY($1::uuid[])
                          AND status IN ('running', 'retrying')
                          AND (
                            (
                              COALESCE(last_event_id, '') = ''
                              AND started_at < NOW() - INTERVAL '20 minutes'
                              AND updated_at < NOW() - INTERVAL '10 minutes'
                            )
                            OR (
                              COALESCE(last_event_id, '') <> ''
                              AND started_at < NOW() - INTERVAL '45 minutes'
                              AND updated_at < NOW() - INTERVAL '20 minutes'
                            )
                            OR (
                              started_at < NOW() - INTERVAL '30 minutes'
                              AND COALESCE(actual_model, '') = ''
                            )
                            OR (
                              started_at < NOW() - INTERVAL '90 minutes'
                            )
                          )
                        RETURNING id, session_id
                        """,
                        _settle_ids,
                    )
                    if settled:
                        _sids = list({r["session_id"] for r in settled})
                        _execution_ids = [r["id"] for r in settled]
                        await conn.execute(
                            """
                            UPDATE chat_messages
                            SET content = CASE
                                    WHEN trim(COALESCE(content, '')) = ''
                                        THEN '⚠️ 응답이 중단되었습니다. 다시 시도해 주세요.'
                                    ELSE regexp_replace(
                                        content,
                                        E'\\n*⏳ _(?:생성 중|AI가 응답을 생성 중).*?_\\s*$',
                                        ''
                                    )
                                END,
                                intent = 'interrupted_partial',
                                model_used = 'interrupted',
                                edited_at = NOW()
                            WHERE execution_id = ANY($1::uuid[])
                              AND intent = 'streaming_placeholder'
                            """,
                            _execution_ids,
                        )
                        await conn.execute(
                            """
                            UPDATE chat_sessions
                            SET current_execution_id = NULL, updated_at = NOW()
                            WHERE id = ANY($1::uuid[])
                              AND current_execution_id IS NOT NULL
                            """,
                            _sids,
                        )
                        logger.info(
                            "stale_execution_watchdog: auto-retry scheduled %d, settled %d executions in %d sessions",
                            len(_claimed_ids), len(settled), len(_sids),
                        )
            except Exception as e:
                logger.warning("stale_execution_watchdog_failed: %s", str(e)[:200])

        scheduler = AsyncIOScheduler()
        # 2분마다 규칙 평가
        scheduler.add_job(_run_alert_evaluation, "interval", minutes=2, id="alert_eval")
        # 30초마다 자율복구 사이클
        scheduler.add_job(_run_healing_cycle, "interval", seconds=30, id="healing_cycle")
        # 60초마다 만료된 rate limit 자동 복구 + 모델 재활성화
        scheduler.add_job(_run_rate_limit_recovery, "interval", seconds=60, id="rate_limit_recovery")
        # 90초마다 stale execution 자동 정리 (세션 차단 방지)
        scheduler.add_job(_run_stale_execution_cleanup, "interval", seconds=90, id="stale_execution_watchdog")
        # 최신 LLM catalog 반영 — 기본 6시간 주기
        scheduler.add_job(
            _run_periodic_model_registry_sync,
            "interval",
            hours=max(1, int(os.getenv("LLM_MODEL_REGISTRY_SYNC_HOURS", "6"))),
            id="llm_model_registry_sync",
        )
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
        # Phase 3.5: Project Change Promoter — 중요 변경을 세션 자동인지 메모리로 승격
        async def _run_project_change_promoter():
            try:
                from app.services.project_change_promoter import promote_completed_project_changes
                from app.core.db_pool import get_pool
                result = await promote_completed_project_changes(get_pool(), days=14, limit=30)
                if result.get("inserted", 0) > 0:
                    logger.info("project_change_promoter_done", inserted=result["inserted"], scanned=result["jobs_scanned"])
            except Exception as e:
                logger.warning(f"project_change_promoter_job_error: {e}")
        scheduler.add_job(_run_project_change_promoter, 'interval', minutes=30, id="project_change_promoter")
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

        async def _run_chat_deleted_duplicate_cleanup():
            try:
                from app.services.chat_cleanup_service import cleanup_deleted_duplicate_messages

                result = await cleanup_deleted_duplicate_messages(
                    retention_days=os.getenv("CHAT_DELETED_DUPLICATE_RETENTION_DAYS"),
                    batch_size=os.getenv("CHAT_DELETED_DUPLICATE_CLEANUP_BATCH"),
                    dry_run=os.getenv("CHAT_DELETED_DUPLICATE_CLEANUP_DRY_RUN", "false").lower() == "true",
                )
                if result.get("deleted") or result.get("dry_run"):
                    logger.info("chat_deleted_duplicate_cleanup_result", result=result)
            except Exception as e:
                logger.warning(f"chat_deleted_duplicate_cleanup failed: {e}")
        scheduler.add_job(
            _run_chat_deleted_duplicate_cleanup,
            "interval",
            hours=6,
            id="chat_deleted_duplicate_cleanup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

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
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM jsonb_array_elements(COALESCE(logs, '[]'::jsonb)) AS log "
                        "  WHERE log->>'event' = 'notify_ai' "
                        "  AND log->>'status' = 'awaiting_approval'"
                        ") "
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

        async def _mcp_health_check_job():
            """1분마다 MCP 서버 상태 확인 + 끊긴 서버 자동 복구."""
            try:
                from app.mcp.client import get_mcp_manager, _ping_mcp_server
                from app.mcp.config import get_always_on_connections
                mcp_manager = get_mcp_manager()
                if not mcp_manager:
                    return
                all_connections = get_always_on_connections()
                if not all_connections:
                    return
                failed = [
                    name for name, cfg in all_connections.items()
                    if cfg.get("url") and not await _ping_mcp_server(cfg["url"], timeout=3.0)
                ]
                if failed:
                    logger.warning("mcp_health_check_failed", servers=failed)
                    try:
                        await mcp_manager.initialize()
                        logger.info("mcp_health_check_recovered", available=len(mcp_manager.available_servers))
                    except Exception as e:
                        logger.error("mcp_health_check_recovery_failed", error=str(e))
            except Exception as e:
                logger.warning("mcp_health_check_error", error=str(e))
        scheduler.add_job(_mcp_health_check_job, "interval", minutes=1, id="mcp_health_check", max_instances=1, coalesce=True)

        # OHVIS Loop System: 활성 루프 폴링 (30초 주기)
        async def _run_loop_tick():
            try:
                pool = app_state.get("db_pool")
                if not pool:
                    return
                rows = await pool.fetch(
                    "SELECT id FROM ohvis_loops "
                    "WHERE status = 'active' AND next_run_at IS NOT NULL "
                    "AND next_run_at <= NOW() "
                    "ORDER BY next_run_at ASC LIMIT 3"
                )
                if not rows:
                    return
                from app.services.loop_executor import run_iteration
                for row in rows:
                    try:
                        result = await run_iteration(row["id"])
                        logger.info("loop_tick loop=%d ok=%s", row["id"], result.get("ok"))
                    except Exception as _iter_err:
                        logger.warning("loop_tick_fail loop=%d: %s", row["id"], _iter_err)
            except Exception as e:
                logger.debug("loop_tick_skip: %s", e)
        scheduler.add_job(
            _run_loop_tick, "interval", seconds=30,
            id="loop_tick", replace_existing=True,
            max_instances=1, coalesce=True,
        )

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
                    ("chat_messages", "execution_id", "UUID DEFAULT NULL"),
                    ("chat_messages", "is_hidden", "BOOLEAN NOT NULL DEFAULT FALSE"),
                    ("chat_sessions", "current_execution_id", "UUID DEFAULT NULL"),
                    ("tool_results_archive", "is_error", "BOOLEAN DEFAULT FALSE"),
                    ("tool_results_archive", "result_summary", "TEXT"),
                    ("tool_results_archive", "latency_ms", "INTEGER DEFAULT 0"),
                    ("tool_results_archive", "success", "BOOLEAN"),
                    ("tool_results_archive", "error_detail", "TEXT"),
                ]
                for _tbl, _col, _type in _auto_columns:
                    await conn.execute(
                        f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS {_col} {_type}"
                    )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_turn_executions (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                        user_message_id UUID NULL REFERENCES chat_messages(id) ON DELETE SET NULL,
                        assistant_message_id UUID NULL REFERENCES chat_messages(id) ON DELETE SET NULL,
                        requested_model VARCHAR(100),
                        actual_model VARCHAR(100),
                        status VARCHAR(32) NOT NULL DEFAULT 'running',
                        retry_count INT NOT NULL DEFAULT 0,
                        last_event_id TEXT,
                        error_message TEXT,
                        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        completed_at TIMESTAMPTZ NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_turn_executions_session_created "
                    "ON chat_turn_executions(session_id, created_at DESC)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_turn_executions_session_status "
                    "ON chat_turn_executions(session_id, status)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_messages_execution "
                    "ON chat_messages(execution_id)"
                )
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_placeholder_per_execution "
                    "ON chat_messages(execution_id) "
                    "WHERE intent = 'streaming_placeholder' AND execution_id IS NOT NULL"
                )
                try:
                    from app.services.chat_todo_service import ensure_chat_todo_schema

                    await ensure_chat_todo_schema(conn)
                except Exception as todo_schema_err:
                    logger.warning("chat_todo_schema_init_failed", error=str(todo_schema_err))
                # INSERT 기능 테스트
                _test_ok = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='chat_messages' AND column_name='content')"
                )
                if not _test_ok:
                    logger.error("startup_schema_validation_FAILED: chat_messages.content missing")
                else:
                    logger.info("startup_schema_validation_ok")
                try:
                    from app.api.conversations import ensure_chat_messages_search_index

                    await ensure_chat_messages_search_index(conn)
                    logger.info("chat_messages_trgm_index_ensured")
                except Exception as index_init_err:
                    logger.warning("chat_messages_trgm_index_init_failed", error=str(index_init_err))
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

    # Workspace change ledger 테이블 사전 생성
    try:
        from app.services.workspace_change_tracker import ensure_workspace_change_table
        await ensure_workspace_change_table()
        logger.info("workspace_change_ledger_ensured")
    except Exception as e:
        logger.warning("workspace_change_ledger_ensure_failed", error=str(e))

    # AADS-189B: 서버 시작 시 레지스트리 정합성 보정 + 모델 자동반영 재동기화
    try:
        from app.services.model_registry import sync_model_registry

        registry_sync = await sync_model_registry(triggered_by="startup", reason="startup_bootstrap")
        if registry_sync.get("ok"):
            logger.info(
                "model_registry_startup_synced",
                models_synced=registry_sync.get("models_synced", 0),
                normalized_providers=registry_sync.get("normalized_providers", {}),
                review_required_providers=registry_sync.get("review_required_providers", []),
            )
            if registry_sync.get("review_required_providers"):
                logger.warning(
                    "model_registry_review_required",
                    providers=registry_sync.get("review_required_providers", []),
                )
        else:
            logger.warning(
                "model_registry_startup_sync_failed",
                error=registry_sync.get("error", "unknown"),
            )
    except Exception as e:
        logger.warning("model_registry_startup_sync_failed", error=str(e))

    try:
        from app.services.chat_service import (
            cleanup_overlong_running_executions as _startup_cleanup_overlong_running,
            cleanup_stale_streaming_placeholders as _startup_cleanup_stale_placeholders,
            ensure_stale_placeholder_cleanup_task as _ensure_stale_placeholder_cleanup_task,
        )

        await _startup_cleanup_overlong_running()
        await _startup_cleanup_stale_placeholders()
        _ensure_stale_placeholder_cleanup_task()
    except Exception as _e:
        logger.warning(f"startup_placeholder_cleanup_failed: {_e}")

    # Claude Max 사용량 백그라운드 폴러 (DB 영속 저장 + 실시간 갱신)
    try:
        import asyncio as _claude_max_asyncio
        from app.services.oauth_usage_tracker import claude_max_usage_poller as _claude_max_poller
        _interval = int(os.getenv("CLAUDE_MAX_POLL_INTERVAL_SEC", "60"))
        _claude_max_asyncio.create_task(_claude_max_poller(interval_sec=_interval))
        logger.info("claude_max_usage_poller_started interval=%ds", _interval)
    except Exception as _e:
        logger.warning(f"claude_max_usage_poller_start_failed: {_e}")

    import asyncio as _startup_asyncio
    import time as _resume_time

    # execution_id 기반 미완료 응답 자동 재개
    from datetime import datetime as _resume_datetime, timezone as _resume_timezone

    _execution_resume_attempts: dict[str, int] = {}
    _execution_resume_started_at = _resume_datetime.now(_resume_timezone.utc)

    def _is_execution_resume_owner() -> bool:
        owner, _source = _resolve_execution_resume_owner()
        return owner

    def _resolve_execution_resume_owner() -> tuple[bool, str]:
        """Only the currently published API container should claim DB resume jobs.

        Blue/green backup instances are healthy and can serve failover traffic, but
        if they claim background stream recovery while nginx still points clients at
        the active slot, the chat UI sees a DB-running stream owned by another
        process. The active container marker is updated by deploy.sh.

        Priority:
          1. /tmp/aads_execution_resume_owner file, written by deploy.sh or startup self-heal
          2. .active_container file comparison, with AADS_EXECUTION_RESUME_FORCE_OWNER override
          3. .active_port file comparison as a legacy fallback
          4. AADS_ENABLE_EXECUTION_RESUME_SCANNER env override
          5. True, preserving the single-container default
        """
        expected_container = os.getenv("AADS_CONTAINER_NAME", "").strip()
        expected_port = os.getenv("AADS_PUBLIC_PORT", "").strip()

        active_container_file = os.getenv("AADS_ACTIVE_CONTAINER_FILE", "/app/.active_container")
        active_port_file = os.getenv("AADS_ACTIVE_PORT_FILE", "/app/.active_port")
        owner_flag_file = os.getenv("AADS_RESUME_OWNER_FILE", "/tmp/aads_execution_resume_owner")

        try:
            with open(owner_flag_file, "r", encoding="utf-8") as fh:
                owner_flag = fh.read().strip().lower()
            if owner_flag:
                return owner_flag in {"1", "true", "yes", "on", "active"}, "marker"
        except Exception:
            pass

        if expected_container:
            try:
                with open(active_container_file, "r", encoding="utf-8") as fh:
                    active_container = fh.read().strip()
                if active_container:
                    if active_container == expected_container:
                        return True, "active_file"
                    force_owner = os.getenv("AADS_EXECUTION_RESUME_FORCE_OWNER", "").strip().lower()
                    if force_owner in {"1", "true", "yes", "on"}:
                        return True, "env_force"
                    return False, "active_file"
            except Exception:
                pass

        if expected_port:
            try:
                with open(active_port_file, "r", encoding="utf-8") as fh:
                    active_port = fh.read().strip()
                if active_port:
                    return active_port == expected_port, "active_port"
            except Exception:
                pass

        override = os.getenv("AADS_ENABLE_EXECUTION_RESUME_SCANNER")
        if override is not None:
            return override.lower() in {"1", "true", "yes", "on"}, "env_override"

        return True, "default"

    def _selfheal_execution_resume_owner_marker() -> None:
        """Sync the resume owner marker at startup from the active container file."""
        owner_flag_file = os.getenv("AADS_RESUME_OWNER_FILE", "/tmp/aads_execution_resume_owner")
        active_container_file = os.getenv("AADS_ACTIVE_CONTAINER_FILE", "/app/.active_container")
        expected_container = os.getenv("AADS_CONTAINER_NAME", "").strip()
        marker_synced = False

        try:
            is_owner = True
            source = "default"
            if expected_container:
                try:
                    with open(active_container_file, "r", encoding="utf-8") as fh:
                        active_container = fh.read().strip()
                    if active_container:
                        is_owner = active_container == expected_container
                        source = "active_file"
                except Exception:
                    source = "default"

            desired_marker = "true" if is_owner else "false"
            current_marker = ""
            try:
                with open(owner_flag_file, "r", encoding="utf-8") as fh:
                    current_marker = fh.read().strip().lower()
            except Exception:
                current_marker = ""
            if current_marker != desired_marker:
                with open(owner_flag_file, "w", encoding="utf-8") as fh:
                    fh.write(desired_marker)
                marker_synced = True

            owner, source = _resolve_execution_resume_owner()
            logger.info(
                "execution_resume_owner_resolved",
                owner=owner,
                source=source,
                container=expected_container or "(unset)",
                marker_synced=marker_synced,
            )
        except Exception as exc:
            logger.warning(f"execution_resume_owner_selfheal_failed: {exc}")

    async def _resume_pending_executions_once(
        max_rows: int = 5,
        *,
        reclaim_before=None,
        min_stale_seconds: int = 60,
    ):
        try:
            if not _is_execution_resume_owner():
                logger.info(
                    "execution_resume_scan_skipped_inactive",
                    container=os.getenv("AADS_CONTAINER_NAME", ""),
                    port=os.getenv("AADS_PUBLIC_PORT", ""),
                )
                return
            from app.core.db_pool import get_pool as _gp_exec
            from app.services.chat_service import (
                _resume_single_stream as _rss_exec,
                _active_bg_tasks as _abt_exec,
                _streaming_state as _ss_exec,
                _interrupt_execution_if_newer_user as _ieu_exec,
                _mark_execution_interrupted as _mei_exec,
                _strip_streaming_progress_markers as _strip_streaming_progress_markers_exec,
                _has_meaningful_partial_content as _has_meaningful_partial_content_exec,
            )
            _pool = _gp_exec()
            async with _pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT te.id::text AS execution_id,
                           te.session_id::text AS session_id,
                           te.requested_model,
                           te.retry_count,
                           COALESCE(te.error_message, '') AS error_message,
                           COALESCE(ph.id, te.assistant_message_id) AS assistant_message_id,
                           EXTRACT(EPOCH FROM (
                               NOW() - GREATEST(
                                   te.updated_at,
                                   COALESCE(ph.edited_at, ph.created_at, te.updated_at)
                               )
                           ))::int AS stale_seconds,
                           COALESCE(ph.content, am.content, '') AS partial_content,
                           COALESCE(um.content, '') AS last_user_msg,
                           w.name AS workspace_name
                    FROM chat_turn_executions te
                    JOIN chat_sessions s
                      ON s.id = te.session_id
                     AND s.current_execution_id = te.id
                    JOIN chat_workspaces w
                      ON w.id = s.workspace_id
                    LEFT JOIN chat_messages am
                      ON am.id = te.assistant_message_id
                    LEFT JOIN chat_messages um
                      ON um.id = te.user_message_id
                    LEFT JOIN LATERAL (
                        SELECT id, content, created_at, edited_at
                        FROM chat_messages
                        WHERE execution_id = te.id
                          AND intent = 'streaming_placeholder'
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) ph ON TRUE
                    WHERE te.status IN ('running', 'retrying')
                      AND te.updated_at > NOW() - INTERVAL '2 hours'
                      AND (
                          GREATEST(
                              te.updated_at,
                              COALESCE(ph.edited_at, ph.created_at, te.updated_at)
                          ) < NOW() - ($2::int * INTERVAL '1 second')
                          OR (
                              te.status = 'retrying'
                              AND COALESCE(te.error_message, '') LIKE ANY ($4::text[])
                              AND GREATEST(
                                  te.updated_at,
                                  COALESCE(ph.edited_at, ph.created_at, te.updated_at)
                              ) < NOW() - INTERVAL '10 seconds'
                          )
                          OR (
                              $3::timestamptz IS NOT NULL
                              AND GREATEST(
                                  te.updated_at,
                                  COALESCE(ph.edited_at, ph.created_at, te.updated_at)
                              ) < $3::timestamptz
                          )
                      )
                    ORDER BY te.updated_at DESC
                    LIMIT $1
                    """,
                    max_rows,
                    min_stale_seconds,
                    reclaim_before,
                    [
                        "interrupted_auto_retry_scheduled:%",
                        "interrupted_auto_resume_cancelled:%",
                    ],
                )

                for row in rows:
                    sid = row["session_id"]
                    execution_id = row["execution_id"]
                    if sid in _abt_exec and not _abt_exec[sid].done():
                        continue
                    _live_state = _ss_exec.get(sid)
                    if _live_state and not _live_state.get("completed"):
                        _state_updated_at = float(
                            _live_state.get("updated_at")
                            or _live_state.get("started_at")
                            or 0
                        )
                        _state_age_sec = (
                            _resume_time.monotonic() - _state_updated_at
                            if _state_updated_at > 0
                            else 999999
                        )
                        if _state_age_sec < max(30, min_stale_seconds):
                            continue
                        logger.warning(
                            "execution_resume_reclaim_stale_memory_state: session=%s execution=%s state_age=%.1fs",
                            sid[:8],
                            execution_id[:8],
                            _state_age_sec,
                        )
                    _stale_sec = int(row["stale_seconds"] or 0)
                    if _stale_sec > 600:
                        await _mei_exec(
                            conn,
                            sid,
                            execution_id,
                            f"force_interrupted_stale_{_stale_sec}s",
                            partial_content=row["partial_content"] or "",
                            placeholder_id=str(row["assistant_message_id"]) if row["assistant_message_id"] else None,
                            delete_empty_placeholder=not bool((row["partial_content"] or "").strip()),
                        )
                        logger.info("stale_force_interrupt: session=%s execution=%s stale=%ds", sid[:8], execution_id[:8], _stale_sec)
                        continue
                    if (row.get("retry_count") or 0) > 5:
                        await _mei_exec(
                            conn,
                            sid,
                            execution_id,
                            "execution_resume_attempt_limit_exceeded",
                            partial_content=row["partial_content"] or "",
                            placeholder_id=str(row["assistant_message_id"]) if row["assistant_message_id"] else None,
                            delete_empty_placeholder=False,
                        )
                        continue

                    if await _ieu_exec(
                        conn,
                        sid,
                        execution_id,
                        partial_content=row["partial_content"] or "",
                        placeholder_id=str(row["assistant_message_id"]) if row["assistant_message_id"] else None,
                    ):
                        continue

                    _clean_partial = _strip_streaming_progress_markers_exec(row["partial_content"] or "")
                    if (
                        int(row["stale_seconds"] or 0) > 300
                        and not _has_meaningful_partial_content_exec(_clean_partial)
                    ):
                        await _mei_exec(
                            conn,
                            sid,
                            execution_id,
                            "stale empty execution skipped during startup resume",
                            partial_content="",
                            placeholder_id=str(row["assistant_message_id"]) if row["assistant_message_id"] else None,
                            delete_empty_placeholder=True,
                        )
                        continue

                    _claim_id = await conn.fetchval(
                        """
                        UPDATE chat_turn_executions
                        SET status = 'retrying',
                            retry_count = retry_count + 1,
                            updated_at = NOW(),
                            error_message = $2
                        WHERE id = $1::uuid
                          AND status IN ('running', 'retrying')
                          AND (
                              updated_at < NOW() - ($3::int * INTERVAL '1 second')
                              OR (
                                  status = 'retrying'
                                  AND COALESCE(error_message, '') LIKE ANY ($5::text[])
                                  AND updated_at < NOW() - INTERVAL '10 seconds'
                              )
                              OR ($4::timestamptz IS NOT NULL AND updated_at < $4::timestamptz)
                          )
                        RETURNING id::text
                        """,
                        execution_id,
                        "resume_claimed_by:" + os.getenv("HOSTNAME", "unknown")[:80],
                        min_stale_seconds,
                        reclaim_before,
                        [
                            "interrupted_auto_retry_scheduled:%",
                            "interrupted_auto_resume_cancelled:%",
                        ],
                    )
                    if not _claim_id:
                        continue

                    placeholder_id = row["assistant_message_id"]
                    if not placeholder_id:
                        placeholder_id = await conn.fetchval(
                            """
                            INSERT INTO chat_messages (
                                session_id, execution_id, role, content, intent, model_used, tools_called
                            )
                            VALUES ($1::uuid, $2::uuid, 'assistant', $3, 'streaming_placeholder', 'streaming', '[]'::jsonb)
                            ON CONFLICT (execution_id) WHERE intent = 'streaming_placeholder' AND execution_id IS NOT NULL
                            DO UPDATE SET content = COALESCE(chat_messages.content, EXCLUDED.content), edited_at = NOW()
                            RETURNING id
                            """,
                            sid,
                            execution_id,
                            row["partial_content"] or "",
                        )
                    if placeholder_id:
                        await conn.execute(
                            """
                            UPDATE chat_turn_executions
                            SET assistant_message_id = $2,
                                updated_at = NOW()
                            WHERE id = $1::uuid
                            """,
                            execution_id,
                            placeholder_id,
                        )

                    _resume_t = _startup_asyncio.create_task(
                        _rss_exec(
                            sid,
                            placeholder_id,
                            row["partial_content"] or "",
                            row["last_user_msg"] or "",
                            row["workspace_name"] or "CEO",
                            execution_id=execution_id,
                            requested_model=row["requested_model"],
                        )
                    )
                    def _on_resume_done(_t, _sid=sid, _eid=execution_id):
                        if _t.cancelled():
                            async def _sync_cancelled_status():
                                try:
                                    async with _gp_exec().acquire() as _c:
                                        _ph = await _c.fetchrow(
                                            """
                                            SELECT id::text AS id, content
                                            FROM chat_messages
                                            WHERE execution_id = $1::uuid
                                              AND intent = 'streaming_placeholder'
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                            """,
                                            _eid,
                                        )
                                        _partial = (_ph["content"] if _ph else "") or ""
                                        await _mei_exec(
                                            _c,
                                            _sid,
                                            _eid,
                                            "resume_task_cancelled",
                                            partial_content=_partial,
                                            placeholder_id=_ph["id"] if _ph else None,
                                            delete_empty_placeholder=not bool(_strip_streaming_progress_markers_exec(_partial).strip()),
                                        )
                                except Exception:
                                    pass
                            _startup_asyncio.ensure_future(_sync_cancelled_status())
                            return
                        _exc = _t.exception()
                        if _exc:
                            logger.error("resume_task_escaped: session=%s execution=%s error=%s", _sid[:8], _eid[:8], _exc)
                            async def _sync_exec_status():
                                try:
                                    async with _gp_exec().acquire() as _c:
                                        _ph = await _c.fetchrow(
                                            """
                                            SELECT id::text AS id, content
                                            FROM chat_messages
                                            WHERE execution_id = $1::uuid
                                              AND intent = 'streaming_placeholder'
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                            """,
                                            _eid,
                                        )
                                        _partial = (_ph["content"] if _ph else "") or ""
                                        await _mei_exec(
                                            _c,
                                            _sid,
                                            _eid,
                                            f"task_escaped: {str(_exc)[:400]}",
                                            partial_content=_partial,
                                            placeholder_id=_ph["id"] if _ph else None,
                                            delete_empty_placeholder=not bool(_strip_streaming_progress_markers_exec(_partial).strip()),
                                        )
                                except Exception:
                                    pass
                            _startup_asyncio.ensure_future(_sync_exec_status())
                    _resume_t.add_done_callback(_on_resume_done)
                    logger.info(
                        "execution_resume: session=%s execution=%s attempt=%s",
                        sid[:8],
                        execution_id[:8],
                        (row.get("retry_count") or 0) + 1,
                    )
        except Exception as e:
            logger.warning(f"execution_resume_scan_failed: {e}")

    async def _resume_pending_executions_startup():
        import asyncio as _resume_asyncio
        await _resume_asyncio.sleep(5)
        _startup_stale_seconds = int(os.getenv("AADS_EXECUTION_RESUME_STARTUP_STALE_SECONDS", "15"))
        # P0-3: Rescue interrupted executions from prior shutdown
        try:
            from app.core.db_pool import get_pool as _gp_rescue
            _pool_rescue = _gp_rescue()
            async with _pool_rescue.acquire() as _conn_rescue:
                _rescued_count = await _conn_rescue.execute(
                    "UPDATE chat_turn_executions "
                    "SET status = 'retrying', "
                    "    error_message = COALESCE(error_message, '') || ' [startup_rescue]', "
                    "    updated_at = NOW() "
                    "WHERE status = 'interrupted' "
                    "  AND updated_at > NOW() - INTERVAL '30 minutes' "
                    "  AND (error_message LIKE 'api_shutdown%' "
                    "       OR error_message LIKE 'deploy_shutdown%' "
                    "       OR error_message LIKE 'server_shutdown%' "
                    "       OR error_message LIKE 'shutdown_pending_resume%')"
                )
                _rescued_sessions = await _conn_rescue.fetch(
                    "UPDATE chat_sessions s "
                    "SET current_execution_id = te.id, updated_at = NOW() "
                    "FROM chat_turn_executions te "
                    "WHERE te.session_id = s.id "
                    "  AND te.status = 'retrying' "
                    "  AND te.error_message LIKE '%startup_rescue%' "
                    "  AND s.current_execution_id IS NULL "
                    "RETURNING s.id::text AS session_id"
                )
                if _rescued_sessions:
                    logger.info(f"startup_rescue_interrupted: restored {len(_rescued_sessions)} session(s)")
        except Exception as _rescue_err:
            logger.warning(f"startup_rescue_failed: {_rescue_err}")
        await _resume_pending_executions_once(
            max_rows=10,
            reclaim_before=_execution_resume_started_at,
            min_stale_seconds=_startup_stale_seconds,
        )

    async def _periodic_execution_resume_scanner():
        import asyncio as _prs_asyncio
        _periodic_stale_seconds = int(os.getenv("AADS_EXECUTION_RESUME_STALE_SECONDS", "60"))
        await _prs_asyncio.sleep(15)
        while True:
            try:
                await _prs_asyncio.sleep(30)
                await _resume_pending_executions_once(
                    max_rows=5,
                    min_stale_seconds=_periodic_stale_seconds,
                )
            except Exception as _e:
                logger.warning(f"execution_resume_scanner_error: {_e}")

    _selfheal_execution_resume_owner_marker()
    _startup_asyncio.create_task(_resume_pending_executions_startup())
    _startup_asyncio.create_task(_periodic_execution_resume_scanner())

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

    # Rate limit 만료 자동 정리 + model registry 복구 (재발 방지)
    async def _periodic_rate_limit_cleanup():
        import asyncio as _rl_asyncio
        await _rl_asyncio.sleep(30)
        while True:
            try:
                from app.services.model_registry import clear_expired_rate_limits
                result = await clear_expired_rate_limits()
                if result.get("cleared", 0) > 0:
                    logger.info(f"rate_limit_auto_cleanup: {result}")
            except Exception as _e:
                logger.warning(f"rate_limit_auto_cleanup_failed: {_e}")
            await _rl_asyncio.sleep(60)

    _startup_asyncio.create_task(_periodic_rate_limit_cleanup())

    # 고아 claude CLI 프로세스 주기적 정리 (응답 중단 방지)
    async def _periodic_orphan_claude_reaper():
        import asyncio as _reaper_asyncio
        await _reaper_asyncio.sleep(60)
        while True:
            try:
                from app.services.agent_sdk_service import (
                    cleanup_orphan_claude_processes,
                    _active_iterators,
                    _find_claude_child_pids,
                )
                active_count = len(_active_iterators)
                all_pids = set(_find_claude_child_pids())
                if len(all_pids) > max(active_count + 1, 2):
                    killed = cleanup_orphan_claude_processes()
                    if killed:
                        logger.warning(f"orphan_claude_reaper: {killed}개 프로세스 정리 (활성={active_count})")
            except Exception as _e:
                logger.debug(f"orphan_claude_reaper_failed: {_e}")
            await _reaper_asyncio.sleep(120)

    _startup_asyncio.create_task(_periodic_orphan_claude_reaper())

    # Claude Max 사용량 폴러 시작
    try:
        from app.services.oauth_usage_tracker import ensure_claude_max_poller_running
        ensure_claude_max_poller_running()
        logger.info("claude_max_usage_poller_started_at_boot")
    except Exception as e:
        logger.warning(f"claude_max_usage_poller_start_failed: {e}")

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

    # 종료 정리 — PC Agent WebSocket graceful-shutdown (SIGTERM/Docker 재시작 시 code=1012 전송)
    try:
        from app.services.pc_agent_manager import pc_agent_manager
        _pc_closed = await pc_agent_manager.close_all_connections(reason="server_shutdown")
        if _pc_closed:
            logger.info("pc_agent_graceful_shutdown", closed=_pc_closed)
    except Exception as _pc_err:
        logger.warning(f"pc_agent_graceful_shutdown_failed: {_pc_err}")
    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("apscheduler_stopped")
    try:
        from app.services.chat_service import cancel_stale_placeholder_cleanup_task
        await cancel_stale_placeholder_cleanup_task()
    except Exception as _cleanup_err:
        logger.warning(f"stale_placeholder_cleanup_task_stop_failed: {_cleanup_err}")
    try:
        from app.core.langfuse_config import flush_langfuse
        flush_langfuse()
    except Exception:
        pass
    if mcp_manager:
        await mcp_manager.shutdown()
    await memory_store.close()
    # 활성 스트리밍 태스크 drain (AADS-P0: 배포 시 응답 끊김 방지)
    try:
        import asyncio
        from app.services.chat_service import _active_bg_tasks, preserve_active_streams_for_shutdown
        if _active_bg_tasks:
            _n_tasks = len(_active_bg_tasks)
            logger.info(f"preserving {_n_tasks} active background tasks before pool close...")
            await preserve_active_streams_for_shutdown("api_shutdown_before_process_stop")
            for _sid, _task in list(_active_bg_tasks.items()):
                if not _task.done():
                    _task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*list(_active_bg_tasks.values()), return_exceptions=True),
                timeout=180,
            )
            logger.info(f"drained {_n_tasks} background tasks")
    except asyncio.TimeoutError:
        logger.warning("bg task drain timed out after 180s, proceeding with shutdown")
    except Exception as _drain_err:
        logger.warning(f"bg task drain error: {_drain_err}")
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
    version="0.2.1",
    description="Autonomous AI Development System — Phase 2 Dashboard",
    lifespan=lifespan,
)

# H-07: CORS middleware — restrict to AADS dashboard origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://aads.newtalk.kr",
        "https://kakaobot.newtalk.kr",
        "https://newtalk.kr",
        "https://www.newtalk.kr",
        "https://v2.newtalk.kr",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://5.104.86.116:3100",
        "http://5.104.86.116:8100",
        *[
            origin.strip()
            for origin in os.getenv("AADS_EXTERNAL_CHAT_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ],
    ],
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
    "/api/v1/auth/e2e-inject",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/mcp",
    "/api/v1/pc-agent",
    "/api/v1/review",
    "/api/v1/kakao-bot/msgbot/webhook",
    "/api/v1/kakao-bot/respond",
    "/api/v1/kakao-bot/agent",
    "/api/v1/devices/android/manifest",
    "/api/v1/devices/android/install",
    "/api/v1/devices/android/download",
    "/api/v1/devices/android/auto-register",
    "/api/v1/devices/android/source.zip",
    "/api/v1/browser-bridge/sessions/register",
    "/api/v1/ops/hot-reload",  # 내부 hot-reload (127.0.0.1 전용)
    "/api/v1/ops/active-streams",  # 내부 스트림 drain 감지 (deploy.sh 전용)
    "/api/v1/image/gallery",  # AI 모델 이미지 갤러리 (공개 읽기전용)
    "/api/v1/ops/usage-stats",  # 사용량 통계 (읽기전용)
    "/api/v1/ops/codex-usage",  # Codex 사용량 (읽기전용)
    "/api/v1/ops/claude-max-usage",  # Claude Max 사용량 (읽기전용)
    "/api/v1/external/chat",  # 외부 서비스 임베드 채팅: 자체 service-token/HMAC 인증
    "/api/v1/ops/locks",        # 내부 서비스 잠금 API (pipeline-runner.sh 전용)
    "/api/v1/ops/active-work",  # 내부 활성 작업 조회
    "/static",  # 정적 파일 (기술문서/보고서/갤러리)
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
    if not auth_header.startswith("Bearer "):
        cookie_token = _auth_mod.extract_aads_cookie_token(request)
        if cookie_token:
            auth_header = f"Bearer {cookie_token}"
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = _auth_mod.verify_token(token)
        if payload:
            request.state.user = payload
            return await call_next(request)

    # 5) X-Monitor-Key 헤더가 있으면 통과 (내부 서비스 간 호출)
    if request.headers.get("x-monitor-key"):
        return await call_next(request)

    # 인증 실패 — CORS 헤더를 포함해야 브라우저가 401 응답을 읽을 수 있음
    _origin = request.headers.get("origin", "")
    _cors_h: dict = {}
    if _origin and any(_origin == o for o in (
        "https://aads.newtalk.kr", "https://kakaobot.newtalk.kr",
        "https://newtalk.kr", "https://www.newtalk.kr", "https://v2.newtalk.kr",
        "http://localhost:3000", "http://localhost:3001",
    )):
        _cors_h = {"access-control-allow-origin": _origin, "access-control-allow-credentials": "true", "vary": "Origin"}
    return JSONResponse(status_code=401, content={"detail": "인증이 필요합니다. Bearer 토큰을 제공하세요."}, headers=_cors_h)

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
app.include_router(governance_router, prefix="/api/v1", tags=["governance"])
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
app.include_router(pc_ollama_bridge_router)
app.include_router(assistant_router, prefix="/api/v1", tags=["assistant"])
app.include_router(voice_router, prefix="/api/v1", tags=["voice"])
app.include_router(local_models_router, prefix="/api/v1", tags=["local-models"])
app.include_router(device_router, prefix="/api/v1", tags=["device"])
app.include_router(kakao_bot_router, prefix="/api/v1", tags=["kakao-bot"])
app.include_router(agenda_router, prefix="/api/v1/agenda", tags=["agenda"])
app.include_router(hot_reload_router, prefix="/api/v1", tags=["hot-reload"])
app.include_router(admin_router, prefix="/api/v1", tags=["admin"])
app.include_router(admin_users_router, prefix="/api/v1", tags=["admin-users"])
app.include_router(design_modifications.router, prefix="/api/v1", tags=["design-modifications"])
app.include_router(yeoljeong_finance.router, prefix="/api/v1", tags=["yeoljeong-finance"])
app.include_router(credential_vault_router, prefix="/api/v1", tags=["credential-vault"])
app.include_router(google_sheets.router, prefix="/api/v1", tags=["google-sheets"])
app.include_router(notifications.router, prefix="/api/v1", tags=["notifications"])
app.include_router(llm_keys_router, prefix="/api/v1", tags=["llm-keys"])
app.include_router(llm_models_router, prefix="/api/v1", tags=["llm-models"])
app.include_router(braming_router)
app.include_router(project_docs_router, prefix="/api/v1", tags=["project-docs"])
app.include_router(files_router, prefix="/api/v1", tags=["files"])
app.include_router(terminal.router, prefix="/api/v1", tags=["terminal"])
app.include_router(browser_bridge.router, prefix="/api/v1", tags=["browser-bridge"])
app.include_router(external_chat_router, prefix="/api/v1", tags=["external-chat"])
app.include_router(local_media_router)
app.include_router(ohvis_tasks_router, prefix="/api/v1", tags=["ohvis-tasks"])
app.include_router(loops_router, prefix="/api/v1", tags=["loops"])
app.include_router(browser_tasks_router, prefix="/api/v1", tags=["browser-tasks"])
app.include_router(agent_vault_router, prefix="/api/v1", tags=["agent-vault"])

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
        "version": "0.2.1",
        "sandbox": sandbox_health,
    })# 정적 파일 서빙


@app.get("/health/live", tags=["health"], include_in_schema=False)
async def live_health_check():
    """Container liveness probe. Keep this independent from deep service checks."""
    return _JSONResponse({
        "status": "ok",
        "version": "0.2.1",
    })


@app.get("/api/v1/health/live", tags=["health"], include_in_schema=False)
async def api_live_health_check():
    """API-prefixed liveness probe for Docker/Nginx health checks."""
    return _JSONResponse({
        "status": "ok",
        "version": "0.2.1",
    })


# 정적 파일 서빙
import pathlib as _pathlib
_static_dir = _pathlib.Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")# AADS-186C: FastAPI-MCP 마운트 (graceful — MCP_ENABLED=false 시 비활성)
setup_mcp(app)
