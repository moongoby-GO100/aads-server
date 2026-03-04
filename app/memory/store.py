"""
AADS Memory Store - LangGraph PostgresStore + pgvector
5-Layer Memory Architecture:
  L1: Working Memory (AADSState + AsyncPostgresSaver) - 기존
  L2: Project Memory (project_memory 테이블)
  L3: Experience Memory (experience_memory 테이블)
  L4: System Memory (system_memory 테이블) - HANDOVER 대체
  L5: Procedural Memory (procedural_memory 테이블)
"""
import asyncpg
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from app.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()

class AADSMemoryStore:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def initialize(self):
        """DB 커넥션 풀 생성"""
        try:
            self.pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=2,
                max_size=settings.MAX_DB_CONNECTIONS
            )
            logger.info("AADSMemoryStore initialized successfully")
        except Exception as e:
            logger.error(f"AADSMemoryStore init failed: {e}")
            raise

    async def close(self):
        if self.pool:
            await self.pool.close()

    # === L4: System Memory (HANDOVER 대체) ===
    async def get_system(self, category: str, key: str) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value, version, updated_at FROM system_memory WHERE category=$1 AND key=$2",
                category, key
            )
            return dict(row) if row else None

    async def put_system(self, category: str, key: str, value: Dict, version: str = None, updated_by: str = "system"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO system_memory (category, key, value, version, updated_by, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (category, key) DO UPDATE
                SET value=$3, version=$4, updated_by=$5, updated_at=NOW()
            """, category, key, json.dumps(value), version, updated_by)

    async def get_system_by_category(self, category: str) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, version, updated_at FROM system_memory WHERE category=$1 ORDER BY key",
                category
            )
            return [dict(r) for r in rows]

    async def get_all_system(self) -> Dict[str, List[Dict]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT category, key, value, version, updated_at FROM system_memory ORDER BY category, key")
            result = {}
            for r in rows:
                cat = r['category']
                if cat not in result:
                    result[cat] = []
                result[cat].append(dict(r))
            return result

    # === L2: Project Memory ===
    async def store_project_memory(self, project_id: str, memory_type: str, content: Dict, embedding: List[float] = None):
        async with self.pool.acquire() as conn:
            if embedding:
                await conn.execute("""
                    INSERT INTO project_memory (project_id, memory_type, content, embedding)
                    VALUES ($1, $2, $3, $4)
                """, project_id, memory_type, json.dumps(content), embedding)
            else:
                await conn.execute("""
                    INSERT INTO project_memory (project_id, memory_type, content)
                    VALUES ($1, $2, $3)
                """, project_id, memory_type, json.dumps(content))

    async def get_project_memories(self, project_id: str, memory_type: str = None) -> List[Dict]:
        async with self.pool.acquire() as conn:
            if memory_type:
                rows = await conn.fetch(
                    "SELECT * FROM project_memory WHERE project_id=$1 AND memory_type=$2 ORDER BY created_at",
                    project_id, memory_type
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM project_memory WHERE project_id=$1 ORDER BY created_at",
                    project_id
                )
            return [dict(r) for r in rows]

    # === L3: Experience Memory ===
    async def store_experience(self, experience_type: str, domain: str, tags: List[str], content: Dict, embedding: List[float] = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO experience_memory (experience_type, domain, tags, content, embedding)
                VALUES ($1, $2, $3, $4, $5)
            """, experience_type, domain, tags, json.dumps(content), embedding)

    async def search_experience_by_embedding(self, embedding: List[float], limit: int = 5, experience_type: str = None) -> List[Dict]:
        async with self.pool.acquire() as conn:
            if experience_type:
                rows = await conn.fetch("""
                    SELECT *, embedding <=> $1::vector AS distance
                    FROM experience_memory
                    WHERE experience_type = $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                """, str(embedding), experience_type, limit)
            else:
                rows = await conn.fetch("""
                    SELECT *, embedding <=> $1::vector AS distance
                    FROM experience_memory
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                """, str(embedding), limit)
            # 접근 카운트 업데이트
            for r in rows:
                await conn.execute(
                    "UPDATE experience_memory SET access_count=access_count+1, last_accessed=NOW() WHERE id=$1",
                    r['id']
                )
            return [dict(r) for r in rows]

    # === L5: Procedural Memory ===
    async def store_procedure(self, agent_name: str, procedure_type: str, content: Dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO procedural_memory (agent_name, procedure_type, content)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
            """, agent_name, procedure_type, json.dumps(content))

    async def get_procedures(self, agent_name: str) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM procedural_memory WHERE agent_name=$1 ORDER BY success_rate DESC",
                agent_name
            )
            return [dict(r) for r in rows]

# 싱글톤 인스턴스
memory_store = AADSMemoryStore()
