"""
F2: Fact Extraction — 매 턴 AI 응답 후 핵심 사실 추출 → memory_facts 저장.
F7: Error Pattern Memory — 실패→성공 패턴을 error_pattern으로 추출.
F9: Decision Dependency — related_facts UUID로 의존관계 저장.
F12: Timeline Event — 시간순 프로젝트 이력 추적.

비용: ~$0.0005/턴 (Haiku), 백그라운드 실행.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# 워크스페이스명 → 프로젝트 코드 정규화
_PROJECT_KEYS = ("KIS", "AADS", "GO100", "SF", "NTV2", "NAS", "CEO")


def _normalize_project(raw: str | None) -> str | None:
    """'[KIS] 자동매매' → 'KIS', '[AADS] 프로젝트 매니저' → 'AADS' 등."""
    if not raw:
        return None
    upper = raw.upper()
    for key in _PROJECT_KEYS:
        if key in upper:
            return key
    return raw.upper()[:20] if raw else None

_HAIKU_MODEL = os.getenv("FACT_EXTRACTOR_MODEL", "qwen-turbo")
_MAX_FACTS_PER_TURN = int(os.getenv("FACT_EXTRACTOR_MAX_FACTS", "5"))

_EXTRACTION_PROMPT = """다음 대화 턴에서 핵심 사실을 최대 {max_facts}건 추출하세요.

카테고리:
- decision: CEO가 내린 결정/지시
- file_change: 코드/파일 변경 사항
- config_change: 설정/환경 변경
- error_resolution: 에러 해결 방법
- ceo_instruction: CEO의 운영 지침
- error_pattern: 실패→성공 패턴 (무엇이 안 됐고, 어떻게 해결했는지)
- timeline_event: 프로젝트 마일스톤/이벤트

JSON 배열로 반환. 각 항목:
{{"category": "...", "subject": "50자 이내 요약", "detail": "상세 설명 100자 이내", "tags": ["태그1"], "depends_on_subject": "의존하는 이전 결정 subject (없으면 null)"}}

대화 턴:
사용자: {user_msg}
AI: {ai_msg}

중요하지 않은 인사/잡담은 건너뛰고 빈 배열 [] 반환.
JSON만 반환하세요. 마크다운 코드블록 없이."""


async def extract_facts(
    user_message: str,
    ai_response: str,
    session_id: str,
    workspace_id: Optional[str] = None,
    project: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """AI 응답에서 핵심 사실을 추출하여 memory_facts에 저장."""
    if not ai_response or len(ai_response) < 50:
        return []

    try:
        from app.core.anthropic_client import call_llm_messages_with_fallback

        prompt = _EXTRACTION_PROMPT.format(
            max_facts=_MAX_FACTS_PER_TURN,
            user_msg=user_message[:500],
            ai_msg=ai_response[:2000],
        )

        response = await call_llm_messages_with_fallback(
            model=_HAIKU_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Clean markdown code block if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        facts = json.loads(text)
        if not isinstance(facts, list) or not facts:
            return []

        saved = await _save_facts(
            facts, session_id, workspace_id, project,
        )
        logger.info("fact_extraction_complete", count=len(saved), session=session_id[:8])
        return saved

    except json.JSONDecodeError as e:
        logger.debug("fact_extraction_json_error", error=str(e))
        return []
    except Exception as e:
        logger.warning("fact_extraction_error", error=str(e))
        return []


async def _save_facts(
    facts: List[Dict],
    session_id: str,
    workspace_id: Optional[str],
    project: Optional[str],
) -> List[Dict]:
    """추출된 사실을 memory_facts 테이블에 저장 + 임베딩 생성."""
    from app.core.db_pool import get_pool

    saved = []
    pool = get_pool()

    async with pool.acquire() as conn:
        for fact in facts[:_MAX_FACTS_PER_TURN]:
            category = fact.get("category", "")
            subject = fact.get("subject", "")
            detail = fact.get("detail", "")
            tags = fact.get("tags", [])

            if not category or not subject or not detail:
                continue

            try:
                fact_id = uuid.uuid4()
                sid = uuid.UUID(session_id) if session_id else None
                wid = uuid.UUID(workspace_id) if workspace_id else None

                # 의존 관계 처리 (F9)
                related = []
                depends_on = fact.get("depends_on_subject")
                if depends_on:
                    row = await conn.fetchrow(
                        "SELECT id FROM memory_facts WHERE subject ILIKE $1 AND project = $2 ORDER BY created_at DESC LIMIT 1",
                        f"%{depends_on[:100]}%",
                        _normalize_project(project),
                    )
                    if row:
                        related.append(row["id"])

                await conn.execute(
                    """
                    INSERT INTO memory_facts
                        (id, session_id, workspace_id, project, category, subject, detail, tags, related_facts, confidence)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0.7)
                    """,
                    fact_id, sid, wid,
                    _normalize_project(project),
                    category, subject[:300], detail, tags, related,
                )
                saved.append({"id": str(fact_id), "category": category, "subject": subject})

            except Exception as e:
                logger.debug("fact_save_error", error=str(e), subject=subject[:50])

    # 비동기 임베딩 생성
    if saved:
        import asyncio
        asyncio.create_task(_embed_facts(saved))

    return saved


async def _embed_facts(facts: List[Dict]) -> None:
    """memory_facts에 임베딩 벡터 생성."""
    try:
        from app.services.chat_embedding_service import embed_texts
        from app.core.db_pool import get_pool

        texts = [f"{f['category']}: {f['subject']}" for f in facts]
        embeddings = await embed_texts(texts)

        pool = get_pool()
        async with pool.acquire() as conn:
            for fact, emb in zip(facts, embeddings):
                if emb:
                    await conn.execute(
                        "UPDATE memory_facts SET embedding = $1 WHERE id = $2",
                        str(emb), uuid.UUID(fact["id"]),
                    )
    except Exception as e:
        logger.debug("fact_embedding_error", error=str(e))
