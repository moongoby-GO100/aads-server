"""정본 DB 열람 API·열람 참조 계약·R-DOC 준수 판정(AADS-RDOC-UNIFIED-STORAGE-20261006). DB 없이 돈다."""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from app.services import document_refs as refs

TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"
KEY = "rdoc-unified-storage-report"
TITLE = "문서 저장 통합 결과"


def ctx(tenant=TENANT_A, role="member", user="user-1"):
    return {"tenant": {"id": tenant}, "user": {"id": user}, "membership": {"role": role}}


class FakeDb:
    """tenant/project/key 로 격리된 헤드·리비전 저장소를 흉내낸다."""

    def __init__(self, grants=()):
        self.grants = set(grants)
        self.heads: dict[tuple[str, str, str], dict] = {}
        self.revisions: dict[str, dict] = {}
        self.events: dict[str, str] = {}
        self.file_reads = 0

    def add(self, tenant, project, key, *, revisions, approved=None):
        head = {"id": f"head-{len(self.heads)}", "tenant_id": tenant, "project_key": project,
                "document_key": key, "approved_revision_id": None, "latest_revision_id": None}
        for n, content in enumerate(revisions, start=1):
            rid = f"{head['id']}-r{n}"
            self.revisions[rid] = {
                "id": rid, "head_id": head["id"], "tenant_id": tenant, "project_key": project,
                "revision": n, "version": f"1.0.{n}", "title": TITLE, "content": content,
                "content_hash": f"h{n}", "source_path": None,
            }
            head["latest_revision_id"] = rid
        if approved:
            head["approved_revision_id"] = f"{head['id']}-r{approved}"
            self.events[head["approved_revision_id"]] = "approved"
        self.heads[(tenant, project, key)] = head
        return head

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchval(self, sql, *args):
        if "project_document_grants" in sql:
            tenant, project, user = args[:3]
            return (tenant, project, user) in self.grants
        if "project_document_events" in sql:
            return self.events.get(args[0])
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "FROM project_document_heads" in sql:
            return self.heads.get((args[0], args[1], args[2]))
        if "FROM project_document_revisions" in sql:
            if "revision=$4" in sql:
                head_id, tenant, project, revision = args
                return next((r for r in self.revisions.values()
                             if r["head_id"] == head_id and r["tenant_id"] == tenant
                             and r["project_key"] == project and r["revision"] == revision), None)
            head_id, rid = args
            row = self.revisions.get(rid)
            return row if row and row["head_id"] == head_id else None
        raise AssertionError(sql)


@pytest.fixture
def db(monkeypatch, tmp_path):
    fake = FakeDb(grants={(TENANT_A, "AADS", "user-1")})
    monkeypatch.setattr(docs, "get_pool", lambda: fake)
    monkeypatch.setattr(docs, "ROOT", tmp_path / "no-such-repo")  # 파일 경로 경로를 타면 실패한다
    return fake


def call(project, key, ctxt, revision=None, approved_only=False):
    return asyncio.run(docs.get_document_content(
        project, key, revision=revision, approved_only=approved_only, context=ctxt))


def test_canonical_content_is_served_from_db_only(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["# 한글 제목\n첫판", "# 한글 제목\n둘째판"])

    res = call("aads", KEY, ctx())

    assert res["content"].endswith("둘째판")
    assert res["canonical"]["revision"] == 2
    assert res["canonical"]["status"] == "draft"
    assert res["canonical"]["authoritative"] is False
    assert res["encoding"] == "text" and res["is_binary"] is False
    assert res["view"]["api_path"].endswith(f"/documents/{KEY}/content?revision=2")


def test_specific_revision_and_approved_only(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["v1 본문", "v2 본문"], approved=1)

    assert call("AADS", KEY, ctx(), revision=1)["content"] == "v1 본문"
    approved = call("AADS", KEY, ctx(), approved_only=True)
    assert approved["content"] == "v1 본문"
    assert approved["canonical"]["authoritative"] is True
    assert call("AADS", KEY, ctx())["canonical"]["authoritative"] is False  # 최신(초안) 은 승인본이 아니다


def test_approved_only_without_approval_is_404_not_latest_draft(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["초안만 있음"])

    with pytest.raises(HTTPException) as exc:
        call("AADS", KEY, ctx(), approved_only=True)

    assert exc.value.status_code == 404


def test_missing_revision_is_404(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["본문"])
    with pytest.raises(HTTPException) as exc:
        call("AADS", KEY, ctx(), revision=9)
    assert exc.value.status_code == 404


def test_other_tenant_cannot_read(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["A 테넌트 비밀 계획"])
    db.grants.add((TENANT_B, "AADS", "user-1"))

    with pytest.raises(HTTPException) as exc:
        call("AADS", KEY, ctx(tenant=TENANT_B))

    assert exc.value.status_code == 404  # 존재 여부도 새지 않는다


def test_other_project_document_is_not_reachable_through_my_project(db):
    db.add(TENANT_A, "KIS", KEY, revisions=["KIS 전용"])

    with pytest.raises(HTTPException) as exc:
        call("AADS", KEY, ctx())

    assert exc.value.status_code == 404


def test_member_without_project_grant_is_denied(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["본문"])

    with pytest.raises(HTTPException) as exc:
        call("AADS", KEY, ctx(user="stranger"))

    assert exc.value.status_code == 403


def test_admin_is_elevated_within_own_tenant_only(db):
    db.add(TENANT_A, "AADS", KEY, revisions=["본문"])
    assert call("AADS", KEY, ctx(role="admin", user="boss"))["content"] == "본문"
    with pytest.raises(HTTPException) as exc:
        call("AADS", KEY, ctx(tenant=TENANT_B, role="admin", user="boss"))
    assert exc.value.status_code == 404


def test_invalid_project_key_is_rejected(db):
    with pytest.raises(HTTPException) as exc:
        call("../etc", KEY, ctx())
    assert exc.value.status_code == 422


def test_existing_document_key_is_preserved_on_new_revision_view(db):
    db.add(TENANT_A, "AADS", "legacy_Key.v1:x", revisions=["옛 문서"])
    res = call("AADS", "legacy_Key.v1:x", ctx())
    assert res["document_key"] == "legacy_Key.v1:x"
    assert quote("legacy_Key.v1:x", safe="") in res["view"]["api_path"]


def test_content_route_is_registered_before_history_and_resolves():
    paths = [r.path for r in docs.router.routes]
    assert any(p.endswith("/{document_key}/content") for p in paths)


# ── 열람 참조 계약 ─────────────────────────────────────────────

def test_canonical_view_ref_never_fakes_a_viewer_link():
    ref = refs.canonical_view_ref("aads", KEY, title=TITLE, revision=3)

    assert ref["viewer_url"] is None
    assert ref["api_path"] == f"/api/v1/projects/AADS/documents/{KEY}/content?revision=3"
    assert TITLE in ref["report_line"] and KEY in ref["report_line"] and "초안" in ref["report_line"]
    assert "승인" not in ref["report_line"]  # 등록은 초안일 뿐 승인으로 보고하지 않는다


def test_canonical_view_ref_marks_approved_only_when_authoritative():
    assert "승인됨" in refs.canonical_view_ref("AADS", KEY, title=TITLE, authoritative=True)["report_line"]


def test_file_view_ref_uses_real_viewer_url_with_korean_and_space_encoding():
    name = "20261006_AADS_문서 저장 통합 결과.md"
    ref = refs.file_view_ref(f"/root/aads/aads-server/reports/{name}")

    assert ref["viewer_url"].startswith("/docs?")
    assert "project=AADS" in ref["viewer_url"] and "base_path=%2Fapp%2Freports" in ref["viewer_url"]
    assert quote(name, safe="") in ref["viewer_url"].replace("+", "%20")
    assert ref["report_line"].startswith("[") and ref["viewer_url"] in ref["report_line"]


def test_file_view_ref_for_host_mount_path_is_viewable():
    ref = refs.file_view_ref("/host/aads-server/reports/20261006_AADS_결과.md")
    assert ref["viewer_url"] and "base_path=%2Fapp%2Freports" in ref["viewer_url"]


def test_file_view_ref_for_unknown_path_says_no_link_instead_of_raw_path_link():
    ref = refs.file_view_ref("/srv/elsewhere/secret.md")
    assert ref["viewer_url"] is None
    assert "열람 링크 미제공" in ref["report_line"]
    assert "](" not in ref["report_line"]


# ── R-DOC 준수 판정 ────────────────────────────────────────────

def test_unregistered_new_report_is_marked_incomplete():
    res = refs.evaluate_rdoc_compliance(["reports/20261006_AADS_결과.md", "app/api/x.py"])

    assert res["status"] == "incomplete"
    assert res["missing"] == ["reports/20261006_AADS_결과.md"]
    assert res["marker"] == refs.INCOMPLETE_MARK
    assert refs.INCOMPLETE_MARK in refs.format_rdoc_lines(res)[0]


def test_registered_report_is_complete_and_matches_by_path_or_basename():
    by_path = refs.evaluate_rdoc_compliance(["reports/20261006_AADS_결과.md"], ["reports/20261006_AADS_결과.md"])
    by_name = refs.evaluate_rdoc_compliance(["/root/aads/aads-server/reports/20261006_AADS_결과.md"],
                                            ["reports/20261006_AADS_결과.md"])
    assert by_path["status"] == "complete" and by_name["status"] == "complete"
    assert by_path["marker"] is None


def test_code_only_change_is_not_applicable_and_adds_no_report_lines():
    res = refs.evaluate_rdoc_compliance(["app/api/x.py", "tests/unit/test_x.py", "scripts/a.sh"])
    assert res["status"] == "not_applicable"
    assert refs.format_rdoc_lines(res) == []


def test_existing_docs_are_not_retroactively_blocked_when_added_files_given():
    res = refs.evaluate_rdoc_compliance(["docs/old-plan.md"], [], added_files=[])
    assert res["status"] == "not_applicable"


@pytest.mark.parametrize("path", [
    "docs/HANDOVER.md", "docs/shared-lessons/L-1.md", "docs/knowledge/AADS-KNOWLEDGE.md", "docs/_index.json",
])
def test_exempt_documents_are_not_flagged(path):
    assert refs.is_new_document_path(path) is False


def test_english_filename_is_a_warning_not_a_block():
    res = refs.evaluate_rdoc_compliance(["reports/20261006_AADS_english-title.md"], ["reports/20261006_AADS_english-title.md"])
    assert res["status"] == "complete"
    assert res["warnings"] and "한글" in res["warnings"][0]


def test_korean_title_part_has_no_warning():
    res = refs.evaluate_rdoc_compliance(["reports/20261006_AADS_문서 저장 결과.md"], ["reports/20261006_AADS_문서 저장 결과.md"])
    assert res["warnings"] == []


def test_prompt_block_is_project_agnostic_and_path_free():
    block = refs.RDOC_PROMPT_BLOCK
    for token in ("한글", "document_key", "초안", "미완료"):
        assert token in block
    assert "/root/" not in block and "AADS" not in block  # 모든 프로젝트 공통, 경로 미포함


def test_legacy_undated_and_evidence_files_are_not_flagged():
    for path in ("docs/AUTH_SPEC_v2.md", "docs/reports/evidence/x-20261006/VERIFICATION.md",
                 "docs/AADS-LAYOUT-001_OHVIS.md", "reports/system_prompt_full_dump_20260330.md"):
        assert refs.is_new_document_path(path) is False, path
    assert refs.is_new_document_path("docs/reports/20261006_AADS_문서 저장 결과.md") is True
