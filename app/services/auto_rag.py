"""
F1: Auto-RAG — 매 턴 사용자 메시지 임베딩 → memory_facts + chat_messages 시맨틱 검색.
F3: Cross-session Memory — 현재 세션뿐 아니라 모든 세션/워크스페이스 검색.

Top-5 관련 과거 컨텍스트를 Layer 4.5로 자동 주입.
토큰 예산: ~2000 tokens. 지연: ~100ms.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

_AUTO_RAG_ENABLED = os.getenv("AUTO_RAG_ENABLED", "true").lower() == "true"
_RAG_TOP_K = int(os.getenv("AUTO_RAG_TOP_K", "5"))
_RAG_TOKEN_BUDGET = int(os.getenv("AUTO_RAG_TOKEN_BUDGET", "2000"))
_CROSS_SESSION_WEIGHT = float(os.getenv("AUTO_RAG_CROSS_SESSION_WEIGHT", "0.85"))


async def build_auto_rag_context(
    user_message: str,
    session_id: str,
    project: Optional[str] = None,
    current_message_ids: Optional[set] = None,
) -> str:
    """사용자 메시지에 대한 Auto-RAG 컨텍스트 생성.

    Returns:
        XML-wrapped context string for Layer 4.5 injection.
    """
    if not _AUTO_RAG_ENABLED or not user_message or len(user_message) < 10:
        return ""

    try:
        results = await _search_relevant(user_message, session_id, project, current_message_ids)
        if not results:
            return ""

        lines = []
        from app.core.token_utils import estimate_tokens
        total_tokens = 0

        for r in results:
            source = r.get("source", "")
            text = r.get("text", "")
            sim = r.get("similarity", 0)
            ts = r.get("timestamp", "")

            # Cross-session 가중치 적용 (F3)
            origin = r.get("origin", "")
            if origin == "cross_session":
                sim *= _CROSS_SESSION_WEIGHT

            line = f"- [{source}] ({ts}, 유사도:{sim:.2f}) {text}"
            line_tokens = estimate_tokens(line)

            if total_tokens + line_tokens > _RAG_TOKEN_BUDGET:
                break

            lines.append(line)
            total_tokens += line_tokens

        if not lines:
            return ""

        context = "\n".join(lines)
        block = (
            f"<auto_rag_context>\n"
            f"## 관련 과거 컨텍스트 (자동 검색)\n"
            f"{context}\n"
            f"</auto_rag_context>"
        )

        logger.info("auto_rag_injected", results=len(lines), tokens=total_tokens, session=session_id[:8])
        return block

    except Exception as e:
        logger.debug("auto_rag_build_error", error=str(e))
        return ""


async def _search_relevant(
    query: str,
    session_id: str,
    project: Optional[str],
    current_message_ids: Optional[set],
) -> List[Dict[str, Any]]:
    """memory_facts + chat_messages에서 시맨틱 검색."""
    import asyncio

    results = []
    try:
        # 병렬: memory_facts 검색 + chat_messages 검색
        fact_results, msg_results = await asyncio.gather(
            _search_memory_facts(query, project),
            _search_chat_messages(query, session_id),
            return_exceptions=True,
        )

        if isinstance(fact_results, list):
            results.extend(fact_results)
        if isinstance(msg_results, list):
            # 현재 대화 히스토리와 중복 제거
            for r in msg_results:
                msg_id = r.get("msg_id", "")
                if current_message_ids and msg_id in current_message_ids:
                    continue
                results.append(r)

        # 유사도 내림차순 정렬 후 Top-K
        results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
        return results[:_RAG_TOP_K]

    except Exception as e:
        logger.debug("auto_rag_search_error", error=str(e))
        return []


async def _search_memory_facts(query: str, project: Optional[str]) -> List[Dict]:
    """memory_facts 테이블에서 시맨틱 검색."""
    try:
        from app.services.chat_embedding_service import embed_texts
        from app.core.db_pool import get_pool

        embeddings = await embed_texts([query[:500]])
        if not embeddings or not embeddings[0]:
            return []

        query_emb = embeddings[0]
        pool = get_pool()

        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    """
                    SELECT subject, detail, category, project, created_at,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM memory_facts
                    WHERE embedding IS NOT NULL
                      AND superseded_by IS NULL
                      AND confidence > 0.3
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT subject, detail, category, project, created_at,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM memory_facts
                    WHERE embedding IS NOT NULL
                      AND superseded_by IS NULL
                      AND confidence > 0.3
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2,
                )

            results = []
            for r in rows:
                sim = float(r["similarity"]) if r["similarity"] else 0
                if sim < 0.3:
                    continue
                proj = r["project"] or ""
                ts = r["created_at"].strftime("%m/%d") if r["created_at"] else ""
                origin = "same_project" if proj == (project or "").upper() else "cross_session"

                # referenced_count 증가
                await conn.execute(
                    "UPDATE memory_facts SET referenced_count = referenced_count + 1, last_referenced_at = NOW() WHERE subject = $1 AND category = $2",
                    r["subject"], r["category"],
                )

                results.append({
                    "source": f"[{proj}] {r['category']}",
                    "text": f"{r['subject']}: {r['detail'][:200]}",
                    "similarity": sim,
                    "timestamp": ts,
                    "origin": origin,
                })

            return results

    except Exception as e:
        logger.debug("auto_rag_facts_search_error", error=str(e))
        return []


async def _search_chat_messages(query: str, session_id: str) -> List[Dict]:
    """chat_messages 테이블에서 시맨틱 검색 (크로스 세션 포함)."""
    try:
        from app.services.chat_embedding_service import search_semantic
        from app.core.db_pool import get_pool

        pool = get_pool()
        # 전체 세션 검색 (크로스 세션 = F3)
        results_all = await search_semantic(pool, query, session_id=None, limit=_RAG_TOP_K * 2)

        output = []
        for r in results_all:
            sim = r.get("similarity", 0)
            if sim < 0.3:
                continue

            is_current = str(r.get("session_id", "")) == session_id
            origin = "same_session" if is_current else "cross_session"
            session_name = r.get("session_name", "")
            ts = ""
            if r.get("created_at"):
                ts = r["created_at"].strftime("%m/%d") if hasattr(r["created_at"], "strftime") else str(r["created_at"])[:10]

            output.append({
                "source": f"대화({session_name[:20]})" if session_name else "대화",
                "text": (r.get("content", ""))[:200],
                "similarity": sim,
                "timestamp": ts,
                "origin": origin,
                "msg_id": str(r.get("id", "")),
            })

        return output

    except Exception as e:
        logger.debug("auto_rag_messages_search_error", error=str(e))
        return []
