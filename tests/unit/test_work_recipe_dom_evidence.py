"""서버 발급 DOM 증거 artifact — 발급·검증·위조/만료/타 테넌트/타 세션/타 대상 차단과 일괄 초안 등록."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services.work_recipe import dom_evidence as de
from app.services.work_recipe import draft_registration as dr
from app.services.work_recipe import executor as executor_module
from app.services.work_recipe import recorder as recorder_module
from app.services.work_recipe import registration as registration_module
from app.services.work_recipe.schema import RecipeStep

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
OTHER_TENANT = "3e701a8c-9596-4757-8588-faa4f7837113"
SESSION = "11111111-1111-1111-1111-111111111111"
OTHER_SESSION = "22222222-2222-2222-2222-222222222222"
URL = "https://aads.newtalk.kr/ohvis/orders"
VERIFY = [{"assertion": "text_contains", "expected": "주문 목록"}]
SCREEN = "https://aads.newtalk.kr/screenshots/evidence.png"


class _Store:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, dict[str, Any]] = {}

    def acquire(self):
        store = self

        class Conn:
            async def fetchrow(self_inner, query: str, *args: Any):
                if "INSERT INTO browser_artifacts" in query:
                    tenant, kind, source_url, digest, storage, meta = args
                    row = {"id": uuid.uuid4(), "tenant_id": tenant, "artifact_type": kind,
                           "source_url": source_url, "content_hash": digest, "storage_uri": storage, "metadata": meta}
                    store.rows[row["id"]] = row
                    return row
                artifact_id, tenant, kind = args
                row = store.rows.get(artifact_id)
                if row and row["tenant_id"] == tenant and row["artifact_type"] == kind:
                    return row
                return None

        class Acq:
            async def __aenter__(self_inner):
                return Conn()

            async def __aexit__(self_inner, *exc):
                return False

        return Acq()


class _Page:
    def __init__(self, url=URL, *, contains=True):
        self.url, self.contains = url, contains

    async def evaluate(self, expression, argument=None, **kwargs):
        if expression == "window.location.href":
            return self.url
        return {"url": self.url, "visible": True, "contains": self.contains}

    def locator(self, selector):
        class Loc:
            async def inner_text(self_inner, **kwargs):
                return "주문 목록 SESSION-COOKIE=abc123"

        return Loc()

    async def screenshot(self, **kwargs):
        return b"png-bytes"


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(de, "get_pool", lambda: s)
    return s


@pytest.fixture
def page(monkeypatch):
    import asyncio
    import socket

    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    ])
    holder = {"page": _Page()}

    async def acquire(**kwargs):
        class Ctx:
            pages = [holder["page"]]

        return Ctx(), None

    async def save(data, *, prefix=""):
        return SCREEN

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    monkeypatch.setattr(executor_module, "save_png", save)
    return holder


async def _issue(**overrides):
    kwargs = dict(tenant_id=TENANT, chat_session_id=SESSION, agent=f"chat:{SESSION}", url=URL, verify=VERIFY)
    kwargs.update(overrides)
    return await de.issue_dom_evidence(**kwargs)


async def _verify(issued, **overrides):
    kwargs = dict(tenant_id=TENANT, chat_session_id=SESSION, claimed={"artifact_id": issued["artifact_id"]},
                  domain="aads.newtalk.kr", verify=VERIFY)
    kwargs.update(overrides)
    return await de.verify_dom_evidence(**kwargs)


# ------------------------------------------------------------------ 발급·검증


async def test_server_headless_internal_host_issues_without_browser_session(store, page):
    issued = await _issue()
    assert issued["screenshot_url"] == SCREEN
    row = next(iter(store.rows.values()))
    meta = json.loads(row["metadata"])
    assert meta["origin"] == "server_observation" and meta["route"] == "browser_agent"
    assert meta["browser_session_id"] == "" and meta["work_key"] == ""
    assert meta["chat_session_id"] == SESSION and meta["tenant_id"] == TENANT
    assert meta["assertions"][0]["assertion"] == "text_contains"
    assert row["content_hash"] == meta["binding_hash"] == de.binding_hash(meta)


async def test_issued_metadata_never_holds_page_text_or_credentials(store, page):
    await _issue()
    stored = json.dumps(next(iter(store.rows.values())), default=str)
    assert "SESSION-COOKIE" not in stored and "abc123" not in stored


async def test_valid_artifact_is_accepted(store, page):
    issued = await _issue()
    trusted = await _verify(issued)
    assert trusted["artifact_id"] == issued["artifact_id"]
    assert trusted["binding_hash"] == issued["binding_hash"]


async def test_failed_verification_issues_nothing(store, page):
    page["page"] = _Page(contains=False)
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_verify_failed"):
        await _issue()
    assert store.rows == {}


async def test_external_host_needs_pc_lane(store, page):
    page["page"] = _Page(url="https://shop.example.com/orders")
    with pytest.raises(de.DomEvidenceError, match="pc_agent_lane_required"):
        await _issue(url="https://shop.example.com/orders")
    assert store.rows == {}


async def test_pc_session_lane_issues_for_external_host(store, page):
    page["page"] = _Page(url="https://shop.example.com/orders")
    issued = await _issue(url="https://shop.example.com/orders", browser_session_id="bs-1", browser_work_key="wk-1")
    meta = json.loads(store.rows[uuid.UUID(issued["artifact_id"])]["metadata"])
    assert meta["route"] == "pc_agent" and meta["work_key"] == "wk-1"


async def test_page_that_left_the_target_origin_is_refused(store, page):
    page["page"] = _Page(url="https://evil.example.org/orders")
    with pytest.raises(de.DomEvidenceError):
        await _issue()
    assert store.rows == {}


# ------------------------------------------------------------------ 차단 사례


async def test_forged_artifact_id_is_not_found(store, page):
    await _issue()
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_not_found"):
        await de.verify_dom_evidence(tenant_id=TENANT, chat_session_id=SESSION,
                                     claimed={"artifact_id": str(uuid.uuid4())}, domain="aads.newtalk.kr")


async def test_malformed_artifact_id_is_refused(store, page):
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_invalid_id"):
        await de.verify_dom_evidence(tenant_id=TENANT, chat_session_id=SESSION,
                                     claimed={"artifact_id": "not-a-uuid"}, domain="aads.newtalk.kr")


async def test_other_tenant_cannot_use_artifact(store, page):
    issued = await _issue()
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_not_found"):
        await _verify(issued, tenant_id=OTHER_TENANT)


async def test_other_session_cannot_use_artifact(store, page):
    issued = await _issue()
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_session_mismatch"):
        await _verify(issued, chat_session_id=OTHER_SESSION)


async def test_expired_artifact_is_refused(store, page):
    issued = await _issue(ttl_seconds=60)
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_expired"):
        await _verify(issued, now=later)


async def test_other_target_is_refused(store, page):
    issued = await _issue()
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_target_mismatch"):
        await _verify(issued, domain="shop.example.com")


async def test_tampered_metadata_breaks_the_hash(store, page):
    issued = await _issue()
    row = store.rows[uuid.UUID(issued["artifact_id"])]
    meta = json.loads(row["metadata"])
    meta["chat_session_id"] = OTHER_SESSION
    row["metadata"] = json.dumps(meta)
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_hash_mismatch"):
        await _verify(issued, chat_session_id=OTHER_SESSION)


async def test_extended_expiry_breaks_the_hash(store, page):
    issued = await _issue(ttl_seconds=60)
    row = store.rows[uuid.UUID(issued["artifact_id"])]
    meta = json.loads(row["metadata"])
    meta["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    row["metadata"] = json.dumps(meta)
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_hash_mismatch"):
        await _verify(issued)


async def test_claimed_url_must_match_stored_url(store, page):
    issued = await _issue()
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_claim_mismatch"):
        await _verify(issued, claimed={"artifact_id": issued["artifact_id"], "url": "https://aads.newtalk.kr/other"})


async def test_registered_verify_must_equal_observed_assertions(store, page):
    issued = await _issue()
    with pytest.raises(de.DomEvidenceError, match="dom_evidence_assertion_mismatch"):
        await _verify(issued, verify=[{"assertion": "text_contains", "expected": "다른 문구"}])


# ------------------------------------------------------------------ 초안 등록(단건·일괄)


class _Registry:
    """request_registration / 멱등 조회 / 승인 경로를 메모리로 대체한다."""

    def __init__(self, monkeypatch):
        self.rows: list[dict[str, Any]] = []
        self.requests = 0
        self.cards: list[str] = []
        outer = self

        async def request(recipe, *, tenant_id, requested_by=""):
            outer.requests += 1
            row = {"id": str(uuid.uuid4()), "tenant_id": str(tenant_id), "status": "pending",
                   "name": recipe.name, "domain": recipe.domain, "spec": recipe.to_dict(),
                   "requested_by": requested_by}
            outer.rows.append(row)
            return row

        async def find(*, tenant_id, idempotency_key):
            for row in reversed(outer.rows):
                if row["tenant_id"] == str(tenant_id) and \
                        row["spec"].get("metadata", {}).get("idempotency_key") == idempotency_key:
                    return row
            return None

        async def notify(row):
            return None

        async def card(row, *, chat_session_id):
            outer.cards.append(row["id"])
            return {"status": "proposed"}

        async def forbidden(*a, **k):
            raise AssertionError("등록 경로에서 승인/반려를 호출하면 안 된다")

        monkeypatch.setattr(recorder_module.registration, "request_registration", request)
        monkeypatch.setattr(registration_module, "find_registration_by_idempotency_key", find)
        monkeypatch.setattr(recorder_module.approval_notifier, "notify_pending_created", notify)
        monkeypatch.setattr("app.services.work_recipe.approval_card.propose_card", card)
        monkeypatch.setattr(registration_module, "decide_registration", forbidden)
        monkeypatch.setattr(registration_module, "decide_registration_as_human", forbidden)


@pytest.fixture
def registry(monkeypatch):
    return _Registry(monkeypatch)


def _piece(name="orders_open", **overrides):
    piece = {
        "recipe_name": name, "domain": "aads.newtalk.kr",
        "steps": [{"action": "navigate", "url": URL, "risk": "READ"},
                  {"action": "snapshot", "selector": "body", "risk": "READ"}],
        "verify": VERIFY, "starts_when": "주문 화면 진입 전", "succeeds_when": "주문 목록이 보임",
        "lane": "server", "work_key": "orders-flow",
        "predecessor": {"name": "orders_login", "version": 2},
        "idempotency_key": f"batch-{name}-0001",
        "e2e_evidence": {"screen_verified": True, "screenshot_url": SCREEN},
    }
    piece.update(overrides)
    return piece


async def _batch(pieces, **extra):
    return await dr.register_draft_batch(tenant_id=TENANT, session_id=SESSION, inp={"pieces": pieces, **extra})


async def test_batch_preserves_every_piece_field(registry):
    result = await _batch([_piece("a_piece"), _piece("b_piece")])
    assert result["status"] == "ok" and result["counts"]["registered"] == 2
    assert result["approval_required"] is True
    spec = registry.rows[0]["spec"]
    meta = spec["metadata"]
    assert meta["starts_when"] == "주문 화면 진입 전" and meta["succeeds_when"] == "주문 목록이 보임"
    assert meta["lane"] == "server" and meta["work_key"] == "orders-flow"
    assert meta["predecessor"] == {"name": "orders_login", "version": 2}
    assert meta["idempotency_key"] == "batch-a_piece-0001"
    assert meta["screen_e2e"]["trust"] == "self_reported"
    assert spec["verify"][0]["expected"] == "주문 목록"
    assert [r["status"] for r in result["results"]] == ["registered", "registered"]
    assert result["results"][0]["registration_status"] == "pending"
    assert len(registry.cards) == 2


async def test_batch_partial_failure_does_not_block_other_pieces(registry):
    bad = _piece("bad_piece", e2e_evidence={"artifact_id": str(uuid.uuid4())})
    result = await _batch([_piece("a_piece"), bad, _piece("c_piece", steps=[])])
    assert result["status"] == "partial"
    assert [r["status"] for r in result["results"]] == ["registered", "error", "error"]
    assert result["results"][1]["error"] == "dom_evidence_not_found" or \
        result["results"][1]["error"].startswith("registration_failed")
    assert result["results"][2]["error"] == "recipe_name_domain_and_steps_required"
    assert registry.requests == 1


async def test_batch_recall_creates_no_duplicates_even_after_approval(registry):
    pieces = [_piece("a_piece"), _piece("b_piece")]
    await _batch(pieces)
    again = await _batch(pieces)
    assert again["counts"] == {"registered": 0, "duplicate": 2, "error": 0}
    assert again["approval_required"] is False and registry.requests == 2

    for row in registry.rows:
        row["status"] = "approved"
    after_approval = await _batch(pieces)
    assert after_approval["counts"]["duplicate"] == 2 and registry.requests == 2


async def test_same_key_with_different_content_is_a_conflict(registry):
    await _batch([_piece("a_piece")])
    changed = _piece("a_piece", succeeds_when="다른 성공 조건")
    result = await _batch([changed])
    assert result["results"][0]["error"] == "idempotency_key_conflict"
    assert registry.requests == 1


async def test_batch_without_keys_derives_one_from_content(registry):
    piece = _piece("a_piece")
    piece.pop("idempotency_key")
    first = await _batch([piece])
    second = await _batch([piece])
    assert first["results"][0]["idempotency_key"].startswith("auto:")
    assert second["results"][0]["status"] == "duplicate" and registry.requests == 1


async def test_batch_never_approves(registry):
    result = await _batch([_piece("a_piece")])
    assert result["results"][0]["registration_status"] == "pending"


@pytest.mark.parametrize("pieces,error", [
    (None, "pieces_required"), ([], "pieces_required"), ([_piece()] * 21, "too_many_pieces:max_20"),
])
async def test_batch_input_bounds(registry, pieces, error):
    assert (await _batch(pieces))["error"] == error


async def test_batch_piece_with_bad_predecessor_is_reported_per_piece(registry):
    result = await _batch([_piece("a_piece", predecessor={"name": "x", "version": 0}), _piece("b_piece")])
    assert result["results"][0]["error"] == "invalid_predecessor"
    assert result["results"][1]["status"] == "registered"


async def test_register_with_valid_artifact_records_server_issued_trust(store, page, registry):
    issued = await _issue()
    piece = _piece("a_piece", e2e_evidence={"artifact_id": issued["artifact_id"]})
    outcome = await dr.register_generic_draft(tenant_id=TENANT, session_id=SESSION, inp=piece)
    assert outcome["status"] == "registered"
    screen = registry.rows[0]["spec"]["metadata"]["screen_e2e"]
    assert screen["trust"] == "server_issued" and screen["artifact_id"] == issued["artifact_id"]
    assert screen["binding_hash"] == issued["binding_hash"]


@pytest.mark.parametrize("session,tenant,error", [
    (OTHER_SESSION, TENANT, "dom_evidence_session_mismatch"),
    (SESSION, OTHER_TENANT, "dom_evidence_not_found"),
])
async def test_register_refuses_foreign_artifact(store, page, registry, session, tenant, error):
    issued = await _issue()
    piece = _piece("a_piece", e2e_evidence={"artifact_id": issued["artifact_id"]})
    outcome = await dr.register_generic_draft(tenant_id=tenant, session_id=session, inp=piece)
    assert outcome == {"error": error}
    assert registry.requests == 0


async def test_artifact_id_never_falls_back_to_self_reported(store, page, registry):
    piece = _piece("a_piece", e2e_evidence={
        "artifact_id": str(uuid.uuid4()), "screen_verified": True, "screenshot_url": SCREEN})
    outcome = await dr.register_generic_draft(tenant_id=TENANT, session_id=SESSION, inp=piece)
    assert outcome["error"] == "dom_evidence_not_found" and registry.requests == 0


async def test_self_reported_evidence_is_labelled_and_drops_raw_fields(registry):
    piece = _piece("a_piece", e2e_evidence={
        "screen_verified": True, "screenshot_url": SCREEN + "?token=abc123", "raw_dom": "<html>pw</html>"})
    await dr.register_generic_draft(tenant_id=TENANT, session_id=SESSION, inp=piece)
    screen = registry.rows[0]["spec"]["metadata"]["screen_e2e"]
    assert screen["trust"] == "self_reported" and "raw_dom" not in screen
    assert "abc123" not in json.dumps(screen)


async def test_summary_exposes_evidence_trust():
    row = {"spec": {"metadata": {"screen_e2e": {"trust": "server_issued", "url": URL}}}, "dry_run": {}}
    assert registration_module.registration_summary(row)["evidence_trust"] == "server_issued"
    assert registration_module.registration_summary({"spec": {}, "dry_run": {}})["evidence_trust"] == "none"


# ------------------------------------------------------------------ upload 변환


def test_upload_file_path_converts_to_value_and_is_not_duplicated():
    one = RecipeStep.from_dict({"action": "upload", "selector": "input", "file_path": "/tmp/a.txt"}, seq=1)
    many = RecipeStep.from_dict({"action": "upload", "selector": "input", "file_paths": ["/a", "/b"]}, seq=1)
    assert one.value == "/tmp/a.txt" and "file_path" not in one.extra
    assert many.value == ["/a", "/b"] and "file_paths" not in many.extra
    same = RecipeStep.from_dict(
        {"action": "upload", "selector": "i", "value": "/tmp/a.txt", "file_path": "/tmp/a.txt"}, seq=1)
    assert same.value == "/tmp/a.txt"
    with pytest.raises(ValueError):
        RecipeStep.from_dict({"action": "upload", "selector": "i", "value": "/x", "file_path": "/y"}, seq=1)


# ------------------------------------------------------------------ smart_browser 도구 경로

from app.services import tool_executor as tool_executor_module  # noqa: E402
from app.services.tool_executor import ToolExecutor  # noqa: E402


@pytest.fixture
def tool_env(monkeypatch, registry):
    async def tenant(**kwargs):
        return TENANT

    async def card(row, *, chat_session_id):
        return {"status": "proposed"}

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    monkeypatch.setattr("app.services.work_recipe.approval_card.propose_card", card)
    return registry


async def _call(inp, session=SESSION):
    token = tool_executor_module.current_chat_session_id.set(session)
    try:
        return await ToolExecutor()._smart_browser(inp)
    finally:
        tool_executor_module.current_chat_session_id.reset(token)


async def test_tool_capture_then_register_uses_raw_input_not_summary(store, page, tool_env):
    captured = await _call({"action": "capture_evidence", "url": URL, "verify": VERIFY})
    assert captured["status"] == "evidence_issued"
    evidence = {"artifact_id": captured["e2e_evidence"]["artifact_id"]}
    long_url = URL + "?" + "q=" + "x" * 1500
    steps = [{"action": "navigate", "url": long_url, "risk": "READ"},
             {"action": "upload", "selector": "input[type=file]", "file_paths": ["/tmp/a.csv", "/tmp/b.csv"],
              "risk": "WRITE_INTERNAL"}]
    result = await _call({"action": "register_e2e", "recipe_name": "upload_flow", "domain": "aads.newtalk.kr",
                          "steps": steps, "verify": VERIFY, "e2e_evidence": evidence})
    assert result["status"] == "approval_required"
    saved = tool_env.rows[0]["spec"]["steps"]
    assert saved[0]["url"] == long_url
    assert saved[1]["value"] == ["/tmp/a.csv", "/tmp/b.csv"]
    assert tool_env.rows[0]["spec"]["metadata"]["screen_e2e"]["trust"] == "server_issued"


async def test_tool_register_batch_dispatch_and_idempotency(tool_env):
    first = await _call({"action": "register_batch", "pieces": [_piece("a_piece"), _piece("b_piece")]})
    again = await _call({"action": "register_batch", "pieces": [_piece("a_piece"), _piece("b_piece")]})
    assert first["counts"]["registered"] == 2 and again["counts"]["duplicate"] == 2
    assert tool_env.requests == 2


async def test_tool_capture_evidence_failure_returns_code(store, page, tool_env):
    page["page"] = _Page(contains=False)
    result = await _call({"action": "capture_evidence", "url": URL, "verify": VERIFY})
    assert result["error"].startswith("dom_evidence_verify_failed")


async def test_tool_unsupported_action_lists_new_actions(tool_env):
    result = await _call({"action": "nope"})
    assert {"register_batch", "capture_evidence"} <= set(result["allowed"])


def test_tool_registry_exposes_new_actions_and_fields():
    from app.services.tool_registry import ToolRegistry

    tool = ToolRegistry().get_tool("smart_browser")
    props = tool["input_schema"]["properties"]
    assert {"register_batch", "capture_evidence"} <= set(props["action"]["enum"])
    assert {"pieces", "idempotency_key", "predecessor", "work_key", "url", "run_id"} <= set(props)


async def test_explicit_server_lane_issues_public_site_evidence(store, page, monkeypatch):
    import asyncio
    import socket
    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    page['page'] = _Page('https://example.com/orders')
    issued = await _issue(url='https://example.com/orders', lane='server')
    trusted = await _verify(issued, domain='example.com')
    assert trusted['lane'] == 'server'
    with pytest.raises(de.DomEvidenceError, match='lane_conflict'):
        await _issue(lane='server', browser_session_id=SESSION)


async def test_explicit_server_lane_rejects_metadata_ssrf(monkeypatch):
    import asyncio
    import socket
    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))])
    with pytest.raises(de.DomEvidenceError, match="private_target_rejected"):
        await _issue(url="http://169.254.169.254/latest/meta-data", lane="server")


@pytest.mark.parametrize('lane', ['server', ''])
@pytest.mark.parametrize('destination', ['http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/'])
async def test_server_redirect_private_url_rejected_before_artifact(store, page, monkeypatch, destination, lane):
    import socket
    import asyncio
    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    calls = []
    async def acquire(self, payload):
        calls.append(payload['url'])
        return _Page(destination)
    def resolve(host, *args, **kwargs):
        address = host if host[0].isdigit() else '8.8.8.8'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(executor_module.BrowserRecipeExecutor, '_get_page', acquire)
    with pytest.raises(de.DomEvidenceError, match='private_target_rejected'):
        await _issue(lane=lane)
    assert calls == [URL] and not store.rows


async def test_capture_preserves_subsecond_wait_ordering(store, page):
    before = datetime.now(timezone.utc)
    issued = await _issue()
    assert datetime.fromisoformat(issued['captured_at']) > before
    trusted = await _verify(issued)
    assert trusted['captured_at'] == issued['captured_at']
