"""R-DOC: 도구 반환 계약(view/report_line/draft only)과 파일 등록 경로(host 우선·경계 유지)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from app.services import canonical_document_tools as cdt
from tests.unit.test_canonical_document_tools import (  # noqa: F401  (autouse 게이트 스텁 포함)
    FakeConn,
    FakePool,
    SESSION,
    TENANT,
    _head,
    _inp,
    _register,
    _stub_gate,
)


class NewDocConn(FakeConn):
    """처음엔 head 가 없고, heads INSERT 이후에 생긴다(신규 문서 등록 흐름)."""

    async def execute(self, sql, *args):
        await super().execute(sql, *args)
        if "INSERT INTO project_document_heads" in sql:
            self.head = _head(generation=0)


def test_register_returns_viewable_reference_not_raw_path():
    out = _register(NewDocConn(), _inp())
    assert out["registered"] is True and out["status"] == "draft" and out["approved"] is False
    view = out["view"]
    assert view["document_key"] == "ovis-spec" and view["revision"] == 1
    assert view["api_path"].endswith("/documents/ovis-spec/content?revision=1")
    assert view["viewer_url"] is None  # 딥링크가 없으므로 거짓 링크를 만들지 않는다
    assert "「오비스 명세」" in out["report_line"] and "document_key=ovis-spec" in out["report_line"]
    assert "/app/" not in out["report_line"] and "/root/" not in out["report_line"]


def test_register_never_touches_approved_revision():
    conn = NewDocConn()
    _register(conn, _inp())
    assert not any("approved_revision_id" in s and "UPDATE" in s.upper() for s in conn.sql)


def test_new_document_with_english_title_gets_warning_but_is_not_blocked():
    out = _register(NewDocConn(), _inp(title="Spec", content="# Spec\nbody"))
    assert out["registered"] is True
    assert len(out["rdoc_warnings"]) == 2


def test_existing_document_is_not_warned_retroactively():
    out = _register(FakeConn(head=_head(generation=1), revisions=1), _inp(title="Spec", content="# Spec\nx"))
    assert out["registered"] is True and "rdoc_warnings" not in out


def test_existing_non_hangul_key_rules_unchanged():
    assert cdt.validate_document_key("Ovis_Spec") is not None
    assert cdt.validate_document_key("ovis-spec") is None


def test_lookup_by_key_carries_view_ref():
    conn = FakeConn(head=_head(generation=1))
    out = asyncio.run(cdt.lookup_documents(tenant_id=TENANT, inp={"project": "aads", "document_key": "ovis-spec"}, pool=FakePool(conn)))
    assert out["registered"] is True
    assert out["view"]["kind"] == "canonical" and out["view"]["status"] == "draft"


# ── _locate_source: host 우선, 이미지 폴백, 경계 유지 ─────────────────


@pytest.fixture
def roots(tmp_path, monkeypatch):
    host, image = tmp_path / "host", tmp_path / "image"
    for r in (host, image):
        (r / "reports").mkdir(parents=True)
    monkeypatch.setattr(docs, "ROOT", image)
    monkeypatch.setattr(docs, "_DEFAULT_ROOT", image)
    monkeypatch.setattr(docs, "HOST_SOURCE_ROOT", host)
    return host, image


def test_locate_prefers_host_copy(roots):
    host, image = roots
    (host / "reports" / "a.md").write_text("host")
    (image / "reports" / "a.md").write_text("image")
    assert docs._locate_source(docs.Path("reports/a.md")) == (host / "reports" / "a.md").resolve()


def test_locate_falls_back_to_image_for_legacy(roots):
    _, image = roots
    (image / "reports" / "old.md").write_text("old")
    assert docs._locate_source(docs.Path("reports/old.md")) == (image / "reports" / "old.md").resolve()


def test_locate_rejects_symlink_escape(roots, tmp_path):
    host, _ = roots
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    (host / "reports" / "l.md").symlink_to(outside)
    with pytest.raises(HTTPException) as exc:
        docs._locate_source(docs.Path("reports/l.md"))
    assert exc.value.status_code == 422


def test_safe_source_still_rejects_escape_and_non_doc_roots(roots):
    for bad in ("../x.md", "reports/../../x.md", "/etc/passwd", "app/x.py", "reports/.hidden.md"):
        with pytest.raises(HTTPException):
            docs._safe_source(bad)
