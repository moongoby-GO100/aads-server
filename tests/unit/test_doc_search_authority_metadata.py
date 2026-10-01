"""색인 문서의 출처·시각·승인상태가 조회에서 RAG 까지 살아남는지 지키는 테스트.

2026-10-01 지시의 배경은 "과거 문서를 현재 기준으로 오인" 이었다. 실측한 원인은 둘이다.

1. `app/services/auto_rag.py` 가 `timestamp` 칸에 **파일 경로**를 넣고 있었다.
   RAG 포맷터는 그 값을 `- [출처] ({ts}, 유사도:…) 본문` 으로 그대로 찍는다.
   그래서 2026-04-07 에 쓰인 문서가 날짜 없이 올라왔고, 읽는 쪽은 그것이
   반년 전 자료인지 알 방법이 없었다. 경로는 이미 `path`·`msg_id` 에 있었다.
2. `app/services/doc_index.py` 의 두 검색 SELECT 가 `doc_sha256`·`label`·
   `mtime`·`indexed_at` 를 아예 select 하지 않았다. 조회 계층에서 이미
   사라지므로 아래 단계에서 살려 보낼 방법이 없었다.

승인 상태는 **추정하지 않는다.** `doc_chunks.label` 은 위치 설명자일 뿐이고
(실측 값 예: "KIS 문서(contabo14)", "GO100 리포트(contabo14)") 승인 정보가
없다. 날짜로도 추정하지 않는다. 명시 manifest 가 생기기 전까지는 전건
`승인미확인`·`현재상태미검증` 으로 고정 표시한다.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC_INDEX = ROOT / "app/services/doc_index.py"
AUTO_RAG = ROOT / "app/services/auto_rag.py"

# 이 네 값이 "언제 것이고 어디서 왔나" 에 답한다.
METADATA_COLUMNS = ("doc_sha256", "label", "mtime", "indexed_at")


def _load_format_doc_stamp():
    """무거운 임포트를 피하려고 헬퍼 함수만 떼어내 실행한다."""
    src = AUTO_RAG.read_text(encoding="utf-8")
    match = re.search(r"def _format_doc_stamp.*?\n(?=\n\nasync def)", src, re.S)
    assert match, "_format_doc_stamp 를 찾지 못했다"
    namespace: dict = {}
    exec(match.group(0), namespace)  # noqa: S102 - 자기 저장소 소스만 실행한다
    return namespace["_format_doc_stamp"]


def _select_blocks() -> list[str]:
    """doc_index 의 검색 SELECT 두 개(qwen3·legacy)를 본문째로 뽑는다."""
    src = DOC_INDEX.read_text(encoding="utf-8")
    blocks = re.findall(r"SELECT[^;]*?AS similarity", src, re.S)
    return blocks


def test_both_search_queries_select_metadata() -> None:
    blocks = _select_blocks()
    assert len(blocks) == 2, f"검색 SELECT 가 2개가 아니다: {len(blocks)}개"
    for i, block in enumerate(blocks):
        missing = [c for c in METADATA_COLUMNS if c not in block]
        assert not missing, (
            f"{i + 1}번째 검색 SELECT 가 {missing} 를 뽑지 않는다. "
            "조회에서 떨어뜨리면 RAG 까지 보존할 수 없다."
        )


def test_legacy_result_dict_carries_metadata() -> None:
    """legacy 경로는 컬럼을 화이트리스트로 다시 적으므로 dict 도 확인한다."""
    src = DOC_INDEX.read_text(encoding="utf-8")
    body = src.split("async def search_docs_legacy", 1)[1].split("\nasync def ", 1)[0]
    for column in METADATA_COLUMNS:
        assert f'"{column}": r["{column}"]' in body, (
            f"search_docs_legacy 결과 dict 에 {column} 이 없다"
        )


def test_timestamp_field_is_not_the_document_path() -> None:
    """회귀 방지의 핵심 — `"timestamp": path` 가 돌아오면 실패한다."""
    src = AUTO_RAG.read_text(encoding="utf-8")
    assert '"timestamp": path' not in src, (
        'timestamp 칸에 경로를 넣는 코드가 돌아왔다. 포맷터가 그 값을 날짜 '
        "자리에 찍으므로 오래된 문서가 최신처럼 읽힌다."
    )


def test_doc_result_exposes_authority_and_currency() -> None:
    src = AUTO_RAG.read_text(encoding="utf-8")
    body = src.split('"kind": "doc",', 1)[1].split("return out", 1)[0]
    for key in ("doc_sha256", "doc_mtime", "indexed_at", "label",
                "authority_status", "currency_status"):
        assert key in body, f"문서 결과에 {key} 가 없다"
    assert "승인미확인" in body, "승인 미확인 표시가 없다"
    assert "현재상태미검증" in body, "현재상태 미검증 표시가 없다"


def test_authority_is_never_inferred_from_label_or_date() -> None:
    """label·날짜로 승인을 추정하는 분기가 생기면 잡는다.

    `doc_chunks.label` 에는 승인 정보가 없다. 날짜만으로 승인 포인터를
    바꾸는 것도 금지돼 있다. 따라서 authority_status 는 조건 없이 고정값이어야 한다.
    """
    src = AUTO_RAG.read_text(encoding="utf-8")
    body = src.split('"kind": "doc",', 1)[1].split("return out", 1)[0]
    authority_line = next(
        line for line in body.splitlines() if "authority_status" in line
    )
    assert authority_line.strip() == '"authority_status": "승인미확인",', (
        f"authority_status 가 고정값이 아니다: {authority_line.strip()!r}"
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime.datetime(2026, 4, 7, 10, 23), "2026-04-07"),
        (datetime.date(2026, 9, 18), "2026-09-18"),
        ("2026-09-18 10:49:12+09", "2026-09-18"),
        (None, ""),
        ("", ""),
        # 경로가 다시 흘러들면 날짜가 아니라 빈 값이 돼야 한다.
        ("/root/aads/docs/foo.md", ""),
        ("C:\\docs\\foo.md", ""),
    ],
)
def test_format_doc_stamp(value, expected) -> None:
    assert _load_format_doc_stamp()(value) == expected


def test_stamp_prefers_document_mtime_over_index_time() -> None:
    """indexed_at 은 색인한 시각이라 문서의 나이를 말해 주지 않는다."""
    src = AUTO_RAG.read_text(encoding="utf-8")
    body = src.split('"kind": "doc",', 1)[1]
    head = src.split('"kind": "doc",', 1)[0]
    assert "stamp_source = doc_mtime or indexed_at" in head, (
        "문서 mtime 을 1순위로 쓰지 않는다"
    )
    assert '"timestamp": stamp' in body
