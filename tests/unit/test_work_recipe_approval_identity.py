"""레시피 등록 승인 — 사람만 결정, 자기승인 차단, 고위험 재확인, 채팅 승인 카드, 목록 필드."""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import ohvis_console, ohvis_recipes
from app.services.work_recipe import approval_card, registration
from app.services.work_recipe.registration import (
    HighRiskConfirmationRequired,
    HumanDeciderRequired,
    SelfApprovalBlocked,
)

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
HUMAN_ID = "7fb5f50a-1111-4222-8333-444455556666"
HUMAN_EMAIL = "moongoby@gmail.com"
SECRET_VALUE = "S3cretPassw0rd!"


def human_ctx(email: str = HUMAN_EMAIL) -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {"id": "m-1", "user_id": HUMAN_ID, "role": "owner"},
        "user": {"user_id": HUMAN_ID, "email": email, "is_internal_admin": True, "user_role": "ceo"},
    }


def service_key_ctx() -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {
            "id": "service-monitor-key", "user_id": "system:service-api", "role": "owner",
        },
        "user": {
            "user_id": "system:service-api", "email": "system@aads.internal",
            "is_internal_admin": True, "user_role": "system",
        },
    }


def pipeline_ctx() -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {"id": "internal-pipeline-call", "user_id": "system:pipeline-runner"},
        "user": {
            "user_id": "system:pipeline-runner", "email": "system@aads.internal",
            "is_internal_admin": True, "user_role": "system",
        },
    }


def agent_session_ctx() -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {"user_id": "chat:5f0c2b6e"},
        "user": {"user_id": "chat:5f0c2b6e", "email": "", "is_internal_admin": True},
    }


def browser_agent_ctx() -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {"user_id": "browser-agent"},
        "user": {"user_id": "browser-agent", "email": "ceo@aads.dev", "is_internal_admin": True},
    }


def env_admin_ctx() -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {"user_id": "admin", "role": "owner"},
        "user": {"user_id": "admin", "email": HUMAN_EMAIL, "is_internal_admin": True, "user_role": "system"},
    }


def ceo_saas_admin_ctx() -> dict[str, Any]:
    return {
        "tenant": {"id": TENANT},
        "membership": {"id": "m-ceo", "user_id": HUMAN_ID, "role": "owner"},
        "user": {
            "user_id": HUMAN_ID, "email": HUMAN_EMAIL, "is_admin": True,
            "user_role": "system",
        },
    }


def _spec(risk: str = "READ") -> dict[str, Any]:
    return {
        "name": "orders",
        "domain": "shop.example.com",
        "version": 3,
        "inputs": [{"name": "pw", "secret": True}],
        "steps": [
            {"action": "navigate", "url": "https://shop.example.com/orders?token=abc123", "risk": "READ"},
            {"action": "fill", "selector": "#pw", "value": SECRET_VALUE, "risk": "READ",
             "description": f"type {SECRET_VALUE}"},
            {"action": "click", "selector": "button.submit", "risk": risk},
        ],
        "verify": [{"action": "snapshot", "selector": "body", "risk": "READ"}],
        "metadata": {"screen_e2e": {"screen_verified": True,
                                    "screenshot_url": "https://aads.example/shot/1.png?sig=zzzz"}},
    }


def _row(*, requested_by: str = "chat:abc", risk: str = "READ", status: str = "pending") -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()), "tenant_id": TENANT, "status": status,
        "name": "orders", "domain": "shop.example.com", "spec": _spec(risk),
        "dry_run": {"proposed_version": 3, "max_risk": risk},
        "requested_by": requested_by, "requested_at": "2026-10-14T00:00:00+00:00",
        "recipe_id": None,
    }


@pytest.fixture
def client(monkeypatch):
    holder: dict[str, Any] = {"ctx": human_ctx()}
    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = lambda: holder["ctx"]
    return TestClient(app), holder


@pytest.fixture
def decided(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_decide(registration_id, **kwargs):
        calls.append({"id": registration_id, **kwargs})
        return {"id": registration_id, "status": "approved" if kwargs["decision"] == "approve" else "rejected",
                "decided_by": kwargs["decided_by"]}

    monkeypatch.setattr(registration, "decide_registration", fake_decide)
    return calls


def _patch_get(monkeypatch, row):
    async def fake_get(registration_id, *, tenant_id):
        return row

    monkeypatch.setattr(registration, "get_registration", fake_get)


# ------------------------------------------------------------ 1. 사람만 결정


@pytest.mark.parametrize("ctx_factory", [service_key_ctx, pipeline_ctx, agent_session_ctx, browser_agent_ctx])
def test_non_human_principal_gets_403_human_decider_required(client, decided, monkeypatch, ctx_factory):
    http, holder = client
    holder["ctx"] = ctx_factory()
    _patch_get(monkeypatch, _row())

    for payload in ({"decision": "approve"}, {"decision": "reject"}):
        response = http.post("/ohvis/recipes/registrations/r-1/decision", json=payload)
        assert response.status_code == 403
        assert response.json()["detail"] == "human_decider_required"
    assert decided == []


def test_human_jwt_and_env_admin_are_accepted():
    assert registration.is_human_principal(human_ctx())
    assert registration.is_human_principal(env_admin_ctx())
    assert registration.is_human_principal(ceo_saas_admin_ctx())
    assert registration.decider_identity(human_ctx()) == HUMAN_EMAIL
    for ctx in (service_key_ctx(), pipeline_ctx(), agent_session_ctx(), browser_agent_ctx(), {}):
        assert not registration.is_human_principal(ctx)


@pytest.mark.parametrize("principal", [
    {"user": {"user_id": HUMAN_ID, "email": HUMAN_EMAIL, "user_role": "system"}},
    {"user": {"user_id": "system:pipeline-runner", "user_role": "system"}},
    {"user": {"user_id": "system:service-api", "user_role": "system"}},
    {"user": {"user_id": "service-monitor", "email": "monitor@aads.internal"}},
    {"user": {"user_id": HUMAN_ID, "is_admin": True, "user_role": "system"},
     "membership": {"id": "internal-pipeline-call"}},
])
def test_system_principals_remain_non_human(principal):
    assert not registration.is_human_principal(principal)


async def test_decide_as_human_refuses_service_key_before_touching_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("db must not be touched")

    monkeypatch.setattr(registration, "get_pool", boom)
    with pytest.raises(HumanDeciderRequired):
        await registration.decide_registration_as_human(
            "r-1", tenant_id=TENANT, decision="approve", principal=service_key_ctx(),
            confirm_high_risk=True,
        )


def test_decided_by_is_authenticated_user_not_request_body(client, decided, monkeypatch):
    http, _ = client
    _patch_get(monkeypatch, _row())

    response = http.post(
        "/ohvis/recipes/registrations/r-1/decision",
        json={"decision": "approve", "decided_by": "ceo:evil", "reason": "ok"},
    )

    assert response.status_code == 200
    assert decided[0]["decided_by"] == HUMAN_EMAIL


# ------------------------------------------------------------ 2. 자기승인


def test_self_approval_is_refused_but_self_reject_is_allowed(client, decided, monkeypatch):
    http, _ = client
    _patch_get(monkeypatch, _row(requested_by=HUMAN_EMAIL))

    refused = http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "approve"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "self_approval_blocked"
    assert decided == []

    rejected = http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "reject", "reason": "범위 밖 작업"})
    assert rejected.status_code == 200
    assert decided[0]["decision"] == "reject" and decided[0]["decided_by"] == HUMAN_EMAIL


def test_self_approval_matches_user_id_and_case_insensitively(client, decided, monkeypatch):
    http, _ = client
    _patch_get(monkeypatch, _row(requested_by=HUMAN_ID))
    assert http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "approve"}).status_code == 403
    _patch_get(monkeypatch, _row(requested_by=HUMAN_EMAIL.upper()))
    assert http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "approve"}).status_code == 403


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, row):
        self.row = row

    def transaction(self):
        return _Tx()

    async def fetchrow(self, query, *args):
        if "FOR UPDATE" in query:
            return self.row
        if "UPDATE work_recipe_registration_requests" in query:
            return {**self.row, "status": "rejected", "decided_by": args[2]}
        raise AssertionError(f"unexpected query: {query}")


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


async def test_core_decide_registration_blocks_self_approval_for_any_caller(monkeypatch):
    row = {**_row(requested_by="alice@example.com"), "id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT),
           "spec": json.dumps(_spec())}
    monkeypatch.setattr(registration, "get_pool", lambda: _Pool(row))

    with pytest.raises(SelfApprovalBlocked):
        await registration.decide_registration(
            row["id"], tenant_id=TENANT, decision="approve", decided_by="Alice@Example.com")
    rejected = await registration.decide_registration(
        row["id"], tenant_id=TENANT, decision="reject", decided_by="alice@example.com", reason="중복 요청")
    assert rejected["status"] == "rejected"


# ------------------------------------------------------------ 3. 고위험 재확인


@pytest.mark.parametrize("risk", ["WRITE_EXTERNAL", "IRREVERSIBLE"])
def test_high_risk_approve_needs_confirm_high_risk(client, decided, monkeypatch, risk):
    http, _ = client
    _patch_get(monkeypatch, _row(risk=risk))

    missing = http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "approve"})
    assert missing.status_code == 409
    assert missing.json()["detail"] == "high_risk_confirmation_required"
    assert decided == []

    falsy = http.post(
        "/ohvis/recipes/registrations/r-1/decision",
        json={"decision": "approve", "confirm_high_risk": False},
    )
    assert falsy.status_code == 409

    ok = http.post(
        "/ohvis/recipes/registrations/r-1/decision",
        json={"decision": "approve", "confirm_high_risk": True},
    )
    assert ok.status_code == 200 and len(decided) == 1

    reject = http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "reject", "reason": "범위 밖 작업"})
    assert reject.status_code == 200


def test_low_risk_approve_needs_no_confirmation(client, decided, monkeypatch):
    http, _ = client
    _patch_get(monkeypatch, _row(risk="WRITE_INTERNAL"))
    assert http.post("/ohvis/recipes/registrations/r-1/decision", json={"decision": "approve"}).status_code == 200


# ------------------------------------------------------------ 4. 채팅 승인 카드


def test_card_content_has_no_secret_values():
    summary, scope = approval_card.build_card(_row(risk="WRITE_EXTERNAL"))
    blob = summary + json.dumps(scope, ensure_ascii=False)

    assert SECRET_VALUE not in blob and "abc123" not in blob and "zzzz" not in blob
    assert "shop.example.com" in summary and "orders v3" in summary
    assert scope["step_count"] == 3 and scope["verify_count"] == 1
    assert scope["max_risk"] == "WRITE_EXTERNAL" and scope["high_risk_confirm_required"] is True
    assert scope["screen_evidence_url"].startswith("https://aads.example/shot/1.png")
    assert scope["registration_id"]


class _CardConn:
    def __init__(self, existing=None):
        self.existing = existing
        self.executed: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if query.lstrip().startswith("SELECT"):
            return self.existing
        return "card-1"

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _CardPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


async def test_propose_card_inserts_pending_approve_card_bound_to_chat_session(monkeypatch):
    conn = _CardConn()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _CardPool(conn))
    row = _row(risk="WRITE_EXTERNAL")

    result = await approval_card.propose_card(row, chat_session_id="5f0c2b6e-0000-4000-8000-000000000000")

    assert result == {"status": "proposed", "request_id": "card-1"}
    query, args = conn.executed[-1]
    assert "INSERT INTO agent_permission_requests" in query and "'pending'" in query and "'approve'" in query
    assert approval_card.ACTION_TYPE in args and approval_card.GATE_SOURCE in args
    assert "5f0c2b6e-0000-4000-8000-000000000000" in args
    assert "high" in args
    assert SECRET_VALUE not in json.dumps([str(a) for a in args])


async def test_propose_card_refreshes_existing_pending_card(monkeypatch):
    conn = _CardConn(existing="card-0")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _CardPool(conn))

    result = await approval_card.propose_card(_row(), chat_session_id="s")

    assert result == {"status": "refreshed", "request_id": "card-0"}
    assert not any("INSERT INTO" in q for q, _ in conn.executed)


async def test_propose_card_skips_non_pending_and_never_raises(monkeypatch):
    assert (await approval_card.propose_card(_row(status="approved"), chat_session_id="s"))["status"] == "not_pending"

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.db_pool.get_pool", boom)
    assert (await approval_card.propose_card(_row(), chat_session_id="s"))["status"] == "error"


class _DecidePool:
    def __init__(self, card):
        self.card = card
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        assert "action_type = $2" in query
        return self.card

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def fetchval(self, *a, **k):
        return 0

    def acquire(self):
        raise AssertionError("not used when session id is not a chat uuid")


def _card_row() -> dict[str, Any]:
    return {
        "id": "11111111-2222-4333-8444-555555555555", "tenant_id": TENANT,
        "requested_by": "not-a-chat-uuid", "action_summary": "레시피 등록 승인",
        "scope_json": json.dumps({"registration_id": "reg-1", "name": "orders",
                                  "domain": "shop.example.com", "version": 3}),
    }


async def test_card_decide_route_rejects_service_key_and_agent(monkeypatch):
    from app.api import project_docs

    pool = _DecidePool(_card_row())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)

    async def fake_get(registration_id, *, tenant_id):
        return _row()

    monkeypatch.setattr(registration, "get_registration", fake_get)
    for ctx in (service_key_ctx(), agent_session_ctx(), pipeline_ctx()):
        with pytest.raises(HTTPException) as exc:
            await project_docs.approvals_decide(
                request_id=_card_row()["id"], decision="approved", reason="", scope="single",
                hours=2, max_executions=1, confirm_high_risk=True, context=ctx,
            )
        assert exc.value.status_code == 403 and exc.value.detail == "human_decider_required"
    assert pool.executed == []


async def test_card_decide_route_records_jwt_user_and_closes_card(monkeypatch):
    from app.api import project_docs

    pool = _DecidePool(_card_row())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    seen: dict[str, Any] = {}

    async def fake_as_human(registration_id, **kwargs):
        seen.update(kwargs, registration_id=registration_id)
        return {"id": registration_id, "status": "approved", "decided_by": HUMAN_EMAIL,
                "recipe": {"version": 3}}

    monkeypatch.setattr(registration, "decide_registration_as_human", fake_as_human)

    result = await project_docs.approvals_decide(
        request_id=_card_row()["id"], decision="approved", reason="", scope="single",
        hours=2, max_executions=1, confirm_high_risk=True, context=human_ctx(),
    )

    assert seen["registration_id"] == "reg-1" and seen["decision"] == "approve"
    assert seen["principal"]["user"]["email"] == HUMAN_EMAIL and seen["confirm_high_risk"] is True
    assert result["decision"] == "approved" and result["registration"]["decided_by"] == HUMAN_EMAIL
    query, args = pool.executed[-1]
    assert "UPDATE agent_permission_requests" in query and HUMAN_EMAIL in args and "approved" in args


async def test_card_decide_route_maps_high_risk_and_self_approval_errors(monkeypatch):
    from app.api import project_docs

    pool = _DecidePool(_card_row())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)

    async def raising(registration_id, **kwargs):
        raise HighRiskConfirmationRequired("high_risk_confirmation_required")

    monkeypatch.setattr(registration, "decide_registration_as_human", raising)
    with pytest.raises(HTTPException) as exc:
        await project_docs.approvals_decide(
            request_id=_card_row()["id"], decision="approved", reason="", scope="single",
            hours=2, max_executions=1, confirm_high_risk=False, context=human_ctx(),
        )
    assert exc.value.status_code == 409 and exc.value.detail == "high_risk_confirmation_required"
    assert pool.executed == []


# ------------------------------------------------------------ 5. 목록 필드


def test_registration_summary_fields_are_secret_free():
    info = registration.registration_summary(_row(risk="WRITE_EXTERNAL"))

    assert info["domain"] == "shop.example.com" and info["name"] == "orders"
    assert info["version"] == 3 and info["max_risk"] == "WRITE_EXTERNAL"
    assert info["step_count"] == 3 and info["verify_count"] == 1
    assert info["requested_by"] == "chat:abc" and info["requested_at"]
    assert "sig=zzzz" not in info["screen_evidence_url"] and info["screen_evidence_url"].startswith("https://aads.example/")
    first = info["step_summaries"][0]
    assert first["action"] == "navigate" and first["url_host"] == "shop.example.com"
    assert info["step_summaries"][-1]["phase"] == "verify"
    blob = json.dumps(info, default=str)
    assert SECRET_VALUE not in blob and "abc123" not in blob
    assert all(set(s) == {"seq", "phase", "action", "risk", "url_host", "selector"} for s in info["step_summaries"])


def test_list_endpoint_adds_fields_and_keeps_existing_ones(client, monkeypatch):
    http, _ = client
    row = _row()
    row["name"] = "keep-me"

    async def fake_list(**kwargs):
        return [row]

    monkeypatch.setattr(ohvis_recipes, "list_registrations", fake_list)

    body = http.get("/ohvis/recipes/registrations?status=pending").json()["registrations"][0]

    for key in ("id", "tenant_id", "name", "domain", "spec", "dry_run", "status", "requested_by", "requested_at"):
        assert body[key] == row[key]
    for key in ("version", "max_risk", "step_count", "verify_count", "screen_evidence_url", "step_summaries"):
        assert key in body
    assert body["step_count"] == 3


def test_registration_summary_tolerates_minimal_rows():
    info = registration.registration_summary({"name": "x", "domain": "d", "spec": {"steps": [{"action": "navigate"}]}})
    assert info["step_count"] == 1 and info["verify_count"] == 0 and info["max_risk"] == "READ"
