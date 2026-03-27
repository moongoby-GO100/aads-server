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

# B4: 명확한 지시/결정 키워드 (자동 supersede 대상)
_DIRECTIVE_KEYWORDS = re.compile(
    r'지시|변경|취소|결정|확정|폐기|무효|대체|교체',
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
            if project:
                rows = await conn.fetch(
                    """
                    SELECT id, subject, detail, category, project, created_at,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM memory_facts
                    WHERE embedding IS NOT NULL
                      AND category IN ('decision', 'ceo_instruction')
                      AND superseded_by IS NULL
                      AND confidence > 0.5
                      AND project = $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT 3
                    """,
                    str(query_emb), project.upper(),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, subject, detail, category, project, created_at,
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
                    "id": str(r["id"]),
                    "subject": r["subject"],
                    "detail": r["detail"][:150],
                    "similarity": sim,
                    "project": proj,
                    "timestamp": ts,
                })

            if not contradictions:
                return ""

            # B4: 명확한 지시/결정 키워드가 있으면 자동 supersede
            is_directive = bool(_DIRECTIVE_KEYWORDS.search(user_message))
            auto_resolved = []
            if is_directive:
                auto_resolved = await auto_resolve_contradictions(
                    contradictions, user_message, project, conn,
                )

            # 경고 블록 생성
            lines = []
            for c in contradictions:
                resolved_tag = " [자동 대체됨]" if c["id"] in auto_resolved else ""
                lines.append(
                    f"  - [{c['timestamp']}][{c['project']}] {c['subject']}: {c['detail']} (유사도: {c['similarity']:.2f}){resolved_tag}"
                )

            if auto_resolved:
                footer = f"CEO 지시로 판단하여 {len(auto_resolved)}건의 이전 결정을 자동 대체(supersede) 처리했습니다."
            else:
                footer = "CEO에게 확인 후 진행하세요. 변경이 확정되면 이전 결정은 자동 supersede됩니다."

            warning = (
                f"<contradiction_warning>\n"
                f"⚠️ 이전 결정과 충돌 가능성 감지:\n"
                f"{''.join(chr(10) + l for l in lines)}\n"
                f"{footer}\n"
                f"</contradiction_warning>"
            )

            logger.info("contradiction_detected", count=len(contradictions), auto_resolved=len(auto_resolved), project=project)
            return warning

    except Exception as e:
        logger.debug("contradiction_detection_error", error=str(e))
        return ""


async def auto_resolve_contradictions(
    contradictions: list,
    user_message: str,
    project: Optional[str],
    conn,
) -> list:
    """B4: 명확한 지시/결정 메시지일 때 충돌하는 이전 사실을 자동 supersede.

    Returns:
        List of auto-resolved contradiction IDs.
    """
    import uuid as _uuid
    resolved_ids = []
    try:
        async with conn.transaction():
            # 새 지시를 memory_facts에 저장하여 supersede 대상 ID 확보
            new_fact_id = _uuid.uuid4()
            await conn.execute(
                """INSERT INTO memory_facts (id, project, category, subject, detail, confidence, tags)
                   VALUES ($1, $2, 'ceo_instruction', $3, $4, 0.9, ARRAY['auto_resolved', 'directive'])""",
                new_fact_id,
                (project or "").upper()[:20] or None,
                f"CEO 지시: {user_message[:80]}",
                user_message[:500],
            )

            for c in contradictions:
                try:
                    old_id = _uuid.UUID(c["id"])
                    await conn.execute(
                        "UPDATE memory_facts SET superseded_by = $1 WHERE id = $2 AND superseded_by IS NULL",
                        new_fact_id, old_id,
                    )
                    resolved_ids.append(c["id"])
                    logger.info(
                        "b4_auto_resolved",
                        old_subject=c["subject"][:50],
                        new_fact_id=str(new_fact_id)[:8],
                    )
                except Exception as e_res:
                    logger.debug("b4_auto_resolve_item_error", error=str(e_res))

    except Exception as e:
        logger.debug("b4_auto_resolve_error", error=str(e))

    return resolved_ids


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
