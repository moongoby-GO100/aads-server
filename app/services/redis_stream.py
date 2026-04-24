"""
AADS-191: Redis Stream 기반 토큰 버퍼링 — 서버 재시작 시 스트리밍 복구용.

핵심 아이디어:
- Producer(LLM 호출)가 토큰 생성 시 asyncio.Queue + Redis Stream 병행 저장
- 서버 재시작 → Redis Stream에 토큰 보존 → stream-resume에서 복구
- 기존 asyncio.Queue 경로 100% 유지 — Redis 실패해도 기존 동작에 영향 없음
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Redis 연결 (lazy init, 싱글턴)
_redis_client: Optional[aioredis.Redis] = None
_REDIS_HOST = "aads-redis"
_REDIS_PORT = 6379

# Stream 키 prefix + TTL
_STREAM_PREFIX = "chat:stream:"
_STREAM_TTL = 3600  # 1시간 후 자동 만료


async def _get_redis() -> aioredis.Redis:
    """Lazy Redis 연결 (async, 싱글턴)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(
            host=_REDIS_HOST,
            port=_REDIS_PORT,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            retry_on_timeout=True,
        )
    return _redis_client


def _stream_key(stream_id: str) -> str:
    """execution/session 식별자별 Redis Stream 키."""
    return f"{_STREAM_PREFIX}{stream_id}"


async def publish_token(stream_id: str, event_data: str, token_index: int) -> Optional[str]:
    """토큰을 Redis Stream에 추가. 실패 시 None 반환 (기존 동작 영향 없음).

    Args:
        stream_id: execution 또는 세션 ID
        event_data: SSE 이벤트 문자열 (data: {...}\n\n)
        token_index: 토큰 순서 번호

    Returns:
        Redis Stream entry ID (성공 시) 또는 None (실패 시)
    """
    try:
        r = await _get_redis()
        key = _stream_key(stream_id)
        entry_id = await r.xadd(
            key,
            {"data": event_data, "idx": str(token_index), "ts": str(time.time())},
            maxlen=5000,  # 세션당 최대 5000 이벤트 (메모리 보호)
        )
        # TTL 설정 (첫 토큰 시에만 — 이후 XADD는 TTL 갱신 불필요)
        if token_index == 0:
            await r.expire(key, _STREAM_TTL)
        return entry_id
    except Exception as e:
        # Redis 장애 시 경고만 — 기존 Queue 경로에 영향 없음
        if token_index == 0:  # 첫 토큰일 때만 로그 (스팸 방지)
            logger.warning(f"redis_stream_publish_failed stream={stream_id[:8]}: {e}")
        return None


async def mark_stream_done(stream_id: str) -> None:
    """스트리밍 완료 마커를 Redis Stream에 추가."""
    try:
        r = await _get_redis()
        key = _stream_key(stream_id)
        await r.xadd(key, {"data": "", "done": "true", "ts": str(time.time())})
        # 완료 후 TTL 단축 (30분 — 서버 재시작 + resume 시간 충분)
        await r.expire(key, 1800)
    except Exception as e:
        logger.warning(f"redis_stream_mark_done_failed stream={stream_id[:8]}: {e}")


async def read_tokens_after(stream_id: str, last_id: str = "0") -> List[Dict[str, Any]]:
    """Redis Stream에서 last_id 이후 토큰 읽기 (stream-resume용).

    Args:
        stream_id: execution 또는 세션 ID
        last_id: 마지막으로 수신한 entry ID ("0"이면 처음부터)

    Returns:
        [{"id": entry_id, "data": sse_event, "done": bool}, ...]
    """
    try:
        r = await _get_redis()
        key = _stream_key(stream_id)
        entries = await r.xrange(key, min=f"({last_id}" if last_id != "0" else "-", max="+")
        result = []
        for entry_id, fields in entries:
            result.append({
                "id": entry_id,
                "data": fields.get("data", ""),
                "done": fields.get("done") == "true",
                "idx": int(fields.get("idx", 0)),
            })
        return result
    except Exception as e:
        logger.warning(f"redis_stream_read_failed stream={stream_id[:8]}: {e}")
        return []


async def get_stream_info(stream_id: str) -> Optional[Dict[str, Any]]:
    """Redis Stream 상태 조회 (디버그/상태 체크용)."""
    try:
        r = await _get_redis()
        key = _stream_key(stream_id)
        exists = await r.exists(key)
        if not exists:
            return None
        length = await r.xlen(key)
        # 마지막 엔트리 확인
        last_entries = await r.xrevrange(key, count=1)
        is_done = False
        if last_entries:
            _, fields = last_entries[0]
            is_done = fields.get("done") == "true"
        return {
            "exists": True,
            "length": length,
            "is_done": is_done,
            "stream_key": key,
        }
    except Exception as e:
        logger.warning(f"redis_stream_info_failed stream={stream_id[:8]}: {e}")
        return None


async def delete_stream(stream_id: str) -> None:
    """Redis Stream 삭제 (세션 종료/정리용)."""
    try:
        r = await _get_redis()
        await r.delete(_stream_key(stream_id))
    except Exception:
        pass


async def xread_blocking(
    stream_id: str,
    last_id: str = "0",
    timeout_ms: int = 1000,
) -> list:
    """Redis Stream에서 XREAD blocking으로 새 엔트리 읽기 (deliver_sse용).

    Args:
        stream_id: execution 또는 세션 ID
        last_id: 마지막으로 읽은 entry ID
        timeout_ms: 블로킹 타임아웃 (밀리초)

    Returns:
        [(entry_id, {field: value}), ...] 또는 빈 리스트
    """
    try:
        r = await _get_redis()
        key = _stream_key(stream_id)
        # XREAD block: 새 엔트리가 올 때까지 최대 timeout_ms 대기
        result = await r.xread({key: last_id}, count=50, block=timeout_ms)
        if not result:
            return []
        # result: [(stream_name, [(entry_id, fields), ...])]
        return result[0][1] if result else []
    except Exception as e:
        logger.warning(f"redis_xread_blocking_failed stream={stream_id[:8]}: {e}")
        return []


async def reconstruct_from_stream(stream_id: str) -> tuple:
    """Redis Stream에서 전체 텍스트 복원 (서버 재시작 후 복구용).

    Returns:
        (full_text: str, is_complete: bool)
        - full_text: SSE delta 이벤트에서 추출한 전체 텍스트
        - is_complete: done 마커가 있으면 True (완성된 응답)
    """
    try:
        r = await _get_redis()
        key = _stream_key(stream_id)
        if not await r.exists(key):
            return "", False

        entries = await r.xrange(key, "-", "+")
        full_text = ""
        is_done = False

        for _entry_id, fields in entries:
            if fields.get("done") == "true":
                is_done = True
                continue
            data = fields.get("data", "")
            if not data:
                continue
            # SSE 이벤트 파싱: "data: {json}\n\n"
            for line in data.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        payload = json.loads(line[6:])
                        ptype = payload.get("type", "")
                        if ptype == "delta":
                            full_text += payload.get("content", "")
                        elif ptype == "tool_status":
                            pass  # 도구 상태는 텍스트가 아님
                    except (json.JSONDecodeError, KeyError):
                        pass

        return full_text, is_done
    except Exception as e:
        logger.warning(f"reconstruct_from_stream_failed stream={stream_id[:8]}: {e}")
        return "", False


async def health_check() -> bool:
    """Redis Stream 기능 헬스체크."""
    try:
        r = await _get_redis()
        return await r.ping()
    except Exception as e:
        logger.warning(f"redis_stream_health_check_failed: {e}")
        return False
