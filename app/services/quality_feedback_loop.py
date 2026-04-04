"""
AI 자율 연구개선 — Phase 1: 품질 피드백 루프
quality_score 누적 데이터 → 약점 분야 자동 발견 → 교정 지시 생성.
Sleep-Time Agent(매일 05:00 UTC) 이후 06:00 UTC에 실행.
비용: ~$0.001/일 (Haiku 1회)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "qwen-turbo"


async def analyze_quality_weaknesses(pool) -> dict:
    """주간 품질 데이터를 분석하여 약점 분야 발견 + 교정 지시 생성.

    1. 최근 7일 quality_details에서 차원별 평균 점수 계산
    2. 0.5 미만인 차원을 약점으로 식별
    3. 워크스페이스별 품질 편차 분석
    4. Haiku로 구체적 교정 지시 생성
    5. ai_meta_memory에 교정 지시 저장 → 다음 응답부터 주입

    Returns: {"weaknesses": [...], "directives_created": int}
    """
    result = {"weaknesses": [], "directives_created": 0, "analyzed": 0}

    try:
        async with pool.acquire() as conn:
            # 1. 최근 7일 차원별 점수 집계
            rows = await conn.fetch("""
                SELECT
                    quality_details,
                    quality_score
                FROM chat_messages
                WHERE role = 'assistant'
                  AND quality_score IS NOT NULL
                  AND quality_details IS NOT NULL
                  AND created_at >= NOW() - interval '7 days'
                ORDER BY created_at DESC
                LIMIT 200
            """)

            if not rows or len(rows) < 5:
                logger.info("quality_feedback_skip_insufficient_data", count=len(rows))
                return result

            result["analyzed"] = len(rows)

            # 차원별 점수 수집
            dimensions = {
                "context_awareness": [],
                "accuracy": [],
                "completeness": [],
                "relevance": [],
                "tool_grounding": [],
                "actionability": [],
            }
            workspace_scores = {}

            for row in rows:
                details = row["quality_details"]
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not isinstance(details, dict):
                    continue

                for dim in dimensions:
                    val = details.get(dim)
                    if val is not None:
                        try:
                            dimensions[dim].append(float(val))
                        except (ValueError, TypeError):
                            pass

            # 2. 약점 식별 (평균 0.55 미만)
            weaknesses = []
            for dim, scores in dimensions.items():
                if not scores:
                    continue
                avg = sum(scores) / len(scores)
                if avg < 0.55:
                    weaknesses.append({
                        "dimension": dim,
                        "avg_score": round(avg, 3),
                        "sample_count": len(scores),
                    })

            result["weaknesses"] = weaknesses

            if not weaknesses:
                logger.info("quality_feedback_no_weaknesses", analyzed=len(rows))
                return result

            # 3. 교정 지시 생성 (Haiku)
            weakness_text = "\n".join(
                f"- {w['dimension']}: 평균 {w['avg_score']} ({w['sample_count']}건)"
                for w in weaknesses
            )

            try:
                from app.core.anthropic_client import call_llm_with_fallback
                directive_text = await call_llm_with_fallback(
                    prompt=(
                        f"AADS AI의 최근 7일 응답 품질 분석 결과, 다음 영역이 약합니다:\n\n"
                        f"{weakness_text}\n\n"
                        f"각 약점에 대해 구체적인 개선 지시를 작성하세요.\n"
                        f"'항상 ~하라', '반드시 ~하라' 형태로 작성.\n"
                        f"약점당 1~2문장, JSON 배열로 반환:\n"
                        f'[{{"dimension": "xxx", "directive": "..."}}, ...]'
                    ),
                    model=_HAIKU_MODEL,
                    max_tokens=512,
                )

                if directive_text:
                    import re
                    # JSON 배열 파싱
                    json_match = re.search(r'\[[\s\S]*\]', directive_text)
                    if json_match:
                        directives = json.loads(json_match.group())
                    else:
                        directives = []

                    # ai_meta_memory에 저장
                    import time
                    for d in directives:
                        dim = d.get("dimension", "unknown")
                        directive = d.get("directive", "")
                        if not directive:
                            continue

                        key = f"quality_feedback:{dim}:{int(time.time())}"
                        value_json = json.dumps({
                            "directive": directive[:500],
                            "dimension": dim,
                            "avg_score": next(
                                (w["avg_score"] for w in weaknesses if w["dimension"] == dim),
                                0
                            ),
                            "source": "quality_feedback_loop",
                        })
                        await conn.execute(
                            """INSERT INTO ai_meta_memory
                               (project, category, key, value, updated_at)
                               VALUES ('AADS', 'quality_improvement', $1, $2::jsonb, NOW())
                               ON CONFLICT (project, category, key)
                               DO UPDATE SET value = $2::jsonb, updated_at = NOW()""",
                            key, value_json,
                        )
                        result["directives_created"] += 1

            except Exception as llm_err:
                logger.warning("quality_feedback_llm_error", error=str(llm_err))

        logger.info(
            "quality_feedback_complete",
            weaknesses=len(weaknesses),
            directives=result["directives_created"],
            analyzed=result["analyzed"],
        )

    except Exception as e:
        logger.error("quality_feedback_error", error=str(e))

    return result


async def get_quality_improvement_directives(pool) -> list:
    """현재 활성 품질 개선 지시 목록 조회. 컨텍스트 빌더에서 호출."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT key, value
                   FROM ai_meta_memory
                   WHERE category = 'quality_improvement'
                     AND updated_at > NOW() - interval '7 days'
                   ORDER BY updated_at DESC
                   LIMIT 5"""
            )
            return [
                json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
                for r in rows
            ]
    except Exception as e:
        logger.debug("get_quality_directives_error", error=str(e))
        return []
