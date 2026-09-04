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
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from app.auth import ADMIN_EMAIL, extract_aads_cookie_token, verify_token
from app.models.pc_agent import CommandRequest, RoutedCommandRequest, StreamConfig, WSMessage
from app.services.pc_agent_manager import pc_agent_manager

logger = logging.getLogger(__name__)
router = APIRouter()

PC_AGENT_SECRET = os.environ.get("PC_AGENT_SECRET", "")
HEARTBEAT_INTERVAL = 30  # 초
_PEER_FALLBACK_HEADER = "x-aads-pc-agent-peer-fallback"
_PEER_OWNER_HEADER = "x-aads-pc-agent-owner-user-id"
_PEER_RETRYABLE_ERROR_CODES = {"PC_AGENT_OFFLINE", "NO_CAPABLE_AGENT"}
_DEFAULT_BROWSER_WORK_KEY = os.environ.get("PC_AGENT_DEFAULT_BROWSER_WORK_KEY", "aads-ceo-browser").strip() or "aads-ceo-browser"

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
# 같은 agent_id 동시 등록 시 레이스 컨디션 방지
_agent_connect_locks: dict[str, asyncio.Lock] = {}
if _prev_mod is not None and hasattr(_prev_mod, "_agent_connect_locks"):
    _agent_connect_locks = dict(getattr(_prev_mod, "_agent_connect_locks", {}))


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


def _classify_disconnect_cause(
    *,
    close_code: int | None,
    close_reason: str | None,
    uptime_seconds: float,
    exc_type: str,
) -> dict[str, Any]:
    """끊김 원인을 분류하여 구조화된 진단 정보를 반환한다."""
    code = close_code or 0
    reason = str(close_reason or "").lower()
    cause = "unknown"
    severity = "warning"
    auto_recoverable = True

    if code == 1012 or "server_restart" in reason or "fast_reconnect" in reason:
        cause = "server_restart"
        severity = "info"
    elif code == 4010 or "replaced_by_new" in reason:
        cause = "duplicate_instance"
        severity = "info"
    elif code == 4001 or "unauthorized" in reason:
        cause = "auth_failure"
        severity = "critical"
        auto_recoverable = False
    elif "sleep_wake" in reason:
        cause = "pc_sleep_wake"
        severity = "info"
    elif "heartbeat_timeout" in reason or exc_type == "TimeoutError":
        if uptime_seconds < 60:
            cause = "network_unstable"
            severity = "warning"
        else:
            cause = "heartbeat_timeout"
            severity = "warning"
    elif code in (1000, 1001, 1005):
        cause = "normal_close"
        severity = "info"
    elif code in (1006,):
        cause = "abnormal_close"
        severity = "warning"
    elif "hot_reload" in reason:
        cause = "hot_reload"
        severity = "info"
    elif exc_type in ("ConnectionResetError", "BrokenPipeError"):
        cause = "network_error"
        severity = "warning"

    return {
        "cause": cause,
        "severity": severity,
        "auto_recoverable": auto_recoverable,
        "close_code": code,
        "close_reason": close_reason,
        "uptime_seconds": round(uptime_seconds, 1),
        "exc_type": exc_type,
    }


async def _notify_chat_session_disconnect(
    *,
    agent_id: str,
    classification: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    """PC Agent 끊김을 원장에 남기고 최근 FOOD/AADS 채팅 세션에 직접 보고한다."""
    cause = classification.get("cause", "unknown")
    severity = classification.get("severity", "warning")
    auto_recoverable = classification.get("auto_recoverable", True)
    uptime = classification.get("uptime_seconds", 0)

    observation = (
        f"PC Agent '{agent_id}' 연결 끊김 — "
        f"원인: {cause}, 심각도: {severity}, "
        f"자동복구 가능: {'예' if auto_recoverable else '아니오'}, "
        f"연결 유지 시간: {uptime:.0f}초. "
    )
    if not auto_recoverable:
        observation += "⚠️ 수동 조치 필요: 인증 실패 또는 설정 오류 확인 필요."
    elif cause == "heartbeat_timeout":
        observation += "CEO PC 네트워크 상태 또는 절전 모드 확인 권장."
    elif cause == "network_error":
        observation += "네트워크 불안정. PC Agent 트레이 아이콘 재연결 확인 권장."

    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_observations (project, category, key, value, created_at)
                VALUES ($1, $2, $3, $4::jsonb, NOW())
                """,
                "FOOD",
                "pc_agent_disconnect_alert",
                f"disconnect_{agent_id}_{cause}",
                json.dumps({
                    "agent_id": agent_id,
                    "cause": cause,
                    "severity": severity,
                    "auto_recoverable": auto_recoverable,
                    "observation": observation,
                    "classification": classification,
                    "uptime_seconds": uptime,
                }),
            )
        logger.info("pc_agent_disconnect_chat_notified agent_id=%s cause=%s", agent_id, cause)
    except Exception as exc:
        logger.warning("pc_agent_disconnect_chat_notify_failed: %s", exc)

    try:
        session_id = await _latest_pc_agent_alert_session_id()
        if session_id:
            from app.services.session_reporter import post_session_report

            title = f"PC Agent 연결 끊김 자동 알림: {agent_id}"
            body = (
                f"{observation}\n\n"
                "확인할 항목:\n"
                "- `/api/v1/pc-agent/diagnostics`로 최신 launcher/connection 상태 확인\n"
                "- `/api/v1/pc-agent/disconnect-stats`로 최근 24시간 원인 분포 확인\n"
                "- FOOD 자동수집 실행 중이면 중단 지점과 stale lock 여부 확인\n"
                "- 재연결 후 동일 작업을 재개할 수 있으면 세션 재사용으로 재시도"
            )
            reaction_prompt = (
                "[시스템] PC Agent 연결 끊김 자동 알림입니다. "
                "이 채팅 세션에서 diagnostics/disconnect-stats/수집 상태를 확인하고, "
                "가능한 조치를 실행한 뒤 CEO에게 원인과 조치 결과를 보고하세요.\n\n"
                f"agent_id={agent_id}\n"
                f"classification={json.dumps(classification, ensure_ascii=False)}\n"
                f"metadata={json.dumps(metadata, ensure_ascii=False)[:1200]}"
            )
            await post_session_report(
                session_id=session_id,
                title=title,
                body=body,
                status="warning" if severity != "critical" else "error",
                source="pc_agent_disconnect_monitor",
                project="FOOD",
                metadata={
                    "agent_id": agent_id,
                    "classification": classification,
                    "disconnect_metadata": metadata,
                    "auto_generated": True,
                },
                intent="pc_agent_alert",
                idempotency_key=(
                    f"pc-agent-disconnect-{agent_id}-{cause}-"
                    f"{metadata.get('close_code')}-{metadata.get('uptime_seconds')}"
                ),
                trigger_reaction=True,
                reaction_prompt=reaction_prompt,
            )
        else:
            logger.info("pc_agent_disconnect_session_report_skipped no_target_session agent_id=%s", agent_id)
    except Exception as exc:
        logger.warning("pc_agent_disconnect_session_report_failed: %s", exc)

    if severity == "critical":
        try:
            from app.services.telegram_bot import get_telegram_bot
            bot = get_telegram_bot()
            if bot:
                await bot.send_message(
                    f"🚨 PC Agent 치명적 끊김\n"
                    f"Agent: {agent_id}\n"
                    f"원인: {cause}\n"
                    f"자동복구 불가 — 수동 조치 필요"
                )
        except Exception:
            pass


async def _latest_pc_agent_alert_session_id() -> str:
    """Return the most relevant chat session for FOOD PC Agent alerts."""
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.id::text AS session_id
                FROM chat_sessions s
                JOIN chat_workspaces w ON w.id = s.workspace_id
                WHERE COALESCE(w.project_key, '') IN ('FOOD', 'AADS', 'CEO')
                ORDER BY
                    CASE COALESCE(w.project_key, '')
                        WHEN 'FOOD' THEN 0
                        WHEN 'AADS' THEN 1
                        WHEN 'CEO' THEN 2
                        ELSE 3
                    END,
                    s.updated_at DESC
                LIMIT 1
                """
            )
            return str(row["session_id"]) if row else ""
    except Exception as exc:
        logger.warning("pc_agent_alert_session_lookup_failed: %s", exc)
        return ""


async def _verify_token_db(token: str) -> tuple[bool, str, str]:
    """DB에서 토큰 유효성 검증 (kakao_pc_agent_tokens 테이블).

    반환값: (valid, owner_user_id, owner_tenant_id). is_active=false(레거시/비활성)
    토큰은 존재해도 무효 처리한다.
    """
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, tenant_id
                  FROM kakao_pc_agent_tokens
                 WHERE token = $1
                   AND COALESCE(is_active, TRUE) = TRUE
                """,
                token,
            )
            if row is not None:
                await conn.execute(
                    "UPDATE kakao_pc_agent_tokens SET last_used_at = NOW() WHERE token = $1",
                    token,
                )
                return True, str(row["user_id"] or "").strip(), str(row["tenant_id"] or "").strip()
        return False, "", ""
    except Exception as exc:
        logger.warning("pc_agent_token_db_check_failed: %s", exc)
        return False, "", ""


def _resolve_requester_auth_payload(request: Request) -> dict[str, Any] | None:
    """대시보드 요청의 Bearer/쿠키 토큰에서 JWT payload를 추출한다.

    FastAPI Depends(get_current_user)와 달리 인증 실패 시 예외를 던지지 않는다 —
    이 값은 PC Agent 목록/실행 API에서 "요청자 소유 필터링" 용도로만 쓰인다.
    """
    token = ""
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = extract_aads_cookie_token(request) or ""
    if not token:
        return ""
    try:
        payload = verify_token(token)
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    return payload


def _resolve_requester_user_id(request: Request) -> str:
    payload = _resolve_requester_auth_payload(request)
    if not payload:
        return ""
    return str(payload.get("sub") or "").strip()


def _requester_is_admin_principal(request: Request) -> bool:
    payload = _resolve_requester_auth_payload(request)
    if not payload:
        return False
    email = str(payload.get("email") or "").strip().lower()
    admin_email = str(ADMIN_EMAIL or "").strip().lower()
    return bool(payload.get("is_admin", False)) or bool(email and admin_email and email == admin_email)


def _is_trusted_internal_request(request: Request) -> bool:
    if request.headers.get(_PEER_FALLBACK_HEADER, "") == "1":
        return True
    monitor_key = (
        request.headers.get("x-monitor-key")
        or request.headers.get("X-Monitor-Key")
        or ""
    ).strip()
    service_key = os.getenv("AADS_MONITOR_KEY", "").strip()
    if monitor_key and service_key and monitor_key == service_key:
        return True
    client_host = getattr(getattr(request, "client", None), "host", "") or ""
    return (
        client_host in {"127.0.0.1", "::1", "localhost"}
        or client_host.startswith("10.")
        or client_host.startswith("172.")
        or client_host.startswith("192.168.")
    )


def _peer_owner_user_id(request: Request) -> str:
    if request.headers.get(_PEER_FALLBACK_HEADER, "") != "1":
        return ""
    return str(request.headers.get(_PEER_OWNER_HEADER, "") or "").strip()


def _request_user_scope(request: Request) -> tuple[str, bool]:
    requester_user_id = _resolve_requester_user_id(request) or _peer_owner_user_id(request)
    return requester_user_id, _is_trusted_internal_request(request)


def _request_access_scope(request: Request) -> tuple[str, bool, bool]:
    requester_user_id, is_internal_request = _request_user_scope(request)
    return requester_user_id, is_internal_request, _requester_is_admin_principal(request)


def _assert_agent_access(
    agent_id: str,
    requester_user_id: str,
    is_internal_request: bool,
    *,
    allow_all_agents: bool = False,
    allow_unowned_legacy_agent: bool = False,
) -> None:
    if allow_all_agents:
        return
    if is_internal_request and not requester_user_id:
        return
    if not requester_user_id:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")
    agent_owner_user_id = str(getattr(agent, "user_id", "") or "").strip()
    if allow_unowned_legacy_agent and not agent_owner_user_id:
        return
    if agent_owner_user_id != requester_user_id:
        raise HTTPException(status_code=403, detail="다른 사용자의 PC 에이전트에는 접근할 수 없습니다.")


def _resolve_websocket_user_id(websocket: WebSocket) -> str:
    token = ""
    try:
        token = str(websocket.query_params.get("auth_token") or websocket.query_params.get("access_token") or "").strip()
    except Exception:
        token = ""
    if not token:
        try:
            token = str((getattr(websocket, "cookies", {}) or {}).get("aads_token") or "").strip()
        except Exception:
            token = ""
    if not token:
        return ""
    try:
        payload = verify_token(token)
    except Exception:
        return ""
    if not payload:
        return ""
    return str(payload.get("sub") or "").strip()


# ── WebSocket ──────────────────────────────────────────────────────────

@router.websocket("/pc-agent/ws/{agent_id}")
async def ws_pc_agent(websocket: WebSocket, agent_id: str, token: str = Query("")):
    """PC 에이전트 WebSocket 연결."""
    await _flush_pending_reload_disconnects()

    # 인증: DB 토큰 → 환경변수 폴백
    token_valid = False
    owner_user_id = ""
    owner_tenant_id = ""
    if token:
        token_valid, owner_user_id, owner_tenant_id = await _verify_token_db(token)
        if not token_valid and PC_AGENT_SECRET and token == PC_AGENT_SECRET:
            token_valid = True
            owner_user_id = ""  # 공유 시크릿 연결은 소유자 미상 — 사용자별 목록에 노출되지 않음
    if not token_valid and not PC_AGENT_SECRET:
        token_valid = True
    if not token_valid:
        await websocket.close(code=4001, reason="unauthorized")
        logger.warning("pc_agent_ws_auth_failed agent_id=%s", agent_id)
        await _record_agent_event(agent_id, "auth_failed", reason="unauthorized")
        return

    # 같은 agent_id의 동시 연결 레이스 컨디션 방지
    if agent_id not in _agent_connect_locks:
        _agent_connect_locks[agent_id] = asyncio.Lock()
    connect_lock = _agent_connect_locks[agent_id]

    async with connect_lock:
        old_ws = _agent_connections.pop(agent_id, None)
        if old_ws is not None:
            try:
                await old_ws.close(code=4010, reason="replaced_by_new")
            except Exception:
                pass
            logger.info("pc_agent_ws_replaced agent_id=%s", agent_id)
            await _record_agent_event(agent_id, "replaced", reason="replaced_by_new")
            await asyncio.sleep(0.1)  # 이전 연결 정리 시간 확보

        await websocket.accept()
    _agent_connections[agent_id] = websocket
    connected_at = datetime.utcnow()
    logger.info("pc_agent_ws_connected agent_id=%s total=%d", agent_id, len(_agent_connections))
    await _record_agent_event(agent_id, "connected")
    disconnect_recorded = False
    disconnect_reason = ""
    disconnect_metadata: dict[str, Any] = {}

    def _categorize_disconnect(close_code: int | None, exc_type: str) -> str:
        """끊김 원인을 카테고리로 분류 — 진단/통계용."""
        if close_code == 1012:
            return "server_restart"
        if close_code == 4010:
            return "replaced_by_new_instance"
        if close_code in (1000, 1001):
            return "normal_close"
        if close_code in (1005, 1006):
            return "abnormal_close"
        if close_code == 1011:
            return "server_error"
        if exc_type == "TimeoutError" or close_code is None:
            return "heartbeat_timeout"
        return "unknown"

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
            "disconnect_category": _categorize_disconnect(close_code, exc_type),
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
        classification = _classify_disconnect_cause(
            close_code=disconnect_metadata.get("close_code"),
            close_reason=disconnect_metadata.get("close_reason"),
            uptime_seconds=disconnect_metadata.get("uptime_seconds", 0),
            exc_type=disconnect_metadata.get("exc_type", ""),
        )
        disconnect_metadata["classification"] = classification
        await _record_agent_event(
            agent_id,
            "disconnected",
            reason=disconnect_reason,
            metadata=disconnect_metadata,
        )
        if classification["severity"] in ("warning", "critical"):
            asyncio.ensure_future(_notify_chat_session_disconnect(
                agent_id=agent_id,
                classification=classification,
                metadata=disconnect_metadata,
            ))

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
        device_type = (msg.payload or {}).get("device_type", "pc")
        version = (msg.payload or {}).get("version", "")
        await _record_agent_event(
            agent_id, "connected",
            metadata={"device_type": device_type, "version": version, "user_id": owner_user_id or ""},
        )
        pc_agent_manager.register_agent(
            agent_id,
            websocket,
            msg.payload,
            owner_user_id=owner_user_id,
            owner_tenant_id=owner_tenant_id,
        )
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

def _top_level_reconnect_guidance(online_count: int) -> str:
    if online_count > 0:
        return "At least one PC agent heartbeat is healthy."
    return (
        "No healthy PC agent heartbeat is visible on this backend. "
        "If this is a standby/public node, retry the active backend or allow peer fallback to resolve the active agent."
    )


def _local_pc_agent_status_payload() -> dict[str, Any]:
    agents = pc_agent_manager.list_agent_statuses()
    online_count = sum(1 for agent in agents if agent.get("status") == "online")
    return {
        "status": "online" if online_count else "offline",
        "online_count": online_count,
        "agents": agents,
        "reconnect_guidance": _top_level_reconnect_guidance(online_count),
        "backend_source": "local",
    }


def _active_api_ports() -> list[str]:
    ports: list[str] = []
    active_port = _active_api_port()
    for candidate in (active_port, "8100", "8102"):
        if candidate and candidate.isdigit() and candidate not in ports:
            ports.append(candidate)
    return ports


def _active_api_port() -> str:
    for candidate in (
        os.getenv("AADS_ACTIVE_PORT", ""),
        "/root/aads/aads-server/.active_port",
        ".active_port",
    ):
        value = candidate.strip()
        if not value:
            continue
        if value.isdigit():
            return value
        try:
            with open(value, "r", encoding="utf-8") as fh:
                port = fh.read().strip()
        except OSError:
            continue
        if port.isdigit():
            return port
    return ""


def _active_container_name() -> str:
    for candidate in (
        os.getenv("AADS_ACTIVE_CONTAINER", ""),
        "/root/aads/aads-server/.active_container",
        "/app/.active_container",
        ".active_container",
    ):
        value = candidate.strip()
        if not value:
            continue
        if "/" not in value and "." not in value:
            return value
        try:
            with open(value, "r", encoding="utf-8") as fh:
                name = fh.read().strip()
        except OSError:
            continue
        if name:
            return name
    return ""


def _container_name_for_api_port(port: str) -> str:
    if port == "8100":
        return "aads-server"
    if port == "8102":
        return "aads-server-green"
    return ""


def _peer_fallback_urls(path: str) -> list[str]:
    urls: list[str] = []
    active_container = _active_container_name()
    local_container = str(os.getenv("AADS_CONTAINER_NAME", "") or "").strip()

    def add_url(url: str) -> None:
        if url and url not in urls:
            urls.append(url)

    for active_port in _active_api_ports():
        container_name = _container_name_for_api_port(active_port)
        if container_name and container_name != local_container:
            add_url(f"http://{container_name}:8080{path}")
        if not local_container:
            add_url(f"http://127.0.0.1:{active_port}{path}")
        if active_container and active_container != local_container:
            add_url(f"http://{active_container}:8080{path}")
    return urls


def _peer_fallback_allowed(request: Request) -> bool:
    return request.headers.get(_PEER_FALLBACK_HEADER, "") != "1"


def _normalize_browser_command_params(command_type: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(params or {})
    normalized_command = str(command_type or "").strip().lower()
    if not normalized_command.startswith("browser_"):
        return normalized
    normalized.setdefault("work_key", _DEFAULT_BROWSER_WORK_KEY)
    if normalized_command == "browser_launch":
        normalized.setdefault("new_window", False)
    if normalized_command == "browser_close_session":
        normalized.setdefault("close_browser", True)
        normalized.setdefault("close_tabs", True)
    return normalized


async def _request_peer_fallback_json(
    *,
    request: Request,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    if not _peer_fallback_allowed(request):
        return None

    urls = _peer_fallback_urls(path)
    if not urls:
        return None

    def _send() -> dict[str, Any] | None:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {_PEER_FALLBACK_HEADER: "1"}
        if owner_user_id:
            headers[_PEER_OWNER_HEADER] = owner_user_id
        if body is not None:
            headers["Content-Type"] = "application/json"

        for url in urls:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=180 if body is not None else 10) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                try:
                    parsed_error = json.loads(exc.read().decode("utf-8", errors="ignore"))
                except Exception:
                    logger.warning("pc_agent_peer_fallback_http_failed url=%s err=%s", url, exc)
                    continue
                detail = parsed_error.get("detail") if isinstance(parsed_error, dict) else None
                if isinstance(detail, dict):
                    error_code = str(detail.get("error_code") or "")
                    if error_code in _PEER_RETRYABLE_ERROR_CODES:
                        logger.warning(
                            "pc_agent_peer_fallback_retryable_error url=%s error_code=%s",
                            url,
                            error_code,
                        )
                        continue
                    detail.setdefault("backend_source", "peer")
                    return detail
                logger.warning("pc_agent_peer_fallback_http_bad_detail url=%s err=%s", url, exc)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning("pc_agent_peer_fallback_failed url=%s err=%s", url, exc)
                continue

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("pc_agent_peer_fallback_bad_json url=%s err=%s", url, exc)
                continue

            if not isinstance(parsed, dict):
                continue
            if method == "GET":
                online_count = int(parsed.get("online_count", 0) or 0)
                if path.endswith("/status") and online_count <= 0:
                    continue
                if path.endswith("/agents") and not parsed.get("agents"):
                    continue
                if path.endswith("/health") and int(parsed.get("connected", 0) or 0) <= 0:
                    continue
            else:
                if parsed.get("status") == "error" and str(parsed.get("error_code") or "") in _PEER_RETRYABLE_ERROR_CODES:
                    continue
            parsed.setdefault("backend_source", "peer")
            return parsed
        return None

    return await asyncio.to_thread(_send)


async def _peer_online_agents_snapshot() -> dict[str, Any] | None:
    """Return peer PC Agent status for monitor paths that do not have a Request."""
    urls = _peer_fallback_urls("/api/v1/pc-agent/agents")
    if not urls:
        return None

    def _lookup() -> dict[str, Any] | None:
        for url in urls:
            req = urllib.request.Request(url, headers={_PEER_FALLBACK_HEADER: "1"}, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    parsed = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                logger.warning("pc_agent_peer_monitor_lookup_failed url=%s err=%s", url, exc)
                continue
            if not isinstance(parsed, dict):
                continue
            agents = parsed.get("agents")
            online_agents = [
                agent for agent in agents if isinstance(agent, dict) and agent.get("status") == "online"
            ] if isinstance(agents, list) else []
            if online_agents:
                parsed["agents"] = online_agents
                parsed["online_count"] = len(online_agents)
                parsed.setdefault("backend_source", "peer")
                return parsed
        return None

    return await asyncio.to_thread(_lookup)

@router.get("/pc-agent/status")
async def pc_agent_status(request: Request):
    """PC Agent 연결 상태 요약 조회."""
    await _flush_pending_reload_disconnects()
    local_payload = _local_pc_agent_status_payload()
    if local_payload["online_count"] <= 0:
        peer_payload = await _request_peer_fallback_json(
            request=request,
            method="GET",
            path="/api/v1/pc-agent/status",
        )
        if peer_payload is not None:
            return peer_payload
    return local_payload


def _filter_agents_by_owner(
    agents: list[dict[str, Any]],
    requester_user_id: str,
    *,
    include_all_agents: bool = False,
) -> list[dict[str, Any]]:
    """요청자 소유 에이전트만 남긴다. requester_user_id가 비어 있으면 아무것도 노출하지 않는다
    (다른 사용자/미인증 요청에게 소유자 미상 에이전트가 보이지 않도록 기본 차단)."""
    if include_all_agents:
        return agents
    if not requester_user_id:
        return []
    return [a for a in agents if str(a.get("user_id") or "").strip() == requester_user_id]


@router.get("/pc-agent/agents")
async def list_agents(request: Request):
    """연결된 에이전트 목록 조회.

    AADS-다중PC격리: 대시보드 등 일반 요청은 요청자(user_id) 소유 에이전트만 반환한다.
    내부 blue/green 피어 페일오버 및 MCP 브리지 프로세스 간 조회(x-aads-pc-agent-peer-fallback
    헤더로 식별되는 신뢰된 내부 호출)는 기존과 동일하게 전체 목록을 반환한다 — 이 호출자들은
    "다른 사용자"가 아니라 같은 백엔드의 내부 프로세스이기 때문이다.
    """
    await _flush_pending_reload_disconnects()
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)

    agents = pc_agent_manager.list_agent_statuses()
    if is_admin_principal:
        agents = _filter_agents_by_owner(agents, requester_user_id, include_all_agents=True)
    elif requester_user_id or not is_internal_call:
        agents = _filter_agents_by_owner(agents, requester_user_id)
    online_count = sum(1 for agent in agents if agent.get("status") == "online")
    if online_count <= 0:
        peer_payload = await _request_peer_fallback_json(
            request=request,
            method="GET",
            path="/api/v1/pc-agent/agents",
            owner_user_id="" if is_admin_principal else requester_user_id,
        )
        if peer_payload is not None:
            if is_admin_principal:
                peer_payload = dict(peer_payload)
                peer_payload["online_count"] = sum(
                    1 for a in peer_payload.get("agents", []) if isinstance(a, dict) and a.get("status") == "online"
                )
            elif requester_user_id or not is_internal_call:
                peer_agents = peer_payload.get("agents") or []
                peer_payload = dict(peer_payload)
                peer_payload["agents"] = _filter_agents_by_owner(peer_agents, requester_user_id)
                peer_payload["online_count"] = sum(
                    1 for a in peer_payload["agents"] if a.get("status") == "online"
                )
            return peer_payload
    return {
        "agents": agents,
        "online_count": online_count,
        "backend_source": "local",
    }


@router.post("/pc-agent/graceful-shutdown")
async def graceful_shutdown():
    """배포/재시작 전 모든 PC Agent WebSocket을 정상 종료한다.
    클라이언트가 1012 코드를 받으면 즉시 재연결을 시도한다."""
    await _flush_pending_reload_disconnects()
    closed = await pc_agent_manager.close_all_connections(reason="server_restart")
    _agent_connections.clear()
    return {"closed": closed, "message": f"{closed}개 연결 정상 종료"}


@router.post("/pc-agent/execute")
async def execute_command(req: CommandRequest, request: Request):
    """에이전트에 명령 실행 요청.

    AADS-다중PC격리: 요청자 인증 후, 대상 agent_id의 소유자(user_id)와 일치하는 경우에만
    실행을 허용한다. 소유자가 없는(레거시/공유 시크릿) 에이전트는 기존 동작을 유지한다.
    """
    agent = pc_agent_manager.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{req.agent_id}'가 연결되어 있지 않습니다.")

    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    _assert_agent_access(
        req.agent_id,
        requester_user_id,
        is_internal_call,
        allow_all_agents=is_admin_principal,
        allow_unowned_legacy_agent=is_admin_principal,
    )

    params = _normalize_browser_command_params(req.command_type, req.params)
    try:
        command_id = await pc_agent_manager.send_command(
            req.agent_id, req.command_type, params
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "pending"}


@router.post("/pc-agent/route-execute")
async def route_execute_command(req: RoutedCommandRequest, request: Request):
    """Capability 기반 라우팅 + lease/queue 제어로 명령 실행."""
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    if not requester_user_id and not is_internal_call:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    params = _normalize_browser_command_params(req.command_type, req.params)
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
        params["evaluate_timeout_seconds"] = max(1.0, min(60.0, effective_command_timeout_seconds - 0.5))

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
        wait_for_agent_seconds=req.wait_for_agent_seconds,
        owner_user_id="" if is_admin_principal else requester_user_id,
    )
    if result.get("status") == "error" and str(result.get("error_code") or "") in _PEER_RETRYABLE_ERROR_CODES:
        peer_result = await _request_peer_fallback_json(
            request=request,
            method="POST",
            path="/api/v1/pc-agent/route-execute",
            payload={
                "command_type": req.command_type,
                "params": params,
                "agent_id": req.agent_id,
                "job_type": req.job_type,
                "required_capabilities": req.required_capabilities,
                "queue_if_busy": req.queue_if_busy,
                "wait_for_turn": req.wait_for_turn,
                "queue_wait_timeout_seconds": req.queue_wait_timeout_seconds,
                "lease_ttl_seconds": req.lease_ttl_seconds,
                "command_timeout_seconds": effective_command_timeout_seconds,
            },
            owner_user_id="" if is_admin_principal else requester_user_id,
        )
        if peer_result is not None:
            result = peer_result
    if result.get("status") == "error":
        error_code = str(result.get("error_code", "") or "")
        status_code = 409 if error_code in {"AGENT_BUSY", "LEASE_EXPIRED"} else 503
        if error_code in {"NO_CAPABLE_AGENT"}:
            status_code = 422
        if error_code in {"AGENT_FORBIDDEN"}:
            status_code = 403
        if error_code in {"COMMAND_TIMEOUT", "RUNTIME_EVALUATE_TIMEOUT"}:
            status_code = 504
        if error_code in {"CDP_NOT_READY", "STALE_TARGET", "SYNTAX_ERROR", "SPA_SHELL_ONLY", "VVIC_LOGIN_REQUIRED", "VVIC_BLOCKED"}:
            status_code = 424
        raise HTTPException(status_code=status_code, detail=result)
    return result


@router.get("/pc-agent/leases")
async def list_pc_agent_leases(
    request: Request,
    agent_id: str = Query(""),
    job_type: str = Query(""),
    status: str = Query(""),
):
    """현재 lease/queue 상태 조회."""
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    if not requester_user_id and not is_internal_call:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    if agent_id:
        _assert_agent_access(
            agent_id,
            requester_user_id,
            is_internal_call,
            allow_all_agents=is_admin_principal,
            allow_unowned_legacy_agent=is_admin_principal,
        )
    leases = await pc_agent_manager.list_leases(
        agent_id=agent_id,
        job_type=job_type,
        status=status,
        owner_user_id="" if is_admin_principal else requester_user_id,
    )
    return {"leases": leases, "count": len(leases)}


@router.get("/pc-agent/leases/{lease_id}")
async def get_pc_agent_lease(lease_id: str, request: Request):
    """특정 lease 상태 조회."""
    lease = await pc_agent_manager.get_lease(lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail={"error_code": "LEASE_EXPIRED", "message": "lease not found"})
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    _assert_agent_access(
        str(lease.get("agent_id") or ""),
        requester_user_id,
        is_internal_call,
        allow_all_agents=is_admin_principal,
        allow_unowned_legacy_agent=is_admin_principal,
    )
    return {"lease": lease}


@router.post("/pc-agent/leases/{lease_id}/heartbeat")
async def heartbeat_pc_agent_lease(
    request: Request,
    lease_id: str,
    extend_seconds: int = Query(180, ge=30, le=1800),
):
    """진행 중 lease heartbeat/만료 연장."""
    lease = await pc_agent_manager.get_lease(lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail={"error_code": "LEASE_EXPIRED", "message": "lease not found"})
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    _assert_agent_access(
        str(lease.get("agent_id") or ""),
        requester_user_id,
        is_internal_call,
        allow_all_agents=is_admin_principal,
        allow_unowned_legacy_agent=is_admin_principal,
    )
    result = await pc_agent_manager.heartbeat_lease(lease_id, extend_seconds=extend_seconds)
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/pc-agent/result/{command_id}")
async def get_result(request: Request, command_id: str, timeout: float = Query(30.0, ge=1.0, le=120.0)):
    """명령 실행 결과 조회 (대기)."""
    try:
        result = await pc_agent_manager.get_result(command_id, timeout=timeout)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    if result.agent_id:
        _assert_agent_access(
            result.agent_id,
            requester_user_id,
            is_internal_call,
            allow_all_agents=is_admin_principal,
            allow_unowned_legacy_agent=is_admin_principal,
        )
    elif not requester_user_id and not is_internal_call:
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return result.model_dump(mode="json")


# ── 스트리밍 ──────────────────────────────────────────────────────────

@router.websocket("/pc-agent/stream/{agent_id}")
async def ws_stream(websocket: WebSocket, agent_id: str):
    """대시보드 → 스트리밍 수신용 WebSocket."""
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        await websocket.close(code=4004, reason=f"agent '{agent_id}' not connected")
        return
    requester_user_id = _resolve_websocket_user_id(websocket)
    agent_owner_user_id = str(getattr(agent, "user_id", "") or "").strip()
    if not requester_user_id or agent_owner_user_id != requester_user_id:
        await websocket.close(code=4003, reason="forbidden")
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
async def stream_start(request: Request, agent_id: str, config: StreamConfig | None = None):
    """스트리밍 시작 (REST 폴백)."""
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    _assert_agent_access(
        agent_id,
        requester_user_id,
        is_internal_call,
        allow_all_agents=is_admin_principal,
        allow_unowned_legacy_agent=is_admin_principal,
    )

    if config is None:
        config = StreamConfig()

    try:
        command_id = await pc_agent_manager.start_stream(agent_id, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "streaming", "config": config.model_dump()}


@router.post("/pc-agent/stream/{agent_id}/stop")
async def stream_stop(request: Request, agent_id: str):
    """스트리밍 중지."""
    agent = pc_agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")
    requester_user_id, is_internal_call, is_admin_principal = _request_access_scope(request)
    _assert_agent_access(
        agent_id,
        requester_user_id,
        is_internal_call,
        allow_all_agents=is_admin_principal,
        allow_unowned_legacy_agent=is_admin_principal,
    )

    try:
        command_id = await pc_agent_manager.stop_stream(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"command_id": command_id, "status": "stopped"}


@router.get("/pc-agent/health")
async def pc_agent_health(request: Request):
    """PC Agent 서브시스템 상태."""
    await _flush_pending_reload_disconnects()
    _ensure_offline_monitor()
    agents = pc_agent_manager.list_agents()
    if not agents:
        peer_payload = await _request_peer_fallback_json(
            request=request,
            method="GET",
            path="/api/v1/pc-agent/health",
        )
        if peer_payload is not None:
            return peer_payload
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
                peer_payload = await _peer_online_agents_snapshot()
                if peer_payload is not None:
                    if _OFFLINE_ALERT_SENT:
                        _OFFLINE_ALERT_SENT = False
                    logger.debug(
                        "pc_agent_offline_monitor_peer_online count=%s source=%s",
                        peer_payload.get("online_count"),
                        peer_payload.get("backend_source"),
                    )
                    await asyncio.sleep(30)
                    continue
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
                            device_label = "Agent"
                            try:
                                meta = row.get("metadata") if hasattr(row, "get") else None
                                if isinstance(meta, dict):
                                    dt = meta.get("device_type", "")
                                    if dt:
                                        device_label = f"{dt.upper()} Agent"
                            except Exception:
                                pass
                            try:
                                from app.services.telegram_bot import get_telegram_bot
                                bot = get_telegram_bot()
                                if bot:
                                    await bot.send_message(
                                        f"⚠️ {device_label} offline {int(elapsed)}초 경과\n"
                                        f"마지막 끊김: {row['created_at'].strftime('%H:%M KST')}"
                                    )
                            except Exception as e:
                                logger.warning("pc_agent_offline_alert_fail: %s", e)

                            # P0-3: 채팅 세션 AI에게 PC Agent 끊김 알림 — ai_observations에 기록
                            try:
                                async with pool.acquire() as conn:
                                    last_event = await conn.fetchrow(
                                        """SELECT agent_id, reason, metadata::text as meta
                                        FROM pc_agent_connection_events
                                        WHERE event = 'disconnected'
                                        ORDER BY id DESC LIMIT 1"""
                                    )
                                    disconnect_detail = ""
                                    if last_event:
                                        disconnect_detail = (
                                            f"agent_id={last_event['agent_id']}, "
                                            f"reason={last_event['reason']}, "
                                            f"meta={last_event['meta'][:200]}"
                                        )
                                    await conn.execute(
                                        """INSERT INTO ai_observations
                                        (project, category, observation, confidence, metadata)
                                        VALUES ($1, $2, $3, $4, $5::jsonb)
                                        ON CONFLICT DO NOTHING""",
                                        "FOOD",
                                        "pc_agent_alert",
                                        f"⚠️ PC Agent {int(elapsed)}초 이상 오프라인. 원인: {disconnect_detail}. "
                                        f"조치 필요: CEO PC 트레이 아이콘 확인, 네트워크 상태 확인, "
                                        f"launcher 재시작 필요 여부 판단. 수집 작업 중단 상태.",
                                        0.95,
                                        json.dumps({
                                            "alert_type": "pc_agent_offline",
                                            "offline_seconds": int(elapsed),
                                            "disconnect_detail": disconnect_detail,
                                            "action_required": True,
                                            "auto_generated": True,
                                        }),
                                    )
                                logger.info("pc_agent_offline_chat_alert_sent elapsed=%ds", int(elapsed))
                            except Exception as alert_err:
                                logger.warning("pc_agent_offline_chat_alert_failed: %s", alert_err)
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
                    try:
                        from app.core.db_pool import get_pool
                        pool2 = get_pool()
                        async with pool2.acquire() as conn:
                            await conn.execute(
                                """UPDATE ai_observations
                                SET confidence = 0, metadata = metadata || '{"resolved": true}'::jsonb
                                WHERE project = 'FOOD' AND category = 'pc_agent_alert'
                                AND confidence > 0"""
                            )
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


@router.post("/pc-agent/launcher-status")
async def ingest_launcher_status(payload: dict[str, Any]):
    """Receive sanitized launcher/watchdog telemetry from a Windows node."""
    token = str(payload.get("agent_token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="agent_token required")
    token_valid, _, _ = await _verify_token_db(token)
    if not (token == PC_AGENT_SECRET or token_valid):
        raise HTTPException(status_code=401, detail="invalid token")

    agent_id = str(payload.get("agent_id") or "unknown")[:64]
    allowed = {
        "hostname", "version", "node_role", "launcher_pid", "launcher_uptime_seconds",
        "launcher_start_count", "worker_restart_count", "worker_connected",
        "worker_disconnected_seconds", "watchdog_task", "startup_registration", "reported_at",
    }
    metadata = {key: payload.get(key) for key in allowed if key in payload}
    metadata["device_type"] = "pc"
    await _record_agent_event(agent_id, "launcher_status", metadata=metadata)
    return {"ok": True, "agent_id": agent_id}


def _event_metadata_dict(value: Any) -> dict[str, Any]:
    """Normalize JSONB metadata across asyncpg codec configurations."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {"raw": value[:1000]}
        return dict(decoded) if isinstance(decoded, dict) else {"value": decoded}
    return {}


@router.get("/pc-agent/diagnostics")
async def pc_agent_diagnostics():
    """Return online agent details and the latest launcher telemetry per node."""
    launcher_rows: list[dict[str, Any]] = []
    connection_rows: list[dict[str, Any]] = []
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (agent_id) agent_id, created_at, metadata
                FROM pc_agent_connection_events
                WHERE event IN ('launcher_status', 'heartbeat_status')
                ORDER BY agent_id, created_at DESC
                """
            )
            latest_events = await conn.fetch(
                """
                SELECT DISTINCT ON (agent_id)
                       agent_id, event, reason, created_at, metadata
                FROM pc_agent_connection_events
                ORDER BY agent_id, created_at DESC
                """
            )
        launcher_rows = [
            {
                "agent_id": row["agent_id"],
                "reported_at": row["created_at"].isoformat(),
                "status": _event_metadata_dict(row["metadata"]),
            }
            for row in rows
        ]
        connection_rows = [
            {
                "agent_id": row["agent_id"],
                "event": row["event"],
                "reason": row["reason"] or "",
                "reported_at": row["created_at"].isoformat(),
                "metadata": _event_metadata_dict(row["metadata"]),
            }
            for row in latest_events
        ]
    except Exception as exc:
        logger.warning("pc_agent_diagnostics_query_failed: %s", exc)
    return {
        "online_agents": pc_agent_manager.list_agent_statuses(),
        "latest_launcher_status": launcher_rows,
        "latest_connection_events": connection_rows,
        "default_browser_agent": {
            "agent_id": os.getenv("PC_AGENT_DEFAULT_AGENT_ID", "").strip(),
            "hostname": os.getenv("PC_AGENT_DEFAULT_HOSTNAME", "").strip(),
        },
    }


@router.get("/pc-agent/disconnect-stats")
async def pc_agent_disconnect_stats():
    """최근 24시간 끊김 원인별 통계 — AI 진단용."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    metadata->>'classification' as classification_raw,
                    reason,
                    created_at AT TIME ZONE 'Asia/Seoul' as kst,
                    metadata::text as meta
                FROM pc_agent_connection_events
                WHERE event = 'disconnected'
                  AND created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
        events = []
        cause_counts: dict[str, int] = {}
        for row in rows:
            classification = {}
            try:
                raw = row["classification_raw"]
                if raw:
                    classification = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                pass
            cause = classification.get("cause", "unknown") if classification else "legacy_no_classification"
            cause_counts[cause] = cause_counts.get(cause, 0) + 1
            events.append({
                "reason": row["reason"],
                "cause": cause,
                "severity": classification.get("severity", "unknown"),
                "kst": row["kst"].isoformat() if row["kst"] else None,
            })
        return {
            "period": "24h",
            "total_disconnects": len(events),
            "cause_distribution": cause_counts,
            "events": events[:20],
        }
    except Exception as exc:
        logger.warning("pc_agent_disconnect_stats_failed: %s", exc)
        return {"error": str(exc), "events": []}


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

    token_valid, _, _ = await _verify_token_db(token)
    if not (token == PC_AGENT_SECRET or token_valid):
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
                    "device_type": (payload.get("device_type") or "pc")[:16],
                }),
            )
    except Exception as e:
        logger.debug("client_log_db_insert_failed: %s", e)

    return {"ok": True}
