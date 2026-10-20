"""Server-created chat card for a mockup review on real PostgreSQL (metadata.mockup_review_id/_project/_revision_id).

Same environment contract as test_mockup_reviews_postgres.py (AADS_MOCKUP_TEST_DATABASE_URL pointing at a disposable
"test" database). Without it every test skips.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio

from tests.integration import test_mockup_reviews_postgres as base

code, events = base.code, base.events
env = base.env


@pytest_asyncio.fixture
async def cards(env):
    async def in_session(session=None):
        return await env.conn.fetch(
            "SELECT * FROM chat_artifacts WHERE session_id=$1::uuid AND metadata->>'mockup_review_id' IS NOT NULL "
            "ORDER BY created_at", session or env.session)

    async def for_review(review_id):
        return await env.conn.fetch(
            "SELECT * FROM chat_artifacts WHERE tenant_id=$1 AND metadata->>'mockup_review_id'=$2",
            env.tenants[0], review_id)

    env.cards, env.card_for = in_session, for_review
    yield env
    await env.conn.execute("DELETE FROM chat_artifacts WHERE tenant_id = ANY($1::uuid[]) "
                           "AND metadata->>'mockup_review_id' IS NOT NULL", env.tenants)


def meta(row):
    import json
    return json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]


async def test_submit_creates_one_card_bound_to_review_project_and_revision(cards):
    env = cards
    review = await env.new_review()
    rid = review["review_id"]
    rev = await env.add_revision(rid, 0)
    assert rev.status_code == 201, rev.text
    (draft_card,) = await env.card_for(rid)  # the first revision already binds the card to this session
    assert meta(draft_card)["mockup_revision_id"] == rev.json()["revision_id"]

    key = env.key()
    body = dict(idempotency_key=key, expected_generation=1, revision_id=rev.json()["revision_id"])
    first = await env.post(f"/{rid}/submit", **body)
    assert first.status_code == 200, first.text
    rows = await env.card_for(rid)
    assert len(rows) == 1 and rows[0]["id"] == draft_card["id"]
    card = rows[0]
    assert str(card["session_id"]) == env.session and card["tenant_id"] == env.tenants[0]
    assert meta(card) == {"subtype": "mockup_review", "mockup_review_id": rid, "mockup_project": env.project,
                          "mockup_revision_id": rev.json()["revision_id"]}
    submitted = (await events(env, rid, "submitted"))[0]
    assert str(submitted["payload"]).count(str(card["id"])) == 1

    again = await env.post(f"/{rid}/submit", **body)  # same key + same revision: replay, nothing new
    assert again.status_code == 200 and again.json()["idempotent"] is True
    assert [r["id"] for r in await env.card_for(rid)] == [card["id"]]


async def test_new_revision_updates_the_same_card(cards):
    env = cards
    rid, rev1, _ = await env.to_review_ready()
    (card,) = await env.card_for(rid)
    assert meta(card)["mockup_revision_id"] == rev1["revision_id"]

    args = dict(idempotency_key=env.key(), expected_generation=2, manifest=env.manifest("v2"), doc_refs=await env.doc_refs())
    rev2 = await env.post(f"/{rid}/revisions", **args)
    assert rev2.status_code == 201, rev2.text
    (moved,) = await env.card_for(rid)
    assert moved["id"] == card["id"] and meta(moved)["mockup_revision_id"] == rev2.json()["revision_id"]

    retry = await env.post(f"/{rid}/revisions", **args)  # same key: replay, card unchanged
    assert retry.status_code == 200 and retry.json()["idempotent"] is True
    (still,) = await env.card_for(rid)
    assert meta(still)["mockup_revision_id"] == rev2.json()["revision_id"] and len(await env.cards()) == 1

    stale = await env.post(f"/{rid}/revisions", idempotency_key=env.key(), expected_generation=2,
                           manifest=env.manifest("late"), doc_refs=await env.doc_refs(), archive_if_stale=True)
    assert stale.status_code == 202, stale.text  # archived, pointer not applied: card must not follow it
    (kept,) = await env.card_for(rid)
    assert meta(kept)["mockup_revision_id"] == rev2.json()["revision_id"]


async def test_review_without_session_gets_no_card(cards):
    env = cards
    res = await env.post("", title="No session", change_type="new", idempotency_key=env.key(), goal_id=env.goal_id)
    assert res.status_code == 201, res.text
    rid = res.json()["review_id"]
    rev = await env.add_revision(rid, 0)
    sub = await env.post(f"/{rid}/submit", idempotency_key=env.key(), expected_generation=1,
                         revision_id=rev.json()["revision_id"])
    assert sub.status_code == 200, sub.text
    assert await env.card_for(rid) == []


async def test_other_users_private_session_blocks_submit_atomically(cards):
    env = cards
    review = await env.new_review()  # session belongs to "owner"
    rid = review["review_id"]
    rev = await env.add_revision(rid, 0)
    denied = await env.post(f"/{rid}/submit", who="writer", idempotency_key=env.key(), expected_generation=1,
                            revision_id=rev.json()["revision_id"])
    assert denied.status_code == 403 and code(denied) == "session_access_denied"
    assert (await env.get(f"/{rid}")).json()["status"] == "draft"
    assert await events(env, rid, "submitted") == [] and len(await env.card_for(rid)) == 1  # only the owner's own card


async def test_session_of_another_tenant_or_project_is_never_written(cards):
    env = cards
    foreign_ws, foreign_session = uuid4(), uuid4()
    await env.conn.execute("INSERT INTO chat_workspaces(id,name,tenant_id,project_key) VALUES($1,'foreign',$2,$3)",
                           foreign_ws, env.tenants[1], env.project)
    await env.conn.execute("INSERT INTO chat_sessions(id,workspace_id,tenant_id,user_id) VALUES($1,$2,$3,$4)",
                           foreign_session, foreign_ws, env.tenants[1], env.users["foreign"])
    other_ws, other_session = uuid4(), uuid4()
    await env.conn.execute("INSERT INTO chat_workspaces(id,name,tenant_id,project_key) VALUES($1,'other',$2,$3)",
                           other_ws, env.tenants[0], env.other_project)
    await env.conn.execute("INSERT INTO chat_sessions(id,workspace_id,tenant_id,user_id) VALUES($1,$2,$3,$4)",
                           other_session, other_ws, env.tenants[0], env.users["owner"])

    for session in (foreign_session, other_session):
        review = await env.new_review()
        rid = review["review_id"]
        rev = await env.add_revision(rid, 0)
        # the API validated the session at create time; point the head elsewhere to prove submit re-checks it
        await env.conn.execute("UPDATE mockup_review_heads SET session_id=$2 WHERE id=$1::uuid", rid, session)
        res = await env.post(f"/{rid}/submit", idempotency_key=env.key(), expected_generation=1,
                             revision_id=rev.json()["revision_id"])
        assert res.status_code == 404 and code(res) == "session_not_found"
        assert (await env.get(f"/{rid}")).json()["status"] == "draft" and await events(env, rid, "submitted") == []
        assert await env.cards(str(session)) == []


async def test_card_is_resolvable_by_change_intake(cards):
    env = cards
    if not await env.conn.fetchval("SELECT to_regclass('public.ohvis_notifications') IS NOT NULL"):
        pytest.skip("ohvis_notifications migration is not applied")
    rid, rev, _ = await env.to_review_ready()
    (card,) = await env.card_for(rid)
    res = await env.post("/change-intake", idempotency_key=env.key(), change_request_id=str(uuid4()),
                         session_id=env.session, source_message_id=env.msgs["user"], comment="버튼 색 변경",
                         artifact_id=str(card["id"]))
    assert res.status_code == 201, res.text
    assert res.json()["target"]["review_id"] == rid and res.json()["target"]["revision_id"] == rev["revision_id"]
