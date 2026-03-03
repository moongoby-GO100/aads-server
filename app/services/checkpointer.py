"""
AsyncPostgresSaver 초기화.
연결 우선순위:
  1) DATABASE_URL (로컬 Docker postgres — IPv4/Docker DNS)
  2) SUPABASE_DIRECT_URL (Supabase port 5432, R-011)
  3) MemorySaver fallback (WARNING 로그 필수)

langgraph-checkpoint-postgres 3.0.4 필수 요구사항:
  - psycopg 3.x (psycopg[binary])
  - autocommit=True, row_factory=dict_row (from_conn_string 내부 처리)
  - 첫 실행 시 .setup() 호출 (멱등)
⚠️ IPv6 주소 사용 금지 — Docker 내부 DNS(postgres) 또는 IPv4만 허용
"""
from contextlib import asynccontextmanager
import structlog

logger = structlog.get_logger()


async def _try_postgres(conn_string: str, label: str):
    """PostgreSQL 연결 시도 헬퍼. 실패 시 None 반환."""
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        # IPv6 주소 패턴 감지 및 차단 (Docker DNS 사용 권장)
        if "::1" in conn_string or "%3A%3A" in conn_string:
            logger.warning("checkpointer_ipv6_blocked", label=label)
            return None
        checkpointer = AsyncPostgresSaver.from_conn_string(conn_string)
        return checkpointer
    except Exception as e:
        logger.warning("checkpointer_postgres_unavailable", label=label, error=str(e))
        return None


@asynccontextmanager
async def get_checkpointer():
    """
    PostgreSQL (로컬 → Supabase) 우선, 실패 시 MemorySaver fallback.
    연결 성공 시 setup() 호출하여 체크포인트 테이블 생성.
    """
    from app.config import settings
    import os

    # 로컬 postgres(Docker 내부 DNS) 우선, 없으면 Supabase
    db_url = os.getenv("DATABASE_URL") or settings.SUPABASE_DIRECT_URL

    # 1순위: 로컬 PostgreSQL (DATABASE_URL)
    local_url = getattr(settings, "DATABASE_URL", None)
    if local_url and local_url.startswith("postgresql"):
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            async with AsyncPostgresSaver.from_conn_string(local_url) as checkpointer:
                await checkpointer.setup()
                logger.info("checkpointer_ready", source="local_postgres", url_masked=local_url[:40] + "...")
                yield checkpointer
                return
        except Exception as e:
            logger.warning("checkpointer_local_postgres_failed", error=str(e))

    # 2순위: Supabase (SUPABASE_DIRECT_URL, R-011)
    supabase_url = getattr(settings, "SUPABASE_DIRECT_URL", None)
    if supabase_url and supabase_url.startswith("postgresql"):
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            async with AsyncPostgresSaver.from_conn_string(supabase_url) as checkpointer:
                await checkpointer.setup()
                logger.info("checkpointer_ready", source="supabase", url_masked=supabase_url[:40] + "...")
                yield checkpointer
                return
        except Exception as e:
            logger.warning("checkpointer_supabase_failed", error=str(e))

    # 3순위: MemorySaver (Graceful Degradation)
    from langgraph.checkpoint.memory import MemorySaver
    logger.warning(
        "checkpointer_fallback_memory_saver",
        reason="All PostgreSQL connections failed",
        impact="Checkpoints not persisted — restart loses state",
    )
    yield MemorySaver()
