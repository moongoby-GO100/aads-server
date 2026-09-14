"""문서 색인·검색 파이프라인 회귀 테스트.

2026-09-14. 대표가 "이거 왜 이렇게 돼 있지" 라고 물어도 문서가 근거로
잡히지 않았다. Auto-RAG 는 memory_facts 와 채팅 기록만 검색했고 저장소
문서 823건은 검색 대상이 아니었다.

이 테스트가 지키는 것은 셋이다.

1. **사본을 색인하지 않는다.** `/root/aads` 의 `.md` 13,219개 중 고유는
   823개였다. 릴리스 스냅숏·워크트리·go100 클론 6벌이 같은 문서를 33벌까지
   만든다. 제외 규칙이 풀리면 임베딩이 그만큼 낭비되고, 검색 결과가 같은
   문서의 사본으로 채워진다.
2. **Auto-RAG 가 문서를 본다.** 연결이 끊겨도 채팅은 멀쩡히 돌기 때문에
   증상이 "답이 좀 부실하다" 뿐이라 눈치채기 어렵다.
3. **출처 경로가 결과에 있다.** 근거 경로가 없으면 비전문가는 답을 검증할
   방법이 없다.
"""
import importlib.util
import inspect
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "index_docs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("index_docs", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clone_and_worktree_paths_are_excluded():
    m = _load_script()
    for path in (
        "/root/aads/.worktrees/x/docs/a.md",
        "/root/aads/aads-server-releases/autoheal/docs/a.md",
        "/root/aads/go100-token-opt/docs/a.md",
        "/root/aads/go100-direct-yBauuU/report/a.md",
        "/root/aads/aads-dashboard-unni/docs/a.md",
        "/root/aads/x/node_modules/pkg/readme.md",
        "/root/aads/aads-server-unified-p0/docs/a.md",
    ):
        assert m.excluded(path), f"제외돼야 하는데 색인 대상이다: {path}"


def test_canonical_paths_are_kept():
    m = _load_script()
    for path in (
        "/root/aads/aads-server/docs/chat/CHAT-BACKEND-SPEC.md",
        "/root/aads/aads-docs/reports/x.md",
        "/root/aads/go100/docs/reports/y.md",
    ):
        assert not m.excluded(path), f"정본인데 제외됐다: {path}"


def test_chunker_keeps_heading_context():
    m = _load_script()
    text = "# 제목\n\n" + ("본문 " * 400) + "\n\n## 두번째 절\n\n" + ("다른 본문 " * 400)
    chunks = m.chunk(text)
    assert len(chunks) >= 2
    headings = {h for h, _ in chunks}
    assert any("두번째" in h for h in headings), "절 제목이 청크에 안 붙었다"
    assert all(len(body) > 0 for _, body in chunks)


def test_chunker_caps_runaway_documents():
    """HANDOVER.md 는 1.67MB 다. 상한이 없으면 한 문서가 검색을 독점한다."""
    m = _load_script()
    chunks = m.chunk("본문 " * 400_000)
    assert len(chunks) <= m.MAX_CHUNKS_PER_DOC


def test_embed_helper_never_fabricates():
    """임베딩 실패는 실패로 둔다 — 더미를 만들면 안 된다."""
    m = _load_script()
    src = inspect.getsource(m.ollama_embed)
    assert "return None" in src
    assert "dummy" not in src.lower() and "hash" not in src.lower()


def test_auto_rag_searches_documents():
    from app.services import auto_rag

    src = inspect.getsource(auto_rag._search_relevant)
    assert "_search_documents" in src, "Auto-RAG 가 문서를 검색하지 않는다"


def test_doc_results_carry_source_path():
    from app.services import auto_rag

    src = inspect.getsource(auto_rag._search_documents)
    # 포맷터가 읽는 키를 그대로 채워야 컨텍스트에 들어간다.
    for key in ('"text"', '"source"', '"timestamp"', '"path"'):
        assert key in src, f"결과에 {key} 가 없다 — 포맷터가 읽지 못한다"


@pytest.mark.asyncio
async def test_doc_search_survives_db_failure(monkeypatch):
    """문서 검색이 죽어도 채팅은 멈추면 안 된다."""
    from app.services import doc_index

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.db_pool.get_pool", _boom)
    assert await doc_index.search_docs([0.0] * 768) == []


def test_index_and_query_prefixes_stay_paired():
    """색인 접두어와 질문 접두어는 짝이 맞아야 한다.

    nomic-embed-text 는 작업 접두어를 요구한다. 한쪽만 바꾸면 검색이
    **조용히** 나빠진다 — 오류도 없고 결과도 나온다. 순위만 틀어진다.

    2026-09-14 실측. 질문 "채팅 응답이 왜 느려졌지" 에 대해
    정답 문단과 무관한 계약서 문단의 유사도 차이가
    접두어 없이 0.015, 접두어를 붙이면 0.061 이었다. 4배다.
    """
    from app.services import doc_index

    script = _load_script()
    assert script.DOC_PREFIX == doc_index.DOC_PREFIX, (
        "색인 스크립트와 서비스의 문서 접두어가 다르다 — 백필 경로별로 "
        "다른 벡터가 저장된다"
    )
    assert doc_index.QUERY_PREFIX != doc_index.DOC_PREFIX
    assert doc_index.QUERY_PREFIX.startswith("search_query")
    assert doc_index.DOC_PREFIX.startswith("search_document")


def test_query_prefix_applied_in_one_place():
    """호출자가 각자 붙이면 한 곳이 빠졌을 때 그 경로만 나빠진다.

    2026-09-14 `chat_messages` 도 접두어 세대(embedding_ver=2)로 넘어오면서
    접두어 붙은 질문 벡터를 쓰는 곳이 둘이 됐다(문서·채팅). 그래서 만드는
    자리를 `build_auto_rag_context` 한 곳으로 올리고 아래로 넘긴다 —
    각자 만들면 CPU Ollama 에 같은 문장을 두 번 태운다(요청당 약 20초).
    """
    from app.services import doc_index

    assert inspect.iscoroutinefunction(doc_index.embed_query)
    src = inspect.getsource(doc_index.embed_query)
    assert "QUERY_PREFIX" in src

    from app.services import auto_rag

    top = inspect.getsource(auto_rag.build_auto_rag_context)
    assert "embed_query" in top, "접두어 붙은 질문 벡터를 만드는 곳이 없다"
    assert top.count("embed_query(") == 1, "질문 벡터를 두 번 만들고 있다"

    for fn in (auto_rag._search_documents, auto_rag._search_chat_messages):
        assert "embed_query" not in inspect.getsource(fn), (
            f"{fn.__name__} 이 자기 벡터를 또 만든다 — 넘겨받아야 한다"
        )


def test_chat_message_search_only_reads_current_generation():
    """세대가 섞인 벡터를 한 검색에서 비교하면 조용히 나빠진다.

    2026-09-14 실측. `chat_messages` 의 벡터 33,140건 중 진짜는 65건이었고
    나머지는 임베딩 경로가 끊긴 채 저장된 해시 더미였다. 더미는 값이
    난수라 **어떤 질문과도 적당히 비슷하게** 나온다 — 그래서 섞어 놓으면
    상위를 더미가 차지한다. 오류도 없고 결과도 나온다. 순위만 무의미하다.
    """
    from app.services import auto_rag
    from app.services import chat_embedding_service as ces

    assert ces.CHAT_EMBED_VER == 2
    assert ces.CHAT_DOC_PREFIX.startswith("search_document")
    assert ces.CHAT_QUERY_PREFIX.startswith("search_query")

    src = inspect.getsource(auto_rag._search_chat_messages)
    assert src.count("embedding_ver = 2") == 3, (
        "크로스 세션 검색 경로 3개(오케스트레이터·프로젝트·워크스페이스) "
        "전부에 세대 조건이 있어야 한다 — 하나만 빠져도 그 경로가 더미를 읽는다"
    )
    assert "embedding_ver = 2" in inspect.getsource(auto_rag._detect_reask)

    # 저장하는 쪽도 같은 세대를 적어야 짝이 맞는다.
    store = inspect.getsource(ces.embed_and_store_message)
    assert "CHAT_DOC_PREFIX" in store and "embedding_ver" in store
    assert "embed_texts_strict" in store, "더미를 저장하면 안 된다"
