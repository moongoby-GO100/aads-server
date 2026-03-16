"""
Semantic Response Cache — 유사 질문에 대한 즉시 응답 캐시.

Gemini 임베딩으로 쿼리를 벡터화하고, pgvector <=> 연산자로 코사인 유사도가
임계값 이상이면 캐시된 응답을 반환. ai_meta_memory 테이블(category='semantic_cache')에 저장.
H-13: pgvector 서버사이드 유사도 검색으로 O(1) 조회.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

from app.services.chat_embedding_service import embed_texts

logger = structlog.get_logger(__name__)

_DEFAULT_SIMILARITY_THRESHOLD = 0.92
_DEFAULT_TTL_HOURS = 24
_DEFAULT_MAX_ENTRIES = 500
_DEFAULT_MIN_QUALITY = 0.5


def _embedding_hash(embedding: List[float]) -> str:
    """임베딩 벡터의 해시 (첫 16자 hex)."""
    raw = ",".join(f"{v:.6f}" for v in embedding[:32])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SemanticCache:
    """시맨틱 응답 캐시 — 유사 질문 즉시 응답."""

    def __init__(
        self,
        pool: Any,
        similarity_threshold: float | None = None,
        ttl_hours: int | None = None,
        max_entries: int | None = None,
    ):
        self.pool = pool
        self.similarity_threshold = similarity_threshold or float(
            os.getenv("SEMANTIC_CACHE_SIMILARITY", str(_DEFAULT_SIMILARITY_THRESHOLD))
        )
        self.ttl_hours = ttl_hours or int(
            os.getenv("SEMANTIC_CACHE_TTL_HOURS", str(_DEFAULT_TTL_HOURS))
        )
        self.max_entries = max_entries or int(
            os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", str(_DEFAULT_MAX_ENTRIES))
        )
        self.min_quality = float(
            os.getenv("SEMANTIC_CACHE_MIN_QUALITY", str(_DEFAULT_MIN_QUALITY))
        )
        self._hits = 0
        self._misses = 0
        # cosine distance threshold = 1 - similarity
        self._distance_threshold = 1.0 - self.similarity_threshold

    async def lookup(
        self, query: str, workspace_id: str | None = None
    ) -> Optional[Dict]:
        """
        캐시 조회 — pgvector <=> 연산자로 유사 쿼리 서버사이드 검색.

        Returns:
            {"cached_response": str, "original_query": str,
             "similarity": float, "age_minutes": int}  또는 None
        """
        if not query or len(query.strip()) < 5:
            return None

        try:
            embeddings = await embed_texts([query[:2000]])
            if not embeddings:
                self._misses += 1
                return None
            query_emb = embeddings[0]
        except Exception as e:
            logger.warning("semantic_cache.lookup.embed_fail", error=str(e))
            self._misses += 1
            return None

        try:
            async with self.pool.acquire() as conn:
                # pgvector HNSW index로 O(1) 조회
                if workspace_id:
                    row = await conn.fetchrow(
                        """
                        SELECT key, value, updated_at,
                               (embedding <=> $1::vector) AS distance
                        FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND embedding IS NOT NULL
                          AND (value->>'workspace_id' IS NULL
                               OR value->>'workspace_id' = $3)
                          AND (embedding <=> $1::vector) <= $2
                        ORDER BY (embedding <=> $1::vector) ASC
                        LIMIT 1
                        """,
                        str(query_emb), self._distance_threshold, workspace_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT key, value, updated_at,
                               (embedding <=> $1::vector) AS distance
                        FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND embedding IS NOT NULL
                          AND (embedding <=> $1::vector) <= $2
                        ORDER BY (embedding <=> $1::vector) ASC
                        LIMIT 1
                        """,
                        str(query_emb), self._distance_threshold,
                    )
        except Exception as e:
            logger.warning("semantic_cache.lookup.db_fail", error=str(e))
            self._misses += 1
            return None

        if not row:
            self._misses += 1
            return None

        val = row["value"]
        if not isinstance(val, dict):
            self._misses += 1
            return None

        # TTL 체크
        expires_at_str = val.get("expires_at")
        if expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at:
                    self._misses += 1
                    return None
            except (ValueError, TypeError):
                pass

        similarity = round(1.0 - float(row["distance"]), 4)
        now = datetime.now(timezone.utc)
        created_str = val.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(created_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_minutes = int((now - created_at).total_seconds() / 60)
        except (ValueError, TypeError):
            age_minutes = 0

        self._hits += 1
        result = {
            "cached_response": val.get("response", ""),
            "original_query": val.get("query", ""),
            "similarity": similarity,
            "age_minutes": age_minutes,
        }
        logger.info(
            "semantic_cache.hit",
            similarity=similarity,
            age_minutes=age_minutes,
            query_preview=query[:60],
        )
        return result

    async def store(
        self,
        query: str,
        response: str,
        quality_score: float,
        workspace_id: str | None = None,
        tools_used: List[str] | None = None,
    ) -> bool:
        """응답 캐시 저장. quality_score >= min_quality 인 고품질 응답 저장."""
        if quality_score < self.min_quality:
            return False
        if not query or not response:
            return False

        try:
            embeddings = await embed_texts([query[:2000]])
            if not embeddings:
                return False
            query_emb = embeddings[0]
        except Exception as e:
            logger.warning("semantic_cache.store.embed_fail", error=str(e))
            return False

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.ttl_hours)
        cache_key = f"sc_{_embedding_hash(query_emb)}"

        value = {
            "query": query[:2000],
            "response": response[:10000],
            "quality_score": quality_score,
            "workspace_id": workspace_id,
            "tools_used": tools_used,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ai_meta_memory
                      (category, key, value, embedding, confidence, last_used_at, updated_at)
                    VALUES ('semantic_cache', $1, $2::jsonb, $3::vector, $4, NOW(), NOW())
                    ON CONFLICT (project, category, key) DO UPDATE SET
                        value = $2::jsonb,
                        embedding = $3::vector,
                        confidence = $4,
                        last_used_at = NOW(),
                        updated_at = NOW()
                    """,
                    cache_key,
                    json.dumps(value, ensure_ascii=False),
                    str(query_emb),
                    quality_score,
                )

                # max_entries 초과 시 오래된 항목 삭제
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM ai_meta_memory WHERE category = 'semantic_cache'"
                )
                if count and count > self.max_entries:
                    await conn.execute(
                        """
                        DELETE FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND id IN (
                            SELECT id FROM ai_meta_memory
                            WHERE category = 'semantic_cache'
                            ORDER BY updated_at ASC
                            LIMIT $1
                          )
                        """,
                        count - self.max_entries,
                    )

            logger.info(
                "semantic_cache.stored",
                cache_key=cache_key,
                quality=quality_score,
                tools=tools_used,
                query_preview=query[:60],
            )
            return True
        except Exception as e:
            logger.warning("semantic_cache.store.db_fail", error=str(e))
            return False

    async def invalidate(
        self,
        workspace_id: str | None = None,
        older_than_hours: int | None = None,
    ) -> int:
        """캐시 무효화 — 만료 항목 또는 특정 workspace 항목 삭제."""
        try:
            async with self.pool.acquire() as conn:
                if workspace_id and older_than_hours:
                    result = await conn.execute(
                        """
                        DELETE FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND value->>'workspace_id' = $1
                          AND updated_at < NOW() - ($2 || ' hours')::interval
                        """,
                        workspace_id, str(older_than_hours),
                    )
                elif workspace_id:
                    result = await conn.execute(
                        """
                        DELETE FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND value->>'workspace_id' = $1
                        """,
                        workspace_id,
                    )
                elif older_than_hours:
                    result = await conn.execute(
                        """
                        DELETE FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND updated_at < NOW() - ($1 || ' hours')::interval
                        """,
                        str(older_than_hours),
                    )
                else:
                    result = await conn.execute(
                        """
                        DELETE FROM ai_meta_memory
                        WHERE category = 'semantic_cache'
                          AND (value->>'expires_at')::timestamptz < NOW()
                        """
                    )

                deleted = int(result.split()[-1]) if result else 0
                logger.info(
                    "semantic_cache.invalidated",
                    deleted=deleted,
                    workspace_id=workspace_id,
                    older_than_hours=older_than_hours,
                )
                return deleted
        except Exception as e:
            logger.warning("semantic_cache.invalidate.fail", error=str(e))
            return 0

    async def stats(self) -> Dict:
        """캐시 통계 반환."""
        try:
            async with self.pool.acquire() as conn:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM ai_meta_memory WHERE category = 'semantic_cache'"
                )
                avg_quality = await conn.fetchval(
                    "SELECT AVG(confidence) FROM ai_meta_memory WHERE category = 'semantic_cache'"
                )
                workspace_counts = await conn.fetch(
                    """
                    SELECT value->>'workspace_id' AS ws, COUNT(*) AS cnt
                    FROM ai_meta_memory
                    WHERE category = 'semantic_cache'
                    GROUP BY value->>'workspace_id'
                    """
                )

            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0

            return {
                "total_entries": total or 0,
                "hit_rate_estimate": round(hit_rate, 1),
                "hits": self._hits,
                "misses": self._misses,
                "avg_quality": round(float(avg_quality), 3) if avg_quality else 0.0,
                "entries_by_workspace": {
                    (r["ws"] or "global"): r["cnt"] for r in workspace_counts
                },
            }
        except Exception as e:
            logger.warning("semantic_cache.stats.fail", error=str(e))
            return {
                "total_entries": 0,
                "hit_rate_estimate": 0.0,
                "hits": self._hits,
                "misses": self._misses,
                "avg_quality": 0.0,
                "entries_by_workspace": {},
            }


# ── Module-level Singleton ────────────────────────────────────────────

_singleton_cache: Optional[SemanticCache] = None


def _get_singleton(pool: Any) -> SemanticCache:
    global _singleton_cache
    if _singleton_cache is None or _singleton_cache.pool is not pool:
        _singleton_cache = SemanticCache(pool)
    return _singleton_cache


# ── Convenience Functions ────────────────────────────────────────────


async def get_or_none(
    pool: Any, query: str, workspace_id: str | None = None
) -> Optional[str]:
    """Quick lookup — returns cached response text or None."""
    cache = _get_singleton(pool)
    result = await cache.lookup(query, workspace_id=workspace_id)
    if result:
        return result["cached_response"]
    return None


async def cache_if_worthy(
    pool: Any,
    query: str,
    response: str,
    quality_score: float,
    workspace_id: str | None = None,
    tools_used: List[str] | None = None,
) -> None:
    """Store response if it meets quality threshold."""
    cache = _get_singleton(pool)
    await cache.store(
        query=query,
        response=response,
        quality_score=quality_score,
        workspace_id=workspace_id,
        tools_used=tools_used,
    )
