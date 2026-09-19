import asyncio
import inspect

import pytest

from app.services import doc_index


def test_qwen_contract_and_instruction_pairing():
    assert doc_index.QWEN_DIMENSION == 1024
    assert doc_index.QWEN_MODEL_ID == "qwen3-embedding:0.6b"
    assert doc_index.QWEN_INSTRUCTION_VERSION == "qwen3-doc-v2-payload4000"
    assert doc_index.QWEN_QUERY_INSTRUCTION.startswith("Represent this query")
    assert "한국어 질문" in doc_index.qwen_query_text("한국어 질문")
    assert inspect.signature(doc_index.search_docs).parameters["top_k"].default == 5


def test_rrf_merges_ranks_not_vector_scores():
    legacy = [{"doc_path": "a", "heading": "h", "content": "a", "similarity": .99}]
    qwen = [
        {"doc_path": "b", "heading": "h", "content": "b", "similarity": .1},
        {"doc_path": "a", "heading": "h", "content": "a", "similarity": .2},
    ]
    out = doc_index.reciprocal_rank_fusion(legacy, qwen, top_k=5)
    assert out[0]["doc_path"] == "a"
    assert "rrf_score" in out[0]
    assert out[0]["fusion_hits"] == 2
    assert out[0]["fusion_rank"] == 1


def test_translation_expansion_signals_are_local_only():
    assert doc_index.needs_translation_expansion([], 5)
    assert doc_index.needs_translation_expansion([
        {"similarity": .51}, {"similarity": .50}, {"similarity": .49},
        {"similarity": .48}, {"similarity": .47},
    ])


def test_worker_has_atomic_claim_lease_recovery_and_throttle():
    from scripts import qwen3_embedding_worker as worker
    src = inspect.getsource(worker.claim)
    assert "FOR UPDATE SKIP LOCKED" in src
    assert "lease_expires_at < now()" in src
    assert worker.desired_concurrency(20, 8, 8000, 1) == 0
    assert worker.desired_concurrency(1, 8, 8000, 1) > 0
    assert worker.bounded_concurrency(0, 3) == 1
    assert worker.bounded_concurrency(3, 0) == 0
    assert not worker.allowed_host("contabo14")
    worker.require_worker_site("cafe24_114")
    with pytest.raises(SystemExit):
        worker.require_worker_site("")
    with pytest.raises(SystemExit):
        worker.require_loopback("http://0.0.0.0:11434")
    worker.require_safe_database_url("postgresql://user@127.0.0.1:15433/aads")
    with pytest.raises(SystemExit):
        worker.require_safe_database_url("postgresql://user@db.example/aads")


def test_worker_null_metadata_hash_contract():
    from scripts import qwen3_embedding_worker as worker

    assert worker.canonical_chunk_text(
        {"title": None, "heading": "H", "content": None}
    ) == "\nH\n"
    payload = worker.canonical_document_payload(
        {"title": None, "heading": "H", "content": None}
    )
    assert payload == worker.QWEN_DOCUMENT_INSTRUCTION + "\nH\n"
    assert len(worker.payload_sha256(payload)) == 64


def test_worker_hashes_exact_truncated_ollama_payload():
    from scripts import qwen3_embedding_worker as worker

    prefix = {"title": "T", "heading": "H", "content": "x" * 5000}
    changed_after_cutoff = {**prefix, "content": "x" * 5000 + "changed"}
    payload = worker.canonical_document_payload(prefix)
    assert len(payload) == worker.QWEN_DOCUMENT_PAYLOAD_MAX_CHARS
    assert payload == worker.canonical_document_payload(changed_after_cutoff)
    assert worker.payload_sha256(payload) == worker.payload_sha256(
        worker.canonical_document_payload(changed_after_cutoff)
    )
    assert '"input": payload' in inspect.getsource(worker.process_one)


@pytest.mark.asyncio
async def test_mode_call_order_and_qwen_success_skips_legacy(monkeypatch):
    calls = []

    async def legacy(*args, **kwargs):
        calls.append("legacy")
        return [{"doc_path": "legacy"}]

    async def embed(query):
        calls.append("embed")
        return [0.0] * 1024

    async def qwen(*args, **kwargs):
        calls.append("qwen")
        return [{"doc_path": "qwen"}]

    monkeypatch.setattr(doc_index, "_QWEN_MODE", "qwen3")
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)
    monkeypatch.setattr(doc_index, "search_docs_qwen3", qwen)
    rows = await doc_index.search_docs([0.0] * 768, query_text="질문")
    assert rows[0]["doc_path"] == "qwen"
    assert calls == ["embed", "qwen"]


@pytest.mark.asyncio
async def test_qwen_mode_falls_back_to_legacy(monkeypatch):
    async def legacy(*args, **kwargs):
        return [{"doc_path": "legacy"}]
    async def broken(_query):
        raise RuntimeError("offline")
    monkeypatch.setattr(doc_index, "_QWEN_MODE", "qwen3")
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", broken)
    assert (await doc_index.search_docs([0.0] * 768, query_text="질문"))[0]["doc_path"] == "legacy"


@pytest.mark.asyncio
async def test_qwen_empty_falls_back_to_legacy(monkeypatch):
    async def legacy(*args, **kwargs): return [{"doc_path": "legacy"}]
    async def embed(_query): return [0.0] * 1024
    async def empty(*args, **kwargs): return []
    monkeypatch.setattr(doc_index, "_QWEN_MODE", "qwen3")
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)
    monkeypatch.setattr(doc_index, "search_docs_qwen3", empty)
    assert (await doc_index.search_docs([0.0] * 768, query_text="질문"))[0]["doc_path"] == "legacy"


@pytest.mark.asyncio
async def test_hybrid_runs_independently_and_fuses_ranks(monkeypatch):
    both_started = asyncio.Event()
    started = set()

    async def mark(name, rows):
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), .2)
        return rows

    async def legacy(*args, **kwargs):
        return await mark("legacy", [{"doc_path": "a", "heading": "h", "content": "a"}])
    async def embed(_query): return [0.0] * 1024
    async def qwen(*args, **kwargs):
        return await mark("qwen", [{"doc_path": "a", "heading": "h", "content": "a"}])
    monkeypatch.setattr(doc_index, "_QWEN_MODE", "hybrid")
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)
    monkeypatch.setattr(doc_index, "search_docs_qwen3", qwen)
    rows = await doc_index.search_docs([0.0] * 768, query_text="질문")
    assert rows[0]["fusion_hits"] == 2


@pytest.mark.asyncio
async def test_shadow_returns_legacy_before_tracked_observation_finishes(monkeypatch):
    release = asyncio.Event()
    async def legacy(*args, **kwargs): return [{"doc_path": "legacy"}]
    async def embed(_query):
        await release.wait()
        return [0.0] * 1024
    monkeypatch.setattr(doc_index, "_QWEN_MODE", "shadow")
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)
    rows = await asyncio.wait_for(
        doc_index.search_docs([0.0] * 768, query_text="질문"), .2,
    )
    assert rows[0]["doc_path"] == "legacy"
    assert len(doc_index._shadow_tasks) == 1
    release.set()
    await asyncio.gather(*tuple(doc_index._shadow_tasks))
    await asyncio.sleep(0)
    assert not doc_index._shadow_tasks


@pytest.mark.asyncio
async def test_query_embedding_cache(monkeypatch):
    calls = 0
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"embeddings": [[0.0] * 1024]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return Response()
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: Client())
    doc_index._query_cache.clear()
    await doc_index.embed_qwen_query(" 같은   질문 ")
    await doc_index.embed_qwen_query("같은 질문")
    assert calls == 1
    monkeypatch.setattr(doc_index, "QWEN_QUERY_INSTRUCTION", "new instruction: ")
    await doc_index.embed_qwen_query("같은 질문")
    assert calls == 2


def test_migration_is_isolated_and_idempotent():
    from pathlib import Path
    sql = (
        Path(__file__).parents[2] / "migrations" / "20260919_qwen3_doc_embeddings.sql"
    ).read_text()
    assert "vector(1024)" in sql
    assert "chunk_id uuid NOT NULL REFERENCES doc_chunks(id)" in sql
    assert "UNIQUE (chunk_id, model_id, instruction_version)" in sql
    assert "ON CONFLICT" in sql and "USING hnsw" in sql
    assert "UPDATE doc_chunks SET embedding" not in sql
    assert "qwen3-doc-v2-payload4000" in sql
    assert "digest(left(" in sql and "4000" in sql


def test_worker_claim_and_sync_are_idempotent_and_lease_safe():
    from scripts import qwen3_embedding_worker as worker
    claim_sql = inspect.getsource(worker.claim)
    sync_sql = inspect.getsource(worker.sync_queue)
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "lease_expires_at < now()" in claim_sql
    assert "ON CONFLICT (chunk_id, model_id, instruction_version) DO UPDATE" in sync_sql
    assert "left(" in sync_sql
