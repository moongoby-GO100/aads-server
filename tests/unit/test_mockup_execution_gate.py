"""Mockup approval execution gate, chat change intake and OHVIS inbox (no database).

SQL is answered by an in-memory FakeConn that mirrors the queries' intent, so these tests prove the Python rules
(who is notified, what is denied, how targets resolve). The SQL itself is exercised by
tests/integration/test_mockup_chat_flow.py, which needs AADS_MOCKUP_TEST_DATABASE_URL.
"""
import asyncio
import importlib.util
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.models.mockup_review import ChangeIntake
from app.services import mockup_gate_rules as rules
from app.services import mockup_review_service as svc

REVIEW = "11111111-1111-4111-8111-111111111111"
REVISION = "22222222-2222-4222-8222-222222222222"
HASH = "a" * 64
TENANT = "33333333-3333-4333-8333-333333333333"
OTHER_TENANT = "44444444-4444-4444-8444-444444444444"
GOAL = "55555555-5555-4555-8555-555555555555"
SESSION = UUID("66666666-6666-4666-8666-666666666666")
BUNDLE = f"MOCKUP_REVIEW_ID: {REVIEW}\nMOCKUP_REVISION_ID: {REVISION}\nMOCKUP_MANIFEST_HASH: {HASH}\n"
UI_TASK = "TARGET: /root/aads/aads-dashboard\nTARGET_FILES: src/components/Panel.tsx\n"
BACKEND_TASK = "TARGET: /root/aads/aads-server\nTARGET_FILES: app/api/x.py, tests/unit/test_x.py\nREAD_ONLY_FILES: src/components/Panel.tsx\n"


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ rules

def test_parse_bundle_requires_a_complete_unambiguous_declaration():
    assert rules.parse_bundle(BACKEND_TASK) == {"declared": False, "errors": []}
    ok = rules.parse_bundle(BUNDLE)
    assert ok["errors"] == [] and ok["review_id"] == REVIEW and ok["manifest_hash"] == HASH
    partial = rules.parse_bundle(f"MOCKUP_REVIEW_ID: {REVIEW}\n")
    assert partial["declared"] and set(partial["errors"]) == {"revision_id_missing", "manifest_hash_missing"}
    other = REVIEW.replace("1", "7")
    assert "review_id_ambiguous" in rules.parse_bundle(BUNDLE + f"MOCKUP_REVIEW_ID: {other}\n")["errors"]
    assert "manifest_hash_invalid" in rules.parse_bundle(BUNDLE.replace(HASH, "xyz"))["errors"]
    assert "review_id_invalid" in rules.parse_bundle(BUNDLE.replace(REVIEW, "not-a-uuid"))["errors"]


def test_bundle_lines_inside_code_fences_are_not_evidence():
    fenced = f"설명\n```\n{BUNDLE}```\n"
    assert rules.parse_bundle(fenced)["declared"] is False


def test_ui_task_detection_uses_targets_not_prose_or_read_only_files():
    assert rules.ui_task_reasons(UI_TASK)
    assert rules.ui_task_reasons("TARGET: /root/aads/aads-dashboard\n") == ["dashboard_target"]
    assert rules.ui_task_reasons(BACKEND_TASK) == []
    assert rules.ui_task_reasons("panel.tsx 를 참고만 한다\nTARGET_FILES: docs/plan.md, src/components/x.md\n") == []
    assert rules.ui_task_reasons("UI_TASK: true\n") == ["ui_task_declared"]
    assert rules.ui_task_reasons("UI_TASK: false\n") == []


def test_gate_mode_defaults_to_shadow_and_unreadable_values_enforce():
    assert rules.gate_mode({}) == "shadow"
    assert rules.gate_mode({"MOCKUP_GATE_MODE": "ENFORCE"}) == "enforce"
    assert rules.gate_mode({"MOCKUP_GATE_MODE": "off"}) == "off"
    assert rules.gate_mode({"MOCKUP_GATE_MODE": "enfroce"}) == "enforce"


def test_classify_leaves_unrelated_backend_tasks_alone_in_every_mode():
    for mode in ("off", "shadow", "enforce"):
        assert rules.classify(BACKEND_TASK, {"MOCKUP_GATE_MODE": mode})["applicable"] is False
    assert rules.classify(UI_TASK, {"MOCKUP_GATE_MODE": "off"})["applicable"] is False
    assert rules.classify(UI_TASK, {})["applicable"] is True


def test_no_bundle_and_unavailable_decisions():
    assert rules.no_bundle_decision("enforce") == {"allowed": False, "reasons": ["mockup_bundle_required"]}
    assert rules.no_bundle_decision("shadow")["allowed"] is True
    declared = rules.classify(BUNDLE, {})
    assert rules.unavailable_decision(declared)["allowed"] is False
    ui_shadow = rules.classify(UI_TASK, {"MOCKUP_GATE_MODE": "shadow"})
    assert rules.unavailable_decision(ui_shadow)["allowed"] is True
    ui_enforce = rules.classify(UI_TASK, {"MOCKUP_GATE_MODE": "enforce"})
    assert rules.unavailable_decision(ui_enforce)["allowed"] is False


# ------------------------------------------------------------- fake database

class FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """First handler whose substring is in the SQL answers; anything unexpected fails the test."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def _answer(self, sql, args):
        self.calls.append((sql, args))
        for needle, fn in self.handlers:
            if needle in sql:
                return fn(*args)
        raise AssertionError(f"unexpected SQL: {sql[:90]}")

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args)

    async def fetch(self, sql, *args):
        return self._answer(sql, args)

    async def fetchval(self, sql, *args):
        return self._answer(sql, args)

    async def execute(self, sql, *args):
        return self._answer(sql, args)

    def transaction(self):
        return FakeTx()


def head_row(**over):
    base = {"id": UUID(REVIEW), "tenant_id": UUID(TENANT), "project_key": "AADS", "title": "목업", "change_type": "modify",
            "goal_id": None, "session_id": SESSION, "status": "approved", "latest_revision_id": UUID(REVISION),
            "approved_revision_id": UUID(REVISION), "approval_event_id": 7, "generation": 3, "pointer_generation": 0,
            "created_by": "creator", "updated_at": datetime.now(timezone.utc)}
    return {**base, **over}


# ------------------------------------------------------------------ gate_check

@pytest.fixture
def verifier(monkeypatch):
    calls = []

    async def fake_verify(conn, tenant, project, review_id, actor, body, declared_goal_id=None):
        calls.append({"phase": body.phase, "task": body.task_id, "goal": declared_goal_id, "review": review_id})
        return fake_verify.answer

    fake_verify.answer = {"allowed": True, "review_id": REVIEW, "revision_id": REVISION, "manifest_hash": HASH,
                          "binding_status": "running", "approval_id": 7}
    monkeypatch.setattr(svc, "verify_bundle", fake_verify)
    fake_verify.calls = calls
    return fake_verify


def gate(instruction, phase="pre_execution", environ=None, authorize=None):
    return run(svc.gate_check(FakeConn([]), phase=phase, tenant=TENANT, project="AADS", task_id="runner-aaaa1111",
                              instruction=instruction, goal_id=GOAL, actor="system:pipeline-runner",
                              authorize=authorize, environ=environ or {}))


def test_unrelated_backend_task_is_not_gated_and_never_touches_verify(verifier):
    out = gate(BACKEND_TASK, environ={"MOCKUP_GATE_MODE": "enforce"})
    assert out["applicable"] is False and out["allowed"] is True and verifier.calls == []


def test_declared_bundle_is_verified_at_submit_and_again_before_the_worker(verifier):
    submit = gate(BUNDLE, phase="submit")
    start = gate(BUNDLE, phase="pre_execution")
    assert submit["allowed"] and start["allowed"]
    assert [c["phase"] for c in verifier.calls] == ["submit", "pre_execution"]
    assert {str(c["review"]) for c in verifier.calls} == {REVIEW}
    assert verifier.calls[0]["goal"] == GOAL


@pytest.mark.parametrize("reason", ["approval_revoked", "revision_mismatch", "manifest_hash_mismatch",
                                    "superseded_by_newer_revision", "change_requested_hold", "task_out_of_scope"])
def test_every_approval_state_problem_denies_the_ui_task(verifier, reason):
    verifier.answer = {"allowed": False, "code": "approval_required", "reasons": [reason]}
    out = gate(UI_TASK + BUNDLE)
    assert out["allowed"] is False and out["reasons"] == [reason]


def test_ui_task_without_a_bundle_is_denied_when_enforcing_and_only_recorded_in_shadow(verifier):
    enforced = gate(UI_TASK, environ={"MOCKUP_GATE_MODE": "enforce"})
    assert enforced["allowed"] is False and enforced["reasons"] == ["mockup_bundle_required"]
    shadow = gate(UI_TASK, environ={"MOCKUP_GATE_MODE": "shadow"})
    assert shadow["allowed"] is True and shadow["shadow"] is True
    assert verifier.calls == []


def test_half_declared_bundle_is_denied_even_in_shadow(verifier):
    out = gate(f"MOCKUP_REVIEW_ID: {REVIEW}\n", environ={"MOCKUP_GATE_MODE": "shadow"})
    assert out["allowed"] is False and "bundle_revision_id_missing" in out["reasons"] and verifier.calls == []


def test_mode_off_disables_the_gate(verifier):
    assert gate(UI_TASK + BUNDLE, environ={"MOCKUP_GATE_MODE": "off"})["applicable"] is False
    assert verifier.calls == []


def test_missing_review_and_missing_permission_are_denials_not_errors(monkeypatch):
    async def missing(*a, **k):
        raise HTTPException(404, {"code": "review_not_found"})

    monkeypatch.setattr(svc, "verify_bundle", missing)
    assert gate(BUNDLE)["reasons"] == ["review_not_found"]

    async def forbidden():
        raise HTTPException(403, "project_access_denied")

    out = gate(BUNDLE, authorize=forbidden)
    assert out["allowed"] is False and out["reasons"] == ["project_access_denied"]


def test_database_failure_fails_closed(monkeypatch):
    async def down(*a, **k):
        raise ConnectionError("db down")

    monkeypatch.setattr(svc, "verify_bundle", down)
    out = gate(BUNDLE)
    assert out["allowed"] is False and out["reasons"] == ["gate_unavailable"]


def test_verify_bundle_accepts_the_goal_named_in_the_submission_before_a_task_link_exists():
    head = head_row()
    revision = {"id": UUID(REVISION), "manifest_hash": HASH, "manifest": json.dumps({"assets": [], "screens": []}),
                "doc_refs": "[]"}
    inserted = []
    conn = FakeConn([
        ("FOR UPDATE", lambda *a: head),
        ("FROM goal_task_links", lambda *a: None),
        ("FROM goals WHERE id=$1::uuid", lambda *a: 1),
        ("FROM mockup_review_revisions", lambda *a: revision),
        ("FROM mockup_review_task_bindings", lambda *a: None),
        ("INSERT INTO mockup_review_task_bindings", lambda *a: inserted.append(a)),
        ("nextval", lambda *a: 99),
        ("INSERT INTO mockup_review_events", lambda *a: None),
    ])
    body = svc.VerifyBundle(task_id="runner-aaaa1111", revision_id=UUID(REVISION), manifest_hash=HASH, phase="submit")
    out = run(svc.verify_bundle(conn, TENANT, "AADS", UUID(REVIEW), "u1", body, declared_goal_id=GOAL))
    assert out["allowed"] is True and out["binding_status"] == "bound" and len(inserted) == 1
    denied = run(svc.verify_bundle(
        FakeConn([("FOR UPDATE", lambda *a: head), ("FROM goal_task_links", lambda *a: None),
                  ("FROM mockup_review_revisions", lambda *a: revision), ("nextval", lambda *a: 5),
                  ("INSERT INTO mockup_review_events", lambda *a: None),
                  ("FROM tenant_memberships", lambda *a: []),
                  ("FROM chat_sessions", lambda *a: "creator"),
                  ("INSERT INTO ohvis_notifications", lambda *a: "INSERT 0 0")]),
        TENANT, "AADS", UUID(REVIEW), "u1", body))
    assert denied["allowed"] is False and denied["reasons"] == ["task_out_of_scope"]


# --------------------------------------------------------------- notifications

class Inbox:
    """In-memory ohvis_notifications + membership model for _emit."""

    def __init__(self, members, owner="creator", requesters=()):
        self.members = members  # {user: (role, {"approve"|"read"|"write"})} per tenant
        self.owner = owner
        self.requesters = list(requesters)
        self.rows = {}

    def conn(self):
        return FakeConn([
            ("FROM chat_sessions", lambda *a: self.owner),
            ("SELECT DISTINCT requested_by", lambda *a: [{"requested_by": u} for u in self.requesters]),
            ("m.user_id=ANY($2::text[])", self._eligible),
            ("FROM tenant_memberships m WHERE m.tenant_id=$1::uuid AND m.status='active' AND m.deleted_at IS NULL AND "
             "(m.role", self._approvers),
            ("SELECT revision FROM mockup_review_revisions", lambda *a: 2),
            ("INSERT INTO ohvis_notifications", self._insert),
        ])

    def _approvers(self, tenant, project, elevated):
        return [{"user_id": u} for u, (role, grants) in self.members.items()
                if role in elevated or "approve" in grants]

    def _eligible(self, tenant, ids, project, access, elevated):
        return [{"user_id": u, "role": self.members[u][0]} for u in ids if u in self.members
                and (self.members[u][0] in elevated or set(access) & self.members[u][1])]

    def _insert(self, tenant, project, user, kind, review, revision, number, session, event, title, body, link,
                payload, key):
        slot = (str(tenant), user, key)
        if slot in self.rows:
            return "INSERT 0 0"
        self.rows[slot] = {"kind": kind, "link": link, "revision": number, "review": review, "session": session,
                           "revision_id": revision, "title": title}
        return "INSERT 0 1"


MEMBERS = {"creator": ("member", {"write"}), "approver": ("member", {"approve"}), "admin": ("admin", set()),
           "reader": ("member", {"read"}), "stranger": ("member", set()), "reviewer": ("member", {"write"})}


def emit(inbox, kind, actor="creator", head=None, event_id=10, audience=("owners",), **kw):
    return run(svc._emit(inbox.conn(), head or head_row(), kind, event_id=event_id, actor=actor,
                         revision_id=UUID(REVISION), audience=audience, **kw))


def test_every_notification_kind_exists_in_the_service_and_the_migration():
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "20261004_ohvis_notifications.sql").read_text()
    for kind in svc.NOTIFICATION_KINDS:
        assert f"'{kind}'" in sql and kind in svc._TITLES
    assert set(svc.NOTIFICATION_KINDS) == {"review_requested", "change_received", "re_reported", "approval_waiting",
                                           "approved", "revoked", "failed"}


def test_link_opens_the_exact_review_revision_and_session():
    inbox = Inbox(MEMBERS)
    assert emit(inbox, "approved", actor="approver", audience=("owners",)) == 1
    row = next(iter(inbox.rows.values()))
    assert row["link"] == f"/chat?mockup_review={REVIEW}&mockup_project=AADS&mockup_revision={REVISION}#{SESSION}"
    assert row["revision"] == 2 and str(row["review"]) == REVIEW and row["session"] == SESSION


def test_replayed_event_never_notifies_twice():
    inbox = Inbox(MEMBERS)
    assert emit(inbox, "change_received", actor="reviewer", audience=("owners", "approvers")) >= 1
    first = len(inbox.rows)
    assert emit(inbox, "change_received", actor="reviewer", audience=("owners", "approvers")) == 0
    assert len(inbox.rows) == first
    assert emit(inbox, "change_received", actor="reviewer", event_id=11, audience=("owners", "approvers")) >= 1
    assert len(inbox.rows) == first * 2


def test_the_actor_is_not_notified_of_their_own_action():
    inbox = Inbox(MEMBERS)
    emit(inbox, "approved", actor="approver", audience=("owners", "approvers"))
    assert "approver" not in {user for (_, user, _) in inbox.rows}


def test_recipients_need_project_access_and_session_access():
    inbox = Inbox(MEMBERS, owner="creator")
    emit(inbox, "failed", actor="system:pipeline-runner", audience=("owners", "approvers"))
    users = {user for (_, user, _) in inbox.rows}
    assert users == {"creator", "admin"}  # approver grant alone cannot read another user's private session
    assert not users & {"stranger", "reader"}
    open_head = head_row(session_id=None)
    inbox2 = Inbox(MEMBERS, owner=None)
    emit(inbox2, "failed", actor="system:pipeline-runner", head=open_head, audience=("owners", "approvers"))
    assert {user for (_, user, _) in inbox2.rows} == {"creator", "approver", "admin"}


def test_users_without_a_project_grant_are_never_notified():
    inbox = Inbox({"creator": ("member", set()), "outsider": ("member", set())}, owner=None)
    assert emit(inbox, "approved", actor="x", head=head_row(session_id=None), audience=("owners", "requesters")) == 0
    assert inbox.rows == {}


def test_notifications_are_keyed_by_tenant():
    inbox = Inbox(MEMBERS)
    emit(inbox, "approved", actor="approver")
    other = head_row(tenant_id=UUID(OTHER_TENANT))
    emit(inbox, "approved", actor="approver", head=other)
    assert {t for (t, _, _) in inbox.rows} == {TENANT, OTHER_TENANT}


def test_a_failing_insert_is_swallowed_and_does_not_break_the_review_operation():
    conn = FakeConn([("FROM chat_sessions", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))])
    assert run(svc._emit(conn, head_row(), "approved", event_id=1, actor="a", revision_id=UUID(REVISION),
                         audience=("owners",))) == 0


def test_resubmitting_a_revised_review_reports_to_requesters_and_first_submit_requests_review(monkeypatch):
    sent = []

    async def fake_emit(conn, head, kind, **kw):
        sent.append((kind, kw["audience"]))
        return 1

    monkeypatch.setattr(svc, "_emit", fake_emit)
    run(svc._notify_submitted(None, head_row(), {"id": UUID(REVISION), "revision": 1}, "u", 5))
    run(svc._notify_submitted(None, head_row(), {"id": UUID(REVISION), "revision": 2}, "u", 6))
    assert sent == [("review_requested", ("approvers",)), ("approval_waiting", ("approvers",)),
                    ("re_reported", ("requesters", "owners")), ("approval_waiting", ("approvers",))]


def test_inbox_is_only_ever_the_callers_own_rows():
    seen = []
    conn = FakeConn([("FROM ohvis_notifications WHERE", lambda *a: seen.append(a) or []),
                     ("count(*)", lambda *a: 0)])
    run(svc.list_notifications(conn, TENANT, "u1", unread_only=True, project="AADS", limit=9999))
    assert seen[0][0] == TENANT and seen[0][1] == "u1" and seen[0][4] == 200
    mark = FakeConn([("UPDATE ohvis_notifications", lambda *a: None)])
    with pytest.raises(HTTPException) as exc:
        run(svc.mark_notification_read(mark, TENANT, "u1", 5))
    assert exc.value.status_code == 404
    assert "recipient_user_id=$3" in mark.calls[0][0] and mark.calls[0][1] == (5, TENANT, "u1")


def test_no_external_channel_is_wired_into_the_notification_code():
    source = inspect.getsource(svc)
    start = source.index("# -------------------------------------------------------- notifications")
    end = source.index("# ------------------------------------------------------------- execution gate")
    block = "\n".join(line for line in source[start:end].splitlines() if not line.lstrip().startswith("#")).lower()
    for word in ("telegram", "smtp", "sendmail", "slack", "sms", "requests.", "httpx", "aiohttp", "webhook"):
        assert word not in block


# ------------------------------------------------------------------ chat intake

def intake_body(**over):
    base = {"idempotency_key": "intake-key-0001", "change_request_id": uuid4(), "session_id": SESSION,
            "source_message_id": uuid4(), "comment": "버튼 색을 바꿔 주세요"}
    return ChangeIntake(**{**base, **over})


def target_conn(*, heads=(), artifact=None, replied=None, session_ok=True, review=None):
    def session(*a):
        return {"id": SESSION, "user_id": "u1"} if session_ok else None

    return FakeConn([
        ("FROM chat_sessions", session),
        ("FROM chat_messages WHERE id=$1", lambda *a: replied),
        ("FROM chat_artifacts", lambda *a: artifact),
        ("FOR UPDATE", lambda *a: review or (heads[0] if heads else None)),
        ("FROM mockup_review_heads WHERE tenant_id", lambda *a: [{"id": h["id"]} for h in heads]),
    ])


def resolve(conn, body):
    return run(svc.resolve_change_target(conn, TENANT, "AADS", body))


def code_of(exc):
    return exc.value.detail["code"]


def test_target_comes_from_the_artifact_the_user_selected():
    art = {"session_id": SESSION, "metadata": {"mockup_review_id": REVIEW}}
    head = resolve(target_conn(artifact=art, review=head_row()), intake_body(artifact_id=uuid4()))
    assert str(head["id"]) == REVIEW


def test_target_comes_from_the_message_being_replied_to():
    replied = {"session_id": SESSION, "artifact_id": uuid4()}
    art = {"session_id": SESSION, "metadata": {"mockup_review_id": REVIEW}}
    head = resolve(target_conn(replied=replied, artifact=art, review=head_row()), intake_body(reply_to_id=uuid4()))
    assert str(head["id"]) == REVIEW


def test_target_falls_back_to_the_single_active_review_of_the_session():
    head = resolve(target_conn(heads=[head_row()]), intake_body())
    assert str(head["id"]) == REVIEW


def test_two_active_reviews_in_one_session_are_ambiguous_and_none_is_not_found():
    other = head_row(id=uuid4())
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(heads=[head_row(), other]), intake_body())
    assert exc.value.status_code == 409 and code_of(exc) == "ambiguous_review_target"
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(heads=[]), intake_body())
    assert code_of(exc) == "review_target_not_found"


def test_conflicting_hints_and_foreign_sessions_are_rejected():
    art = {"session_id": SESSION, "metadata": {"mockup_review_id": REVIEW}}
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(artifact=art, review=head_row()), intake_body(artifact_id=uuid4(), review_id=uuid4()))
    assert code_of(exc) == "review_target_conflict"
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(replied={"session_id": uuid4(), "artifact_id": None}), intake_body(reply_to_id=uuid4()))
    assert code_of(exc) == "reply_target_session_mismatch"
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(artifact={"session_id": uuid4(), "metadata": {}}), intake_body(artifact_id=uuid4()))
    assert code_of(exc) == "artifact_not_found"
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(session_ok=False), intake_body())
    assert code_of(exc) == "session_not_found"
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(review=head_row(session_id=uuid4())), intake_body(review_id=UUID(REVIEW)))
    assert code_of(exc) == "review_session_mismatch"


def test_a_review_id_from_another_project_or_tenant_is_not_found():
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(review=None), intake_body(review_id=uuid4()))
    assert exc.value.status_code == 404 and code_of(exc) == "review_not_found"


def test_a_selected_artifact_that_is_not_a_mockup_is_rejected():
    art = {"session_id": SESSION, "metadata": {}}
    with pytest.raises(HTTPException) as exc:
        resolve(target_conn(artifact=art), intake_body(artifact_id=uuid4()))
    assert code_of(exc) == "artifact_not_a_mockup"


def test_intake_files_a_change_request_on_the_server_chosen_revision_and_never_approves(monkeypatch):
    head = head_row(status="review_ready")
    captured = {}

    async def fake_changes(conn, tenant, project, review_id, actor, elevated, body):
        captured.update(review=review_id, body=body)
        return {"review_id": str(review_id), "status": "changes_requested", "approved": False}

    monkeypatch.setattr(svc, "request_changes", fake_changes)
    body = intake_body()
    conn = FakeConn([
        ("FROM chat_sessions", lambda *a: {"id": SESSION, "user_id": "u1"}),
        ("SELECT session_id FROM chat_messages", lambda *a: SESSION),
        ("FROM mockup_review_heads WHERE tenant_id", lambda *a: [{"id": head["id"]}]),
        ("FOR UPDATE", lambda *a: head),
    ])
    out = run(svc.intake_change(conn, TENANT, "AADS", "u1", False, body))
    assert captured["body"].base_revision_id == head["latest_revision_id"]
    assert captured["body"].expected_generation == head["generation"]
    assert out["approved"] is False and out["implementation_command"] is False
    assert out["target"] == {"review_id": REVIEW, "revision_id": REVISION, "resolved_by": "server"}
    assert out["next"] == "revise_then_resubmit"


def test_intake_rejects_a_source_message_from_another_session():
    conn = FakeConn([("FROM chat_sessions", lambda *a: {"id": SESSION, "user_id": "u1"}),
                     ("SELECT session_id FROM chat_messages", lambda *a: uuid4())])
    with pytest.raises(HTTPException) as exc:
        run(svc.intake_change(conn, TENANT, "AADS", "u1", False, intake_body()))
    assert code_of(exc) == "source_message_session_mismatch"


def test_intake_cannot_use_another_users_private_session():
    conn = FakeConn([("FROM chat_sessions", lambda *a: {"id": SESSION, "user_id": "someone-else"})])
    with pytest.raises(HTTPException) as exc:
        run(svc.intake_change(conn, TENANT, "AADS", "u1", False, intake_body()))
    assert exc.value.status_code == 403 and code_of(exc) == "session_access_denied"


# --------------------------------------------------------- host CLI and routes

def load_cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_mockup_approval.py"
    spec = importlib.util.spec_from_file_location("verify_mockup_approval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(instruction, ask=None, env=None):
    module = load_cli()
    return module.run(["--job-id", "runner-aaaa1111", "--api-url", "http://x"], stdin_text=instruction,
                      environ=env or {}, ask=ask or (lambda *a: pytest.fail("API must not be called")))


def test_cli_exit_codes():
    assert cli(BACKEND_TASK)[0] == 0
    assert cli(UI_TASK, env={"MOCKUP_GATE_MODE": "enforce"})[0] == 10
    assert cli(UI_TASK)[0] == 0  # shadow
    assert cli(f"MOCKUP_REVIEW_ID: {REVIEW}\n")[0] == 10
    assert cli(BUNDLE, ask=lambda *a: {"allowed": True})[0] == 0
    assert cli(BUNDLE, ask=lambda *a: {"allowed": False, "reasons": ["approval_revoked"]})[0] == 10


def test_cli_fails_closed_when_the_api_cannot_answer():
    def down(*a):
        raise OSError("refused")

    code, out = cli(BUNDLE, ask=down)
    assert code == 20 and out["allowed"] is False and out["reasons"] == ["gate_unavailable"]
    assert cli(BUNDLE, ask=lambda *a: {"unexpected": 1})[0] == 20
    assert cli(BUNDLE, ask=lambda *a: {"allowed": "yes"})[0] == 10  # only a literal true allows


def test_cli_does_not_touch_jobs():
    source = (Path(__file__).resolve().parents[2] / "scripts" / "verify_mockup_approval.py").read_text()
    for word in ("kill", "pkill", "systemctl", "supervisorctl", "subprocess", "docker"):
        assert word not in source.replace("never kills", "").replace("kills,", "")


def test_runner_script_runs_the_gate_before_the_worker_and_blocks_on_failure():
    text = (Path(__file__).resolve().parents[2] / "scripts" / "pipeline-runner.sh").read_text()
    assert text.index("pre_validate \"$job_id\"") < text.index("verify_mockup_approval.py") < text.index(
        "check_duplicate \"$job_id\"")
    assert "mockup_approval_required" in text


def test_routes_are_registered_in_a_safe_order():
    from app.api import mockup_reviews, pipeline_runner

    paths = [r.path for r in mockup_reviews.router.routes]
    assert paths.index("/projects/{project_key}/mockup-reviews/notifications") < paths.index(
        "/projects/{project_key}/mockup-reviews/{review_id}")
    assert "/projects/{project_key}/mockup-reviews/change-intake" in paths
    assert "/pipeline/jobs/{job_id}/mockup-gate" in [r.path for r in pipeline_runner.router.routes]


def test_submit_gate_denies_with_409_and_leaves_unrelated_tasks_alone(monkeypatch):
    from app.api import pipeline_runner

    class Pool:
        def acquire(self):
            raise AssertionError("an unrelated task must not touch the database")

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: Pool())
    context = {"user": {"user_id": "u1", "is_internal_admin": True}, "tenant": {"id": TENANT},
               "membership": {"role": "owner"}}
    out = run(pipeline_runner._run_mockup_gate(context, phase="submit", project="AADS", task_id="runner-aaaa1111",
                                               instruction=BACKEND_TASK, goal_id=None))
    assert out["applicable"] is False and out["allowed"] is True

    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    denied = run(pipeline_runner._run_mockup_gate(context, phase="submit", project="AADS", task_id="runner-aaaa1111",
                                                  instruction=BUNDLE, goal_id=None))
    assert denied["allowed"] is False and denied["reasons"] == ["gate_unavailable"]  # Pool.acquire raised -> closed
    exc = pipeline_runner._mockup_denial(denied)
    assert exc.status_code == 409 and exc.detail["code"] == "mockup_approval_required"
