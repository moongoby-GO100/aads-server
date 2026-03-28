"""
메타-메타 루프 — 메모리 효과성 자기평가 + 자동 튜닝.

P2-2: "메모리가 실제로 도움이 되었는가?"를 통계 기반으로 측정하고
confidence를 자동 조정하여 메모리 시스템 자체를 진화시킨다.

주요 함수:
- evaluate_memory_effectiveness(project) -> 활용률/재교정 횟수 측정
- auto_tune_memory(project)              -> confidence 자동 조정 + 평가 결과 저장
"""
from __future__ import annotations

import json
import structlog
from typing import Optional

logger = structlog.get_logger(__name__)

# 활용률 임계값: 이 미만이면 confidence 감소 대상
_LOW_UTILIZATION_THRESHOLD = 0.30

# 재교정 빈도 임계값: 주당 이 이상이면 해당 메모리를 높은 신뢰로 상향
_HIGH_CORRECTION_THRESHOLD = 3

# confidence 조정 상수
_CONFIDENCE_DECREASE = 0.1
_CONFIDENCE_MINIMUM = 0.1
_CONFIDENCE_HIGH_VALUE = 0.9

# GC와 동일한 보호 카테고리
_PROTECTED_CATEGORIES = ("ceo_preference", "ceo_directive", "compaction_directive")


def _get_pool():
    """DB 커넥션 풀 반환."""
    from app.core.db_pool import get_pool
    return get_pool()


async def evaluate_memory_effectiveness(project: Optional[str] = None) -> dict:
    """메모리 효과성 평가 — 순수 통계 기반, LLM 호출 없음.

    측정 항목:
    1. 활용률: 최근 24시간 내 last_used_at IS NOT NULL 건수 / 전체 건수
    2. CEO 재교정 횟수: 최근 7일 ceo_correction 카테고리 건수 (만족도 역프록시)

    Args:
        project: 프로젝트 필터 (None 이면 전체, 값 전달 시 해당 project + 공통 포함)

    Returns:
        {
          "utilization_rate": float,   # 0.0 ~ 1.0
          "re_correction_count": int,  # 최근 7일 ceo_correction 건수
          "total_memories": int,       # 전체 ai_observations 건수
          "active_memories": int,      # 최근 24시간 내 활용된 건수
        }
    """
    result = {
        "utilization_rate": 0.0,
        "re_correction_count": 0,
        "total_memories": 0,
        "active_memories": 0,
    }
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            # 1. 전체 건수 + 최근 24시간 활용 건수
            if project:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (
                            WHERE last_used_at > NOW() - INTERVAL '24 hours'
                        ) AS active
                    FROM ai_observations
                    WHERE project = $1 OR project IS NULL
                    """,
                    project,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (
                            WHERE last_used_at > NOW() - INTERVAL '24 hours'
                        ) AS active
                    FROM ai_observations
                    """
                )

            total = int(row["total"]) if row else 0
            active = int(row["active"]) if row else 0
            utilization = active / total if total > 0 else 0.0

            result["total_memories"] = total
            result["active_memories"] = active
            result["utilization_rate"] = round(utilization, 4)

            # 2. 최근 7일 ceo_correction 카테고리 재교정 건수
            if project:
                correction_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ai_observations
                    WHERE category = 'ceo_correction'
                      AND (project = $1 OR project IS NULL)
                      AND updated_at > NOW() - INTERVAL '7 days'
                    """,
                    project,
                )
            else:
                correction_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ai_observations
                    WHERE category = 'ceo_correction'
                      AND updated_at > NOW() - INTERVAL '7 days'
                    """
                )

            result["re_correction_count"] = int(correction_count) if correction_count else 0

        logger.info(
            "memory_effectiveness_evaluated",
            project=project,
            **result,
        )
    except Exception as e:
        logger.error("memory_effectiveness_eval_error", project=project, error=str(e))

    return result


async def auto_tune_memory(project: Optional[str] = None) -> dict:
    """메모리 자동 튜닝 — confidence 조정 + 평가 결과 ai_meta_memory 저장.

    규칙:
    1. 활용률 < 30% 카테고리: confidence = GREATEST(confidence - 0.1, 0.1) 일괄 감소
       (보호 카테고리: ceo_preference / ceo_directive / compaction_directive 제외)
    2. 재교정 빈도 > 3회/week인 key: confidence = 0.9 로 상향
    3. 평가 결과를 ai_meta_memory category='meta_evaluation' 으로 저장

    Args:
        project: 프로젝트 필터 (None 이면 전체)

    Returns:
        {"tuned_down": int, "tuned_up": int, "evaluation": dict}
    """
    tune_result: dict = {"tuned_down": 0, "tuned_up": 0, "evaluation": {}}

    try:
        evaluation = await evaluate_memory_effectiveness(project)
        tune_result["evaluation"] = evaluation

        pool = _get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():

                # ── 규칙 1: 활용률 낮은 카테고리 confidence 감소 ──────────────
                if evaluation["utilization_rate"] < _LOW_UTILIZATION_THRESHOLD:
                    # 활용률이 낮은 카테고리 목록을 집계
                    low_cats = await conn.fetch(
                        """
                        SELECT category
                        FROM ai_observations
                        WHERE category NOT IN (SELECT unnest($1::text[]))
                        GROUP BY category
                        HAVING
                            COUNT(*) FILTER (
                                WHERE last_used_at > NOW() - INTERVAL '24 hours'
                            )::float / NULLIF(COUNT(*), 0) < $2
                        """,
                        list(_PROTECTED_CATEGORIES),
                        _LOW_UTILIZATION_THRESHOLD,
                    )

                    total_tuned_down = 0
                    for cat_row in low_cats:
                        cat = cat_row["category"]
                        if project:
                            res = await conn.execute(
                                """
                                UPDATE ai_observations
                                SET confidence = GREATEST(confidence - $1, $2),
                                    updated_at = NOW()
                                WHERE category = $3
                                  AND (project = $4 OR project IS NULL)
                                  AND category NOT IN (SELECT unnest($5::text[]))
                                """,
                                _CONFIDENCE_DECREASE,
                                _CONFIDENCE_MINIMUM,
                                cat,
                                project,
                                list(_PROTECTED_CATEGORIES),
                            )
                        else:
                            res = await conn.execute(
                                """
                                UPDATE ai_observations
                                SET confidence = GREATEST(confidence - $1, $2),
                                    updated_at = NOW()
                                WHERE category = $3
                                  AND category NOT IN (SELECT unnest($4::text[]))
                                """,
                                _CONFIDENCE_DECREASE,
                                _CONFIDENCE_MINIMUM,
                                cat,
                                list(_PROTECTED_CATEGORIES),
                            )
                        total_tuned_down += int(res.split()[-1]) if res else 0

                    tune_result["tuned_down"] = total_tuned_down
                    if total_tuned_down > 0:
                        logger.info(
                            "memory_auto_tune_down",
                            project=project,
                            count=total_tuned_down,
                            utilization_rate=evaluation["utilization_rate"],
                        )

                # ── 규칙 2: 재교정 빈도 높은 key confidence 상향 ─────────────
                if evaluation["re_correction_count"] > _HIGH_CORRECTION_THRESHOLD:
                    high_corr_keys = await conn.fetch(
                        """
                        SELECT key
                        FROM ai_observations
                        WHERE category = 'ceo_correction'
                          AND updated_at > NOW() - INTERVAL '7 days'
                        GROUP BY key
                        HAVING COUNT(*) > $1
                        """,
                        _HIGH_CORRECTION_THRESHOLD,
                    )

                    total_tuned_up = 0
                    for key_row in high_corr_keys:
                        k = key_row["key"]
                        if project:
                            res = await conn.execute(
                                """
                                UPDATE ai_observations
                                SET confidence = $1,
                                    updated_at = NOW()
                                WHERE key = $2
                                  AND (project = $3 OR project IS NULL)
                                """,
                                _CONFIDENCE_HIGH_VALUE,
                                k,
                                project,
                            )
                        else:
                            res = await conn.execute(
                                """
                                UPDATE ai_observations
                                SET confidence = $1,
                                    updated_at = NOW()
                                WHERE key = $2
                                """,
                                _CONFIDENCE_HIGH_VALUE,
                                k,
                            )
                        total_tuned_up += int(res.split()[-1]) if res else 0

                    tune_result["tuned_up"] = total_tuned_up
                    if total_tuned_up > 0:
                        logger.info(
                            "memory_auto_tune_up",
                            project=project,
                            count=total_tuned_up,
                            re_correction_count=evaluation["re_correction_count"],
                        )

            # ── 규칙 3: 평가 결과 ai_meta_memory 저장 ────────────────────────
            meta_key = f"meta_eval_{project.lower() if project else 'global'}"
            meta_value = json.dumps(
                {
                    "project": project,
                    "utilization_rate": evaluation["utilization_rate"],
                    "re_correction_count": evaluation["re_correction_count"],
                    "total_memories": evaluation["total_memories"],
                    "active_memories": evaluation["active_memories"],
                    "tuned_down": tune_result["tuned_down"],
                    "tuned_up": tune_result["tuned_up"],
                },
                ensure_ascii=False,
            )

            await conn.execute(
                """
                INSERT INTO ai_meta_memory (category, key, value, confidence, updated_at)
                VALUES ('meta_evaluation', $1, $2, 0.8, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value      = EXCLUDED.value,
                        updated_at = NOW()
                """,
                meta_key,
                meta_value,
            )

        logger.info(
            "memory_auto_tune_complete",
            project=project,
            tuned_down=tune_result["tuned_down"],
            tuned_up=tune_result["tuned_up"],
        )

    except Exception as e:
        logger.error("memory_auto_tune_error", project=project, error=str(e))

    return tune_result
