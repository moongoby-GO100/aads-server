import asyncio

import pytest

from app.services import doc_index


def _reset_shadow_scheduler(monkeypatch, *, in_flight=1, queue_max=1):
    monkeypatch.setattr(doc_index, "_QWEN_SHADOW_MAX_IN_FLIGHT", in_flight)
    monkeypatch.setattr(doc_index, "_QWEN_SHADOW_QUEUE_MAX", queue_max)
    monkeypatch.setattr(doc_index, "_shadow_semaphore", asyncio.BoundedSemaphore(in_flight))
    monkeypatch.setattr(doc_index, "_shadow_reserved", 0)
    monkeypatch.setattr(
        doc_index,
        "_shadow_metrics",
        {"created": 0, "completed": 0, "dropped": 0, "timeout": 0, "error": 0},
    )


@pytest.mark.asyncio
async def test_shadow_is_bounded_and_drops_without_delaying_legacy(monkeypatch):
    _reset_shadow_scheduler(monkeypatch, in_flight=1, queue_max=1)
    release = asyncio.Event()
    started = 0

    async def legacy(*args, **kwargs):
        return [{"doc_path": "legacy"}]

    async def embed(_query):
        nonlocal started
        started += 1
        await release.wait()
        return [0.0] * 1024

    monkeypatch.setattr(doc_index, "_QWEN_MODE", "shadow")
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)

    results = await asyncio.gather(*(
        asyncio.wait_for(doc_index.search_docs([0.0] * 768, query_text="q"), .2)
        for _ in range(4)
    ))
    assert all(rows[0]["doc_path"] == "legacy" for rows in results)
    assert doc_index.shadow_metrics()["created"] == 2
    assert doc_index.shadow_metrics()["dropped"] == 2
    await asyncio.sleep(0)
    assert started == 1
    release.set()
    await asyncio.gather(*tuple(doc_index._shadow_tasks))
    assert doc_index.shadow_metrics()["completed"] == 2


@pytest.mark.asyncio
async def test_shadow_timeout_is_counted_and_does_not_escape(monkeypatch):
    _reset_shadow_scheduler(monkeypatch, in_flight=1, queue_max=0)
    release = asyncio.Event()

    async def legacy(*args, **kwargs):
        return [{"doc_path": "legacy"}]

    async def embed(_query):
        await release.wait()
        return [0.0] * 1024

    monkeypatch.setattr(doc_index, "_QWEN_MODE", "shadow")
    monkeypatch.setattr(doc_index, "_QWEN_SHADOW_TIMEOUT", .01)
    monkeypatch.setattr(doc_index, "search_docs_legacy", legacy)
    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)
    assert (await doc_index.search_docs([0.0] * 768, query_text="q"))[0]["doc_path"] == "legacy"
    await asyncio.gather(*tuple(doc_index._shadow_tasks))
    assert doc_index.shadow_metrics()["timeout"] == 1


@pytest.mark.asyncio
async def test_shadow_error_is_counted_and_does_not_escape(monkeypatch):
    _reset_shadow_scheduler(monkeypatch, in_flight=1, queue_max=0)

    async def fail_observation():
        raise RuntimeError("shadow unavailable")

    assert doc_index._schedule_shadow(fail_observation) is True
    await asyncio.gather(*tuple(doc_index._shadow_tasks))

    metrics = doc_index.shadow_metrics()
    assert metrics["error"] == 1
    assert metrics["created"] == 1
