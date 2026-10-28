"""Canonical document M1 isolation and contract checks without a live database."""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs

TENANT = "00000000-0000-0000-0000-000000000001"


def context(role="member"):
    return {"tenant": {"id": TENANT}, "user": {"id": "user-1", "auth_type": "human_session"}, "membership": {"role": role, "status": "active"}}


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
        self.lookups = []

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
        if "FROM chat_sessions" in sql or "FROM goal_task_links" in sql or "FROM pipeline_jobs" in sql:
            self.lookups.append(sql)
            return 1
        if "max(revision)" in sql:
            return 1
        raise AssertionError(sql)


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def request(expected=1, **over):
    fields = dict(document_key="requirements", kind="prd", title="PRD", version="1.0.0", content="safe text",
                  expected_generation=expected, source_session_id=SESSION_ID, change_summary="요구사항 정본 등록")
    fields.update(over)
    return docs.RevisionInput(**fields)


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


@pytest.mark.parametrize("user,status", [("system:pipeline-runner", "active"), ("user-1", "removed")])
def test_approval_rejects_nonhuman_or_removed_before_writes(monkeypatch, user, status):
    conn = StubConnection()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    ctx = context("admin")
    ctx["user"]["id"] = user
    ctx["membership"]["status"] = status
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.approve_document("AADS", "prd", docs.DecisionInput(
            revision_id="00000000-0000-0000-0000-000000000002", expected_generation=1), ctx))
    assert error.value.status_code == 403
    assert conn.writes == []


def test_human_approval_and_duplicate_audit(monkeypatch):
    from uuid import UUID
    rid = UUID("00000000-0000-0000-0000-000000000002")
    class Conn(StubConnection):
        approved = None
        async def fetchrow(self, sql, *args):
            return {"id": "head-1", "generation": 1, "latest_revision_id": rid,
                    "approved_revision_id": self.approved}
        async def fetchval(self, sql, *args):
            return "review"
        async def execute(self, sql, *args):
            self.writes.append((sql, args))
            if sql.startswith("UPDATE"):
                self.approved = rid
    conn = Conn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = docs.DecisionInput(revision_id=rid, expected_generation=1)
    assert asyncio.run(docs.approve_document("AADS", "prd", body, context("admin")))["idempotent"] is False
    assert conn.writes[-1][1][-1] == "user-1"
    assert asyncio.run(docs.approve_document("AADS", "prd", body, context("admin")))["idempotent"] is True
    assert len(conn.writes) == 2


@pytest.mark.parametrize("tenant", [TENANT, "00000000-0000-0000-0000-000000000099"])
def test_approval_denies_missing_or_cross_tenant_grant(monkeypatch, tenant):
    class Conn(StubConnection):
        async def fetchval(self, sql, *args):
            assert args[:3] == (tenant, "AADS", "user-1")
            assert args[3] == ["approve"]
            return False
    conn = Conn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    ctx = context()
    ctx["tenant"]["id"] = tenant
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.approve_document("AADS", "prd", docs.DecisionInput(
            revision_id="00000000-0000-0000-0000-000000000002", expected_generation=1), ctx))
    assert error.value.status_code == 403
    assert conn.writes == []


def test_lookup_recovers_after_database_failure():
    from app.services.canonical_document_tools import lookup_documents
    class Conn(StubConnection):
        fail = True
        async def fetchrow(self, sql, *args):
            if self.fail:
                raise RuntimeError("temporary database failure")
            return None
    conn = Conn()
    kwargs = dict(tenant_id=TENANT, inp={"project": "AADS", "document_key": "prd"}, pool=conn)
    with pytest.raises(RuntimeError):
        asyncio.run(lookup_documents(**kwargs))
    conn.fail = False
    assert asyncio.run(lookup_documents(**kwargs))["registered"] is False
    assert conn.writes == []


@pytest.mark.parametrize("user", [
    {"user_id": "primary", "id": "other", "auth_type": "human_session"},
    {"user_id": "primary", "auth_type": "human_session"},
])
def test_approver_identity_matches_authorization(user):
    ctx = context()
    ctx["user"] = user
    docs._require_human_approver(ctx)
    conn = GrantConnection(True)
    assert asyncio.run(docs._authorize(conn, ctx, "AADS", "approve"))[1] == "primary"
    assert conn.args[2] == "primary"


@pytest.mark.parametrize("membership", [None, {}, {"role": "owner"}, {"status": "removed"}])
def test_missing_or_inactive_membership_fails_closed(membership):
    ctx = context()
    if membership is None:
        ctx.pop("membership")
    else:
        ctx["membership"] = membership
    with pytest.raises(HTTPException) as exc:
        docs._require_human_approver(ctx)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("auth_type", [None, "service_token", "pipeline_runner", "handover_key"])
def test_nonhuman_auth_rejected_without_system_prefix(auth_type):
    ctx = context("owner")
    ctx["user"] = {"user_id": "ordinary-looking-id", "auth_type": auth_type}
    with pytest.raises(HTTPException) as exc:
        docs._require_human_approver(ctx)
    assert exc.value.detail == "human_login_required"


def test_get_document_card_reports_read_only_capability(monkeypatch):
    from uuid import UUID
    rid = UUID("00000000-0000-0000-0000-000000000002")
    class Conn(StubConnection):
        async def fetchrow(self, sql, *args):
            if "project_document_heads" in sql:
                return {"id": "head", "tenant_id": TENANT, "document_key": "prd",
                        "generation": 2, "latest_revision_id": rid, "approved_revision_id": None}
            return {"id": rid, "revision": 2, "version": "1.0.1", "title": "설계"}
        async def fetchval(self, sql, *args):
            if "project_document_grants" in sql:
                return "read" in args[3]
            return None
    monkeypatch.setattr(docs, "get_pool", lambda: Conn())
    result = asyncio.run(docs.get_document("AADS", "prd", False, context()))
    assert result["approval_card"]["can_approve"] is False
    result = asyncio.run(docs.get_document("AADS", "prd", False, context("owner")))
    assert result["approval_card"]["can_approve"] is True


def test_audit_failure_rolls_back_then_human_can_retry(monkeypatch):
    from contextlib import asynccontextmanager
    from uuid import UUID
    rid = UUID("00000000-0000-0000-0000-000000000002")
    class Conn(StubConnection):
        approved = None
        fail_audit = True
        @asynccontextmanager
        async def transaction(self):
            before = self.approved
            try:
                yield self
            except Exception:
                self.approved = before
                raise
        async def fetchrow(self, sql, *args):
            return {"id": "head", "generation": 1, "latest_revision_id": rid,
                    "approved_revision_id": self.approved}
        async def fetchval(self, sql, *args):
            return "review"
        async def execute(self, sql, *args):
            if sql.startswith("UPDATE"):
                self.approved = rid
            elif self.fail_audit:
                raise RuntimeError("audit unavailable")
    conn = Conn()
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = docs.DecisionInput(revision_id=rid, expected_generation=1)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(docs.approve_document("AADS", "prd", body, context("owner")))
    assert conn.approved is None
    conn.fail_audit = False
    assert asyncio.run(docs.approve_document("AADS", "prd", body, context("owner")))["idempotent"] is False


def _status_conn(last_action, approved=None):
    from uuid import UUID
    rid = UUID("00000000-0000-0000-0000-000000000002")

    class Conn(StubConnection):
        async def fetchrow(self, sql, *args):
            return {"id": "head-1", "generation": 1, "latest_revision_id": rid,
                    "approved_revision_id": approved}

        async def fetchval(self, sql, *args):
            assert "action IN ('review','approved','archived')" in sql
            assert args == ("head-1", rid)
            return last_action

    return Conn(), docs.DecisionInput(revision_id=rid, expected_generation=1), rid


@pytest.mark.parametrize("last_action", [None, "archived", "approved"])
def test_approval_requires_review_status(monkeypatch, last_action):
    conn, body, _ = _status_conn(last_action)
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.approve_document("AADS", "prd", body, context("admin")))
    assert error.value.status_code == 409
    assert error.value.detail == "revision_not_in_review"
    assert conn.writes == []


def test_approval_succeeds_after_review(monkeypatch):
    conn, body, rid = _status_conn("review")
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    result = asyncio.run(docs.approve_document("AADS", "prd", body, context("admin")))
    assert result == {"approved_revision_id": rid, "idempotent": False}
    assert any("UPDATE project_document_heads" in sql for sql in conn.writes)
    assert any("INSERT INTO project_document_events" in sql for sql in conn.writes)


def test_already_approved_revision_is_idempotent_without_status_lookup(monkeypatch):
    from uuid import UUID
    rid = UUID("00000000-0000-0000-0000-000000000002")
    conn, body, _ = _status_conn("archived", approved=rid)
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    result = asyncio.run(docs.approve_document("AADS", "prd", body, context("admin")))
    assert result == {"approved_revision_id": rid, "idempotent": True}
    assert conn.writes == []


PROVENANCE_REASON = "작성 출처(세션 또는 러너)와 작성 사유가 필요합니다"


@pytest.mark.parametrize("over", [
    {"source_session_id": None},
    {"change_summary": None},
    {"change_summary": "   "},
    {"source_session_id": None, "source_task_id": "  ", "change_summary": None},
])
def test_registration_without_provenance_or_reason_is_422_and_writes_nothing(monkeypatch, over):
    conn = StubConnection(generation=0)
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.create_revision("AADS", request(**over), context("admin")))
    assert error.value.status_code == 422
    assert error.value.detail == PROVENANCE_REASON
    assert conn.writes == [] and conn.lookups == []


def test_authorization_failure_wins_over_provenance_422(monkeypatch):
    class Denied(StubConnection):
        async def fetchval(self, sql, *args):
            return False

    monkeypatch.setattr(docs, "get_pool", lambda: Denied())
    with pytest.raises(HTTPException) as error:
        asyncio.run(docs.create_revision("AADS", request(source_session_id=None), context("member")))
    assert error.value.status_code == 403


def test_repository_import_with_source_path_is_allowed_without_session_or_task(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "plan.md").write_text("plan body")
    monkeypatch.setattr(docs, "ROOT", root)

    async def no_origin(source):
        raise HTTPException(422, "invalid_source_path")

    monkeypatch.setattr(docs, "_read_origin_source", no_origin)
    conn = StubConnection(generation=0)
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = request(expected=0, content="", source_path="docs/plan.md", source_session_id=None, change_summary=None)
    with pytest.raises(AssertionError) as error:  # 출처 검사는 통과하고 StubConnection 이 INSERT 의 fetchrow 에서 멈춘다
        asyncio.run(docs.create_revision("AADS", body, context("admin")))
    assert "INSERT INTO project_document_revisions" in str(error.value)


def test_provenance_problem_contract():
    ok = dict(source_session_id=SESSION_ID, source_task_id=None, source_path=None, change_summary="사유")
    assert docs.provenance_problem(**ok) is None
    assert docs.provenance_problem(**{**ok, "source_session_id": None, "source_task_id": "runner-1a2b3c4d"}) is None
    assert docs.provenance_problem(**{**ok, "source_session_id": None}) == PROVENANCE_REASON
    assert docs.provenance_problem(**{**ok, "change_summary": ""}) == PROVENANCE_REASON
    assert docs.provenance_problem(**{**ok, "source_session_id": None, "change_summary": None, "source_path": "docs/a.md"}) is None


def test_session_and_runner_job_provenance_are_validated_and_stored(monkeypatch):
    conn = StubConnection(generation=0)
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    with pytest.raises(AssertionError) as error:
        asyncio.run(docs.create_revision(
            "AADS", request(expected=0, source_task_id="runner-1a2b3c4d"), context("admin")))
    assert "INSERT INTO project_document_revisions" in str(error.value)
    assert any("FROM chat_sessions" in sql and "upper(w.project_key)" in sql for sql in conn.lookups)
    assert any("FROM goal_task_links" in sql for sql in conn.lookups)


def test_runner_job_not_linked_to_goal_is_accepted_via_pipeline_jobs():
    calls = []

    class Conn:
        async def fetchval(self, sql, *args):
            calls.append(sql)
            return 1 if "FROM pipeline_jobs" in sql else None

        async def execute(self, sql, *args):
            pass

        async def fetchrow(self, sql, *args):
            return {"id": "head-1", "kind": "prd", "generation": 1}

        async def fetch(self, sql, *args):
            return [{"id": "r1", "content_hash": docs._body("safe text", None)[2], "version": "1.0.0", "idempotency_key": None}]

    result = asyncio.run(docs.create_revision_in_tx(
        Conn(), TENANT, "runner", "AADS",
        request(expected=1, source_session_id=None, source_task_id="runner-1a2b3c4d")))
    assert result["idempotent"] is True
    assert any("FROM goal_task_links" in sql for sql in calls) and any("FROM pipeline_jobs" in sql for sql in calls)
