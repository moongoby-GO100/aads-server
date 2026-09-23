"""Canonical document M1 isolation and contract checks without a live database."""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs

TENANT = "00000000-0000-0000-0000-000000000001"


def context(role="member"):
    return {"tenant": {"id": TENANT}, "user": {"id": "user-1"}, "membership": {"role": role}}


class GrantConnection:
    def __init__(self, grant=False):
        self.grant = grant
        self.args = None

    async def fetchval(self, sql, *args):
        self.args = args
        return self.grant


def test_project_grant_requires_exact_tenant_project_and_user():
    conn = GrantConnection(False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs._authorize(conn, context(), "AADS", "read"))
    assert error.value.status_code == 403
    assert conn.args[:3] == (TENANT, "AADS", "user-1")
    conn.grant = True
    assert asyncio.run(docs._authorize(conn, context(), "AADS", "write")) == (TENANT, "user-1")
    assert conn.args[3] == ["write", "approve"]


def test_source_path_rejects_traversal_url_and_symlink(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "plan.md").write_text("plan")
    (root / "docs" / "escape.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("outside")
    monkeypatch.setattr(docs, "ROOT", root)
    assert docs._body("", "docs/plan.md")[0] == "plan"
    for path in ("../outside.md", "https://example.com/plan.md", "/tmp/outside.md",
                 "docs/escape.md", "docs/missing.md", ".env"):
        with pytest.raises(HTTPException):
            docs._body("", path)


def test_rejects_credentials_and_oversize_content():
    for content in ("password=supersecretvalue", '{"password": "abc"}',
                    "-----BEGIN PRIVATE KEY-----abc",
                    "sk-exampletokenabcdefghijklmnop", "gho_" + "a" * 24,
                    "xoxb-" + "a" * 24):
        with pytest.raises(HTTPException) as error:
            docs._body(content, None)
        assert error.value.detail == "credential_content_rejected"
    with pytest.raises(HTTPException) as error:
        docs._body("a" * 262145, None)
    assert error.value.status_code == 413


def test_oversize_source_is_rejected_before_read(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    source = root / "docs" / "large.md"
    source.write_bytes(b"x" * (docs.MAX_DOCUMENT_BYTES + 1))
    monkeypatch.setattr(docs, "ROOT", root)
    with pytest.raises(HTTPException) as error:
        docs._body("", "docs/large.md")
    assert error.value.status_code == 413


def test_identity_validation_and_duplicate_body_hash():
    with pytest.raises(ValueError):
        docs.RevisionInput(document_key="../bad", kind="prd", title="x", version="1.0.0", content="one", expected_generation=0)
    with pytest.raises(ValueError):
        docs.RevisionInput(document_key="prd", kind="prd", title="x", version="latest", content="one", expected_generation=0)
    assert docs._body("same", None)[2] == docs._body("same", None)[2]


class StubConnection:
    def __init__(self, generation=1, matches=()):
        self.generation = generation
        self.matches = matches
        self.writes = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, *args):
        self.writes.append(sql)

    async def fetchrow(self, sql, *args):
        if "FROM project_document_heads" in sql:
            return {"id": "head-1", "kind": "prd", "generation": self.generation}
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        return self.matches

    async def fetchval(self, sql, *args):
        raise AssertionError(sql)


def request(expected=1):
    return docs.RevisionInput(document_key="requirements", kind="prd", title="PRD",
                              version="1.0.0", content="safe text", expected_generation=expected)


def test_stale_generation_preserves_pointers(monkeypatch):
    conn = StubConnection(generation=2)
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.create_revision("AADS", request(), context("admin")))
    assert error.value.detail == "generation_conflict"
    assert not any("UPDATE project_document_heads" in sql for sql in conn.writes)
    assert not any("INSERT INTO project_document_revisions" in sql for sql in conn.writes)


def test_duplicate_content_returns_existing_revision(monkeypatch):
    digest = docs._body("safe text", None)[2]
    conn = StubConnection(matches=[{"id": "revision-1", "content_hash": digest, "version": "1.0.0", "idempotency_key": None}])
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    result = asyncio.run(docs.create_revision("AADS", request(expected=0), context("admin")))
    assert result["idempotent"] is True
    assert not any("INSERT INTO project_document_revisions" in sql for sql in conn.writes)


def test_duplicate_content_cannot_satisfy_different_version(monkeypatch):
    digest = docs._body("safe text", None)[2]
    conn = StubConnection(matches=[
        {"id": "old", "content_hash": digest, "version": "0.9.0", "idempotency_key": None},
        {"id": "current", "content_hash": "different", "version": "1.0.0", "idempotency_key": None},
    ])
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.create_revision("AADS", request(), context("admin")))
    assert error.value.detail == "revision_conflict"
    assert not any("INSERT INTO project_document_revisions" in sql for sql in conn.writes)


def test_brief_only_reads_approved_pointer_and_is_bounded():
    class BriefConn:
        async def fetch(self, sql, *args):
            assert "r.id=h.approved_revision_id" in sql
            assert "h.tenant_id=$1::uuid AND h.project_key=$2" in sql
            assert args == (TENANT, "AADS", 8)
            return [{"document_key": "plan", "excerpt": "approved"}]

    assert asyncio.run(docs.approved_brief(BriefConn(), TENANT, "AADS"))[0]["excerpt"] == "approved"
    with pytest.raises(ValueError):
        asyncio.run(docs.approved_brief(BriefConn(), TENANT, "AADS", 21))


def test_approved_search_uses_selected_revision_and_scopes_tenant_project(monkeypatch):
    class SearchConn(StubConnection):
        async def fetch(self, sql, *args):
            assert "selected.id=CASE WHEN $5::bool THEN h.approved_revision_id" in sql
            assert "selected.content ILIKE" in sql
            assert "h.tenant_id=$1::uuid AND h.project_key=$2" in sql
            assert args[:2] == (TENANT, "AADS")
            assert args[4] is True
            return []

    conn = SearchConn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    result = asyncio.run(docs.list_documents("aads", q="requirements", approved_only=True,
                                             context=context("admin")))
    assert result == {"documents": []}


def test_failed_approval_keeps_existing_approved_pointer(monkeypatch):
    class ApprovalConn(StubConnection):
        async def fetchrow(self, sql, *args):
            return {"id": "head-1", "generation": 4, "latest_revision_id": "new-revision",
                    "approved_revision_id": "old-revision"}

    conn = ApprovalConn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = docs.DecisionInput(revision_id="00000000-0000-0000-0000-000000000002", expected_generation=3)
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.approve_document("AADS", "prd", body, context("admin")))
    assert error.value.detail == "generation_conflict"
    assert conn.writes == []


def test_goal_link_requires_same_tenant_and_project(monkeypatch):
    class GoalConn(StubConnection):
        async def fetchval(self, sql, *args):
            assert "tenant_id=$2::uuid AND project=$3" in sql
            assert args[1:] == (TENANT, "AADS")
            return None

    conn = GoalConn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = docs.GoalLinkInput(goal_id="00000000-0000-0000-0000-000000000003")
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.link_goal("AADS", "prd", body, context("admin")))
    assert error.value.detail == "goal_not_found"
    assert conn.writes == []


def test_legacy_goal_document_link_validates_identity_and_hash(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "prd.md").write_text("safe source", encoding="utf-8")
    monkeypatch.setattr(docs, "ROOT", root)
    revision_id = "00000000-0000-0000-0000-000000000004"
    goal_id = "00000000-0000-0000-0000-000000000005"

    class LinkConn(StubConnection):
        version = "1.0.0"
        digest = docs._body("safe source", None)[2]

        async def fetchrow(self, sql, *args):
            if "FROM project_document_heads" in sql:
                return {"id": "head-1", "kind": "prd"}
            if "FROM project_document_revisions" in sql:
                return {"id": revision_id, "version": "1.0.0", "source_path": "docs/prd.md",
                        "content_hash": self.digest}
            if "FROM goal_documents" in sql:
                assert args[1:] == (TENANT, "AADS")
                return {"id": 12, "goal_id": goal_id, "kind": "prd", "doc_path": "docs/prd.md",
                        "version": self.version}
            if "FROM project_document_legacy_links" in sql:
                return None
            raise AssertionError(sql)

    conn = LinkConn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = docs.LegacyLinkInput(revision_id=revision_id, goal_document_id=12)
    assert asyncio.run(docs.link_legacy_goal_document("AADS", "prd", body, context("admin")))["linked"]
    assert any("INSERT INTO project_document_legacy_links" in sql for sql in conn.writes)
    conn.writes.clear()
    conn.version = "2.0.0"
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.link_legacy_goal_document("AADS", "prd", body, context("admin")))
    assert error.value.detail == "legacy_document_identity_mismatch"
    assert conn.writes == []
