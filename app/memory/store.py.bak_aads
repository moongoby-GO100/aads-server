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

    async def get_system_by_category(self, category: str, limit: int = 1000) -> List[Dict]:
        """카테고리별 시스템 메모리 조회. 기본 LIMIT 1000 (conversation:kis 19,420건 방지)"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, version, updated_at FROM system_memory WHERE category=$1 ORDER BY key LIMIT $2",
                category, limit
            )
            return [dict(r) for r in rows]

    async def get_all_system(self, exclude_conversation: bool = True) -> Dict[str, List[Dict]]:
        """전체 시스템 메모리 조회. 기본값으로 conversation:* 카테고리 제외 (41,000건 이상, 99.6%)"""
        async with self.pool.acquire() as conn:
            if exclude_conversation:
                rows = await conn.fetch(
                    "SELECT category, key, value, version, updated_at FROM system_memory "
                    "WHERE category NOT LIKE 'conversation:%' ORDER BY category, key"
                )
            else:
                rows = await conn.fetch(
                    "SELECT category, key, value, version, updated_at FROM system_memory ORDER BY category, key"
                )
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
            # title 컬럼이 NOT NULL인 DB 호환: content에서 title 추출 또는 자동 생성
            title = content.get('title', f'{experience_type}:{domain or "general"}')
            if embedding:
                await conn.execute("""
                    INSERT INTO experience_memory (experience_type, domain, tags, content, embedding, title)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                """, experience_type, domain, tags, json.dumps(content), embedding, title)
            else:
                await conn.execute("""
                    INSERT INTO experience_memory (experience_type, domain, tags, content, title)
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                """, experience_type, domain, tags, json.dumps(content), title)

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
            # DB에 procedure_name/steps NOT NULL 컬럼이 있을 경우 자동 채움
            procedure_name = f'{agent_name}:{procedure_type}'
            await conn.execute("""
                INSERT INTO procedural_memory (agent_name, procedure_type, content, procedure_name, steps)
                VALUES ($1, $2, $3::jsonb, $4, $3::jsonb)
                ON CONFLICT DO NOTHING
            """, agent_name, procedure_type, json.dumps(content), procedure_name)

    async def get_procedures(self, agent_name: str) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM procedural_memory WHERE agent_name=$1 ORDER BY success_rate DESC",
                agent_name
            )
            return [dict(r) for r in rows]

# 싱글톤 인스턴스
memory_store = AADSMemoryStore()
