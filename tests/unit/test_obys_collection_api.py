"""수집 API 계약: 라우트 목록, 권한 게이트, 오류 매핑. 서비스 계층은 대체한다."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import obys_collections as api
from app.auth import get_current_user
from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_service as svc

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"


def _user(role="owner", *, admin=False, status="active", membership_tenant=TENANT):
    return {"tenant_id": TENANT, "email": "u@example.com", "is_internal_admin": admin,
            "current_membership": {"tenant_id": membership_tenant, "role": role, "status": status}}


def _client(user):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_route_inventory_is_the_ui_contract():
    routes = {(m, r.path) for r in api.router.routes for m in r.methods}
    assert routes == {
        ("GET", "/obys-collections/status"),
        ("POST", "/obys-collections/admin/discover"),
        ("POST", "/obys-collections/admin/companies/{company_id}/permission-check"),
        ("POST", "/obys-collections/admin/companies/{company_id}/link"),
        ("POST", "/obys-collections/admin/companies/{company_id}/block"),
        ("POST", "/obys-collections/companies/{company_id}/runs"),
        ("GET", "/obys-collections/companies/{company_id}/runs"),
        ("GET", "/obys-collections/businesses/{business_id}/items"),
        ("GET", "/obys-collections/businesses/{business_id}/ledger-entries"),
        ("GET", "/obys-collections/businesses/{business_id}/channel-revenue"),
        ("GET", "/obys-collections/businesses/{business_id}/delivery-overview"),
        ("GET", "/obys-collections/today-overview"),
        ("POST", "/obys-collections/businesses/{business_id}/items/review"),
        ("POST", "/obys-collections/businesses/{business_id}/items/confirm"),
        ("POST", "/obys-collections/businesses/{business_id}/items/bulk-confirm"),
    }


def test_main_mounts_router_and_reauth_endpoint_exists():
    from app.api import clobe_integration
    assert any(r.path.endswith("/reauth") and "POST" in r.methods for r in clobe_integration.router.routes)
    src = open("app/main.py", encoding="utf-8").read()
    assert 'obys_collections_router, prefix="/api/v1"' in src
    # 오비서(fb.newtalk.kr)는 main.py 가 아니라 yeoljeong_main 프로세스가 서빙한다 — 거기에도 같은 계약이 올라와야 한다.
    ob = open("app/yeoljeong_main.py", encoding="utf-8").read()
    assert 'obys_collections.router, prefix="/api/v1"' in ob


def test_obys_app_serves_collection_routes():
    from app import yeoljeong_main
    paths = {r.path for r in yeoljeong_main.app.routes}
    assert "/api/v1/obys-collections/status" in paths
    assert "/api/v1/obys-collections/companies/{company_id}/runs" in paths


@pytest.mark.parametrize("method,path", [
    ("post", "/api/v1/obys-collections/admin/discover"),
    ("post", "/api/v1/obys-collections/admin/companies/c1/link"),
    ("post", "/api/v1/obys-collections/admin/companies/c1/block"),
    ("post", "/api/v1/obys-collections/admin/companies/c1/permission-check"),
])
def test_admin_endpoints_reject_tenant_owner(method, path):
    resp = getattr(_client(_user("owner")), method)(path, json={"business_id": "biz-x"})
    assert resp.status_code == 403


def test_status_scope_all_is_admin_only_and_hides_token_details_from_tenants(monkeypatch):
    async def status(tenant, internal_admin=False):
        return {"companies": [], "unsupported_sources": []}

    async def get_status():
        return {"status": "connected", "reauth_required": False, "last_success_at": None,
                "token_expires_at": "2026-10-04T00:00:00+00:00"}

    monkeypatch.setattr(svc, "company_status", status)
    monkeypatch.setattr(clobe, "get_status", get_status)
    assert _client(_user("owner")).get("/api/v1/obys-collections/status?scope=all").status_code == 403
    tenant = _client(_user("viewer")).get("/api/v1/obys-collections/status").json()
    assert "token_expires_at" not in tenant["auth"] and "reauth_endpoint" not in tenant["auth"]
    admin = _client(_user("owner", admin=True)).get("/api/v1/obys-collections/status?scope=all").json()
    assert admin["auth"]["token_expires_at"] and admin["auth"]["reauth_endpoint"] == "/api/v1/integrations/clobe/reauth"


@pytest.mark.parametrize("user,expected", [
    (_user("viewer"), 403), (_user("member"), 403), (_user("owner", status="invited"), 403),
    (_user("owner", membership_tenant="other"), 403),
])
def test_run_requires_active_owner_or_admin_of_same_tenant(user, expected):
    resp = _client(user).post("/api/v1/obys-collections/companies/c1/runs", json={})
    assert resp.status_code == expected


def test_run_other_tenants_company_is_404_and_nothing_runs(monkeypatch):
    async def deny(company_id, tenant_id):
        raise svc.CollectionError("company_not_found", 404)

    async def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(svc, "require_company_tenant", deny)
    monkeypatch.setattr(svc, "run_collection", boom)
    resp = _client(_user("owner")).post("/api/v1/obys-collections/companies/c1/runs", json={})
    assert resp.status_code == 404 and resp.json()["detail"] == "company_not_found"


def test_run_rejects_unknown_kind_and_write_like_values():
    c = _client(_user("owner", admin=True))
    assert c.post("/api/v1/obys-collections/companies/c1/runs", json={"kinds": ["journal_post"]}).status_code == 422
    assert c.post("/api/v1/obys-collections/companies/c1/runs", json={"mode": "write"}).status_code == 422


@pytest.mark.parametrize("exc,code,detail", [
    (clobe.ClobeReauthRequired("x"), 409, "reauth_required"),
    (clobe.ClobeTransientError("http_503"), 503, "clobe_temporarily_unavailable"),
    (clobe.ClobeToolDenied("tool_denied:x"), 403, "tool_denied"),
    (clobe.ClobeError("boom"), 502, "clobe_error"),
    (svc.CollectionError("collection_in_progress", 409), 409, "collection_in_progress"),
])
def test_run_error_mapping_never_leaks_internal_text(monkeypatch, exc, code, detail):
    async def fail(*a, **k):
        raise exc

    async def runnable(company_id):
        return None

    monkeypatch.setattr(svc, "require_runnable_company", runnable)
    monkeypatch.setattr(svc, "run_collection", fail)
    resp = _client(_user("owner", admin=True)).post("/api/v1/obys-collections/companies/c1/runs", json={})
    assert resp.status_code == code and resp.json()["detail"] == detail


def test_each_run_request_passes_its_own_unique_instance_name(monkeypatch):
    seen = []

    async def runnable(company_id):
        return None

    async def capture(company_id, **kw):
        seen.append(kw["instance"])
        return {"status": "succeeded"}

    monkeypatch.setattr(svc, "require_runnable_company", runnable)
    monkeypatch.setattr(svc, "run_collection", capture)
    c = _client(_user("owner", admin=True))
    assert c.post("/api/v1/obys-collections/companies/c1/runs", json={}).status_code == 200
    assert c.post("/api/v1/obys-collections/companies/c1/runs", json={}).status_code == 200
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(i.startswith(f"{svc.DEFAULT_INSTANCE}:manual:") for i in seen)


def test_review_and_confirm_role_split(monkeypatch):
    async def ok_review(*a, **k):
        return {"updated": 1, "skipped": 0, "stage": "reviewed"}

    async def ok_confirm(*a, **k):
        return {"confirmed": 1, "skipped": 0}

    monkeypatch.setattr(svc, "review_items", ok_review)
    monkeypatch.setattr(svc, "confirm_items", ok_confirm)
    body = {"item_ids": ["3f2b7c0e-1111-4222-8333-444455556666"]}
    member = _client(_user("member"))
    assert member.post("/api/v1/obys-collections/businesses/b1/items/review", json={**body, "decision": "approve"}).status_code == 200
    assert member.post("/api/v1/obys-collections/businesses/b1/items/confirm", json=body).status_code == 403
    assert _client(_user("viewer")).post("/api/v1/obys-collections/businesses/b1/items/review",
                                          json={**body, "decision": "approve"}).status_code == 403
    assert _client(_user("owner")).post("/api/v1/obys-collections/businesses/b1/items/confirm", json=body).status_code == 200


@pytest.mark.parametrize("body,detail", [
    ({"period_start": "2026-10-01", "period_end": "2026-09-01"}, "invalid_period"),
    ({"period_start": "2020-01-01", "period_end": "2026-09-01"}, "period_too_long"),
    ({"period_start": "not-a-date"}, "invalid_period"),
])
def test_run_with_bad_period_is_400_with_its_own_code_not_500(monkeypatch, body, detail):
    async def runnable(company_id):
        return None

    monkeypatch.setattr(svc, "require_runnable_company", runnable)
    resp = _client(_user("owner", admin=True)).post("/api/v1/obys-collections/companies/c1/runs", json=body)
    assert resp.status_code == 400 and resp.json()["detail"] == detail


@pytest.mark.parametrize("action,extra", [("review", {"decision": "approve"}), ("confirm", {})])
def test_malformed_item_id_is_400_invalid_item_id(action, extra):
    resp = _client(_user("owner")).post(
        f"/api/v1/obys-collections/businesses/b1/items/{action}", json={"item_ids": ["not-a-uuid"], **extra})
    assert resp.status_code == 400 and resp.json()["detail"] == "invalid_item_id"


@pytest.mark.parametrize("action,extra", [("review", {"decision": "approve"}), ("confirm", {})])
def test_unrelated_value_error_inside_service_is_not_reported_as_invalid_item_id(monkeypatch, action, extra):
    async def boom(*a, **k):
        raise ValueError("invalid_period")

    monkeypatch.setattr(svc, "review_items", boom)
    monkeypatch.setattr(svc, "confirm_items", boom)
    valid = "3f2b7c0e-1111-4222-8333-444455556666"
    resp = _client(_user("owner")).post(
        f"/api/v1/obys-collections/businesses/b1/items/{action}", json={"item_ids": [valid], **extra})
    assert resp.status_code == 500


# ── 내부 관리자도 미연결·차단 회사는 수집을 시작하지 못한다 ──────

@pytest.mark.parametrize("code,status", [("company_not_discovered", 404), ("company_not_linked", 409),
                                          ("company_blocked", 403), ("tenant_not_in_collection_scope", 403)])
def test_admin_run_is_refused_before_collection_for_unrunnable_company(monkeypatch, code, status):
    async def refuse(company_id):
        raise svc.CollectionError(code, status)

    async def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(svc, "require_runnable_company", refuse)
    monkeypatch.setattr(svc, "run_collection", boom)
    resp = _client(_user("owner", admin=True)).post("/api/v1/obys-collections/companies/c1/runs", json={})
    assert resp.status_code == status and resp.json()["detail"] == code


@pytest.mark.parametrize("link,code", [
    ({"link_status": "review", "tenant_id": None, "business_id": None}, "company_not_linked"),
    ({"link_status": "blocked", "tenant_id": None, "business_id": None}, "company_not_linked"),
])
@pytest.mark.asyncio
async def test_require_runnable_company_rejects_unlinked_and_blocked(monkeypatch, link, code):
    class Conn:
        async def fetchrow(self, *a):
            return link

        async def close(self):
            return None

    async def connect():
        return Conn()

    monkeypatch.setattr(svc, "_connect", connect)
    with pytest.raises(svc.CollectionError) as e:
        await svc.require_runnable_company("c1")
    assert e.value.code == code


# ── /reauth 와 get_status().reauth_required ─────────────────

def _admin_client():
    from app.api import clobe_integration
    from app.auth import require_internal_admin
    app = FastAPI()
    app.include_router(clobe_integration.router, prefix="/api/v1")
    app.dependency_overrides[require_internal_admin] = lambda: {"user_id": "admin-1"}
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("status,allowed", [
    ("connected", False), ("not_connected", True), ("reauth_required", True), ("revoked", True)])
def test_reauth_route_only_issues_authorize_url_when_connection_needs_consent(monkeypatch, status, allowed):
    async def get_status():
        return {"status": status, "reauth_required": status != "connected"}

    started = []

    async def start(created_by=None):
        started.append(created_by)
        return {"authorize_url": "https://example.invalid/authorize", "expires_at": "x"}

    monkeypatch.setattr(clobe, "get_status", get_status)
    monkeypatch.setattr(clobe, "start_authorization", start)
    resp = _admin_client().post("/api/v1/integrations/clobe/reauth")
    if allowed:
        assert resp.status_code == 200 and started == ["admin-1"]
    else:
        assert resp.status_code == 409 and resp.json()["detail"] == "reauth_not_needed" and started == []


@pytest.mark.parametrize("status,expected", [
    ("connected", False), ("not_connected", True), ("reauth_required", True), ("revoked", True)])
@pytest.mark.asyncio
async def test_get_status_reauth_required_flag_and_existing_fields_kept(monkeypatch, status, expected):
    class Pool:
        async def fetchval(self, sql, *a):
            return 0

    async def load():
        return None if status == "not_connected" else {"status": status, "last_success_at": None,
                                                        "token_expires_at": None, "last_error": None}

    monkeypatch.setattr(clobe, "_load_connection", load)
    monkeypatch.setattr(clobe, "get_pool", lambda: Pool())
    out = await clobe.get_status()
    assert out["reauth_required"] is expected and out["status"] == status
    assert {"last_success_at", "allowed_tool_count", "observed_tool_count", "last_error"} <= set(out)


def test_discover_summary_counter_tolerates_unknown_status():
    summary = {"linked": 1}
    svc._bump(summary, "linked")
    svc._bump(summary, "something_new")
    assert summary == {"linked": 2, "something_new": 1}


class _StatusConn:
    def __init__(self, states, permission_ok=None, runs=None):
        self.states = states
        self.permission_ok = permission_ok
        self.runs = runs or []

    async def fetch(self, sql, *args):
        if "obys_clobe_company_link" in sql:
            return [{"clobe_company_id": "c1", "company_name": "단하루", "reg_no": "1234567890", "link_status": "linked",
                     "link_basis": "reg_no", "tenant_id": TENANT, "business_id": "biz-1",
                     "permission_ok": self.permission_ok}]
        if "obys_clobe_collection_state" in sql:
            return self.states
        if "obys_clobe_collection_run" in sql:
            return self.runs
        return []

    async def close(self):
        pass


def _state(kind, status, code, fails, run_id=None):
    from datetime import datetime, timezone

    return {"data_kind": kind, "status": status, "last_success_at": None,
            "last_failure_at": datetime(2026, 11, 4, tzinfo=timezone.utc), "last_error_code": code,
            "consecutive_failures": fails, "next_retry_at": None, "covered_from": None, "covered_to": None,
            "source_as_of": None, "last_run_id": run_id}


def _patch_status(monkeypatch, conn, reauth=False):
    async def connect():
        return conn

    async def reauth_required():
        return reauth

    monkeypatch.setattr(svc, "_connect", connect)
    monkeypatch.setattr(svc, "_clobe_reauth_required", reauth_required)


@pytest.mark.asyncio
async def test_company_status_keeps_existing_fields_and_adds_next_action(monkeypatch):
    states = [_state("bank_transaction", "needs_reauth", "reauth_required", 1),
              _state("tax_invoice", "failed", "clobe:502", 3),
              _state("cash_receipt", "partial", "transient:timeout", 1),
              _state("card_approval", "failed", "clobe:permission_denied", 1)]
    _patch_status(monkeypatch, _StatusConn(states))
    out = await svc.company_status(TENANT)
    kinds = out["companies"][0]["kinds"]
    legacy = {"status", "last_success_at", "last_failure_at", "last_error_code", "consecutive_failures",
              "next_retry_at", "covered_from", "covered_to", "source_as_of"}
    assert all(legacy <= set(v) and "next_action" in v for v in kinds.values())
    assert kinds["bank_transaction"]["status"] == "needs_reauth" and kinds["bank_transaction"]["last_error_code"] == "reauth_required"
    assert kinds["bank_transaction"]["next_action"]["key"] == "reconnect_clobe"
    assert kinds["tax_invoice"]["next_action"]["key"] == "contact_admin"
    assert kinds["cash_receipt"]["next_action"]["key"] == "wait_auto_retry"
    assert kinds["card_approval"]["next_action"]["key"] == "request_permission"
    assert kinds["channel_revenue"]["status"] == "never_run" and kinds["channel_revenue"]["next_action"]["key"] == "start_collection"
    assert set(out) == {"companies", "unsupported_sources"}


@pytest.mark.asyncio
async def test_company_status_shows_reconnect_when_session_open_fails_before_any_state_is_written(monkeypatch):
    succeeded = _state("bank_transaction", "succeeded", None, 0)
    _patch_status(monkeypatch, _StatusConn([succeeded]), reauth=True)
    kinds = (await svc.company_status(TENANT))["companies"][0]["kinds"]
    assert all(v["next_action"]["key"] == "reconnect_clobe" for v in kinds.values())
    assert kinds["bank_transaction"]["status"] == "succeeded" and kinds["card_approval"]["status"] == "never_run"


@pytest.mark.asyncio
async def test_company_status_shows_permission_request_when_preflight_denied(monkeypatch):
    _patch_status(monkeypatch, _StatusConn([_state("bank_transaction", "succeeded", None, 0)], permission_ok=False))
    kinds = (await svc.company_status(TENANT))["companies"][0]["kinds"]
    assert all(v["next_action"]["key"] == "request_permission" for v in kinds.values())
    assert kinds["bank_transaction"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_company_status_reads_scrape_failure_categories_for_captcha_and_file_fallback(monkeypatch):
    runs = [{"run_id": "r-captcha", "counts": '{"scrape": {"failure_categories": ["CAPTCHA"]}}'},
            {"run_id": "r-auth", "counts": {"scrape": {"failure_categories": ["AUTH"]}}},
            {"run_id": "r-bad", "counts": "not json"}]
    states = [_state("bank_transaction", "partial", "source_scrape_error", 1, "r-captcha"),
              _state("channel_revenue", "partial", "source_scrape_error", 2, "r-auth"),
              _state("tax_invoice", "partial", "source_scrape_error", 1, "r-bad"),
              _state("card_approval", "partial", "source_scrape_error", 2, "r-auth")]
    _patch_status(monkeypatch, _StatusConn(states, runs=runs))
    kinds = (await svc.company_status(TENANT))["companies"][0]["kinds"]
    assert kinds["bank_transaction"]["next_action"]["key"] == "solve_captcha"
    assert kinds["channel_revenue"]["next_action"]["key"] == "upload_file"
    assert kinds["channel_revenue"]["next_action"]["buttons"] == ["upload_file", "retry"]
    assert kinds["channel_revenue"]["next_action"]["view"] == "sales"
    assert kinds["card_approval"]["next_action"]["key"] == "wait_auto_retry"  # 카드는 CSV 업로드 화면이 없다
    assert kinds["tax_invoice"]["next_action"]["key"] == "wait_auto_retry"


@pytest.mark.asyncio
async def test_clobe_reauth_check_failure_does_not_break_status(monkeypatch):
    async def boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(svc.clobe, "get_status", boom)
    assert await svc._clobe_reauth_required() is False

    async def reauth():
        return {"reauth_required": True}

    monkeypatch.setattr(svc.clobe, "get_status", reauth)
    assert await svc._clobe_reauth_required() is True


@pytest.mark.asyncio
async def test_company_status_channel_not_connected_is_not_reconnect(monkeypatch):
    states = [_state("channel_revenue", "not_connected", "channel_not_connected", 0)]
    _patch_status(monkeypatch, _StatusConn(states))
    kinds = (await svc.company_status(TENANT))["companies"][0]["kinds"]
    assert kinds["channel_revenue"]["next_action"]["key"] == "connect_channel"
    assert kinds["channel_revenue"]["connect_required"] is True
