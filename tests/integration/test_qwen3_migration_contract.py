"""Opt-in isolated-DB verification for the immutable Qwen3 migration sequence."""
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.mark.asyncio
async def test_qwen3_contract_migration_apply_and_rollback_on_isolated_db():
    dsn = os.getenv("QWEN_MIGRATION_TEST_DSN")
    if not dsn:
        pytest.skip("set QWEN_MIGRATION_TEST_DSN to an isolated local database")
    parsed = urlparse(dsn)
    database = parsed.path.strip("/")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not database.startswith("qwen3_"):
        pytest.fail("QWEN_MIGRATION_TEST_DSN must target an isolated local qwen3_* database")

    asyncpg = pytest.importorskip("asyncpg")
    root = Path(__file__).parents[2]
    base_sql = (root / "migrations" / "20260919_qwen3_doc_embeddings.sql").read_text()
    contract_sql = (root / "migrations" / "20260919_qwen3_doc_embeddings_contract.sql").read_text()
    rollback_sql = (
        root / "migrations" / "rollback" / "20260919_qwen3_doc_embeddings_contract.sql"
    ).read_text()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS doc_chunk_embeddings_qwen3")
        await conn.execute("DROP TABLE IF EXISTS doc_chunks")
        await conn.execute(
            "CREATE TABLE doc_chunks (id uuid PRIMARY KEY, title text, heading text, content text)"
        )
        await conn.execute(base_sql)
        await conn.execute(contract_sql)
        chunk_id = await conn.fetchval("SELECT gen_random_uuid()")
        await conn.execute("INSERT INTO doc_chunks (id) VALUES ($1)", chunk_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO doc_chunk_embeddings_qwen3 "
                "(chunk_id, model_id, dimension, instruction_version, content_sha256) "
                "VALUES ($1, 'not-qwen', 1024, 'qwen3-doc-v2-payload4000', repeat('a', 64))",
                chunk_id,
            )
        await conn.execute(rollback_sql)
        await conn.execute(
            "INSERT INTO doc_chunk_embeddings_qwen3 "
            "(chunk_id, model_id, dimension, instruction_version, content_sha256) "
            "VALUES ($1, 'not-qwen', 1024, 'qwen3-doc-v2-payload4000', repeat('a', 64))",
            chunk_id,
        )
    finally:
        await conn.execute("DROP TABLE IF EXISTS doc_chunk_embeddings_qwen3")
        await conn.execute("DROP TABLE IF EXISTS doc_chunks")
        await conn.close()
