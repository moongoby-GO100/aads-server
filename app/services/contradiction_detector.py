"""
F10: Contradiction Detection — 사용자 메시지를 memory_facts의 decision/ceo_instruction과 비교.
유사도 > 0.85 + 부정어 감지 시 경고.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("CONTRADICTION_DETECTION_ENABLED", "true").lower() == "true"
_SIMILARITY_THRESHOLD = float(os.getenv("CONTRADICTION_SIMILARITY_THRESHOLD", "0.80"))

# 변경/취소 의도를 나타내는 키워드
_CHANGE_KEYWORDS = re.compile(
    r'취소|변경|대신|바꿔|수정|되돌려|롤백|안 할|하지 말|중단|포기|다시|반대로|아니[야라]|말고',
    re.IGNORECASE,
)


async def detect_contradictions(
    user_message: str,
    project: Optional[str] = None,
) -> str:
    """사용자 메시지에서 이전 결정과의 모순을 감지.

    Returns:
        Contradiction warning XML block or empty string.
    """
    if not _ENABLED or not user_message or len(user_message) < 15:
        return ""

    # 변경 의도 키워드가 없으면 스킵
    if not _CHANGE_KEYWORDS.search(user_message):
        return ""

    try:
        from app.services.chat_embedding_service import embed_texts
        from app.core.db_pool import get_pool

        embeddings = await embed_texts([user_message[:500]])
        if not embeddings or not embeddings[0]:
            return ""

        query_emb = embeddings[0]
        pool = get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject, detail, category, project, created_at,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM memory_facts
                WHERE embedding IS NOT NULL
                  AND category IN ('decision', 'ceo_instruction')
                  AND superseded_by IS NULL
                  AND confidence > 0.5
                ORDER BY embedding <=> $1::vector
                LIMIT 3
                """,
                str(query_emb),
            )

            contradictions = []
            for r in rows:
                sim = float(r["similarity"]) if r["similarity"] else 0
                if sim < _SIMILARITY_THRESHOLD:
                    continue

                proj = r["project"] or ""
                ts = r["created_at"].strftime("%m/%d") if r["created_at"] else ""
                contradictions.append({
                    "subject": r["subject"],
                    "detail": r["detail"][:150],
                    "similarity": sim,
                    "project": proj,
                    "timestamp": ts,
                })

            if not contradictions:
                return ""

            # 경고 블록 생성
            lines = []
            for c in contradictions:
                lines.append(
                    f"  - [{c['timestamp']}][{c['project']}] {c['subject']}: {c['detail']} (유사도: {c['similarity']:.2f})"
                )

            warning = (
                f"<contradiction_warning>\n"
                f"⚠️ 이전 결정과 충돌 가능성 감지:\n"
                f"{''.join(chr(10) + l for l in lines)}\n"
                f"CEO에게 확인 후 진행하세요. 변경이 확정되면 이전 결정은 자동 supersede됩니다.\n"
                f"</contradiction_warning>"
            )

            logger.info("contradiction_detected", count=len(contradictions), project=project)
            return warning

    except Exception as e:
        logger.debug("contradiction_detection_error", error=str(e))
        return ""


async def supersede_fact(old_subject: str, new_fact_id: str, project: Optional[str] = None) -> bool:
    """이전 결정을 supersede 처리."""
    try:
        from app.core.db_pool import get_pool
        import uuid
        pool = get_pool()

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memory_facts
                SET superseded_by = $1
                WHERE subject ILIKE $2
                  AND category IN ('decision', 'ceo_instruction')
                  AND superseded_by IS NULL
                  AND ($3::varchar IS NULL OR project = $3)
                """,
                uuid.UUID(new_fact_id),
                f"%{old_subject[:100]}%",
                (project or "").upper() or None,
            )
            return True
    except Exception as e:
        logger.warning("supersede_fact_error", error=str(e))
        return False
