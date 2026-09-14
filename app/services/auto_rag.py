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

# CEO 통합지시 워크스페이스 — 전 프로젝트 시맨틱 검색 대상
# 통합지시 검색 범위는 `project_config` 가 정본이다 — 사본을 만들지 마라.
# 환경변수 `CEO_ORCHESTRATOR_PROJECTS` 로 바꾼다.
from app.core.project_config import ORCHESTRATOR_PROJECTS as _CEO_ORCHESTRATOR_PROJECTS


def _is_ceo_orchestrator(project: Optional[str]) -> bool:
    """CEO 통합지시 워크스페이스 여부 판별. 일반 프로젝트 세션은 격리 유지."""
    if not project:
        return False
    return "CEO" in project.upper() or "통합" in project


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

        # 찾는 쪽 벡터는 두 벌이다.
        #
        #   query_emb  접두어 없음 — memory_facts 용. 저장된 쪽이 아직 옛
        #              세대라서 같은 규격으로 비교해야 한다.
        #   ask_emb    `search_query: ` 접두어 — doc_chunks 와 세대 2
        #              chat_messages 용.
        #
        # 세대가 다른 벡터끼리 비교하면 유사도 숫자는 나오는데 뜻이 없다.
        # 한 벌로 두 세대를 다 찾으려다 2026-09-14 하루를 날렸다.
        ask_emb = None
        try:
            from app.services.doc_index import embed_query

            ask_emb = await embed_query(user_message[:500])
        except Exception as e:
            logger.debug("auto_rag_query_embed_failed", error=str(e))

        results = await _search_relevant(
            query_emb, session_id, project, current_message_ids, user_message,
            ask_emb=ask_emb,
        )

        # 그래프 근거는 벡터 검색과 별개다.
        #
        # 질문에 파일 경로·커밋·오류 키가 들어 있으면 그것이 **무엇과
        # 이어져 있는지**를 준다 — 어떤 오류가 그 파일에 있었고, 어느
        # 커밋이 고쳤고, 어느 문서가 설명하는지.
        #
        # 벡터 검색이 빈손이어도 그래프는 답이 있을 수 있다. 예전처럼
        # 여기서 일찍 반환하면 그 근거까지 같이 버린다.
        #
        # 관계가 없으면 빈 문자열이다. 억지로 비슷한 것을 끌어오지 않는다 —
        # 그래프의 값어치는 "확실히 이어져 있다" 하나다.
        graph_context = ""
        try:
            from app.services.kg_query import context_for_question

            graph_context = await context_for_question(user_message)
        except Exception as e:
            logger.debug("auto_rag_graph_failed", error=str(e))

        if not results:
            return graph_context

        # A4: Re-ask detection — same session, high similarity, recent (임베딩 재사용)
        reask_warning = ""
        try:
            reask_detected = await _detect_reask(ask_emb, session_id)
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
            (graph_context + "\n" if graph_context else "")
            + f"<auto_rag_context>\n"
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
    query_text: str = "",
    ask_emb: Optional[list] = None,
) -> List[Dict[str, Any]]:
    """memory_facts + chat_messages + 문서에서 시맨틱 검색.

    `query_emb` 는 접두어 없는 벡터(memory_facts 용), `ask_emb` 는
    `search_query: ` 접두어가 붙은 벡터(문서·세대 2 채팅 메시지 용)다.
    둘 다 바깥에서 한 번씩 만들어 넘긴다 — 여기서 또 만들면 CPU Ollama 에
    같은 문장을 두 번 태운다.
    """
    import asyncio

    results = []
    try:
        # 병렬: memory_facts + chat_messages + 문서
        #
        # 2026-09-14 문서 추가. 그 전까지 대표님이 "이거 왜 이렇게 돼 있지" 라고
        # 물어도 문서가 근거로 잡히지 않았다 — 검색 대상이 아니었기 때문이다.
        # 문서 823건이 저장소에 있는데 채팅은 그걸 못 봤다.
        fact_results, msg_results, doc_results = await asyncio.gather(
            _search_memory_facts(query_emb, project),
            _search_chat_messages(ask_emb, session_id, project),
            _search_documents(ask_emb, project, query_text),
            return_exceptions=True,
        )

        if isinstance(doc_results, list):
            results.extend(doc_results)
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


async def _search_documents(
    query_emb: list, project: Optional[str], query_text: str = ""
) -> List[Dict]:
    """저장소 문서에서 검색. 결과에 출처 경로를 넣는다.

    근거 경로가 없으면 비전문가는 답을 검증할 방법이 없다. "어디에 그렇게
    적혀 있나" 에 답할 수 있어야 문서를 붙인 의미가 있다.
    """
    try:
        from app.services.doc_index import search_docs

        # 접두어 붙은 질문 벡터가 없으면 문서 검색은 건너뛴다 — 접두어 없는
        # 벡터로 찾느니 안 찾는 편이 낫다.
        if query_emb is None:
            return []
        rows = await search_docs(query_emb, top_k=_RAG_TOP_K, project=None)
    except Exception as e:
        logger.debug("auto_rag_doc_search_failed", error=str(e))
        return []

    import os

    out: List[Dict] = []
    for r in rows:
        path = r.get("doc_path", "")
        heading = r.get("heading", "")
        title = r.get("title", "")
        # 포맷터가 읽는 키는 `text`/`source`/`timestamp` 다. 문서 출처는
        # **파일 경로**여야 한다 — "어디에 그렇게 적혀 있나" 에 답할 수
        # 있어야 근거로서 쓸모가 있다.
        where = f"{title} › {heading}" if heading else title
        out.append({
            "kind": "doc",
            "source": f"문서 {os.path.basename(path)}",
            "msg_id": f"doc:{path}",
            "similarity": r.get("similarity", 0.0),
            "text": f"[{where}] {r.get('content', '')}",
            "timestamp": path,
            "path": path,
        })
    return out


async def _search_memory_facts(query_emb: list, project: Optional[str]) -> List[Dict]:
    """memory_facts 테이블에서 시맨틱 검색. query_emb는 사전 생성된 임베딩."""
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        orchestrator = _is_ceo_orchestrator(project)

        async with pool.acquire() as conn:
            if orchestrator:
                rows = await conn.fetch(
                    """
                    SELECT id, subject, detail, category, project, created_at,
                           referenced_count,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM memory_facts
                    WHERE embedding IS NOT NULL
                      AND superseded_by IS NULL
                      AND confidence > 0.3
                      AND project = ANY($3::text[])
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2, _CEO_ORCHESTRATOR_PROJECTS,
                )
            elif project:
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
                # 프로젝트 미지정 시: 공통 팩트만 검색 (타 프로젝트 오염 방지)
                rows = await conn.fetch(
                    """
                    SELECT id, subject, detail, category, project, created_at,
                           referenced_count,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM memory_facts
                    WHERE embedding IS NOT NULL
                      AND superseded_by IS NULL
                      AND confidence > 0.3
                      AND (project IS NULL OR project = '')
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


async def _search_chat_messages(query_emb: Optional[list], session_id: str, project: Optional[str] = None) -> List[Dict]:
    """chat_messages 테이블에서 시맨틱 검색 (동일 프로젝트 워크스페이스 내 크로스 세션).

    **세대 2 벡터만 본다.** 2026-09-14 이전에 쌓인 33,075건은 임베딩 경로가
    끊긴 채 저장된 해시 더미다. 섞어서 검색하면 더미가 상위에 올라온다 —
    값이 난수라 어떤 질문과도 적당히 비슷하게 나오기 때문이다.
    백필은 `scripts/backfill_chat_embeddings.py` 가 채운다.
    """
    if query_emb is None:
        return []
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        orchestrator = _is_ceo_orchestrator(project)
        async with pool.acquire() as conn:
            if orchestrator:
                rows = await conn.fetch(
                    """
                    SELECT m.id, m.role, m.content, m.created_at,
                           m.session_id::text AS session_id,
                           s.title AS session_name,
                           1 - (m.embedding <=> $1::vector) AS similarity
                    FROM chat_messages m
                    JOIN chat_sessions s ON s.id = m.session_id
                    JOIN chat_workspaces w ON w.id = s.workspace_id
                    WHERE m.embedding IS NOT NULL
                      AND m.embedding_ver = 2
                      AND w.project_key = ANY($3::text[])
                    ORDER BY m.embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2, _CEO_ORCHESTRATOR_PROJECTS,
                )
            elif project:
                rows = await conn.fetch(
                    """
                    SELECT m.id, m.role, m.content, m.created_at,
                           m.session_id::text AS session_id,
                           s.title AS session_name,
                           1 - (m.embedding <=> $1::vector) AS similarity
                    FROM chat_messages m
                    JOIN chat_sessions s ON s.id = m.session_id
                    JOIN chat_workspaces w ON w.id = s.workspace_id
                    WHERE m.embedding IS NOT NULL
                      AND m.embedding_ver = 2
                      AND w.project_key = $3
                    ORDER BY m.embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2, project.upper(),
                )
            else:
                # 프로젝트 미지정 시: 동일 워크스페이스 내 세션으로 제한 (전역 검색 → 세션 오염 방지)
                rows = await conn.fetch(
                    """
                    SELECT m.id, m.role, m.content, m.created_at,
                           m.session_id::text AS session_id,
                           s.title AS session_name,
                           1 - (m.embedding <=> $1::vector) AS similarity
                    FROM chat_messages m
                    JOIN chat_sessions s ON s.id = m.session_id
                    WHERE m.embedding IS NOT NULL
                      AND m.embedding_ver = 2
                      AND s.workspace_id = (
                          SELECT workspace_id FROM chat_sessions WHERE id = $3::uuid
                      )
                    ORDER BY m.embedding <=> $1::vector
                    LIMIT $2
                    """,
                    str(query_emb), _RAG_TOP_K * 2, session_id,
                )

        output = []
        for r in rows:
            sim = float(r["similarity"]) if r["similarity"] else 0
            if sim < 0.3:
                continue

            is_current = r["session_id"] == session_id
            origin = "same_session" if is_current else "cross_session"
            session_name = r["session_name"] or ""
            ts = ""
            if r["created_at"]:
                try:
                    ts = r["created_at"].strftime("%m/%d")
                except (AttributeError, TypeError):
                    ts = str(r["created_at"])[:10]

            output.append({
                "source": f"대화({session_name[:20]})" if session_name else "대화",
                "text": (r["content"] or "")[:200],
                "similarity": sim,
                "timestamp": ts,
                "origin": origin,
                "msg_id": str(r["id"]),
            })

        return output

    except Exception as e:
        logger.debug("auto_rag_messages_search_error", error=str(e))
        return []


async def _detect_reask(query_emb: Optional[list], session_id: str) -> bool:
    """A4: Detect if user is re-asking a similar question within the same session (last 30 min).
    MEDIUM-1 fix: query_emb는 사전 생성된 임베딩을 재사용 (중복 API 호출 제거).
    """
    if query_emb is None:
        return False
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
                  AND embedding_ver = 2
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
