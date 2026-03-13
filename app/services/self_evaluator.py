"""
F11: Self-Evaluation — AI 응답 후 Haiku로 정확도/완성도/관련성 평가.
chat_messages.quality_score + quality_details 저장.
비용: ~$0.0003/턴 (200자 이상 응답만).
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("SELF_EVAL_ENABLED", "true").lower() == "true"
_MIN_RESPONSE_LEN = int(os.getenv("SELF_EVAL_MIN_LEN", "200"))
_HAIKU_MODEL = os.getenv("SELF_EVAL_MODEL", "claude-haiku-4-5-20251001")

_EVAL_PROMPT = """다음 AI 응답의 품질을 평가하세요.

사용자 질문: {user_msg}
AI 응답 (앞 1000자): {ai_msg}

3가지 기준으로 0.0~1.0 점수와 간단한 이유를 JSON으로 반환:
{{"accuracy": 0.0~1.0, "completeness": 0.0~1.0, "relevance": 0.0~1.0, "overall": 0.0~1.0, "note": "한줄 평가"}}

JSON만 반환. 마크다운 코드블록 없이."""


async def evaluate_response(
    user_message: str,
    ai_response: str,
    message_id: str,
) -> Optional[float]:
    """AI 응답 품질을 평가하고 DB에 저장."""
    if not _ENABLED or len(ai_response) < _MIN_RESPONSE_LEN:
        return None

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()

        prompt = _EVAL_PROMPT.format(
            user_msg=user_message[:300],
            ai_msg=ai_response[:1000],
        )

        response = await client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        details = json.loads(text)
        overall = float(details.get("overall", 0.5))

        # DB 저장
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE chat_messages
                SET quality_score = $1, quality_details = $2::jsonb
                WHERE id = $3
                """,
                overall,
                json.dumps(details),
                uuid.UUID(message_id),
            )

        logger.info("self_eval_complete", score=overall, message=message_id[:8])
        return overall

    except Exception as e:
        logger.debug("self_eval_error", error=str(e))
        return None
