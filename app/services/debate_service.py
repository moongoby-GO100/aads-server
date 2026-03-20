"""
AI-to-AI 피드백 시스템 — Feature 3: Multi-Agent Debate
전략적 질문에 2~3 에이전트가 다관점 분석 후 종합.
비용: ~$1~2/토론 (Sonnet x3~4)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# 토론 대상 인텐트
DEBATE_INTENTS = frozenset({
    "strategy", "decision", "planning",
    "cto_analysis", "cto_strategy", "architect",
})

_MAX_COST_PER_DEBATE = 3.0  # USD
_SONNET_MODEL = "claude-sonnet-4-6-20250514"

# 기본 관점 3가지
DEFAULT_PERSPECTIVES = [
    {
        "name": "기술 (Technical)",
        "role": "researcher",
        "system": """당신은 기술 관점의 분석가입니다.
구현 가능성, 기술 리스크, 성능 영향, 아키텍처 적합성에 초점을 맞춥니다.
구체적 기술 근거와 수치를 제시하세요. 낙관적 편향을 경계하세요.""",
    },
    {
        "name": "비즈니스 (Business)",
        "role": "researcher",
        "system": """당신은 비즈니스 관점의 분석가입니다.
ROI, 시장 영향, 경쟁력, 사용자 가치, 비용 대비 효과에 초점을 맞춥니다.
CEO의 의사결정에 직접 도움이 되는 분석을 제공하세요.""",
    },
    {
        "name": "리스크 (Risk)",
        "role": "qa",
        "system": """당신은 리스크 분석가입니다.
장애 시나리오, 비용 초과 가능성, 외부 의존성, 보안 위험, 확장성 한계에 초점을 맞춥니다.
최악의 시나리오와 완화 방안을 함께 제시하세요. 다른 관점의 맹점을 지적하세요.""",
    },
    {
        "name": "독립 검증 (Gemini)",
        "role": "researcher",
        "model": "gemini",
        "system": """당신은 독립적 검증자입니다. 다른 AI의 분석과 무관하게 순수하게 질문 자체를 분석합니다.
숨겨진 전제, 간과된 대안, 비직관적 리스크를 찾아내세요.
다른 분석가들이 놓칠 수 있는 맹점을 지적하는 것이 핵심 역할입니다.""",
    },
]


@dataclass
class PerspectiveResult:
    """개별 관점 분석 결과."""
    name: str
    analysis: str
    key_points: list = field(default_factory=list)
    cost: float = 0.0
    duration_ms: int = 0


@dataclass
class DebateResult:
    """토론 종합 결과."""
    question: str
    perspectives: list  # List[PerspectiveResult]
    synthesis: str
    total_cost: float
    duration_ms: int
    debate_id: str = ""


async def should_debate(intent: str) -> bool:
    """토론 대상 인텐트인지 확인."""
    return intent in DEBATE_INTENTS


async def run_debate(
    question: str,
    intent: str = "",
    context: str = "",
    session_id: Optional[str] = None,
    perspectives: Optional[list] = None,
) -> DebateResult:
    """다관점 토론 실행."""
    start = time.time()
    debate_id = uuid.uuid4().hex[:12]
    persp_configs = perspectives or DEFAULT_PERSPECTIVES

    logger.info("debate_start debate_id=%s question=%s perspectives=%d",
                debate_id, question[:100], len(persp_configs))

    # Phase 1: 병렬 관점 분석
    perspective_results = await _run_perspectives(
        question, context, persp_configs, debate_id
    )

    # Phase 2: 종합
    synthesis = await _synthesize(question, perspective_results)

    # R-CRITICAL-003: 토론 결과에 구체적 수치가 포함되면 경고 삽입
    import re as _re
    if _re.search(r'\d+[%배점건]|\d+\s*→\s*\d+|\d+\.\d+', synthesis):
        synthesis += (
            "\n\n⚠️ **주의**: 위 토론에서 제시된 구체적 수치(%, 배, →)는 "
            "실측 근거가 아닌 LLM 추정입니다. "
            "실제 적용 전 백테스트/실측으로 검증이 필요합니다."
        )

    total_cost = sum(p.cost for p in perspective_results) + 0.01  # synthesis cost
    duration_ms = int((time.time() - start) * 1000)

    result = DebateResult(
        question=question,
        perspectives=perspective_results,
        synthesis=synthesis,
        total_cost=total_cost,
        duration_ms=duration_ms,
        debate_id=debate_id,
    )

    # DB 저장
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        sid = None
        if session_id:
            try:
                sid = uuid.UUID(session_id)
            except ValueError:
                pass
        persp_json = json.dumps(
            [{"name": p.name, "analysis": p.analysis[:2000], "key_points": p.key_points}
             for p in perspective_results],
            ensure_ascii=False,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO debate_sessions
                   (session_id, question, intent, perspectives, synthesis, total_cost, duration_ms)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)""",
                sid, question[:1000], intent,
                persp_json, synthesis[:5000],
                total_cost, duration_ms,
            )
    except Exception as db_err:
        logger.warning("debate_db_save_error: %s", str(db_err))

    logger.info("debate_complete debate_id=%s cost=%.4f duration_ms=%d",
                debate_id, total_cost, duration_ms)

    return result


async def _run_perspectives(
    question: str,
    context: str,
    perspectives: list,
    debate_id: str,
) -> list:
    """병렬로 관점 분석 실행."""
    tasks = []
    for persp in perspectives:
        tasks.append(_analyze_perspective(question, context, persp, debate_id))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    perspective_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning("perspective_failed name=%s error=%s",
                          perspectives[i]["name"], str(result))
            perspective_results.append(PerspectiveResult(
                name=perspectives[i]["name"],
                analysis=f"분석 실패: {str(result)[:200]}",
            ))
        else:
            perspective_results.append(result)

    return perspective_results


async def _analyze_perspective(
    question: str,
    context: str,
    persp: dict,
    debate_id: str,
) -> PerspectiveResult:
    """개별 관점 분석."""
    start = time.time()

    prompt = f"""다음 질문을 당신의 관점에서 분석하세요.

질문: {question}
{f'배경: {context[:500]}' if context else ''}

분석 후 다음 JSON 형식으로 응답:
{{"analysis": "상세 분석 (500자 이내)", "key_points": ["핵심 포인트 1", "핵심 포인트 2", "핵심 포인트 3"]}}"""

    try:
        model = persp.get("model", "claude-haiku-4-5-20251001")

        if "gemini" in model.lower():
            # Gemini 직접 호출 — 교차 모델 검증용
            from app.core.anthropic_client import _call_gemini
            result_text = await _call_gemini(
                prompt=prompt,
                max_tokens=512,
                system=persp.get("system", ""),
            )
        else:
            from app.core.anthropic_client import call_llm_with_fallback
            result_text = await call_llm_with_fallback(
                prompt=prompt,
                model=model,
                max_tokens=512,
                system=persp.get("system", ""),
            )

        if not result_text:
            return PerspectiveResult(name=persp["name"], analysis="응답 없음")

        import re
        text = result_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = {"analysis": text, "key_points": []}

        duration_ms = int((time.time() - start) * 1000)

        return PerspectiveResult(
            name=persp["name"],
            analysis=data.get("analysis", text[:1000]),
            key_points=data.get("key_points", [])[:5],
            cost=0.001,
            duration_ms=duration_ms,
        )

    except Exception as e:
        return PerspectiveResult(
            name=persp["name"],
            analysis=f"오류: {str(e)[:200]}",
        )


async def _synthesize(question: str, perspectives: list) -> str:
    """다관점 분석 결과 종합."""
    persp_text = ""
    for p in perspectives:
        persp_text += f"\n### {p.name}\n{p.analysis}\n"
        if p.key_points:
            persp_text += "핵심: " + ", ".join(p.key_points) + "\n"

    prompt = f"""다음 질문에 대해 3가지 관점의 분석이 완료되었습니다.

질문: {question}

{persp_text}

위 3가지 관점을 종합하여 CEO에게 보고할 최종 분석을 작성하세요.
- 관점 간 일치점과 불일치점을 명시
- CEO가 바로 의사결정할 수 있는 형태로 정리
- 추천 방향과 근거를 제시
- 마크다운 표 형식 활용"""

    try:
        from app.core.anthropic_client import call_llm_with_fallback
        result = await call_llm_with_fallback(
            prompt=prompt,
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="당신은 CEO를 위한 전략 종합 분석가입니다. 다관점 분석을 종합하여 명확한 의사결정 근거를 제공합니다.",
        )
        return result or "종합 분석 생성 실패"
    except Exception as e:
        logger.error("synthesis_error: %s", str(e))
        return f"종합 분석 오류: {str(e)[:200]}"
