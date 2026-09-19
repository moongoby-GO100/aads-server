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
        v1_chunk_id = await conn.fetchval("SELECT gen_random_uuid()")
        v2_chunk_id = await conn.fetchval("SELECT gen_random_uuid()")
        invalid_chunk_id = await conn.fetchval("SELECT gen_random_uuid()")
        await conn.executemany(
            "INSERT INTO doc_chunks (id, title, heading, content) VALUES ($1, $2, $3, $4)",
            [
                (v1_chunk_id, "historical", "v1", "existing v1 payload"),
                (v2_chunk_id, "current", "v2", "existing v2 payload"),
                (invalid_chunk_id, "invalid", "candidate", "new payload"),
            ],
        )
        await conn.execute(base_sql)
        await conn.execute(
            "INSERT INTO doc_chunk_embeddings_qwen3 "
            "(chunk_id, model_id, dimension, instruction_version, content_sha256) "
            "VALUES ($1, 'qwen3-embedding:0.6b', 1024, 'qwen3-doc-v1', repeat('b', 64))",
            v1_chunk_id,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM doc_chunk_embeddings_qwen3 "
            "WHERE model_id = 'qwen3-embedding:0.6b' AND dimension = 1024 "
            "AND instruction_version IN ('qwen3-doc-v1', 'qwen3-doc-v2-payload4000')"
        ) == 4
        await conn.execute(contract_sql)
        assert await conn.fetchval(
            "SELECT NOT convalidated FROM pg_constraint "
            "WHERE conname = 'doc_chunk_embeddings_qwen3_contract_ck' "
            "AND conrelid = 'doc_chunk_embeddings_qwen3'::regclass"
        ) is True
        assert await conn.fetchval(
            "SELECT count(*) FROM doc_chunk_embeddings_qwen3 "
            "WHERE instruction_version = 'qwen3-doc-v1'"
        ) == 1
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO doc_chunk_embeddings_qwen3 "
                "(chunk_id, model_id, dimension, instruction_version, content_sha256) "
                "VALUES ($1, 'not-qwen', 1024, 'qwen3-doc-v2-payload4000', repeat('a', 64))",
                invalid_chunk_id,
            )
        await conn.execute(rollback_sql)
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'doc_chunk_embeddings_qwen3_contract_ck' "
            "AND conrelid = 'doc_chunk_embeddings_qwen3'::regclass"
        ) == 0
        await conn.execute(
            "INSERT INTO doc_chunk_embeddings_qwen3 "
            "(chunk_id, model_id, dimension, instruction_version, content_sha256) "
            "VALUES ($1, 'not-qwen', 1024, 'qwen3-doc-v2-payload4000', repeat('a', 64))",
            invalid_chunk_id,
        )
    finally:
        await conn.execute("DROP TABLE IF EXISTS doc_chunk_embeddings_qwen3")
        await conn.execute("DROP TABLE IF EXISTS doc_chunks")
        await conn.close()
