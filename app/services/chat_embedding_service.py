"""
채팅 메시지 임베딩 서비스 — 시맨틱 검색용.

DB route_key='embedding' 기준으로 로컬 PC Agent/Ollama를 우선 사용하고,
외부 provider가 활성화된 경우에만 fallback한다. 저장 차원은 pgvector 계약에
맞춰 768차원으로 고정한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import OrderedDict
from typing import Any, List, Optional

import structlog

from app.services.ai_route_resolver import (
    GOOGLE_PROVIDERS,
    get_route_candidates,
    normalize_embedding_dimension,
)

logger = structlog.get_logger(__name__)

_EMBED_BATCH_SIZE = 50
_EMBED_DIM = 768

# ── 인메모리 임베딩 캐시 (TTL 300초, 최대 500항목) ──
_EMBED_CACHE_TTL = int(os.getenv("EMBED_CACHE_TTL", "300"))
_EMBED_CACHE_MAX = int(os.getenv("EMBED_CACHE_MAX", "500"))

# ── AADS P0(2026-07-26): Gemini 임베딩 429 차단기 ──
# 크레딧 고갈(RESOURCE_EXHAUSTED) 감지 시 일정 시간 호출을 끊어
# 2분간 32회식 재시도 폭주와 로그 오염을 방지한다. 충전 후 자동 복구.
_EMBED_GEMINI_ENABLED = os.getenv("EMBED_GEMINI_ENABLED", "0").strip().lower() in ("1", "true", "yes")
_GEMINI_BLOCK_SECONDS = int(os.getenv("EMBED_GEMINI_BLOCK_SECONDS", "3600"))
_gemini_blocked_until: float = 0.0
_PROVIDER_BLOCK_SECONDS = int(os.getenv("EMBED_PROVIDER_BLOCK_SECONDS", "300"))
_provider_blocked_until: dict[tuple[str, str], float] = {}

class _EmbedCache:
    """TTL + LRU 인메모리 임베딩 캐시."""
    def __init__(self, ttl: int, maxsize: int):
        self._ttl = ttl
        self._maxsize = maxsize
        self._cache: OrderedDict[str, tuple[float, List[float]]] = OrderedDict()

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def get(self, text: str) -> List[float] | None:
        k = self._key(text)
        if k not in self._cache:
            return None
        ts, vec = self._cache[k]
        if time.monotonic() - ts > self._ttl:
            del self._cache[k]
            return None
        self._cache.move_to_end(k)
        return vec

    def set(self, text: str, vec: List[float]) -> None:
        k = self._key(text)
        self._cache[k] = (time.monotonic(), vec)
        self._cache.move_to_end(k)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

_embed_cache = _EmbedCache(ttl=_EMBED_CACHE_TTL, maxsize=_EMBED_CACHE_MAX)


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    DB 라우팅 기반 텍스트 임베딩.
    인메모리 캐시(TTL 300초, 최대 500항목) 적용 — 동일 텍스트 반복 호출 최소화.
    가용 provider가 없으면 hash 기반 dummy 반환.
    """
    # 캐시 적중 여부 확인 — 캐시 HIT 항목은 API 호출 스킵
    results: List[List[float] | None] = [_embed_cache.get(t) for t in texts]
    uncached_indices = [i for i, r in enumerate(results) if r is None]

    if not uncached_indices:
        return [r for r in results]  # type: ignore[return-value]

    uncached_texts = [texts[i] for i in uncached_indices]

    fetched = await _embed_uncached_with_routes(uncached_texts)

    # 캐시에 저장 + 결과 병합
    for idx, vec in zip(uncached_indices, fetched):
        _embed_cache.set(texts[idx], vec)
        results[idx] = vec

    return [r for r in results]  # type: ignore[return-value]


async def _embed_uncached_with_routes(texts: List[str]) -> List[List[float]]:
    candidates = await get_route_candidates("embedding")
    for candidate in candidates:
        provider = candidate.provider
        if candidate.availability not in {"available", "unknown"}:
            logger.info(
                "[ChatEmbed] embedding route skipped unavailable",
                provider=provider,
                model=candidate.runtime_model,
                availability=candidate.availability,
            )
            continue
        block_key = (provider, candidate.runtime_model)
        if time.time() < _provider_blocked_until.get(block_key, 0.0):
            continue
        if provider in {"pc_ollama", "local", "ollama"}:
            try:
                from app.core.local_embedding_bridge import embed as local_embed
                result = await local_embed(texts, model=candidate.runtime_model)
                vectors = result.get("embeddings") or []
                if len(vectors) == len(texts):
                    normalized = [normalize_embedding_dimension([float(x) for x in vec], _EMBED_DIM) for vec in vectors]
                    logger.info("[ChatEmbed] local embedding route ok", provider=provider, model=candidate.runtime_model)
                    return normalized
            except Exception as exc:
                logger.warning(
                    "[ChatEmbed] local embedding route failed",
                    provider=provider,
                    model=candidate.runtime_model,
                    error=str(exc)[:160],
                )
                _provider_blocked_until[block_key] = time.time() + _PROVIDER_BLOCK_SECONDS
                continue
        if provider == "openai":
            try:
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    continue
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=api_key)
                resp = await client.embeddings.create(model=candidate.runtime_model, input=texts)
                vectors = [item.embedding for item in resp.data]
                if len(vectors) == len(texts):
                    normalized = [normalize_embedding_dimension([float(x) for x in vec], _EMBED_DIM) for vec in vectors]
                    logger.info("[ChatEmbed] openai embedding route ok", model=candidate.runtime_model)
                    return normalized
            except Exception as exc:
                logger.warning(
                    "[ChatEmbed] openai embedding route failed",
                    model=candidate.runtime_model,
                    error=str(exc)[:160],
                )
                _provider_blocked_until[block_key] = time.time() + _PROVIDER_BLOCK_SECONDS
                continue
        if provider in GOOGLE_PROVIDERS:
            google_vectors = await _try_gemini_embeddings(texts, candidate.runtime_model)
            if google_vectors:
                return google_vectors

    logger.debug("[ChatEmbed] no embedding route available — dummy embedding")
    return [_dummy_embedding(t) for t in texts]


async def _try_gemini_embeddings(texts: List[str], model_name: str = "models/gemini-embedding-001") -> List[List[float]] | None:
    """Legacy Gemini embedding path. Only reached when DB route explicitly enables it."""
    global _gemini_blocked_until
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    now = time.monotonic()
    if not api_key or not _EMBED_GEMINI_ENABLED or now < _gemini_blocked_until:
        return None
    try:
        from google import genai as google_genai  # type: ignore
        client = google_genai.Client(api_key=api_key)
        loop = asyncio.get_running_loop()
        fetched: List[List[float]] = []

        for i in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[i: i + _EMBED_BATCH_SIZE]

            def _call(b: List[str] = batch) -> Any:
                return client.models.embed_content(
                    model=model_name if model_name.startswith("models/") else f"models/{model_name}",
                    contents=b,
                    config={"output_dimensionality": _EMBED_DIM},
                )

            result = await loop.run_in_executor(None, _call)
            for emb in result.embeddings:
                fetched.append(normalize_embedding_dimension(list(emb.values), _EMBED_DIM))
        return fetched
    except Exception as e:
        err_str = str(e)
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
            _gemini_blocked_until = now + _GEMINI_BLOCK_SECONDS
            logger.warning("[ChatEmbed] Gemini 429/크레딧 고갈 → 서킷 브레이커 발동", seconds=_GEMINI_BLOCK_SECONDS)
        else:
            logger.warning(f"[ChatEmbed] Gemini 임베딩 실패: {e}")
        return None


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


def schedule_message_embedding(pool: Any, message_id: Any, content: str) -> bool:
    """메시지 임베딩 생성을 백그라운드로 예약한다.

    메시지 저장/수정 트랜잭션은 임베딩 실패와 분리되어야 하므로 호출자는
    예약 성공 여부만 받는다. 실행 중 이벤트 루프가 없으면 False를 반환한다.
    """
    if not content or len(content.strip()) < 10:
        return False
    try:
        asyncio.create_task(embed_and_store_message(pool, str(message_id), content))
        return True
    except RuntimeError as e:
        logger.debug(f"[ChatEmbed] 임베딩 예약 실패: {e}")
        return False
    except Exception as e:
        logger.warning(f"[ChatEmbed] 임베딩 예약 오류: {e}")
        return False


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
                   m.session_id::text AS session_id,
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
            "session_id": r["session_id"],
            "role": r["role"],
            "content": r["content"][:500],
            "created_at": r["created_at"],
            "session_name": r["session_name"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
