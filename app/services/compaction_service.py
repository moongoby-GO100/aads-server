"""
AADS-185: 자동 압축 서비스 — 20턴 초과 시 Claude Haiku로 요약
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic
from app.config import Settings

logger = logging.getLogger(__name__)

settings = Settings()
_anthropic = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())

COMPACTION_TRIGGER_TURNS = int(os.getenv("COMPACTION_TRIGGER_TURNS", "20"))
_DB_URL_ENV = "DATABASE_URL"


def _db_url() -> str:
    url = os.getenv(_DB_URL_ENV, "")
    return url.replace("postgresql://", "postgres://") if url else url


async def check_and_compact(
    session_id: str,
    messages: List[Dict[str, Any]],
    db_conn=None,
) -> List[Dict[str, Any]]:
    """
    20턴 초과 시 자동 압축 실행.
    압축 결과를 session_notes에 저장, 이전 메시지를 is_compacted=true로 마킹.

    Returns:
        압축 후 메시지 리스트 (최근 5턴 + 압축 요약 메시지)
    """
    if len(messages) <= COMPACTION_TRIGGER_TURNS:
        return messages

    logger.info(f"compaction_service: session={session_id} turns={len(messages)} → 압축 시작")

    # 압축 대상: 최근 5턴 이전 메시지들
    to_compress = messages[:-5]
    recent = messages[-5:]

    summary = await _summarize(to_compress)

    # DB에 압축 요약 저장
    if db_conn:
        try:
            await db_conn.execute(
                """
                INSERT INTO session_notes (session_id, note_type, content)
                VALUES ($1, 'compaction', $2)
                """,
                uuid.UUID(session_id),
                summary,
            )
            # 이전 메시지 is_compacted 마킹
            await db_conn.execute(
                """
                UPDATE chat_messages
                SET is_compacted = true
                WHERE session_id = $1
                  AND id NOT IN (
                      SELECT id FROM chat_messages
                      WHERE session_id = $1
                      ORDER BY created_at DESC
                      LIMIT 10
                  )
                """,
                uuid.UUID(session_id),
            )
        except Exception as e:
            logger.warning(f"compaction_service db error: {e}")

    # 압축 메시지를 히스토리 앞에 삽입
    compaction_msg = {
        "role": "user",
        "content": f"[대화 압축 요약 — 이전 {len(to_compress)}개 메시지]\n\n{summary}",
    }
    result = [compaction_msg] + recent
    logger.info(f"compaction_service: {len(messages)} → {len(result)} 메시지로 압축 완료")
    return result


async def _summarize(messages: List[Dict[str, Any]]) -> str:
    """Claude Haiku로 대화 요약 (비용 최소화)."""
    if not messages:
        return "(압축할 내용 없음)"

    # 메시지를 텍스트로 변환
    conv_text = ""
    for msg in messages:
        role = "사용자" if msg["role"] == "user" else "AI"
        content = msg.get("content", "")
        # 도구 결과 블록 제거 (너무 길면 요약 품질 저하)
        if "[시스템 도구 조회 결과" in content:
            lines = content.split("\n")
            header_idx = next(
                (i for i, l in enumerate(lines) if "[시스템 도구 조회 결과" in l), -1
            )
            if header_idx >= 0:
                content = "\n".join(lines[:header_idx]) + " [도구 조회 포함]"
        conv_text += f"\n{role}: {content[:500]}\n"

    prompt = f"""다음 대화를 압축 요약하세요. 보존 항목:
1. 아키텍처 결정 및 기술적 합의
2. 미해결 이슈 및 다음 단계
3. 중요한 수치/데이터/경로
4. CEO 주요 지시 내용

삭제 항목:
- 중복 인사, 확인 대화
- 이전 도구 raw 출력
- 반복되는 상태 설명

대화:
{conv_text[:6000]}

요약 (핵심만, 500자 이내):"""

    try:
        msg = await _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning(f"compaction_service summarize error: {e}")
        # 폴백: 간단 트리밍
        return f"[압축 오류, 원본 {len(messages)}개 메시지 — 최근 내용 우선 참고]"
