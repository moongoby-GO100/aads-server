"""
AI-to-AI 피드백 시스템 — Feature 2: Critic AI
CEO에게 응답 전송 전에 품질 검증.
미달 시 재생성 유도. Self-Evaluator(사후)와 달리 사전 검증.
비용: ~$0.0005/응답 (Haiku)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("CRITIC_AI_ENABLED", "true").lower() == "true"
_HAIKU_MODEL = os.getenv("CRITIC_MODEL", "qwen-turbo")
_SCORE_THRESHOLD = float(os.getenv("CRITIC_THRESHOLD", "0.55"))

# 스킵 대상 인텐트 (비용 절약)
_SKIP_INTENTS = frozenset({
    "casual", "greeting", "acknowledge", "memory_recall", "workspace_switch",
})

# 최소 응답 길이 (이 미만은 스킵)
_MIN_RESPONSE_LEN = 100


@dataclass
class CriticVerdict:
    """Critic AI 판정 결과."""
    verdict: str  # PASS / REGENERATE
    score: float
    details: dict
    feedback: str  # 재생성 시 피드백


_CRITIC_PROMPT = """다음 AI 응답의 품질을 CEO 전달 전에 사전 검증하세요.

## 검증 기준 (각 0.0~1.0)
1. factual_grounding: 도구 결과와 응답 내 수치/사실 일치 여부. 도구 미사용 질문이면 0.5.
2. completeness: 질문의 모든 부분에 답했는가
3. actionability: CEO가 바로 의사결정 가능한 정보 제공
4. ceo_alignment: CEO 선호(간결, 표 형식, 비용 명시) 부합
5. numeric_source_check: 응답에 구체적 수치(%, →, 배, 건, 점)가 포함된 경우, 각 수치의 출처(도구 결과, DB, 코드)가 명시되어 있는가. 출처 없는 수치가 있으면 0.0. 수치가 없으면 0.8.

overall = factual_grounding×0.30 + completeness×0.20 + actionability×0.15 + ceo_alignment×0.15 + numeric_source_check×0.20

## 핵심 위반 감지 (R-CRITICAL-003)
- "XX%→YY%", "AUC X→Y", "샤프비율 X→Y" 등 개선 추정치가 [출처] 표기 없이 존재하면 → factual_grounding=0.0, feedback에 "미검증 추정치 제거 필요" 명시.
- 표(table)에 수치가 있는데 [출처] 컬럼이 없으면 → numeric_source_check=0.0.

## 응답 형식 (JSON만):
{{"factual_grounding": 0.0~1.0, "completeness": 0.0~1.0, "actionability": 0.0~1.0, "ceo_alignment": 0.0~1.0, "numeric_source_check": 0.0~1.0, "overall": 0.0~1.0, "feedback": "재생성 필요 시 구체적 개선 지시", "note": "한줄 평가"}}

사용자 질문: {user_msg}
인텐트: {intent}
AI 응답 (앞 1500자): {ai_msg}
도구 사용 여부: {tool_used}

JSON만 반환하세요."""


async def critique_response(
    user_msg: str,
    ai_response: str,
    intent: str = "",
    tools_called: Optional[list] = None,
    session_id: Optional[str] = None,
) -> Optional[CriticVerdict]:
    """응답 사전 품질 검증. None 반환 = 스킵."""
    if not _ENABLED:
        return None

    # 스킵 조건
    if intent in _SKIP_INTENTS:
        return None
    if len(ai_response) < _MIN_RESPONSE_LEN:
        return None

    start = time.time()

    try:
        from app.core.anthropic_client import call_background_llm

        prompt = _CRITIC_PROMPT.format(
            user_msg=user_msg[:300],
            intent=intent or "unknown",
            ai_msg=ai_response[:1500],
            tool_used="예" if tools_called else "아니오",
        )

        result_text = await call_background_llm(
            prompt=prompt,
            max_tokens=256,
        )

        if not result_text:
            return None

        text = result_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        import re
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            details = json.loads(json_match.group())
        else:
            details = json.loads(text)

        # overall 계산 (R-CRITICAL-003: numeric_source_check 추가)
        fg = float(details.get("factual_grounding", 0.5))
        if not tools_called:
            fg = max(fg, 0.5)  # 도구 불필요 질문은 패널티 제거
        nsc = float(details.get("numeric_source_check", 0.8))
        overall = (
            fg * 0.30
            + float(details.get("completeness", 0.5)) * 0.20
            + float(details.get("actionability", 0.5)) * 0.15
            + float(details.get("ceo_alignment", 0.5)) * 0.15
            + nsc * 0.20
        )
        overall = min(1.0, max(0.0, overall))
        details["overall"] = round(overall, 3)

        verdict = "PASS" if overall >= _SCORE_THRESHOLD else "REGENERATE"
        feedback = details.get("feedback", "") if verdict == "REGENERATE" else ""

        # DB 저장 (비동기)
        try:
            from app.core.db_pool import get_pool
            import uuid
            pool = get_pool()
            async with pool.acquire() as conn:
                # session_id → sid UUID
                sid = None
                if session_id:
                    try:
                        sid = uuid.UUID(session_id)
                    except ValueError:
                        pass
                await conn.execute(
                    """INSERT INTO response_critiques
                       (session_id, verdict, score, details, model_used, cost)
                       VALUES ($1, $2, $3, $4::jsonb, $5, $6)""",
                    sid, verdict, overall,
                    json.dumps(details, ensure_ascii=False),
                    _HAIKU_MODEL,
                    0.0005,
                )
        except Exception as db_err:
            logger.debug("critic_db_save_error", error=str(db_err))

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "critic_complete",
            verdict=verdict, score=round(overall, 3),
            duration_ms=duration_ms,
        )

        return CriticVerdict(
            verdict=verdict,
            score=overall,
            details=details,
            feedback=feedback,
        )

    except Exception as e:
        logger.debug("critic_error", error=str(e))
        return None
