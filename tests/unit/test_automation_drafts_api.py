"""업무 자동화 대화형 등록 API·해석기·계정 연결·예약·통합 목록 (T2)."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import automation_drafts as api
from app.api import automations as automations_api
from app.auth import get_current_tenant_context
from app.services import automation_catalog as catalog
from app.services import automation_credential_linker as linker
from app.services import automation_draft_flow as flow
from app.services import automation_draft_service as svc
from app.services import automation_intent_parser as parser
from app.services import automation_schedule as schedule

TENANT_A = "2d701a8c-9596-4757-8588-faa4f7837112"
TENANT_B = "7a1c3f0e-1111-4222-8333-944455556666"
ALICE = "alice@example.com"
BOB = "bob@example.com"
FORBIDDEN_TERMS = ("work_key", "lane", "credential_scope", "session_id", "recipe", "registration", "e2e")
CRED_ID = str(uuid.uuid4())
SK_LIKE = "sk" + "-" + "ant-oat01-abcdefghijklmnop"


def ctx(user: str = ALICE, role: str = "member", tenant: str = TENANT_A) -> dict[str, Any]:
    return {
        "user": {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, user)), "email": user},
        "tenant": {"id": tenant},
        "membership": {"role": role, "user_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, user))},
    }


class Store:
    """automation_drafts 서비스 함수의 메모리 대역. 전이표·카드·비밀값 검사는 실제 코드를 그대로 쓴다."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def _get(self, draft_id: str, tenant_id: str) -> dict[str, Any]:
        row = self.rows.get(draft_id)
        if row is None or row["tenant_id"] != tenant_id:
            raise svc.DraftNotFound(draft_id)
        return row

    async def create_draft(self, *, tenant_id, owner_user, title, intent_text, intent=None, session_id=None,
                           aliases=None, lane="server", actor=None):
        svc.assert_no_secret({"t": title, "i": intent_text, "n": dict(intent or {})}, "draft")
        now = datetime.now(timezone.utc)
        row = {
            "id": str(uuid.uuid4()), "tenant_id": tenant_id, "owner_user": owner_user, "session_id": session_id,
            "title": title, "intent_text": intent_text, "intent": json.dumps(dict(intent or {})),  # 운영은 문자열로 온다
            "aliases": aliases or [], "state": "intent_draft", "lane": lane, "failure_code": None,
            "credential_id": None, "credential_request_id": None, "recording_ref": {}, "evidence": {},
            "schedule_job_id": None, "recipe_names": [], "registration_ids": [],
            "created_at": now, "updated_at": now,
        }
        self.rows[row["id"]] = row
        self.events.append({"draft_id": row["id"], "type": "draft_created"})
        return dict(row)

    async def get_draft(self, draft_id, *, tenant_id):
        row = self.rows.get(draft_id)
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    async def transition(self, draft_id, from_state, to_state, actor, reason, *, tenant_id,
                         failure_code=None, detail=None):
        if to_state not in svc.TRANSITIONS.get(from_state, ()):
            raise svc.InvalidTransition(f"{from_state}->{to_state}")
        svc.assert_no_secret(dict(detail or {}), "detail")
        row = self._get(draft_id, tenant_id)
        if row["state"] != from_state:
            raise svc.TransitionConflict(from_state, row["state"])
        row.update(state=to_state, failure_code=failure_code, updated_at=datetime.now(timezone.utc))
        self.events.append({"draft_id": draft_id, "type": reason, "to": to_state, "actor": actor})
        return dict(row)

    async def update_fields(self, draft_id, *, tenant_id, expected_state, fields, actor, event_type, detail=None):
        svc.assert_no_secret(dict(fields), "fields")
        row = self._get(draft_id, tenant_id)
        if expected_state is not None and row["state"] != expected_state:
            raise svc.TransitionConflict(expected_state, row["state"])
        for key, value in fields.items():
            row[key] = json.dumps(value) if key in ("intent", "recording_ref", "evidence") else value
        row["updated_at"] = datetime.now(timezone.utc)
        self.events.append({"draft_id": draft_id, "type": event_type, "actor": actor})
        return dict(row)

    async def list_drafts(self, *, tenant_id, owner_user=None, limit=200):
        return [dict(r) for r in self.rows.values()
                if r["tenant_id"] == tenant_id and (owner_user is None or r["owner_user"] == owner_user)]

    def set_state(self, draft_id: str, state: str, **extra: Any) -> None:
        self.rows[draft_id].update(state=state, **extra)


class World:
    def __init__(self) -> None:
        self.store = Store()
        self.llm_calls = 0
        self.requests: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.credentials: list[dict[str, Any]] = []
        self.request_status = {"status": "pending", "credential_id": None}
        self.secure_input_ok = True
        self.launched: list[str] = []


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv(svc.DRAFTS_FLAG, "true")
    w = World()
    for name in ("create_draft", "get_draft", "transition", "update_fields", "list_drafts"):
        monkeypatch.setattr(svc, name, getattr(w.store, name))

    async def fake_llm(text, tenant_id):
        w.llm_calls += 1
        return {"task": "어제 매출을 표로 정리", "output_kind": "table"}

    async def fake_recipes(**kwargs):
        return [{"id": str(uuid.uuid4()), "name": "wing_sales", "domain": "wing.coupang.com"}]

    async def fake_session(actor, session_id):
        return str(uuid.UUID(session_id))

    async def fake_find(*, tenant_id, domain):
        return [c for c in w.credentials if linker.domain_matches(c["host"], domain)]

    async def fake_cred(*, tenant_id, credential_id, domain):
        for c in w.credentials:
            if c["id"] == credential_id and linker.domain_matches(c["host"], domain):
                return c
        return None

    async def fake_request(*, tenant_id, session_id, url, draft_id):
        if not w.secure_input_ok:
            raise svc_error()
        w.requests.append({"tenant_id": tenant_id, "session_id": session_id, "url": url, "draft_id": draft_id})
        return {"request_id": "req-1", "requestId": "req-1", "host": "store.coupangeats.com",
                "reused": False, "expires_in_minutes": 30}

    async def fake_progress(*, tenant_id, request_id):
        return dict(w.request_status)

    async def fake_register(*, draft_id, tenant_id, owner_user, directive, schedule, active):
        job_id = schedule_module.job_id_of(draft_id)
        w.jobs[job_id] = {"active": active, "schedule": schedule, "owner_user": owner_user, "tenant_id": tenant_id}
        return {"job_id": job_id, "deferred": False, "persisted": True}

    schedule_module = schedule
    monkeypatch.setattr(parser, "_ask_llm", fake_llm)
    monkeypatch.setattr(flow, "list_recipes", fake_recipes)
    monkeypatch.setattr(flow, "_check_session", fake_session)
    monkeypatch.setattr(linker, "find_credentials", fake_find)
    monkeypatch.setattr(linker, "get_credential_for_domain", fake_cred)
    monkeypatch.setattr(linker, "request_new_input", fake_request)
    monkeypatch.setattr(linker, "request_progress", fake_progress)
    monkeypatch.setattr(schedule, "register", fake_register)
    # T3: 시연 실행기는 별도 테스트(test_automation_demo_runner)가 본다. 여기서는 띄운 기록만 남긴다.
    monkeypatch.setattr(flow, "_launch_demo", lambda draft_id, tenant_id: w.launched.append(draft_id))
    monkeypatch.setattr(schedule, "remove", lambda job_id: w.jobs.pop(job_id, None))
    monkeypatch.setattr(schedule, "pause", lambda job_id: w.jobs[job_id].update(active=False))
    monkeypatch.setattr(schedule, "resume", lambda job_id: w.jobs[job_id].update(active=True))
    monkeypatch.setattr(schedule, "next_run_at", lambda job_id: "2026-10-15T09:00:00+09:00" if job_id in w.jobs else None)
    return w


def svc_error():
    from app.services.vault_secure_input import SecureInputError

    return SecureInputError(404, "secure_input_disabled")


class Caller:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=True)

    def as_(self, **kw: Any) -> TestClient:
        context = ctx(**kw)
        self.app.dependency_overrides[get_current_tenant_context] = lambda: context
        return self.client


@pytest.fixture
def call(world):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.include_router(automations_api.router, prefix="/api/v1")
    return Caller(app)


def make(call, text="매일 아침 쿠팡이츠 어제 매출 표로 정리해줘", **kw):
    response = call.as_(**kw).post("/api/v1/automation-drafts", json={"intent_text": text})
    assert response.status_code == 201, response.text
    return response.json()


def to_state(call, world, state, **kw):
    """초안을 만들어 곧바로 state 로 둔다(그 앞 단계는 별도 테스트가 본다)."""
    body = make(call, **kw)
    world.store.set_state(body["draft"]["id"], state)
    return body["draft"]["id"]


def assert_clean(body: Any) -> None:
    dumped = json.dumps(body, ensure_ascii=False)
    assert svc.find_secret(body) is None
    assert SK_LIKE not in dumped
    for term in FORBIDDEN_TERMS:
        assert term not in json.dumps(body.get("card", {}), ensure_ascii=False).lower()
    for key in ("lane", "registration_ids", "recipe_names", "schedule_job_id", "session_id", "owner_user"):
        assert key not in body["draft"]


# ── 1. 시작·조회·고치기 ────────────────────────────────────────────────────

def test_start_returns_draft_and_card_with_one_llm_call(call, world):
    body = make(call)
    assert world.llm_calls == 1
    draft, card = body["draft"], body["card"]
    assert draft["state"] == "intent_draft"
    assert draft["intent"]["domain"] == "store.coupangeats.com"
    assert draft["intent"]["schedule"] == {"kind": "daily", "hour": 9, "minute": 0}
    assert draft["intent"]["output_kind"] == "table"
    assert {f["id"] for f in card["fields"]} == {"site", "task", "output", "when"}
    assert world.store.rows[draft["id"]]["owner_user"] == ALICE
    assert_clean(body)


def test_owner_is_person_not_chat_session(call, world):
    session = str(uuid.uuid4())
    response = call.as_().post("/api/v1/automation-drafts", json={"intent_text": "쿠팡이츠 매출 확인", "session_id": session})
    assert response.status_code == 201
    row = next(iter(world.store.rows.values()))
    assert row["owner_user"] == ALICE and row["owner_user"] != session
    assert row["session_id"] == session


def test_flag_off_returns_404_for_every_route(call, world, monkeypatch):
    monkeypatch.setenv(svc.DRAFTS_FLAG, "false")
    client = call.as_()
    did = str(uuid.uuid4())
    calls = [
        ("post", "/api/v1/automation-drafts", {"intent_text": "쿠팡이츠 매출"}),
        ("get", f"/api/v1/automation-drafts/{did}", None),
        ("patch", f"/api/v1/automation-drafts/{did}", {"task": "x"}),
        ("post", f"/api/v1/automation-drafts/{did}/confirm-intent", {"expected_state": "intent_draft"}),
        ("post", f"/api/v1/automation-drafts/{did}/credential", {"request_new": True}),
        ("post", f"/api/v1/automation-drafts/{did}/demo", {"expected_state": "credential_ready"}),
        ("post", f"/api/v1/automation-drafts/{did}/risk-decision", {"decision": "proceed"}),
        ("post", f"/api/v1/automation-drafts/{did}/review", {"ok": True}),
        ("post", f"/api/v1/automation-drafts/{did}/manual-record", None),
        ("post", f"/api/v1/automation-drafts/{did}/schedule", {"kind": "daily", "hour": 9}),
        ("get", "/api/v1/automations", None),
        ("post", f"/api/v1/automations/draft:{did}/run", None),
        ("post", f"/api/v1/automations/draft:{did}/pause", None),
        ("post", f"/api/v1/automations/draft:{did}/resume", None),
    ]
    for method, url, payload in calls:
        response = getattr(client, method)(url, **({"json": payload} if payload is not None else {}))
        assert response.status_code == 404, (url, response.status_code)
        assert response.json()["detail"]["error"] == "automation_drafts_disabled"
    assert world.store.rows == {}


def test_pending_count_stays_open_when_flag_off(call, world, monkeypatch):
    monkeypatch.setenv(svc.DRAFTS_FLAG, "false")
    seen = {}

    async def fake_count(*, tenant_id):
        seen["tenant_id"] = tenant_id
        return {"pending": 2, "oldest_hours": 1.5}

    monkeypatch.setattr(automations_api, "pending_count", fake_count)
    response = call.as_(role="viewer").get("/api/v1/automations/pending-count")
    assert response.status_code == 200, response.text
    assert response.json() == {"pending": 2, "oldest_hours": 1.5}
    assert seen["tenant_id"] == TENANT_A


def test_each_module_owns_one_router_and_flag_gates_only_catalog_routes():
    assert not hasattr(api, "automations_router")
    gated = {r.path: bool(r.dependencies) for r in automations_api.router.routes}
    assert gated == {
        "/automations/pending-count": False,
        "/automations": True,
        "/automations/{key}/run": True,
        "/automations/{key}/pause": True,
        "/automations/{key}/resume": True,
    }


def test_viewer_role_is_forbidden(call, world):
    client = call.as_(role="viewer")
    assert client.post("/api/v1/automation-drafts", json={"intent_text": "쿠팡이츠 매출"}).status_code == 403
    assert client.get("/api/v1/automations").status_code == 403
    assert world.store.rows == {}


def test_secret_in_text_is_rejected_and_not_stored(call, world):
    response = call.as_().post(
        "/api/v1/automation-drafts", json={"intent_text": f"쿠팡이츠 로그인 {SK_LIKE} 로 해줘"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "secret_detected"
    assert world.store.rows == {} and world.llm_calls == 0


def test_get_other_user_forbidden_admin_allowed_other_tenant_404(call, world):
    did = make(call)["draft"]["id"]
    url = f"/api/v1/automation-drafts/{did}"
    assert call.as_(user=BOB).get(url).status_code == 403
    assert call.as_(user=BOB, role="admin").get(url).status_code == 200
    assert call.as_(user=ALICE, tenant=TENANT_B).get(url).status_code == 404
    assert call.as_().get(f"/api/v1/automation-drafts/{uuid.uuid4()}").status_code == 404
    assert call.as_().get("/api/v1/automation-drafts/not-a-uuid").status_code == 404


def test_patch_changes_only_given_fields_and_rejects_bad_values(call, world):
    body = make(call)
    did = body["draft"]["id"]
    url = f"/api/v1/automation-drafts/{did}"
    ok = call.as_().patch(url, json={"output": "파일"})
    assert ok.status_code == 200
    assert ok.json()["draft"]["intent"]["output_kind"] == "file"
    assert ok.json()["draft"]["intent"]["task"] == body["draft"]["intent"]["task"]
    assert call.as_().patch(url, json={"schedule": None}).json()["draft"]["intent"]["schedule"] is None
    assert call.as_().patch(url, json={"output": "음성"}).json()["detail"]["error"] == "output_invalid"
    assert call.as_().patch(url, json={}).status_code == 422
    assert call.as_().patch(url, json={"schedule": {"kind": "weekly", "hour": 9}}).status_code == 422
    assert call.as_().patch(url, json={"unknown": 1}).status_code == 422
    site = call.as_().patch(url, json={"site": "https://wing.coupang.com/login"})
    assert site.json()["draft"]["intent"]["domain"] == "wing.coupang.com"
    assert site.json()["draft"]["intent"]["needs_domain_confirm"] is False


def test_patch_after_confirm_is_409(call, world):
    did = to_state(call, world, "intent_confirmed")
    response = call.as_().patch(f"/api/v1/automation-drafts/{did}", json={"task": "다른 일"})
    assert response.status_code == 409


# ── 2. 확정·계정 연결 ──────────────────────────────────────────────────────

def confirm(call, did, **kw):
    return call.as_(**kw).post(
        f"/api/v1/automation-drafts/{did}/confirm-intent", json={"expected_state": "intent_draft"},
    )


def test_confirm_with_single_credential_shows_display_name_only(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "쿠팡이츠 사장님", "host": "store.coupangeats.com", "verified": True}]
    did = make(call)["draft"]["id"]
    response = confirm(call, did)
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["state"] == "intent_confirmed"
    assert body["credential"]["status"] == "single"
    assert body["credential"]["credentials"][0]["display_name"] == "쿠팡이츠 사장님"
    assert set(body["credential"]["credentials"][0]) == {"id", "display_name", "host", "verified"}
    assert body["draft"]["credential_id"] is None
    assert_clean(body)


def test_confirm_with_several_credentials_offers_choices(call, world):
    world.credentials = [
        {"id": str(uuid.uuid4()), "display_name": "본점", "host": "store.coupangeats.com", "verified": True},
        {"id": str(uuid.uuid4()), "display_name": "2호점", "host": "coupangeats.com", "verified": False},
    ]
    body = confirm(call, make(call)["draft"]["id"]).json()
    assert body["credential"]["status"] == "choices" and len(body["credential"]["credentials"]) == 2


def test_confirm_without_credential_issues_request_and_waits(call, world):
    did = make(call)["draft"]["id"]
    body = confirm(call, did).json()
    assert body["draft"]["state"] == "credential_waiting"
    assert body["credential"]["requestId"] == "req-1"
    assert world.requests[0]["draft_id"] == did
    assert world.requests[0]["session_id"] == f"automation-draft:{did}"
    assert world.store.rows[did]["credential_request_id"] == "req-1"


def test_confirm_when_secure_input_unavailable_stays_confirmed(call, world):
    world.secure_input_ok = False
    body = confirm(call, make(call)["draft"]["id"]).json()
    assert body["draft"]["state"] == "intent_confirmed"
    assert body["credential"]["request_error"] == "secure_input_disabled"


def test_confirm_wrong_expected_state_is_409(call, world):
    did = make(call)["draft"]["id"]
    response = call.as_().post(f"/api/v1/automation-drafts/{did}/confirm-intent", json={"expected_state": "intent_confirmed"})
    assert response.status_code == 409
    assert world.store.rows[did]["state"] == "intent_draft"


def test_double_confirm_second_is_409(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "a", "host": "store.coupangeats.com", "verified": True}]
    did = make(call)["draft"]["id"]
    assert confirm(call, did).status_code == 200
    assert confirm(call, did).status_code == 409


def test_confirm_requires_domain_confirmation_when_guessed(call, world, monkeypatch):
    async def llm_domain(text, tenant_id):
        return {"site_name": "내가게", "domain": "shop.example.com", "task": "주문 확인"}

    monkeypatch.setattr(parser, "_ask_llm", llm_domain)
    body = make(call, text="내가게 주문 확인해줘")
    assert body["draft"]["intent"]["needs_domain_confirm"] is True
    assert body["card"]["needs_domain_confirm"] is True
    did = body["draft"]["id"]
    assert confirm(call, did).json()["detail"]["error"] == "domain_confirmation_required"
    ok = call.as_().post(
        f"/api/v1/automation-drafts/{did}/confirm-intent",
        json={"expected_state": "intent_draft", "domain_confirmed": True},
    )
    assert ok.status_code == 200


def test_confirm_without_domain_is_422(call, world, monkeypatch):
    async def llm_nothing(text, tenant_id):
        return {}

    monkeypatch.setattr(parser, "_ask_llm", llm_nothing)
    did = make(call, text="뭔가 정리해줘")["draft"]["id"]
    assert confirm(call, did).json()["detail"]["error"] == "domain_required"


def test_choose_credential_stores_reference_only(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "쿠팡이츠 사장님", "host": "store.coupangeats.com", "verified": True}]
    did = make(call)["draft"]["id"]
    confirm(call, did)
    response = call.as_().post(f"/api/v1/automation-drafts/{did}/credential", json={"credential_id": CRED_ID})
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["state"] == "credential_ready"
    assert body["draft"]["credential_id"] == CRED_ID
    assert body["credential"] == {"status": "linked", "display_name": "쿠팡이츠 사장님"}
    assert "쿠팡이츠 사장님" not in json.dumps(world.store.rows[did], default=str, ensure_ascii=False)


def test_choose_credential_validation(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "a", "host": "store.coupangeats.com", "verified": True}]
    did = make(call)["draft"]["id"]
    url = f"/api/v1/automation-drafts/{did}/credential"
    assert call.as_().post(url, json={"credential_id": CRED_ID}).status_code == 409
    confirm(call, did)
    assert call.as_().post(url, json={}).json()["detail"]["error"] == "credential_choice_required"
    assert call.as_().post(url, json={"credential_id": CRED_ID, "request_new": True}).status_code == 422
    other = call.as_().post(url, json={"credential_id": str(uuid.uuid4())})
    assert other.status_code == 422 and other.json()["detail"]["error"] == "credential_mismatch"


def test_credential_request_new_from_choices(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "a", "host": "store.coupangeats.com", "verified": True}]
    did = make(call)["draft"]["id"]
    confirm(call, did)
    body = call.as_().post(f"/api/v1/automation-drafts/{did}/credential", json={"request_new": True}).json()
    assert body["draft"]["state"] == "credential_waiting" and body["credential"]["status"] == "requested"


def _credential_url(did):
    return f"/api/v1/automation-drafts/{did}/credential"


def test_no_link_never_skips_login_on_its_own_and_offers_no_login_button(call, world):
    body = confirm(call, make(call)["draft"]["id"]).json()
    assert body["draft"]["state"] == "credential_waiting"
    assert body["draft"]["credential_id"] is None
    assert body["draft"]["intent"]["login_required"] is None
    ids = [b["id"] for b in body["card"]["buttons"]]
    assert "no_login" in ids and "cancel" in ids
    assert next(b for b in body["card"]["buttons"] if b["id"] == "no_login")["label"] == "로그인 없이 진행"


def test_no_login_choice_moves_to_ready_without_credential_then_demo_starts(call, world):
    did = make(call)["draft"]["id"]
    confirm(call, did)
    response = call.as_().post(_credential_url(did), json={"no_login": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"]["state"] == "credential_ready"
    assert body["draft"]["credential_id"] is None
    assert body["draft"]["intent"]["login_required"] is False
    assert body["credential"] == {"status": "not_required"}
    assert any(e.get("type") == "credential_not_required" and e.get("to") == "credential_ready"
               for e in world.store.events)
    assert world.store.rows[did]["credential_id"] is None
    started = call.as_().post(f"/api/v1/automation-drafts/{did}/demo", json={"expected_state": "credential_ready"})
    assert started.status_code == 202 and started.json()["draft"]["state"] == "demonstrating"
    assert world.launched == [did]


def test_no_login_is_also_allowed_when_secure_input_could_not_open(call, world):
    world.secure_input_ok = False
    did = make(call)["draft"]["id"]
    assert confirm(call, did).json()["draft"]["state"] == "intent_confirmed"
    body = call.as_().post(_credential_url(did), json={"no_login": True}).json()
    assert body["draft"]["state"] == "credential_ready"


def test_credential_choice_must_be_exactly_one_of_three(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "a", "host": "store.coupangeats.com", "verified": True}]
    did = make(call)["draft"]["id"]
    confirm(call, did)
    url = _credential_url(did)
    for payload in (
        {"credential_id": CRED_ID, "no_login": True},
        {"request_new": True, "no_login": True},
        {"credential_id": CRED_ID, "request_new": True, "no_login": True},
        {"no_login": False},
    ):
        response = call.as_().post(url, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["detail"]["error"] == "credential_choice_required"
    assert world.store.rows[did]["state"] == "intent_confirmed"
    assert world.store.rows[did]["credential_id"] is None


def test_no_login_in_wrong_state_is_409(call, world):
    did = make(call)["draft"]["id"]
    response = call.as_().post(_credential_url(did), json={"no_login": True})
    assert response.status_code == 409 and response.json()["detail"]["error"] == "state_conflict"
    assert world.store.rows[did]["state"] == "intent_draft"


def test_no_login_does_not_follow_the_model_guess(call, world, monkeypatch):
    async def guess_no_login(text, tenant_id):
        return {"task": "공개 양식 확인", "output_kind": "screen", "login_required": False}

    monkeypatch.setattr(parser, "_ask_llm", guess_no_login)
    did = make(call)["draft"]["id"]
    assert json.loads(world.store.rows[did]["intent"])["login_required"] is None
    body = confirm(call, did).json()
    assert body["draft"]["state"] == "credential_waiting"
    assert body["draft"]["intent"]["login_required"] is None


def test_credential_waiting_moves_to_ready_when_submitted(call, world):
    did = make(call)["draft"]["id"]
    confirm(call, did)
    world.request_status = {"status": "submitted", "credential_id": CRED_ID}
    body = call.as_().get(f"/api/v1/automation-drafts/{did}").json()
    assert body["draft"]["state"] == "credential_ready"
    assert body["draft"]["credential_id"] == CRED_ID


def test_credential_waiting_expires_after_30_minutes(call, world):
    did = make(call)["draft"]["id"]
    confirm(call, did)
    world.store.rows[did]["updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=31)
    assert call.as_().get(f"/api/v1/automation-drafts/{did}").json()["draft"]["state"] == "expired"
    other = make(call)["draft"]["id"]
    confirm(call, other)
    world.request_status = {"status": "cancelled", "credential_id": None}
    assert call.as_().get(f"/api/v1/automation-drafts/{other}").json()["draft"]["state"] == "expired"


def test_credential_waiting_stays_waiting_before_30_minutes(call, world):
    did = make(call)["draft"]["id"]
    confirm(call, did)
    body = call.as_().get(f"/api/v1/automation-drafts/{did}").json()
    assert body["draft"]["state"] == "credential_waiting"
    assert body["credential"]["expires_in_minutes"] == 30


# ── 3. 시연·위험·결과 ──────────────────────────────────────────────────────

def test_demo_starts_from_ready_and_failed_only(call, world):
    did = to_state(call, world, "credential_ready")
    url = f"/api/v1/automation-drafts/{did}/demo"
    wrong = call.as_().post(url, json={"expected_state": "intent_draft"})
    assert wrong.status_code == 409
    ok = call.as_().post(url, json={"expected_state": "credential_ready"})
    assert ok.status_code == 202
    assert ok.json()["draft"]["state"] == "demonstrating"
    assert ok.json()["progress_event"] == "automation_draft.progress"
    assert call.as_().post(url, json={"expected_state": "demonstrating"}).status_code == 409
    failed = to_state(call, world, "failed")
    again = call.as_().post(f"/api/v1/automation-drafts/{failed}/demo", json={"expected_state": "failed"})
    assert again.status_code == 202


def test_demo_blocked_for_other_user_and_other_tenant(call, world):
    did = to_state(call, world, "credential_ready")
    url = f"/api/v1/automation-drafts/{did}/demo"
    assert call.as_(user=BOB).post(url, json={"expected_state": "credential_ready"}).status_code == 403
    assert call.as_(tenant=TENANT_B).post(url, json={"expected_state": "credential_ready"}).status_code == 404
    assert world.store.rows[did]["state"] == "credential_ready"


@pytest.mark.parametrize("decision,target", [
    ("proceed", "demonstrating"), ("stop_here", "result_review"), ("cancel", "cancelled"),
])
def test_risk_decision(call, world, decision, target):
    did = to_state(call, world, "paused_risk")
    response = call.as_().post(f"/api/v1/automation-drafts/{did}/risk-decision", json={"decision": decision})
    assert response.status_code == 200 and response.json()["draft"]["state"] == target


def test_risk_decision_wrong_state_and_bad_value(call, world):
    did = to_state(call, world, "credential_ready")
    url = f"/api/v1/automation-drafts/{did}/risk-decision"
    assert call.as_().post(url, json={"decision": "proceed"}).status_code == 409
    assert call.as_().post(url, json={"decision": "whatever"}).status_code == 422


def test_review_ok_marks_screen_verified_and_moves_to_registering(call, world):
    did = to_state(call, world, "result_review")
    response = call.as_().post(f"/api/v1/automation-drafts/{did}/review", json={"ok": True})
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["state"] == "registering"
    assert body["draft"]["evidence"] == {"screen_verified": True}


def test_review_not_ok_goes_to_revising_with_reason(call, world):
    did = to_state(call, world, "result_review")
    response = call.as_().post(
        f"/api/v1/automation-drafts/{did}/review", json={"ok": False, "reason_code": "wrong_table", "note": "열이 달라요"},
    )
    assert response.json()["draft"]["state"] == "revising"
    bad = to_state(call, world, "result_review")
    assert call.as_().post(f"/api/v1/automation-drafts/{bad}/review", json={"ok": False, "reason_code": "Bad Code"}).status_code == 422


def test_review_wrong_state_409_and_secret_in_note_422(call, world):
    did = to_state(call, world, "credential_ready")
    assert call.as_().post(f"/api/v1/automation-drafts/{did}/review", json={"ok": True}).status_code == 409
    rev = to_state(call, world, "result_review")
    secret = call.as_().post(f"/api/v1/automation-drafts/{rev}/review", json={"ok": False, "note": SK_LIKE})
    assert secret.status_code == 422
    assert world.store.rows[rev]["state"] == "result_review"


def test_manual_record_flow(call, world, monkeypatch):
    did = to_state(call, world, "revising")
    url = f"/api/v1/automation-drafts/{did}/manual-record"
    first = call.as_().post(url)
    assert first.status_code == 200 and first.json()["draft"]["state"] == "manual_record"
    assert call.as_().post(url).status_code == 403  # 녹화 시작은 관리자만

    import app.api.ohvis_recipes as recipes_api

    started: list[Any] = []

    async def fake_start(body, context):
        started.append(body)
        return {"recording_id": "rec-1"}

    monkeypatch.setattr(recipes_api, "start_recording", fake_start)
    admin = call.as_(user=ALICE, role="admin").post(url)
    assert admin.status_code == 200 and admin.json()["recording_id"] == "rec-1"
    assert started[0].domain == "store.coupangeats.com"
    assert json.loads(world.store.rows[did]["recording_ref"]) == {"recording_id": "rec-1"}
    assert call.as_().post(f"/api/v1/automation-drafts/{to_state(call, world, 'intent_draft')}/manual-record").status_code == 409


# ── 4. 예약 ────────────────────────────────────────────────────────────────

def test_schedule_before_approval_registers_inactive(call, world):
    did = to_state(call, world, "result_review")
    response = call.as_().post(f"/api/v1/automation-drafts/{did}/schedule", json={"kind": "daily", "hour": 9})
    assert response.status_code == 200
    body = response.json()
    assert body["schedule"]["text"] == "매일 09:00" and body["schedule"]["active"] is False
    job = world.jobs[body["schedule"]["schedule_id"]]
    assert job["active"] is False and job["owner_user"] == ALICE
    assert body["draft"]["intent"]["schedule"]["kind"] == "daily"
    assert "schedule_job_id" not in body["draft"]
    assert world.store.rows[did]["schedule_job_id"] == body["schedule"]["schedule_id"]


def test_schedule_replaces_old_job_and_active_draft_runs_immediately(call, world):
    did = to_state(call, world, "active")
    url = f"/api/v1/automation-drafts/{did}/schedule"
    first = call.as_().post(url, json={"kind": "weekdays", "hour": 8, "minute": 30}).json()
    second = call.as_().post(url, json={"kind": "weekly", "hour": 7, "day_of_week": "mon"}).json()
    assert second["schedule"]["text"] == "매주 월요일 07:00" and second["schedule"]["active"] is True
    assert len(world.jobs) == 1 and first["schedule"]["schedule_id"] == second["schedule"]["schedule_id"]
    assert second["schedule"]["next_run"]


def test_schedule_validation_and_state(call, world):
    did = to_state(call, world, "result_review")
    url = f"/api/v1/automation-drafts/{did}/schedule"
    assert call.as_().post(url, json={"kind": "weekly", "hour": 9}).status_code == 422
    assert call.as_().post(url, json={"kind": "daily", "hour": 24}).status_code == 422
    assert call.as_().post(url, json={"kind": "hourly", "hour": 9}).status_code == 422
    done = to_state(call, world, "cancelled")
    assert call.as_().post(f"/api/v1/automation-drafts/{done}/schedule", json={"kind": "daily", "hour": 9}).status_code == 409
    assert call.as_(user=BOB).post(url, json={"kind": "daily", "hour": 9}).status_code == 403


def test_activate_on_approval_resumes_schedule(call, world):
    did = to_state(call, world, "pending_approval")
    asyncio.run(flow.set_schedule(flow.Actor(TENANT_A, ALICE), did, {"kind": "daily", "hour": 9}))
    job_id = schedule.job_id_of(did)
    assert world.jobs[job_id]["active"] is False
    body = asyncio.run(flow.activate_on_approval(flow.Actor(TENANT_A, ALICE), did))
    assert body["draft"]["state"] == "active" and world.jobs[job_id]["active"] is True


# ── 5. 통합 목록·동작 ──────────────────────────────────────────────────────

def test_list_hides_source_for_members_and_shows_to_admin(call, world, monkeypatch):
    async def none(*a, **k):
        return []

    monkeypatch.setattr(catalog, "_work_items", lambda *a, **k: none())
    monkeypatch.setattr(catalog, "_browser_items", lambda *a, **k: none())
    monkeypatch.setattr(catalog, "_ovis_items", lambda *a, **k: none())
    mine = to_state(call, world, "active")
    pend = to_state(call, world, "pending_approval")
    theirs = to_state(call, world, "active", user=BOB)
    gone = to_state(call, world, "cancelled")
    member = call.as_().get("/api/v1/automations").json()
    keys = {item["key"] for item in member["automations"]}
    assert keys == {f"draft:{mine}", f"draft:{pend}"}
    assert all("detail" not in item for item in member["automations"])
    labels = {item["key"]: item["status_label"] for item in member["automations"]}
    assert labels[f"draft:{mine}"] == "사용 가능" and labels[f"draft:{pend}"] == "확인 기다리는 중"
    assert member["counts"]["available"] == 1 and member["counts"]["waiting"] == 1
    admin = call.as_(user="ceo@example.com", role="owner").get("/api/v1/automations").json()
    assert {item["key"] for item in admin["automations"]} == {f"draft:{mine}", f"draft:{pend}", f"draft:{theirs}"}
    assert all(item["detail"]["source"] == "draft" for item in admin["automations"])
    assert f"draft:{gone}" not in {item["key"] for item in admin["automations"]}
    filtered = call.as_().get("/api/v1/automations", params={"status": "waiting"}).json()
    assert [item["key"] for item in filtered["automations"]] == [f"draft:{pend}"]
    assert call.as_().get("/api/v1/automations", params={"status": "bogus"}).status_code == 422


def test_list_is_tenant_isolated(call, world, monkeypatch):
    async def none(*a, **k):
        return []

    monkeypatch.setattr(catalog, "_work_items", lambda *a, **k: none())
    monkeypatch.setattr(catalog, "_browser_items", lambda *a, **k: none())
    monkeypatch.setattr(catalog, "_ovis_items", lambda *a, **k: none())
    to_state(call, world, "active")
    other = call.as_(tenant=TENANT_B, role="owner").get("/api/v1/automations").json()
    assert other["automations"] == [] and other["total"] == 0


def test_pause_and_resume_touch_scheduler_only(call, world):
    did = to_state(call, world, "active")
    call.as_().post(f"/api/v1/automation-drafts/{did}/schedule", json={"kind": "daily", "hour": 9})
    job_id = schedule.job_id_of(did)
    paused = call.as_().post(f"/api/v1/automations/draft:{did}/pause")
    assert paused.status_code == 200 and world.jobs[job_id]["active"] is False
    assert paused.json()["draft"]["state"] == "active"
    resumed = call.as_().post(f"/api/v1/automations/draft:{did}/resume")
    assert resumed.status_code == 200 and world.jobs[job_id]["active"] is True


def test_pause_wrong_state_source_and_owner(call, world):
    pending = to_state(call, world, "pending_approval")
    assert call.as_().post(f"/api/v1/automations/draft:{pending}/pause").status_code == 409
    active = to_state(call, world, "active")
    assert call.as_().post(f"/api/v1/automations/draft:{active}/pause").json()["detail"]["error"] == "no_schedule"
    assert call.as_().post(f"/api/v1/automations/work:{uuid.uuid4()}/pause").status_code == 409
    assert call.as_().post("/api/v1/automations/bogus/pause").status_code == 404
    assert call.as_(user=BOB).post(f"/api/v1/automations/draft:{active}/pause").status_code == 403


def test_run_now_uses_server_context_and_reports_status(call, world, monkeypatch):
    import app.services.work_recipe.orchestrator as orch

    seen: dict[str, Any] = {}

    class Result:
        status = "success"
        run_id = uuid.uuid4()

    async def fake_run(directive, tenant_id, **kw):
        seen.update(directive=directive, tenant_id=tenant_id, triggered_by=kw["triggered_by"], intent=kw["action_intent"])
        return Result()

    monkeypatch.setattr(orch, "run_directive", fake_run)
    did = to_state(call, world, "active")
    response = call.as_().post(f"/api/v1/automations/draft:{did}/run")
    assert response.status_code == 200, response.text
    assert response.json()["run"]["status"] == "success"
    assert seen["tenant_id"] == TENANT_A and seen["triggered_by"] == ALICE
    assert seen["intent"].tenant_id == TENANT_A
    waiting = to_state(call, world, "pending_approval")
    assert call.as_().post(f"/api/v1/automations/draft:{waiting}/run").status_code == 409


def test_run_scheduled_builds_intent_from_attested_context(world, monkeypatch):
    import app.services.work_recipe.orchestrator as orch

    seen: dict[str, Any] = {}

    class Result:
        status = "success"

    async def fake_run(directive, tenant_id, **kw):
        seen.update(directive=directive, tenant_id=tenant_id, who=kw["triggered_by"], intent=kw["action_intent"])
        return Result()

    monkeypatch.setattr(orch, "run_directive", fake_run)
    config = {"tenant_id": TENANT_A, "draft_id": str(uuid.uuid4()), "owner_user": ALICE, "directive": "쿠팡이츠 매출"}
    assert asyncio.run(flow.run_scheduled(config)) == "status=success"
    assert seen["who"] == f"schedule:{ALICE}" and seen["tenant_id"] == TENANT_A
    assert seen["intent"].payload["directive"] == "쿠팡이츠 매출"


# ── 6. 비밀값 0 ────────────────────────────────────────────────────────────

def test_no_secret_in_any_response_or_event(call, world):
    world.credentials = [{"id": CRED_ID, "display_name": "쿠팡이츠 사장님", "host": "store.coupangeats.com", "verified": True}]
    body = make(call)
    did = body["draft"]["id"]
    assert_clean(body)
    steps = [
        confirm(call, did),
        call.as_().post(f"/api/v1/automation-drafts/{did}/credential", json={"credential_id": CRED_ID}),
        call.as_().post(f"/api/v1/automation-drafts/{did}/demo", json={"expected_state": "credential_ready"}),
    ]
    for step in steps:
        assert_clean(step.json())
    assert svc.find_secret(world.store.rows[did]) is None
    assert svc.find_secret(world.store.events) is None


def test_flow_error_responses_carry_codes_not_secrets(call, world):
    response = call.as_().post("/api/v1/automation-drafts", json={"intent_text": f"비밀번호 {SK_LIKE}"})
    assert SK_LIKE not in response.text


# ── 7. 해석기 ──────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def test_parser_dictionary_url_recipe_and_llm_priority(monkeypatch):
    async def llm(text, tenant_id):
        return {"domain": "llm.example.com", "site_name": "LLM", "task": "요약", "output_kind": "file"}

    monkeypatch.setattr(parser, "_ask_llm", llm)
    rows = [{"domain": "wing.coupang.com"}, {"domain": "mystore.co.kr"}]
    out = run(parser.parse_intent("쿠팡이츠 매출", recipe_rows=rows))
    assert out["domain"] == "store.coupangeats.com" and out["domain_source"] == "dictionary"
    assert out["needs_domain_confirm"] is False and out["output_kind"] == "file"
    out = run(parser.parse_intent("https://admin.foo.kr/login 에서 주문 확인", recipe_rows=rows))
    assert out["domain"] == "admin.foo.kr" and out["domain_source"] == "text"
    out = run(parser.parse_intent("mystore 주문 확인", recipe_rows=rows))
    assert out["domain"] == "mystore.co.kr" and out["domain_source"] == "recipe"
    out = run(parser.parse_intent("어떤 가게 주문 확인", recipe_rows=rows))
    assert out["domain"] == "llm.example.com" and out["needs_domain_confirm"] is True


def test_parser_falls_back_to_rules_when_llm_fails(monkeypatch):
    import app.core.anthropic_client as client

    calls: list[int] = []

    async def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("llm down")

    monkeypatch.setattr(client, "call_llm_with_fallback", boom)
    out = run(parser.parse_intent("매일 아침 쿠팡이츠 매출 정리", tenant_id=TENANT_A))
    assert len(calls) == 1
    assert out["domain"] == "store.coupangeats.com" and out["output_kind"] == "screen"
    assert out["schedule"] == {"kind": "daily", "hour": 9, "minute": 0}


def test_parser_calls_llm_once_through_central_client(monkeypatch):
    import app.core.anthropic_client as client

    seen: list[dict[str, Any]] = []

    async def fake(prompt, **kw):
        seen.append(kw)
        return '{"task": "매출 확인", "output_kind": "table", "domain": ""}'

    monkeypatch.setattr(client, "call_llm_with_fallback", fake)
    out = run(parser.parse_intent("쿠팡이츠 매출 확인", tenant_id=TENANT_A))
    assert len(seen) == 1 and out["task"] == "매출 확인" and out["output_kind"] == "table"


def test_parser_rejects_secret_before_llm(monkeypatch):
    called: list[int] = []

    async def llm(text, tenant_id):
        called.append(1)
        return {}

    monkeypatch.setattr(parser, "_ask_llm", llm)
    with pytest.raises(svc.SecretDetected):
        run(parser.parse_intent(f"토큰 {SK_LIKE}"))
    assert called == []


@pytest.mark.parametrize("text,expected", [
    ("매일 아침 정리", {"kind": "daily", "hour": 9, "minute": 0}),
    ("매일 오후 3시 30분", {"kind": "daily", "hour": 15, "minute": 30}),
    ("평일 저녁 6시", {"kind": "weekdays", "hour": 18, "minute": 0}),
    ("매주 월요일 오전 8시", {"kind": "weekly", "hour": 8, "minute": 0, "day_of_week": "mon"}),
    ("지금 한 번만 해줘", {"kind": "once", "hour": 9, "minute": 0}),
])
def test_parse_schedule(text, expected):
    assert parser.parse_schedule(text) == expected


def test_parse_schedule_none_and_normalize():
    assert parser.parse_schedule("매출 확인해줘") is None
    assert parser.normalize_schedule(None) is None
    assert parser.normalize_schedule({"kind": "weekly", "hour": "7", "day_of_week": "0"})["day_of_week"] == "mon"
    for bad in ("daily", {"kind": "yearly"}, {"kind": "daily", "hour": 30}, {"kind": "weekly", "hour": 9}):
        with pytest.raises(ValueError):
            parser.normalize_schedule(bad)


# ── 8. 계정 연결 ───────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.queries = rows, []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *args):
        self.queries.append(query)
        return self.rows

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return self.rows[0] if self.rows else None


def cred_row(origin: str, label: str = "계정", **kw: Any) -> dict[str, Any]:
    return {"id": uuid.uuid4(), "label": label, "origin": origin, "metadata": kw.get("metadata", "{}"),
            "last_used_at": None, "updated_at": None}


@pytest.mark.parametrize("origin,domain,expected", [
    ("https://store.coupangeats.com", "store.coupangeats.com", True),
    ("https://login.coupangeats.com", "store.coupangeats.com", True),
    ("https://coupangeats.com", "store.coupangeats.com", True),
    ("https://coupangeats.com.evil.io", "store.coupangeats.com", False),
    ("https://evilcoupangeats.com", "store.coupangeats.com", False),
    ("https://wing.coupang.com", "store.coupangeats.com", False),
    ("", "store.coupangeats.com", False),
])
def test_domain_matches(origin, domain, expected):
    assert linker.domain_matches(origin, domain) is expected


def test_find_credentials_never_selects_secret_columns(monkeypatch):
    conn = FakeConn([
        cred_row("https://store.coupangeats.com", "본점", metadata='{"verification_status": "verified"}'),
        cred_row("https://evilcoupangeats.com", "다른곳"),
    ])
    monkeypatch.setattr(linker, "get_pool", lambda: conn)
    found = run(linker.find_credentials(tenant_id=TENANT_A, domain="store.coupangeats.com"))
    assert [c["display_name"] for c in found] == ["본점"] and found[0]["verified"] is True
    assert set(found[0]) == {"id", "display_name", "host", "verified"}
    for query in conn.queries:
        assert "_enc" not in query and "password" not in query.lower() and "tenant_id" in query
    status = run(linker.link_status(tenant_id=TENANT_A, domain="store.coupangeats.com"))
    assert status["status"] == "single"
    monkeypatch.setattr(linker, "get_pool", lambda: FakeConn([]))
    assert run(linker.link_status(tenant_id=TENANT_A, domain="store.coupangeats.com"))["status"] == "none"


def test_get_credential_for_domain_checks_tenant_and_domain(monkeypatch):
    row = cred_row("https://store.coupangeats.com", "본점")
    monkeypatch.setattr(linker, "get_pool", lambda: FakeConn([row]))
    ok = run(linker.get_credential_for_domain(tenant_id=TENANT_A, credential_id=str(row["id"]), domain="store.coupangeats.com"))
    assert ok and ok["display_name"] == "본점"
    assert run(linker.get_credential_for_domain(tenant_id=TENANT_A, credential_id=str(row["id"]), domain="wing.coupang.com")) is None
    assert run(linker.get_credential_for_domain(tenant_id=TENANT_A, credential_id="nope", domain="store.coupangeats.com")) is None
    monkeypatch.setattr(linker, "get_pool", lambda: FakeConn([]))
    assert run(linker.get_credential_for_domain(tenant_id=TENANT_B, credential_id=str(row["id"]), domain="store.coupangeats.com")) is None


def test_request_new_input_uses_existing_secure_input(monkeypatch):
    from app.services import vault_secure_input as vault

    seen: dict[str, Any] = {}

    async def fake(**kw):
        seen.update(kw)
        return {"request_id": "r-9", "host": "store.coupangeats.com", "reused": False}

    monkeypatch.setattr(vault, "is_enabled", lambda: True)
    monkeypatch.setattr(vault, "request_credential_input", fake)
    out = run(linker.request_new_input(
        tenant_id=TENANT_A, session_id="automation-draft:x", url="https://store.coupangeats.com/login?x=1", draft_id="x",
    ))
    assert out["requestId"] == "r-9" and out["expires_in_minutes"] == 30
    assert seen["url"] == "https://store.coupangeats.com/login" and seen["tenant_id"] == TENANT_A
    monkeypatch.setattr(vault, "is_enabled", lambda: False)
    with pytest.raises(vault.SecureInputError):
        run(linker.request_new_input(tenant_id=TENANT_A, session_id="s", url="https://a.com", draft_id="x"))


# ── 9. 예약 변환 ───────────────────────────────────────────────────────────

def test_cron_config_shifts_weekday_back_when_kst_before_nine():
    assert schedule.cron_config({"kind": "daily", "hour": 9, "minute": 0})["day_of_week"] == "mon,tue,wed,thu,fri,sat,sun"
    assert schedule.cron_config({"kind": "weekdays", "hour": 9, "minute": 0})["day_of_week"] == "mon,tue,wed,thu,fri"
    assert schedule.cron_config({"kind": "weekdays", "hour": 8, "minute": 0})["day_of_week"] == "sun,mon,tue,wed,thu"
    assert schedule.cron_config({"kind": "weekly", "hour": 7, "minute": 30, "day_of_week": "mon"}) == {
        "hour": 7, "minute": 30, "day_of_week": "sun",
    }
    with pytest.raises(ValueError):
        schedule.cron_config({"kind": "once", "hour": 9, "minute": 0})


def test_once_delay_minutes():
    now = datetime(2026, 10, 14, 8, 0, tzinfo=schedule.KST)
    assert schedule.once_delay_minutes({"hour": 9, "minute": 0}, now=now) == 60
    assert schedule.once_delay_minutes({"hour": 7, "minute": 0}, now=now) == 23 * 60


def test_register_inactive_pauses_job_and_defers_once(monkeypatch):
    from app.api import ceo_chat_tools_scheduler as sched

    paused: list[str] = []
    added: list[tuple] = []

    class FakeScheduler:
        def pause_job(self, job_id):
            paused.append(job_id)

    async def fake_schedule(scheduler, name, schedule_type, action_type, action_config, schedule_config, **kw):
        added.append((name, schedule_type, action_type, action_config, schedule_config, kw))
        return {"job_id": f"user_{name}", "persisted": True}

    monkeypatch.setattr(sched, "_ensure_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(sched, "schedule_task_on", fake_schedule)
    out = run(schedule.register(
        draft_id="d1", tenant_id=TENANT_A, owner_user=ALICE, directive="쿠팡이츠",
        schedule={"kind": "daily", "hour": 9, "minute": 0}, active=False,
    ))
    assert out["job_id"] == "user_automation_d1" and paused == ["user_automation_d1"]
    name, kind, action, config, cron, kw = added[0]
    assert (kind, action) == ("cron", "automation_run") and cron["hour"] == 9
    assert config == {"tenant_id": TENANT_A, "draft_id": "d1", "owner_user": ALICE, "directive": "쿠팡이츠"}
    assert kw["report_to_session"] is False
    deferred = run(schedule.register(
        draft_id="d2", tenant_id=TENANT_A, owner_user=ALICE, directive="x",
        schedule={"kind": "once", "hour": 9, "minute": 0}, active=False,
    ))
    assert deferred == {"job_id": None, "deferred": True} and len(added) == 1


def test_register_failure_raises_unavailable(monkeypatch):
    from app.api import ceo_chat_tools_scheduler as sched

    async def fail(*a, **k):
        return {"error": "bad"}

    monkeypatch.setattr(sched, "_ensure_scheduler", lambda: object())
    monkeypatch.setattr(sched, "schedule_task_on", fail)
    with pytest.raises(schedule.ScheduleUnavailable):
        run(schedule.register(
            draft_id="d1", tenant_id=TENANT_A, owner_user=ALICE, directive="x",
            schedule={"kind": "daily", "hour": 9, "minute": 0}, active=True,
        ))


def test_scheduler_accepts_automation_run_action():
    import inspect

    from app.api import ceo_chat_tools_scheduler as sched

    source = inspect.getsource(sched)
    assert '"automation_run"' in source and "run_scheduled" in source


# ── 10. 통합 목록(출처 합치기) ─────────────────────────────────────────────

def test_catalog_merges_sources_dedups_and_tolerates_missing_tables(world, monkeypatch):
    import asyncpg

    async def work(tenant_id, taken):
        assert "wing_sales" in taken
        return [catalog._item("work:1", "승인된 업무", "available", source="work_recipe",
                              last_run={"status": "success", "at": "2026-10-13T00:00:00"})]

    async def browser(tenant_id):
        raise asyncpg.UndefinedTableError("missing")

    async def ovis(tenant_id):
        return [catalog._item("ovis:1", "스킬", "broken", source="ovis_recipe")]

    monkeypatch.setattr(catalog, "_work_items", work)
    monkeypatch.setattr(catalog, "_browser_items", browser)
    monkeypatch.setattr(catalog, "_ovis_items", ovis)
    did = run(world.store.create_draft(tenant_id=TENANT_A, owner_user=ALICE, title="t", intent_text="i"))["id"]
    world.store.rows[did].update(state="active", recipe_names=["wing_sales"])
    admin = run(catalog.list_automations(tenant_id=TENANT_A, user=ALICE, is_admin=True))
    assert admin["unavailable_sources"] == ["browser_recipe"]
    assert {i["key"] for i in admin["automations"]} == {f"draft:{did}", "work:1", "ovis:1"}
    assert admin["counts"] == {"available": 2, "waiting": 0, "broken": 1, "expired": 0}
    member = run(catalog.list_automations(tenant_id=TENANT_A, user=ALICE, is_admin=False))
    assert "unavailable_sources" not in member and all("detail" not in i for i in member["automations"])
    work_item = next(i for i in member["automations"] if i["key"] == "work:1")
    assert work_item["last_run"] == {"status": "success", "at": "2026-10-13T00:00:00"}


def test_catalog_sql_is_tenant_scoped_and_selects_no_errors():
    for sql in (catalog._WORK_RUNS_SQL, catalog._BROWSER_SQL, catalog._BROWSER_RUNS_SQL, catalog._OVIS_SQL):
        assert "tenant_id" in sql
    for sql in (catalog._WORK_RUNS_SQL, catalog._BROWSER_RUNS_SQL):
        assert "error" not in sql.lower()


# ── 11. 서비스 확장(SQL) ───────────────────────────────────────────────────

def test_update_fields_and_list_drafts_sql_are_tenant_scoped():
    import inspect

    update_src = inspect.getsource(svc.update_fields)
    list_src = inspect.getsource(svc.list_drafts)
    assert "tenant_id" in update_src and "tenant_id=$1::uuid" in list_src
    assert set(svc._UPDATABLE_COLUMNS) == {
        "title", "intent", "aliases", "credential_id", "credential_request_id", "recording_ref", "evidence",
        "schedule_job_id",
        # T3: 시연 실행기·조각 등록이 채우는 열
        "max_risk", "demo_attempts", "registration_ids", "recipe_names",
    }
    for forbidden in ("state", "tenant_id", "owner_user", "id", "lane"):
        assert forbidden not in svc._UPDATABLE_COLUMNS


def test_actor_requires_a_person():
    with pytest.raises(flow.FlowError) as err:
        flow.actor_from_context({"tenant": {"id": TENANT_A}, "membership": {"role": "member"}, "user": {}})
    assert err.value.status_code == 403
    actor = flow.actor_from_context(ctx(role="owner"))
    assert actor.user == ALICE and actor.is_admin is True


def test_tool_registered():
    from app.services import tool_registry
    from app.services.tool_executor import ToolExecutor

    tool = tool_registry._TOOLS["automation_draft"]
    assert tool["input_schema"]["properties"]["action"]["enum"] == [
        "start", "confirm", "credential", "demo", "review", "status",
    ]
    assert "smart_browser" in tool_registry._TOOLS
    assert hasattr(ToolExecutor, "_automation_draft")
