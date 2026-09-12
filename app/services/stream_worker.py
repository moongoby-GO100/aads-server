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
from app.services.chat_protocol import (
    CHAT_CONTRACT_V1,
    CHAT_CONTRACT_V2,
    ChatProtocolError,
    compare_redis_event_ids,
    encode_snapshot_required_event,
    encode_v2_sse_event,
)

logger = logging.getLogger(__name__)


async def deliver_sse(
    stream_id: str,
    last_event_id: str = "0",
    timeout_sec: float = 300.0,
    *,
    contract_version: int = CHAT_CONTRACT_V1,
    session_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    owner_epoch: Optional[int | str] = None,
) -> AsyncGenerator[str, None]:
    """Redis Stream에서 XREAD blocking으로 토큰을 읽어 SSE 이벤트로 전달.

    Args:
        stream_id: execution 또는 세션 ID
        last_event_id: 마지막으로 수신한 Redis Stream entry ID ("0"이면 처음부터)
        timeout_sec: 전체 타임아웃 (기본 300초 = 5분)
        contract_version: 1은 기존 원문 이벤트, 2는 versioned envelope
        session_id/execution_id/owner_epoch: v2 envelope scope metadata.  An
            epoch may only be supplied when it was captured with the event;
            callers must not relabel replay with a later DB owner epoch.

    Yields:
        SSE 포맷 문자열 (id:{entry_id}\ndata: {...}\n\n)
    """
    import time
    _start = time.monotonic()
    current_id = last_event_id if last_event_id and last_event_id != "0" else "0"
    _empty_count = 0  # 연속 빈 응답 횟수

    async def _v2_event(
        data: str,
        *,
        event_id: Optional[str] = None,
        sequence=None,
        occurred_at=None,
        event_owner_epoch=None,
    ) -> Optional[str]:
        try:
            return encode_v2_sse_event(
                data,
                event_id=event_id,
                session_id=session_id,
                execution_id=execution_id or stream_id,
                owner_epoch=(
                    event_owner_epoch if event_owner_epoch is not None else owner_epoch
                ),
                sequence=sequence,
                occurred_at=occurred_at,
            )
        except ChatProtocolError:
            return None

    # V2 alone performs retention-boundary checks.  V1 remains byte-compatible,
    # while v2 fails closed to a snapshot instead of silently skipping trimmed
    # events or accepting a cursor ahead of the stream.
    if contract_version == CHAT_CONTRACT_V2:
        try:
            initial_info = await _rs.get_stream_info(stream_id)
        except Exception:
            initial_info = None
        if initial_info:
            first_id = initial_info.get("first_event_id")
            high_watermark = initial_info.get("last_event_id")
            retention_trimmed = initial_info.get("retention_trimmed")
            max_deleted_id = initial_info.get("max_deleted_event_id")
            mismatch_reason = None
            if high_watermark and not first_id:
                mismatch_reason = "retention_boundary_unavailable"
            elif current_id == "0" and first_id and retention_trimmed is not False:
                # A zero cursor is safe only when Redis can prove that no
                # earlier entries were trimmed.  Unknown retention metadata
                # fails closed to the DB snapshot contract.
                mismatch_reason = "snapshot_required_before_replay"
            elif current_id != "0" and first_id:
                try:
                    # A snapshot covering exactly through Redis's greatest
                    # deleted ID can safely replay the first retained entry.
                    # Without that proof, a cursor below the first retained
                    # entry may have skipped trimmed data or belong elsewhere.
                    retention_floor = (
                        str(max_deleted_id)
                        if retention_trimmed is True and max_deleted_id
                        else str(first_id)
                    )
                    if compare_redis_event_ids(current_id, retention_floor) < 0:
                        mismatch_reason = "cursor_before_retention"
                except ChatProtocolError:
                    mismatch_reason = "invalid_applied_cursor"
            if mismatch_reason is None and current_id != "0" and high_watermark:
                try:
                    if compare_redis_event_ids(current_id, str(high_watermark)) > 0:
                        mismatch_reason = "cursor_ahead_of_high_watermark"
                except ChatProtocolError:
                    mismatch_reason = "invalid_applied_cursor"
            if mismatch_reason:
                yield encode_snapshot_required_event(
                    reason=mismatch_reason,
                    session_id=session_id,
                    execution_id=execution_id or stream_id,
                    server_high_watermark=str(high_watermark) if high_watermark else None,
                    failed_event_id=current_id,
                )
                return

    # 초기: 이미 저장된 토큰을 즉시 전달 (catch-up)
    try:
        cached = await _rs.read_tokens_after(stream_id, current_id)
        for entry in cached:
            eid = entry.get("id", "")
            if entry.get("done"):
                if contract_version == CHAT_CONTRACT_V2:
                    encoded = await _v2_event(
                        f'data: {json.dumps({"type": "resume_done"})}\n\n',
                        event_id=eid or None,
                        sequence=entry.get("idx"),
                        occurred_at=entry.get("ts"),
                        event_owner_epoch=entry.get("owner_epoch"),
                    )
                    if encoded is None:
                        yield encode_snapshot_required_event(
                            reason="invalid_replay_done_event",
                            session_id=session_id,
                            execution_id=execution_id or stream_id,
                            failed_event_id=eid or None,
                        )
                    else:
                        yield encoded
                else:
                    yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                return
            data = entry.get("data", "")
            if data:
                if contract_version == CHAT_CONTRACT_V2:
                    encoded = await _v2_event(
                        data,
                        event_id=eid or None,
                        sequence=entry.get("idx"),
                        occurred_at=entry.get("ts"),
                        event_owner_epoch=entry.get("owner_epoch"),
                    )
                    if encoded is None:
                        yield encode_snapshot_required_event(
                            reason="invalid_event_frame",
                            session_id=session_id,
                            execution_id=execution_id or stream_id,
                            failed_event_id=eid or None,
                        )
                        return
                    yield encoded
                else:
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
                        # Redis stream이 없다는 것은 완료가 아니라 복구 불가 상태다.
                        # 완료로 보내면 클라이언트가 부분 응답을 최종 응답처럼 확정할 수 있다.
                        if contract_version == CHAT_CONTRACT_V2:
                            yield encode_snapshot_required_event(
                                reason="stream_missing",
                                session_id=session_id,
                                execution_id=execution_id or stream_id,
                            )
                        else:
                            yield f'data: {json.dumps({"type": "resume_unavailable", "reason": "stream_missing"})}\n\n'
                        return
                    if info.get("is_done"):
                        # 완료 마커 있음 → 종료
                        if contract_version == CHAT_CONTRACT_V2:
                            encoded = await _v2_event(
                                f'data: {json.dumps({"type": "resume_done"})}\n\n',
                                event_id=info.get("last_event_id"),
                                event_owner_epoch=info.get("last_event_owner_epoch"),
                            )
                            if encoded:
                                yield encoded
                            else:
                                yield encode_snapshot_required_event(
                                    reason="invalid_replay_done_event",
                                    session_id=session_id,
                                    execution_id=execution_id or stream_id,
                                    failed_event_id=info.get("last_event_id"),
                                )
                        else:
                            yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                        return
                    _empty_count = 0  # 리셋 후 계속 대기

                # heartbeat 전송 (연결 유지)
                if contract_version == CHAT_CONTRACT_V2:
                    encoded = await _v2_event(
                        f'data: {json.dumps({"type": "heartbeat"})}\n\n'
                    )
                    if encoded:
                        yield encoded
                else:
                    yield f'data: {json.dumps({"type": "heartbeat"})}\n\n'
                continue

            _empty_count = 0
            for eid, fields in entries:
                current_id = eid
                if fields.get("done") == "true":
                    if contract_version == CHAT_CONTRACT_V2:
                        encoded = await _v2_event(
                            f'data: {json.dumps({"type": "resume_done"})}\n\n',
                            event_id=eid,
                            sequence=fields.get("idx"),
                            occurred_at=fields.get("ts"),
                            event_owner_epoch=fields.get("owner_epoch"),
                        )
                        if encoded:
                            yield encoded
                        else:
                            yield encode_snapshot_required_event(
                                reason="invalid_replay_done_event",
                                session_id=session_id,
                                execution_id=execution_id or stream_id,
                                failed_event_id=eid,
                            )
                    else:
                        yield f'data: {json.dumps({"type": "resume_done"})}\n\n'
                    return
                data = fields.get("data", "")
                if data:
                    if contract_version == CHAT_CONTRACT_V2:
                        encoded = await _v2_event(
                            data,
                            event_id=eid,
                            sequence=fields.get("idx"),
                            occurred_at=fields.get("ts"),
                            event_owner_epoch=fields.get("owner_epoch"),
                        )
                        if encoded is None:
                            yield encode_snapshot_required_event(
                                reason="invalid_event_frame",
                                session_id=session_id,
                                execution_id=execution_id or stream_id,
                                failed_event_id=eid,
                            )
                            return
                        yield encoded
                    else:
                        yield f"id:{eid}\n{data}" if not data.endswith("\n\n") else f"id:{eid}\n{data}"

        except Exception as e:
            logger.warning(f"deliver_sse_xread_error stream={stream_id[:8]}: {e}")
            await asyncio.sleep(1)

    # 타임아웃
    if contract_version == CHAT_CONTRACT_V2:
        encoded = await _v2_event(
            f'data: {json.dumps({"type": "resume_timeout", "reason": "timeout"})}\n\n'
        )
        if encoded:
            yield encoded
    else:
        yield f'data: {json.dumps({"type": "resume_timeout", "reason": "timeout"})}\n\n'
