"""임베딩이 가짜로 저장되는 것을 막는 회귀 테스트.

2026-09-14. 임베딩이 **전부 더미였다.**

`local_embedding_bridge` 가 Ollama 주소를 `127.0.0.1:11434` 로 박아 뒀는데
앱은 컨테이너 안에서 돈다. 컨테이너의 127.0.0.1 은 자기 자신이라 호스트의
Ollama 에 닿지 않았다. PC Agent 폴백도 죽어 있었고(에이전트가 `ollama_embed`
명령을 구현하지 않았다), Gemini 는 꺼져 있었다.

세 경로가 다 실패하면 `chat_embedding_service` 가 해시 기반 더미 벡터를
돌려주는데, 그 로그가 **debug** 였다. 그래서 아무도 몰랐다.

더미도 값이 골고루 흩어져서 유사도 숫자는 그럴듯하게 나온다 — 의미만 없다.
증상은 "검색이 되긴 하는데 결과가 이상하다" 하나뿐이었다. 문서 7,847청크를
색인하고 "채팅 응답이 왜 느려졌지" 로 검색했더니 온보딩 가이드가 0.97 로
1등이었다.

조용한 실패가 시끄러운 실패보다 훨씬 비싸다.
"""
import inspect
import os

import pytest


def test_ollama_url_is_container_aware():
    """컨테이너 안에서는 127.0.0.1 을 쓰면 안 된다."""
    from app.core import local_embedding_bridge as bridge

    src = inspect.getsource(bridge._default_ollama_url)
    assert "host.docker.internal" in src, "컨테이너 경로가 없다"
    assert "/.dockerenv" in src, "컨테이너 판별이 없다"
    assert "LOCAL_OLLAMA_URL" in src, "환경변수 오버라이드가 없다"


def test_dummy_fallback_is_loud():
    """더미로 떨어질 때 debug 로 숨기면 안 된다."""
    from app.services import chat_embedding_service as ces

    src = inspect.getsource(ces._embed_uncached_with_routes)
    assert "logger.warning" in src, "더미 폴백이 warning 이 아니다"
    assert "_LAST_ROUTE_WAS_DUMMY" in src, "더미 여부를 기록하지 않는다"


def test_strict_helper_refuses_dummy():
    """영구 저장하는 호출자를 위한 strict 경로가 있어야 한다."""
    from app.services import chat_embedding_service as ces

    assert inspect.iscoroutinefunction(ces.embed_texts_strict)
    assert issubclass(ces.EmbeddingRouteUnavailable, Exception)
    src = inspect.getsource(ces.embed_texts_strict)
    assert "EmbeddingRouteUnavailable" in src


def test_doc_index_uses_strict():
    """문서 색인은 더미를 저장하면 안 된다."""
    from app.services import doc_index

    src = inspect.getsource(doc_index.backfill_embeddings)
    assert "embed_texts_strict" in src, "문서 색인이 strict 를 안 쓴다"
    assert "embed_texts(" not in src.replace("embed_texts_strict(", ""), (
        "느슨한 embed_texts 를 아직 쓰고 있다"
    )


@pytest.mark.asyncio
async def test_strict_raises_when_no_route(monkeypatch):
    from app.services import chat_embedding_service as ces

    async def _fake(texts):
        ces._LAST_ROUTE_WAS_DUMMY = True
        return [[0.0] * 768 for _ in texts]

    monkeypatch.setattr(ces, "_embed_uncached_with_routes", _fake)
    ces._embed_cache = ces._EmbedCache(ttl=1, maxsize=8)
    with pytest.raises(ces.EmbeddingRouteUnavailable):
        await ces.embed_texts_strict(["더미 경로 확인용 문장"])
