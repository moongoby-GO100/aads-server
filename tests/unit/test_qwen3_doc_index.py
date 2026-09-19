import inspect

import pytest

from app.services import doc_index


def test_qwen_contract_and_instruction_pairing():
    assert doc_index.QWEN_DIMENSION == 1024
    assert doc_index.QWEN_MODEL_ID == "qwen3-embedding:0.6b"
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
    assert not worker.allowed_host("contabo14")
    with pytest.raises(SystemExit):
        worker.require_loopback("http://0.0.0.0:11434")


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


def test_migration_is_isolated_and_idempotent():
    from pathlib import Path
    sql = (Path(__file__).parents[2] / "migrations" / "20260919_qwen3_doc_embeddings.sql").read_text()
    assert "vector(1024)" in sql
    assert "UNIQUE (chunk_id, model_id, instruction_version)" in sql
    assert "ON CONFLICT" in sql and "USING hnsw" in sql
    assert "UPDATE doc_chunks SET embedding" not in sql
