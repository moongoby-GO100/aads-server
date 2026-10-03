"""Chat -> change request -> new revision -> re-approval, OHVIS inbox and the execution gate on real PostgreSQL.

Same environment contract as test_mockup_reviews_postgres.py (AADS_MOCKUP_TEST_DATABASE_URL pointing at a disposable
"test" database) plus migrations/20261004_ohvis_notifications.sql. Without it every test skips.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from tests.integration import test_mockup_reviews_postgres as base

code, events = base.code, base.events
env = base.env  # re-exported fixture: real tenants, users, grants, goal, session and messages

BASE = "/api/v1/projects"


@pytest_asyncio.fixture
async def flow(env):
    if not await env.conn.fetchval("SELECT to_regclass('public.ohvis_notifications') IS NOT NULL"):
        pytest.fail("ohvis_notifications migration is not applied to the test database")
    await env.conn.execute("UPDATE tenant_memberships SET role='admin' WHERE tenant_id=$1 AND user_id=$2",
                           env.tenants[0], env.users["writer"])  # a second, elevated reviewer who may open the session
    artifacts = []

    async def mockup_artifact(review_id, message_key="assistant", session=None):
        artifact = uuid4()
        await env.conn.execute(
            "INSERT INTO chat_artifacts(id,session_id,type,title,content,metadata,tenant_id) "
            "VALUES($1,$2::uuid,'html_preview','mockup','<html></html>',$3::jsonb,$4)",
            artifact, session or env.session, '{"mockup_review_id": "%s"}' % review_id, env.tenants[0])
        await env.conn.execute("UPDATE chat_messages SET artifact_id=$1 WHERE id=$2::uuid", artifact,
                               env.msgs[message_key])
        artifacts.append(artifact)
        return str(artifact)

    async def intake(who, comment="버튼 색을 바꿔 주세요", message="user", expected=None, **extra):
        body = {"idempotency_key": env.key(), "change_request_id": str(uuid4()), "session_id": env.session,
                "source_message_id": env.msgs[message], "comment": comment, **extra}
        if expected is not None:
            body["expected_generation"] = expected
        return await env.post("/change-intake", who=who, **body)

    async def inbox(who="owner", **params):
        res = await env.client.get(f"{BASE}/{env.project}/mockup-reviews/notifications", headers=env.headers[who],
                                   params=params)
        assert res.status_code == 200, res.text
        return res.json()

    async def rows(review_id):
        return await env.conn.fetch("SELECT * FROM ohvis_notifications WHERE review_id=$1::uuid ORDER BY id", review_id)

    env.mockup_artifact, env.intake, env.inbox, env.rows = mockup_artifact, intake, inbox, rows
    yield env
    await env.conn.execute("DELETE FROM ohvis_notifications WHERE tenant_id = ANY($1::uuid[])", env.tenants)
    if artifacts:
        await env.conn.execute("DELETE FROM chat_artifacts WHERE id = ANY($1::uuid[])", artifacts)


def pairs(notifications, env):
    names = {v: k for k, v in env.users.items()}
    return {(names[r["recipient_user_id"]], r["kind"]) for r in notifications}


async def test_chat_message_to_new_revision_to_reapproval_end_to_end(flow):
    env = flow
    review_id, rev1, _ = await env.approved_review()
    await env.mockup_artifact(review_id)

    # the reviewer replies to the AI message that carries the mockup; the SERVER picks review and revision
    res = await env.intake("writer", reply_to_id=env.msgs["assistant"])
    assert res.status_code == 201, res.text
    answer = res.json()
    cr = answer["change_request_id"]
    assert answer["target"] == {"review_id": review_id, "revision_id": rev1["revision_id"], "resolved_by": "server"}
    assert answer["approved"] is False and answer["implementation_command"] is False
    assert (await env.get(f"/{review_id}")).json()["status"] == "changes_requested"

    # an approved bundle no longer lets work start while a change is pending
    verify = await env.post(f"/{review_id}/verify", task_id=env.task_id, revision_id=rev1["revision_id"],
                            manifest_hash=rev1["manifest_hash"])
    assert verify.status_code == 409 and "change_requested_hold" in verify.json()["detail"]["reasons"]

    started = await env.post(f"/{review_id}/revising", idempotency_key=env.key(), expected_generation=4)
    assert started.status_code == 200, started.text
    rev2 = await env.add_revision(review_id, 5, resolves=[
        {"change_request_id": cr, "outcome": "applied", "reason": "버튼 색상 반영"}])
    assert rev2.status_code == 201, rev2.text
    submit = await env.post(f"/{review_id}/submit", idempotency_key=env.key(), expected_generation=6,
                            revision_id=rev2.json()["revision_id"])
    assert submit.status_code == 200, submit.text
    approved = await env.approve(review_id, rev2.json(), 7)
    assert approved.status_code == 200, approved.text
    assert approved.json()["revision"] == 2 and approved.json()["revision_id"] != rev1["revision_id"]

    report = (await env.get(f"/{review_id}/change-report")).json()
    item = report["change_requests"][0]
    assert item["outcome"] == "applied" and item["new_revision"]["revision"] == 2
    assert item["original_text"] == "버튼 색을 바꿔 주세요"

    sent = await env.rows(review_id)
    assert pairs(sent, env) >= {
        ("writer", "review_requested"), ("writer", "approval_waiting"),   # first submit, to the other approver
        ("owner", "approved"),                                              # rev 1 approved by someone else
        ("owner", "change_received"),                                       # change came from chat
        ("writer", "re_reported"),                                          # revised document reported back
    }
    assert ("owner", "approved") in pairs(sent, env) and len([r for r in sent if r["kind"] == "approved"]) >= 2
    # nobody is told about their own action, and nobody outside the session sees anything
    assert ("writer", "change_received") not in pairs(sent, env)
    outsiders = {"reader", "nogrant", "foreign", "approver"}
    assert not {u for u, _ in pairs(sent, env)} & outsiders

    # clicking a notification opens exactly the version it names
    reported = next(r for r in sent if r["kind"] == "re_reported")
    link = urlsplit(reported["link"])
    query = parse_qs(link.query)
    assert link.path == "/chat" and link.fragment == env.session
    assert query["mockup_review"] == [review_id] and query["mockup_project"] == [env.project]
    assert query["mockup_revision"] == [rev2.json()["revision_id"]] and reported["revision"] == 2
    opened = await env.get(f"/{review_id}/revisions/{query['mockup_revision'][0]}", who="writer")
    assert opened.status_code == 200 and opened.json()["revision"] == 2

    mine = await env.inbox("writer", unread_only="true", project=env.project)
    assert mine["unread"] >= 1 and all(not i["read"] for i in mine["items"])
    first = mine["items"][0]["id"]
    assert (await env.post(f"/notifications/{first}/read", who="writer")).status_code == 200
    assert (await env.inbox("writer"))["unread"] == mine["unread"] - 1


async def test_intake_target_is_decided_by_the_server(flow):
    env = flow
    review_a, rev_a, _ = await env.approved_review()
    review_b, _rev_b, _ = await env.to_review_ready()
    artifact_a = await env.mockup_artifact(review_a)

    ambiguous = await env.intake("writer")
    assert ambiguous.status_code == 409 and code(ambiguous) == "ambiguous_review_target"
    assert set(ambiguous.json()["detail"]["candidates"]) == {review_a, review_b}

    conflict = await env.intake("writer", artifact_id=artifact_a, review_id=review_b)
    assert conflict.status_code == 422 and code(conflict) == "review_target_conflict"
    not_mockup = await env.intake("writer", artifact_id=str(uuid4()))
    assert not_mockup.status_code == 404 and code(not_mockup) == "artifact_not_found"
    foreign_review = await env.intake("writer", review_id=str(uuid4()))
    assert foreign_review.status_code == 404 and code(foreign_review) == "review_not_found"
    wrong_session = await env.intake("writer", message="other", review_id=review_a)
    assert wrong_session.status_code == 422 and code(wrong_session) == "source_message_session_mismatch"
    stale = await env.intake("writer", review_id=review_a, expected=0)
    assert stale.status_code == 409 and code(stale) == "stale_revision"
    assert (await env.get(f"/{review_a}")).json()["status"] == "approved"  # nothing was filed

    ok = await env.intake("writer", artifact_id=artifact_a)
    assert ok.status_code == 201 and ok.json()["target"]["review_id"] == review_a
    assert ok.json()["target"]["revision_id"] == rev_a["revision_id"]
    assert (await env.get(f"/{review_b}")).json()["status"] == "review_ready"  # the other review is untouched


async def test_intake_permissions_follow_tenant_project_and_session(flow):
    env = flow
    review_id, _rev, _ = await env.approved_review()
    assert (await env.intake("nogrant", review_id=review_id)).status_code == 403
    assert (await env.intake("reader", review_id=review_id)).status_code == 403  # read grant cannot write
    private = await env.intake("writer", review_id=review_id)  # elevated: may use the owner's session
    assert private.status_code == 201
    other_tenant = await env.intake("foreign", review_id=review_id)
    assert other_tenant.status_code == 404 and code(other_tenant) == "session_not_found"
    assert (await env.get(f"/{review_id}")).json()["pending_change_requests"] == 1


async def test_replay_does_not_notify_twice_and_inboxes_are_private(flow):
    env = flow
    review_id, _rev, _ = await env.approved_review()
    body = {"idempotency_key": env.key(), "change_request_id": str(uuid4()), "session_id": env.session,
            "source_message_id": env.msgs["user"], "comment": "한 번만 알려 주세요", "review_id": review_id}
    first = await env.post("/change-intake", who="writer", **body)
    assert first.status_code == 201, first.text
    before = len(await env.rows(review_id))
    again = await env.post("/change-intake", who="writer", **body)
    assert again.status_code == 200 and again.json()["idempotent"] is True
    assert len(await env.rows(review_id)) == before

    owner_items = (await env.inbox("owner"))["items"]
    assert owner_items and all(i["project"] == env.project for i in owner_items)
    for stranger in ("foreign", "nogrant", "reader", "approver"):
        assert (await env.inbox(stranger))["items"] == []
    some = owner_items[0]["id"]
    for stranger in ("foreign", "reader", "writer"):
        denied = await env.post(f"/notifications/{some}/read", who=stranger)
        assert denied.status_code == 404 and code(denied) == "notification_not_found"
    assert (await env.inbox("owner", unread_only="true"))["unread"] > 0


async def test_gate_denials_notify_once_per_cause(flow):
    from app.core import db_pool
    from app.services import mockup_review_service as svc

    env = flow
    review_id, rev, ok = await env.approved_review()
    bundle = (f"TARGET: /root/aads/aads-dashboard\nMOCKUP_REVIEW_ID: {review_id}\n"
              f"MOCKUP_REVISION_ID: {rev['revision_id']}\nMOCKUP_MANIFEST_HASH: {rev['manifest_hash']}\n")

    async def check(phase, instruction=bundle, task=None, goal=None):
        async with db_pool.get_pool().acquire() as conn, conn.transaction():
            return await svc.gate_check(conn, phase=phase, tenant=str(env.tenants[0]), project=env.project,
                                        task_id=task or env.task_id, instruction=instruction, goal_id=goal,
                                        actor="system:pipeline-runner", environ={"MOCKUP_GATE_MODE": "enforce"})

    submit = await check("submit", task=f"runner-{uuid4().hex[:8]}", goal=env.goal_id)  # no task link yet
    assert submit["allowed"] and submit["binding_status"] == "bound"
    start = await check("pre_execution")
    assert start["allowed"] and start["binding_status"] == "running"
    unrelated = await check("pre_execution", instruction="TARGET: /root/aads/aads-server\nTARGET_FILES: app/x.py\n")
    assert unrelated["applicable"] is False and unrelated["allowed"] is True
    no_bundle = await check("pre_execution", instruction="TARGET: /root/aads/aads-dashboard\n")
    assert no_bundle["allowed"] is False and no_bundle["reasons"] == ["mockup_bundle_required"]

    revoked = await env.post(f"/{review_id}/revoke", who="approver", idempotency_key=env.key(),
                             expected_generation=3, approval_id=ok["approval_id"], reason="승인 철회")
    assert revoked.status_code == 200
    assert await env.conn.fetchval("SELECT status FROM mockup_review_task_bindings WHERE head_id=$1::uuid "
                                   "AND task_id=$2", review_id, env.task_id) == "review_required"  # flagged, not killed
    for _ in range(3):
        denied = await check("pre_execution")
        assert denied["allowed"] is False and "approval_revoked" in denied["reasons"]
    assert len(await events(env, review_id, "verify_denied")) == 3
    failed = [r for r in await env.rows(review_id) if r["kind"] == "failed"]
    assert len(failed) == len({r["recipient_user_id"] for r in failed}) >= 1  # one per recipient, not per attempt
    assert UUID(str(failed[0]["revision_id"])) == UUID(rev["revision_id"])
    assert {r["recipient_user_id"] for r in failed} <= {env.users["owner"], env.users["writer"]}
