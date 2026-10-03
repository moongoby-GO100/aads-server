"""Mockup review API on a real PostgreSQL schema with real Bearer auth.

Set AADS_MOCKUP_TEST_DATABASE_URL to a disposable database whose name contains "test" and whose schema has the
canonical-document/chat/goal tables plus migrations/20261003_mockup_reviews.sql. Without it the tests skip
(AADS_CANONICAL_DB_REQUIRED=1 turns the skip into a failure).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

BASE = "/api/v1/projects"
NOW = "2026-10-03T00:00:00Z"
STATES = ("loading", "empty", "error", "permission", "session-expired", "offline")


def _dsn() -> str:
    dsn = os.getenv("AADS_MOCKUP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("AADS_MOCKUP_TEST_DATABASE_URL not set")
    assert "test" in urlsplit(dsn).path.lower(), "refusing to run against a non-test database"
    return dsn


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app() -> FastAPI:
    from app.api.canonical_documents import router as docs
    from app.api.mockup_reviews import router as reviews

    instance = FastAPI()
    instance.include_router(docs, prefix="/api/v1")
    instance.include_router(reviews, prefix="/api/v1")
    return instance


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    dsn = _dsn()
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
    except (OSError, asyncpg.PostgresError) as exc:
        if os.getenv("AADS_CANONICAL_DB_REQUIRED") == "1":
            pytest.fail(str(exc))
        pytest.skip(f"test database unreachable: {exc}")
    if not await conn.fetchval("SELECT to_regclass('public.mockup_review_heads') IS NOT NULL"):
        await conn.close()
        pytest.fail("mockup review migration is not applied to the test database")

    monkeypatch.setenv("DATABASE_URL", dsn)
    secret = uuid4().hex
    monkeypatch.setenv("JWT_" + "SECRET_KEY", secret)
    monkeypatch.setenv("MOCKUP_REVIEW_ASSET_ROOT", str(tmp_path / "assets"))
    (tmp_path / "assets" / "m").mkdir(parents=True)
    from app import auth
    from app.core import db_pool

    monkeypatch.setattr(auth, "SECRET_KEY", secret)
    monkeypatch.setattr(auth, "_pool", None)
    monkeypatch.setattr(db_pool, "_pool", None)
    monkeypatch.setattr(auth, "_saas_schema_ready", False)

    suffix = uuid4().hex
    project, other_project = f"TEST-MR-{suffix[:10].upper()}", f"TEST-MR-{suffix[10:20].upper()}"
    tenants = [uuid4(), uuid4()]
    names = {n: f"mr-{suffix[:12]}-{n}" for n in ("owner", "writer", "reader", "nogrant", "approver", "foreign")}
    tenant_of = {n: tenants[1] if n == "foreign" else tenants[0] for n in names}
    grants = {"owner": "approve", "approver": "approve", "writer": "write", "reader": "read", "foreign": "approve"}
    goal_id, workspace, session, other_session = uuid4(), uuid4(), uuid4(), uuid4()
    msgs = {k: uuid4() for k in ("user", "assistant", "other", "user2", "user3", "user4")}
    task_id = f"task-{suffix[:12]}"
    client = None
    refs_cache: list = []
    try:
        for index, tenant in enumerate(tenants):
            await conn.execute("INSERT INTO tenants(id,slug,name,kind,status) VALUES($1,$2,$2,'customer','active')",
                               tenant, f"mr-{suffix[:10]}-t{index}")
        for name, user in names.items():
            await conn.execute("INSERT INTO saas_users(id,email,password_hash,role) VALUES($1,$2,'unusable','user')",
                               user, f"{user}@example.invalid")
            await conn.execute("INSERT INTO tenant_memberships(tenant_id,user_id,role,status) "
                               "VALUES($1,$2,'member','active')", tenant_of[name], user)
            if name in grants:
                for scoped in (project, other_project):
                    await conn.execute("INSERT INTO project_document_grants(tenant_id,project_key,user_id,access) "
                                       "VALUES($1,$2,$3,$4)", tenant_of[name], scoped, user, grants[name])
        await conn.execute("INSERT INTO goals(id,title,project,status,tenant_id) VALUES($1,'mr goal',$2,'active',$3)",
                           goal_id, project, tenants[0])
        await conn.execute("INSERT INTO goal_task_links(task_type,task_id,goal_id,tenant_id,link_state) "
                           "VALUES('pipeline',$1,$2,$3,'active')", task_id, goal_id, tenants[0])
        await conn.execute("INSERT INTO chat_workspaces(id,name,tenant_id,project_key) VALUES($1,'mr',$2,$3)",
                           workspace, tenants[0], project)
        for sid in (session, other_session):
            await conn.execute("INSERT INTO chat_sessions(id,workspace_id,tenant_id,user_id) VALUES($1,$2,$3,$4)",
                               sid, workspace, tenants[0], names["owner"])
        for key, role, sid in (("user", "user", session), ("assistant", "assistant", session),
                               ("other", "user", other_session), ("user2", "user", session),
                               ("user3", "user", session), ("user4", "user", session)):
            await conn.execute("INSERT INTO chat_messages(id,session_id,role,content,tenant_id) "
                               "VALUES($1,$2,$3,$4,$5)", msgs[key], sid, role, f"message {key}", tenants[0])

        await db_pool.init_pool()
        headers = {n: {"Authorization": "Bearer " + auth.create_token(u, f"{u}@example.invalid",
                                                                      tenant_id=str(tenant_of[n]))}
                   for n, u in names.items()}
        client = AsyncClient(transport=ASGITransport(app=app()), base_url="http://test")
        store = tmp_path / "assets" / "m"
        ctx = SimpleNamespace(client=client, conn=conn, dsn=dsn, headers=headers, project=project,
                              other_project=other_project, goal_id=str(goal_id), session=str(session),
                              msgs={k: str(v) for k, v in msgs.items()}, task_id=task_id, store=store,
                              users=names, tenants=tenants, counter=0)

        async def put_doc(kind, version="1.0.0", content=None, key=None):
            key = key or f"{kind}-{suffix[:8]}"
            head = await conn.fetchrow("SELECT generation FROM project_document_heads WHERE tenant_id=$1 "
                                       "AND project_key=$2 AND document_key=$3", tenants[0], project, key)
            res = await client.post(f"{BASE}/{project}/documents", headers=headers["owner"], json={
                "document_key": key, "kind": kind, "title": f"{kind} doc", "version": version,
                "content": content or f"{kind} {version} {uuid4().hex}",
                "expected_generation": head["generation"] if head else 0})
            assert res.status_code == 201, res.text
            rid = res.json()["revision_id"]
            digest = await conn.fetchval("SELECT content_hash FROM project_document_revisions WHERE id=$1::uuid", rid)
            return {"role": kind, "document_key": key, "revision_id": rid, "content_hash": digest.strip()}

        async def doc_refs():
            if not refs_cache:
                refs_cache.extend([await put_doc(k) for k in ("plan", "prd", "spec")])
            return [dict(r) for r in refs_cache]

        def manifest(tag=None):
            ctx.counter += 1
            tag = tag or f"r{ctx.counter}"
            files = {"style.css": b"body{color:#111}", "app.js": b"console.log('m')"}
            assets = []
            for name, data in files.items():
                (store / name).write_bytes(data)
                assets.append({"asset_id": name.split(".")[0], "role": "child", "uri": f"internal://mockup_assets/m/{name}",
                               "sha256": sha(data), "byte_size": len(data),
                               "mime": "text/css" if name.endswith("css") else "text/javascript"})
            for viewport in ("desktop", "mobile"):
                name = f"{tag}-{viewport}.html"
                html = f"<html><link rel=stylesheet href=style.css><script src=app.js></script>{tag}".encode()
                (store / name).write_bytes(html)
                assets.append({"asset_id": f"{tag}-{viewport}", "role": "primary", "uri": f"internal://mockup_assets/m/{name}",
                               "sha256": sha(html), "byte_size": len(html), "mime": "text/html", "screen_id": "s1",
                               "phase": "mockup", "viewport": viewport, "fixture_id": "fx", "state": "default",
                               "captured_at": NOW, "capture_source": "browser_capture"})
            return {"screens": [{"screen_id": "s1", "title": "S1", "route": "/s1", "requirement_ids": ["M01"],
                                 "states_not_applicable": {s: "not applicable for this screen" for s in STATES}}],
                    "assets": assets, "design_tokens_version": "dt-1", "source_sha": "abcdef1",
                    "evidence": [{"evidence_id": "ev1", "kind": "browser_capture", "screen_id": "s1", "route": "/s1",
                                  "success": True, "recorded_at": NOW}]}

        def key():
            return f"k-{uuid4().hex[:20]}"

        async def post(path, who="owner", **body):
            return await client.post(f"{BASE}/{project}/mockup-reviews{path}", headers=headers[who], json=body)

        async def get(path, who="owner"):
            return await client.get(f"{BASE}/{project}/mockup-reviews{path}", headers=headers[who])

        async def new_review(**extra):
            res = await post("", title="Mockup review", change_type="new", idempotency_key=key(),
                             goal_id=str(goal_id), session_id=str(session), **extra)
            assert res.status_code == 201, res.text
            return res.json()

        async def add_revision(review_id, generation, refs=None, tag=None, who="owner", **extra):
            res = await post(f"/{review_id}/revisions", who=who, idempotency_key=key(), expected_generation=generation,
                             manifest=manifest(tag), doc_refs=refs if refs is not None else await doc_refs(), **extra)
            return res

        async def to_review_ready(refs=None):
            review = await new_review()
            rev = await add_revision(review["review_id"], 0, refs)
            assert rev.status_code == 201, rev.text
            sub = await post(f"/{review['review_id']}/submit", idempotency_key=key(), expected_generation=1,
                             revision_id=rev.json()["revision_id"])
            assert sub.status_code == 200, sub.text
            return review["review_id"], rev.json(), sub.json()

        async def approve(review_id, rev, generation, who="approver", **extra):
            return await post(f"/{review_id}/approve", who=who, idempotency_key=key(), expected_generation=generation,
                              revision_id=rev["revision_id"], manifest_hash=rev["manifest_hash"], confirm=True, **extra)

        async def approved_review():
            review_id, rev, _ = await to_review_ready()
            res = await approve(review_id, rev, 2)
            assert res.status_code == 200, res.text
            return review_id, rev, res.json()

        ctx.post, ctx.get, ctx.new_review, ctx.add_revision, ctx.key = post, get, new_review, add_revision, key
        ctx.to_review_ready, ctx.approve, ctx.approved_review, ctx.put_doc, ctx.manifest, ctx.doc_refs = (
            to_review_ready, approve, approved_review, put_doc, manifest, doc_refs)
        yield ctx
    finally:
        if client is not None:
            await client.aclose()
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL session_replication_role = replica")
                for table in ("mockup_review_task_bindings", "mockup_review_change_requests", "mockup_review_events",
                              "mockup_review_revisions", "mockup_review_heads", "project_document_events",
                              "project_document_revisions", "project_document_heads", "project_document_grants",
                              "goal_task_links", "chat_messages", "chat_sessions", "chat_workspaces", "goals",
                              "tenant_memberships"):
                    await conn.execute(f"DELETE FROM {table} WHERE tenant_id = ANY($1::uuid[])", tenants)
                await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", tenants)
                await conn.execute("DELETE FROM saas_users WHERE id = ANY($1::text[])", list(names.values()))
        finally:
            await conn.close()
            await db_pool.close_pool()
            if auth._pool is not None:
                await auth._pool.close()
                auth._pool = None


def code(response):
    return response.json()["detail"]["code"]


async def events(env, review_id, action=None):
    rows = await env.conn.fetch("SELECT action,payload,revision_id,generation_after FROM mockup_review_events "
                                "WHERE head_id=$1::uuid ORDER BY id", review_id)
    return [r for r in rows if action is None or r["action"] == action]


# ------------------------------------------------------------------- lifecycle

async def test_draft_save_review_submit_and_approval_are_distinct_steps(env):
    review = await env.new_review()
    assert review["status"] == "draft" and review["generation"] == 0 and review["latest_revision"] is None
    rev = await env.add_revision(review["review_id"], 0)
    assert rev.status_code == 201, rev.text
    body = rev.json()
    assert body["status"] == "draft" and body["unapproved"] is True and body["approval_inherited"] is False
    assert (await env.get(f"/{review['review_id']}")).json()["status"] == "draft"

    sub = await env.post(f"/{review['review_id']}/submit", idempotency_key=env.key(), expected_generation=1,
                         revision_id=body["revision_id"])
    assert sub.status_code == 200 and sub.json()["status"] == "review_ready" and sub.json()["approved"] is False
    view = (await env.get(f"/{review['review_id']}")).json()
    assert view["approved_revision"] is None

    ok = await env.approve(review["review_id"], body, 2)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    view = (await env.get(f"/{review['review_id']}")).json()
    assert view["approved_revision"]["revision_id"] == body["revision_id"]
    assert view["approved_revision"]["approval_id"] == ok.json()["approval_id"]
    assert [e["action"] for e in await events(env, review["review_id"])] == [
        "created", "revision_created", "submitted", "approved"]


async def test_manifest_hash_is_computed_over_actual_bytes_and_served_back(env):
    review = await env.new_review()
    manifest = env.manifest("served")
    res = await env.post(f"/{review['review_id']}/revisions", idempotency_key=env.key(), expected_generation=0,
                         manifest=manifest, doc_refs=[])
    assert res.status_code == 201, res.text
    detail = (await env.get(f"/{review['review_id']}/revisions/{res.json()['revision_id']}")).json()
    server_hash = res.json()["manifest_hash"]
    assert len(server_hash) == 64 and detail["manifest_hash"] == server_hash
    for asset in detail["manifest"]["assets"]:
        data = (env.store / asset["uri"].rsplit("/", 1)[1]).read_bytes()
        assert asset["sha256"] == sha(data) and asset["byte_size"] == len(data)
    canonical = json.dumps({k: v for k, v in detail["manifest"].items() if k != "assets"} | {
        "assets": [{k: v for k, v in a.items() if k != "url"} for a in detail["manifest"]["assets"]]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert sha(canonical.encode()) == server_hash

    asset = next(a for a in detail["manifest"]["assets"] if a["role"] == "primary")
    served = await env.client.get(asset["url"], headers=env.headers["writer"])
    assert served.status_code == 200 and sha(served.content) == asset["sha256"]
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]

    forged = env.manifest("forged")
    forged["assets"][0]["sha256"] = "0" * 64
    bad = await env.post(f"/{review['review_id']}/revisions", idempotency_key=env.key(), expected_generation=1,
                         manifest=forged, doc_refs=[])
    assert bad.status_code == 422 and code(bad) == "asset_hash_mismatch"
    asserted = await env.post(f"/{review['review_id']}/revisions", idempotency_key=env.key(), expected_generation=1,
                              manifest=env.manifest("asserted"), doc_refs=[], manifest_hash="f" * 64)
    assert asserted.status_code == 422 and code(asserted) == "manifest_hash_mismatch"


async def test_new_revision_does_not_inherit_approval_and_revisions_are_immutable(env):
    review_id, rev1, _ = await env.approved_review()
    rev2 = await env.add_revision(review_id, 3)
    assert rev2.status_code == 201, rev2.text
    assert rev2.json()["revision"] == 2 and rev2.json()["status"] == "draft"
    view = (await env.get(f"/{review_id}")).json()
    assert view["latest_revision"]["revision_id"] == rev2.json()["revision_id"]
    assert view["approved_revision"]["revision_id"] == rev1["revision_id"]  # r1 stays the only approved one
    statuses = {r["revision"]: r["status"] for r in view["revisions"]}
    assert statuses == {1: "approved", 2: "draft"}

    verify = await env.post(f"/{review_id}/verify", task_id=env.task_id, revision_id=rev2.json()["revision_id"],
                            manifest_hash=rev2.json()["manifest_hash"])
    assert verify.status_code == 409 and "revision_mismatch" in verify.json()["detail"]["reasons"]
    stale = await env.post(f"/{review_id}/verify", task_id=env.task_id, revision_id=rev1["revision_id"],
                           manifest_hash=rev1["manifest_hash"])
    assert stale.status_code == 409 and "superseded_by_newer_revision" in stale.json()["detail"]["reasons"]

    for sql in ("UPDATE mockup_review_revisions SET manifest='{}'::jsonb WHERE id=$1::uuid",
                "DELETE FROM mockup_review_revisions WHERE id=$1::uuid"):
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await env.conn.execute(sql, rev1["revision_id"])
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await env.conn.execute("DELETE FROM mockup_review_events WHERE head_id=$1::uuid", review_id)


# ------------------------------------------------------------ exception contracts

async def test_exception_contracts(env):
    review = await env.new_review()
    rid = review["review_id"]
    first = await env.add_revision(rid, 0)
    assert first.status_code == 201

    # 1. 409 generation_conflict carries the current state
    conflict = await env.add_revision(rid, 0)
    assert conflict.status_code == 409 and code(conflict) == "generation_conflict"
    assert conflict.json()["detail"]["generation"] == 1
    assert conflict.json()["detail"]["latest_revision_id"] == first.json()["revision_id"]

    # 2. 409 stale_revision when a submit names a superseded revision
    second = await env.add_revision(rid, 1, parent_revision_id=first.json()["revision_id"])
    assert second.status_code == 201
    stale = await env.post(f"/{rid}/submit", idempotency_key=env.key(), expected_generation=2,
                           revision_id=first.json()["revision_id"])
    assert stale.status_code == 409 and code(stale) == "stale_revision"

    # 3. 409 idempotency_key_conflict: same key, different body
    k = env.key()
    one = await env.post(f"/{rid}/submit", idempotency_key=k, expected_generation=2,
                         revision_id=second.json()["revision_id"])
    assert one.status_code == 200
    different = await env.post(f"/{rid}/submit", idempotency_key=k, expected_generation=2,
                               revision_id=first.json()["revision_id"])
    assert different.status_code == 409 and code(different) == "idempotency_key_conflict"

    # 4. 422 missing_artifacts: a draft without plan/prd/spec refs cannot be submitted
    thin = await env.new_review()
    t_rev = await env.add_revision(thin["review_id"], 0, refs=[])
    missing = await env.post(f"/{thin['review_id']}/submit", idempotency_key=env.key(), expected_generation=1,
                             revision_id=t_rev.json()["revision_id"])
    assert missing.status_code == 422 and code(missing) == "missing_artifacts"
    assert {m["role"] for m in missing.json()["detail"]["missing"] if m["code"] == "document_ref_missing"} == {
        "plan", "prd", "spec"}

    # 5. 409 approval_required on verify when nothing is approved
    denied = await env.post(f"/{rid}/verify", task_id=env.task_id, revision_id=second.json()["revision_id"],
                            manifest_hash=second.json()["manifest_hash"])
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "approval_required"
    assert "not_approved" in denied.json()["detail"]["reasons"]

    # 6. 409 asset_tampered when a hashed child changes after the revision was created
    (env.store / "app.js").write_bytes(b"console.log('tampered')")
    tampered = await env.approve(rid, second.json(), 3)
    assert tampered.status_code == 409 and code(tampered) == "asset_tampered"
    assert (await env.get(f"/{rid}")).json()["status"] == "review_ready"
    assert not await events(env, rid, "approved")


async def test_document_reference_validation(env):
    review = await env.new_review()
    refs = await env.doc_refs()
    bad_hash = [dict(refs[0], content_hash="0" * 64)] + refs[1:]
    res = await env.add_revision(review["review_id"], 0, refs=bad_hash)
    assert res.status_code == 422 and code(res) == "document_ref_hash_mismatch"
    wrong_role = [dict(refs[0], role="prd")]
    assert code(await env.add_revision(review["review_id"], 0, refs=wrong_role)) == "document_ref_mismatch"
    ghost = [dict(refs[0], revision_id=str(uuid4()))] + refs[1:]
    assert code(await env.add_revision(review["review_id"], 0, refs=ghost)) == "document_ref_not_found"

    good = await env.add_revision(review["review_id"], 0, refs=refs)
    assert good.status_code == 201, good.text
    await env.put_doc("plan", version="1.1.0", key=refs[0]["document_key"])  # newer plan revision appears
    sub = await env.post(f"/{review['review_id']}/submit", idempotency_key=env.key(), expected_generation=1,
                         revision_id=good.json()["revision_id"])
    assert sub.status_code == 409 and code(sub) == "document_revision_stale"


async def test_exempt_document_requires_policy_reference(env):
    review = await env.new_review()
    exempt = [{"role": r, "exempt_reason": "Backend-only change, no UI plan", "exempt_policy_ref": "POLICY-RDOC-1"}
              for r in ("plan", "prd", "spec")]
    ok = await env.add_revision(review["review_id"], 0, refs=exempt)
    assert ok.status_code == 201, ok.text
    no_policy = [{"role": "plan", "exempt_reason": "Backend-only change, no UI plan"}]
    assert (await env.add_revision(review["review_id"], 1, refs=no_policy)).status_code == 422


# -------------------------------------------------------------------- permissions

async def test_permissions_tenant_project_and_session(env):
    review = await env.new_review()
    rid = review["review_id"]
    path = f"/{rid}"
    assert (await env.get(path, who="reader")).status_code == 200
    assert (await env.get(path, who="nogrant")).status_code == 403
    # read grant cannot write; write grant cannot approve; approve grant can
    denied = await env.post("", who="reader", title="x", change_type="new", idempotency_key=env.key())
    assert denied.status_code == 403
    rev = await env.add_revision(rid, 0)
    assert (await env.add_revision(rid, 1, who="reader")).status_code == 403
    sub = await env.post(f"/{rid}/submit", idempotency_key=env.key(), expected_generation=1,
                         revision_id=rev.json()["revision_id"])
    assert sub.status_code == 200
    assert (await env.approve(rid, rev.json(), 2, who="writer")).status_code == 403
    assert (await env.approve(rid, rev.json(), 2, who="reader")).status_code == 403
    assert (await env.approve(rid, rev.json(), 2, who="nogrant")).status_code == 403
    assert (await env.approve(rid, rev.json(), 2, who="approver")).status_code == 200
    # tenant isolation: the foreign tenant sees nothing, even with a grant on the same project key
    assert (await env.get(path, who="foreign")).status_code == 404
    assert (await env.client.get(f"{BASE}/{env.other_project}/mockup-reviews/{rid}",
                                 headers=env.headers["owner"])).status_code == 404
    # session scope: a writer who does not own the session cannot attach it to a review
    attach = await env.post("", who="writer", title="x", change_type="new", idempotency_key=env.key(),
                            session_id=env.session)
    assert attach.status_code == 403 and code(attach) == "session_access_denied"
    unauth = await env.client.get(f"{BASE}/{env.project}/mockup-reviews/{rid}")
    assert unauth.status_code == 401


# ------------------------------------------------------------ idempotency/concurrency

async def test_idempotent_replay_returns_the_stored_result(env):
    k = env.key()
    body = dict(title="Replay", change_type="new", idempotency_key=k)
    one, two = await env.post("", **body), await env.post("", **body)
    assert one.status_code == 201 and two.status_code == 200 and two.json()["idempotent"] is True
    assert one.json()["review_id"] == two.json()["review_id"]
    assert (await env.post("", **{**body, "title": "Other"})).status_code == 409

    rid = one.json()["review_id"]
    kk, manifest, refs = env.key(), env.manifest("idem"), [await env.put_doc(r) for r in ("plan", "prd", "spec")]
    args = dict(idempotency_key=kk, expected_generation=0, manifest=manifest, doc_refs=refs)
    first, again = await env.post(f"/{rid}/revisions", **args), await env.post(f"/{rid}/revisions", **args)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["revision_id"] == first.json()["revision_id"] and again.json()["idempotent"] is True
    assert await env.conn.fetchval("SELECT count(*) FROM mockup_review_revisions WHERE head_id=$1::uuid", rid) == 1


async def test_concurrent_writers_produce_exactly_one_winner(env):
    review = await env.new_review()
    rid = review["review_id"]
    refs = await env.doc_refs()
    results = await asyncio.gather(*(env.add_revision(rid, 0, refs=refs, tag=f"race{i}") for i in range(6)))
    assert sorted(r.status_code for r in results) == [201] + [409] * 5
    assert await env.conn.fetchval("SELECT count(*) FROM mockup_review_revisions WHERE head_id=$1::uuid", rid) == 1

    review_id, rev, _ = await env.to_review_ready()
    approvals = await asyncio.gather(*(env.approve(review_id, rev, 2) for _ in range(5)))
    assert sorted(r.status_code for r in approvals) == [200] + [409] * 4
    assert len(await events(env, review_id, "approved")) == 1


async def test_stale_writer_can_be_archived_without_moving_the_pointer(env):
    review_id, rev1, _ = await env.to_review_ready()
    newer = await env.add_revision(review_id, 2, parent_revision_id=rev1["revision_id"])
    assert newer.status_code == 201
    late = await env.add_revision(review_id, 2, archive_if_stale=True, tag="late")
    assert late.status_code == 202 and late.json()["pointer_applied"] is False
    view = (await env.get(f"/{review_id}")).json()
    assert view["latest_revision"]["revision_id"] == newer.json()["revision_id"]
    assert {r["revision"]: r["status"] for r in view["revisions"]}[3] == "archived_stale"
    assert view["generation"] == newer.json()["generation"]


# ----------------------------------------------------------------- change requests

async def test_change_requests_preserve_text_links_and_outcome(env):
    review_id, rev1, _ = await env.approved_review()
    text1 = "  첫 번째 수정 요청\n두 줄 원문 그대로  "

    async def change(text, message, generation, base, **extra):
        return await env.post(f"/{review_id}/changes", idempotency_key=env.key(), expected_generation=generation,
                              change_request_id=str(uuid4()), base_revision_id=base, source_message_id=message,
                              comment=text, **extra)

    first = await change(text1, env.msgs["user"], 3, rev1["revision_id"], screen_id="s1")
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "changes_requested" and first.json()["approved"] is False
    assert first.json()["implementation_command"] is False and first.json()["queued"] is False
    cr1 = first.json()["change_request_id"]
    second = await change("두 번째 요청", env.msgs["user2"], 4, rev1["revision_id"])
    assert second.status_code == 201 and second.json()["pending_change_requests"] == 2
    cr2 = second.json()["change_request_id"]

    replay = await env.post(f"/{review_id}/changes", idempotency_key=env.key(), expected_generation=4,
                            change_request_id=cr1, base_revision_id=rev1["revision_id"],
                            source_message_id=env.msgs["user"], comment=text1, screen_id="s1")
    assert replay.status_code == 200 and replay.json()["idempotent"] is True
    mutated = await env.post(f"/{review_id}/changes", idempotency_key=env.key(), expected_generation=4,
                             change_request_id=cr1, base_revision_id=rev1["revision_id"],
                             source_message_id=env.msgs["user"], comment="rewritten", screen_id="s1")
    assert mutated.status_code == 409 and code(mutated) == "change_request_conflict"

    stored = await env.conn.fetchrow("SELECT comment,source_message_id::text AS m FROM mockup_review_change_requests "
                                     "WHERE change_request_id=$1::uuid", cr1)
    assert stored["comment"] == text1 and stored["m"] == env.msgs["user"]
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await env.conn.execute("UPDATE mockup_review_change_requests SET comment='x' WHERE change_request_id=$1::uuid", cr1)

    # an approved revision loses its hold when a change is requested and cannot be re-approved until resolved
    verify = await env.post(f"/{review_id}/verify", task_id=env.task_id, revision_id=rev1["revision_id"],
                            manifest_hash=rev1["manifest_hash"])
    assert verify.status_code == 409 and "change_requested_hold" in verify.json()["detail"]["reasons"]

    started = await env.post(f"/{review_id}/revising", idempotency_key=env.key(), expected_generation=4)
    assert started.status_code == 200 and sorted(started.json()["change_request_ids"]) == sorted([cr1, cr2])
    third = await change("작업 중 들어온 세 번째", env.msgs["user3"], 5, rev1["revision_id"])
    assert third.status_code == 201 and third.json()["queued"] is True and third.json()["status"] == "revising"
    cr3 = third.json()["change_request_id"]

    res = {"outcome": "applied", "reason": "반영함: 제목 변경"}
    unresolved = await env.add_revision(review_id, 5, resolves=[{"change_request_id": cr1, **res}])
    assert unresolved.status_code == 422 and code(unresolved) == "unresolved_change_requests"
    rev2 = await env.add_revision(review_id, 5, resolves=[
        {"change_request_id": cr1, **res},
        {"change_request_id": cr2, "outcome": "not_applied", "reason": "범위 밖이라 미반영"}])
    assert rev2.status_code == 201, rev2.text
    assert rev2.json()["status"] == "changes_requested"  # cr3 is still pending -> not approvable
    submit = await env.post(f"/{review_id}/submit", idempotency_key=env.key(), expected_generation=6,
                            revision_id=rev2.json()["revision_id"])
    assert submit.status_code == 409 and code(submit) == "invalid_state"

    rev3 = await env.add_revision(review_id, 6, resolves=[{"change_request_id": cr3, **res}])
    assert rev3.status_code == 201 and rev3.json()["status"] == "draft"

    report = (await env.get(f"/{review_id}/change-report")).json()
    by_id = {c["change_request_id"]: c for c in report["change_requests"]}
    assert by_id[cr1]["original_text"] == text1
    assert by_id[cr1]["source_message_id"] == env.msgs["user"] and by_id[cr1]["screen_id"] == "s1"
    assert by_id[cr1]["base_revision"]["revision"] == 1 and by_id[cr1]["new_revision"]["revision"] == 2
    assert by_id[cr1]["outcome"] == "applied" and by_id[cr2]["outcome"] == "not_applied"
    assert by_id[cr2]["outcome_reason"] == "범위 밖이라 미반영" and by_id[cr3]["new_revision"]["revision"] == 3
    assert by_id[cr3]["queued"] is True
    assert "## 반영 (2)" in report["markdown"] and "## 미반영 (1)" in report["markdown"]
    assert by_id[cr1]["base_revision"]["url"] in report["markdown"]
    assert (await env.client.get(by_id[cr1]["new_revision"]["url"], headers=env.headers["reader"])).status_code == 200

    with pytest.raises(asyncpg.PostgresError, match="final"):
        await env.conn.execute("UPDATE mockup_review_change_requests SET outcome_reason='x' "
                               "WHERE change_request_id=$1::uuid", cr1)


async def test_change_request_input_validation(env):
    review_id, rev, _ = await env.to_review_ready()

    async def change(message, comment="수정해 주세요", base=None, **extra):
        return await env.post(f"/{review_id}/changes", idempotency_key=env.key(), expected_generation=2,
                              change_request_id=str(uuid4()), base_revision_id=base or rev["revision_id"],
                              source_message_id=message, comment=comment, **extra)

    assert code(await change(env.msgs["user"], "   \n ")) == "empty_comment"
    assert code(await change(env.msgs["assistant"])) == "source_message_not_user_message"
    assert code(await change(env.msgs["other"])) == "source_message_session_mismatch"
    assert code(await change(str(uuid4()))) == "source_message_not_found"
    assert code(await change(env.msgs["user"], base=str(uuid4()))) == "stale_revision"
    assert code(await change(env.msgs["user"], screen_id="nope")) == "unknown_screen"
    assert code(await change(env.msgs["user"], f"{'api_' + 'key'} = {'abcdefghijklmnop' + '1234'}")) == "credential_content_rejected"
    assert await env.conn.fetchval("SELECT count(*) FROM mockup_review_change_requests WHERE head_id=$1::uuid",
                                   review_id) == 0
    assert (await env.get(f"/{review_id}")).json()["status"] == "review_ready"


# -------------------------------------------------------------- verify and revoke

async def test_verify_binds_tasks_and_revoke_holds_them(env):
    review_id, rev, ok = await env.approved_review()
    args = dict(task_id=env.task_id, revision_id=rev["revision_id"], manifest_hash=rev["manifest_hash"])

    submitted = await env.post(f"/{review_id}/verify", phase="submit", **args)
    assert submitted.status_code == 200 and submitted.json()["binding_status"] == "bound"
    running = await env.post(f"/{review_id}/verify", phase="pre_execution", **args)
    assert running.status_code == 200 and running.json()["binding_status"] == "running"
    assert running.json()["approval_id"] == ok["approval_id"]

    foreign = await env.post(f"/{review_id}/verify", task_id="task-not-in-goal", revision_id=rev["revision_id"],
                             manifest_hash=rev["manifest_hash"])
    assert foreign.status_code == 409 and "task_out_of_scope" in foreign.json()["detail"]["reasons"]
    wrong_hash = await env.post(f"/{review_id}/verify", task_id=env.task_id, revision_id=rev["revision_id"],
                                manifest_hash="a" * 64)
    assert "manifest_hash_mismatch" in wrong_hash.json()["detail"]["reasons"]

    stale = await env.post(f"/{review_id}/revoke", who="approver", idempotency_key=env.key(), expected_generation=3,
                           approval_id=ok["approval_id"] + 1000, reason="오래된 승인 번호")
    assert stale.status_code == 409 and code(stale) == "stale_approval"
    revoked = await env.post(f"/{review_id}/revoke", who="approver", idempotency_key=env.key(),
                             expected_generation=3, approval_id=ok["approval_id"], reason="승인 철회 사유")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert revoked.json()["bindings_held"] == 1
    assert await env.conn.fetchval("SELECT status FROM mockup_review_task_bindings WHERE head_id=$1::uuid",
                                   review_id) == "review_required"
    after = await env.post(f"/{review_id}/verify", **args)
    assert after.status_code == 409 and "approval_revoked" in after.json()["detail"]["reasons"]
    checkpoint = await env.post(f"/{review_id}/verify", phase="checkpoint", **args)
    assert checkpoint.status_code == 409
    # the denial is audited even though the HTTP answer is an error
    denied = await events(env, review_id, "verify_denied")
    assert len(denied) >= 3 and any("approval_revoked" in d["payload"] for d in denied)
    assert (await env.get(f"/{review_id}")).json()["approved_revision"] is None
    assert (await env.post(f"/{review_id}/revoke", who="approver", idempotency_key=env.key(),
                           expected_generation=4, approval_id=ok["approval_id"], reason="두 번째 철회")).status_code == 409


async def test_tampering_after_approval_blocks_execution(env):
    review_id, rev, _ = await env.approved_review()
    (env.store / "style.css").write_bytes(b"body{color:red}")
    res = await env.post(f"/{review_id}/verify", task_id=env.task_id, revision_id=rev["revision_id"],
                         manifest_hash=rev["manifest_hash"])
    assert res.status_code == 409 and "asset_tampered" in res.json()["detail"]["reasons"]
    detail = (await env.get(f"/{review_id}/revisions/{rev['revision_id']}")).json()
    css = next(a for a in detail["manifest"]["assets"] if a["asset_id"] == "style")
    assert (await env.client.get(css["url"], headers=env.headers["reader"])).status_code == 409


async def test_approval_audit_is_atomic_with_the_head_pointer(env):
    review_id, rev, ok = await env.approved_review()
    head = await env.conn.fetchrow("SELECT approval_event_id,approved_revision_id::text AS r,status "
                                   "FROM mockup_review_heads WHERE id=$1::uuid", review_id)
    assert head["approval_event_id"] == ok["approval_id"] and head["r"] == rev["revision_id"]
    event = (await events(env, review_id, "approved"))[0]
    assert str(event["revision_id"]) == rev["revision_id"] and event["generation_after"] == 3
    unconfirmed = await env.post(f"/{review_id}/approve", who="approver", idempotency_key=env.key(),
                                 expected_generation=3, revision_id=rev["revision_id"],
                                 manifest_hash=rev["manifest_hash"], confirm=False)
    assert unconfirmed.status_code == 422
    assert len(await events(env, review_id, "approved")) == 1
