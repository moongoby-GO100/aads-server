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
