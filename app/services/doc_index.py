"""문서 청크 임베딩·검색 — 채팅이 문서를 근거로 답하게 한다.

수집·분할은 `scripts/index_docs.py` 가 한다(모든 서버에서 도는 이식 가능한
도구). 여기는 **임베딩과 검색**만 맡는다. 둘을 나눈 이유는 임베딩 경로가
contabo116 의 aads-server 에만 있기 때문이다 — 원격 서버에 LLM 키를 복사하는
것은 R-KEY 위반이다. 원격은 텍스트만 올리고 임베딩은 여기서 채운다.

2026-09-14 배경. 대표님이 "이거 왜 이렇게 돼 있지"라고 물어도 문서가 근거로
잡히지 않았다. Auto-RAG 는 memory_facts 와 채팅 기록만 검색했고 문서 823건은
검색 대상이 아니었다. `ohvis_wiki_pages` 가 그 자리여야 했는데 2026-09-07
덤프(slug 가 전부 `memory-fact-<uuid>`) 이후 멈춰 있었다.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

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

QWEN_MODEL_ID = "qwen3-embedding:0.6b"
QWEN_DIMENSION = 1024
QWEN_INSTRUCTION_VERSION = "qwen3-doc-v2-payload4000"
QWEN_DOCUMENT_INSTRUCTION = "Represent this English document for retrieval: "
QWEN_QUERY_INSTRUCTION = "Represent this query for retrieving relevant English documents: "
QWEN_DOCUMENT_PAYLOAD_MAX_CHARS = 4000
_QWEN_MODE = os.getenv("DOC_SEARCH_MODE", "shadow").strip().lower()
_QWEN_CACHE_TTL = int(os.getenv("QWEN_QUERY_CACHE_TTL", "300"))
_QWEN_CACHE_MAX = int(os.getenv("QWEN_QUERY_CACHE_MAX", "500"))
_QWEN_TOP_N = int(os.getenv("QWEN_SEARCH_TOP_N", "30"))
_QWEN_SHADOW_TIMEOUT = float(os.getenv("QWEN_SHADOW_TIMEOUT_SECONDS", "10"))
_QWEN_SHADOW_MAX_IN_FLIGHT = max(1, int(os.getenv("QWEN_SHADOW_MAX_IN_FLIGHT", "2")))
_QWEN_SHADOW_QUEUE_MAX = max(0, int(os.getenv("QWEN_SHADOW_QUEUE_MAX", "16")))
_QWEN_URL = os.getenv("QWEN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
_query_cache: "OrderedDict[str, tuple[float, List[float]]]" = OrderedDict()
_query_cache_lock = asyncio.Lock()
_shadow_tasks: set[asyncio.Task[Any]] = set()
_shadow_semaphore = asyncio.BoundedSemaphore(_QWEN_SHADOW_MAX_IN_FLIGHT)
_shadow_reserved = 0
_shadow_metrics = {"created": 0, "completed": 0, "dropped": 0, "timeout": 0, "error": 0}


def shadow_metrics() -> Dict[str, int]:
    """Return shadow-only scheduler counters for health/structured-log consumers."""
    return dict(_shadow_metrics)


def _schedule_shadow(observe: Any) -> bool:
    """Schedule a bounded, best-effort shadow observation without delaying the API.

    Reservations include active work and queued work.  This matters because merely
    retaining tasks in a set still permits one task (and eventually one HTTP
    connection) per request while Ollama is slow or unavailable.
    """
    global _shadow_reserved
    capacity = _QWEN_SHADOW_MAX_IN_FLIGHT + _QWEN_SHADOW_QUEUE_MAX
    if _shadow_reserved >= capacity:
        _shadow_metrics["dropped"] += 1
        logger.info("doc_qwen_shadow_dropped", reason="saturated", **shadow_metrics())
        return False
    _shadow_reserved += 1
    _shadow_metrics["created"] += 1

    async def run_observation() -> None:
        global _shadow_reserved
        try:
            async with _shadow_semaphore:
                await observe()
            _shadow_metrics["completed"] += 1
            logger.info("doc_qwen_shadow_completed", **shadow_metrics())
        except asyncio.TimeoutError:
            _shadow_metrics["timeout"] += 1
            logger.warning("doc_qwen_shadow_timeout", **shadow_metrics())
        except Exception as exc:  # Shadow failures must never affect the request.
            _shadow_metrics["error"] += 1
            # ``shadow_metrics`` already exposes the numeric ``error`` counter.
            # Reusing that key for the exception text makes Python reject the
            # logger call before structlog can emit it, leaking a background-task
            # exception into the event loop. Keep the metric and message distinct.
            logger.warning(
                "doc_qwen_shadow_failed",
                exception_message=str(exc),
                **shadow_metrics(),
            )
        finally:
            _shadow_reserved -= 1

    task = asyncio.create_task(run_observation(), name="doc-qwen-shadow")
    _shadow_tasks.add(task)
    task.add_done_callback(_shadow_tasks.discard)
    logger.info("doc_qwen_shadow_created", **shadow_metrics())
    return True


def _require_loopback(url: str) -> None:
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("QWEN_OLLAMA_URL must remain loopback-only")


def normalize_query(text: str) -> str:
    return " ".join(text.strip().split())[:500]


def qwen_query_text(query: str) -> str:
    """Keep the English retrieval instruction paired with the original Korean query."""
    return QWEN_QUERY_INSTRUCTION + normalize_query(query)


async def embed_qwen_query(query: str) -> List[float]:
    """Embed a query through loopback Ollama with a bounded TTL/LRU cache."""
    import httpx

    _require_loopback(_QWEN_URL)
    key_text = normalize_query(query)
    key = f"{QWEN_MODEL_ID}|{QWEN_QUERY_INSTRUCTION}|{key_text}"
    now = time.monotonic()
    async with _query_cache_lock:
        cached = _query_cache.get(key)
        if cached and now - cached[0] <= _QWEN_CACHE_TTL:
            _query_cache.move_to_end(key)
            return list(cached[1])
        if cached:
            _query_cache.pop(key, None)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_QWEN_URL}/api/embed",
            json={"model": QWEN_MODEL_ID, "input": qwen_query_text(key_text)},
        )
        response.raise_for_status()
        vectors = response.json().get("embeddings") or []
    vector = vectors[0] if vectors else []
    if len(vector) != QWEN_DIMENSION:
        raise ValueError(f"qwen dimension mismatch: {len(vector)} != {QWEN_DIMENSION}")
    async with _query_cache_lock:
        _query_cache[key] = (now, list(vector))
        _query_cache.move_to_end(key)
        while len(_query_cache) > _QWEN_CACHE_MAX:
            _query_cache.popitem(last=False)
    logger.info("doc_qwen_query_embedding", latency_ms=round((time.monotonic()-started)*1000, 1))
    return list(vector)


async def search_docs_qwen3(
    query_embedding: List[float], *, top_k: int = 5, project: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if len(query_embedding) != QWEN_DIMENSION:
        return []
    from app.core.db_pool import get_pool
    started = time.monotonic()
    try:
        rows = await get_pool().fetch(
            """
            SELECT d.doc_path, d.server, d.project, d.title, d.heading, d.content,
                   1 - (q.embedding <=> $1::vector) AS similarity
            FROM doc_chunk_embeddings_qwen3 q
            JOIN doc_chunks d ON d.id = q.chunk_id
            WHERE q.state = 'ready' AND q.embedding IS NOT NULL
              AND q.model_id = $4 AND q.instruction_version = $5
              AND ($3::text IS NULL OR d.project = $3::text)
            ORDER BY q.embedding <=> $1::vector LIMIT $2
            """,
            str(query_embedding), min(max(1, top_k), _QWEN_TOP_N), project,
            QWEN_MODEL_ID, QWEN_INSTRUCTION_VERSION,
        )
    except Exception as exc:
        logger.warning("doc_qwen_search_failed", error=str(exc))
        return []
    logger.info("doc_qwen_search", results=len(rows), latency_ms=round((time.monotonic()-started)*1000, 1))
    return [{"kind": "doc", **dict(r), "similarity": float(r["similarity"] or 0)} for r in rows]


def reciprocal_rank_fusion(*rankings: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
    """Merge ranks, never raw scores from incompatible vector spaces."""
    fused: Dict[tuple, Dict[str, Any]] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            key = (item.get("doc_path"), item.get("heading"), item.get("content"))
            entry = fused.setdefault(key, {**item, "rrf_score": 0.0, "fusion_hits": 0})
            entry["rrf_score"] += 1.0 / (60 + rank)
            entry["fusion_hits"] += 1
    ordered = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)[:top_k]
    for rank, item in enumerate(ordered, 1):
        item["fusion_rank"] = rank
    return ordered


def needs_translation_expansion(results: List[Dict[str, Any]], requested: int = 5) -> bool:
    """Signal only; callers may attach a translator/reranker without a paid API here."""
    if len(results) < requested:
        return True
    scores = [float(r.get("similarity", 0)) for r in results[:2]]
    return not scores or scores[0] < 0.40 or (len(scores) > 1 and scores[0] - scores[1] < 0.015)


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


async def search_docs_legacy(
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


async def search_docs(
    query_embedding: List[float], *, top_k: int = 5,
    project: Optional[str] = None, query_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Route modes without adding work from a mode the caller did not select."""
    limit = max(1, top_k)
    mode = _QWEN_MODE if _QWEN_MODE in {"legacy", "shadow", "qwen3", "hybrid"} else "shadow"

    async def legacy() -> List[Dict[str, Any]]:
        return await search_docs_legacy(query_embedding, top_k=limit, project=project)

    async def qwen() -> List[Dict[str, Any]]:
        if not query_text:
            return []
        vector = await embed_qwen_query(query_text)
        return await search_docs_qwen3(vector, top_k=limit, project=project)

    async def observe_shadow() -> None:
        rows = await asyncio.wait_for(qwen(), timeout=_QWEN_SHADOW_TIMEOUT)
        logger.info("doc_qwen_shadow", qwen3=len(rows))

    if mode == "legacy":
        return (await legacy())[:limit]
    if mode == "shadow":
        rows = await legacy()
        if query_text:
            _schedule_shadow(observe_shadow)
        return rows[:limit]
    if mode == "hybrid":
        legacy_result, qwen_result = await asyncio.gather(
            legacy(), qwen(), return_exceptions=True,
        )
        legacy_rows = legacy_result if isinstance(legacy_result, list) else []
        qwen_rows = qwen_result if isinstance(qwen_result, list) else []
        if not qwen_rows:
            return legacy_rows[:limit]
        if not legacy_rows:
            return qwen_rows[:limit]
        return reciprocal_rank_fusion(legacy_rows, qwen_rows, top_k=limit)
    try:
        qwen_rows = await qwen()
    except Exception as exc:
        logger.warning("doc_qwen_fallback", mode=mode, error=str(exc))
        return (await legacy())[:limit]
    if not qwen_rows:
        return (await legacy())[:limit]
    return qwen_rows[:limit]


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
