"""
AsyncPostgresSaver 초기화.
langgraph-checkpoint-postgres 3.0.4 필수 요구사항:
  - psycopg 3.x (psycopg[binary])
  - autocommit=True (setup() 커밋 보장)
  - row_factory=dict_row (딕셔너리 접근 보장)
  - 첫 실행시 .setup() 호출
  - Supabase 직접 연결 port 5432 (R-011)
"""
from contextlib import asynccontextmanager
import structlog

logger = structlog.get_logger()


@asynccontextmanager
async def get_checkpointer():
    """
    AsyncPostgresSaver를 connection string으로 생성.
    3.0.4에서는 from_conn_string이 내부적으로
    autocommit=True, row_factory=dict_row를 설정함.
    """
    from app.config import settings

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(
            settings.SUPABASE_DIRECT_URL
        ) as checkpointer:
            # 첫 실행 시 테이블 생성 (멱등)
            await checkpointer.setup()
            logger.info(
                "checkpointer_ready",
                url_masked=settings.SUPABASE_DIRECT_URL[:30] + "...",
            )
            yield checkpointer
    except Exception as e:
        logger.error("checkpointer_failed", error=str(e))
        # Graceful Degradation: InMemory 폴백
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning("falling_back_to_memory_saver")
        yield MemorySaver()
