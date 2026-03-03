"""
AsyncPostgresSaver 초기화.
langgraph-checkpoint-postgres 3.0.4 필수 요구사항:
  - psycopg 3.x (psycopg[binary])
  - autocommit=True (setup() 커밋 보장)
  - row_factory=dict_row (딕셔너리 접근 보장)
  - 첫 실행시 .setup() 호출
  - 로컬 postgres (Docker 내부 DNS: postgres:5432) 우선 (R-011 수정)
  - MemorySaver fallback 유지, WARNING 로그 출력
"""
from contextlib import asynccontextmanager
import structlog

logger = structlog.get_logger()


@asynccontextmanager
async def get_checkpointer():
    """
    AsyncPostgresSaver를 connection string으로 생성.
    우선순위: DATABASE_URL(로컬 postgres) > SUPABASE_DIRECT_URL > MemorySaver fallback
    """
    from app.config import settings
    import os

    # 로컬 postgres(Docker 내부 DNS) 우선, 없으면 Supabase
    db_url = os.getenv("DATABASE_URL") or settings.SUPABASE_DIRECT_URL

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(db_url) as checkpointer:
            # 첫 실행 시 테이블 생성 (멱등)
            await checkpointer.setup()
            logger.info(
                "checkpointer_ready",
                db_host=db_url.split("@")[-1].split("/")[0] if "@" in db_url else "local",
                backend="postgres",
            )
            yield checkpointer
    except Exception as e:
        logger.error("checkpointer_failed", error=str(e), db_url_prefix=db_url[:40])
        # Graceful Degradation: InMemory 폴백 (비영구적 — 재시작 시 체크포인트 소실)
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "falling_back_to_memory_saver",
            reason=str(e),
            impact="체크포인트 비영구적 — 서버 재시작 시 프로젝트 상태 소실",
        )
        yield MemorySaver()

