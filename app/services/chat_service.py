"""
AADS-170: CEO Chat-First 시스템 — 채팅 서비스 레이어
DB CRUD, 메시지 전송(SSE 스트리밍), 파일 업로드/다운로드 비즈니스 로직.
AADS-188C: Claude Agent SDK 통합 (execute/code_modify 인텐트 → SDK primary, bridge fallback).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import asyncpg
from anthropic import APIStatusError
from app.config import Settings
from app.core.anthropic_client import get_client
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

# P2-2: 분기 모드에서 AI 응답에 branch_id를 자동 부여하기 위한 ContextVar
from contextvars import ContextVar as _ContextVar
_current_branch_id: _ContextVar[Optional[str]] = _ContextVar("_current_branch_id", default=None)


# ── SSE heartbeat wrapper ─────────────────────────────────────────
import asyncio as _heartbeat_asyncio


async def with_heartbeat(
    gen: AsyncGenerator[str, None],
    interval: float = 5.0,
) -> AsyncGenerator[str, None]:
    """Wrap an SSE async generator to interleave heartbeat events.

    If the inner generator hasn't yielded anything for *interval* seconds,
    a lightweight ``{"type": "heartbeat"}`` SSE line is emitted so that
    Cloudflare(100s)/Nginx/frontend can keep the connection alive.

    interval=5s → Cloudflare 100s 유휴 타임아웃 대비 충분한 여유 (P0-FIX: 8s→5s).
    """
    HEARTBEAT = f'data: {json.dumps({"type": "heartbeat"})}\n\n'
    ait = gen.__aiter__()
    pending: _heartbeat_asyncio.Task | None = None
    while True:
        if pending is None:
            pending = _heartbeat_asyncio.ensure_future(ait.__anext__())
        try:
            chunk = await _heartbeat_asyncio.wait_for(
                _heartbeat_asyncio.shield(pending), timeout=interval,
            )
            pending = None  # consumed — get next on next iteration
            yield chunk
        except _heartbeat_asyncio.TimeoutError:
            yield HEARTBEAT  # pending is still running, will retry
        except StopAsyncIteration:
            break
        except Exception as exc:
            logger.warning(f"with_heartbeat inner generator error: {type(exc).__name__}: {exc}", exc_info=True)
            # 에러도 SSE로 전달 후 종료 (조용히 삼키지 않음)
            yield f'data: {json.dumps({"type": "error", "content": f"Stream error: {type(exc).__name__}", "recoverable": True})}\n\n'
            break

# ── Background completion wrapper (Queue 기반) ────────────────────
# 클라이언트 SSE 연결이 끊겨도 LLM 생성을 백그라운드에서 완료하여 DB에 저장.
# 핵심: 생성 태스크(producer)와 SSE 전송(consumer)을 asyncio.Queue로 분리.
# 클라이언트 disconnect → consumer만 중단, producer는 독립적으로 계속 실행.
_active_bg_tasks: Dict[str, _heartbeat_asyncio.Task] = {}
# 스트리밍 중간 상태 추적: session_id → {content, tool_count, last_tool, updated_at}
_streaming_state: Dict[str, Dict[str, Any]] = {}
# 클라이언트 이탈 후 자동 종료 시간 (초)
_BG_AUTO_CANCEL_SEC = int(os.getenv("BG_AUTO_CANCEL_SEC", "300"))  # 5분

_SENTINEL = object()  # Queue 종료 신호

import time as _bg_time


async def _interim_save_streaming(session_id: str, state: Dict[str, Any]) -> None:
    """백그라운드 생성 중 중간 상태를 DB에 저장 (세션 이동 후 돌아왔을 때 보이도록).
    변경이 없으면 스킵 (1초 간격 호출 시 불필요한 DB write 방지).
    """
    try:
        content = state.get("content", "")
        tool_count = state.get("tool_count", 0)
        last_tool = state.get("last_tool", "")

        # 변경 감지: 이전 저장 내용과 동일하면 스킵
        _save_key = f"{len(content)}:{tool_count}:{last_tool}"
        if state.get("_last_save_key") == _save_key:
            return
        state["_last_save_key"] = _save_key
        # 스트리밍 중임을 나타내는 마커 + 현재 진행상황
        streaming_note = f"\n\n⏳ _생성 중... (도구 {tool_count}회 호출{', 최근: ' + last_tool if last_tool else ''})_"
        display_content = (content + streaming_note) if content else f"⏳ _AI가 응답을 생성 중입니다... (도구 {tool_count}회 호출 중)_"

        pool = get_pool()
        async with pool.acquire() as conn:
            # streaming placeholder 메시지가 있으면 UPDATE, 없으면 INSERT
            existing = await conn.fetchval(
                "SELECT id FROM chat_messages WHERE session_id = $1 AND role = 'assistant' AND intent = 'streaming_placeholder' ORDER BY created_at DESC LIMIT 1",
                uuid.UUID(session_id),
            )
            if existing:
                await conn.execute(
                    "UPDATE chat_messages SET content = $1, edited_at = NOW() WHERE id = $2",
                    display_content, existing,
                )
            else:
                await conn.execute(
                    """INSERT INTO chat_messages (session_id, role, content, intent, model_used)
                       VALUES ($1, 'assistant', $2, 'streaming_placeholder', 'streaming')""",
                    uuid.UUID(session_id), display_content,
                )
                await conn.execute(
                    "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
                    uuid.UUID(session_id),
                )
        logger.info(f"interim_save session={session_id[:8]} tools={tool_count} content_len={len(content)}")
    except Exception as e:
        logger.warning(f"interim_save_failed session={session_id[:8]}: {e}")


async def _delete_streaming_placeholder(session_id: str) -> None:
    """스트리밍 완료 후 placeholder 메시지 삭제 (최종 응답이 별도로 저장되므로).
    안전장치: 최종 응답이 없으면 placeholder를 최종 응답으로 전환."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            # placeholder 조회
            placeholder = await conn.fetchrow(
                "SELECT id, content FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder' ORDER BY created_at DESC LIMIT 1",
                uuid.UUID(session_id),
            )
            if not placeholder:
                return

            # 최종 응답이 이미 저장되었는지 확인 (placeholder 제외한 모든 assistant 메시지)
            final_exists = await conn.fetchval(
                """SELECT count(*) FROM chat_messages
                    WHERE session_id = $1 AND role = 'assistant'
                    AND intent IS DISTINCT FROM 'streaming_placeholder'
                    AND id != $2
                    AND created_at >= (SELECT created_at FROM chat_messages WHERE id = $2)""",
                uuid.UUID(session_id), placeholder['id'],
            )

            if final_exists and final_exists > 0:
                # 최종 응답 있음 → placeholder 안전하게 삭제
                await conn.execute(
                    "DELETE FROM chat_messages WHERE id = $1",
                    placeholder['id'],
                )
                await conn.execute(
                    "UPDATE chat_sessions SET message_count = GREATEST(message_count - 1, 0), updated_at = NOW() WHERE id = $1",
                    uuid.UUID(session_id),
                )
                logger.info(f"placeholder_deleted session={session_id[:8]}")
            else:
                # 최종 응답 없음 → placeholder를 최종 응답으로 전환 (응답 소실 방지)
                content = placeholder['content'] or ""
                # 마커 텍스트 제거 (⏳ 생성 중... 잔류 방지)
                content = re.sub(r'\n*⏳ _(?:생성 중|AI가 응답을 생성 중).*?_\s*$', '', content).rstrip()
                if content.strip():
                    await conn.execute(
                        "UPDATE chat_messages SET content = $2, intent = NULL, model_used = 'recovered' WHERE id = $1",
                        placeholder['id'], content,
                    )
                    logger.warning(f"placeholder_promoted session={session_id[:8]} — final save missing, placeholder promoted to response")
                else:
                    # 내용도 없으면 삭제
                    await conn.execute("DELETE FROM chat_messages WHERE id = $1", placeholder['id'])
                    await conn.execute(
                        "UPDATE chat_sessions SET message_count = GREATEST(message_count - 1, 0), updated_at = NOW() WHERE id = $1",
                        uuid.UUID(session_id),
                    )
                    logger.warning(f"empty_placeholder_deleted session={session_id[:8]}")
    except Exception as e:
        logger.warning(f"delete_placeholder_failed session={session_id[:8]}: {e}")


async def with_background_completion(
    gen: AsyncGenerator[str, None],
    session_id: str,
) -> AsyncGenerator[str, None]:
    """SSE generator를 Queue 기반으로 감싸서, 클라이언트 연결 종료에도 DB 저장 보장.

    동작 방식:
    1. Producer task: gen을 소비하여 Queue에 chunk를 넣음 (독립 태스크)
    2. Heartbeat task: 8초 간격 독립 heartbeat → Queue에 직접 주입 (도구 실행 중에도 보장)
    3. Consumer (이 generator): Queue에서 읽어 yield (SSE 전달)
    4. 클라이언트 disconnect → consumer만 중단, producer는 계속 실행 → DB 저장 완료
    5. 클라이언트 disconnect 후 10초마다 중간 결과를 DB에 저장 (세션 돌아왔을 때 보임)
    """
    queue: _heartbeat_asyncio.Queue = _heartbeat_asyncio.Queue(maxsize=500)
    _client_gone = False
    # heartbeat 정지 신호 — producer 완료 또는 client disconnect 시 set
    _hb_stop = _heartbeat_asyncio.Event()

    # 스트리밍 상태 초기화
    state: Dict[str, Any] = {"content": "", "tool_count": 0, "last_tool": "", "last_save": _bg_time.monotonic(), "started_at": _bg_time.monotonic()}
    _streaming_state[session_id] = state

    _client_gone_since: float = 0  # 클라이언트 이탈 시각 (monotonic)

    async def _producer():
        nonlocal _client_gone, _client_gone_since
        _my_task = _heartbeat_asyncio.current_task()
        try:
            async for chunk in gen:
                await queue.put(chunk)
                # SSE 이벤트 파싱하여 상태 추적
                if 'data: {' in chunk:
                    try:
                        _d = json.loads(chunk[chunk.index('{'):chunk.rstrip().rindex('}') + 1])
                        _t = _d.get("type", "")
                        if _t == "delta":
                            state["content"] += _d.get("content", "")
                        elif _t == "tool_use":
                            state["tool_count"] += 1
                            state["last_tool"] = _d.get("tool_name", "")
                        elif _t == "tool_result":
                            state["last_tool"] = _d.get("tool_name", "")
                    except Exception:
                        pass

                # 클라이언트 연결 중 3초마다 중간 저장 (Invisible Recovery: 10s→3s, 끊김 후 partial_content 실시간성)
                if not _client_gone:
                    _now_rt = _bg_time.monotonic()
                    if _now_rt - state["last_save"] > 3:
                        state["last_save"] = _now_rt
                        await _interim_save_streaming(session_id, state)

                # 클라이언트 disconnect 후 처리
                if _client_gone:
                    now = _bg_time.monotonic()
                    # 1초마다 중간 저장
                    if now - state["last_save"] > 1:
                        state["last_save"] = now
                        await _interim_save_streaming(session_id, state)
                    # 클라이언트 이탈 후 _BG_AUTO_CANCEL_SEC(5분) 경과 시 자동 중단
                    if _client_gone_since and (now - _client_gone_since) > _BG_AUTO_CANCEL_SEC:
                        logger.warning(f"bg_auto_cancel: session={session_id[:8]} client gone for {now - _client_gone_since:.0f}s, auto-stopping")
                        await _interim_save_streaming(session_id, state)
                        return  # producer 종료 → finally에서 cleanup
        except BaseException as e:
            # BaseException: CancelledError, GeneratorExit 등 모두 잡음
            import traceback as _tb
            logger.warning(f"bg_producer_error session={session_id}: {type(e).__name__}: {e}\n{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
        finally:
            # heartbeat 태스크 정지 신호 (producer 완료 시 heartbeat 불필요)
            _hb_stop.set()
            try:
                await queue.put(_SENTINEL)
            except Exception:
                pass
            if _active_bg_tasks.get(session_id) is _my_task:
                _active_bg_tasks.pop(session_id, None)
            # 🆕 bg_task 완료 후 대기 중인 trigger_ai_reaction 큐 소비
            if session_id in _ai_reaction_queue and _ai_reaction_queue[session_id]:
                _next_msg = _ai_reaction_queue[session_id].pop(0)
                if not _ai_reaction_queue[session_id]:
                    del _ai_reaction_queue[session_id]
                _ai_reaction_active[session_id] = _time.time()
                _heartbeat_asyncio.create_task(_consume_next_reaction(session_id, _next_msg))
            # 스트리밍 완료 → placeholder 삭제 (최종 응답이 generator 내부에서 저장됨)
            try:
                await _delete_streaming_placeholder(session_id)
            except Exception as del_err:
                logger.warning(f"bg_producer_placeholder_delete_err session={session_id}: {del_err}")
            # 상태를 즉시 삭제하지 않고 completed로 전환 (세션 복귀 시 감지용, 90초 후 자동 정리)
            if session_id in _streaming_state:
                _streaming_state[session_id]["completed"] = True
                _streaming_state[session_id]["completed_at"] = _bg_time.monotonic()

                async def _delayed_cleanup(sid: str):
                    await _heartbeat_asyncio.sleep(300)  # 90s→300s: 장시간 도구 실행 후 just_completed 감지 여유
                    _streaming_state.pop(sid, None)
                    logger.debug(f"streaming_state_cleaned session={str(sid)[:8]}")

                _heartbeat_asyncio.create_task(_delayed_cleanup(session_id))
            logger.info(f"bg_producer_done session={session_id}")

    # 기존 태스크가 있으면 취소 후 교체
    old_task = _active_bg_tasks.pop(session_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
        logger.info(f"bg_task_replaced session={session_id}")

    # BUG-SESSION-MIX FIX: 새 producer 시작 전 잔류 streaming_placeholder 즉시 삭제
    # — old producer의 finally 정리보다 new producer 시작이 빠르면 이전 응답이 잔류
    try:
        _pool = get_pool()
        async with _pool.acquire() as _conn:
            _del_count = await _conn.fetchval(
                "SELECT count(*) FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder'",
                uuid.UUID(session_id),
            )
            if _del_count and _del_count > 0:
                await _conn.execute(
                    "DELETE FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder'",
                    uuid.UUID(session_id),
                )
                await _conn.execute(
                    "UPDATE chat_sessions SET message_count = GREATEST(message_count - $1, 0), updated_at = NOW() WHERE id = $2",
                    _del_count, uuid.UUID(session_id),
                )
                logger.info(f"stale_placeholder_cleaned session={session_id[:8]} count={_del_count}")
    except Exception as _clean_err:
        logger.warning(f"stale_placeholder_clean_failed session={session_id[:8]}: {_clean_err}")

    task = _heartbeat_asyncio.create_task(_producer())
    _active_bg_tasks[session_id] = task

    # ── 독립 heartbeat task (P0-FIX: 도구 실행 30s+ 시에도 연결 유지) ──────
    # producer/도구 블로킹과 완전 분리된 asyncio.Task에서 5초마다 heartbeat 전송.
    # 3중 안전장치: (1) heartbeat_pump → queue, (2) consumer timeout → 직접 yield,
    # (3) pump 비정상 종료 시 자동 재시작.
    _HB_INTERVAL = 5.0  # 기본 5초: Cloudflare 100s/nginx 600s 대비 충분한 여유
    _HB_INTERVAL_TOOL = 2.0  # 도구 실행 중 2초: 긴 도구 실행 시 연결 안정성 강화
    _HB_LINE = f'data: {json.dumps({"type": "heartbeat"})}\n\n'

    async def _heartbeat_pump():
        """Adaptive heartbeat — 도구 실행 중 2초, 평시 5초 간격으로 queue에 heartbeat 삽입.
        도구 실행 중에는 tool_count/last_tool 포함 → 프론트 timeout 리셋 + 진행상황 표시."""
        while not _hb_stop.is_set():
            try:
                # Adaptive: 도구 실행 중이면 2초, 아니면 5초
                _tc = state.get("tool_count", 0)
                _lt = state.get("last_tool", "")
                _interval = _HB_INTERVAL_TOOL if (_tc > 0 and _lt) else _HB_INTERVAL
                await _heartbeat_asyncio.wait_for(_hb_stop.wait(), timeout=_interval)
                break  # stop 신호 수신 → 정상 종료
            except _heartbeat_asyncio.TimeoutError:
                if _client_gone:
                    break  # 클라이언트 이탈 시 heartbeat 불필요
                try:
                    _tc = state.get("tool_count", 0)
                    _lt = state.get("last_tool", "")
                    if _tc > 0 and _lt:
                        _hb_data = f'data: {json.dumps({"type": "heartbeat", "tool_count": _tc, "last_tool": _lt})}\n\n'
                    else:
                        _hb_data = _HB_LINE
                    queue.put_nowait(_hb_data)
                except Exception:
                    pass
            except Exception as _hb_exc:
                logger.warning(f"heartbeat_pump_error session={session_id[:8]}: {_hb_exc}")
                await _heartbeat_asyncio.sleep(1)
        logger.debug(f"heartbeat_pump_done session={session_id[:8]}")

    hb_task = _heartbeat_asyncio.create_task(_heartbeat_pump())

    # SSE retry 헤더: 클라이언트 자동 재연결 간격 3초 (EventSource 표준)
    yield f"retry: 3000\n\n"
    # P0-FIX: 초기 heartbeat 즉시 전송 — 연결 수립 직후 SSE 채널 활성화 확인
    yield _HB_LINE

    # Consumer: Queue에서 읽어서 yield — 클라이언트 disconnect 시 자연스럽게 종료
    # queue.get()에 timeout을 걸어 heartbeat_pump 실패 시에도 직접 heartbeat 전송 (이중 안전)
    try:
        while True:
            # P0-FIX: pump 비정상 종료 감지 → 자동 재시작
            if hb_task.done() and not _hb_stop.is_set() and not _client_gone:
                logger.warning(f"heartbeat_pump_died session={session_id[:8]}, restarting")
                hb_task = _heartbeat_asyncio.create_task(_heartbeat_pump())
            try:
                # Adaptive consumer timeout: 도구 실행 중 2초, 평시 5초 (pump과 동기)
                _c_tc = state.get("tool_count", 0)
                _c_lt = state.get("last_tool", "")
                _c_interval = _HB_INTERVAL_TOOL if (_c_tc > 0 and _c_lt) else _HB_INTERVAL
                item = await _heartbeat_asyncio.wait_for(queue.get(), timeout=_c_interval)
            except _heartbeat_asyncio.TimeoutError:
                # heartbeat_pump이 큐에 넣지 못한 경우 → consumer에서 직접 heartbeat yield
                yield _HB_LINE
                continue
            if item is _SENTINEL:
                break
            yield item
    except (GeneratorExit, _heartbeat_asyncio.CancelledError):
        _client_gone = True
        _client_gone_since = _bg_time.monotonic()
        # 즉시 중간 저장 (돌아왔을 때 바로 보이도록)
        _heartbeat_asyncio.create_task(_interim_save_streaming(session_id, state))
        logger.info(f"client_disconnected session={session_id} — producer continues in background (auto-cancel in {_BG_AUTO_CANCEL_SEC}s), interim save triggered")
    finally:
        _hb_stop.set()
        hb_task.cancel()


def get_active_bg_tasks() -> Dict[str, bool]:
    """현재 백그라운드 진행 중인 세션 목록 (health check / 디버그용).
    AADS-CRITICAL-FIX #4: 완료된 태스크 자동 정리."""
    done_sids = [sid for sid, task in _active_bg_tasks.items() if task.done()]
    for sid in done_sids:
        _active_bg_tasks.pop(sid, None)
    return {sid: not task.done() for sid, task in _active_bg_tasks.items()}


async def stop_session_streaming(session_id: str) -> Dict[str, Any]:
    """세션의 진행 중인 스트리밍을 강제 중단하고 현재까지 결과를 반환.

    Returns:
        {"stopped": True/False, "content": "현재까지 내용", "tool_count": N}
    """
    task = _active_bg_tasks.get(session_id)
    state = _streaming_state.get(session_id, {})
    result = {
        "stopped": False,
        "content": state.get("content", ""),
        "tool_count": state.get("tool_count", 0),
        "last_tool": state.get("last_tool", ""),
    }

    if task and not task.done():
        # BUG-2 FIX: 부분 응답을 DB에 저장 (유실 방지)
        partial_content = result["content"]
        if partial_content.strip():
            try:
                pool = get_pool()
                sid = uuid.UUID(session_id)
                stopped_content = partial_content.strip() + "\n\n_(응답이 중지되었습니다)_"
                async with pool.acquire() as conn:
                    existing = await conn.fetchval(
                        "SELECT id FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder' ORDER BY created_at DESC LIMIT 1",
                        sid,
                    )
                    if existing:
                        await conn.execute(
                            "UPDATE chat_messages SET content = $1, intent = NULL, model_used = 'stopped' WHERE id = $2",
                            stopped_content, existing,
                        )
                    else:
                        await conn.execute(
                            """INSERT INTO chat_messages (session_id, role, content, model_used, intent)
                               VALUES ($1, 'assistant', $2, 'stopped', NULL)""",
                            sid, stopped_content,
                        )
                        await conn.execute(
                            "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
                            sid,
                        )
                logger.info(f"stop_partial_saved session={session_id[:8]} len={len(partial_content)}")
            except Exception as save_err:
                logger.warning(f"stop_partial_save_failed session={session_id[:8]}: {save_err}")

        # 태스크 취소
        task.cancel()
        try:
            await _heartbeat_asyncio.wait_for(
                _heartbeat_asyncio.shield(task), timeout=3.0
            )
        except (_heartbeat_asyncio.CancelledError, _heartbeat_asyncio.TimeoutError, Exception):
            pass
        _active_bg_tasks.pop(session_id, None)
        _streaming_state.pop(session_id, None)
        # placeholder가 이미 최종 응답으로 교체되었으므로 삭제 불필요
        # (위에서 intent=NULL로 변경했으므로 _delete_streaming_placeholder는 아무것도 안 함)
        result["stopped"] = True
        logger.info(f"session_streaming_stopped session={session_id[:8]} content_len={len(partial_content)} tools={result['tool_count']}")
    else:
        # 이미 완료되었거나 존재하지 않음
        _active_bg_tasks.pop(session_id, None)
        _streaming_state.pop(session_id, None)
        result["stopped"] = False

    return result


async def resume_interrupted_streams() -> int:
    """서버 재시작 후 중단된 스트리밍을 자동 이어서 생성.

    streaming_placeholder가 남은 세션을 찾아서:
    1. placeholder의 중간 결과를 보존
    2. 마지막 user 메시지 기반으로 이어서 생성
    3. 완료 후 placeholder를 최종 응답으로 교체

    Returns: 이어서 생성한 세션 수
    """
    resumed = 0
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            # streaming_placeholder가 남은 세션 + 마지막 user 메시지 조회
            rows = await conn.fetch("""
                SELECT DISTINCT ON (m.session_id)
                    m.session_id,
                    m.id AS placeholder_id,
                    m.content AS partial_content,
                    (SELECT content FROM chat_messages
                     WHERE session_id = m.session_id AND role = 'user'
                     ORDER BY created_at DESC LIMIT 1) AS last_user_msg,
                    (SELECT name FROM chat_workspaces w
                     JOIN chat_sessions s ON s.workspace_id = w.id
                     WHERE s.id = m.session_id) AS workspace_name
                FROM chat_messages m
                WHERE m.intent = 'streaming_placeholder'
                ORDER BY m.session_id, m.created_at DESC
            """)

        if not rows:
            return 0

        logger.info(f"resume_interrupted: found {len(rows)} interrupted session(s)")

        for row in rows:
            sid = str(row["session_id"])
            placeholder_id = row["placeholder_id"]
            partial = row["partial_content"] or ""
            last_user = row["last_user_msg"] or ""
            workspace = row["workspace_name"] or "CEO"

            if not last_user:
                # user 메시지 없으면 placeholder만 삭제
                async with pool.acquire() as c:
                    await c.execute("DELETE FROM chat_messages WHERE id = $1", placeholder_id)
                logger.info(f"resume_skip: session={str(sid)[:8]} no user message, placeholder removed")
                continue

            # 중간 결과에서 ⏳ 마커 제거
            clean_partial = re.sub(r'\n\n⏳ _.*?_', '', partial, flags=re.DOTALL)
            clean_partial = re.sub(r'^⏳ _[^\n]*_\s*', '', clean_partial)
            clean_partial = clean_partial.strip()

            try:
                # 이어서 생성
                import asyncio as _resume_asyncio
                _resume_asyncio.create_task(
                    _resume_single_stream(sid, placeholder_id, clean_partial, last_user, workspace)
                )
                resumed += 1
                logger.info(f"resume_launched: session={str(sid)[:8]} partial_len={len(clean_partial)}")
            except Exception as e:
                logger.warning(f"resume_launch_failed: session={str(sid)[:8]} error={e}")
                # 실패 시 placeholder를 중단 메시지로 교체
                async with pool.acquire() as c:
                    final = clean_partial + "\n\n⚠️ _서버 재시작으로 응답이 중단되었습니다. 다시 질문해주세요._" if clean_partial else "⚠️ _서버 재시작으로 응답이 중단되었습니다. 다시 질문해주세요._"
                    await c.execute(
                        "UPDATE chat_messages SET content = $1, intent = NULL, model_used = 'interrupted' WHERE id = $2",
                        final, placeholder_id,
                    )

    except Exception as e:
        logger.warning(f"resume_interrupted_streams_error: {e}")

    return resumed


async def _resume_single_stream(
    session_id: str,
    placeholder_id,
    partial_content: str,
    last_user_msg: str,
    workspace_name: str,
) -> None:
    """단일 세션의 중단된 스트리밍을 이어서 생성."""
    try:
        pool = get_pool()
        sid = uuid.UUID(session_id)

        # 1. 히스토리 로드 (placeholder 제외)
        async with pool.acquire() as conn:
            hist_rows = await conn.fetch("""
                SELECT role, content FROM (
                    SELECT role, content, created_at FROM chat_messages
                    WHERE session_id = $1
                      AND (is_compacted IS NULL OR is_compacted = false)
                      AND intent != 'streaming_placeholder'
                    ORDER BY created_at DESC LIMIT 30
                ) sub ORDER BY created_at ASC
            """, sid)

        raw_messages = [{"role": r["role"], "content": r["content"]} for r in hist_rows]

        # 2. 이어서 생성 프롬프트 구성
        resume_instruction = (
            "[시스템: 서버 재시작으로 이전 응답이 중단되었습니다. "
            "아래는 중단 전까지 생성된 부분 응답입니다. "
            "이어서 완성해주세요. 이미 작성된 내용을 반복하지 말고, 끊긴 지점부터 자연스럽게 이어 작성하세요.]\n\n"
            f"--- 중단된 부분 응답 ---\n{partial_content}\n--- 여기서 이어서 작성 ---"
        ) if partial_content else (
            "[시스템: 서버 재시작으로 이전 응답 생성이 시작되지 못했습니다. 처음부터 응답해주세요.]"
        )
        raw_messages.append({"role": "user", "content": resume_instruction})

        # 3. 컨텍스트 빌드
        from app.services.context_builder import build_messages_context
        # workspace에서 base_prompt 조회
        async with pool.acquire() as conn:
            sp_row = await conn.fetchrow("""
                SELECT w.system_prompt FROM chat_workspaces w
                JOIN chat_sessions s ON s.workspace_id = w.id
                WHERE s.id = $1
            """, sid)
        base_prompt = (sp_row["system_prompt"] if sp_row and sp_row["system_prompt"] else "")

        try:
            messages, system_prompt = await build_messages_context(
                workspace_name=workspace_name,
                session_id=session_id,
                raw_messages=raw_messages,
                base_system_prompt=base_prompt,
            )
        except Exception:
            system_prompt = base_prompt or "You are a helpful AI assistant."
            messages = raw_messages[-20:]

        # 4. LLM 호출 (도구 포함)
        from app.services.model_selector import call_stream
        from app.services.intent_router import IntentResult

        # BUG-4 FIX v3: 세션의 마지막 사용 모델로 이어서 생성 (CEO 선택 모델 유지)
        # 우선순위: 1) user 메시지의 model_override → 2) assistant model_used → 3) 워크스페이스 기본 모델
        _resume_model: Optional[str] = None
        try:
            async with pool.acquire() as conn:
                # 1순위: 마지막 user 메시지의 model_used (CEO가 선택한 model_override)
                _user_model = await conn.fetchval("""
                    SELECT model_used FROM chat_messages
                    WHERE session_id = $1
                      AND role = 'user'
                      AND model_used IS NOT NULL
                      AND model_used != ''
                    ORDER BY created_at DESC LIMIT 1
                """, sid)
                if _user_model:
                    from app.services.intent_router import get_model_for_override
                    _resume_model = get_model_for_override(_user_model)
                    logger.info(f"resume_model_from_user_override session={session_id[:8]} model={_resume_model}")
                else:
                    # 2순위: 마지막 assistant 메시지의 실제 사용 모델
                    _asst_model = await conn.fetchval("""
                        SELECT model_used FROM chat_messages
                        WHERE session_id = $1
                          AND role = 'assistant'
                          AND model_used IS NOT NULL
                          AND model_used NOT IN ('stopped', 'interrupted', 'semantic_cache', 'streaming')
                        ORDER BY created_at DESC LIMIT 1
                    """, sid)
                    if _asst_model:
                        _resume_model = _asst_model
                        logger.info(f"resume_model_from_assistant session={session_id[:8]} model={_resume_model}")

                # 3순위: 워크스페이스 settings에서 기본 모델 조회
                if not _resume_model:
                    _ws_settings = await conn.fetchval("""
                        SELECT w.settings FROM chat_workspaces w
                        JOIN chat_sessions s ON s.workspace_id = w.id
                        WHERE s.id = $1
                    """, sid)
                    if _ws_settings and isinstance(_ws_settings, dict):
                        _resume_model = _ws_settings.get("default_model")
                    if _resume_model:
                        logger.info(f"resume_model_from_workspace session={session_id[:8]} model={_resume_model}")
        except Exception as _model_err:
            logger.warning(f"resume_model_lookup_failed session={session_id[:8]}: {_model_err}")

        if not _resume_model:
            _resume_model = "claude-sonnet"
            logger.info(f"resume_model_fallback session={session_id[:8]} model={_resume_model}")

        intent_result = IntentResult(
            intent="status_check",
            model=_resume_model,
            use_tools=True,
            tool_group="all",
        )

        from app.services.tool_registry import ToolRegistry
        tools_for_api = ToolRegistry().get_tools("all")

        full_response = partial_content  # 기존 부분 응답에 이어붙임
        cost_usd = Decimal("0")
        tools_called = []

        async for event in call_stream(
            intent_result=intent_result,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools_for_api,
            model_override=_resume_model,
            session_id=session_id,
        ):
            etype = event.get("type", "")
            if etype == "delta":
                full_response += event.get("content", "")
            elif etype == "tool_use":
                tools_called.append(event["tool_name"])
            elif etype == "done":
                cost_usd = Decimal(str(event.get("cost", "0")))

        # 5. placeholder를 최종 응답으로 교체
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE chat_messages
                   SET content = $1, intent = NULL, model_used = $4,
                       cost = $2, edited_at = NOW()
                   WHERE id = $3""",
                full_response,
                cost_usd,
                placeholder_id,
                _resume_model,
            )
            await conn.execute(
                "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
                cost_usd, sid,
            )

        logger.info(
            f"resume_completed: session={session_id[:8]} "
            f"partial={len(partial_content)} final={len(full_response)} "
            f"tools={len(tools_called)} cost=${cost_usd}"
        )

    except Exception as e:
        logger.error(f"resume_single_stream_error: session={session_id[:8]} error={e}")
        # 실패 시 중단 메시지로 교체
        try:
            async with get_pool().acquire() as c:
                final = partial_content + "\n\n⚠️ _서버 재시작 후 이어서 생성에 실패했습니다. 다시 질문해주세요._" if partial_content else "⚠️ _서버 재시작 후 응답 생성에 실패했습니다. 다시 질문해주세요._"
                await c.execute(
                    "UPDATE chat_messages SET content = $1, intent = NULL, model_used = 'interrupted' WHERE id = $2",
                    final, placeholder_id,
                )
        except Exception:
            pass


def get_streaming_status(session_id: str) -> Optional[Dict[str, Any]]:
    """특정 세션의 스트리밍 상태 반환 (프론트엔드 폴링용).

    Returns:
        is_streaming: True if still generating
        just_completed: True if finished within last 30s (세션 복귀 시 즉시 메시지 reload 트리거)
        content_length: 현재까지 생성된 텍스트 길이
        tool_count: 도구 호출 횟수
        last_tool: 마지막 호출 도구 이름
    """
    _STREAMING_MAX_AGE_SEC = 600  # 10분 이상 된 streaming state 자동 만료

    if session_id in _streaming_state:
        s = _streaming_state[session_id]
        is_completed = s.get("completed", False)

        # STUCK 방지: 에러 content 감지 → 즉시 완료 처리
        _content = s.get("content", "")
        if not is_completed and "API Error:" in _content and "no_db_connection" in _content:
            logger.warning(f"streaming_state_error_detected session={session_id[:8]} — force completing")
            is_completed = True
            s["completed"] = True

        # STUCK 방지: 10분 이상 된 미완료 state 자동 만료
        if not is_completed:
            _started = s.get("started_at", 0)
            if _started and (_bg_time.monotonic() - _started) > _STREAMING_MAX_AGE_SEC:
                logger.warning(f"streaming_state_expired session={session_id[:8]} age={_bg_time.monotonic() - _started:.0f}s")
                is_completed = True
                s["completed"] = True

        result = {
            "is_streaming": not is_completed,
            "just_completed": is_completed,
            "content_length": len(_content),
            "token_count": len(_content) // 4,  # 근사 토큰 수 (프론트 진행도 판단용)
            "tool_count": s.get("tool_count", 0),
            "last_tool": s.get("last_tool", ""),
            "partial_content": _content,
        }
        # P1-FIX: just_completed=True 반환 후 즉시 state 제거 (one-shot)
        if is_completed:
            _streaming_state.pop(session_id, None)
        return result

    if session_id in _active_bg_tasks:
        task = _active_bg_tasks[session_id]
        if task.done():
            # 완료된 태스크 정리
            _active_bg_tasks.pop(session_id, None)
        else:
            return {"is_streaming": True, "just_completed": False, "content_length": 0, "token_count": 0, "tool_count": 0, "last_tool": ""}

    # 스트리밍 없음: 명시적 False 반환 → 프론트 폴링 즉시 중단
    return {"is_streaming": False, "just_completed": False, "content_length": 0, "token_count": 0, "tool_count": 0, "last_tool": ""}


# AADS-186C: Langfuse 트레이스 (optional — graceful degradation)
try:
    from app.core.langfuse_config import create_trace, is_enabled as langfuse_is_enabled
    _LANGFUSE_AVAILABLE = True
except ImportError:
    _LANGFUSE_AVAILABLE = False
    def create_trace(*args, **kwargs): return None  # type: ignore[misc]
    def langfuse_is_enabled() -> bool: return False  # type: ignore[misc]
settings = Settings()

# ─── DB 연결 ──────────────────────────────────────────────────────────────────

def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def _get_conn() -> asyncpg.Connection:
    """[DEPRECATED] async with get_pool().acquire() as conn: 패턴을 사용하세요.
    풀에서 커넥션 acquire. 호출자가 반드시 release해야 함.
    기존 코드 호환: conn = await _get_conn() → conn.close() 대신 pool.release(conn).
    """
    pool = get_pool()
    return await pool.acquire(timeout=10)


# ─── Anthropic 클라이언트 ──────────────────────────────────────────────────────

_anthropic = get_client()


# ─── Workspace CRUD ───────────────────────────────────────────────────────────

async def list_workspaces() -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_workspaces ORDER BY created_at"
        )
        return [_row_to_dict(r) for r in rows]


async def create_workspace(data: Dict[str, Any]) -> Dict[str, Any]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_workspaces (name, system_prompt, files, settings, color, icon)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
            RETURNING *
            """,
            data["name"],
            data.get("system_prompt"),
            json.dumps(data.get("files", [])),
            json.dumps(data.get("settings", {})),
            data.get("color", "#6366F1"),
            data.get("icon", "💬"),
        )
        return _row_to_dict(row)


async def update_workspace(workspace_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("name", "system_prompt", "color", "icon"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        for jfield in ("files", "settings"):
            if jfield in data and data[jfield] is not None:
                sets.append(f"{jfield} = ${idx}::jsonb")
                vals.append(json.dumps(data[jfield]))
                idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_workspaces WHERE id = $1", uuid.UUID(workspace_id))
            return _row_to_dict(row) if row else None
        sets.append(f"updated_at = NOW()")
        vals.append(uuid.UUID(workspace_id))
        row = await conn.fetchrow(
            f"UPDATE chat_workspaces SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None


async def delete_workspace(workspace_id: str) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM chat_workspaces WHERE id = $1", uuid.UUID(workspace_id)
        )
        return result == "DELETE 1"


# ─── Session CRUD ─────────────────────────────────────────────────────────────

async def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """단일 세션 조회 (ID 기반)."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_sessions WHERE id = $1",
            uuid.UUID(session_id),
        )
        return _row_to_dict(row) if row else None


async def list_sessions(workspace_id: str, limit: int = 50, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        if tag:
            rows = await conn.fetch(
                "SELECT * FROM chat_sessions WHERE workspace_id = $1 AND $3 = ANY(tags) ORDER BY pinned DESC, updated_at DESC LIMIT $2",
                uuid.UUID(workspace_id),
                limit,
                tag,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM chat_sessions WHERE workspace_id = $1 ORDER BY pinned DESC, updated_at DESC LIMIT $2",
                uuid.UUID(workspace_id),
                limit,
            )
        return [_row_to_dict(r) for r in rows]


async def create_session(data: Dict[str, Any]) -> Dict[str, Any]:
    async with get_pool().acquire() as conn:
        ws_id = uuid.UUID(str(data["workspace_id"]))
        title = data.get("title")

        # 버전 관리형 세션명 자동 생성
        if not title or title in ("새 대화", "New Chat", ""):
            title = await _generate_versioned_title(conn, ws_id)

        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (workspace_id, title)
            VALUES ($1, $2)
            RETURNING *
            """,
            ws_id,
            title,
        )
        return _row_to_dict(row)


async def _generate_versioned_title(conn, ws_id: uuid.UUID) -> str:
    """워크스페이스명에서 프로젝트 코드 추출 → 버전 넘버링 세션명 생성.
    예: [KIS] 자동매매 → KIS-001, KIS-002, ...
        [CEO] 통합지시 → CEO-001, CEO-002, ...
    """
    import re as _re
    # 워크스페이스명 조회
    ws_row = await conn.fetchrow(
        "SELECT name FROM chat_workspaces WHERE id = $1", ws_id
    )
    if not ws_row:
        return "새 대화"

    ws_name = ws_row["name"] or ""
    # [PROJECT] 패턴에서 코드 추출
    m = _re.match(r'\[([A-Za-z0-9]+)\]', ws_name)
    project_code = m.group(1).upper() if m else ws_name.strip()[:10]

    # 해당 워크스페이스의 동일 패턴 세션 최대 번호 조회
    prefix = f"{project_code}-"
    pattern = f'^{_re.escape(prefix)}[0-9]+$'
    rows = await conn.fetch(
        "SELECT title FROM chat_sessions WHERE workspace_id = $1 AND title ~ $2",
        ws_id, pattern,
    )
    max_num = 0
    for r in rows:
        try:
            num = int(r["title"][len(prefix):])
            if num > max_num:
                max_num = num
        except (ValueError, IndexError):
            pass
    # 패턴 매칭 안 되는 기존 세션도 카운트 (최소 보장)
    count_row = await conn.fetchrow(
        "SELECT COUNT(*) AS cnt FROM chat_sessions WHERE workspace_id = $1", ws_id
    )
    total = count_row["cnt"] if count_row else 0

    next_num = max(max_num + 1, total + 1)
    return f"{project_code}-{next_num:03d}"


async def update_session(session_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("title", "summary"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        if "pinned" in data and data["pinned"] is not None:
            sets.append(f"pinned = ${idx}")
            vals.append(data["pinned"])
            idx += 1
        if "tags" in data and data["tags"] is not None:
            sets.append(f"tags = ${idx}")
            vals.append(data["tags"])
            idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_sessions WHERE id = $1", uuid.UUID(session_id))
            return _row_to_dict(row) if row else None
        sets.append("updated_at = NOW()")
        vals.append(uuid.UUID(session_id))
        row = await conn.fetchrow(
            f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None


async def delete_session(session_id: str) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM chat_sessions WHERE id = $1", uuid.UUID(session_id)
        )
        return result == "DELETE 1"


# ─── Message ──────────────────────────────────────────────────────────────────

async def list_messages(session_id: str, limit: int = 200, offset: int = 0, sort: str = "asc") -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        order = "DESC" if sort == "desc" else "ASC"
        # 활성 스트리밍 중이 아니면 placeholder 제외 (중복 버블 방지)
        _is_active = session_id in _streaming_state and not _streaming_state[session_id].get("completed", False)
        _intent_filter = (
            "AND intent IS DISTINCT FROM '_deleted_duplicate'"
            if _is_active
            else "AND intent IS DISTINCT FROM '_deleted_duplicate' AND intent IS DISTINCT FROM 'streaming_placeholder'"
        )
        rows = await conn.fetch(
            f"SELECT * FROM chat_messages WHERE session_id = $1 {_intent_filter} ORDER BY created_at {order} LIMIT $2 OFFSET $3",
            uuid.UUID(session_id),
            limit,
            offset,
        )
        return [_row_to_dict(r) for r in rows]


async def list_messages_cursor(
    session_id: str,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Cursor 기반 메시지 조회 — 최근 N건 또는 cursor 이전 N건 (항상 ASC 반환)."""
    async with get_pool().acquire() as conn:
        sid = uuid.UUID(session_id)
        fetch_limit = limit + 1  # has_more 판별용 1건 추가
        # 활성 스트리밍 중이 아니면 placeholder 제외 (중복 버블 방지)
        _is_active = session_id in _streaming_state and not _streaming_state[session_id].get("completed", False)
        _placeholder_filter = "" if _is_active else "AND intent IS DISTINCT FROM 'streaming_placeholder'"
        if cursor:
            from datetime import datetime as _dt
            cursor_dt = _dt.fromisoformat(cursor)
            rows = await conn.fetch(
                "SELECT * FROM ("
                "  SELECT * FROM chat_messages"
                f"  WHERE session_id = $1 AND intent IS DISTINCT FROM '_deleted_duplicate' {_placeholder_filter}"
                "    AND created_at < $2"
                "  ORDER BY created_at DESC LIMIT $3"
                ") sub ORDER BY created_at ASC",
                sid, cursor_dt, fetch_limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM ("
                "  SELECT * FROM chat_messages"
                f"  WHERE session_id = $1 AND intent IS DISTINCT FROM '_deleted_duplicate' {_placeholder_filter}"
                "  ORDER BY created_at DESC LIMIT $2"
                ") sub ORDER BY created_at ASC",
                sid, fetch_limit,
            )
        messages = [_row_to_dict(r) for r in rows]
        has_more = len(messages) > limit
        if has_more:
            messages = messages[1:]  # 가장 오래된 1건 제거 (초과분)
        next_cursor = messages[0]["created_at"].isoformat() if has_more and messages else None
        return {
            "messages": messages,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }


async def _extract_artifacts(session_id: uuid.UUID, content: str, workspace_id: uuid.UUID = None) -> None:
    """AI 응답에서 아티팩트 자동 추출 → chat_artifacts 저장.
    감지 유형: 코드, 보고서, 기획서, 계획서, 분석, 지시서, 체크리스트, 테이블, 이미지, 차트, 파일
    """
    if not content or len(content) < 100:
        return

    import re as _re

    artifacts = []

    # 1) 코드 블록 추출 (```language ... ```)
    code_blocks = _re.findall(
        r'```(\w+)\n(.*?)```', content, _re.DOTALL
    )
    for lang, code in code_blocks:
        if lang.lower() in ('diff', 'text', 'log', 'output', 'bash', 'sh', 'shell',
                              'json', 'xml', 'yaml', 'toml', 'ini', 'conf', 'env'):
            continue
        if len(code.strip()) < 200:
            continue
        # JSON 형태 코드 블록 제외 (언어 태그 무관)
        stripped = code.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            continue
            continue
        title = f"{lang} 코드"
        first_line = code.strip().split('\n')[0]
        if 'def ' in first_line:
            title = first_line.strip()[:80]
        elif 'class ' in first_line:
            title = first_line.strip()[:80]
        artifacts.append(("code", title, code.strip(), {"language": lang}))

    # 2) 보고서/기획서/분석 추출 (# 또는 이모지 제목으로 시작하는 구조화된 문서)
    # 2a) 마크다운 헤더 (# / ##) 시작
    report_match = _re.search(
        r'(^#{1,2}\s+.+?\n(?:.*?\n){10,})', content, _re.MULTILINE
    )
    if report_match and len(report_match.group(1)) > 300:
        report_text = report_match.group(1)
        title_match = _re.search(r'^#{1,2}\s+(.+)', report_text)
        title = title_match.group(1)[:100] if title_match else "보고서"
        artifacts.append(("report", title, report_text, {}))

    # 2b) 이모지 제목 시작 (📋 📊 🔬 📄 🎯 💡 🚀 ✅ 등)
    if not report_match or (report_match and len(report_match.group(1)) <= 300):
        emoji_report = _re.search(
            r'(^[\U0001F300-\U0001FAFFa-zA-Z가-힣]?[📋📊🔬📄🎯💡🚀✅⚠️🛠️📌🔍📈📉🗂️🧬🏗️].+?\n(?:.*?\n){8,})',
            content, _re.MULTILINE
        )
        if emoji_report and len(emoji_report.group(1)) > 300:
            report_text = emoji_report.group(1)
            first_line = report_text.split('\n')[0].strip()
            artifacts.append(("report", first_line[:100], report_text, {}))

    # 3) 기획서/계획서/제안서 감지 (키워드 기반)
    _plan_keywords = [
        '기획서', '계획서', '제안서', '설계서', '명세서', '가이드',
        '로드맵', 'PRD', '스펙', '아키텍처', '수정 방안', '수정 계획',
        '구현 계획', '개선 방안', '마이그레이션', '체크리스트',
    ]
    for kw in _plan_keywords:
        if kw.lower() in content.lower():
            # 키워드 포함 섹션 추출 (해당 키워드가 있는 줄부터 다음 빈줄 2개 또는 끝까지)
            kw_match = _re.search(
                rf'(^.*{_re.escape(kw)}.*\n(?:.*\n){{5,}})',
                content, _re.MULTILINE | _re.IGNORECASE
            )
            if kw_match and len(kw_match.group(1)) > 400:
                plan_text = kw_match.group(1)
                first_line = plan_text.split('\n')[0].strip()
                # 중복 방지: 이미 report로 잡힌 내용과 80% 이상 겹치면 스킵
                is_dup = any(
                    a[0] == "report" and len(set(a[2][:200]) & set(plan_text[:200])) > 160
                    for a in artifacts
                )
                if not is_dup:
                    artifacts.append(("report", first_line[:100] or kw, plan_text, {"subtype": "plan"}))
                break  # 첫 번째 매칭만

    # 4) 지시서 (>>>DIRECTIVE_START 블록)
    directive_match = _re.search(
        r'(>>>DIRECTIVE_START.*?>>>DIRECTIVE_END)', content, _re.DOTALL
    )
    if directive_match:
        directive_text = directive_match.group(1)
        title_m = _re.search(r'TITLE:\s*(.+)', directive_text)
        title = title_m.group(1).strip()[:100] if title_m else "지시서"
        artifacts.append(("report", f"📋 지시서: {title}", directive_text, {"subtype": "directive"}))

    # 5) 테이블 추출 (마크다운 테이블)
    table_blocks = _re.findall(
        r'(\|.+\|(?:\n\|[-:| ]+\|)?\n(?:\|.+\|\n?){3,})', content
    )
    for table in table_blocks:
        if len(table) > 200:
            header = table.split('\n')[0].strip()
            title = f"테이블: {header[:60]}"
            artifacts.append(("table", title, table, {}))

    # 6) 번호 목록 구조 문서 (1. 2. 3. ... 5개 이상 + 500자 이상)
    numbered_items = _re.findall(r'^(?:\d+[\.\)]\s+.+)', content, _re.MULTILINE)
    if len(numbered_items) >= 5:
        # 번호 목록 전체 영역 추출
        num_match = _re.search(
            r'((?:^.*\n)?(?:^\d+[\.\)]\s+.+\n(?:(?!\d+[\.\)]).+\n)*){5,})',
            content, _re.MULTILINE
        )
        if num_match and len(num_match.group(0)) > 500:
            num_text = num_match.group(0)
            # 이미 다른 타입으로 잡혔는지 중복 체크
            is_dup = any(
                len(set(a[2][:200]) & set(num_text[:200])) > 160
                for a in artifacts
            )
            if not is_dup:
                first_line = num_text.strip().split('\n')[0].strip()
                artifacts.append(("report", first_line[:100] or "구조화 문서", num_text, {"subtype": "numbered_list"}))

    # 7) 이미지 URL 감지 (generate_image 결과 등)
    img_pattern = _re.findall(r'!\[([^\]]*)\]\((https?://[^\)]+\.(?:png|jpg|jpeg|gif|webp|svg)[^\)]*)\)', content)
    for alt_text, img_url in img_pattern:
        if not any(a[2] == img_url for a in artifacts):
            artifacts.append(("image", alt_text or "이미지", img_url, {"url": img_url}))

    # 8) Mermaid 다이어그램 감지
    mermaid_match = _re.findall(r'```mermaid\s*\n(.*?)```', content, _re.DOTALL)
    for diagram in mermaid_match:
        if len(diagram.strip()) > 30:
            artifacts.append(("chart", "Mermaid 다이어그램", diagram.strip(), {"subtype": "mermaid"}))

    # 9) 파일 링크 감지 (다운로드 링크)
    file_pattern = _re.findall(r'\[([^\]]+)\]\((https?://[^\)]+\.(?:pdf|xlsx|csv|docx|zip|tar\.gz)[^\)]*)\)', content)
    for fname, furl in file_pattern:
        artifacts.append(("file", fname, furl, {"url": furl, "filename": fname}))

    if not artifacts:
        return

    # workspace_id 조회 (None이면 세션에서 역추적)
    if workspace_id is None:
        try:
            async with get_pool().acquire() as _wconn:
                workspace_id = await _wconn.fetchval(
                    "SELECT workspace_id FROM chat_sessions WHERE id = $1", session_id
                )
        except Exception:
            pass

    # 최대 5개 저장 (기존 3개 → 확장)
    import json as _json
    async with get_pool().acquire() as conn:
        for art_type, title, art_content, metadata in artifacts[:5]:
            # 중복 방지: 같은 session_id + type + title 조합이 이미 존재하면 스킵
            existing = await conn.fetchval(
                "SELECT 1 FROM chat_artifacts WHERE session_id = $1 AND type = $2 AND title = $3 LIMIT 1",
                session_id, art_type, title[:200],
            )
            if existing:
                continue
            await conn.execute(
                """
                INSERT INTO chat_artifacts (session_id, workspace_id, type, title, content, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                session_id, workspace_id, art_type, title[:200], art_content[:50000],
                _json.dumps(metadata, ensure_ascii=False),
            )
    logger.info(f"artifacts_extracted: session={str(session_id)[:8]} count={min(len(artifacts), 5)}")


async def _save_message(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    role: str,
    content: str,
    model_used: Optional[str] = None,
    intent: Optional[str] = None,
    cost: Decimal = Decimal("0"),
    tokens_in: int = 0,
    tokens_out: int = 0,
    attachments: Optional[List[Any]] = None,
    sources: Optional[List[Any]] = None,
    tools_called: Optional[List[str]] = None,
    thinking_summary: Optional[str] = None,
    reply_to_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    # Strip raw XML tool-call / tool-response / fabricated-result blocks from assistant messages
    # 닫힌 태그 + 닫히지 않은 태그(이후 전부) 모두 제거
    if role == "assistant" and content:
        for tag in ("function_calls", "function_response", "function_results", "tool_results", "tool_call", "tool_response"):
            content = re.sub(rf'<{tag}>.*?</{tag}>', '', content, flags=re.DOTALL)
            content = re.sub(rf'<{tag}>.*', '', content, flags=re.DOTALL)  # 닫히지 않은 태그
        content = re.sub(r'<invoke\s+name=[^>]*>.*?</invoke>', '', content, flags=re.DOTALL)
        content = re.sub(r'<invoke\s+name=[^>]*>.*', '', content, flags=re.DOTALL)  # 닫히지 않은 invoke
        # streaming_placeholder 텍스트 잔류 방지
        content = re.sub(r'\n\n⏳ _.*?_', '', content, flags=re.DOTALL)
        content = re.sub(r'^⏳ _[^\n]*_\s*', '', content)
        content = content.strip()

    # P2-2: ContextVar에서 branch_id 가져오기 (분기 모드)
    _branch_id_str = _current_branch_id.get(None)
    _branch_uuid = uuid.UUID(_branch_id_str) if _branch_id_str else None

    # AADS-CRITICAL-FIX #2: INSERT + UPDATE를 트랜잭션으로 감싸 message_count 정합성 보장
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO chat_messages
                (session_id, role, content, model_used, intent, cost, tokens_in, tokens_out,
                 attachments, sources, tools_called, thinking_summary, reply_to_id, branch_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12, $13, $14)
            RETURNING *
            """,
            session_id,
            role,
            content,
            model_used,
            intent,
            cost,
            tokens_in,
            tokens_out,
            json.dumps(attachments or []),
            json.dumps(sources or []),
            json.dumps(tools_called or []),
            thinking_summary,
            reply_to_id,
            _branch_uuid,
        )
        # Update session message count (atomic with INSERT)
        await conn.execute(
            "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
            session_id,
        )
    result = _row_to_dict(row)
    # 비동기 임베딩 생성 (실패해도 메시지 저장에 영향 없음)
    try:
        import asyncio as _emb_asyncio
        from app.services.chat_embedding_service import embed_and_store_message
        _emb_asyncio.create_task(
            embed_and_store_message(get_pool(), str(result["id"]), content)
        )
    except Exception:
        pass  # 임베딩 실패는 무시
    return result


async def _save_and_update_session(
    sid: uuid.UUID,
    content: str,
    *,
    session_id_str: str = "",
    raw_messages: Optional[List[Dict[str, Any]]] = None,
    model_used: str = "",
    intent: str = "",
    cost: Decimal = Decimal("0"),
    tokens_in: int = 0,
    tokens_out: int = 0,
    sources: Optional[list] = None,
    tools_called: Optional[list] = None,
    thinking_summary: Optional[str] = None,
    auto_save_check: bool = False,
) -> None:
    """#19: Phase C — 별도 커넥션으로 응답 저장 + 세션 비용 업데이트.
    BUG-FIX: placeholder가 있으면 UPDATE로 전환 (DELETE+INSERT gap 제거).
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # streaming_placeholder가 있으면 UPDATE로 최종 응답 전환 (gap 제거)
            placeholder_id = await conn.fetchval(
                "SELECT id FROM chat_messages WHERE session_id = $1 AND intent = 'streaming_placeholder' ORDER BY created_at DESC LIMIT 1",
                sid,
            )
            if placeholder_id:
                # placeholder → 최종 응답으로 전환 (같은 row, atomic)
                # content 정리 (XML 태그, placeholder 마커 제거)
                clean_content = content
                if clean_content:
                    for tag in ("function_calls", "function_response", "function_results", "tool_results", "tool_call", "tool_response"):
                        clean_content = re.sub(rf'<{tag}>.*?</{tag}>', '', clean_content, flags=re.DOTALL)
                        clean_content = re.sub(rf'<{tag}>.*', '', clean_content, flags=re.DOTALL)
                    clean_content = re.sub(r'<invoke\s+name=[^>]*>.*?</invoke>', '', clean_content, flags=re.DOTALL)
                    clean_content = re.sub(r'<invoke\s+name=[^>]*>.*', '', clean_content, flags=re.DOTALL)
                    clean_content = re.sub(r'\n\n⏳ _.*?_', '', clean_content, flags=re.DOTALL)
                    clean_content = re.sub(r'^⏳ _[^\n]*_\s*', '', clean_content)
                    clean_content = clean_content.strip()
                await conn.execute(
                    """UPDATE chat_messages
                       SET content = $1, intent = $2, model_used = $3,
                           cost = $4, tokens_in = $5, tokens_out = $6,
                           sources = $7::jsonb, tools_called = $8::jsonb,
                           thinking_summary = $9, edited_at = NOW()
                       WHERE id = $10""",
                    clean_content, intent or None, model_used,
                    cost, tokens_in, tokens_out,
                    json.dumps(sources or []), json.dumps(tools_called or []),
                    thinking_summary, placeholder_id,
                )
                logger.info(f"placeholder_promoted_to_final session={str(sid)[:8]} placeholder_id={placeholder_id}")
            else:
                await _save_message(
                    conn, sid, "assistant", content,
                    model_used=model_used, intent=intent, cost=cost,
                    tokens_in=tokens_in, tokens_out=tokens_out,
                    sources=sources or [], tools_called=tools_called or [],
                    thinking_summary=thinking_summary,
                )
            await conn.execute(
                "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
                cost, sid,
            )
        # 아티팩트 자동 추출 (코드 블록, 보고서, 테이블, 이미지, 차트, 파일)
        try:
            await _extract_artifacts(sid, content)
        except Exception as _art_err:
            logger.debug(f"artifact_extract_error: {_art_err}")

        # 20턴마다 자동 세션 노트 (트랜잭션 밖)
        if auto_save_check and session_id_str:
            try:
                msg_count_row = await conn.fetchrow(
                    "SELECT message_count FROM chat_sessions WHERE id = $1", sid
                )
                msg_count = (msg_count_row["message_count"] if msg_count_row else 0) or 0
                if msg_count >= 20 and msg_count % 20 == 0:
                    import asyncio as _asyncio
                    _asyncio.create_task(_auto_save_session_note(session_id_str, raw_messages or []))
                    _asyncio.create_task(_auto_observe_session(raw_messages or []))
            except Exception:
                pass


# ── 재귀 방지 플래그: trigger_ai_reaction → send_message_stream → tool → trigger 무한 루프 차단 ──
import time as _time
_ai_reaction_active: dict[str, float] = {}  # session_id → timestamp
_ai_reaction_queue: dict[str, list[str]] = {}  # session_id → 대기 메시지 리스트
_AI_REACTION_MAX_QUEUE = 5


async def _consume_next_reaction(sid: str, msg: str) -> None:
    """큐에서 꺼낸 다음 AI 반응 메시지를 소비하는 헬퍼."""
    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(sid)
    try:
        async for _ in send_message_stream(
            session_id=sid,
            content=msg,
            intent_override="auto_reaction",
        ):
            pass
        # message_count 보정
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as _conn:
                await _conn.execute(
                    "UPDATE chat_sessions SET message_count = "
                    "(SELECT COUNT(*) FROM chat_messages WHERE session_id = $1), "
                    "updated_at = NOW() WHERE id = $1",
                    uuid.UUID(sid),
                )
        except Exception as _mc_err:
            logger.warning(f"_consume_next_reaction: message_count update failed session={str(sid)[:8]}: {_mc_err}")
    except Exception as e:
        logger.warning(f"trigger_ai_reaction_queue error session={str(sid)[:8]}: {e}")
    finally:
        _ai_reaction_active.pop(sid, None)
        # 큐에 대기 중인 트리거가 있으면 다음 것 처리
        if sid in _ai_reaction_queue and _ai_reaction_queue[sid]:
            next_msg = _ai_reaction_queue[sid].pop(0)
            if not _ai_reaction_queue[sid]:
                del _ai_reaction_queue[sid]
            _ai_reaction_active[sid] = _time.time()
            import asyncio as _aio
            loop = _aio.get_running_loop()
            loop.create_task(_consume_next_reaction(sid, next_msg))


async def trigger_ai_reaction(
    session_id: str,
    system_message: str,
) -> None:
    """
    채팅방에 시스템 사용자 메시지를 삽입한 후 AI가 자동 반응하도록 트리거.
    Pipeline Runner / delegate_to_agent 완료 후 AI가 결과를 확인·조치하게 함.

    동작:
    1. 재귀 호출 방지 (같은 세션에서 이미 반응 중이면 큐잉)
    2. send_message_stream()을 백그라운드에서 소비 → AI 응답 생성 + DB 저장

    주의: [시스템] 접두사 메시지에 대해 AI가 다시 delegate_to_agent를 호출하면
    무한 루프가 될 수 있으므로, 재귀 방지 플래그로 차단함.
    """
    import asyncio as _asyncio

    # TTL 기반 만료: 5분 이상 된 항목 자동 정리 (크래시 잔류 방지)
    now = _time.time()
    expired = [k for k, v in _ai_reaction_active.items() if now - v > 300]
    for k in expired:
        _ai_reaction_active.pop(k, None)
        _ai_reaction_queue.pop(k, None)

    # 시스템 메시지에 작업 재실행 도구만 금지 (무한 루프 방지), 진단 도구는 허용
    safe_message = (
        system_message + "\n\n"
        "⚠️ 이 메시지는 자동 트리거입니다.\n"
        "**금지 도구** (무한 루프 방지): delegate_to_agent, pipeline_c_start, spawn_subagent, spawn_parallel_subagents\n"
        "**허용 도구** (진단·조치용): run_remote_command, check_task_status, read_task_logs, "
        "terminate_task, health_check, query_database, read_remote_file 등 읽기/진단 도구는 자유롭게 사용하세요.\n"
        "오류가 발생했으면 도구로 원인을 직접 확인하고, 가능한 한 자율적으로 조치하세요.\n\n"
        "**배포 완료 보고 시 필수 규칙:**\n"
        "- 도구를 호출하지 않고 수치(건수/개수)를 보고하는 것은 금지. 반드시 query_database/run_remote_command로 실측.\n"
        "- '정상 완료'라고 보고하려면 최소 health_check 또는 docker ps로 실제 확인 필수.\n"
        "- 프론트엔드 변경 시 browser_snapshot으로 렌더링 확인 권장.\n\n"
        f"[현재 세션 ID: {session_id}]\n"
        f"pipeline_runner_submit 호출 시 반드시 session_id=\"{session_id}\"를 포함하세요."
    )

    # 🆕 CEO의 SSE 스트리밍(with_background_completion) 실행 중이면 큐잉 (CEO 작업 중단 금지)
    if session_id in _active_bg_tasks and not _active_bg_tasks[session_id].done():
        if session_id not in _ai_reaction_queue:
            _ai_reaction_queue[session_id] = []
        q = _ai_reaction_queue[session_id]
        if len(q) >= _AI_REACTION_MAX_QUEUE:
            q.pop(0)
        q.append(safe_message)
        logger.info(f"trigger_ai_reaction: deferred (bg_task active) session={session_id[:8]}... queue_size={len(q)}")
        return

    # 이미 이 세션에서 AI 반응이 진행 중이면 큐에 추가
    if session_id in _ai_reaction_active:
        if session_id not in _ai_reaction_queue:
            _ai_reaction_queue[session_id] = []
        q = _ai_reaction_queue[session_id]
        if len(q) >= _AI_REACTION_MAX_QUEUE:
            dropped = q.pop(0)
            logger.warning(f"trigger_ai_reaction: queue full, dropped oldest for session={session_id[:8]}...")
        q.append(safe_message)
        logger.info(f"trigger_ai_reaction: queued (active) session={session_id[:8]}... queue_size={len(q)}")
        return

    _ai_reaction_active[session_id] = now

    # ContextVar 설정 (백그라운드 task에서도 session_id 사용 가능하도록)
    from app.services.tool_executor import current_chat_session_id
    current_chat_session_id.set(session_id)

    async def _consume_stream():
        try:
            async for _ in send_message_stream(
                session_id=session_id,
                content=safe_message,
                intent_override="auto_reaction",
            ):
                pass  # 스트림 전체 소비 → DB에 AI 응답 자동 저장
            # message_count 보정 (trigger_ai_reaction으로 생성된 시스템+AI 메시지 반영)
            try:
                from app.core.db_pool import get_pool
                pool = get_pool()
                async with pool.acquire() as _conn:
                    await _conn.execute(
                        "UPDATE chat_sessions SET message_count = "
                        "(SELECT COUNT(*) FROM chat_messages WHERE session_id = $1), "
                        "updated_at = NOW() WHERE id = $1",
                        uuid.UUID(session_id),
                    )
            except Exception as _mc_err:
                logger.warning(f"trigger_ai_reaction: message_count update failed session={session_id[:8]}: {_mc_err}")
        except Exception as e:
            logger.warning(f"trigger_ai_reaction error session={session_id}: {e}")
        finally:
            _ai_reaction_active.pop(session_id, None)
            # 큐에 대기 중인 트리거가 있으면 다음 것 처리
            if session_id in _ai_reaction_queue and _ai_reaction_queue[session_id]:
                next_msg = _ai_reaction_queue[session_id].pop(0)
                if not _ai_reaction_queue[session_id]:
                    del _ai_reaction_queue[session_id]
                _ai_reaction_active[session_id] = _time.time()
                loop = _asyncio.get_running_loop()
                loop.create_task(_consume_next_reaction(session_id, next_msg))

    try:
        loop = _asyncio.get_running_loop()
        loop.create_task(_consume_stream())
        logger.info(f"trigger_ai_reaction: triggered for session={session_id[:8]}...")
    except RuntimeError:
        _ai_reaction_active.pop(session_id, None)
        logger.error("trigger_ai_reaction: no running event loop")


async def _analyze_videos_with_gemini(
    video_attachments: list,
    user_prompt: str,
) -> list:
    """
    동영상 파일 목록을 Gemini 2.0 Flash API로 분석.
    base64 인라인 데이터 방식 (최대 20MB).

    Returns:
        list of {name, analysis} 딕셔너리
    """
    import os
    import base64 as _b64

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("[VIDEO] GOOGLE_API_KEY not set — skipping video analysis")
        return []

    results = []
    try:
        from google import genai as genai_sdk
        from google.genai import types as genai_types

        client = genai_sdk.Client(api_key=api_key)

        for att in video_attachments:
            file_name = att.get("name", "video")
            media_type = att.get("media_type", "video/mp4")
            raw_b64 = att.get("base64", "")
            if not raw_b64:
                continue

            try:
                video_bytes = _b64.b64decode(raw_b64)
                analysis_prompt = (
                    f"다음 동영상을 분석해주세요. 사용자 요청: {user_prompt}\n"
                    "동영상의 주요 내용, 장면, 텍스트, 중요한 시각 정보를 한국어로 상세히 설명해주세요."
                )
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        genai_types.Part.from_bytes(data=video_bytes, mime_type=media_type),
                        analysis_prompt,
                    ],
                )
                analysis_text = response.text or "(분석 결과 없음)"
                results.append({"name": file_name, "analysis": f"[동영상 분석: {file_name}]\n{analysis_text}"})
                logger.info(f"[VIDEO] Gemini analyzed '{file_name}': {len(analysis_text)} chars")
            except Exception as e:
                logger.error(f"[VIDEO] Gemini analysis failed for '{file_name}': {e}")
                results.append({"name": file_name, "analysis": f"[동영상: {file_name}] (Gemini 분석 실패: {e})"})

    except ImportError:
        logger.warning("[VIDEO] google-genai not installed — skipping video analysis")
    except Exception as e:
        logger.error(f"[VIDEO] Gemini client error: {e}")

    return results


async def _analyze_images_with_gemini(
    image_contents: list,
    user_prompt: str,
) -> list:
    """이미지 파일 목록을 Gemini Flash API로 분석하여 텍스트 설명 반환.
    CLI Relay가 이미지를 직접 전달할 수 없으므로 Gemini Vision으로 전처리."""
    import os
    import base64 as _b64

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("[VISION-PRE] GOOGLE_API_KEY not set — skipping image analysis")
        return []

    results = []
    try:
        from google import genai as genai_sdk
        from google.genai import types as genai_types

        client = genai_sdk.Client(api_key=api_key)

        for img in image_contents:
            file_name = img.get("name", "image")
            media_type = img.get("media_type", "image/jpeg")
            raw_b64 = img.get("base64_data", "")
            if not raw_b64:
                continue

            try:
                img_bytes = _b64.b64decode(raw_b64)
                analysis_prompt = (
                    f"다음 이미지를 분석해주세요. 사용자 요청: {user_prompt}\n"
                    "이미지의 내용, 텍스트, UI 요소, 에러 메시지, 차트/그래프, 중요한 시각 정보를 한국어로 상세히 설명해주세요. "
                    "코드나 에러가 보이면 정확히 옮겨 적어주세요."
                )
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        genai_types.Part.from_bytes(data=img_bytes, mime_type=media_type),
                        analysis_prompt,
                    ],
                )
                analysis_text = response.text or "(분석 결과 없음)"
                results.append({"name": file_name, "analysis": f"[이미지 분석: {file_name}]\n{analysis_text}"})
                logger.info(f"[VISION-PRE] Gemini analyzed '{file_name}': {len(analysis_text)} chars")
            except Exception as e:
                logger.error(f"[VISION-PRE] Gemini analysis failed for '{file_name}': {e}")
                results.append({"name": file_name, "analysis": f"[이미지: {file_name}] (Gemini 분석 실패: {e})"})

    except ImportError:
        logger.warning("[VISION-PRE] google-genai not installed — skipping image analysis")
    except Exception as e:
        logger.error(f"[VISION-PRE] Gemini client error: {e}")

    return results


async def process_files_for_claude(files: list) -> list:
    """파일 데이터 목록을 Claude API content 배열 형식으로 변환.

    Args:
        files: [{"filename": str, "data": bytes, "mime_type": str}, ...]
    Returns:
        Claude API content blocks 리스트
    """
    import base64 as b64
    content_parts = []
    for file_data in files:
        filename = file_data.get("filename", "unknown")
        data = file_data.get("data", b"")
        mime_type = file_data.get("mime_type", "application/octet-stream")

        if mime_type.startswith("image/"):
            content_parts.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": b64.b64encode(data).decode(),
                },
            })
        elif mime_type == "application/pdf":
            try:
                import pdfplumber
                import io
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
                content_parts.append({"type": "text", "text": f"[PDF: {filename}]\n{text[:10000]}"})
            except Exception as e:
                content_parts.append({"type": "text", "text": f"[첨부파일: {filename} - PDF 추출 실패: {e}]"})
        elif mime_type.startswith("text/") or Path(filename).suffix.lower() in (
            ".py", ".js", ".ts", ".md", ".txt", ".json", ".yaml", ".yml", ".csv",
            ".tsx", ".jsx", ".sh", ".sql", ".go", ".rs", ".java", ".c", ".cpp",
        ):
            try:
                text = data.decode("utf-8", errors="replace")
                content_parts.append({"type": "text", "text": f"[파일: {filename}]\n```\n{text[:10000]}\n```"})
            except Exception:
                content_parts.append({"type": "text", "text": f"[첨부파일: {filename}]"})
        else:
            content_parts.append({"type": "text", "text": f"[첨부파일: {filename} ({mime_type})]"})
    return content_parts


async def process_video_with_gemini(file_data: dict, user_message: str) -> str:
    """동영상 raw bytes를 Gemini Flash로 분석하여 텍스트 반환.

    Args:
        file_data: {"filename": str, "data": bytes, "mime_type": str}
        user_message: 사용자 메시지 (분석 컨텍스트)
    Returns:
        분석 결과 텍스트
    """
    import base64 as _b64
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return f"[동영상 분석 실패: GOOGLE_API_KEY 미설정 — {file_data.get('filename', 'video')}]"
    try:
        import google.generativeai as genai
        import tempfile, time
        genai.configure(api_key=api_key)
        suffix = Path(file_data.get("filename", "video.mp4")).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(file_data["data"])
            tmp_path = f.name
        video_file = genai.upload_file(path=tmp_path)
        while video_file.state.name == "PROCESSING":
            await _heartbeat_asyncio.sleep(2)
            video_file = genai.get_file(video_file.name)
        model_g = genai.GenerativeModel("gemini-2.5-flash")
        response = model_g.generate_content(
            [video_file, user_message or "이 동영상의 내용을 분석하고 설명해주세요."]
        )
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return f"[동영상 분석 결과 (Gemini Flash) — {file_data.get('filename', 'video')}]\n{response.text}"
    except Exception as e:
        logger.error(f"[VIDEO_RAW] Gemini error: {e}")
        return f"[동영상 분석 실패: {str(e)}]"


async def send_message_stream(
    session_id: str,
    content: str,
    attachments: Optional[List[Any]] = None,
    model_override: Optional[str] = None,
    intent_override: Optional[str] = None,
    uploaded_files: Optional[List[Any]] = None,
    reply_to_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    branch_point_msg_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    AADS-185: 3계층 Context Engineering + IntentRouter + ModelSelector + Tool Use 루프.
    SSE 청크: data: {"type": "delta"|"thinking"|"tool_use"|"tool_result"|"done"|"error", ...}
    """
    # AADS-186C: Langfuse 트레이스 시작
    _lf_trace = create_trace(
        name="chat_turn",
        session_id=session_id,
        user_id="CEO",
        input_data={"content": content[:500], "model_override": model_override},
    )
    _lf_span_intent = None
    _lf_span_llm = None
    _trace_start_time = __import__("time").monotonic()

    try:
        from app.services.tool_executor import current_chat_session_id
        current_chat_session_id.set(session_id)

        from app.core.interrupt_queue import set_streaming, has_pending_interrupts, pop_pending_interrupts
        set_streaming(session_id, True)
        sid = uuid.UUID(session_id)
        sid_short = session_id[:8]  # 로깅용 축약 (str 보장 — sid[:8] 직접 사용 금지)

        # SSE 재연결 프로토콜: 고유 stream_id 발행
        import time as _stream_time
        _stream_id = str(uuid.uuid4())[:8]
        yield f"data: {json.dumps({'type': 'stream_start', 'stream_id': _stream_id})}\n\n"

        # AADS-FIX: 이전 턴에서 미소비된 인터럽트를 현재 user 메시지 앞에 주입
        if has_pending_interrupts(session_id):
            _pending = pop_pending_interrupts(session_id)
            _pending_text = "\n".join(f"[이전 추가 지시] {p['content']}" for p in _pending)
            content = f"{_pending_text}\n\n{content}"
            logger.info(f"[PENDING_INTERRUPT] session={session_id[:8]} injected={len(_pending)} items")

        # 1. 첨부파일 처리 — Ephemeral Document Context (#파일맥락보호)
        #    파일 전문은 content에 넣지 않고 Layer D로 현재 턴에만 주입.
        #    히스토리에는 참조 요약만 저장하여 컨텍스트 낭비 방지.
        _ephemeral_doc_context = ""
        _vision_images: list = []  # Claude Vision API용 이미지 content blocks
        logger.info(f"[ATTACH] session={session_id[:8]} attachments={attachments}")
        if attachments:
            from app.core.document_context import (
                extract_file_contents,
                build_ephemeral_document_layer,
                build_file_reference_summary,
            )
            _file_contents = extract_file_contents(attachments)
            _readable_count = sum(1 for f in _file_contents if f.get("readable"))
            _total_tokens = sum(f["tokens"] for f in _file_contents)
            logger.info(f"[ATTACH] extracted {_readable_count} files, ~{_total_tokens} tokens")

            # Vision: 이미지 파일 추출 → Claude Vision API content blocks 구성
            _image_files = [f for f in _file_contents if f.get("is_image") and f.get("base64_data")]
            for _img in _image_files:
                _vision_images.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _img["media_type"],
                        "data": _img["base64_data"],
                    },
                })
            if _vision_images:
                logger.info(f"[VISION] {len(_vision_images)} image(s) extracted for Vision API")

            # file_id 기반 첨부파일 처리 (디스크 저장 파일 → Vision)
            for att in attachments:
                if isinstance(att, dict) and att.get("file_id"):
                    try:
                        _finfo = await get_chat_file(att["file_id"])
                        if _finfo and _finfo["mime_type"].startswith("image/"):
                            import base64 as _cf_b64
                            _fpath = Path(_finfo["storage_path"])
                            if _fpath.exists():
                                _img_data = _fpath.read_bytes()
                                _vision_images.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": _finfo["mime_type"],
                                        "data": _cf_b64.b64encode(_img_data).decode(),
                                    },
                                })
                                logger.info(f"[VISION] file_id={att['file_id'][:8]} loaded from disk")
                    except Exception as _fe:
                        logger.warning(f"[VISION] file_id load failed: {_fe}")

            # Gemini: 동영상 파일 → Gemini 2.0 Flash 분석 후 텍스트로 변환
            _video_attachments = [a for a in attachments if isinstance(a, dict) and a.get("type") == "video" and a.get("base64")]
            if _video_attachments:
                _video_texts = await _analyze_videos_with_gemini(_video_attachments, content)
                for _vt in _video_texts:
                    _file_contents.append({
                        "name": _vt["name"],
                        "path": "",
                        "ext": "",
                        "content": _vt["analysis"],
                        "tokens": len(_vt["analysis"]) // 4,
                        "readable": True,
                        "error": None,
                    })
                logger.info(f"[VIDEO] {len(_video_attachments)} video(s) analyzed via Gemini API")

            # Vision 이미지도 Gemini로 전처리 (CLI Relay용 텍스트 변환)
            _image_for_preprocess = [f for f in _file_contents if f.get("is_image") and f.get("base64_data")]
            if _image_for_preprocess:
                _image_texts = await _analyze_images_with_gemini(_image_for_preprocess, content)
                for _it in _image_texts:
                    _file_contents.append({
                        "name": _it["name"],
                        "path": "",
                        "ext": "",
                        "content": _it["analysis"],
                        "tokens": len(_it["analysis"]) // 4,
                        "readable": True,
                        "error": None,
                    })
                logger.info(f"[VISION-PRE] {len(_image_for_preprocess)} image(s) pre-analyzed via Gemini for CLI Relay")

            # Layer D: 현재 턴에만 주입될 전문 컨텍스트 (텍스트 파일만)
            _ephemeral_doc_context = build_ephemeral_document_layer(_file_contents)

            # 히스토리에 저장할 참조 요약 (전문 대신)
            _ref_summary = build_file_reference_summary(_file_contents)
            if _ref_summary:
                content = content + "\n\n" + _ref_summary
        else:
            # Stage 3: 첨부파일 없지만 이전 파일 재참조 감지 시 Layer D 재주입
            from app.core.document_context import (
                detect_file_rereference,
                build_rereference_context,
            )
            if detect_file_rereference(content):
                _ephemeral_doc_context = await build_rereference_context(
                    content, session_id, get_pool(),
                )

        # 1-B. uploaded_files 처리 (multipart/form-data로 전송된 raw 파일들)
        if uploaded_files:
            import base64 as _uf_b64
            logger.info(f"[UPLOAD] session={session_id[:8]} uploaded_files={len(uploaded_files)}")
            _uf_ref_lines = []
            for uf in uploaded_files:
                fname = uf.get("filename", "unknown")
                data = uf.get("data", b"")
                mime = uf.get("mime_type", "application/octet-stream")
                if mime.startswith("image/"):
                    _vision_images.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": _uf_b64.b64encode(data).decode(),
                        },
                    })
                    _uf_ref_lines.append(f"- [이미지: {fname}] (Vision API 분석)")
                elif mime.startswith("video/"):
                    _vtext = await process_video_with_gemini(uf, content)
                    _ephemeral_doc_context = (_ephemeral_doc_context + "\n\n" + _vtext).strip()
                    _uf_ref_lines.append(f"- [동영상: {fname}] Gemini 분석 완료")
                elif mime == "application/pdf":
                    try:
                        import pdfplumber, io as _io
                        with pdfplumber.open(_io.BytesIO(data)) as _pdf:
                            _pdf_text = "\n".join(p.extract_text() or "" for p in _pdf.pages)
                        _ephemeral_doc_context = (_ephemeral_doc_context + f"\n\n[PDF: {fname}]\n{_pdf_text[:10000]}").strip()
                    except Exception as _pe:
                        _ephemeral_doc_context = (_ephemeral_doc_context + f"\n\n[PDF: {fname} — 추출 실패: {_pe}]").strip()
                    _uf_ref_lines.append(f"- [PDF: {fname}]")
                elif mime.startswith("text/") or Path(fname).suffix.lower() in (
                    ".py", ".js", ".ts", ".tsx", ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".sh", ".sql"
                ):
                    try:
                        _txt = data.decode("utf-8", errors="replace")
                        _ephemeral_doc_context = (_ephemeral_doc_context + f"\n\n[파일: {fname}]\n```\n{_txt[:10000]}\n```").strip()
                    except Exception:
                        pass
                    _uf_ref_lines.append(f"- [텍스트파일: {fname}]")
                else:
                    _uf_ref_lines.append(f"- [첨부파일: {fname} ({mime})]")
            if _uf_ref_lines:
                _ref_block = "\n\n[첨부파일 목록]\n" + "\n".join(_uf_ref_lines)
                content = content + _ref_block

        # ── AADS-191: URL 감지 및 크롤링 ────────────────────────────────
        try:
            from app.services.media.url_processor import UrlProcessor
            _url_proc = UrlProcessor()
            _detected_urls = _url_proc.extract_urls(content)
            _url_contexts = []
            for _u in _detected_urls[:3]:  # 최대 3개 URL
                try:
                    _url_result = await _url_proc.process(_u)
                    if _url_result.content:
                        _url_text = _url_result.content[:5000]
                        _url_contexts.append(f"[URL 내용: {_url_result.url}]\n제목: {_url_result.title}\n{_url_text}")
                except Exception as _ue:
                    logger.warning(f"url_process_failed: {_ue}")
            if _url_contexts:
                _ephemeral_doc_context = (_ephemeral_doc_context + "\n\n" + "\n\n".join(_url_contexts)).strip()
                logger.info(f"[URL] {len(_url_contexts)} URL(s) crawled for session={session_id[:8]}")
        except Exception as _url_err:
            logger.warning(f"url_detection_skipped: {_url_err}")

        # ── P2-7: 멘션(@) 프로젝트 감지 → 명시적 프로젝트 컨텍스트 오버라이드 ──
        _MENTION_PROJECTS = {"KIS", "GO100", "AADS", "SF", "NTV2", "NAS"}
        _mention_matches = re.findall(r'@(KIS|GO100|AADS|SF|NTV2|NAS)\b', content, re.IGNORECASE)
        _mentioned_projects: list = []
        if _mention_matches:
            _mentioned_projects = list(dict.fromkeys(p.upper() for p in _mention_matches))
            logger.info(f"[MENTION] session={session_id[:8]} projects={_mentioned_projects}")

        # ★ Phase A: DB 커넥션 사용 구간 — async with로 자동 반환 (LLM 스트리밍 전 해제)
        # Reply-to: 이전 AI 응답 지정 시 인용 컨텍스트 주입
        _reply_to_uuid = None
        async with get_pool().acquire() as conn:
            if reply_to_id:
                try:
                    _reply_to_uuid = uuid.UUID(reply_to_id)
                    _quoted_row = await conn.fetchrow(
                        "SELECT content FROM chat_messages WHERE id = $1 AND session_id = $2",
                        _reply_to_uuid, sid,
                    )
                    if _quoted_row and _quoted_row["content"]:
                        _quoted = _quoted_row["content"][:2000]
                        content = f"[CEO가 지정한 이전 AI 응답 (reply_to)]\n{_quoted}\n\n[CEO 추가 지시]\n{content}"
                        logger.info(f"[REPLY_TO] session={session_id[:8]} reply_to={reply_to_id[:8]} quoted={len(_quoted)}chars")
                except Exception as _rte:
                    logger.warning(f"[REPLY_TO] failed: {_rte}")

            # auto_resume: 서버 재시작 후 자동 재실행 — 일반 사용자 메시지처럼 처리
            if intent_override == "auto_resume":
                logger.info(f"[AUTO_RESUME] session={session_id[:8]} re-processing after server restart")
                intent_override = None  # 일반 흐름으로 전환

            # 사용자 메시지 저장 (trigger 메시지는 intent로 구분)
            # model_override를 user 메시지의 model_used에 저장 → 재개 시 CEO 선택 모델 복원용
            # P2-2: branch 모드에서는 라우터에서 이미 저장했으므로 skip
            if not branch_point_msg_id:
                user_intent = "system_trigger" if intent_override else None
                await _save_message(conn, sid, "user", content, model_used=model_override, intent=user_intent, attachments=attachments or [], reply_to_id=_reply_to_uuid)

            # CEO 채팅 학습 트리거 (백그라운드, 비차단)
            if not intent_override:
                import asyncio as _asyncio_learn
                _learn_project = _mentioned_projects[0] if _mentioned_projects else None
                _asyncio_learn.create_task(_detect_and_save_learning(
                    session_id=session_id,
                    user_msg=content,
                    project=_learn_project,
                ))

            # 2. 워크스페이스 정보 조회
            sp_row = await conn.fetchrow(
                """
                SELECT w.id::text AS workspace_id, w.system_prompt, w.name AS workspace_name
                FROM chat_workspaces w
                JOIN chat_sessions s ON s.workspace_id = w.id
                WHERE s.id = $1
                """,
                sid,
            )
            base_prompt = (sp_row["system_prompt"] if sp_row and sp_row["system_prompt"] else "")
            workspace_name = (sp_row["workspace_name"] if sp_row and sp_row["workspace_name"] else "CEO")

            # 3. 세션 히스토리 조회 (#16: 서브쿼리로 ASC 정렬, Python reverse 제거)
            # P2-2: branch 모드일 때는 branch_point 메시지 이전(포함)까지만 히스토리 사용
            if branch_point_msg_id:
                _bp_uuid = uuid.UUID(branch_point_msg_id)
                _bp_row = await conn.fetchrow(
                    "SELECT created_at FROM chat_messages WHERE id = $1", _bp_uuid
                )
                if _bp_row:
                    hist_rows = await conn.fetch(
                        """
                        SELECT role, content FROM (
                            SELECT role, content, created_at FROM chat_messages
                            WHERE session_id = $1
                              AND (is_compacted IS NULL OR is_compacted = false)
                              AND branch_id IS NULL
                              AND created_at <= $2
                            ORDER BY created_at DESC LIMIT 200
                        ) sub ORDER BY created_at ASC
                        """,
                        sid, _bp_row["created_at"],
                    )
                else:
                    hist_rows = []
                # 분기 user 메시지를 히스토리 끝에 추가
                raw_messages = [{"role": r["role"], "content": r["content"]} for r in hist_rows]
                raw_messages.append({"role": "user", "content": content})
                logger.info(f"[BRANCH] session={session_id[:8]} branch_id={branch_id} point={branch_point_msg_id[:8]} hist={len(raw_messages)}")
            else:
                hist_rows = await conn.fetch(
                    """
                    SELECT role, content FROM (
                        SELECT role, content, created_at FROM chat_messages
                        WHERE session_id = $1 AND (is_compacted IS NULL OR is_compacted = false)
                        ORDER BY created_at DESC LIMIT 200
                    ) sub ORDER BY created_at ASC
                    """,
                    sid,
                )
                raw_messages = [{"role": r["role"], "content": r["content"]} for r in hist_rows]

            # 세션 누적 비용 조회 (프론트엔드 표시용)
            _session_cost_row = await conn.fetchrow(
                "SELECT cost_total, message_count FROM chat_sessions WHERE id = $1", sid
            )
            _session_cost = float(_session_cost_row["cost_total"] or 0) if _session_cost_row else 0
            _session_turns = int(_session_cost_row["message_count"] or 0) if _session_cost_row else 0

            # 4. 3계층 컨텍스트 빌드 (AADS-CRITICAL-FIX #7: fallback 방어)
            from app.services.context_builder import build_messages_context
            try:
                messages, system_prompt = await build_messages_context(
                    workspace_name=workspace_name,
                    session_id=session_id,
                    raw_messages=raw_messages,
                    base_system_prompt=base_prompt,
                    db_conn=conn,
                    document_context=_ephemeral_doc_context,
                )
            except Exception as _ctx_err:
                logger.error(f"context_builder failed, using raw fallback: {_ctx_err}")
                system_prompt = base_prompt or "You are a helpful AI assistant."
                messages = [{"role": m["role"], "content": m["content"]} for m in raw_messages[-20:]]

            # P2-7: 멘션된 프로젝트 컨텍스트를 system_prompt에 주입
            if _mentioned_projects:
                _mention_desc = {
                    "KIS": "KIS AI 자동매매 시스템 (서버62, /root/kis/)",
                    "GO100": "GO100 백억이 투자분석 플랫폼 (서버211, /root/kis-autotrade-v4/)",
                    "AADS": "AADS 자율 AI 개발 시스템 (서버68, /root/aads/)",
                    "SF": "SmartFarm 스마트팜 시스템 (서버65, /root/sf/)",
                    "NTV2": "NTV2 뉴톡 v2 서비스 (서버65, /root/ntv2/)",
                    "NAS": "NAS 스토리지 시스템 (서버65, /root/nas/)",
                }
                _proj_lines = [f"- {p}: {_mention_desc.get(p, p)}" for p in _mentioned_projects]
                _mention_ctx = (
                    "\n\n[CEO 멘션 프로젝트 — 아래 프로젝트를 대상으로 작업하세요]\n"
                    + "\n".join(_proj_lines)
                    + "\n이 멘션은 인텐트 분류보다 우선합니다. 반드시 해당 프로젝트 컨텍스트에서 답변하세요."
                )
                system_prompt = system_prompt + _mention_ctx
                logger.info(f"[MENTION] injected project context: {_mentioned_projects}")

            # Vision: 이미지가 있으면 마지막 user 메시지를 멀티모달 content 배열로 교체
            if _vision_images:
                for _vi in range(len(messages) - 1, -1, -1):
                    if messages[_vi].get("role") == "user":
                        _text = messages[_vi].get("content", "")
                        if isinstance(_text, str):
                            messages[_vi] = {
                                "role": "user",
                                "content": [{"type": "text", "text": _text}] + _vision_images,
                            }
                        break
                logger.info(f"[VISION] injected {len(_vision_images)} image(s) into last user message")

            # ★ #19: Agent SDK resume용 세션 설정 프리페치
            _session_settings: dict = {}
            try:
                _ss_row = await conn.fetchrow("SELECT settings FROM chat_sessions WHERE id = $1", sid)
                if _ss_row:
                    _session_settings = _row_to_dict(_ss_row).get("settings") or {}
            except Exception:
                pass

        # ★ Phase A 종료 — DB 커넥션 async with 블록 종료로 자동 반환 (LLM 스트리밍 중 점유 방지)

        # Pipeline Runner 등 도구에서 현재 세션 ID를 참조할 수 있도록 컨텍스트 변수 설정
        from app.services.tool_executor import current_chat_session_id
        current_chat_session_id.set(session_id)
        logger.info(f"[DIAG] current_chat_session_id SET to '{session_id}' in send_message_stream")

        # P2-2: 분기 모드 시 branch_id ContextVar 설정 → _save_message에서 자동 적용
        _current_branch_id.set(branch_id if branch_id else None)

        # 프로젝트명 정규화 (workspace_name → project code)
        _PROJECT_KEYS = ("KIS", "AADS", "GO100", "SF", "NTV2", "NAS", "CEO")
        _normalized_project = None
        if workspace_name:
            _ws_upper = workspace_name.upper()
            for _pk in _PROJECT_KEYS:
                if _pk in _ws_upper:
                    _normalized_project = _pk
                    break
            if not _normalized_project:
                _normalized_project = _ws_upper[:20]

        # P2-7: 멘션이 있으면 첫 번째 멘션 프로젝트로 _normalized_project 오버라이드
        if _mentioned_projects:
            _normalized_project = _mentioned_projects[0]

        # 4.4. 시맨틱 캐시 조회 (유사 질문 즉시 응답)
        # BUG-3 FIX: 운영/조회성 질문은 캐시 바이패스 (실시간 데이터 필요)
        _CACHE_BYPASS_KEYWORDS = (
            "상태", "확인", "점검", "검수", "조회", "진단", "로그", "서버",
            "DB", "현재", "최근", "지금", "오늘", "실행", "배포", "수정",
            "에러", "버그", "장애", "모니터", "헬스", "status", "check",
            "deploy", "fix", "error", "health", "지시서", "파이프라인",
        )
        _skip_cache = any(kw in content for kw in _CACHE_BYPASS_KEYWORDS)
        _semantic_cache_hit = None
        if _skip_cache:
            logger.debug(f"semantic_cache_bypassed: operational query detected, session={session_id[:8]}")
        else:
            try:
                from app.services.semantic_cache import SemanticCache
                _sem_cache = SemanticCache(pool=get_pool())
                _ws_id = sp_row["workspace_id"] if sp_row and sp_row.get("workspace_id") else None
                _semantic_cache_hit = await _sem_cache.lookup(content, workspace_id=_ws_id)
                if _semantic_cache_hit:
                    _cached_resp = _semantic_cache_hit.get("cached_response", "")
                    _cached_sim = _semantic_cache_hit.get("similarity", 0)
                    logger.info("semantic_cache_hit", similarity=f"{_cached_sim:.3f}", session=session_id[:8])
                    # 캐시 응답을 SSE 형식으로 직접 반환
                    yield f'data: {json.dumps({"type": "delta", "content": _cached_resp})}\n\n'
                    yield f'data: {json.dumps({"type": "message_stop", "cached": True})}\n\n'
                    # Phase C: 캐시 응답도 DB에 저장
                    await _save_and_update_session(
                        sid, _cached_resp,
                        session_id_str=session_id,
                        raw_messages=raw_messages,
                        model_used="semantic_cache",
                        intent=intent_override or "cache_hit",
                        cost=0.0, tokens_in=0, tokens_out=0,
                        tools_called=[], thinking_summary=None,
                    )
                    return
            except Exception as _sc_err:
                logger.debug(f"semantic_cache_lookup_skipped: {_sc_err}")

        # 4.5-pre. F10: Contradiction Detection (모순 감지)
        try:
            from app.services.contradiction_detector import detect_contradictions
            _contradiction_warning = await detect_contradictions(content, project=_normalized_project)
            if _contradiction_warning:
                system_prompt = system_prompt + "\n\n" + _contradiction_warning
                logger.info("contradiction_warning_injected", session=session_id[:8])
        except Exception as _cd_err:
            logger.debug(f"contradiction_detection_skipped: {_cd_err}")

        # 4.5. AADS-188E: 시맨틱 코드 검색 컨텍스트 주입 (code_search 관련 키워드 감지)
        _CODE_SEARCH_KEYWORDS = (
            "코드", "함수", "클래스", "어디", "어디야", "파일", "소스", "구현",
            "처리", "로직", "어디서", "찾아", "검색", "code", "where", "function",
        )
        if any(kw in content for kw in _CODE_SEARCH_KEYWORDS) and len(content) < 200:
            try:
                from app.services.semantic_code_search import SemanticCodeSearch
                _scs = SemanticCodeSearch()
                if _scs._is_available():
                    _search_results = await _scs.search(content, top_k=3)
                    if _search_results and not any("error" in r for r in _search_results):
                        _ctx_lines = ["<codebase_knowledge_inline>"]
                        for _r in _search_results[:3]:
                            _ctx_lines.append(
                                f"  {_r.get('file','?')}:{_r.get('start_line','?')} "
                                f"[{_r.get('type','?')}] {_r.get('name','?')} "
                                f"(유사도: {_r.get('similarity_score', 0):.2f})"
                            )
                            if _r.get("code_snippet"):
                                _ctx_lines.append(f"    {_r['code_snippet'][:150]}")
                        _ctx_lines.append("</codebase_knowledge_inline>")
                        _inline_ctx = "\n".join(_ctx_lines)
                        # 시스템 프롬프트 마지막에 삽입
                        system_prompt = system_prompt + "\n\n" + _inline_ctx
                        # #20: 시맨틱 코드 검색 감사 로그
                        logger.info("semantic_code_search_injected",
                                    query=content[:100], results=len(_search_results),
                                    files=[r.get('file','?') for r in _search_results[:3]],
                                    tokens_est=len(_inline_ctx.encode('utf-8')) // 3)
            except Exception as _sce:
                logger.debug(f"[188E] 시맨틱 코드 검색 컨텍스트 주입 실패 (무시): {_sce}")

        # 5. 자동 압축은 context_builder.build_messages_context() 내에서 토큰 기반으로 트리거됨
        # (80K 토큰 초과 시 compaction_service.check_and_compact 자동 호출)

        # 6. 인텐트 분류 + 모델/도구 결정
        from app.services.intent_router import classify, get_model_for_override
        intent_result = await classify(content, workspace_name, recent_messages=messages)
        intent = intent_override if intent_override else intent_result.intent
        # Langfuse: intent_classification span
        if _lf_trace is not None:
            try:
                _lf_span_intent = _lf_trace.span(
                    name="intent_classification",
                    input={"content": content[:300], "workspace": workspace_name},
                    output={"intent": intent, "model": intent_result.model, "use_tools": intent_result.use_tools},
                    metadata={"use_gemini_direct": intent_result.use_gemini_direct},
                )
                if _lf_span_intent:
                    _lf_span_intent.end()
            except Exception:
                pass

        # 6.4. casual/greeting 인텐트인데 도구가 필요한 키워드 감지 → 도구 활성화
        _tool_requiring_keywords = (
            "확인", "조회", "점검", "검수", "진단", "분석", "조사", "체크", "보고",
            "수정", "배포", "실행", "테스트", "로그", "상태", "서버", "DB", "쿼리",
            "코드", "파일", "에러", "버그", "fix", "deploy", "check", "status",
        )
        if intent in ("casual", "greeting") and not intent_result.use_tools:
            if any(kw in content for kw in _tool_requiring_keywords) and len(content) > 5:
                intent_result.use_tools = True
                if not intent_result.tool_group:
                    intent_result.tool_group = "all"
                logger.info(f"[INTENT_FIX] casual→tool_enabled for keyword match in: {content[:80]}")

        # 6.5. 첨부파일 키워드 감지 → file_read 인텐트 강제 (업로드 파일 재읽기)
        _file_keywords = ("업로드한 파일", "첨부파일", "첨부한 파일", "파일 읽어", "파일 다시", "이전 파일", "올린 파일", "파일 검토")
        if any(kw in content for kw in _file_keywords) and not intent_result.use_tools:
            from app.services.intent_router import INTENT_MAP, IntentResult as _IR
            _fm = INTENT_MAP.get("file_read", {})
            intent_result = _IR(
                intent="file_read", model=_fm.get("model", "claude-sonnet"),
                use_tools=True, tool_group="all",
            )
            intent = "file_read"
            logger.info(f"[INTENT_OVERRIDE] file_read forced for content containing file keywords")

        if model_override and model_override not in ("mixture", "auto"):
            intent_result.model = get_model_for_override(model_override)
            intent_result.use_gemini_direct = False
            # Claude 모델 선택 시 도구 항상 활성화 (시스템프롬프트에 도구 설명이 있으므로
            # tools 없이 호출하면 <tool_call> XML 할루시네이션 발생)
            _override_lower = (model_override or "").lower()
            if "claude" in _override_lower or "opus" in _override_lower or "sonnet" in _override_lower or "haiku" in _override_lower:
                intent_result.use_tools = True
                if not intent_result.tool_group:
                    intent_result.tool_group = "all"

        # 7. Gemini Direct (Grounding / Deep Research)
        if intent_result.use_gemini_direct:
            if intent_result.gemini_mode == "grounding":
                import asyncio as _search_asyncio
                from app.services.gemini_search_service import GeminiSearchService
                from app.services.naver_search_service import NaverSearchService

                svc = GeminiSearchService()

                async def _try_gemini():
                    try:
                        return await svc.search_grounded(content)
                    except Exception as e:
                        logger.warning(f"gemini_grounding_failed: {e}")
                        return None

                async def _try_naver():
                    try:
                        naver = NaverSearchService()
                        if not naver.is_available():
                            return None
                        naver_type = getattr(intent_result, "naver_type", "")
                        if naver_type:
                            r = await naver.search(content, search_type=naver_type, count=5)
                        else:
                            r = await naver.multi_search(content, count=3)
                        return None if r.error else r
                    except Exception as e:
                        logger.warning(f"naver_search_failed: {e}")
                        return None

                # Gemini + Naver 병렬 실행
                gemini_result, naver_result = await _search_asyncio.gather(
                    _try_gemini(), _try_naver()
                )
                result = gemini_result or naver_result  # Gemini 우선

                # 둘 다 실패 시 Kakao → Brave 순차 폴백
                if result is None:
                    from app.services.kakao_search_service import KakaoSearchService
                    kakao = KakaoSearchService()
                    if kakao.is_available():
                        try:
                            result = await kakao.search(content)
                            if result.error:
                                result = None
                        except Exception as e:
                            logger.warning(f"kakao_search_failed: {e}")
                if result is None:
                    from app.services.brave_search_service import BraveSearchService
                    brave = BraveSearchService()
                    result = await brave.search(content)
                yield f"data: {json.dumps({'type': 'delta', 'content': result.text})}\n\n"
                if result.citations:
                    yield f"data: {json.dumps({'type': 'sources', 'sources': result.citations})}\n\n"
                # F7: 실제 비용 추정 — result.cost가 있으면 사용, 없으면 토큰 기반 추정
                _search_cost = getattr(result, "cost", None)
                if _search_cost is None or _search_cost == Decimal("0"):
                    from app.core.token_utils import estimate_tokens
                    _in_tok = estimate_tokens(content)
                    _out_tok = estimate_tokens(result.text)
                    # Gemini Flash: $0.075/1M in, $0.3/1M out
                    _search_cost = Decimal(str(round(_in_tok * 0.075 / 1_000_000 + _out_tok * 0.3 / 1_000_000, 6)))
                await _save_and_update_session(
                    sid, result.text, model_used="gemini-flash", intent=intent,
                    cost=_search_cost, sources=result.citations)
                yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': 'gemini-flash', 'cost': str(_search_cost)})}\n\n"
                return

            elif intent_result.gemini_mode == "deep_research":
                from app.services.deep_research_service import DeepResearchService
                dr_svc = DeepResearchService()
                if not dr_svc.is_available():
                    # API 키 없으면 Claude 폴백
                    intent_result.model = "claude-sonnet"
                    intent_result.use_gemini_direct = False
                else:
                    try:
                        # research_start SSE 발송
                        yield f"data: {json.dumps({'type': 'research_start', 'message': '딥리서치를 시작합니다... (3~10분 소요, 수십 개 소스 탐색)'})}\n\n"

                        collected_report_parts: list[str] = []
                        final_citations: list[dict] = []
                        final_interaction_id = ""
                        cost_usd = 0.0  # F7: 고정값 제거, 실제 토큰 기반 추정

                        # AADS-188A: research_stream() 사용 — planning/searching/analyzing 실시간 SSE
                        async for ev in await dr_svc.research_stream(content, timeout=600):
                            ev_type = ev.type
                            if ev_type in ("planning", "searching", "analyzing"):
                                yield f"data: {json.dumps({'type': 'research_progress', 'phase': ev_type, 'content': ev.content or '', 'progress_pct': ev.progress_pct or 0})}\n\n"
                            elif ev_type == "thinking" and ev.content:
                                yield f"data: {json.dumps({'type': 'thinking', 'thinking': (ev.content or '')[:2000]})}\n\n"
                            elif ev_type == "content" and ev.content:
                                collected_report_parts.append(ev.content)
                                yield f"data: {json.dumps({'type': 'delta', 'content': ev.content})}\n\n"
                            elif ev_type == "complete":
                                if ev.content and not collected_report_parts:
                                    # 청크 없이 완료된 경우 — 보고서를 delta로 분할 전송
                                    chunk_size = 500
                                    report_text = ev.content
                                    collected_report_parts.append(report_text)
                                    for i in range(0, len(report_text), chunk_size):
                                        yield f"data: {json.dumps({'type': 'delta', 'content': report_text[i:i+chunk_size]})}\n\n"
                                if ev.sources:
                                    final_citations = ev.sources
                                if ev.interaction_id:
                                    final_interaction_id = ev.interaction_id
                            elif ev_type == "error":
                                # error 이벤트: Claude 폴백으로 이동
                                raise Exception(ev.content or "deep_research error")

                        report_text = "".join(collected_report_parts)

                        if final_citations:
                            yield f"data: {json.dumps({'type': 'sources', 'sources': final_citations})}\n\n"

                        # F7: 딥리서치 비용 — 토큰 기반 추정 (Gemini Pro 요금 적용)
                        if cost_usd == 0.0:
                            from app.core.token_utils import estimate_tokens
                            _dr_in = estimate_tokens(content)
                            _dr_out = estimate_tokens(report_text)
                            # Deep Research: 내부적으로 다수 API 호출 → Gemini Pro 요금 × 10 (경험치)
                            cost_usd = round(_dr_in * 1.25 / 1_000_000 * 10 + _dr_out * 5.0 / 1_000_000 * 10, 4)
                            if cost_usd < 0.5:
                                cost_usd = 0.5  # 최소 $0.50 (수십 페이지 크롤링 비용)

                        yield f"data: {json.dumps({'type': 'research_complete', 'interaction_id': final_interaction_id, 'cost': str(cost_usd)})}\n\n"

                        await _save_and_update_session(
                            sid, report_text, model_used="gemini-deep-research", intent=intent,
                            cost=Decimal(str(cost_usd)), sources=final_citations)
                        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': 'gemini-deep-research', 'cost': str(cost_usd)})}\n\n"
                        return
                    except Exception as e:
                        logger.warning(f"gemini_deep_research_failed: {e}")
                        intent_result.model = "claude-sonnet"
                        intent_result.use_gemini_direct = False

        # 8. 도구 목록 (Anthropic Tool Use 포맷)
        # 인텐트 오분류 방지: tools=False여도 핵심 도구는 항상 제공
        from app.services.tool_registry import ToolRegistry
        _registry = ToolRegistry()
        if intent_result.use_tools:
            tools_for_api = _registry.get_tools(intent_result.tool_group)
        else:
            # Gemini 직접 호출 모델은 도구 미지원 → 제외
            if not intent_result.use_gemini_direct:
                tools_for_api = _registry.get_eager_tools()
            else:
                tools_for_api = None

        # 8.5a. AADS-188C: Agent SDK 실시간 자율 실행 (execute/code_modify 인텐트)
        # CEO 명시적 직접 실행 지시 시에만 Agent SDK, 그 외는 Runner 위임
        # Runner 위임 대상: 코드 작업 + CTO 분석 + 서비스 점검 (모든 자율 실행 인텐트)
        _RUNNER_DELEGATION_INTENTS = frozenset({
            "execute", "code_modify", "code_task",
            "cto_code_analysis", "cto_verify", "service_inspection", "cto_impact",
        })
        _DIRECT_EXECUTION_TRIGGERS = (
            "직접 해", "직접 수정", "여기서 해", "여기서 수정", "바로 고쳐",
            "바로 수정", "세션에서 해", "세션에서 수정", "직접 처리",
        )
        if intent in _RUNNER_DELEGATION_INTENTS and any(t in content for t in _DIRECT_EXECUTION_TRIGGERS):
            logger.info(f"[DIRECT_EXECUTION] session={sid_short} intent={intent} trigger_matched=True")
            # resume 지원: Phase A에서 프리페치한 세션 설정 사용 (#19)
            sdk_session_id: Optional[str] = _session_settings.get("sdk_session_id")

            from app.services.agent_sdk_service import get_agent_sdk_service, AGENT_SDK_ENABLED as _sdk_flag
            sdk_svc = get_agent_sdk_service()
            sdk_success = False

            if sdk_svc.is_available() and _sdk_flag:
                try:
                    full_response = ""
                    _captured_sdk_sid: Optional[str] = None
                    model_used = "claude-opus-4-6"
                    cost_usd = Decimal("0")
                    tools_called: list = []

                    async for sse_line in sdk_svc.execute_stream(
                        prompt=content,
                        session_id=sdk_session_id,
                    ):
                        yield sse_line
                        # 이벤트 파싱: session_id 캡처 + 텍스트 수집
                        try:
                            _ev = json.loads(sse_line.replace("data: ", "").strip())
                            _et = _ev.get("type", "")
                            if _et == "sdk_session":
                                _captured_sdk_sid = _ev.get("session_id")
                            elif _et == "delta":
                                full_response += _ev.get("content", "")
                            elif _et == "sdk_complete":
                                sdk_success = True
                        except Exception:
                            pass

                    # #19: sdk_session_id 저장 (별도 커넥션)
                    if _captured_sdk_sid:
                        try:
                            _new_settings = {**_session_settings, "sdk_session_id": _captured_sdk_sid}
                            async with get_pool().acquire() as _c:
                                await _c.execute(
                                    "UPDATE chat_sessions SET settings = $1::jsonb, updated_at = NOW() WHERE id = $2",
                                    json.dumps(_new_settings), sid,
                                )
                        except Exception as _se:
                            logger.debug(f"sdk_session_id 저장 실패: {_se}")

                    if sdk_success:
                        # 날조 방지: SDK 경로도 검증
                        from app.services.output_validator import validate_response as _sdk_validate
                        _sdk_val = _sdk_validate(
                            response_text=full_response,
                            tools_called=bool(tools_called),
                            intent=intent,
                        )
                        if not _sdk_val.is_valid:
                            logger.error(f"sdk_path_validation_failed: {_sdk_val.violation_type} — {_sdk_val.message}")
                            for _tag in ("function_calls", "function_response", "function_results", "tool_results"):
                                full_response = re.sub(rf'<{_tag}>.*?</{_tag}>', '', full_response, flags=re.DOTALL)
                                full_response = re.sub(rf'<{_tag}>.*', '', full_response, flags=re.DOTALL)
                            full_response = re.sub(r'<invoke\s+name=[^>]*>.*?</invoke>', '', full_response, flags=re.DOTALL)
                            full_response = re.sub(r'<invoke\s+name=[^>]*>.*', '', full_response, flags=re.DOTALL)
                        await _save_and_update_session(
                            sid, full_response, model_used=model_used, intent=intent,
                            cost=cost_usd, tools_called=tools_called)
                        yield f"data: {json.dumps({'type': 'done', 'stream_id': _stream_id, 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'agent_sdk': True})}\n\n"
                        return

                except Exception as _sdk_err:
                    logger.warning(f"agent_sdk_failed (fallback to bridge): {_sdk_err}")
                    # SDK 실패 → AutonomousExecutor fallback으로 계속 진행

        # 8.5b. execute/code_modify 인텐트 중 직접 실행 조건 미충족 → Runner 위임
        if intent in _RUNNER_DELEGATION_INTENTS and not any(t in content for t in _DIRECT_EXECUTION_TRIGGERS):
            logger.info(f"[RUNNER_DELEGATION] session={sid_short} intent={intent} → pipeline_runner")
            intent = "pipeline_runner"

        # 8.5. 복잡 인텐트 → AutonomousExecutor (max_iterations=25) (AADS-186E-3)
        _AUTONOMOUS_INTENTS = frozenset({
            "pipeline_runner",
        })
        if intent in _AUTONOMOUS_INTENTS and intent_result.use_tools and tools_for_api:
            # Pipeline Runner: 시스템 프롬프트에 파이프라인 가이드 주입
            _auto_system = system_prompt
            if intent == "pipeline_runner":
                _auto_system += (
                    "\n[Runner모드] 도구: submit→status→approve. "
                    "프로젝트: KIS/GO100(211), SF/NTV2(114), AADS(68). "
                    "미지정 시 확인 필수. 승인 시 diff 먼저 확인 후 보고."
                )
            from app.services.autonomous_executor import AutonomousExecutor
            auto_exec = AutonomousExecutor(max_iterations=60, cost_limit=10.0)
            full_response = ""
            thinking_summary = ""
            model_used = intent_result.model
            cost_usd = Decimal("0")
            input_tokens = 0
            output_tokens = 0
            tools_called: list = []

            async for sse_line in auto_exec.execute_task(
                task_description="",
                tools=tools_for_api,
                messages=messages,
                model=intent_result.model,
                system_prompt=_auto_system,
                session_id=session_id,
            ):
                yield sse_line
                # 완료/비용/오류 이벤트 파싱하여 응답 수집
                try:
                    import json as _json
                    _data = _json.loads(sse_line.replace("data: ", "").strip())
                    _etype = _data.get("type", "")
                    if _etype == "delta":
                        full_response += _data.get("content", "")
                    elif _etype in ("complete", "max_iterations", "cost_limit"):
                        cost_usd = Decimal(str(_data.get("total_cost", "0")))
                        if _etype == "complete":
                            full_response = _data.get("content", full_response)
                    elif _etype == "tool_use":
                        tools_called.append(_data.get("tool_name", ""))
                except Exception:
                    pass

            # #19: 날조 방지 검증 후 응답 저장 (별도 커넥션)
            from app.services.output_validator import validate_response as _auto_validate
            _auto_val = _auto_validate(
                response_text=full_response,
                tools_called=bool(tools_called),
                intent=intent,
            )
            if not _auto_val.is_valid:
                logger.error(f"autonomous_executor_validation_failed: {_auto_val.violation_type} — {_auto_val.message}")
                for _tag in ("function_calls", "function_response", "function_results", "tool_results"):
                    full_response = re.sub(rf'<{_tag}>.*?</{_tag}>', '', full_response, flags=re.DOTALL)
                    full_response = re.sub(rf'<{_tag}>.*', '', full_response, flags=re.DOTALL)
                full_response = re.sub(r'<invoke\s+name=[^>]*>.*?</invoke>', '', full_response, flags=re.DOTALL)
                full_response = re.sub(r'<invoke\s+name=[^>]*>.*', '', full_response, flags=re.DOTALL)
            await _save_and_update_session(
                sid, full_response, model_used=model_used, intent=intent,
                cost=cost_usd, tools_called=tools_called)
            yield f"data: {json.dumps({'type': 'done', 'stream_id': _stream_id, 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'input_tokens': 0, 'output_tokens': 0, 'autonomous': True})}\n\n"
            return

        # 9. 모델 선택기 → SSE 스트리밍
        from app.services.model_selector import call_stream
        # Langfuse: llm_generation span 시작
        if _lf_trace is not None:
            try:
                _lf_span_llm = _lf_trace.span(
                    name="llm_generation",
                    input={"model": intent_result.model, "intent": intent},
                )
            except Exception:
                pass
        full_response = ""
        thinking_summary = ""
        model_used = intent_result.model
        cost_usd = Decimal("0")
        input_tokens = 0
        output_tokens = 0
        tools_called: list = []

        async for event in call_stream(
            intent_result=intent_result,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools_for_api,
            model_override=model_override,
            session_id=session_id,
        ):
            etype = event.get("type", "")
            if etype == "heartbeat":
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            elif etype == "model_info":
                model_used = event.get("model", model_used)
                yield f"data: {json.dumps({'type': 'model_info', 'model': model_used})}\n\n"
            elif etype == "interrupt_applied":
                yield f"event: interrupt_applied\ndata: {json.dumps({'type': 'interrupt_applied', 'content': event.get('content', '')})}\n\n"
            elif etype == "delta":
                full_response += event.get("content", "")
                yield f"data: {json.dumps({'type': 'delta', 'content': event['content']})}\n\n"
            elif etype == "thinking":
                thinking_summary += event.get("thinking", "")
                yield f"data: {json.dumps({'type': 'thinking', 'thinking': event['thinking']})}\n\n"
            elif etype == "tool_use":
                tools_called.append(event["tool_name"])
                yield f"data: {json.dumps({'type': 'tool_use', 'tool_name': event['tool_name'], 'tool_use_id': event['tool_use_id'], 'tool_input': event.get('tool_input', {})})}\n\n"
            elif etype == "tool_result":
                yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'content': str(event.get('content', ''))[:300]})}\n\n"
            elif etype == "yellow_limit":
                yield f"data: {json.dumps({'type': 'yellow_limit', 'content': event.get('content', ''), 'tool_name': event.get('tool_name', ''), 'consecutive_count': event.get('consecutive_count', 0)})}\n\n"
            elif etype == "done":
                model_used = event.get("model", intent_result.model)
                cost_usd = Decimal(str(event.get("cost", "0")))
                input_tokens = event.get("input_tokens", 0) or 0
                output_tokens = event.get("output_tokens", 0) or 0
                thinking_summary = event.get("thinking_summary") or thinking_summary
                tools_called = event.get("tools_called", tools_called)
            elif etype == "error":
                yield f"data: {json.dumps({'type': 'error', 'content': event.get('content', '오류')})}\n\n"
                return

        # 9.5 Layer ④: Output Validator — 빈 약속 응답 감지 및 재시도 (AADS-188C Phase 3)
        from app.services.output_validator import validate_response
        _validation = validate_response(
            response_text=full_response,
            tools_called=bool(tools_called),
            intent=intent,
        )
        if not _validation.is_valid:
            logger.warning(
                f"output_validator: {_validation.violation_type} — {_validation.message} "
                f"(intent={intent}, model={model_used}, tokens_out={output_tokens})"
            )
            # F8: 클라이언트에 stream_reset 전송 — 이전 잘못된 텍스트 초기화
            yield f"data: {json.dumps({'type': 'stream_reset', 'reason': _validation.violation_type})}\n\n"
            # DB 저장 시 재시도 응답만 사용하도록 원본 응답 별도 보관
            _failed_response = full_response
            full_response = ""

            # 재시도: output_validator가 생성한 retry_prompt 사용
            _retry_messages = list(messages)
            _retry_messages.append({"role": "assistant", "content": _failed_response.strip()})
            _retry_messages.append({"role": "user", "content": _validation.retry_prompt})

            _retry_response = ""
            async for event in call_stream(
                intent_result=intent_result,
                system_prompt=system_prompt,
                messages=_retry_messages,
                tools=tools_for_api,
                model_override=model_override,
                session_id=session_id,
            ):
                etype = event.get("type", "")
                if etype == "heartbeat":
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                elif etype == "delta":
                    _retry_response += event.get("content", "")
                    yield f"data: {json.dumps({'type': 'delta', 'content': event['content']})}\n\n"
                elif etype == "thinking":
                    yield f"data: {json.dumps({'type': 'thinking', 'thinking': event['thinking']})}\n\n"
                elif etype == "tool_use":
                    tools_called.append(event["tool_name"])
                    yield f"data: {json.dumps({'type': 'tool_use', 'tool_name': event['tool_name'], 'tool_use_id': event['tool_use_id'], 'tool_input': event.get('tool_input', {})})}\n\n"
                elif etype == "tool_result":
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'content': str(event.get('content', ''))[:300]})}\n\n"
                elif etype == "done":
                    model_used = event.get("model", intent_result.model)
                    cost_usd += Decimal(str(event.get("cost", "0")))
                    input_tokens = event.get("input_tokens", 0) or 0
                    output_tokens = event.get("output_tokens", 0) or 0
                    tools_called = event.get("tools_called", tools_called)
                elif etype == "error":
                    yield f"data: {json.dumps({'type': 'error', 'content': event.get('content', '오류')})}\n\n"
                    return

            # 재시도 응답도 검증 (날조 방지 — 재시도에서도 가짜 결과 차단)
            if _retry_response.strip():
                _retry_validation = validate_response(
                    response_text=_retry_response,
                    tools_called=bool(tools_called),
                    intent=intent,
                )
                if not _retry_validation.is_valid:
                    logger.error(
                        f"output_validator_retry_also_failed: {_retry_validation.violation_type} — "
                        f"{_retry_validation.message} (intent={intent})"
                    )
                    # 재시도도 실패하면 날조된 부분 제거하고 경고 메시지로 대체
                    _retry_response = (
                        "⚠️ 요청을 처리하는 중 검증에 실패했습니다. "
                        "정확한 정보를 위해 도구를 직접 호출하여 확인해주세요."
                    )
                # F8: stream_reset했으므로 재시도 응답만 저장 (이전 실패 응답 제외)
                full_response = _retry_response

        # ═══ #19: Phase C — 응답 저장 (별도 커넥션) ═══
        _thinking_truncated = (thinking_summary or "")[:2000] or None
        if thinking_summary and len(thinking_summary) > 2000:
            logger.info(f"thinking_truncated original_len={len(thinking_summary)} session={session_id[:8]}")
        await _save_and_update_session(
            sid, full_response,
            session_id_str=session_id,
            raw_messages=raw_messages,
            model_used=model_used,
            intent=intent,
            cost=cost_usd,
            tokens_in=input_tokens,
            tokens_out=output_tokens,
            tools_called=tools_called,
            thinking_summary=_thinking_truncated,
            auto_save_check=True,
        )

        # ═══ Phase C-1.5: Output Validator violation → quality_details 기록 ═══
        if not _validation.is_valid:
            try:
                _violation_details = json.dumps({
                    "violation_type": _validation.violation_type,
                    "violation_message": _validation.message,
                    "retried": True,
                    "original_response_len": len(_failed_response) if '_failed_response' in dir() else 0,
                })
                async with get_pool().acquire() as _viol_conn:
                    await _viol_conn.execute(
                        """
                        UPDATE chat_messages
                        SET quality_details = $1::jsonb,
                            quality_score = CASE WHEN quality_score IS NULL THEN 0.2 ELSE LEAST(quality_score, 0.3) END
                        WHERE id = (
                            SELECT id FROM chat_messages
                            WHERE session_id = $2 AND role = 'assistant'
                            ORDER BY created_at DESC LIMIT 1
                        )
                        """,
                        _violation_details, sid,
                    )
                logger.info(f"output_validator_violation_recorded: {_validation.violation_type}", session_id=session_id)
            except Exception as _viol_err:
                logger.warning(f"output_validator_violation_save_error: {_viol_err}")

        # ═══ Phase C-2: Memory Upgrade Background Tasks ═══
        import asyncio as _bg_asyncio
        # F2: Fact Extraction (핵심사실 추출)
        try:
            from app.services.fact_extractor import extract_facts
            _bg_asyncio.create_task(
                extract_facts(content, full_response, session_id,
                              workspace_id=None,
                              project=_normalized_project)
            )
        except Exception as _bg_err:
            logger.debug("bg_fact_extraction_launch_error", error=str(_bg_err))
        # F8: CEO Pattern Tracking
        try:
            from app.services.ceo_pattern_tracker import track_interaction
            _bg_asyncio.create_task(
                track_interaction(content, workspace_name=workspace_name, intent=intent)
            )
        except Exception as _bg_err:
            logger.debug("bg_ceo_pattern_launch_error", error=str(_bg_err))
        # F11: Self-Evaluation
        try:
            from app.services.self_evaluator import evaluate_response
            # 최근 저장된 assistant message ID 조회
            async with get_pool().acquire() as _eval_conn:
                _last_msg = await _eval_conn.fetchval(
                    "SELECT id::text FROM chat_messages WHERE session_id = $1 AND role = 'assistant' ORDER BY created_at DESC LIMIT 1",
                    sid,
                )
            if _last_msg:
                _bg_asyncio.create_task(
                    evaluate_response(content, full_response, _last_msg,
                                       session_id=session_id, project=_normalized_project,
                                       prev_messages=messages[-8:] if messages else None)
                )
        except Exception as _bg_err:
            logger.debug("bg_self_eval_launch_error", error=str(_bg_err))

        # 시맨틱 캐시 저장 (도구 사용 + quality >= 0.7인 응답만)
        if tools_called and len(full_response) > 200:
            try:
                from app.services.semantic_cache import SemanticCache
                _sem_cache_store = SemanticCache(pool=get_pool())
                _ws_id = sp_row["workspace_id"] if sp_row and sp_row.get("workspace_id") else None
                _bg_asyncio.create_task(
                    _sem_cache_store.store(content, full_response, quality_score=0.7, workspace_id=_ws_id)
                )
            except Exception as _sc_store_err:
                logger.debug(f"semantic_cache_store_skipped: {_sc_store_err}")

        # 누적 비용 업데이트
        _session_cost += float(cost_usd)
        _session_turns += 2  # user + assistant

        yield f"data: {json.dumps({'type': 'done', 'stream_id': _stream_id, 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'input_tokens': input_tokens, 'output_tokens': output_tokens, 'thinking_summary': (thinking_summary[:2000] if thinking_summary else None), 'session_cost': f'${_session_cost:.2f}', 'session_turns': _session_turns})}\n\n"

    finally:
        set_streaming(session_id, False)


# ── CEO 채팅 학습 트리거 ────────────────────────────────────────────
_LEARNING_TRIGGERS = {
    "correction": ["아니", "틀렸", "그게 아니라", "다시 해", "잘못", "아닌데"],
    "preference": ["항상", "앞으로", "기억해", "절대", "반드시", "무조건", "금지"],
    "positive": ["잘했", "좋아", "이대로", "완벽", "훌륭", "정확"],
}


async def _detect_and_save_learning(
    session_id: str,
    user_msg: str,
    project: str = None,
) -> bool:
    """CEO 메시지에서 학습 신호 감지 → observation 자동 저장.

    Returns: True if learning was saved
    """
    detected_category = None
    for category, triggers in _LEARNING_TRIGGERS.items():
        if any(t in user_msg for t in triggers):
            detected_category = category
            break

    if not detected_category:
        return False

    import hashlib
    key = f"chat_learning_{hashlib.md5(user_msg[:50].encode()).hexdigest()[:8]}"

    if detected_category == "correction":
        content = f"CEO 교정: '{user_msg[:100]}' → AI 응답 수정 필요"
        confidence = 0.7
        save_category = "ceo_correction"
    elif detected_category == "preference":
        content = f"CEO 선호: {user_msg[:200]}"
        confidence = 0.8
        save_category = "ceo_preference"
    else:  # positive
        content = f"CEO 긍정 피드백: {user_msg[:100]}"
        confidence = 0.6
        save_category = "ceo_preference"

    try:
        from app.core.memory_recall import save_observation
        await save_observation(
            category=save_category,
            key=key,
            content=content,
            source="chat_learning",
            confidence=confidence,
            project=project,
        )
        logger.info(f"chat_learning_saved: category={save_category} key={key} session={session_id[:8]}")
        return True
    except Exception as e:
        logger.warning(f"chat_learning_save_failed: {e}")
        return False


async def _auto_save_session_note(session_id: str, messages: List[Dict[str, Any]]) -> None:
    """20턴 컴팩션 시 자동 세션 노트 저장 (백그라운드 태스크)."""
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        await mgr.save_session_note(session_id=session_id, messages=messages)
        logger.info(f"auto_save_session_note: session_id={session_id}")
    except Exception as e:
        logger.warning(f"auto_save_session_note error: {e}")


async def _auto_observe_session(messages: List[Dict[str, Any]]) -> None:
    """세션 종료 시 자동 패턴 관찰 (백그라운드 태스크, AADS-186E-3)."""
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        await mgr.auto_observe_from_session(messages)
        logger.info("auto_observe_session: 완료")
    except Exception as e:
        logger.warning(f"auto_observe_session error: {e}")


async def toggle_bookmark(message_id: str) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE chat_messages SET bookmarked = NOT bookmarked WHERE id = $1 RETURNING *",
            uuid.UUID(message_id),
        )
        return _row_to_dict(row) if row else None


async def update_message(message_id: str, new_content: str) -> Optional[Dict[str, Any]]:
    """사용자 메시지 내용 수정 (role=user만 허용). edited_at 기록. B2: CEO 교정 학습."""
    async with get_pool().acquire() as conn:
        # B2: 수정 전 원본 내용 조회
        original_row = await conn.fetchrow(
            "SELECT content, session_id FROM chat_messages WHERE id = $1 AND role = 'user'",
            uuid.UUID(message_id),
        )
        original_content = original_row["content"] if original_row else None
        original_session_id = original_row["session_id"] if original_row else None

        row = await conn.fetchrow(
            """
            UPDATE chat_messages
            SET content = $2, edited_at = NOW()
            WHERE id = $1 AND role = 'user'
            RETURNING *
            """,
            uuid.UUID(message_id),
            new_content,
        )

        # B2: CEO correction learning — 수정 내용을 memory_facts에 저장
        if row and original_content and original_content != new_content:
            try:
                # 프로젝트 정보 조회
                proj_row = await conn.fetchrow(
                    """SELECT w.name FROM chat_workspaces w
                       JOIN chat_sessions s ON s.workspace_id = w.id
                       WHERE s.id = $1""",
                    original_session_id,
                )
                proj_name = proj_row["name"] if proj_row else None
                # 프로젝트 정규화
                _proj = None
                if proj_name:
                    for _pk in ("KIS", "AADS", "GO100", "SF", "NTV2", "NAS", "CEO"):
                        if _pk in proj_name.upper():
                            _proj = _pk
                            break

                diff_summary = f"원본: {original_content[:150]} → 수정: {new_content[:150]}"
                await conn.execute(
                    """INSERT INTO memory_facts (session_id, project, category, subject, detail, confidence, tags)
                       VALUES ($1, $2, 'ceo_instruction', $3, $4, 0.9, ARRAY['ceo_correction', 'learning'])""",
                    original_session_id,
                    _proj,
                    f"CEO 교정: {new_content[:80]}",
                    diff_summary[:500],
                )
                logger.info("b2_ceo_correction_saved", message_id=message_id[:8])
            except Exception as e_b2:
                logger.debug("b2_ceo_correction_error", error=str(e_b2))

        return _row_to_dict(row) if row else None


async def delete_message_and_response(message_id: str) -> int:
    """
    사용자 메시지 삭제 + 바로 뒤의 AI 응답도 함께 삭제.
    방식A(수정재전송)에서 기존 메시지+응답 제거 용도.
    Returns 삭제된 메시지 수.
    """
    async with get_pool().acquire() as conn:
        # 먼저 해당 메시지 정보 조회
        msg = await conn.fetchrow(
            "SELECT id, session_id, role, created_at FROM chat_messages WHERE id = $1",
            uuid.UUID(message_id),
        )
        if not msg:
            return 0

        session_id = msg["session_id"]
        created_at = msg["created_at"]

        # 해당 메시지 + 바로 다음 AI 응답 삭제
        # (created_at 이후 가장 가까운 assistant 메시지 1개)
        next_ai = await conn.fetchrow(
            """
            SELECT id FROM chat_messages
            WHERE session_id = $1 AND role = 'assistant' AND created_at > $2
            ORDER BY created_at ASC LIMIT 1
            """,
            session_id,
            created_at,
        )

        ids_to_delete = [msg["id"]]
        if next_ai:
            ids_to_delete.append(next_ai["id"])

        async with conn.transaction():
            deleted = await conn.execute(
                "DELETE FROM chat_messages WHERE id = ANY($1::uuid[])",
                ids_to_delete,
            )
            count = int(deleted.split()[-1])

            # message_count 갱신
            if count > 0:
                await conn.execute(
                    "UPDATE chat_sessions SET message_count = GREATEST(message_count - $2, 0), updated_at = NOW() WHERE id = $1",
                    session_id,
                    count,
                )
        return count


async def search_messages(query: str, workspace_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        if workspace_id:
            rows = await conn.fetch(
                """
                SELECT m.* FROM chat_messages m
                JOIN chat_sessions s ON s.id = m.session_id
                WHERE s.workspace_id = $1
                  AND to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $2)
                ORDER BY m.created_at DESC LIMIT $3
                """,
                uuid.UUID(workspace_id),
                query,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM chat_messages
                WHERE to_tsvector('simple', content) @@ plainto_tsquery('simple', $1)
                ORDER BY created_at DESC LIMIT $2
                """,
                query,
                limit,
            )
        return [_row_to_dict(r) for r in rows]


# ─── Artifact ────────────────────────────────────────────────────────────────

async def list_artifacts(session_id: str = None, workspace_id: str = None) -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        if workspace_id:
            rows = await conn.fetch(
                "SELECT * FROM chat_artifacts WHERE workspace_id = $1 ORDER BY created_at DESC",
                uuid.UUID(workspace_id),
            )
        elif session_id:
            rows = await conn.fetch(
                "SELECT * FROM chat_artifacts WHERE session_id = $1 ORDER BY created_at DESC",
                uuid.UUID(session_id),
            )
        else:
            return []
        return [_row_to_dict(r) for r in rows]


async def get_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_artifacts WHERE id = $1",
            uuid.UUID(artifact_id),
        )
        return _row_to_dict(row) if row else None


async def update_artifact(artifact_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("title", "content"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        if "metadata" in data and data["metadata"] is not None:
            sets.append(f"metadata = ${idx}::jsonb")
            vals.append(json.dumps(data["metadata"]))
            idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_artifacts WHERE id = $1", uuid.UUID(artifact_id))
            return _row_to_dict(row) if row else None
        sets.append("updated_at = NOW()")
        vals.append(uuid.UUID(artifact_id))
        row = await conn.fetchrow(
            f"UPDATE chat_artifacts SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None


async def delete_artifact(artifact_id: str) -> bool:
    """아티팩트 삭제. 성공 시 True, 미존재 시 False."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM chat_artifacts WHERE id = $1", uuid.UUID(artifact_id)
        )
        return result == "DELETE 1"


async def export_artifact(artifact_id: str, fmt: str) -> Dict[str, Any]:
    """단순 텍스트 내보내기. PDF는 향후 확장."""
    artifact = await get_artifact(artifact_id)
    if not artifact:
        return {}
    content = artifact["content"]
    if fmt == "md":
        body = f"# {artifact.get('title', 'Artifact')}\n\n{content}"
        mime = "text/markdown"
    elif fmt == "html":
        import html as _html_mod
        _t = _html_mod.escape(artifact.get('title', 'Artifact'))
        _c = _html_mod.escape(content)
        body = f"<html><body><h1>{_t}</h1><pre>{_c}</pre></body></html>"
        mime = "text/html"
    else:
        # pdf: 텍스트로 반환 (실제 PDF 변환은 별도 라이브러리 필요)
        body = content
        mime = "application/pdf"
    return {"content": body, "mime": mime, "filename": f"artifact_{artifact_id}.{fmt}"}


# ─── Drive ───────────────────────────────────────────────────────────────────

UPLOAD_DIR = Path(os.getenv("CHAT_UPLOAD_DIR", "/root/aads/uploads/chat"))


async def list_drive_files(workspace_id: str) -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_drive_files WHERE workspace_id = $1 ORDER BY created_at DESC",
            uuid.UUID(workspace_id),
        )
        return [_row_to_dict(r) for r in rows]


async def save_drive_file(
    workspace_id: str,
    filename: str,
    file_bytes: bytes,
    file_type: Optional[str],
    uploaded_by: str = "user",
) -> Dict[str, Any]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4()}_{filename}"
    file_path = UPLOAD_DIR / safe_name
    file_path.write_bytes(file_bytes)

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_drive_files (workspace_id, filename, file_path, file_type, file_size, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            uuid.UUID(workspace_id),
            filename,
            str(file_path),
            file_type,
            len(file_bytes),
            uploaded_by,
        )
        return _row_to_dict(row)


async def delete_drive_file(file_id: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM chat_drive_files WHERE id = $1 RETURNING file_path",
            uuid.UUID(file_id),
        )
        if not row:
            return False
        path = Path(row["file_path"])
        if path.exists():
            path.unlink(missing_ok=True)
        return True


async def get_drive_file(file_id: str) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_drive_files WHERE id = $1",
            uuid.UUID(file_id),
        )
        return _row_to_dict(row) if row else None


# ─── Research Archive ────────────────────────────────────────────────────────

async def get_research_cache(topic: str, days: int = 7) -> Optional[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM research_archive
            WHERE topic ILIKE $1
              AND created_at >= NOW() - ($2 || ' days')::INTERVAL
            ORDER BY created_at DESC LIMIT 1
            """,
            f"%{topic.replace(chr(92), chr(92)*2).replace('%', chr(92)+'%').replace('_', chr(92)+'_')}%",
            str(days),
        )
        return _row_to_dict(row) if row else None


async def list_research_history(limit: int = 50) -> List[Dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM research_archive ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [_row_to_dict(r) for r in rows]


# ─── 내부 헬퍼 ────────────────────────────────────────────────────────────────

# #8: JSONB 필드 목록 (파싱 필요한 컬럼만)
_JSONB_FIELDS = frozenset({"attachments", "sources", "tools_called", "settings", "files", "metadata"})


def _row_to_dict(row: asyncpg.Record) -> Dict[str, Any]:
    """asyncpg Record → Python dict. #8: JSONB 필드만 선택적 파싱."""
    if row is None:
        return {}
    result = {}
    for key in row.keys():
        val = row[key]
        # JSONB 필드만 파싱 시도 (전체 필드 순회 대신)
        if key in _JSONB_FIELDS and isinstance(val, str) and len(val) >= 2 and val[0] in ("{", "["):
            try:
                val = json.loads(val)
            except Exception:
                pass
        result[key] = val
    return result


# ─── 메모리 컨텍스트 뷰어 API (AADS 메모리 & 맥락 뷰어) ─────────────────────

async def get_memory_context_info(session_id: str) -> Dict[str, Any]:
    """세션의 주입 메모리 + 맥락 상태 + 이전 세션 요약 조회."""
    pool = get_pool()
    async with pool.acquire() as conn:
      try:
        # 1) 세션 + 워크스페이스 기본 정보
        session_row = await conn.fetchrow(
            """
            SELECT s.id, s.title, s.message_count, s.cost_total, s.summary,
                   s.workspace_id, s.created_at, s.updated_at,
                   w.name AS workspace_name, w.system_prompt
            FROM chat_sessions s
            LEFT JOIN chat_workspaces w ON s.workspace_id = w.id
            WHERE s.id = $1
            """,
            uuid.UUID(session_id),
        )
        if not session_row:
            return {}

        workspace_name = session_row["workspace_name"] or ""
        workspace_id = session_row["workspace_id"]
        system_prompt = session_row["system_prompt"] or ""
        system_prompt_tokens = max(1, len(system_prompt)) * 2 // 3  # 대략 추정

        # 2) 토큰 합계 조회
        token_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(tokens_in), 0) AS total_in,
                   COALESCE(SUM(tokens_out), 0) AS total_out,
                   COALESCE(SUM(cost), 0) AS total_cost,
                   COUNT(*) AS msg_count
            FROM chat_messages
            WHERE session_id = $1
            """,
            uuid.UUID(session_id),
        )

        message_count = int(token_row["msg_count"]) if token_row else 0
        tokens_in = int(token_row["total_in"]) if token_row else 0
        tokens_out = int(token_row["total_out"]) if token_row else 0
        total_cost = float(token_row["total_cost"]) if token_row else 0.0

        # 3) 장기 메모리 (ai_meta_memory)
        meta_rows = await conn.fetch(
            """
            SELECT category, key, value, confidence
            FROM ai_meta_memory
            WHERE category IN ('ceo_preference', 'project_pattern', 'known_issue', 'decision_history')
            ORDER BY confidence DESC, updated_at DESC
            """,
        )
        long_term_items = []
        ltm_text_len = 0
        for r in meta_rows:
            val = r["value"]
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    pass
            summary = ""
            if isinstance(val, dict):
                summary = val.get("summary") or val.get("description") or json.dumps(val, ensure_ascii=False)[:120]
            else:
                summary = str(val)[:120]
            long_term_items.append({
                "key": r["key"],
                "category": r["category"],
                "summary": summary,
            })
            ltm_text_len += len(summary)
        ltm_tokens = max(1, ltm_text_len) * 2 // 3

        # 4) AI observations (세션 주입되는 것들) — 워크스페이스 프로젝트로 필터
        # 워크스페이스명에서 프로젝트 추출: "[KIS] 자동매매" → "KIS"
        _ws_project = ""
        if workspace_name and workspace_name.startswith("["):
            _ws_project = workspace_name.split("]")[0].lstrip("[").strip()
        
        if _ws_project and _ws_project != "CEO":
            obs_rows = await conn.fetch(
                """
                SELECT category, key, value, confidence
                FROM ai_observations
                WHERE confidence >= 0.2
                  AND (project = $1 OR project IS NULL)
                ORDER BY confidence DESC, updated_at DESC
                """,
                _ws_project,
            )
        else:
            obs_rows = await conn.fetch(
                """
                SELECT category, key, value, confidence
                FROM ai_observations
                WHERE confidence >= 0.2
                ORDER BY confidence DESC, updated_at DESC
                """,
            )
        obs_items = []
        obs_text_len = 0
        for r in obs_rows:
            obs_items.append({
                "key": r["key"],
                "category": r["category"],
                "summary": str(r["value"])[:120],
            })
            obs_text_len += min(len(str(r["value"])), 120)
        obs_tokens = max(1, obs_text_len) * 2 // 3

        # 5) 세션 노트 (session_notes) — 같은 워크스페이스의 세션만
        note_rows = await conn.fetch(
            """
            SELECT sn.summary, sn.key_decisions, sn.created_at, sn.projects_discussed
            FROM session_notes sn
            JOIN chat_sessions cs ON sn.session_id = cs.id
            WHERE cs.workspace_id = $1
            ORDER BY sn.created_at DESC
            """,
            workspace_id,
        )
        session_summaries = []
        ss_text_len = 0
        for r in note_rows:
            ts = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else ""
            summ = r["summary"] or ""
            session_summaries.append({
                "date": ts,
                "summary": summ[:200],
            })
            ss_text_len += min(len(summ), 200)
        ss_tokens = max(1, ss_text_len) * 2 // 3

        # 5b) experience_memory — 워크스페이스 프로젝트로 필터
        if _ws_project and _ws_project != "CEO":
            exp_rows = await conn.fetch(
                """
                SELECT experience_type, domain, tags, content, rif_score, created_at
                FROM experience_memory
                WHERE domain = $1 OR domain IS NULL OR domain = ''
                ORDER BY rif_score DESC, created_at DESC
                """,
                _ws_project,
            )
        else:
            exp_rows = await conn.fetch(
                """
                SELECT experience_type, domain, tags, content, rif_score, created_at
                FROM experience_memory
                ORDER BY rif_score DESC, created_at DESC
                """,
            )
        exp_items = []
        exp_text_len = 0
        for r in exp_rows:
            val = r["content"]
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    pass
            summary = ""
            if isinstance(val, dict):
                summary = val.get("summary") or val.get("description") or json.dumps(val, ensure_ascii=False)[:120]
            else:
                summary = str(val)[:120]
            exp_items.append({
                "experience_type": r["experience_type"],
                "domain": r["domain"] or "",
                "tags": r["tags"] or [],
                "summary": summary,
                "rif_score": float(r["rif_score"]) if r["rif_score"] else 1.0,
            })
            exp_text_len += len(summary)
        exp_tokens = max(1, exp_text_len) * 2 // 3

        total_memory_count = len(long_term_items) + len(obs_items) + len(exp_items)
        total_injected_tokens = system_prompt_tokens + ltm_tokens + obs_tokens + ss_tokens + exp_tokens

        # 6) Compaction 상태
        compaction_threshold = 30
        compaction_needed = message_count >= compaction_threshold
        has_summary = bool(session_row["summary"])

        # 건강도 판정
        if compaction_needed and not has_summary:
            health = "danger"
        elif compaction_needed:
            health = "warning"
        else:
            health = "normal"

        # 7) 같은 워크스페이스의 다른 세션 이력
        history_rows = await conn.fetch(
            """
            SELECT s.id, s.title, s.summary, s.message_count, s.created_at
            FROM chat_sessions s
            WHERE s.workspace_id = $1 AND s.id != $2
            ORDER BY s.updated_at DESC
            LIMIT 10
            """,
            workspace_id,
            uuid.UUID(session_id),
        )
        session_history = []
        for r in history_rows:
            session_history.append({
                "date": r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else "",
                "title": r["title"] or "",
                "summary": (r["summary"] or "")[:200],
                "message_count": r["message_count"] or 0,
            })

        return {
            "session_id": session_id,
            "workspace_name": workspace_name,
            "injected_memory": {
                "system_prompt_tokens": system_prompt_tokens,
                "long_term_memory": {
                    "count": len(long_term_items),
                    "tokens": ltm_tokens,
                    "items": long_term_items,
                },
                "observations": {
                    "count": len(obs_items),
                    "tokens": obs_tokens,
                    "items": obs_items,
                },
                "session_summaries": {
                    "count": len(session_summaries),
                    "tokens": ss_tokens,
                    "items": session_summaries,
                },
                "experience_memory": {
                    "count": len(exp_items),
                    "tokens": exp_tokens,
                    "items": exp_items,
                },
                "total_memory_count": total_memory_count,
                "total_injected_tokens": total_injected_tokens,
            },
            "context_status": {
                "session_title": session_row["title"] or "",
                "message_count": message_count,
                "total_cost": round(total_cost, 4),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "compaction_needed": compaction_needed,
                "compaction_threshold": compaction_threshold,
                "has_summary": has_summary,
                "health": health,
            },
            "session_history": session_history,
        }
      except Exception as e:
        logger.error(f"get_memory_context_info failed: {e}")
        return {"error": str(e)}


# ─── Chat Files (파일 첨부 시스템 Phase 1) ──────────────────────────────────

CHAT_FILES_DIR = Path(os.getenv("CHAT_FILES_DIR", "/root/aads/uploads/chat/files"))


async def save_chat_file(
    session_id: str,
    file: Any,
    data: bytes,
    uploaded_by: str = "user",
) -> Dict[str, Any]:
    """파일 저장 + 이미지 압축 + DB 등록."""
    file_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    mime = file.content_type or "application/octet-stream"

    # 디렉토리 생성
    session_dir = CHAT_FILES_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{file_id}{ext}"
    storage_path = session_dir / stored_name
    thumbnail_path = None
    width, height = None, None

    is_image = mime.startswith("image/")

    if is_image:
        try:
            from PIL import Image
            from io import BytesIO

            img = Image.open(BytesIO(data))
            width, height = img.size

            # AI 생성 이미지는 압축 안 함
            if uploaded_by != "ai":
                # 1024px 리사이즈 + WebP 변환
                if max(img.size) > 1024:
                    img.thumbnail((1024, 1024), Image.LANCZOS)
                stored_name = f"{file_id}.webp"
                storage_path = session_dir / stored_name
                img.save(storage_path, "WEBP", quality=85)

                # 썸네일 200px
                thumb = img.copy()
                thumb.thumbnail((200, 200), Image.LANCZOS)
                thumb_name = f"{file_id}_thumb.webp"
                thumb_path = session_dir / thumb_name
                thumb.save(thumb_path, "WEBP", quality=75)
                thumbnail_path = str(thumb_path)
            else:
                # AI 생성: 원본 저장
                storage_path.write_bytes(data)
        except Exception:
            # 이미지 처리 실패 시 원본 저장
            storage_path = session_dir / f"{file_id}{ext}"
            storage_path.write_bytes(data)
            is_image = False
    else:
        # 비이미지: 원본 저장
        storage_path.write_bytes(data)

    # DB 등록
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chat_files (id, session_id, original_name, stored_name, mime_type,
                                     file_size, uploaded_by, storage_path, thumbnail_path, width, height)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            uuid.UUID(file_id), uuid.UUID(session_id), file.filename or "unknown",
            stored_name, mime, len(data), uploaded_by, str(storage_path),
            thumbnail_path, width, height,
        )

    return {
        "file_id": file_id,
        "original_name": file.filename,
        "mime_type": mime,
        "file_size": len(data),
        "width": width,
        "height": height,
        "thumbnail_url": f"/api/v1/chat/files/{file_id}/thumbnail" if thumbnail_path else None,
        "file_url": f"/api/v1/chat/files/{file_id}",
    }


async def get_chat_file(file_id: str) -> Optional[Dict[str, Any]]:
    """파일 메타 조회."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_files WHERE id = $1", uuid.UUID(file_id)
        )
    if not row:
        return None
    return dict(row)


# ════════════════════════════════════════════════════════════════════════════════
# Prompt Templates (P2-10)
# ════════════════════════════════════════════════════════════════════════════════

async def list_templates(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """템플릿 목록 (usage_count DESC 정렬)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if category:
            rows = await conn.fetch(
                "SELECT * FROM prompt_templates WHERE category = $1 ORDER BY usage_count DESC, updated_at DESC",
                category,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM prompt_templates ORDER BY usage_count DESC, updated_at DESC"
            )
    return [dict(r) for r in rows]


async def create_template(data: Dict[str, Any]) -> Dict[str, Any]:
    """새 템플릿 생성."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO prompt_templates (title, content, category)
               VALUES ($1, $2, $3) RETURNING *""",
            data["title"], data["content"], data.get("category", "일반"),
        )
    return dict(row)


async def delete_template(template_id: str) -> bool:
    """템플릿 삭제."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM prompt_templates WHERE id = $1", uuid.UUID(template_id)
        )
    return result == "DELETE 1"


async def use_template(template_id: str) -> Optional[Dict[str, Any]]:
    """템플릿 사용 → usage_count 증가, 템플릿 반환."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE prompt_templates
               SET usage_count = usage_count + 1, updated_at = NOW()
               WHERE id = $1 RETURNING *""",
            uuid.UUID(template_id),
        )
    if not row:
        return None
    return dict(row)
