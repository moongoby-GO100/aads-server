"""채팅 도구 mockup_review_submit 계약 테스트.

고정하는 계약
  1. 도구가 registry·executor dispatch·action 그룹에 있고 일관성 스크립트를 통과한다.
  2. approve·revoke 는 action 으로도, 코드 경로로도 없다.
  3. 권한(세션 사용자·멤버십·project_document_grants) 없으면 서비스 함수까지 가지 않는다.
  4. 면제 doc_ref 는 사유 10자 이상 + 정책 참조가 있어야 하며 DB 접근 전에 거절한다.
  5. create → revision → submit 이 라우트와 같은 서비스 함수를 같은 트랜잭션에서 부른다.
  6. 서버 검증 오류(code·서버 값)를 그대로 돌려준다.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services import mockup_review_service as svc
from app.services import mockup_review_tools as mrt

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "11111111-1111-1111-1111-111111111111"
REVIEW = "22222222-2222-2222-2222-222222222222"
REVISION = "33333333-3333-3333-3333-333333333333"
_REPO = Path(__file__).resolve().parents[2]
_MANIFEST = "docs/design/mockups/20261015_goal_strip_dormancy/manifest.json"
EXEMPT = {"role": "plan", "exempt_reason": "정본 plan 이 아직 없어 면제합니다", "exempt_policy_ref": "CEO-DIRECTIVES#R-DOC"}


class FakeConn:
    def __init__(self, *, user_id="user-1", member=True, role="member", granted=True, generation=4, latest=REVISION):
        self.user_id, self.member, self.role, self.granted = user_id, member, role, granted
        self.generation, self.latest = generation, latest
        self.sql: list[str] = []
        self.in_tx = False

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                conn.in_tx = True

            async def __aexit__(self_inner, *exc):
                conn.in_tx = False
                return False

        return _Tx()

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        if "FROM chat_sessions" in sql:
            return self.user_id
        if "project_document_grants" in sql:
            return self.granted
        return None

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "FROM tenant_memberships" in sql:
            return {"kind": "customer", "role": self.role, "user_email": "u@example.com", "user_role": "member"} \
                if self.member else None
        if "FROM mockup_review_heads" in sql:
            return {"generation": self.generation, "latest_revision_id": uuid.UUID(self.latest) if self.latest else None}
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


@pytest.fixture
def services(monkeypatch):
    mocks = {
        "create_review": AsyncMock(return_value={"review_id": REVIEW, "status": "draft", "generation": 0, "idempotent": False}),
        "create_revision": AsyncMock(return_value={
            "review_id": REVIEW, "revision_id": REVISION, "manifest_hash": "h" * 64, "status": "draft",
            "generation": 5, "pointer_applied": True, "idempotent": False}),
        "submit_review": AsyncMock(return_value={
            "review_id": REVIEW, "revision_id": REVISION, "manifest_hash": "h" * 64, "status": "review_ready",
            "generation": 6, "approved": False, "idempotent": False}),
        "get_review": AsyncMock(return_value={"review_id": REVIEW, "status": "draft", "revisions": list(range(30)), "events": []}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(svc, name, mock)
    return mocks


def run(conn, **inp):
    inp.setdefault("project", "aads")
    return asyncio.run(mrt.run_action(tenant_id=TENANT, session_id=SESSION, inp=inp, pool=FakePool(conn)))


def _writes(conn):
    return [s for s in conn.sql if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


# ── 1. 등록 ────────────────────────────────────────────────────────────


def test_tool_registered_in_registry_executor_and_group():
    from app.services.tool_executor import _DATABASE_TOOLS, ToolExecutor
    from app.services.tool_registry import _GROUPS, ToolRegistry

    schema = ToolRegistry().get_tool("mockup_review_submit")["input_schema"]
    assert "mockup_review_submit" in _GROUPS["action"] and "mockup_review_submit" in _GROUPS["all"]
    assert "mockup_review_submit" in _DATABASE_TOOLS
    assert hasattr(ToolExecutor, "_mockup_review_submit")
    assert '"mockup_review_submit":' in inspect.getsource(ToolExecutor._dispatch)
    assert set(schema["required"]) == {"action", "project"}
    assert "tenant_id" not in schema["properties"] and "session_id" not in schema["required"]
    assert set(schema["properties"]["action"]["enum"]) == set(mrt.ACTIONS) == {"create", "revision", "submit", "status"}


def test_tool_consistency_script_passes():
    res = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_tool_consistency.py")],
                         capture_output=True, text=True, cwd=_REPO)
    assert res.returncode == 0, res.stdout + res.stderr


# ── 2. approve 경로 없음 ───────────────────────────────────────────────


@pytest.mark.parametrize("action", ["approve", "revoke", "approved", "verify", "", None])
def test_approve_and_revoke_actions_are_rejected_before_any_query(action, services):
    conn = FakeConn()
    out = run(conn, action=action, review_id=REVIEW)
    assert out["error"] == "invalid_input"
    assert conn.sql == []
    assert not any(mock.await_count for mock in services.values())


def test_no_approve_tool_or_input_exposed_and_sources_do_not_reference_approve_paths():
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry()
    assert not registry.get_tool("mockup_review_approve") and not registry.get_tool("mockup_review_revoke")
    props = registry.get_tool("mockup_review_submit")["input_schema"]["properties"]
    assert not any("approv" in p or "revoke" in p for p in props)

    src = (_REPO / "app/services/mockup_review_tools.py").read_text(encoding="utf-8")
    for needle in ("approve_review", "revoke_review", "/approve", "/revoke", "ApproveReview", "RevokeReview", '"approve"'):
        assert needle not in src
    executor = inspect.getsource(__import__("app.services.tool_executor", fromlist=["ToolExecutor"]).ToolExecutor)
    body = executor.split("async def _mockup_review_submit")[1].split("    async def ")[0]
    assert "approve_review" not in body and "revoke_review" not in body


# ── 3. 권한 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("conn_kwargs, action", [
    ({"user_id": ""}, "create"),
    ({"user_id": None}, "submit"),
    ({"member": False}, "create"),
    ({"granted": False}, "create"),
    ({"granted": False}, "revision"),
    ({"granted": False}, "submit"),
    ({"granted": False}, "status"),
    ({"role": "viewer"}, "create"),
])
def test_unauthorized_callers_are_denied_without_touching_the_service(conn_kwargs, action, services):
    conn = FakeConn(**conn_kwargs)
    out = run(conn, action=action, review_id=REVIEW, title="t", change_type="new",
              manifest={"screens": []}, doc_refs=[EXEMPT])
    assert out["error"] == "project_access_denied" and out["status"] == 403
    assert not any(mock.await_count for mock in services.values())
    assert _writes(conn) == []


def test_viewer_may_read_status_with_read_grant(services):
    out = run(FakeConn(role="viewer"), action="status", review_id=REVIEW)
    assert out["review_id"] == REVIEW
    services["get_review"].assert_awaited_once()


def test_elevated_role_bypasses_grant_like_the_route(services):
    out = run(FakeConn(role="admin", granted=False), action="create", title="t", change_type="new")
    assert out["review_id"] == REVIEW


def test_invalid_session_id_is_denied(services):
    out = asyncio.run(mrt.run_action(tenant_id=TENANT, session_id="not-a-uuid",
                                     inp={"action": "status", "project": "AADS", "review_id": REVIEW},
                                     pool=FakePool(FakeConn())))
    assert out["error"] == "project_access_denied"
    assert not services["get_review"].await_count


# ── 4. 면제 doc_ref 검증 ───────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    {**EXEMPT, "exempt_reason": "짧은사유"},
    {**EXEMPT, "exempt_reason": "   열자가안되는사유   "},
    {**EXEMPT, "exempt_policy_ref": ""},
    {"role": "plan", "exempt_reason": "정본 plan 이 아직 없어 면제합니다"},
    {**EXEMPT, "document_key": "some-doc"},
    {"role": "prd", "document_key": "x-prd"},
    {"role": "other", **{k: v for k, v in EXEMPT.items() if k != "role"}},
])
def test_bad_doc_refs_rejected_before_any_query(bad, services):
    conn = FakeConn()
    out = run(conn, action="revision", review_id=REVIEW, manifest={"screens": []}, doc_refs=[bad])
    assert out["error"] == "invalid_input" and "doc_refs" in out["message"]
    assert conn.sql == []
    assert not services["create_revision"].await_count


def test_exempt_doc_ref_with_10_char_reason_is_accepted(services):
    out = run(FakeConn(), action="revision", review_id=REVIEW, manifest={"screens": []},
              doc_refs=[{**EXEMPT, "exempt_reason": "가" * 10}])
    assert out["revision_id"] == REVISION
    body = services["create_revision"].await_args.args[5]
    assert body.doc_refs[0].exempt_reason == "가" * 10


# ── 5. 정상 흐름 ────────────────────────────────────────────────────────


def test_create_revision_submit_flow_uses_route_service_functions_in_one_transaction(services, monkeypatch):
    created = run(FakeConn(), action="create", title="목표 띠 목업", change_type="modify")
    assert created["review_id"] == REVIEW and created["approved"] is False
    args = services["create_review"].await_args.args
    assert args[1] == TENANT and args[2] == "AADS" and args[3] == "user-1" and args[4] is False
    assert args[5].title == "목표 띠 목업" and args[5].change_type == "modify"
    assert args[5].idempotency_key.startswith("mrt-")

    conn = FakeConn()
    seen_in_tx = []

    async def in_tx(*a, **kw):
        seen_in_tx.append(conn.in_tx)
        return services["create_revision"].return_value

    monkeypatch.setattr(svc, "create_revision", AsyncMock(side_effect=in_tx))
    revised = run(conn, action="revision", review_id=REVIEW, manifest_path=_MANIFEST, doc_refs=[EXEMPT])
    assert revised["review_id"] == REVIEW and revised["revision_id"] == REVISION
    assert revised["manifest_hash"] == "h" * 64 and revised["status"] == "draft"
    assert revised["approved"] is False and revised["revision_url"].endswith(f"/revisions/{REVISION}")
    assert seen_in_tx == [True]

    submitted = run(FakeConn(), action="submit", review_id=REVIEW)
    assert submitted["status"] == "review_ready" and submitted["approved"] is False
    sub = services["submit_review"].await_args.args[5]
    assert str(sub.revision_id) == REVISION and sub.expected_generation == 4


def test_repo_manifest_passes_server_build_manifest_without_duplicate_asset_uri():
    from app.models.mockup_review import RevisionCreate

    raw = json.loads((_REPO / _MANIFEST).read_text(encoding="utf-8"))
    uris = [a["uri"] for a in raw["revision"]["manifest"]["assets"]]
    assert len(uris) == len(set(uris))
    body = RevisionCreate.model_validate({
        "idempotency_key": "regression-dup-uri-0001", "expected_generation": 0,
        "manifest": raw["revision"]["manifest"],
    })
    built = asyncio.run(svc.build_manifest(body, "modify"))
    assert len(built["assets"]) == len(uris)


def test_revision_loads_repo_manifest_file_and_defaults_generation_and_key(services):
    run(FakeConn(generation=7), action="revision", review_id=REVIEW, manifest_path=_MANIFEST)
    body = services["create_revision"].await_args.args[5]
    assert body.expected_generation == 7
    assert {s.screen_id for s in body.manifest.screens} and body.manifest.assets
    again = run(FakeConn(generation=7), action="revision", review_id=REVIEW, manifest_path=_MANIFEST)
    assert services["create_revision"].await_args.args[5].idempotency_key == body.idempotency_key
    assert again["revision_id"] == REVISION
    run(FakeConn(), action="revision", review_id=REVIEW, manifest_path=_MANIFEST, idempotency_key="my-key-0001",
        expected_generation=2)
    explicit = services["create_revision"].await_args.args[5]
    assert explicit.idempotency_key == "my-key-0001" and explicit.expected_generation == 2


@pytest.mark.parametrize("inp", [
    {}, {"manifest_path": _MANIFEST, "manifest": {"screens": []}}, {"manifest_path": "reports/x.json"},
    {"manifest_path": "docs/../etc/passwd"}, {"manifest_path": "docs/design/mockups/nope/manifest.json"},
    {"manifest": "{not json"}, {"manifest": {"screens": [{"screen_id": "!!"}]}},
])
def test_revision_manifest_input_errors(inp, services):
    out = run(FakeConn(), action="revision", review_id=REVIEW, **inp)
    assert out["error"] == "invalid_input"
    assert not services["create_revision"].await_count


def test_submit_with_doc_refs_must_match_the_revision(services, monkeypatch):
    stored = [{"role": "plan", "exempt_reason": EXEMPT["exempt_reason"], "exempt_policy_ref": EXEMPT["exempt_policy_ref"]}]
    conn = FakeConn()
    original = conn.fetchval

    async def fetchval(sql, *args):
        if "SELECT doc_refs FROM mockup_review_revisions" in sql:
            return stored
        return await original(sql, *args)

    conn.fetchval = fetchval
    ok = run(conn, action="submit", review_id=REVIEW, doc_refs=[EXEMPT])
    assert ok["status"] == "review_ready"

    other = run(conn, action="submit", review_id=REVIEW,
                doc_refs=[{**EXEMPT, "exempt_policy_ref": "다른-정책"}])
    assert other["error"] == "doc_refs_differ_from_revision" and other["status"] == 422
    assert services["submit_review"].await_count == 1


def test_submit_without_any_revision_is_invalid(services):
    out = run(FakeConn(latest=None), action="submit", review_id=REVIEW)
    assert out["error"] == "invalid_input"
    assert not services["submit_review"].await_count


def test_status_trims_history(services):
    out = run(FakeConn(), action="status", review_id=REVIEW)
    assert len(out["revisions"]) == 10


def test_unknown_review_is_reported(services):
    conn = FakeConn()

    async def missing(sql, *args):
        conn.sql.append(sql)
        if "FROM tenant_memberships" in sql:
            return {"kind": "customer", "role": "member", "user_email": "u@example.com", "user_role": "member"}
        return None

    conn.fetchrow = missing
    out = run(conn, action="submit", review_id=REVIEW)
    assert out["error"] == "review_not_found" and out["status"] == 404


# ── 6. 서버 오류 전달 ───────────────────────────────────────────────────


def test_server_validation_errors_are_returned_verbatim(services):
    services["create_revision"].side_effect = HTTPException(
        422, {"code": "manifest_hash_mismatch", "server_manifest_hash": "a" * 64})
    out = run(FakeConn(), action="revision", review_id=REVIEW, manifest={"screens": []})
    assert out["error"] == "manifest_hash_mismatch" and out["status"] == 422
    assert out["detail"]["server_manifest_hash"] == "a" * 64

    services["submit_review"].side_effect = HTTPException(
        422, {"code": "missing_artifacts", "missing": [{"code": "doc_ref_missing", "role": "prd"}]})
    out = run(FakeConn(), action="submit", review_id=REVIEW)
    assert out["error"] == "missing_artifacts" and out["detail"]["missing"][0]["role"] == "prd"


# ── executor 핸들러 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_handler_requires_tenant_and_delegates(monkeypatch):
    from app.services import tool_executor as te

    token_s, token_t = te.current_chat_session_id.set(""), te.current_tenant_id.set("")
    monkeypatch.setattr("app.services.agent_sdk_service.get_active_chat_session_id", lambda: "")
    try:
        res = await te.ToolExecutor()._mockup_review_submit({"project": "AADS", "action": "status"})
        assert res["error"] == "missing_tenant_id"

        te.current_tenant_id.set(TENANT)
        te.current_chat_session_id.set(SESSION)
        run_action = AsyncMock(return_value={"status": "draft"})
        monkeypatch.setattr(mrt, "run_action", run_action)
        assert (await te.ToolExecutor()._mockup_review_submit({"project": "AADS", "action": "status"}))["status"] == "draft"
        assert run_action.await_args.kwargs["tenant_id"] == TENANT
        assert run_action.await_args.kwargs["session_id"] == SESSION
    finally:
        te.current_tenant_id.reset(token_t)
        te.current_chat_session_id.reset(token_s)
