"""
AADS-191 Phase4: 워커 분리 — Redis Stream 기반 SSE 전송 분리.

핵심 역할:
- deliver_sse(): Redis Stream에서 XREAD blocking으로 토큰을 읽어 SSE 이벤트로 전달
- stream-resume 엔드포인트에서 Last-Event-ID 기반 재연결 시 사용
- 서버 재시작 후에도 Redis Stream에 보존된 토큰을 클라이언트에 전달

아키텍처:
  LLM API → _producer (chat_service) → Redis Stream + Queue
  Redis Stream → deliver_sse (이 모듈) → SSE to client (재연결 시)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional

from app.services import redis_stream as _rs

logger = logging.getLogger(__name__)


async def deliver_sse(
    stream_id: str,
    last_event_id: str = "0",
    timeout_sec: float = 300.0,
) -> AsyncGenerator[str, None]:
    """Redis Stream에서 XREAD blocking으로 토큰을 읽어 SSE 이벤트로 전달.

    Args:
        stream_id: execution 또는 세션 ID
        last_event_id: 마지막으로 수신한 Redis Stream entry ID ("0"이면 처음부터)
        timeout_sec: 전체 타임아웃 (기본 300초 = 5분)

    Yields:
        SSE 포맷 문자열 (id:{entry_id}\ndata: {...}\n\n)
    """
    import time
    _start = time.monotonic()
    current_id = last_event_id if last_event_id and last_event_id != "0" else "0"
    _empty_count = 0  # 연속 빈 응답 횟수

    # 초기: 이미 저장된 토큰을 즉시 전달 (catch-up)
    try:
        cached = await _rs.read_tokens_after(stream_id, current_id)
        for entry in cached:
            if entry.get("done"):
                yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                return
            data = entry.get("data", "")
            eid = entry.get("id", "")
            if data:
                yield f"id:{eid}\n{data}" if not data.endswith("\n\n") else f"id:{eid}\n{data}"
                current_id = eid
                _empty_count = 0
    except Exception as e:
        logger.warning(f"deliver_sse_catchup_failed stream={stream_id[:8]}: {e}")

    # 실시간: XREAD blocking으로 새 토큰 대기
    while (time.monotonic() - _start) < timeout_sec:
        try:
            entries = await _rs.xread_blocking(stream_id, current_id, timeout_ms=1000)

            if not entries:
                _empty_count += 1
                # 5초 이상 빈 응답 → 스트림 완료 여부 확인
                if _empty_count >= 5:
                    info = await _rs.get_stream_info(stream_id)
                    if info is None:
                        # Stream 자체가 없음 → 종료
                        yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                        return
                    if info.get("is_done"):
                        # 완료 마커 있음 → 종료
                        yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                        return
                    _empty_count = 0  # 리셋 후 계속 대기

                # heartbeat 전송 (연결 유지)
                yield f'data: {json.dumps({"type": "heartbeat"})}\n\n'
                continue

            _empty_count = 0
            for eid, fields in entries:
                current_id = eid
                if fields.get("done") == "true":
                    yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                    return
                data = fields.get("data", "")
                if data:
                    yield f"id:{eid}\n{data}" if not data.endswith("\n\n") else f"id:{eid}\n{data}"

        except Exception as e:
            logger.warning(f"deliver_sse_xread_error stream={stream_id[:8]}: {e}")
            await asyncio.sleep(1)

    # 타임아웃
    yield f'data: {json.dumps({"type": "resume_done", "reason": "timeout"})}\n\n'
