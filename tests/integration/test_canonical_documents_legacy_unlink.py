"""DELETE legacy-links: link -> unlink -> relist, scope isolation, goal_documents immutability, audit."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.test_canonical_documents_workflow import (
    BASE, DATABASE, _app, _isolated_settings, _unreachable,
)

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "20261002_project_document_legacy_unlink.sql"
SOURCE = "docs/AADS-BACKUP-RETENTION-POLICY.md"


@pytest.mark.asyncio
async def test_unlink_legacy_goal_document_on_isolated_postgres(monkeypatch):
    assert DATABASE == "aads_doc_m1_final"
    dsn = _isolated_settings()
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
    except (OSError, asyncpg.CannotConnectNowError, asyncpg.ConnectionDoesNotExistError) as exc:
        _unreachable(exc)
    if not await conn.fetchval("SELECT to_regclass('public.project_document_heads') IS NOT NULL "
                               "AND to_regclass('public.tenant_memberships') IS NOT NULL"):
        await conn.close()
        pytest.fail("isolated canonical DB is missing the canonical/auth schema")
    # Idempotent; applies only to this isolated test database.
    await conn.execute(MIGRATION.read_text())

    monkeypatch.setenv("DATABASE_URL", dsn)
    jwt_secret = uuid4().hex
    monkeypatch.setenv("JWT_" + "SECRET_KEY", jwt_secret)
    from app import auth
    from app.core import db_pool
    monkeypatch.setattr(auth, "SECRET_KEY", jwt_secret)
    monkeypatch.setattr(auth, "_pool", None)
    monkeypatch.setattr(db_pool, "_pool", None)
    monkeypatch.setattr(auth, "_saas_schema_ready", False)

    suffix = uuid4().hex
    project = f"TEST-M3-{suffix[:12].upper()}"
    other_project = f"TEST-M3-{suffix[12:24].upper()}"
    ungranted = f"TEST-M3-{suffix[:6].upper()}{suffix[24:30].upper()}"
    key = f"TEST-M3-UNLINK-{suffix}"
    tenants = [uuid4(), uuid4()]
    users = [f"test-m3-unlink-{suffix}-a", f"test-m3-unlink-{suffix}-b"]
    goals = [uuid4(), uuid4()]
    doc_ids: list[int] = []
    try:
        for index, tenant in enumerate(tenants):
            await conn.execute("INSERT INTO saas_users(id,email,password_hash,role) VALUES($1,$2,$3,'user')",
                               users[index], f"{users[index]}@example.invalid", "test-only-unusable-hash")
            await conn.execute("INSERT INTO tenants(id,slug,name,kind,status) VALUES($1,$2,$3,'customer','active')",
                               tenant, users[index], users[index])
            await conn.execute("INSERT INTO tenant_memberships(tenant_id,user_id,role,status) "
                               "VALUES($1,$2,'member','active')", tenants[index], users[index])
        for tenant, user, scoped, access in ((tenants[0], users[0], project, "approve"),
                                             (tenants[0], users[0], other_project, "approve"),
                                             (tenants[1], users[1], project, "approve")):
            await conn.execute("INSERT INTO project_document_grants(tenant_id,project_key,user_id,access) "
                               "VALUES($1,$2,$3,$4)", tenant, scoped, user, access)
        for goal in goals:
            await conn.execute("INSERT INTO goals(id,title,project,tenant_id) VALUES($1,'unlink test',$2,$3)",
                               goal, project, tenants[0])
            doc_ids.append(await conn.fetchval(
                "INSERT INTO goal_documents(goal_id,kind,doc_path,document_key,version) "
                "VALUES($1,'plan',$2,$3,'1.0.0') RETURNING id", goal, SOURCE, key))

        async def legacy_snapshot():
            rows = await conn.fetch("SELECT * FROM goal_documents WHERE id=ANY($1::bigint[]) ORDER BY id", doc_ids)
            return [dict(row) for row in rows]

        before = await legacy_snapshot()
        assert len(before) == 2

        await db_pool.init_pool()
        headers_a = {"Authorization": "Bearer " + auth.create_token(
            users[0], f"{users[0]}@example.invalid", tenant_id=str(tenants[0]))}
        headers_b = {"Authorization": "Bearer " + auth.create_token(
            users[1], f"{users[1]}@example.invalid", tenant_id=str(tenants[1]))}
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            path = f"{BASE}/{project}/documents"
            other_path = f"{BASE}/{other_project}/documents"
            payload = {"document_key": key, "kind": "plan", "title": "unlink plan", "version": "1.0.0",
                       "source_path": SOURCE, "expected_generation": 0}
            created = await client.post(path, json=payload, headers=headers_a)
            assert created.status_code == 201, created.text
            revision_id = created.json()["revision_id"]
            # Same key in another project and in another tenant: heads exist but hold no links.
            assert (await client.post(other_path, json=payload, headers=headers_a)).status_code == 201
            assert (await client.post(path, json=payload, headers=headers_b)).status_code == 201
            approved = await client.post(f"{path}/{key}/approve", headers=headers_a, json={
                "revision_id": revision_id, "expected_generation": 1})
            assert approved.status_code == 200, approved.text

            for doc_id in doc_ids:
                linked = await client.post(f"{path}/{key}/legacy-links", headers=headers_a, json={
                    "revision_id": revision_id, "goal_document_id": doc_id})
                assert linked.status_code == 200 and linked.json()["linked"] is True, linked.text
            links = await client.get(f"{path}/{key}/legacy-links", headers=headers_a)
            assert {row["goal_document_id"] for row in links.json()["links"]} == set(doc_ids)

            async def link_count() -> int:
                return await conn.fetchval("SELECT count(*) FROM project_document_legacy_links "
                                           "WHERE tenant_id=$1::uuid", tenants[0])

            # Cross-scope attempts never remove the link.
            wrong_tenant = await client.delete(f"{path}/{key}/legacy-links/{doc_ids[0]}", headers=headers_b)
            assert wrong_tenant.status_code == 404 and wrong_tenant.json()["detail"] == "legacy_link_not_found"
            wrong_project = await client.delete(f"{other_path}/{key}/legacy-links/{doc_ids[0]}", headers=headers_a)
            assert wrong_project.status_code == 404 and wrong_project.json()["detail"] == "legacy_link_not_found"
            no_grant = await client.delete(
                f"{BASE}/{ungranted}/documents/{key}/legacy-links/{doc_ids[0]}", headers=headers_a)
            assert no_grant.status_code == 403 and no_grant.json()["detail"] == "project_access_denied"
            missing_doc = await client.delete(f"{path}/no-such-doc/legacy-links/{doc_ids[0]}", headers=headers_a)
            assert missing_doc.status_code == 404 and missing_doc.json()["detail"] == "document_not_found"
            bad_id = await client.delete(f"{path}/{key}/legacy-links/0", headers=headers_a)
            assert bad_id.status_code == 422
            assert await link_count() == 2

            # archive_head needs approve access; write-only is refused, plain unlink would pass authz.
            await conn.execute("UPDATE project_document_grants SET access='write' WHERE tenant_id=$1 AND user_id=$2",
                               tenants[1], users[1])
            write_only = await client.delete(
                f"{path}/{key}/legacy-links/{doc_ids[0]}?archive_head=true", headers=headers_b)
            assert write_only.status_code == 403 and write_only.json()["detail"] == "project_access_denied"

            # Direct SQL deletion stays blocked: the trigger only opens inside the API transaction.
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await conn.execute("DELETE FROM project_document_legacy_links WHERE goal_document_id=$1", doc_ids[0])
            assert await link_count() == 2

            first = await client.delete(f"{path}/{key}/legacy-links/{doc_ids[0]}", headers=headers_a)
            assert first.status_code == 200, first.text
            assert first.json() == {"unlinked": True, "goal_document_id": doc_ids[0], "revision_id": revision_id,
                                    "remaining_links": 1, "archived": False}
            links = await client.get(f"{path}/{key}/legacy-links", headers=headers_a)
            assert [row["goal_document_id"] for row in links.json()["links"]] == [doc_ids[1]]
            again = await client.delete(f"{path}/{key}/legacy-links/{doc_ids[0]}", headers=headers_a)
            assert again.status_code == 404 and again.json()["detail"] == "legacy_link_not_found"
            event = await conn.fetchrow(
                "SELECT e.actor_id,e.goal_document_id,e.revision_id::text AS revision_id,e.created_at "
                "FROM project_document_events e JOIN project_document_heads h ON h.id=e.head_id "
                "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 AND h.document_key=$3 AND e.action='legacy_unlinked'",
                tenants[0], project, key)
            assert event["actor_id"] == users[0] and event["goal_document_id"] == doc_ids[0]
            assert event["revision_id"] == revision_id and event["created_at"] is not None
            # archive_head with a surviving link must leave the head approved.
            head = await conn.fetchrow("SELECT approved_revision_id FROM project_document_heads "
                                       "WHERE tenant_id=$1::uuid AND project_key=$2 AND document_key=$3",
                                       tenants[0], project, key)
            assert head["approved_revision_id"] is not None

            # Rollback path: relink works after an unlink, then unlink the last one with archive_head.
            relinked = await client.post(f"{path}/{key}/legacy-links", headers=headers_a, json={
                "revision_id": revision_id, "goal_document_id": doc_ids[0]})
            assert relinked.status_code == 200 and relinked.json()["idempotent"] is False
            kept = await client.delete(f"{path}/{key}/legacy-links/{doc_ids[0]}?archive_head=true", headers=headers_a)
            assert kept.json()["remaining_links"] == 1 and kept.json()["archived"] is False
            relinked = await client.post(f"{path}/{key}/legacy-links", headers=headers_a, json={
                "revision_id": revision_id, "goal_document_id": doc_ids[0]})
            assert relinked.status_code == 200
            assert (await client.delete(f"{path}/{key}/legacy-links/{doc_ids[0]}", headers=headers_a)).status_code == 200
            last = await client.delete(f"{path}/{key}/legacy-links/{doc_ids[1]}?archive_head=true", headers=headers_a)
            assert last.status_code == 200, last.text
            assert last.json()["remaining_links"] == 0 and last.json()["archived"] is True
            assert await link_count() == 0
            head = await conn.fetchrow("SELECT approved_revision_id FROM project_document_heads "
                                       "WHERE tenant_id=$1::uuid AND project_key=$2 AND document_key=$3",
                                       tenants[0], project, key)
            assert head["approved_revision_id"] is None
            actions = [row["action"] for row in await conn.fetch(
                "SELECT e.action FROM project_document_events e JOIN project_document_heads h ON h.id=e.head_id "
                "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 AND h.document_key=$3 ORDER BY e.id",
                tenants[0], project, key)]
            assert actions.count("legacy_unlinked") == 4 and actions[-1] == "archived"
            brief = await client.get(f"{path}/brief/approved", headers=headers_a)
            assert brief.json() == {"documents": [], "authoritative": False}
            # Tenant B's identically keyed head was never affected.
            assert await conn.fetchval("SELECT count(*) FROM project_document_events WHERE tenant_id=$1::uuid "
                                       "AND action='legacy_unlinked'", tenants[1]) == 0

        assert await legacy_snapshot() == before
    finally:
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL session_replication_role = replica")
                for table in ("project_document_legacy_links", "project_document_goal_links",
                              "project_document_events", "project_document_revisions",
                              "project_document_heads", "project_document_grants"):
                    await conn.execute(f"DELETE FROM {table} WHERE tenant_id = ANY($1::uuid[])", tenants)
                await conn.execute("DELETE FROM goal_documents WHERE goal_id = ANY($1::uuid[])", goals)
                await conn.execute("DELETE FROM goals WHERE id = ANY($1::uuid[])", goals)
                await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id = ANY($1::uuid[])", tenants)
                await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", tenants)
                await conn.execute("DELETE FROM saas_users WHERE id = ANY($1::text[])", users)
        finally:
            try:
                await conn.close()
            finally:
                await db_pool.close_pool()
                if auth._pool is not None:
                    await auth._pool.close()
                    auth._pool = None
