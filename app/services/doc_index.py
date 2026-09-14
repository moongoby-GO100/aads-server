"""문서 청크 임베딩·검색 — 채팅이 문서를 근거로 답하게 한다.

수집·분할은 `scripts/index_docs.py` 가 한다(모든 서버에서 도는 이식 가능한
도구). 여기는 **임베딩과 검색**만 맡는다. 둘을 나눈 이유는 임베딩 경로가
contabo116 의 aads-server 에만 있기 때문이다 — 원격 서버에 LLM 키를 복사하는
것은 R-KEY 위반이다. 원격은 텍스트만 올리고 임베딩은 여기서 채운다.

2026-09-14 배경. 대표가 "이거 왜 이렇게 돼 있지"라고 물어도 문서가 근거로
잡히지 않았다. Auto-RAG 는 memory_facts 와 채팅 기록만 검색했고 문서 823건은
검색 대상이 아니었다. `ohvis_wiki_pages` 가 그 자리여야 했는데 2026-09-07
덤프(slug 가 전부 `memory-fact-<uuid>`) 이후 멈춰 있었다.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# CPU Ollama 는 1,400자 한 건에 약 6초다. 20건을 한 요청에 넣으면
# 2분이 걸려 어떤 타임아웃에도 걸린다. 4건이 실측상 가장 안정적이다.
_BACKFILL_BATCH = int(os.getenv("DOC_EMBED_BATCH", "4"))
_BACKFILL_PER_CYCLE = int(os.getenv("DOC_EMBED_PER_CYCLE", "200"))
_SEARCH_MIN_SIMILARITY = float(os.getenv("DOC_SEARCH_MIN_SIMILARITY", "0.35"))

# nomic-embed-text 의 작업 접두어. `scripts/index_docs.py` 의 DOC_PREFIX 와
# **짝이 맞아야 한다** — 근거와 실측은 그 파일 주석에 있다.
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


async def embed_query(text: str) -> List[float]:
    """질문을 검색용 벡터로 바꾼다. 접두어를 여기서만 붙인다.

    호출자가 각자 붙이면 한 곳이 빠졌을 때 그 경로만 조용히 나빠진다.
    """
    from app.services.chat_embedding_service import embed_texts_strict

    vectors = await embed_texts_strict([QUERY_PREFIX + text[:500]])
    return vectors[0]


async def backfill_embeddings(limit: int = 0) -> int:
    """임베딩이 비어 있는 청크를 채운다. 채운 개수를 돌려준다.

    실패는 건너뛰고 다음 주기에 다시 잡는다 — 임베딩 공급자가 일시적으로
    죽어도(2026-09-14 현재 로컬 Ollama·PC Agent 가 죽어 Gemini 폴백으로 돈다)
    색인 전체가 멈추면 안 된다.
    """
    from app.core.db_pool import get_pool
    from app.services.chat_embedding_service import (
        EmbeddingRouteUnavailable,
        embed_texts_strict,
    )

    pool = get_pool()
    budget = limit or _BACKFILL_PER_CYCLE
    done = 0
    while done < budget:
        rows = await pool.fetch(
            """
            SELECT id, title, heading, content
            FROM doc_chunks
            WHERE embedding IS NULL
            ORDER BY indexed_at ASC
            LIMIT $1
            """,
            min(_BACKFILL_BATCH, budget - done),
        )
        if not rows:
            break
        # 제목·절 제목을 본문 앞에 붙여 임베딩한다. 본문만 넣으면 "무슨
        # 문서의 어느 절인지"가 벡터에 안 들어가서, 비슷한 문장이 여러
        # 문서에 있을 때 엉뚱한 쪽이 잡힌다.
        texts = [
            DOC_PREFIX + f"{r['title']}\n{r['heading']}\n{r['content']}"[:1600]
            for r in rows
        ]
        try:
            # strict 를 쓴다 — 더미 벡터를 저장하면 검색이 조용히 무의미해진다.
            # 2026-09-14 에 정확히 그 일이 있었다(더미 7,847청크).
            vectors = await embed_texts_strict(texts)
        except EmbeddingRouteUnavailable as exc:
            logger.warning("doc_embed_route_unavailable", error=str(exc))
            break
        except Exception as exc:
            logger.warning("doc_embed_batch_failed", error=str(exc))
            break
        if not vectors or len(vectors) != len(rows):
            logger.warning("doc_embed_count_mismatch", got=len(vectors or []), want=len(rows))
            break
        for row, vec in zip(rows, vectors):
            try:
                await pool.execute(
                    "UPDATE doc_chunks SET embedding = $1::vector WHERE id = $2",
                    str(vec), row["id"],
                )
                done += 1
            except Exception as exc:
                logger.warning("doc_embed_store_failed", chunk=str(row["id"])[:8], error=str(exc))
    if done:
        logger.info("doc_embed_backfilled", count=done)
    return done


async def search_docs(
    query_embedding: List[float],
    *,
    top_k: int = 5,
    project: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """문서 청크에서 시맨틱 검색.

    결과에 **출처 경로**를 반드시 넣는다. 근거 없이 답이 나오면 비전문가는
    그 답을 검증할 방법이 없다.
    """
    from app.core.db_pool import get_pool

    try:
        rows = await get_pool().fetch(
            """
            SELECT doc_path, server, project, title, heading, content,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM doc_chunks
            WHERE embedding IS NOT NULL
              AND ($3::text IS NULL OR project = $3::text)
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            str(query_embedding), max(1, top_k), project,
        )
    except Exception as exc:
        logger.debug("doc_search_failed", error=str(exc))
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        sim = float(r["similarity"] or 0.0)
        if sim < _SEARCH_MIN_SIMILARITY:
            continue
        out.append({
            "kind": "doc",
            "similarity": sim,
            "doc_path": r["doc_path"],
            "server": r["server"],
            "project": r["project"],
            "title": r["title"],
            "heading": r["heading"],
            "content": r["content"],
        })
    return out


async def index_status() -> Dict[str, Any]:
    """색인 현황 — 운영 확인용."""
    from app.core.db_pool import get_pool

    row = await get_pool().fetchrow(
        """
        SELECT count(*) AS chunks,
               count(embedding) AS embedded,
               count(DISTINCT doc_path) AS docs,
               count(DISTINCT server) AS servers,
               max(indexed_at) AS last_indexed
        FROM doc_chunks
        """
    )
    if not row:
        return {}
    chunks = int(row["chunks"] or 0)
    embedded = int(row["embedded"] or 0)
    return {
        "chunks": chunks,
        "embedded": embedded,
        "coverage": round(100.0 * embedded / chunks, 1) if chunks else 0.0,
        "docs": int(row["docs"] or 0),
        "servers": int(row["servers"] or 0),
        "last_indexed": row["last_indexed"].isoformat() if row["last_indexed"] else None,
    }
