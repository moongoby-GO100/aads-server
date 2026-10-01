"""문서 근거의 "언제 것이고 · 어디서 왔고 · 승인되었나" 가 **최종 RAG 문자열까지**
살아남는지 지키는 테스트.

2026-10-01 지시의 배경은 "과거 문서를 현재 기준으로 오인" 이었다. 실측한 원인은 셋이다.

1. `app/services/auto_rag.py` 가 `timestamp` 칸에 **파일 경로**를 넣고 있었다.
   포맷터는 그 값을 `- [출처] ({ts}, 유사도:…) 본문` 으로 그대로 찍는다. 그래서
   4월에 쓰인 문서가 날짜 없이 올라왔다.
2. 그 다음 판은 `mtime or indexed_at` 로 날짜 **하나만** 찍었다. `mtime` 은 파일이
   마지막으로 바뀐 시각이고 `indexed_at` 은 우리가 색인한 시각이다. 둘 다 작성일도
   운영 검증 시각도 아니다. 이름표 없이 한 날짜만 찍으면 색인일이 작성일로 읽힌다.
   게다가 문자열을 `[:10]` 으로 잘라 내보냈기 때문에 "문서가 쓰인 시각" 같은 설명문도
   날짜 자리에 찍힐 수 있었다.
3. dict 에 `doc_sha256`·`path`·`currency_status` 를 담아도 포맷터가 찍지 않으면
   **LLM 은 그것을 못 본다.** 최종 문자열이 유일한 전달 경로다.

그래서 이 파일은 소스 문자열이 아니라 **실제 함수를 돌려** 확인한다. 날짜 종류 분리는
`_format_doc_stamp`·`_doc_evidence_header` 를 호출해서, 메타데이터 보존은
`build_auto_rag_context` 가 돌려주는 문자열에서 찾아서 확인한다.

승인 상태는 **추정하지 않는다.** `doc_chunks.label` 은 위치 설명자일 뿐이고(실측 값 예:
"KIS 문서(contabo14)") 승인 정보가 없다. 날짜로도 추정하지 않는다. 명시 manifest 가
생기기 전까지는 전건 `승인미확인`·`현재상태미검증` 으로 고정 표시한다.
"""
from __future__ import annotations

import asyncio
import datetime
import importlib
import re
import sys
import types
from pathlib import Path

import pytest

from app.core.token_utils import estimate_tokens

rag = importlib.import_module("app.services.auto_rag")
doc_index = importlib.import_module("app.services.doc_index")

ROOT = Path(__file__).resolve().parents[2]
DOC_INDEX_SRC = ROOT / "app/services/doc_index.py"

# 이 네 값이 "언제 것이고 어디서 왔나" 에 답한다.
METADATA_COLUMNS = ("doc_sha256", "label", "mtime", "indexed_at")

DOC_PATH = "/root/aads/docs/operations/CURRENT_AUTHORITY_AND_SERVER_LEDGER.md"
DOC_SHA = "c6e3105cf68727446358210e57f4b70b033e2da425a2008990260b24ab1c1b32"
MTIME = datetime.datetime(2026, 4, 7, 10, 23)
INDEXED_AT = datetime.datetime(2026, 10, 1, 7, 2)
BODY = "원장 서버는 4대이고 건강 감시 대상은 3대다. " * 12


def _row(**overrides) -> dict:
    """`doc_index.search_docs` 가 돌려주는 모양의 행."""
    row = {
        "doc_path": DOC_PATH,
        "server": "contabo116",
        "project": "AADS",
        "title": "현재 권한·서버 원장",
        "heading": "원장",
        "content": BODY,
        "doc_sha256": DOC_SHA,
        "label": "AADS 문서(contabo116)",
        "mtime": MTIME,
        "indexed_at": INDEXED_AT,
        "similarity": 0.81,
    }
    row.update(overrides)
    return row


def _docs_from(rows, monkeypatch) -> list:
    """실제 `_search_documents` 를 돌려 RAG 가 받는 dict 를 만든다."""

    async def fake_search_docs(embedding, *, top_k=5, project=None, query_text=None):
        return [dict(r) for r in rows]

    monkeypatch.setattr("app.services.doc_index.search_docs", fake_search_docs)
    return asyncio.run(rag._search_documents([0.1] * 8, "AADS", "원장 서버 몇 대인가"))


def _render(docs, monkeypatch, budget=None) -> str:
    """실제 `build_auto_rag_context` 를 돌려 **LLM 이 보는 문자열**을 받는다.

    임베딩·그래프·DB 는 네트워크를 타므로 대역만 끼운다. 포맷 경로는 건드리지
    않는다 — 이 테스트가 보려는 것이 바로 그 경로다.
    """
    embed_mod = types.ModuleType("app.services.chat_embedding_service")

    async def embed_texts(texts):
        return [[0.1] * 8]

    embed_mod.embed_texts = embed_texts
    monkeypatch.setitem(sys.modules, "app.services.chat_embedding_service", embed_mod)

    kg_mod = types.ModuleType("app.services.kg_query")

    async def context_for_question(question):
        return ""

    kg_mod.context_for_question = context_for_question
    monkeypatch.setitem(sys.modules, "app.services.kg_query", kg_mod)

    async def fake_embed_query(text):
        return [0.2] * 8

    async def fake_search_relevant(*args, **kwargs):
        return [dict(d) for d in docs]

    async def fake_detect_reask(*args, **kwargs):
        return False

    monkeypatch.setattr("app.services.doc_index.embed_query", fake_embed_query)
    monkeypatch.setattr(rag, "_search_relevant", fake_search_relevant)
    monkeypatch.setattr(rag, "_detect_reask", fake_detect_reask)
    monkeypatch.setattr(rag, "_AUTO_RAG_ENABLED", True)
    if budget is not None:
        monkeypatch.setattr(rag, "_RAG_TOKEN_BUDGET", budget)
    return asyncio.run(
        rag.build_auto_rag_context(
            "원장 서버가 지금 몇 대인지 확인해 주십시오", "sess-behavior-1", "AADS",
        )
    )


def _doc_line(block: str) -> str:
    line = next((l for l in block.splitlines() if l.startswith("- [문서 ")), "")
    assert line, f"문서 근거 줄이 최종 문자열에 없다:\n{block}"
    return line


# ── 1) 날짜 검증: 진짜 날짜만 통과한다 ──────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime.datetime(2026, 4, 7, 10, 23), "2026-04-07"),
        (datetime.date(2026, 9, 18), "2026-09-18"),
        ("2026-09-18 10:49:12+09", "2026-09-18"),
        ("2026-09-18T10:49:12.123456", "2026-09-18"),
        ("  2026-09-18  ", "2026-09-18"),
        (None, ""),
        ("", ""),
        # 경로가 다시 흘러들면 날짜가 아니라 빈 값이 돼야 한다.
        ("/root/aads/docs/foo.md", ""),
        ("C:\\docs\\foo.md", ""),
        # `[:10]` 자르기 시절에 날짜처럼 새어 나갔던 값들.
        ("문서가 쓰인 시각", ""),
        ("2026-13-45", ""),
        ("abcdefghijklmn", ""),
        ("오늘", ""),
        (1759000000, ""),
        (True, ""),
    ],
)
def test_format_doc_stamp_accepts_only_real_dates(value, expected) -> None:
    assert rag._format_doc_stamp(value) == expected


# ── 2) 날짜 종류를 섞지 않는다 ──────────────────────────────────────────
def test_header_labels_each_date_kind(monkeypatch) -> None:
    header = rag._doc_evidence_header(_docs_from([_row()], monkeypatch)[0])
    assert "파일변경일 2026-04-07" in header
    assert "색인일 2026-10-01" in header
    assert "승인미확인" in header
    assert "현재상태미검증" in header
    assert f"경로 {DOC_PATH}" in header
    assert f"hash {DOC_SHA[:12]}" in header


def test_missing_mtime_says_unknown_and_never_promotes_index_date(monkeypatch) -> None:
    """mtime 이 없으면 작성일은 미상이다. 색인일을 그 자리에 올리지 않는다."""
    doc = _docs_from([_row(mtime=None)], monkeypatch)[0]
    header = rag._doc_evidence_header(doc)
    assert "작성일 미상" in header
    # 파일변경일 자리에 **어떤 날짜도** 찍히지 않아야 한다(이름표만 남는다).
    assert not re.search(r"파일변경일 \d", header), header
    assert "색인일 2026-10-01" in header
    assert doc["timestamp"] == "작성일 미상"
    assert "2026-10-01" not in doc["timestamp"]


def test_unparseable_mtime_becomes_unknown(monkeypatch) -> None:
    doc = _docs_from([_row(mtime="문서가 쓰인 시각")], monkeypatch)[0]
    header = rag._doc_evidence_header(doc)
    assert "작성일 미상" in header
    assert "문서가 쓰인 시각" not in header


def test_timestamp_field_is_labelled_and_not_a_path(monkeypatch) -> None:
    """회귀 방지의 핵심 — 경로가 날짜 칸으로 돌아오면 실패한다."""
    doc = _docs_from([_row()], monkeypatch)[0]
    assert doc["timestamp"] == "파일변경일 2026-04-07"
    assert DOC_PATH not in doc["timestamp"]


# ── 3) 승인은 추정하지 않는다 ───────────────────────────────────────────
def test_authority_is_never_inferred_from_label_or_date(monkeypatch) -> None:
    rows = [
        _row(label="승인 정본", mtime=datetime.datetime.now()),
        _row(doc_path="/root/aads/docs/approved.md", label="approved"),
    ]
    docs = _docs_from(rows, monkeypatch)
    assert len(docs) == 2
    for doc in docs:
        assert doc["authority_status"] == "승인미확인"
        assert doc["currency_status"] == "현재상태미검증"


# ── 4) 최종 RAG 문자열까지 보존된다 ─────────────────────────────────────
def test_final_render_carries_every_evidence_field(monkeypatch) -> None:
    block = _render(_docs_from([_row()], monkeypatch), monkeypatch)
    line = _doc_line(block)
    for needle in (
        "파일변경일 2026-04-07",
        "색인일 2026-10-01",
        "승인미확인",
        "현재상태미검증",
        DOC_PATH,
        DOC_SHA[:12],
    ):
        assert needle in line, f"최종 RAG 문자열에 {needle!r} 가 없다:\n{line}"


def test_truncated_body_still_keeps_evidence_header(monkeypatch) -> None:
    """예산이 모자라면 본문만 줄이고 근거 머리말은 남는다.

    통째로 떨어뜨리면 그 문서가 걸렸다는 사실과 출처가 같이 사라진다.
    """
    docs = _docs_from([_row()], monkeypatch)
    head = f"- [{docs[0]['source']}] ({rag._doc_evidence_header(docs[0])}, 유사도:0.81) "
    budget = estimate_tokens(head + rag._DOC_BODY_TRIM_MARK) + 10
    block = _render(docs, monkeypatch, budget=budget)
    line = _doc_line(block)

    assert rag._DOC_BODY_TRIM_MARK in line
    assert estimate_tokens(line) <= budget
    assert BODY.strip() not in line, "본문이 줄지 않았다"
    for needle in ("파일변경일 2026-04-07", "색인일 2026-10-01", "승인미확인",
                   "현재상태미검증", DOC_PATH, DOC_SHA[:12]):
        assert needle in line, f"줄인 뒤 {needle!r} 가 사라졌다:\n{line}"


def test_fit_doc_line_gives_up_when_header_does_not_fit() -> None:
    head = "- [문서 a.md · 승인미확인] (파일변경일 2026-04-07, 유사도:0.81) "
    assert rag._fit_doc_line(head, BODY, 1) is None


def test_fit_doc_line_returns_full_body_when_budget_allows() -> None:
    assert rag._fit_doc_line("머리말 ", "본문", 10_000) == "머리말 본문"


# ── 5) 조회 계층에서 이미 떨어뜨리지 않는다 ─────────────────────────────
def test_search_docs_legacy_preserves_metadata(monkeypatch) -> None:
    """legacy 경로는 컬럼을 화이트리스트로 다시 적으므로 실제로 돌려 확인한다."""

    class _Pool:
        async def fetch(self, *args, **kwargs):
            return [_row()]

    pool_mod = types.ModuleType("app.core.db_pool")
    pool_mod.get_pool = lambda: _Pool()
    monkeypatch.setitem(sys.modules, "app.core.db_pool", pool_mod)

    out = asyncio.run(doc_index.search_docs_legacy([0.1] * 8, top_k=3, project="AADS"))
    assert out, "유사도 0.81 행이 걸러졌다"
    expected = _row()
    for column in METADATA_COLUMNS:
        assert out[0][column] == expected[column], f"{column} 가 결과 dict 에서 사라졌다"


def test_both_search_queries_select_metadata() -> None:
    """SQL 은 DB 없이 돌릴 수 없으므로 SELECT 목록만 스키마 가드로 확인한다.

    qwen3 경로는 `**dict(r)` 로 행을 그대로 펼치므로, 거기서 빠지면 아래 단계에서
    살려 보낼 방법이 없다.
    """
    blocks = re.findall(
        r"SELECT[^;]*?AS similarity", DOC_INDEX_SRC.read_text(encoding="utf-8"), re.S,
    )
    assert len(blocks) == 2, f"검색 SELECT 가 2개가 아니다: {len(blocks)}개"
    for i, block in enumerate(blocks):
        missing = [c for c in METADATA_COLUMNS if c not in block]
        assert not missing, f"{i + 1}번째 검색 SELECT 가 {missing} 를 뽑지 않는다"
