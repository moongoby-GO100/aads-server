"""승인 발췌 예산·정제·미검증 분리 회귀 검사."""
import asyncio
import json

import pytest

from app.services import doc_review_rubric as rubric


@pytest.mark.parametrize("boundary", ["share", "limit", "fallback"])
def test_secret_is_sanitized_before_excerpt_boundaries(boundary):
    limit = 600
    token_prefix = "-".join(("sk", "ABCDEFG"))
    secret = token_prefix + "12345678901234567890"
    heading = "## 목표\n"
    share = (limit - 3) // 4
    cut = share + (limit - 3) % 4 if boundary == "share" else limit
    prefix = heading if boundary != "fallback" else ""
    body = prefix + "x" * (cut - len(prefix) - len(token_prefix) - 1) + " " + secret + " y"
    if boundary == "share":
        body += "\n## 범위\n" + "z" * 300 + "\n## 완료기준\n" + "z" * 300 + "\n## 결정\n" + "z" * 300
    # 실제 원문 자르기는 검출 불가능한 접두를 남긴다.
    assert body[:cut].endswith(token_prefix)
    excerpt = rubric.approved_excerpt(body, limit)
    assert token_prefix not in excerpt
    assert len(excerpt) <= limit


def test_priority_sections_share_budget_and_keep_all_titles():
    body = "\n".join("## " + title + "\n" + "x" * 1000 for title in ("목표", "범위", "완료기준", "결정"))
    excerpt = rubric.approved_excerpt(body)
    assert len(excerpt) <= 600
    for title in ("목표", "범위", "완료기준", "결정"):
        assert "## " + title in excerpt


def test_excerpt_fails_closed(monkeypatch):
    monkeypatch.setattr(rubric, "_sanitize", lambda text: text)
    assert rubric.approved_excerpt("-".join(("sk", "ABCDEFG" + "12345678901234567890"))) == ""


def test_approved_titles_metadata_budget_and_self_exclusion():
    from app.services.doc_cross_review import _approved_titles
    secret = "-".join(("sk", "ant", "oat01", "Z" * 16))
    class Conn:
        async def fetch(self, *_args):
            return [dict(document_key=str(i), kind="plan", title="제목", version="1", revision_id="revision",
                         content_hash="hash", excerpt=secret + " " + "x" * 1000) for i in range(8)]
    rows = asyncio.run(_approved_titles(Conn(), "tenant", "AADS", {"document_key": "0"}))
    assert secret not in json.dumps(rows)
    assert len(rows) == 7
    assert sum(len(row["excerpt"]) for row in rows) <= 3000
    assert all(len(row["excerpt"]) <= 600 for row in rows)
    assert set(rows[0]) == {"document_key", "kind", "title", "version", "revision_id", "content_hash", "excerpt"}


def test_approved_titles_query_failure_is_empty():
    from app.services.doc_cross_review import _approved_titles
    class Conn:
        async def fetch(self, *_args):
            raise RuntimeError("unavailable")
    assert asyncio.run(_approved_titles(Conn(), "tenant", "AADS", {})) == []


def test_unverified_optional_validated_and_separate():
    data = dict(verdict="pass", summary="요약", ceo_brief="설명", scores=dict.fromkeys(rubric.SCORE_KEYS, 1), score=1, issues=[])
    assert rubric.validate_structured(data)[0]
    data["unverified"] = [dict(criterion="정합성", location="승인 발췌", problem="확인 불가", needed="전체 문서")]
    assert rubric.validate_structured(data)[0]
    verdict, reason = rubric.finalize_review(data, [])
    head, parsed = rubric.parse_review_reason(reason)
    assert verdict == "pass" and parsed["issues"] == []
    assert "미검증 1건" in head
    assert len(reason) <= rubric.REASON_LIMIT
    data["unverified"][0]["needed"] = "x" * 601
    assert not rubric.validate_structured(data)[0]


def test_many_unverified_fit_reason_budget():
    data = dict(verdict="pass", summary="요약", ceo_brief="설명", scores=dict.fromkeys(rubric.SCORE_KEYS, 1), score=1, issues=[],
                unverified=[dict(criterion="기준", location="위치", problem="x" * 600, needed="y" * 600) for _ in range(30)])
    _, reason = rubric.finalize_review(data, [])
    assert len(reason) <= rubric.REASON_LIMIT
    assert "미검증 30건" in reason
    assert json.loads(reason.split(rubric.REVIEW_MARKER)[1])["unverified_count"] == 30
