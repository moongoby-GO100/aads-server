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
    session_id: Optional[str] = None,
    project: Optional[str] = None,
) -> Optional[float]:
    """AI 응답 품질을 평가하고 DB에 저장. B1: 낮은 품질 시 reflexion 생성."""
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
        overall = min(1.0, max(0.0, overall))

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

        # A1: Low quality → reduce confidence of recently extracted facts
        if overall < 0.5:
            try:
                async with pool.acquire() as conn2:
                    session_row = await conn2.fetchrow(
                        "SELECT session_id FROM chat_messages WHERE id = $1",
                        uuid.UUID(message_id),
                    )
                    if session_row and session_row["session_id"]:
                        await conn2.execute(
                            """UPDATE memory_facts SET confidence = confidence * $1
                               WHERE session_id = $2::uuid
                                 AND created_at > NOW() - interval '60 seconds'
                                 AND confidence > 0.1""",
                            max(0.3, overall),
                            session_row["session_id"],
                        )
                        logger.info(
                            "a1_fact_confidence_reduced",
                            score=overall,
                            session=str(session_row["session_id"])[:8],
                        )
            except Exception as e_a1:
                logger.debug("a1_fact_confidence_error", error=str(e_a1))

        # B1: Reflexion — auto-reflect on poor quality responses
        if overall < 0.4 and session_id:
            try:
                reflection_prompt = (
                    f"이 AI 응답의 품질이 낮습니다 (점수: {overall:.1f}).\n"
                    f"무엇이 잘못되었고, 다음에 어떻게 개선해야 하는지 1-2문장으로 반성하세요.\n\n"
                    f"사용자: {user_message[:300]}\n"
                    f"AI: {ai_response[:500]}\n"
                    f"품질 세부: {json.dumps(details, ensure_ascii=False)}\n\n"
                    f"반성문만 작성하세요."
                )
                from anthropic import AsyncAnthropic as _ReflexionClient
                _refl_client = _ReflexionClient()
                _refl_resp = await _refl_client.messages.create(
                    model=_HAIKU_MODEL,
                    max_tokens=256,
                    messages=[{"role": "user", "content": reflection_prompt}],
                )
                reflection = _refl_resp.content[0].text.strip() if _refl_resp.content else ""
                if reflection:
                    async with pool.acquire() as conn_refl:
                        await conn_refl.execute(
                            """INSERT INTO memory_facts (session_id, project, category, subject, detail, confidence, tags)
                               VALUES ($1::uuid, $2, 'error_pattern', $3, $4, 0.85, ARRAY['reflexion', 'self-improvement'])""",
                            uuid.UUID(session_id),
                            (project or "").upper()[:20] or None,
                            f"품질 부족: {details.get('note', '')[:100]}",
                            reflection[:500],
                        )
                    logger.info("b1_reflexion_saved", score=overall, session=session_id[:8])
            except Exception as e_refl:
                logger.debug("b1_reflexion_error", error=str(e_refl))

        logger.info("self_eval_complete", score=overall, message=message_id[:8])
        return overall

    except Exception as e:
        logger.debug("self_eval_error", error=str(e))
        return None
