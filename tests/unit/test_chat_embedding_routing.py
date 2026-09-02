import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_embedding_skips_unavailable_external_route(monkeypatch):
    from app.services import chat_embedding_service as svc
    from app.services.ai_route_resolver import AIRouteCandidate

    class FailingOpenAI:
        def __init__(self, *args, **kwargs):
            raise AssertionError("unavailable route must not instantiate OpenAI client")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FailingOpenAI))
    async def _fake_candidates(route_key):
        return [
            AIRouteCandidate(
                route_key=route_key,
                provider="openai",
                model_id="text-embedding-3-small",
                display_order=10,
                is_default=True,
                availability="not_configured",
            )
        ]

    monkeypatch.setattr(svc, "get_route_candidates", _fake_candidates)

    vectors = await svc._embed_uncached_with_routes(["hello"])

    assert len(vectors) == 1
    assert len(vectors[0]) == 768
