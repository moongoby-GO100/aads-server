"""
AADS-191 Phase4: 워커 분리 — Redis Stream 기반 SSE 전송 분리.

아키텍처:
  send_message_stream() → Producer Task → Redis Stream → SSE Delivery → Client

Primary 경로 (첫 연결):
  with_background_completion → Queue + Redis Stream 병행 → yield (저지연)

Reconnect 경로 (재연결):
  deliver_sse → Redis XREAD blocking → yield (Last-Event-ID 기반 이어읽기)

모든 SSE data 이벤트는 Redis Stream에 저장되므로,
재연결 시 놓친 토큰을 Redis Stream에서 복구하여 끊김 없이 전달.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from app.services import redis_stream as _rs

logger = logging.getLogger(__name__)

# Cloudflare flush 유도 패딩 (256byte+)
_PAD = ":" + " " * 256 + "\n"
_HB_LINE = f'data: {json.dumps({"type": "heartbeat"})}\n{_PAD}\n'


async def deliver_sse(
    session_id: str,
    last_event_id: str = "0",
) -> AsyncGenerator[str, None]:
    """Redis Stream에서 XREAD blocking으로 토큰을 읽어 SSE yield.

    SSE 재연결 시 Last-Event-ID로 끊긴 지점부터 이어서 전송.
    워커(producer)와 완전 분리 — 워커가 다른 프로세스여도 동작.

    Args:
        session_id: 채팅 세션 ID
        last_event_id: 마지막 수신한 Redis Stream entry ID ("0"이면 처음부터)
    """
    try:
        r = await _rs._get_redis()
    except Exception as e:
        logger.warning(f"deliver_sse_redis_fail session={session_id[:8]}: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': 'Redis 연결 실패'})}\n\n"
        return

    key = _rs._stream_key(session_id)
    current_id = last_event_id

    # SSE 재연결 간격 2초
    yield "retry: 2000\n\n"
    # 초기 heartbeat — 연결 수립 확인
    yield _HB_LINE

    _idle_rounds = 0
    _MAX_IDLE = 150  # 150 × 2초 = 5분 timeout

    while True:
        try:
            # XREAD: 새 이벤트 대기 (최대 2초 블로킹)
            entries = await r.xread({key: current_id}, count=20, block=2000)

            if not entries:
                _idle_rounds += 1

                # 5분 timeout
                if _idle_rounds >= _MAX_IDLE:
                    yield f"data: {json.dumps({'type': 'error', 'message': '응답 시간 초과'})}\n\n"
                    return

                # Stream done 체크 (워커 완료 여부)
                info = await _rs.get_stream_info(session_id)
                if info and info.get("is_done"):
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                if not info and _idle_rounds > 5:
                    # Stream 자체가 없고 10초 경과 — 생성 시작 전이거나 만료됨
                    yield f"data: {json.dumps({'type': 'error', 'message': '스트림 없음'})}\n\n"
                    return

                # 연결 유지 heartbeat
                yield _HB_LINE
                continue

            _idle_rounds = 0

            for _stream_name, messages in entries:
                for msg_id, fields in messages:
                    current_id = msg_id

                    # 완료 마커
                    if fields.get("done") == "true":
                        yield f"id:{msg_id}\ndata: {json.dumps({'type': 'done'})}\n\n"
                        return

                    # 토큰 데이터 전송 (id: 포함 → 클라이언트 Last-Event-ID 갱신)
                    data = fields.get("data", "")
                    if data:
                        yield f"id:{msg_id}\n{data}"

        except asyncio.CancelledError:
            logger.info(f"deliver_sse_cancelled session={session_id[:8]}")
            return
        except Exception as e:
            logger.warning(f"deliver_sse_error session={session_id[:8]}: {e}")
            yield _HB_LINE
            await asyncio.sleep(1)
            _idle_rounds += 1
