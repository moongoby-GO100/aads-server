"""OHVIS main chat: pure contract/gate tests always run; DB tests need OHVIS_MAIN_CHAT_TEST_DATABASE_URL (*_test)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models.ohvis_main_chat import (
    CARD_LABELS, ControlRequest, GrantCreate, ReportEvent, RouteUpsert, effect_key, sha256_hex,
)
from app.services import ohvis_main_chat_service as svc

ROOT = Path(__file__).parents[2]
UP = (ROOT / "migrations/20261005_ohvis_main_chat.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20261005_ohvis_main_chat.down.sql").read_text()
DATABASE_URL = os.getenv("OHVIS_MAIN_CHAT_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="OHVIS_MAIN_CHAT_TEST_DATABASE_URL is not configured")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64
COMMIT = "abc1234"
JOB_SPEC = {"instruction": "follow up", "project": "AADS"}


def event_dict(**over):
    base = {"schema_version": 1, "event_type": "runner_completed", "source_kind": "runner",
            "source_event_id": "evt-1", "source_revision": 1, "project_key": "aads", "root_task_id": "TASK-1",
            "correlation_id": "corr-1", "commit_sha": COMMIT, "diff_sha256": SHA, "summary": "done",
            "occurred_at": NOW.isoformat()}
    base.update(over)
    return base


def grant_dict(**over):
    base = {"grant_type": "execute_followup", "project_key": "AADS", "root_task_id": "TASK-1", "commit_sha": COMMIT,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "scope": {"after_event_type": "runner_completed", "job_spec": JOB_SPEC, "allowed_files": ["app/x.py"]}}
    base.update(over)
    return base


GOOD_EVIDENCE = {"commit_found": True, "diff_sha256": SHA, "changed_files": ["app/x.py"], "allowed_files": ["app/x.py"]}


# ------------------------------------------------------------------------------------------ pure contracts

def test_report_event_rejects_client_supplied_routing_and_self_source():
    for extra in ({"tenant_id": str(uuid4())}, {"main_session_id": str(uuid4())}, {"route_revision": 3}):
        with pytest.raises(ValidationError):
            ReportEvent(**event_dict(**extra))
    with pytest.raises(ValidationError):
        ReportEvent(**event_dict(source_kind="ohvis_main_chat"))
    with pytest.raises(ValidationError):
        ReportEvent(**event_dict(schema_version=2))
    with pytest.raises(ValidationError):
        ReportEvent(**event_dict(commit_sha="not-a-sha"))


def test_payload_hash_is_stable_and_content_sensitive():
    a, b = ReportEvent(**event_dict()), ReportEvent(**event_dict())
    assert a.payload_hash() == b.payload_hash()
    assert a.payload_hash() != ReportEvent(**event_dict(summary="other")).payload_hash()
    assert a.project_key == "AADS"


def test_effect_key_is_semantic():
    base = effect_key("t", "AADS", "T1", "submit_followup_job", SHA, "g1")
    assert base == effect_key("t", "AADS", "T1", "submit_followup_job", SHA, "g1")
    assert base != effect_key("t", "AADS", "T1", "submit_followup_job", SHA, "g2")
    assert base != effect_key("t2", "AADS", "T1", "submit_followup_job", SHA, "g1")


def test_grant_contract_is_explicit_and_unpromotable():
    assert GrantCreate(**grant_dict()).target_hash() == sha256_hex(JOB_SPEC)
    for bad in (grant_dict(grant_type="design_approval"), grant_dict(grant_type="code_review"),
                grant_dict(commit_sha=None), grant_dict(commit_sha=None, generation_id=None)):
        with pytest.raises(ValidationError):
            GrantCreate(**bad)
    for files in (["*"], ["../x"], ["/etc/passwd"], ["."], []):
        scope = {"after_event_type": "runner_completed", "job_spec": JOB_SPEC, "allowed_files": files}
        with pytest.raises(ValidationError):
            GrantCreate(**grant_dict(scope=scope))
    with pytest.raises(ValidationError):
        GrantCreate(**grant_dict(scope={"after_event_type": "runner_completed", "job_spec": {}, "allowed_files": ["a"]}))
    with pytest.raises(ValidationError):
        RouteUpsert(main_session_id=uuid4(), expected_revision=0, tenant_id=str(uuid4()))
    with pytest.raises(ValidationError):
        ControlRequest(project_key="AADS", action="delete", expected_revision=0)


def test_flags_default_to_off_and_unset_cap_is_none(monkeypatch):
    for name in ("OHVIS_MAIN_CHAT_ENABLED", "OHVIS_MAIN_CHAT_AUTO_EFFECT", "OHVIS_MAIN_CHAT_MODEL_CALL_CAP",
                 "OHVIS_MAIN_CHAT_MAX_EFFECTS_PER_ROOT"):
        monkeypatch.delenv(name, raising=False)
    flags = svc.load_flags()
    assert (flags.enabled, flags.auto_effect, flags.model_call_cap, flags.max_effects_per_root) == (False, False, None, None)
    monkeypatch.setenv("OHVIS_MAIN_CHAT_MODEL_CALL_CAP", "garbage")
    assert svc.load_flags().model_call_cap is None


def test_decide_review_matrix():
    ev = ReportEvent(**event_dict()).model_dump(mode="json")
    assert svc.decide_review(ev, GOOD_EVIDENCE)[:2] == ("verified", True)
    assert svc.decide_review(ev, {**GOOD_EVIDENCE, "diff_sha256": "b" * 64})[0] == "needs_rework"
    assert svc.decide_review(ev, {**GOOD_EVIDENCE, "changed_files": ["app/other.py"]})[0] == "needs_rework"
    assert svc.decide_review(ev, {**GOOD_EVIDENCE, "commit_found": False})[0] == "unverifiable"
    assert svc.decide_review(ev, None)[:2] == ("unverifiable", False)
    assert svc.decide_review({**ev, "commit_sha": None}, GOOD_EVIDENCE)[0] == "unverifiable"
    assert svc.decide_review({**ev, "event_type": "runner_failed"}, None)[0] == "needs_rework"
    assert svc.decide_review({**ev, "event_type": "report_only"}, None)[:2] == ("needs_decision", False)
    assert svc.decide_review(ev, GOOD_EVIDENCE, superseded=True)[0] == "obsolete"


def _gate_inputs(**over):
    tenant = str(uuid4())
    grant = {"tenant_id": tenant, "project_key": "AADS", "root_task_id": "TASK-1", "grant_type": "execute_followup",
             "revoked_at": None, "expires_at": NOW + timedelta(hours=1), "revision": 1, "commit_sha": COMMIT,
             "generation_id": None, "target_hash": sha256_hex(JOB_SPEC), "scope": {"job_spec": JOB_SPEC}}
    effect = {"tenant_id": tenant, "project_key": "AADS", "root_task_id": "TASK-1", "owner_epoch": 1,
              "grant_revision": 1, "payload": {"job_spec": JOB_SPEC}}
    args = dict(action="submit_followup_job", grant=grant, inbox={"owner_epoch": 1, "state": "reviewed",
                                                                    "commit_sha": COMMIT, "generation_id": None},
                review={"decision": "verified", "code_verified": True}, effect=effect, paused=False,
                flags=svc.Flags(True, True, 3, 2), counted_effects=0, adapter_idempotent=True, now=NOW)
    args.update(over)
    return args


def test_gate_allows_only_the_fully_bound_case():
    assert svc.authorize_effect(**_gate_inputs())[:2] == (True, "ok")


@pytest.mark.parametrize("mutate,reason,permanent", [
    (lambda a: a.update(action="deploy"), "action_has_no_adapter", True),
    (lambda a: a.update(action="push"), "action_has_no_adapter", True),
    (lambda a: a.update(flags=svc.Flags(False, True, 3, 2)), "feature_disabled", False),
    (lambda a: a.update(flags=svc.Flags(True, False, 3, 2)), "auto_effect_off", False),
    (lambda a: a.update(paused=True), "project_paused", False),
    (lambda a: a.update(flags=svc.Flags(True, True, 3, None)), "effect_budget_unset", False),
    (lambda a: a.update(counted_effects=2), "effect_budget_exhausted", True),
    (lambda a: a.update(adapter_idempotent=False), "adapter_without_idempotent_lookup", True),
    (lambda a: a.update(review={"decision": "needs_rework", "code_verified": False}), "review_not_verified", True),
    (lambda a: a.update(review={"decision": "verified", "code_verified": False}), "review_not_verified", True),
    (lambda a: a["inbox"].update(owner_epoch=2), "stale_epoch", True),
    (lambda a: a["inbox"].update(state="reviewing"), "inbox_not_reviewed", True),
    (lambda a: a.update(grant=None), "grant_missing", True),
    (lambda a: a["grant"].update(grant_type="design_approval"), "grant_type_mismatch", True),
    (lambda a: a["grant"].update(grant_type="push"), "grant_type_mismatch", True),
    (lambda a: a["grant"].update(revoked_at=NOW), "grant_revoked", True),
    (lambda a: a["grant"].update(expires_at=NOW - timedelta(seconds=1)), "grant_expired", True),
    (lambda a: a["grant"].update(revision=2), "grant_revision_changed", True),
    (lambda a: a["grant"].update(project_key="OTHER"), "grant_scope_mismatch", True),
    (lambda a: a["grant"].update(tenant_id=str(uuid4())), "grant_scope_mismatch", True),
    (lambda a: a["grant"].update(root_task_id="TASK-2"), "grant_scope_mismatch", True),
    (lambda a: a["grant"].update(commit_sha="def5678"), "commit_mismatch", True),
    (lambda a: a["grant"].update(commit_sha=None, generation_id="g1"), "generation_mismatch", True),
    (lambda a: a["grant"].update(commit_sha=None), "grant_unbound", True),
    (lambda a: a["grant"].update(target_hash="0" * 64), "target_hash_mismatch", True),
    (lambda a: a["effect"].update(payload={"job_spec": {"instruction": "different"}}), "target_hash_mismatch", True),
])
def test_gate_denials(mutate, reason, permanent):
    args = _gate_inputs()
    mutate(args)
    allowed, why, perm = svc.authorize_effect(**args)
    assert (allowed, why, perm) == (False, reason, permanent)


def test_cards_never_claim_completion():
    for label in CARD_LABELS.values():
        assert "완료" not in label and "complete" not in label.lower()
    assert svc.card_label("reviewed", "verified") == CARD_LABELS["verified"]
    assert svc.card_label("pending_review", None) == CARD_LABELS["report_arrived"]


def test_report_with_secret_is_rejected_before_any_storage():
    leaked = ReportEvent(**event_dict(summary="password=hunter2hunter2"))
    with pytest.raises(svc.MainChatError) as err:
        asyncio.run(svc.ingest(None, str(uuid4()), "runner", leaked))
    assert err.value.code == "secret_in_report"


def test_standby_slot_never_dispatches_or_recovers():
    standby = lambda: False  # noqa: E731
    assert asyncio.run(svc.dispatch_effect(None, str(uuid4()), None, slot_active=standby))["reason"] == "standby_slot"
    assert asyncio.run(svc.recover_unknown(None, FakeAdapter(), slot_active=standby)) == []


def test_router_exposes_no_deploy_or_push_surface():
    from app.api import ohvis_main_chat as api
    paths = {route.path for route in api.router.routes}
    assert paths and not any("deploy" in p or "push" in p for p in paths)
    source = (ROOT / "app/api/ohvis_main_chat.py").read_text() + (ROOT / "app/services/ohvis_main_chat_service.py").read_text()
    forbidden = "ANTHROPIC_" + "API_KEY"
    assert forbidden not in source


# ------------------------------------------------------------------------------------------ mounted API surface

TENANT = str(uuid4())
OTHER_TENANT = str(uuid4())
BASE = "/api/v1/projects/aads/main-chat"
ALL_PATHS = {
    ("get", "/api/v1/projects/{project_key}/main-chat"),
    ("put", "/api/v1/projects/{project_key}/main-chat/routes"),
    ("post", "/api/v1/projects/{project_key}/main-chat/reports"),
    ("post", "/api/v1/projects/{project_key}/main-chat/evidence/requeue"),
    ("post", "/api/v1/projects/{project_key}/main-chat/control"),
    ("get", "/api/v1/projects/{project_key}/main-chat/grants"),
    ("post", "/api/v1/projects/{project_key}/main-chat/grants"),
    ("post", "/api/v1/projects/{project_key}/main-chat/grants/{grant_id}/revoke"),
    ("get", "/api/v1/projects/{project_key}/main-chat/notices"),
    ("post", "/api/v1/projects/{project_key}/main-chat/notices/{notice_id}/read"),
}


class _FakeConn:
    def __init__(self, has_grant: bool):
        self.has_grant = has_grant
        self.queries: list[str] = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return self.has_grant


class _FakePool:
    def __init__(self, has_grant: bool = True):
        self.conn = _FakeConn(has_grant)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _context(*, role="member", internal_admin=False, tenant=TENANT):
    return {"user": {"user_id": "user-1", "is_internal_admin": internal_admin}, "tenant": {"id": tenant},
            "membership": {"role": role}}


def api_client(monkeypatch, *, context=None, has_grant=True, enabled=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import canonical_documents as docs
    from app.api import ohvis_main_chat as api

    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    if context is not None:
        app.dependency_overrides[docs.VIEW.dependency] = lambda: context
        app.dependency_overrides[docs.WRITE.dependency] = lambda: context
    pool = _FakePool(has_grant)
    monkeypatch.setattr(api, "get_pool", lambda: pool)
    if enabled:
        monkeypatch.setenv("OHVIS_MAIN_CHAT_ENABLED", "1")
    else:
        monkeypatch.delenv("OHVIS_MAIN_CHAT_ENABLED", raising=False)
    return TestClient(app, raise_server_exceptions=False), pool


def _forbid_service(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("service must not be reached")

    for name in ("upsert_route", "ingest", "requeue_waiting_evidence", "set_control", "list_grants", "create_grant",
                 "revoke_grant", "list_cards", "list_notices", "mark_notice_read"):
        monkeypatch.setattr(svc, name, boom)


def test_router_is_mounted_in_main_app_with_the_api_prefix():
    import ast
    tree = ast.parse((ROOT / "app/main.py").read_text())
    imported = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "app.api.ohvis_main_chat"
                and any(a.name == "router" and a.asname == "ohvis_main_chat_router" for a in n.names)]
    mounts = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "include_router"
              and n.args and getattr(n.args[0], "id", "") == "ohvis_main_chat_router"]
    assert len(imported) == 1 and len(mounts) == 1
    assert {k.arg: getattr(k.value, "value", None) for k in mounts[0].keywords} == {"prefix": "/api/v1"}


def test_openapi_exposes_exactly_the_main_chat_surface(monkeypatch):
    client, _ = api_client(monkeypatch)
    paths = client.get("/openapi.json").json()["paths"]
    found = {(method, path) for path, ops in paths.items() for method in ops}
    assert found == ALL_PATHS
    assert not any("deploy" in p or "push" in p for _, p in found)


def test_every_endpoint_rejects_unauthenticated_callers(monkeypatch):
    client, pool = api_client(monkeypatch)
    _forbid_service(monkeypatch)
    for method, path in ALL_PATHS:
        url = path.replace("{project_key}", "aads").replace("{grant_id}", str(uuid4())).replace("{notice_id}", "1")
        response = client.request(method, url, json={} if method != "get" else None)
        assert response.status_code in (401, 403), (method, path, response.status_code)
    assert pool.conn.queries == []


def test_flag_off_blocks_every_write_except_the_pause_switch(monkeypatch):
    client, pool = api_client(monkeypatch, context=_context(internal_admin=True), enabled=False)
    _forbid_service(monkeypatch)
    writes = [("put", "/routes", {"main_session_id": str(uuid4()), "expected_revision": 0}),
              ("post", "/reports", event_dict()),
              ("post", "/evidence/requeue", None),
              ("post", "/grants", grant_dict())]
    for method, suffix, body in writes:
        response = client.request(method, BASE + suffix, json=body)
        assert response.status_code == 503 and response.json()["detail"] == {"error": "main_chat_disabled"}, suffix
    assert pool.conn.queries == []

    async def fake_control(conn, tenant, actor, body):
        return {"paused": True}

    monkeypatch.setattr(svc, "set_control", fake_control)
    response = client.post(BASE + "/control", json={"project_key": "AADS", "action": "pause", "reason": "stop",
                                                    "expected_revision": 0})
    assert response.status_code == 200 and response.json() == {"paused": True}


def test_plain_members_cannot_change_routing_grants_control_or_runner_reports(monkeypatch):
    client, _ = api_client(monkeypatch, context=_context(role="member"))
    _forbid_service(monkeypatch)
    calls = [("put", "/routes", {"main_session_id": str(uuid4()), "expected_revision": 0}),
             ("post", "/reports", event_dict()),
             ("post", "/evidence/requeue", None),
             ("post", "/control", {"project_key": "AADS", "action": "pause", "reason": "x", "expected_revision": 0}),
             ("post", "/grants", grant_dict()),
             ("post", f"/grants/{uuid4()}/revoke", {"reason": "no longer needed"})]
    for method, suffix, body in calls:
        response = client.request(method, BASE + suffix, json=body)
        assert response.status_code == 403 and response.json()["detail"] == {"error": "admin_role_required"}, suffix


def test_project_grant_is_required_for_non_elevated_callers(monkeypatch):
    client, pool = api_client(monkeypatch, context=_context(role="member"), has_grant=False)
    _forbid_service(monkeypatch)
    for response in (client.get(BASE), client.get(BASE + "/grants"), client.get(BASE + "/notices"),
                     client.post(BASE + "/notices/1/read"),
                     client.post(BASE + "/reports", json=event_dict(source_kind="agent"))):
        assert response.status_code == 403 and response.json()["detail"] == "project_access_denied"
    assert pool.conn.queries


def test_path_project_must_match_body_project(monkeypatch):
    client, _ = api_client(monkeypatch, context=_context(internal_admin=True))
    _forbid_service(monkeypatch)
    for suffix, body in (("/reports", event_dict(project_key="OTHER")), ("/grants", grant_dict(project_key="OTHER")),
                         ("/control", {"project_key": "OTHER", "action": "pause", "reason": "x",
                                       "expected_revision": 0})):
        response = client.post(BASE + suffix, json=body)
        assert response.status_code == 422 and response.json()["detail"] == {"error": "project_key_mismatch"}


def test_tenant_comes_from_the_authenticated_context_never_the_request(monkeypatch):
    client, _ = api_client(monkeypatch, context=_context(internal_admin=True, tenant=TENANT))
    seen: list[str] = []

    async def fake_cards(conn, tenant, project, limit):
        seen.append(tenant)
        return {"cards": []}

    async def fake_ingest(conn, tenant, actor, body):
        seen.append(tenant)
        return {"id": "1", "state": "pending_review", "duplicate": False}

    monkeypatch.setattr(svc, "list_cards", fake_cards)
    monkeypatch.setattr(svc, "ingest", fake_ingest)
    assert client.get(BASE + f"?tenant_id={OTHER_TENANT}", headers={"X-Tenant-Id": OTHER_TENANT}).status_code == 200
    assert client.post(BASE + "/reports", json=event_dict()).status_code == 202
    extra = client.post(BASE + "/reports", json=event_dict(tenant_id=OTHER_TENANT))
    assert extra.status_code == 422
    assert seen == [TENANT, TENANT]


def test_missing_migration_is_a_503_not_a_500(monkeypatch):
    import asyncpg
    client, _ = api_client(monkeypatch, context=_context(internal_admin=True))

    async def missing(*a, **k):
        raise asyncpg.UndefinedTableError('relation "ohvis_report_inbox" does not exist')

    monkeypatch.setattr(svc, "list_cards", missing)
    response = client.get(BASE)
    assert response.status_code == 503 and response.json()["detail"] == {"error": "main_chat_not_migrated"}


def test_service_errors_keep_their_status_and_code(monkeypatch):
    client, _ = api_client(monkeypatch, context=_context(internal_admin=True))

    async def conflict(*a, **k):
        raise svc.MainChatError(409, "payload_conflict", inbox_id="x")

    monkeypatch.setattr(svc, "ingest", conflict)
    response = client.post(BASE + "/reports", json=event_dict())
    assert response.status_code == 409 and response.json()["detail"] == {"error": "payload_conflict", "inbox_id": "x"}


def test_notices_listing_is_limited_to_the_path_project(monkeypatch):
    client, _ = api_client(monkeypatch, context=_context(role="viewer", internal_admin=True))

    async def fake_notices(conn, tenant, user, unread_only):
        return [{"project_key": "AADS", "id": 1}, {"project_key": "OTHER", "id": 2}]

    monkeypatch.setattr(svc, "list_notices", fake_notices)
    assert client.get(BASE + "/notices").json() == [{"project_key": "AADS", "id": 1}]


# ------------------------------------------------------------------------------------------ DB tests

BASELINE = """
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
CREATE TABLE chat_turn_executions (id uuid PRIMARY KEY, status text, completed_at timestamptz, lease_expires_at timestamptz);
CREATE TABLE chat_sessions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, current_execution_id uuid);
"""


class FakeAdapter:
    def __init__(self, *, idempotent=True, fail=False, lookup_status="not_found"):
        self.supports_idempotent_lookup = idempotent
        self.fail = fail
        self.lookup_status = lookup_status
        self.calls: list[str] = []
        self.remote: dict[str, str] = {}

    async def submit(self, effect_key, spec):
        self.calls.append(effect_key)
        if self.fail:
            raise TimeoutError("ambiguous")
        self.remote[effect_key] = f"runner-{len(self.calls)}"
        return {"remote_ref": self.remote[effect_key]}

    async def lookup(self, effect_key):
        if self.lookup_status == "found":
            return {"status": "found", "remote_ref": "runner-existing"}
        return {"status": self.lookup_status}


def db_test(fn):
    def wrapper(monkeypatch):
        if urlparse(DATABASE_URL).path.removeprefix("/") and not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
            pytest.fail("OHVIS_MAIN_CHAT_TEST_DATABASE_URL database name must end in _test")
        import asyncpg
        monkeypatch.setattr(svc, "default_slot_active", lambda: True)
        for name in ("OHVIS_MAIN_CHAT_ENABLED", "OHVIS_MAIN_CHAT_AUTO_EFFECT", "OHVIS_MAIN_CHAT_MODEL_CALL_CAP",
                     "OHVIS_MAIN_CHAT_MAX_EFFECTS_PER_ROOT"):
            monkeypatch.delenv(name, raising=False)

        async def run():
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6)
            try:
                async with pool.acquire() as conn:
                    await conn.execute(BASELINE)
                    await conn.execute(UP)
                await fn(pool, monkeypatch)
            finally:
                await pool.close()

        asyncio.run(run())
    wrapper.__name__ = fn.__name__
    return wrapper


async def make_session(pool, tenant):
    session = uuid4()
    await pool.execute("INSERT INTO chat_sessions (id, tenant_id) VALUES ($1,$2)", session, tenant)
    return session


async def make_route(pool, tenant, *, accepts_goal=False, actor="admin-1"):
    session = await make_session(pool, tenant)
    async with pool.acquire() as conn:
        await svc.upsert_route(conn, str(tenant), actor, "AADS",
                               RouteUpsert(main_session_id=session, accepts_goal_events=accepts_goal, expected_revision=0))
    return session


def enable(monkeypatch, *, auto=False, cap=3, max_effects=2):
    monkeypatch.setenv("OHVIS_MAIN_CHAT_ENABLED", "1")
    if auto:
        monkeypatch.setenv("OHVIS_MAIN_CHAT_AUTO_EFFECT", "1")
    if cap:
        monkeypatch.setenv("OHVIS_MAIN_CHAT_MODEL_CALL_CAP", str(cap))
    if max_effects:
        monkeypatch.setenv("OHVIS_MAIN_CHAT_MAX_EFFECTS_PER_ROOT", str(max_effects))


async def ingest(pool, tenant, **over):
    async with pool.acquire() as conn:
        return await svc.ingest(conn, str(tenant), "runner-svc", ReportEvent(**event_dict(**over)))


async def review_next(pool, evidence=GOOD_EVIDENCE):
    async with pool.acquire() as conn:
        claimed = await svc.claim_next(conn, "inst-1", slot_active=lambda: True)
    assert claimed is not None
    async with pool.acquire() as conn:
        return claimed, await svc.record_review(conn, str(claimed["id"]), claimed["owner_epoch"], evidence)


async def grant(pool, tenant, **over):
    async with pool.acquire() as conn:
        return await svc.create_grant(conn, str(tenant), "admin-1", GrantCreate(**grant_dict(**over)))


@needs_db
@db_test
async def test_migration_is_idempotent_and_rollback_drops_everything(pool, monkeypatch):
    async with pool.acquire() as conn:
        await conn.execute(UP)
        await conn.execute(DOWN)
        assert await conn.fetchval("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'ohvis_%'") == 0
        await conn.execute(UP)
        assert await conn.fetchval("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'ohvis_%'") == 8


@needs_db
@db_test
async def test_inbox_is_idempotent_conflict_quarantined_and_tenant_scoped(pool, monkeypatch):
    t1, t2 = uuid4(), uuid4()
    await make_route(pool, t1)
    first = await ingest(pool, t1)
    again = await ingest(pool, t1)
    assert again["duplicate"] and again["id"] == first["id"]
    row = await pool.fetchrow("SELECT delivery_attempts, state FROM ohvis_report_inbox WHERE id=$1", UUID(first["id"]))
    assert row["delivery_attempts"] == 1 and row["state"] == "pending_review"
    with pytest.raises(svc.MainChatError) as err:
        await ingest(pool, t1, summary="tampered")
    assert err.value.code == "payload_conflict"
    assert await pool.fetchval("SELECT count(*) FROM ohvis_report_inbox WHERE tenant_id=$1", t1) == 1
    assert await pool.fetchval("SELECT count(*) FROM ohvis_main_chat_audit WHERE action='payload_conflict'") == 1
    other = await ingest(pool, t2)
    assert other["id"] != first["id"] and other["state"] == "waiting_route"
    async with pool.acquire() as conn:
        assert (await svc.list_cards(conn, str(t2), "AADS"))["cards"][0]["id"] == other["id"]
        assert len((await svc.list_cards(conn, str(t1), "AADS"))["cards"]) == 1


@needs_db
@db_test
async def test_routing_goal_scope_and_cas(pool, monkeypatch):
    t, goal, doc_goal = uuid4(), uuid4(), uuid4()
    parked = await ingest(pool, t, goal_id=str(goal))
    assert parked["state"] == "waiting_route"
    session = await make_session(pool, t)
    async with pool.acquire() as conn:
        view = await svc.upsert_route(conn, str(t), "admin", "AADS",
                                      RouteUpsert(main_session_id=session, accepts_goal_events=False, expected_revision=0))
        assert view["released_waiting_reports"] == 0
    assert (await ingest(pool, t, source_event_id="evt-2", goal_id=str(goal)))["state"] == "waiting_route"
    assert (await ingest(pool, t, source_event_id="evt-3"))["state"] == "pending_review"
    async with pool.acquire() as conn:
        with pytest.raises(svc.MainChatError) as err:
            await svc.upsert_route(conn, str(t), "admin", "AADS", RouteUpsert(main_session_id=session, expected_revision=0))
        assert err.value.code == "revision_conflict"
        await svc.upsert_route(conn, str(t), "admin", "AADS",
                               RouteUpsert(main_session_id=session, accepts_goal_events=True, expected_revision=1))
        goal_session = await make_session(pool, t)
        await svc.upsert_route(conn, str(t), "admin", "AADS",
                               RouteUpsert(goal_id=goal, main_session_id=goal_session, expected_revision=0))
        with pytest.raises(svc.MainChatError) as err:
            await svc.upsert_route(conn, str(t), "admin", "AADS", RouteUpsert(main_session_id=await make_session(pool, uuid4()), expected_revision=2))
        assert err.value.code == "session_not_in_tenant"
        route = await svc.resolve_route(conn, str(t), "AADS", goal)
        assert str(route["main_session_id"]) == str(goal_session)
        project_level = await svc.resolve_route(conn, str(t), "AADS", doc_goal)
        assert project_level is not None and str(project_level["main_session_id"]) == str(session)
    assert await pool.fetchval("SELECT count(*) FROM ohvis_report_inbox WHERE state='waiting_route'") == 0
    assert await pool.fetchval("SELECT count(*) FROM ohvis_report_inbox WHERE state='pending_review'") == 3


@needs_db
@db_test
async def test_claim_fencing_busy_session_pause_and_counters(pool, monkeypatch):
    t = uuid4()
    session = await make_route(pool, t)
    await ingest(pool, t)
    async with pool.acquire() as conn:
        assert await svc.claim_next(conn, "i1", slot_active=lambda: True) is None  # feature off
    enable(monkeypatch)
    async with pool.acquire() as conn:
        assert await svc.claim_next(conn, "i1", slot_active=lambda: False) is None  # standby slot
    execution = uuid4()
    await pool.execute("INSERT INTO chat_turn_executions VALUES ($1,'running',NULL, now() + interval '5 minutes')", execution)
    await pool.execute("UPDATE chat_sessions SET current_execution_id=$2 WHERE id=$1", session, execution)
    async with pool.acquire() as conn:
        assert await svc.claim_next(conn, "i1", slot_active=lambda: True) is None
    row = await pool.fetchrow("SELECT state, claim_count, model_call_attempts FROM ohvis_report_inbox")
    assert (row["state"], row["claim_count"], row["model_call_attempts"]) == ("waiting_session", 0, 0)
    await pool.execute("UPDATE chat_turn_executions SET status='completed', completed_at=now()")
    async with pool.acquire() as conn:
        await svc.set_control(conn, str(t), "admin", ControlRequest(project_key="AADS", action="pause", expected_revision=0))
        assert await svc.claim_next(conn, "i1", slot_active=lambda: True) is None  # paused
        with pytest.raises(svc.MainChatError):
            await svc.set_control(conn, str(t), "admin", ControlRequest(project_key="AADS", action="resume", expected_revision=0))
        await svc.set_control(conn, str(t), "admin", ControlRequest(project_key="AADS", action="resume", expected_revision=1))
        first = await svc.claim_next(conn, "i1", slot_active=lambda: True)
    assert first["owner_epoch"] == 1 and first["claim_count"] == 1 and first["model_call_attempts"] == 0
    async with pool.acquire() as conn:
        assert await svc.claim_next(conn, "i2", slot_active=lambda: True) is None  # live lease holds
    await pool.execute("UPDATE ohvis_report_inbox SET lease_expires_at = now() - interval '1 second'")
    async with pool.acquire() as conn:
        second = await svc.claim_next(conn, "i2", slot_active=lambda: True)
        assert second["owner_epoch"] == 2 and second["claim_count"] == 2 and second["model_call_attempts"] == 0
        with pytest.raises(svc.MainChatError) as err:
            await svc.record_review(conn, str(first["id"]), first["owner_epoch"], GOOD_EVIDENCE)
        assert err.value.code == "stale_epoch"
        assert await svc.reserve_model_call(conn, str(second["id"]), 1) is False  # stale epoch
        assert await svc.reserve_model_call(conn, str(second["id"]), 2) is True
        assert await svc.reserve_model_call(conn, str(second["id"]), 2) is True
        assert await svc.reserve_model_call(conn, str(second["id"]), 2) is True
        assert await svc.reserve_model_call(conn, str(second["id"]), 2) is False  # cap 3 spent
    assert await pool.fetchval("SELECT count(*) FROM ohvis_report_reviews") == 0


@needs_db
@db_test
async def test_concurrent_claims_lease_one_report_once(pool, monkeypatch):
    t = uuid4()
    await make_route(pool, t)
    await ingest(pool, t)
    enable(monkeypatch)

    async def claim(name):
        async with pool.acquire() as conn:
            return await svc.claim_next(conn, name, slot_active=lambda: True)

    results = await asyncio.gather(*(claim(f"i{n}") for n in range(5)))
    assert sum(r is not None for r in results) == 1


@needs_db
@db_test
async def test_review_evidence_supersede_and_unverifiable(pool, monkeypatch):
    t = uuid4()
    await make_route(pool, t)
    enable(monkeypatch)
    await ingest(pool, t)
    _, waiting = await review_next(pool, evidence=None)
    assert waiting["state"] == "waiting_evidence"
    async with pool.acquire() as conn:
        assert await svc.requeue_waiting_evidence(conn, str(t), "admin", "AADS") == 1
    _, verified = await review_next(pool)
    assert verified["decision"] == "verified" and verified["code_verified"] is True
    await ingest(pool, t, source_event_id="evt-2", source_revision=2, commit_sha="def5678")
    stale = await ingest(pool, t, source_event_id="evt-0", source_revision=1)
    assert stale["state"] == "obsolete"
    _, rework = await review_next(pool, evidence={**GOOD_EVIDENCE, "diff_sha256": "b" * 64})
    assert rework["decision"] == "needs_rework" and rework["code_verified"] is False
    async with pool.acquire() as conn:
        cards = (await svc.list_cards(conn, str(t), "AADS"))["cards"]
    assert all(c["badges"]["tests_rechecked"] is False for c in cards)
    assert {c["label"] for c in cards}.isdisjoint({"완료"})


@needs_db
@db_test
async def test_effect_flow_is_gated_idempotent_and_never_doubles(pool, monkeypatch):
    t = uuid4()
    await make_route(pool, t)
    enable(monkeypatch, auto=False)
    wrong = await grant(pool, t, commit_sha="def5678")
    push = await grant(pool, t, grant_type="push")
    assert wrong["created"] and push["created"]
    assert (await grant(pool, t))["created"] is True
    again = await grant(pool, t)
    assert again["created"] is False
    await ingest(pool, t)
    _, review = await review_next(pool)
    assert len(review["planned_effects"]) == 1  # only the bound execute_followup grant matches
    effect_id = review["planned_effects"][0]
    adapter = FakeAdapter()
    off = await svc.dispatch_effect(pool, effect_id, adapter)
    assert off == {"state": "pending", "dispatched": False, "reason": "auto_effect_off"}
    monkeypatch.setenv("OHVIS_MAIN_CHAT_AUTO_EFFECT", "1")
    async with pool.acquire() as conn:
        await svc.set_control(conn, str(t), "admin", ControlRequest(project_key="AADS", action="pause", expected_revision=0))
    assert (await svc.dispatch_effect(pool, effect_id, adapter))["reason"] == "project_paused"
    assert adapter.calls == []
    async with pool.acquire() as conn:
        await svc.set_control(conn, str(t), "admin", ControlRequest(project_key="AADS", action="resume", expected_revision=1))
    sent = await svc.dispatch_effect(pool, effect_id, adapter)
    assert sent["state"] == "confirmed" and sent["remote_ref"] == "runner-1"
    assert (await svc.dispatch_effect(pool, effect_id, adapter))["dispatched"] is False
    await ingest(pool, t)  # redelivery
    assert len(adapter.calls) == 1
    assert await pool.fetchval("SELECT count(*) FROM ohvis_action_outbox") == 1
    async with pool.acquire() as conn:
        card = (await svc.list_cards(conn, str(t), "AADS"))["cards"][0]
    assert card["effects"][0]["result_link"] == "/pipeline/jobs/runner-1"
    notices = await pool.fetch("SELECT link, kind FROM ohvis_main_chat_notices")
    assert notices and all(n["link"].startswith("/projects/AADS/") for n in notices)


@needs_db
@db_test
async def test_revoked_expired_unsupported_and_no_lookup_adapters_block(pool, monkeypatch):
    t = uuid4()
    await make_route(pool, t)
    enable(monkeypatch, auto=True)
    made = await grant(pool, t)
    await ingest(pool, t)
    _, review = await review_next(pool)
    effect_id = review["planned_effects"][0]
    blocked = await svc.dispatch_effect(pool, effect_id, FakeAdapter(idempotent=False))
    assert blocked["reason"] == "adapter_without_idempotent_lookup" and blocked["state"] == "blocked"
    assert (await svc.dispatch_effect(pool, effect_id, NullAdapterFree()))["state"] == "blocked"

    t2 = uuid4()
    await make_route(pool, t2)
    await grant(pool, t2)
    await ingest(pool, t2)
    _, review2 = await review_next(pool)
    async with pool.acquire() as conn:
        grant_id = (await conn.fetchval("SELECT id FROM ohvis_action_grants WHERE tenant_id=$1", t2))
        await svc.revoke_grant(conn, str(t2), "admin", str(grant_id), "no longer wanted")
    adapter = FakeAdapter()
    assert (await svc.dispatch_effect(pool, review2["planned_effects"][0], adapter))["state"] == "blocked"
    assert adapter.calls == []
    assert made["created"]

    t3 = uuid4()
    await make_route(pool, t3)
    await grant(pool, t3)
    await ingest(pool, t3)
    _, review3 = await review_next(pool)
    await pool.execute("UPDATE ohvis_action_grants SET expires_at = now() - interval '1 second' WHERE tenant_id=$1", t3)
    assert (await svc.dispatch_effect(pool, review3["planned_effects"][0], adapter))["reason"] == "grant_expired"
    assert adapter.calls == []


class NullAdapterFree:
    supports_idempotent_lookup = False


@needs_db
@db_test
async def test_unknown_outcome_is_recovered_by_lookup_not_blind_retry(pool, monkeypatch):
    t = uuid4()
    await make_route(pool, t)
    enable(monkeypatch, auto=True)
    await grant(pool, t)
    await ingest(pool, t)
    _, review = await review_next(pool)
    effect_id = review["planned_effects"][0]
    broken = FakeAdapter(fail=True)
    assert (await svc.dispatch_effect(pool, effect_id, broken))["state"] == "unknown"
    assert (await svc.dispatch_effect(pool, effect_id, broken))["dispatched"] is False  # unknown is not re-sent
    assert len(broken.calls) == 1
    key = broken.calls[0]
    assert (await svc.recover_unknown(pool, FakeAdapter(lookup_status="unavailable")))[0]["state"] == "unknown"
    assert await svc.recover_unknown(pool, FakeAdapter(idempotent=False)) == []
    assert (await svc.recover_unknown(pool, FakeAdapter(lookup_status="not_found")))[0]["state"] == "authorized"
    good = FakeAdapter()
    assert (await svc.dispatch_effect(pool, effect_id, good))["state"] == "confirmed"
    assert good.calls == [key]  # same idempotency key as the first attempt
    assert await pool.fetchval("SELECT dispatch_attempts FROM ohvis_action_outbox") == 2

    t2 = uuid4()
    await make_route(pool, t2)
    await grant(pool, t2)
    await ingest(pool, t2)
    _, review2 = await review_next(pool)
    await svc.dispatch_effect(pool, review2["planned_effects"][0], FakeAdapter(fail=True))
    found = await svc.recover_unknown(pool, FakeAdapter(lookup_status="found"))
    assert found[0]["state"] == "confirmed"
    assert await pool.fetchval("SELECT remote_ref FROM ohvis_action_outbox WHERE tenant_id=$1", t2) == "runner-existing"


@needs_db
@db_test
async def test_effect_budget_and_audit_is_append_only(pool, monkeypatch):
    t = uuid4()
    await make_route(pool, t)
    enable(monkeypatch, auto=True, max_effects=1)
    await grant(pool, t)
    await grant(pool, t, scope={"after_event_type": "runner_completed", "job_spec": {"instruction": "second"},
                                "allowed_files": ["app/y.py"]})
    await ingest(pool, t)
    _, review = await review_next(pool)
    assert len(review["planned_effects"]) == 2
    adapter = FakeAdapter()
    assert (await svc.dispatch_effect(pool, review["planned_effects"][0], adapter))["state"] == "confirmed"
    over = await svc.dispatch_effect(pool, review["planned_effects"][1], adapter)
    assert over["state"] == "blocked" and over["reason"] == "effect_budget_exhausted"
    assert len(adapter.calls) == 1
    import asyncpg
    with pytest.raises(asyncpg.PostgresError):
        await pool.execute("UPDATE ohvis_main_chat_audit SET action='x'")
    with pytest.raises(asyncpg.PostgresError):
        await pool.execute("DELETE FROM ohvis_main_chat_audit")


@needs_db
@db_test
async def test_notices_are_deduped_tenant_scoped_and_markable(pool, monkeypatch):
    t1, t2 = uuid4(), uuid4()
    await make_route(pool, t1, actor="user-1")
    await ingest(pool, t1)
    async with pool.acquire() as conn:
        mine = await svc.list_notices(conn, str(t1), "user-1")
        assert [n["kind"] for n in mine] == ["report_arrived"]
        assert await svc.list_notices(conn, str(t2), "user-1") == []
        assert await svc.mark_notice_read(conn, str(t2), "user-1", mine[0]["id"]) is False
        assert await svc.mark_notice_read(conn, str(t1), "user-1", mine[0]["id"]) is True
        assert await svc.list_notices(conn, str(t1), "user-1") == []
    await ingest(pool, t1)
    assert await pool.fetchval("SELECT count(*) FROM ohvis_main_chat_notices") == 1


@needs_db
@db_test
async def test_mounted_api_round_trip_is_tenant_isolated(pool, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api import canonical_documents as docs
    from app.api import ohvis_main_chat as api

    enable(monkeypatch)
    t1, t2 = uuid4(), uuid4()
    session = await make_session(pool, t1)
    holder = {"ctx": _context(internal_admin=True, tenant=str(t1))}
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[docs.VIEW.dependency] = lambda: holder["ctx"]
    app.dependency_overrides[docs.WRITE.dependency] = lambda: holder["ctx"]
    monkeypatch.setattr(api, "get_pool", lambda: pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        route = await client.put(BASE + "/routes", json={"main_session_id": str(session), "expected_revision": 0})
        assert route.status_code == 200 and route.json()["revision"] == 1
        first = await client.post(BASE + "/reports", json=event_dict())
        again = await client.post(BASE + "/reports", json=event_dict())
        assert first.status_code == again.status_code == 202
        assert first.json()["state"] == "pending_review" and again.json() == {**first.json(), "duplicate": True}
        tampered = await client.post(BASE + "/reports", json=event_dict(summary="tampered"))
        assert tampered.status_code == 409 and tampered.json()["detail"]["error"] == "payload_conflict"
        granted = await client.post(BASE + "/grants", json=grant_dict())
        assert granted.status_code == 201 and granted.json()["created"]
        assert len((await client.get(BASE)).json()["cards"]) == 1
        assert len((await client.get(BASE + "/notices")).json()) == 1

        holder["ctx"] = _context(internal_admin=True, tenant=str(t2))
        overview = (await client.get(BASE)).json()
        assert overview["cards"] == [] and overview["routes"] == []
        assert (await client.get(BASE + "/grants")).json() == []
        assert (await client.get(BASE + "/notices")).json() == []
        stolen = await client.put(BASE + "/routes", json={"main_session_id": str(session), "expected_revision": 0})
        assert stolen.status_code == 422 and stolen.json()["detail"]["error"] == "session_not_in_tenant"
        revoke = await client.post(f"{BASE}/grants/{granted.json()['id']}/revoke", json={"reason": "not mine to revoke"})
        assert revoke.status_code == 404
    assert await pool.fetchval("SELECT revoked_at IS NULL FROM ohvis_action_grants WHERE tenant_id=$1", t1)
