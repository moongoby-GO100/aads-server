"""
F1: Auto-RAG — 매 턴 사용자 메시지 임베딩 → memory_facts + chat_messages 시맨틱 검색.
F3: Cross-session Memory — 현재 세션뿐 아니라 모든 세션/워크스페이스 검색.

Top-5 관련 과거 컨텍스트를 Layer 4.5로 자동 주입.
토큰 예산: ~2000 tokens. 지연: ~100ms.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
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
        # 임베딩을 한 번만 생성하여 search + reask detection에 재사용
        from app.services.chat_embedding_service import embed_texts
        embeddings = await embed_texts([user_message[:500]])
        if not embeddings or not embeddings[0]:
            return ""
        query_emb = embeddings[0]

        results = await _search_relevant(query_emb, session_id, project, current_message_ids)
        if not results:
            return ""

        # A4: Re-ask detection — same session, high similarity, recent (임베딩 재사용)
        reask_warning = ""
        try:
            reask_detected = await _detect_reask(query_emb, session_id)
            if reask_detected:
                reask_warning = (
                    "\u26a0\ufe0f \uc774\uc804\uc5d0 \uc720\uc0ac\ud55c \uc9c8\ubb38\uc774 "
                    "\uc788\uc5c8\uc2b5\ub2c8\ub2e4. \uc774\uc804 \ub2f5\ubcc0\uc774 "
                    "\ubd80\uc871\ud588\uc744 \uc218 \uc788\uc73c\ub2c8 \ub354 \uc815\ud655\ud558\uace0 "
                    "\uc0c1\uc138\ud558\uac8c \ub2f5\ubcc0\ud558\uc138\uc694.\n"
                )
        except Exception:
            pass

        lines = []
        from app.core.token_utils import estimate_tokens
        total_tokens = 0
        used_fact_ids = []  # HIGH-5: 최종 출력에 포함된 fact ID만 추적

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
            # fact_id가 있으면 (memory_facts 출처) 추적
            if r.get("fact_id"):
                used_fact_ids.append(r["fact_id"])

        if not lines:
            return ""

        # HIGH-5: 최종 출력에 포함된 fact만 referenced_count 증가
        if used_fact_ids:
            try:
                from app.core.db_pool import get_pool
                pool = get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE memory_facts
                           SET referenced_count = referenced_count + 1,
                               last_referenced_at = NOW()
                           WHERE id = ANY($1::uuid[])""",
                        used_fact_ids,
                    )
            except Exception as e_ref:
                logger.debug("auto_rag_ref_update_error", error=str(e_ref))

        context = "\n".join(lines)
        block = (
            f"<auto_rag_context>\n"
            f"{reask_warning}"
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
    query_emb: list,
    session_id: str,
    project: Optional[str],
    current_message_ids: Optional[set],
) -> List[Dict[str, Any]]:
    """memory_facts + chat_messages에서 시맨틱 검색. query_emb는 사전 생성된 임베딩."""
    import asyncio

    results = []
    try:
        # 병렬: memory_facts 검색 + chat_messages 검색
        fact_results, msg_results = await asyncio.gather(
            _search_memory_facts(query_emb, project),
            _search_chat_messages(query_emb, session_id),
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


async def _search_memory_facts(query_emb: list, project: Optional[str]) -> List[Dict]:
    """memory_facts 테이블에서 시맨틱 검색. query_emb는 사전 생성된 임베딩."""
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()

        async with pool.acquire() as conn:
            if project:
                rows = await conn.fetch(
                    """
                    SELECT id, subject, detail, category, project, created_at,
                           referenced_count,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM memory_facts
                    WHERE embedding IS NOT NULL
                      AND superseded_by IS NULL
                      AND confidence > 0.3
                      AND project = $3
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2, project.upper(),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, subject, detail, category, project, created_at,
                           referenced_count,
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
            now_utc = datetime.now(timezone.utc)
            for r in rows:
                sim = float(r["similarity"]) if r["similarity"] else 0
                if sim < 0.3:
                    continue
                proj = r["project"] or ""
                ts = r["created_at"].strftime("%m/%d") if r["created_at"] else ""
                origin = "same_project" if proj == (project or "").upper() else "cross_session"

                # A2: Triple search score (Stanford Generative Agents style)
                days_old = 0.0
                if r["created_at"]:
                    created = r["created_at"]
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    days_old = max(0, (now_utc - created).total_seconds() / 86400)
                recency_weight = math.exp(-days_old / 30)

                ref_count = int(r["referenced_count"] or 0)
                importance_weight = 1.0 + math.log(1 + ref_count) * 0.1

                final_score = sim * recency_weight * importance_weight

                # HIGH-5: referenced_count는 여기서 증가하지 않음
                # → build_auto_rag_context에서 최종 출력 fact만 batch update

                results.append({
                    "source": f"[{proj}] {r['category']}",
                    "text": f"{r['subject']}: {r['detail'][:200]}",
                    "similarity": final_score,
                    "timestamp": ts,
                    "origin": origin,
                    "fact_id": r["id"],  # UUID for batch update
                })

            # Re-sort by composite score
            results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
            return results

    except Exception as e:
        logger.debug("auto_rag_facts_search_error", error=str(e))
        return []


async def _search_chat_messages(query_emb: list, session_id: str) -> List[Dict]:
    """chat_messages 테이블에서 시맨틱 검색 (크로스 세션 포함). query_emb는 사전 생성된 임베딩."""
    try:
        from app.services.chat_embedding_service import search_semantic
        from app.core.db_pool import get_pool

        pool = get_pool()
        # Cross-session search: session_id를 전달하지 않아 모든 세션에서 검색 (F3)
        results_all = await search_semantic(pool, query_emb, session_id=None, limit=_RAG_TOP_K * 2, pre_embedded=True)

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


async def _detect_reask(query_emb: list, session_id: str) -> bool:
    """A4: Detect if user is re-asking a similar question within the same session (last 30 min).
    MEDIUM-1 fix: query_emb는 사전 생성된 임베딩을 재사용 (중복 API 호출 제거).
    """
    try:
        import uuid as _uuid
        from app.core.db_pool import get_pool

        pool = get_pool()
        sid = _uuid.UUID(session_id)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 1 - (embedding <=> $1::vector) AS similarity
                FROM chat_messages
                WHERE session_id = $2
                  AND role = 'user'
                  AND embedding IS NOT NULL
                  AND created_at > NOW() - interval '30 minutes'
                ORDER BY embedding <=> $1::vector
                LIMIT 3
                """,
                str(query_emb), sid,
            )

            for r in rows:
                sim = float(r["similarity"]) if r["similarity"] else 0
                if sim > 0.85:
                    logger.info("a4_reask_detected", similarity=sim, session=session_id[:8])
                    return True

        return False
    except Exception as e:
        logger.debug("a4_reask_detection_error", error=str(e))
        return False
