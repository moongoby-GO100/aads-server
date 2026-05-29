"""
AADS-195: PC 제어 에이전트 API.
WebSocket 엔드포인트 + REST API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys as _sys_reload
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.models.pc_agent import CommandRequest, RoutedCommandRequest, StreamConfig, WSMessage
from app.services.pc_agent_manager import pc_agent_manager

logger = logging.getLogger(__name__)
router = APIRouter()

PC_AGENT_SECRET = os.environ.get("PC_AGENT_SECRET", "")
HEARTBEAT_INTERVAL = 30  # 초

# hot-reload 시 기존 WebSocket 연결 상태 보존
_prev_mod = _sys_reload.modules.get(__name__)
if _prev_mod is not None and hasattr(_prev_mod, "_agent_connections"):
    _agent_connections: dict[str, WebSocket] = dict(getattr(_prev_mod, '_agent_connections', {}))
    _pending_reload_disconnects: list[str] = []
    _EVENT_TABLE_READY: bool = getattr(_prev_mod, "_EVENT_TABLE_READY", False)
    _EVENT_RECORD_FAILURE_COUNT: int = getattr(_prev_mod, "_EVENT_RECORD_FAILURE_COUNT", 0)
else:
    _agent_connections: dict[str, WebSocket] = {}
    _pending_reload_disconnects = []
    _EVENT_TABLE_READY = False
    _EVENT_RECORD_FAILURE_COUNT = 0
_RELOAD_DISCONNECT_FLUSH_TASK: asyncio.Task[Any] | None = None


async def _record_agent_event(
    agent_id: str,
    event: str,
    *,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """PC Agent 연결 이벤트를 best-effort로 DB에 기록한다."""
    global _EVENT_TABLE_READY, _EVENT_RECORD_FAILURE_COUNT
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            if not _EVENT_TABLE_READY:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pc_agent_connection_events (
                        id BIGSERIAL PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        event TEXT NOT NULL,
                        reason TEXT DEFAULT '',
                        metadata JSONB DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_pc_agent_connection_events_recent
                    ON pc_agent_connection_events (created_at DESC, agent_id)
                    """
                )
                _EVENT_TABLE_READY = True
            await conn.execute(
                """
                INSERT INTO pc_agent_connection_events (agent_id, event, reason, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                agent_id,
                event,
                reason,
                json.dumps(metadata or {}),
            )
    except Exception as exc:
        _EVENT_RECORD_FAILURE_COUNT += 1
        logger.warning("pc_agent_event_record_failed agent_id=%s event=%s err=%s", agent_id, event, exc)
        logger.error(
            "pc_agent_event_record_failed_count=%d agent_id=%s event=%s",
            _EVENT_RECORD_FAILURE_COUNT,
            agent_id,
            event,
            exc_info=True,
        )


async def _flush_pending_reload_disconnects() -> None:
    """hot-reload로 stale 된 연결의 disconnected 이벤트를 기록한다."""
    global _pending_reload_disconnects, _RELOAD_DISCONNECT_FLUSH_TASK
    if not _pending_reload_disconnects:
        return

    stale_agent_ids = _pending_reload_disconnects
    _pending_reload_disconnects = []
    logger.warning(
        "pc_agent_ws_hot_reload_stale_connections count=%d",
        len(stale_agent_ids),
    )
    for stale_agent_id in stale_agent_ids:
        await _record_agent_event(
            stale_agent_id,
            "disconnected",
            reason="hot_reload_stale_connection",
            metadata={"reason_source": "hot_reload_guard"},
        )


def _schedule_reload_disconnect_flush() -> None:
    """가능한 경우 stale 연결 정리를 즉시 백그라운드로 시작한다."""
    global _RELOAD_DISCONNECT_FLUSH_TASK
    if not _pending_reload_disconnects:
        return
    if _RELOAD_DISCONNECT_FLUSH_TASK is not None and not _RELOAD_DISCONNECT_FLUSH_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    _RELOAD_DISCONNECT_FLUSH_TASK = loop.create_task(_flush_pending_reload_disconnects())


_schedule_reload_disconnect_flush()


async def _verify_token_db(token: str) -> bool:
    """DB에서 토큰 유효성 검증 (kakao_pc_agent_tokens 테이블)."""
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM kakao_pc_agent_tokens WHERE token = $1",
                token,
            )
            if row is not None:
                await conn.execute(
                    "UPDATE kakao_pc_agent_tokens SET last_used_at = NOW() WHERE token = $1",
                    token,
                )
        return row is not None
    except Exception as exc:
        logger.warning("pc_agent_token_db_check_failed: %s", exc)
        return False


# ── WebSocket ──────────────────────────────────────────────────────────

@router.websocket("/pc-agent/ws/{agent_id}")
async def ws_pc_agent(websocket: WebSocket, agent_id: str, token: str = Query("")):
    """PC 에이전트 WebSocket 연결."""
    await _flush_pending_reload_disconnects()

    # 인증: DB 토큰 → 환경변수 폴백
    token_valid = False
    if token:
        token_valid = await _verify_token_db(token)
        if not token_valid and PC_AGENT_SECRET and token == PC_AGENT_SECRET:
            token_valid = True
    if not token_valid and not PC_AGENT_SECRET:
        token_valid = True
    if not token_valid:
        await websocket.close(code=4001, reason="unauthorized")
        logger.warning("pc_agent_ws_auth_failed agent_id=%s", agent_id)
        await _record_agent_event(agent_id, "auth_failed", reason="unauthorized")
        return

    old_ws = _agent_connections.pop(agent_id, None)
    if old_ws is not None:
        try:
            await old_ws.close(code=4010, reason="replaced_by_new")
        except Exception:
            pass
        logger.info("pc_agent_ws_replaced agent_id=%s", agent_id)
        await _record_agent_event(agent_id, "replaced", reason="replaced_by_new")

    await websocket.accept()
    _agent_connections[agent_id] = websocket
    connected_at = datetime.utcnow()
    logger.info("pc_agent_ws_connected agent_id=%s total=%d", agent_id, len(_agent_connections))
    await _record_agent_event(agent_id, "connected")
    disconnect_recorded = False
    disconnect_reason = ""
    disconnect_metadata: dict[str, Any] = {}

    def _build_disconnect_metadata(
        *,
        close_code: int | None,
        close_reason: str | None,
        exc_type: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "uptime_seconds": round((datetime.utcnow() - connected_at).total_seconds(), 1),
            "close_code": close_code,
            "close_reason": close_reason,
            "exc_type": exc_type,
        }
        if extra:
            metadata.update(extra)
        return metadata

    async def _record_disconnect_once(reason: str, metadata: dict[str, Any]) -> None:
        nonlocal disconnect_recorded, disconnect_reason, disconnect_metadata
        if not disconnect_reason:
            disconnect_reason = reason
            disconnect_metadata = metadata
        if disconnect_recorded:
            return
        disconnect_recorded = True
        await _record_agent_event(
            agent_id,
            "disconnected",
            reason=disconnect_reason,
            metadata=disconnect_metadata,
        )

    async def _close_socket(code: int, reason: str) -> None:
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    # 등록 메시지 대기 (첫 메시지)
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        msg = WSMessage.model_validate(raw)
        if msg.type != "register":
            await _record_agent_event(agent_id, "register_failed", reason="first message must be register")
            if _agent_connections.get(agent_id) is websocket:
                _agent_connections.pop(agent_id, None)
            await websocket.close(code=4002, reason="first message must be register")
            return
        pc_agent_manager.register_agent(agent_id, websocket, msg.payload)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.error("pc_agent_ws_register_failed agent_id=%s err=%s", agent_id, exc)
        await _record_agent_event(agent_id, "register_failed", reason=str(exc)[:300])
        if _agent_connections.get(agent_id) is websocket:
            _agent_connections.pop(agent_id, None)
        await websocket.close(code=4003, reason="register failed")
        return

    # 서버 → 클라이언트 keepalive ping (dead connection 조기 감지)
    async def _server_ping() -> None:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await websocket.send_json({"type": "heartbeat", "id": "", "payload": {}})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metadata = _build_disconnect_metadata(
                    close_code=1011,
                    close_reason="server_ping_failed",
                    exc_type=type(exc).__name__,
                    extra={"reason_source": "server_ping"},
                )
                logger.info(
                    "pc_agent_ws_server_ping_failed agent_id=%s err=%s",
                    agent_id, exc,
                )
                await _record_disconnect_once("server_ping_failed", metadata)
                # 메시지 수신 루프가 즉시 빠져나가도록 명시적으로 닫는다
                await _close_socket(code=1011, reason="server_ping_failed")
                return

    ping_task = asyncio.create_task(_server_ping())

    # 메시지 수신 루프
    try:
        while True:
            raw = await asyncio.wait_for(
                websocket.receive_json(), timeout=HEARTBEAT_INTERVAL * 3
            )
            msg = WSMessage.model_validate(raw)

            if msg.type == "heartbeat":
                pc_agent_manager.update_heartbeat(agent_id)
                await websocket.send_json(
                    {"type": "heartbeat", "id": msg.id, "payload": {}}
                )

            elif msg.type == "result":
                pc_agent_manager.receive_result(msg.id, msg.payload)

            elif msg.type == "stream_frame":
                frame = msg.payload.get("frame", "")
                if frame:
                    await pc_agent_manager.broadcast_frame(agent_id, frame)

            elif msg.type == "network_info":
                try:
                    from app.services.wol_service import register_agent_network
                    payload = msg.payload
                    await register_agent_network(
                        agent_id=agent_id,
                        mac_address=payload.get("mac_address", ""),
                        ip_address=payload.get("ip_address", ""),
                        label=payload.get("hostname", ""),
                    )
                except Exception as e:
                    logger.warning("WoL 네트워크 등록 실패: %s", e)

            else:
                logger.warning(
                    "pc_agent_ws_unknown_type agent_id=%s type=%s", agent_id, msg.type
                )

    except (WebSocketDisconnect, asyncio.TimeoutError) as exc:
        close_code = getattr(exc, 'code', None)
        close_reason = getattr(exc, 'reason', None)
        close_reason_to_use = close_reason
        if isinstance(exc, asyncio.TimeoutError):
            reason_detail = "heartbeat_timeout"
            close_code = 1011
            close_reason_to_use = "heartbeat_timeout"
        else:
            reason_detail = type(exc).__name__
            if close_code is not None:
                reason_detail = f"{reason_detail} code={close_code}"
            if close_reason:
                reason_detail = f"{reason_detail} reason={close_reason}"
        uptime_s = (datetime.utcnow() - connected_at).total_seconds()
        logger.info(
            "pc_agent_ws_disconnected agent_id=%s reason=%s uptime=%.1fs",
            agent_id, reason_detail, uptime_s,
        )
        metadata = _build_disconnect_metadata(
            close_code=close_code,
            close_reason=close_reason_to_use,
            exc_type=type(exc).__name__,
        )
        await _record_disconnect_once(reason_detail, metadata)
        await _close_socket(code=close_code or 1000, reason=close_reason_to_use or "disconnected")
    except Exception as exc:
        uptime_s = (datetime.utcnow() - connected_at).total_seconds()
        logger.error("pc_agent_ws_error agent_id=%s err=%s uptime=%.1fs", agent_id, exc, uptime_s)
        await _record_agent_event(
            agent_id,
            "error",
            reason=str(exc)[:300],
            metadata={"uptime_seconds": round(uptime_s, 1), "exc_type": type(exc).__name__},
        )
        if not disconnect_reason:
            disconnect_reason = "unexpected_error"
            disconnect_metadata = _build_disconnect_metadata(
                close_code=1011,
                close_reason="unexpected_error",
                exc_type=type(exc).__name__,
            )
        await _close_socket(code=1011, reason="unexpected_error")
    finally:
        ping_task.cancel()
        pc_agent_manager.unregister_agent(agent_id, websocket)
        if _agent_connections.get(agent_id) is websocket:
            _agent_connections.pop(agent_id, None)
        if not disconnect_reason:
            disconnect_reason = "connection_cleanup"
            disconnect_metadata = _build_disconnect_metadata(
                close_code=None,
                close_reason=None,
                exc_type="cleanup",
            )
        if not disconnect_recorded:
            try:
                await _record_agent_event(
                    agent_id,
                    "disconnected",
                    reason=disconnect_reason,
                    metadata=disconnect_metadata,
                )
            except Exception as exc:
                logger.error(
                    "pc_agent_ws_final_disconnect_record_failed agent_id=%s err=%s",
                    agent_id,
                    exc,
                    exc_info=True,
                )


# ── REST API ──────────────────────────────────────────────────────────

@router.get("/pc-agent/status")
async def pc_agent_status():
    """PC Agent 연결 상태 요약 조회."""
    await _flush_pending_reload_disconnects()
    agents = pc_agent_manager.list_agents()
    return {
        "status": "online" if agents else "offline",
        "online_count": len(agents),
        "agents": [a.model_dump(mode="json") for a in agents],
    }


@router.get("/pc-agent/agents")
async def list_agents():
    """연결된 에이전트 목록 조회."""
    await _flush_pending_reload_disconnects()
    agents = pc_agent_manager.list_agents()
    return {"agents": [a.model_dump(mode="json") for a in agents]}


@router.post("/pc-agent/graceful-shutdown")
async def graceful_shutdown():
    """배포/재시작 전 모든 PC Agent WebSocket을 정상 종료한다.
    클라이언트가 1012 코드를 받으면 즉시 재연결을 시도한다."""
    await _flush_pending_reload_disconnects()
    closed = await pc_agent_manager.close_all_connections(reason="server_restart")
    _agent_connections.clear()
    return {"closed": closed, "message": f"{closed}개 연결 정상 종료"}


@router.post("/pc-agent/execute")
async def execute_command(req: CommandRequest):
    """에이전트에 명령 실행 요청."""
    agent = pc_agent_manager.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{req.agent_id}'가 연결되어 있지 않습니다.")

    try:
        command_id = await pc_agent_manager.send_command(
            req.agent_id, req.command_type, req.params
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "pending"}


@router.post("/pc-agent/route-execute")
async def route_execute_command(req: RoutedCommandRequest):
    """Capability 기반 라우팅 + lease/queue 제어로 명령 실행."""
    params = dict(req.params or {})
    effective_command_timeout_seconds = float(req.command_timeout_seconds)
    raw_param_timeout = (
        params.get("command_timeout_seconds")
        if "command_timeout_seconds" in params
        else params.get("timeout")
    )
    if raw_param_timeout is not None:
        try:
            param_timeout = float(raw_param_timeout)
        except Exception:
            param_timeout = effective_command_timeout_seconds
        if param_timeout > 0:
            effective_command_timeout_seconds = min(effective_command_timeout_seconds, param_timeout)
    params["command_timeout_seconds"] = effective_command_timeout_seconds
    if req.command_type.strip().lower() == "browser_eval" and "evaluate_timeout_seconds" not in params:
        params["evaluate_timeout_seconds"] = max(1.0, min(20.0, effective_command_timeout_seconds - 0.5))

    result = await pc_agent_manager.execute_routed_command(
        command_type=req.command_type,
        params=params,
        agent_id=req.agent_id,
        job_type=req.job_type,
        required_capabilities=req.required_capabilities,
        queue_if_busy=req.queue_if_busy,
        wait_for_turn=req.wait_for_turn,
        queue_wait_timeout_seconds=req.queue_wait_timeout_seconds,
        lease_ttl_seconds=req.lease_ttl_seconds,
        command_timeout_seconds=effective_command_timeout_seconds,
    )
    if result.get("status") == "error":
        error_code = str(result.get("error_code", "") or "")
        status_code = 409 if error_code in {"AGENT_BUSY", "LEASE_EXPIRED"} else 503
        if error_code in {"NO_CAPABLE_AGENT"}:
            status_code = 422
        if error_code in {"COMMAND_TIMEOUT", "RUNTIME_EVALUATE_TIMEOUT"}:
            status_code = 504
        if error_code in {"CDP_NOT_READY", "STALE_TARGET", "SYNTAX_ERROR", "SPA_SHELL_ONLY", "VVIC_LOGIN_REQUIRED", "VVIC_BLOCKED"}:
            status_code = 424
        raise HTTPException(status_code=status_code, detail=result)
    return result


@router.get("/pc-agent/leases")
async def list_pc_agent_leases(
    agent_id: str = Query(""),
    job_type: str = Query(""),
    status: str = Query(""),
):
    """현재 lease/queue 상태 조회."""
    leases = await pc_agent_manager.list_leases(
        agent_id=agent_id,
        job_type=job_type,
        status=status,
    )
    return {"leases": leases, "count": len(leases)}


@router.get("/pc-agent/leases/{lease_id}")
async def get_pc_agent_lease(lease_id: str):
    """특정 lease 상태 조회."""
    lease = await pc_agent_manager.get_lease(lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail={"error_code": "LEASE_EXPIRED", "message": "lease not found"})
    return {"lease": lease}


@router.post("/pc-agent/leases/{lease_id}/heartbeat")
async def heartbeat_pc_agent_lease(
    lease_id: str,
    extend_seconds: int = Query(180, ge=30, le=1800),
):
    """진행 중 lease heartbeat/만료 연장."""
    result = await pc_agent_manager.heartbeat_lease(lease_id, extend_seconds=extend_seconds)
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/pc-agent/result/{command_id}")
async def get_result(command_id: str, timeout: float = Query(30.0, ge=1.0, le=120.0)):
    """명령 실행 결과 조회 (대기)."""
    try:
        result = await pc_agent_manager.get_result(command_id, timeout=timeout)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return result.model_dump(mode="json")


# ── 스트리밍 ──────────────────────────────────────────────────────────

@router.websocket("/pc-agent/stream/{agent_id}")
async def ws_stream(websocket: WebSocket, agent_id: str):
    """대시보드 → 스트리밍 수신용 WebSocket."""
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        await websocket.close(code=4004, reason=f"agent '{agent_id}' not connected")
        return

    await websocket.accept()

    # 구독자 등록
    pc_agent_manager.add_stream_subscriber(agent_id, websocket)

    try:
        # 첫 메시지로 StreamConfig JSON 수신 → 스트리밍 시작
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        config = StreamConfig.model_validate(raw)
        await pc_agent_manager.start_stream(agent_id, config)
        logger.info("stream_ws_started agent_id=%s", agent_id)

        # 연결 유지 — 클라이언트가 닫을 때까지 대기
        while True:
            try:
                data = await websocket.receive_json()
                # config 업데이트 지원
                if data.get("type") == "update_config":
                    config = StreamConfig.model_validate(data.get("config", {}))
                    await pc_agent_manager.start_stream(agent_id, config)
            except (WebSocketDisconnect, Exception):
                break

    except (asyncio.TimeoutError, WebSocketDisconnect, Exception) as exc:
        logger.info("stream_ws_closed agent_id=%s reason=%s", agent_id, exc)
    finally:
        remaining = pc_agent_manager.remove_stream_subscriber(agent_id, websocket)
        if remaining == 0:
            # 마지막 구독자 해제 시 스트리밍 중지
            try:
                await pc_agent_manager.stop_stream(agent_id)
            except Exception:
                pass
            logger.info("stream_stopped_no_subscribers agent_id=%s", agent_id)


@router.post("/pc-agent/stream/{agent_id}/start")
async def stream_start(agent_id: str, config: StreamConfig | None = None):
    """스트리밍 시작 (REST 폴백)."""
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

    if config is None:
        config = StreamConfig()

    try:
        command_id = await pc_agent_manager.start_stream(agent_id, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "streaming", "config": config.model_dump()}


@router.post("/pc-agent/stream/{agent_id}/stop")
async def stream_stop(agent_id: str):
    """스트리밍 중지."""
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

    try:
        command_id = await pc_agent_manager.stop_stream(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "stopped"}


@router.get("/pc-agent/health")
async def pc_agent_health():
    """PC Agent 서브시스템 상태."""
    await _flush_pending_reload_disconnects()
    _ensure_offline_monitor()
    agents = pc_agent_manager.list_agents()
    return {
        "connected": len(agents),
        "agents": [
            {
                "agent_id": a.agent_id,
                "hostname": a.hostname,
                "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            }
            for a in agents
        ],
    }


# ── PC Agent Offline 모니터링 (120초 이상 시 텔레그램 알림) ──────────────

_OFFLINE_MONITOR_TASK: asyncio.Task[Any] | None = None
_OFFLINE_ALERT_SENT = False
_OFFLINE_THRESHOLD = 120


async def _offline_monitor_loop() -> None:
    global _OFFLINE_ALERT_SENT
    await asyncio.sleep(60)
    while True:
        try:
            agents = pc_agent_manager.list_agents()
            if not agents:
                if not _OFFLINE_ALERT_SENT:
                    from app.core.db_pool import get_pool
                    pool = get_pool()
                    async with pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT created_at FROM pc_agent_connection_events "
                            "WHERE event = 'disconnected' ORDER BY id DESC LIMIT 1"
                        )
                    if row:
                        from datetime import timezone
                        elapsed = (datetime.now(timezone.utc) - row["created_at"]).total_seconds()
                        if elapsed >= _OFFLINE_THRESHOLD:
                            _OFFLINE_ALERT_SENT = True
                            try:
                                from app.services.telegram_bot import get_telegram_bot
                                bot = get_telegram_bot()
                                if bot:
                                    await bot.send_message(
                                        f"⚠️ PC Agent offline {int(elapsed)}초 경과\n"
                                        f"마지막 끊김: {row['created_at'].strftime('%H:%M KST')}"
                                    )
                            except Exception as e:
                                logger.warning("pc_agent_offline_alert_fail: %s", e)
            else:
                if _OFFLINE_ALERT_SENT:
                    _OFFLINE_ALERT_SENT = False
                    try:
                        from app.services.telegram_bot import get_telegram_bot
                        bot = get_telegram_bot()
                        if bot:
                            await bot.send_message("✅ PC Agent 재연결됨")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("offline_monitor_err: %s", e)
        await asyncio.sleep(30)


def _ensure_offline_monitor() -> None:
    global _OFFLINE_MONITOR_TASK
    if _OFFLINE_MONITOR_TASK is not None and not _OFFLINE_MONITOR_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _OFFLINE_MONITOR_TASK = loop.create_task(_offline_monitor_loop())
        logger.info("pc_agent_offline_monitor_started")
    except RuntimeError:
        pass


# ── Client log ingestion (v1.0.46) ─────────────────────────────────────
@router.post("/pc-agent/client-log")
async def ingest_client_log(payload: dict[str, Any]):
    """PC 런처/에이전트의 ERROR/WARNING 로그를 서버에 수집한다.

    payload 형식:
    {
      "agent_token": "<token>",
      "agent_id": "<optional id>",
      "source": "launcher" | "agent",
      "level": "ERROR" | "WARNING" | "INFO",
      "version": "1.0.46",
      "hostname": "DESKTOP-...",
      "message": "...",
      "ts": "2026-05-29T15:21:00+09:00"
    }
    """
    token = (payload.get("agent_token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="agent_token required")

    if not (token == PC_AGENT_SECRET or await _verify_token_db(token)):
        raise HTTPException(status_code=401, detail="invalid token")

    agent_id = (payload.get("agent_id") or "unknown")[:64]
    source = (payload.get("source") or "launcher")[:16]
    level = (payload.get("level") or "INFO")[:8]
    version = (payload.get("version") or "")[:32]
    hostname = (payload.get("hostname") or "")[:64]
    message = (payload.get("message") or "")[:4000]
    ts = payload.get("ts") or datetime.utcnow().isoformat()

    # 1) 서버 로그에 기록 — 즉시 원격 디버깅 가능
    logger.warning(
        "pc_agent_client_log src=%s lvl=%s agent=%s host=%s ver=%s ts=%s msg=%s",
        source, level, agent_id, hostname, version, ts, message,
    )

    # 2) 이벤트 테이블에 INSERT (best-effort)
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pc_agent_connection_events
                    (agent_id, event, reason, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                agent_id,
                f"client_log_{source}",
                f"{level}: {message[:200]}",
                json.dumps({
                    "source": source, "level": level, "version": version,
                    "hostname": hostname, "ts": ts, "message": message,
                }),
            )
    except Exception as e:
        logger.debug("client_log_db_insert_failed: %s", e)

    return {"ok": True}

