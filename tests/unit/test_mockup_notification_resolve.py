"""Decided reviews close their "review requested / approval waiting" inbox rows (in-memory fake, no DB).

The fake applies the filters the SQL binds (tenant, review, kind list, revision cut-off, read_at) so the arguments the
service passes are checked end to end; the SQL text itself is only asserted for its shape.
"""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models.mockup_review import ApproveReview, ChangeCreate, RevokeReview
from app.services import mockup_review_service as svc

TENANT = uuid4()
REVIEW = uuid4()
REV1, REV2 = uuid4(), uuid4()
NOW = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)
REVISIONS = {REV1: 1, REV2: 2}
REV_IDS = {1: REV1, 2: REV2}
RECIPIENTS = ["approver-a", "approver-b"]
HASH = "a" * 64


def head_row(**over):
    base = {"id": REVIEW, "tenant_id": TENANT, "project_key": "AADS", "title": "목업", "change_type": "new",
            "goal_id": None, "session_id": None, "status": "review_ready", "latest_revision_id": REV1,
            "approved_revision_id": None, "approval_event_id": None, "generation": 2, "pointer_generation": 0,
            "created_by": "creator"}
    return {**base, **over}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Conn:
    def __init__(self, head, notifications=None, heads=()):
        self.head = head
        self.heads = {h["id"]: h for h in (head, *heads) if h}
        self.rows = notifications if notifications is not None else []
        self.next_id = 100
        self.update_args = []

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *args):
        if sql.startswith("UPDATE ohvis_notifications"):
            tenant, review_id, kinds, revision_id = args
            self.update_args.append(args)
            cutoff = REVISIONS.get(revision_id, 2147483647)
            hit = [r for r in self.rows if r["tenant_id"] == tenant and r["review_id"] == review_id
                   and r["kind"] in kinds and r["read_at"] is None
                   and (revision_id is None or r["revision"] is None or r["revision"] <= cutoff)]
            for r in hit:
                r["read_at"] = NOW
            return f"UPDATE {len(hit)}"
        if sql.startswith("INSERT INTO ohvis_notifications"):
            tenant, _project, user, kind, review_id, revision_id, revision = args[:7]
            self.next_id += 1
            self.rows.append({"id": self.next_id, "tenant_id": tenant, "recipient_user_id": user, "kind": kind,
                              "review_id": review_id, "revision_id": revision_id, "revision": revision,
                              "read_at": None, "title": "t", "body": "", "link": "/chat", "project_key": "AADS",
                              "session_id": None, "created_at": NOW})
            return "INSERT 0 1"
        return "UPDATE 1" if sql.startswith("UPDATE") else "INSERT 0 1"

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE" in sql:
            return self.head
        if "SELECT request_hash" in sql or "mockup_review_change_requests" in sql:
            return None
        if "SELECT manifest FROM mockup_review_revisions" in sql:
            return {"manifest": json.dumps({"screens": []})}
        if "FROM mockup_review_revisions" in sql:
            return {"id": args[0], "revision": REVISIONS[args[0]], "manifest_hash": HASH, "manifest": "{}",
                    "doc_refs": "[]"}
        if "FROM chat_messages" in sql:
            return {"session_id": uuid4(), "role": "user"}
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "nextval" in sql:
            return 41
        if "SELECT revision FROM mockup_review_revisions" in sql:
            return REVISIONS[args[0]]
        if "count(*) FROM mockup_review_change_requests" in sql:
            return 1
        if sql.startswith("SELECT count(*) FROM ohvis_notifications"):
            tenant, user = args
            return sum(1 for r in self._visible(tenant, user) if r["is_unread"])
        raise AssertionError(sql)

    def _visible(self, tenant, user):
        out = []
        for r in self.rows:
            if str(r["tenant_id"]) != str(tenant) or r["recipient_user_id"] != user:
                continue
            h = self.heads.get(r["review_id"])
            settled = r["kind"] in ("review_requested", "approval_waiting") and h is not None and (
                h["status"] != "review_ready"
                or (r["revision_id"] is not None and r["revision_id"] != h["latest_revision_id"]))
            out.append({**r, "is_unread": r["read_at"] is None and not settled})
        return out

    async def fetch(self, sql, *args):
        tenant, user, unread_only, project, limit = args
        rows = [r for r in self._visible(tenant, user) if not unread_only or r["is_unread"]]
        return rows[:limit]


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    async def audience(conn, head, tags, actor):
        return list(RECIPIENTS) if "approvers" in tags else ["creator"]

    async def gate(*a, **k):
        return None

    async def scope(*a, **k):
        return None

    monkeypatch.setattr(svc, "_audience", audience)
    monkeypatch.setattr(svc, "_approval_gate", gate)
    monkeypatch.setattr(svc, "_session_scope", scope)


def run(coro):
    return asyncio.run(coro)


def request_rows(revision=1, review_id=REVIEW, read_at=None, tenant=TENANT):
    rows = []
    for user in RECIPIENTS:
        for kind in ("review_requested", "approval_waiting"):
            rows.append({"id": len(rows) + 1 + revision * 10, "tenant_id": tenant, "recipient_user_id": user,
                         "kind": kind, "review_id": review_id, "revision_id": REV_IDS[revision], "revision": revision,
                         "read_at": read_at, "title": "t", "body": "", "link": "/chat", "project_key": "AADS",
                         "session_id": None, "created_at": NOW})
    return rows


def unread_by_kind(conn, user):
    return sorted(r["kind"] for r in conn._visible(TENANT, user) if r["is_unread"])


def approve(conn):
    body = ApproveReview(idempotency_key="approve-key-1", expected_generation=2, revision_id=REV1,
                         manifest_hash=HASH, confirm=True)
    return run(svc.approve_review(conn, str(TENANT), "AADS", REVIEW, "boss", body))


def test_approve_closes_request_rows_for_every_recipient_but_keeps_the_result_row():
    conn = Conn(head_row(), request_rows())
    approve(conn)
    assert all(r["read_at"] is not None for r in conn.rows if r["kind"] in ("review_requested", "approval_waiting"))
    assert unread_by_kind(conn, "approver-a") == [] and unread_by_kind(conn, "approver-b") == []
    approved = [r for r in conn.rows if r["kind"] == "approved"]
    assert [r["recipient_user_id"] for r in approved] == ["creator"] and approved[0]["read_at"] is None
    assert unread_by_kind(conn, "creator") == ["approved"]


def test_approve_leaves_other_reviews_and_other_tenants_alone():
    other_review, other_tenant = uuid4(), uuid4()
    rows = request_rows() + request_rows(review_id=other_review) + request_rows(tenant=other_tenant)
    conn = Conn(head_row(), rows)
    approve(conn)
    still_open = [r for r in conn.rows if r["read_at"] is None and r["kind"] != "approved"]
    assert len(still_open) == 8 and all(r["review_id"] != REVIEW or r["tenant_id"] != TENANT for r in still_open)


def test_request_changes_closes_request_rows_and_keeps_change_received_unread():
    conn = Conn(head_row(), request_rows())
    body = ChangeCreate(idempotency_key="change-key-1", expected_generation=2, change_request_id=uuid4(),
                        base_revision_id=REV1, source_message_id=uuid4(), comment="버튼 색 변경")
    out = run(svc.request_changes(conn, str(TENANT), "AADS", REVIEW, "boss", True, body))
    assert out["status"] == "changes_requested"
    assert not [r for r in conn.rows if r["kind"] in ("review_requested", "approval_waiting") and r["read_at"] is None]
    received = [r for r in conn.rows if r["kind"] == "change_received"]
    assert received and all(r["read_at"] is None for r in received)


def test_request_changes_while_revising_does_not_touch_notifications():
    conn = Conn(head_row(status="revising"), request_rows())
    body = ChangeCreate(idempotency_key="change-key-2", expected_generation=2, change_request_id=uuid4(),
                        base_revision_id=REV1, source_message_id=uuid4(), comment="추가 요청")
    run(svc.request_changes(conn, str(TENANT), "AADS", REVIEW, "boss", True, body))
    assert conn.update_args == []
    assert all(r["read_at"] is None for r in conn.rows if r["kind"] in ("review_requested", "approval_waiting"))


def test_revoke_closes_leftover_request_rows_and_keeps_revoked_unread():
    head = head_row(status="approved", approved_revision_id=REV1, approval_event_id=7)
    conn = Conn(head, request_rows())
    body = RevokeReview(idempotency_key="revoke-key-1", expected_generation=2, approval_id=7, reason="철회 사유")
    run(svc.revoke_review(conn, str(TENANT), "AADS", REVIEW, "boss", body))
    assert not [r for r in conn.rows if r["kind"] in ("review_requested", "approval_waiting") and r["read_at"] is None]
    revoked = [r for r in conn.rows if r["kind"] == "revoked"]
    assert revoked and all(r["read_at"] is None for r in revoked)


def test_older_revision_decision_does_not_close_newer_revision_requests():
    conn = Conn(head_row(), request_rows(revision=1) + request_rows(revision=2))
    closed = run(svc._resolve_actionable_notifications(conn, conn.head, REV1))
    assert closed == 4
    assert all(r["read_at"] is not None for r in conn.rows if r["revision"] == 1)
    assert all(r["read_at"] is None for r in conn.rows if r["revision"] == 2)


def test_resolve_without_revision_closes_every_revision():
    conn = Conn(head_row(), request_rows(revision=1) + request_rows(revision=2))
    assert run(svc._resolve_actionable_notifications(conn, conn.head)) == 8


def test_resolve_failure_never_breaks_the_review_operation():
    class Broken(Conn):
        async def execute(self, sql, *args):
            if sql.startswith("UPDATE ohvis_notifications"):
                raise RuntimeError("boom")
            return await super().execute(sql, *args)

    conn = Broken(head_row(), request_rows())
    assert approve(conn)["status"] == "approved"


def test_legacy_request_rows_of_a_settled_head_read_as_read_and_leave_the_unread_count():
    approved = head_row(status="approved")
    conn = Conn(approved, request_rows())
    conn.rows.append({**request_rows()[0], "id": 900, "kind": "approved"})
    out = run(svc.list_notifications(conn, str(TENANT), "approver-a"))
    by_kind = {i["kind"]: i["read"] for i in out["items"]}
    assert by_kind == {"review_requested": True, "approval_waiting": True, "approved": False}
    assert out["unread"] == 1
    only = run(svc.list_notifications(conn, str(TENANT), "approver-a", unread_only=True))
    assert [i["kind"] for i in only["items"]] == ["approved"]


@pytest.mark.parametrize("status", ["approved", "changes_requested", "revoked", "revising", "draft"])
def test_request_rows_of_any_non_review_ready_head_are_not_unread(status):
    conn = Conn(head_row(status=status), request_rows())
    out = run(svc.list_notifications(conn, str(TENANT), "approver-b", unread_only=True))
    assert out == {"items": [], "unread": 0}


def test_request_rows_of_a_review_ready_head_stay_unread():
    conn = Conn(head_row(status="review_ready"), request_rows())
    out = run(svc.list_notifications(conn, str(TENANT), "approver-a", unread_only=True))
    assert sorted(i["kind"] for i in out["items"]) == ["approval_waiting", "review_requested"]
    assert out["unread"] == 2 and not any(i["read"] for i in out["items"])


def test_already_read_rows_stay_read_and_notifications_without_a_head_stay_unread():
    rows = request_rows(read_at=NOW) + request_rows(review_id=uuid4())
    conn = Conn(head_row(status="review_ready"), rows)
    out = run(svc.list_notifications(conn, str(TENANT), "approver-a"))
    assert out["unread"] == 2  # the orphan review's two rows: no head to judge by, so they are left as they were


def test_old_revision_rows_stay_read_after_the_head_returns_to_review_ready_with_a_new_revision():
    # approved head with legacy unread request rows (revision 1), then revision 2 is submitted: head is review_ready again
    conn = Conn(head_row(status="approved", latest_revision_id=REV1), request_rows(revision=1))
    assert run(svc.list_notifications(conn, str(TENANT), "approver-a", unread_only=True)) == {"items": [], "unread": 0}
    conn.head.update(status="review_ready", latest_revision_id=REV2)
    out = run(svc.list_notifications(conn, str(TENANT), "approver-a", unread_only=True))
    assert out == {"items": [], "unread": 0}
    conn.rows.extend(request_rows(revision=2))  # the new revision's own request rows are actionable
    out = run(svc.list_notifications(conn, str(TENANT), "approver-a", unread_only=True))
    assert out["unread"] == 2 and {i["revision"] for i in out["items"]} == {2}


def test_old_revision_rows_stay_read_after_resubmit_when_the_resolve_update_failed():
    class Broken(Conn):
        async def execute(self, sql, *args):
            if sql.startswith("UPDATE ohvis_notifications"):
                raise RuntimeError("boom")
            return await super().execute(sql, *args)

    conn = Broken(head_row(), request_rows(revision=1))
    approve(conn)
    assert all(r["read_at"] is None for r in conn.rows if r["revision"] == 1)  # the UPDATE really failed
    conn.head.update(status="review_ready", latest_revision_id=REV2)
    out = run(svc.list_notifications(conn, str(TENANT), "approver-a", unread_only=True))
    assert out == {"items": [], "unread": 0}


def test_list_sql_shape_joins_the_head_within_the_tenant_and_filters_on_the_same_rule():
    assert "LEFT JOIN mockup_review_heads h ON h.id=n.review_id AND h.tenant_id=n.tenant_id" in svc._NOTIF_FROM
    for needle in ("review_requested", "approval_waiting", "h.status<>'review_ready'", "n.read_at IS NULL",
                   "n.revision_id IS DISTINCT FROM h.latest_revision_id"):
        assert needle in svc._NOTIF_IS_UNREAD
