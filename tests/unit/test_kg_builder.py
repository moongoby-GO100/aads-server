"""구조화 기록 → 지식 그래프 빌더 회귀 테스트.

2026-09-14. 문서에서 개체·관계를 뽑으려면 문서 823건마다 LLM 을 불러야
하는데 이 서버는 GPU 가 없다. 그런데 **이미 선으로 연결된 데이터**가 있다.

    ohvis_wiki_error_book          증상→원인→고친커밋→고친파일
    chat_workspace_change_ledger   파일→커밋→배포
    deploy_runs                    배포→릴리스SHA→성공여부

추출 비용이 0 이고 정확도가 100% 다 — 기계가 남긴 사실이기 때문이다.
LLM 으로 넓히는 것은 그다음이다.

이 테스트가 지키는 것은 셋이다.

1. **쓰레기를 잇지 않는다.** 문서 본문의 아무 문자열이나 파일로 이으면
   그래프가 못 쓰게 된다. 잘못된 관계는 계속 전파된다.
2. **남의 엣지를 지우지 않는다.** 재구축은 이 빌더가 만든 것만 지운다.
   `app/core/knowledge_graph.py` 가 쌓은 145개체·628관계가 같이 지워지면
   복구할 방법이 없다.
3. **SHA 를 일관되게 자른다.** 같은 커밋이 7자·12자·40자로 각각 노드가
   되면 선이 끊긴다.
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_kg.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_kg", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sha_normalized_to_one_length():
    m = _load()
    full = "07321522f348aabbccddeeff0011223344556677"
    assert m.short(full) == m.short(full[:12]) == m.short(full[:20])
    assert len(m.short(full)) == 12
    # SHA 가 아닌 것은 커밋 노드로 만들지 않는다.
    assert m.short("main") == ""
    assert m.short("") == ""
    assert m.short("not-a-sha-value") == ""


def test_graph_dedupes_nodes_and_edges():
    m = _load()
    g = m.Graph()
    g.node("file", "app/x.py")
    g.node("file", "app/x.py", desc="설명")
    g.edge("commit", "abc123def456", "modifies", "file", "app/x.py")
    g.edge("commit", "abc123def456", "modifies", "file", "app/x.py")
    # edge() 는 노드를 만들지 않는다 — 노드는 명시적으로 등록해야 한다.
    assert len(g.nodes) == 1
    assert len(g.edges) == 1
    assert g.nodes[("file", "app/x.py")]["desc"] == "설명"


def test_graph_refuses_self_and_empty():
    m = _load()
    g = m.Graph()
    g.node("file", "   ")
    g.edge("file", "a.py", "related_to", "file", "a.py")   # 자기 자신
    g.edge("file", "", "modifies", "file", "b.py")          # 빈 이름
    assert g.nodes == {}
    assert g.edges == {}


def test_rebuild_only_deletes_own_edges():
    """재구축이 남의 엣지를 지우면 안 된다."""
    import inspect

    m = _load()
    src = inspect.getsource(m.persist)
    assert "DELETE FROM kg_relations" in src
    assert "evidence LIKE" in src, "전체 삭제로 보인다 — 남의 엣지까지 지운다"
    assert "ORIGIN" in src, "출처 표시 없이 지우고 있다"


def test_doc_mention_requires_known_file():
    """문서에 적힌 경로가 저장소에 실제로 있을 때만 잇는다."""
    import inspect

    m = _load()
    src = inspect.getsource(m.collect_doc_mentions)
    assert "by_base" in src and "known" in src
    # 같은 파일명이 여러 곳이면 모호하므로 잇지 않는다.
    assert "len(targets) > 3" in src


def test_graph_context_only_for_specific_shapes():
    """질문에서 아무 단어나 그래프로 찾지 않는다.

    파일 경로·커밋 SHA·오류 키처럼 **모양이 분명한 것만** 본다. 아무
    명사나 넣으면 엉뚱한 노드가 걸리고, 그 순간 그래프는 근거로 못 쓴다.
    """
    from app.services.kg_query import extract_candidates

    assert extract_candidates("chat_service.py 가 왜 느려졌지") == ["chat_service.py"]
    assert "07321522" in extract_candidates("커밋 07321522 에서 뭘 고쳤지")
    assert "embed.container_localhost" in " ".join(
        extract_candidates("embed.container_localhost_dummy_fallback 이 뭐야")
    )
    # 평범한 문장은 후보가 없어야 한다.
    assert extract_candidates("오늘 매출이 어떻게 되지") == []
    assert extract_candidates("") == []


def test_relation_labels_are_korean():
    """대표가 읽는 화면이다. 관계 이름을 영문 그대로 두지 않는다."""
    from app.services.kg_query import relation_label

    for rel in ("modifies", "deployed_in", "documents", "resolved_by", "affects"):
        label = relation_label(rel)
        assert label != rel, f"{rel} 이 번역되지 않았다"
        assert label.isascii() is False


def test_graph_context_is_empty_without_matches():
    """관계가 없으면 빈 문자열. 억지로 끌어오지 않는다."""
    import inspect

    from app.services import kg_query

    src = inspect.getsource(kg_query.context_for_question)
    assert 'return ""' in src
    assert "knowledge_graph" in src


def test_auto_rag_keeps_graph_when_vector_empty():
    """벡터가 빈손이어도 그래프 근거는 살아야 한다.

    예전에는 `if not results: return ""` 로 일찍 반환해서 그래프까지 버렸다.
    """
    import inspect

    from app.services import auto_rag

    src = inspect.getsource(auto_rag.build_auto_rag_context)
    assert "graph_context" in src
    assert "return graph_context" in src, (
        "벡터 결과가 없을 때 그래프 근거까지 버리고 있다"
    )
    gi = src.index("graph_context = await context_for_question")
    ri = src.index("if not results:")
    assert gi < ri, "그래프 조회가 조기 반환보다 뒤에 있다"
