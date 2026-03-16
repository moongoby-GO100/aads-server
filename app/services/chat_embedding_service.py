"""
채팅 메시지 임베딩 서비스 — 시맨틱 검색용
Gemini gemini-embedding-001 (768차원, output_dimensionality=768) 사용, pgvector 저장.
pgvector 0.8.2 hnsw/ivfflat 2000차원 제한으로 768차원 사용.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any, List, Optional

import structlog

logger = structlog.get_logger(__name__)

_EMBED_BATCH_SIZE = 50
_EMBED_DIM = 768


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Google Gemini gemini-embedding-001 로 텍스트 임베딩.
    API 키 없으면 hash 기반 dummy 반환.
    """
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("[ChatEmbed] GEMINI_API_KEY 없음 — 시맨틱 검색 비활성화 (dummy 임베딩)")
        return [_dummy_embedding(t) for t in texts]

    try:
        from google import genai as google_genai  # type: ignore
        client = google_genai.Client(api_key=api_key)
        loop = asyncio.get_running_loop()
        all_embeddings: List[List[float]] = []

        for i in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[i: i + _EMBED_BATCH_SIZE]

            def _call(b: List[str] = batch) -> Any:
                return client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=b,
                    config={"output_dimensionality": _EMBED_DIM},
                )

            result = await loop.run_in_executor(None, _call)
            for emb in result.embeddings:
                all_embeddings.append(list(emb.values))

        return all_embeddings
    except Exception as e:
        logger.warning(f"[ChatEmbed] Gemini 임베딩 실패: {e} — dummy 사용")
        return [_dummy_embedding(t) for t in texts]


def _dummy_embedding(text: str, dim: int = _EMBED_DIM) -> List[float]:
    """테스트/폴백용 hash 기반 dummy 임베딩."""
    h = hashlib.sha256(text.encode()).digest()
    base: List[float] = []
    for i in range(0, 32, 4):
        val = int.from_bytes(h[i: i + 4], "big")
        base.append((val / 2**32) * 2.0 - 1.0)
    return (base * (dim // len(base) + 1))[:dim]


async def embed_and_store_message(pool: Any, message_id: str, content: str) -> None:
    """단일 메시지 임베딩 생성 후 DB 저장. 실패해도 예외 전파 안 함."""
    if not content or len(content.strip()) < 10:
        return
    try:
        embeddings = await embed_texts([content[:2000]])  # 앞 2000자만
        if not embeddings:
            return
        embedding = embeddings[0]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_messages SET embedding = $1::vector WHERE id = $2",
                str(embedding), message_id,
            )
        logger.debug(f"[ChatEmbed] 메시지 {message_id[:8]}... 임베딩 저장 완료")
    except Exception as e:
        logger.warning(f"[ChatEmbed] 메시지 {message_id[:8]}... 임베딩 실패: {e}")


async def backfill_embeddings(pool: Any, batch_size: int = 20) -> str:
    """embedding이 NULL인 메시지들 일괄 임베딩 생성."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content FROM chat_messages
            WHERE embedding IS NULL AND length(content) >= 10
            ORDER BY created_at DESC
            LIMIT $1
            """,
            batch_size,
        )
    if not rows:
        return "임베딩 백필 대상 없음 (모두 완료)"

    texts = [r["content"][:2000] for r in rows]
    embeddings = await embed_texts(texts)

    updated = 0
    async with pool.acquire() as conn:
        for row, emb in zip(rows, embeddings):
            try:
                await conn.execute(
                    "UPDATE chat_messages SET embedding = $1 WHERE id = $2",
                    str(emb), row["id"],
                )
                updated += 1
            except Exception as e:
                logger.warning(f"[ChatEmbed] 백필 실패 {row['id']}: {e}")

    return f"임베딩 백필 완료: {updated}/{len(rows)}건 처리"


async def search_semantic(pool: Any, query: Any, session_id: Optional[str] = None,
                          limit: int = 10, pre_embedded: bool = False) -> List[dict]:
    """시맨틱 검색 — 쿼리 임베딩 → pgvector 코사인 유사도.
    pre_embedded=True이면 query를 이미 생성된 임베딩 벡터로 사용 (중복 API 호출 방지).
    """
    if pre_embedded:
        query_emb = query
    else:
        embeddings = await embed_texts([query])
        if not embeddings:
            return []
        query_emb = embeddings[0]

    session_filter = ""
    params: list = [str(query_emb), limit]
    if session_id:
        session_filter = "AND m.session_id = $3::uuid"
        params.append(session_id)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT m.id, m.role, m.content, m.created_at,
                   s.title AS session_name,
                   1 - (m.embedding <=> $1::vector) AS similarity
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.embedding IS NOT NULL
              {session_filter}
            ORDER BY m.embedding <=> $1::vector
            LIMIT $2
            """,
            *params,
        )
    return [
        {
            "id": str(r["id"]),
            "role": r["role"],
            "content": r["content"][:500],
            "created_at": r["created_at"],
            "session_name": r["session_name"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
