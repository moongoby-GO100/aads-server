"""
F8: CEO Pattern Learning — 시간대, 요일, 주제, 워크스페이스별 CEO 행동 패턴 추적.
브리핑 시스템에 "예상 관심사항" 섹션 추가.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("CEO_PATTERN_TRACKING_ENABLED", "true").lower() == "true"
_KST = ZoneInfo("Asia/Seoul")


async def track_interaction(
    content: str,
    workspace_name: Optional[str] = None,
    intent: Optional[str] = None,
) -> None:
    """CEO 상호작용 패턴을 ceo_interaction_patterns에 기록."""
    if not _ENABLED or not content:
        return

    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        now = datetime.now(_KST)
        hour = now.hour
        weekday = now.strftime("%A")  # Monday, Tuesday, ...

        async with pool.acquire() as conn:
            # 시간대 패턴 (예: "hour_09" → workspace + intent 빈도)
            hour_key = f"hour_{hour:02d}"
            await _upsert_pattern(
                conn, "time_of_day", hour_key,
                {"workspace": workspace_name, "intent": intent, "count": 1},
            )

            # 요일 패턴
            await _upsert_pattern(
                conn, "day_of_week", weekday,
                {"workspace": workspace_name, "intent": intent, "count": 1},
            )

            # 워크스페이스별 주제 패턴
            if workspace_name:
                await _upsert_pattern(
                    conn, "workspace_topic", workspace_name,
                    {"intent": intent, "last_content_preview": content[:100], "count": 1},
                )

    except Exception as e:
        logger.debug("ceo_pattern_track_error", error=str(e))


async def _upsert_pattern(conn, pattern_type: str, pattern_key: str, value: dict) -> None:
    """패턴 upsert — 원자적 INSERT ON CONFLICT DO UPDATE (TOCTOU 방지)."""
    try:
        await conn.execute(
            """
            INSERT INTO ceo_interaction_patterns (pattern_type, pattern_key, pattern_value)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (pattern_type, pattern_key) DO UPDATE SET
                pattern_value = jsonb_set(
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                ceo_interaction_patterns.pattern_value,
                                '{count}',
                                to_jsonb(COALESCE((ceo_interaction_patterns.pattern_value->>'count')::int, 0) + 1)
                            ),
                            '{workspace}',
                            COALESCE(to_jsonb($4::text), ceo_interaction_patterns.pattern_value->'workspace')
                        ),
                        '{intent}',
                        COALESCE(to_jsonb($5::text), ceo_interaction_patterns.pattern_value->'intent')
                    ),
                    '{last_content_preview}',
                    COALESCE(to_jsonb($6::text), ceo_interaction_patterns.pattern_value->'last_content_preview')
                ),
                confidence = LEAST(1.0, ceo_interaction_patterns.confidence + 0.01),
                updated_at = NOW()
            """,
            pattern_type, pattern_key, json.dumps(value),
            value.get("workspace"), value.get("intent"), value.get("last_content_preview"),
        )
    except Exception as e:
        logger.debug("upsert_pattern_error", error=str(e), type=pattern_type)


async def get_predicted_interests() -> str:
    """현재 시간대/요일 기반 예상 관심사항 반환 (브리핑용)."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        now = datetime.now(_KST)
        hour_key = f"hour_{now.hour:02d}"
        weekday = now.strftime("%A")

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pattern_type, pattern_key, pattern_value, confidence
                FROM ceo_interaction_patterns
                WHERE (pattern_type = 'time_of_day' AND pattern_key = $1)
                   OR (pattern_type = 'day_of_week' AND pattern_key = $2)
                ORDER BY confidence DESC
                LIMIT 5
                """,
                hour_key, weekday,
            )

            if not rows:
                return ""

            lines = []
            for r in rows:
                val = r["pattern_value"] if isinstance(r["pattern_value"], dict) else json.loads(r["pattern_value"])
                ws = val.get("workspace", "")
                intent_val = val.get("intent", "")
                count = val.get("count", 0)
                if ws or intent_val:
                    lines.append(f"  - {r['pattern_type']}({r['pattern_key']}): {ws} / {intent_val} (×{count})")

            return "\n".join(lines) if lines else ""

    except Exception as e:
        logger.debug("predicted_interests_error", error=str(e))
        return ""
