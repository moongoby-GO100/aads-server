"""채팅 도구 canonical_document_register / canonical_document_lookup 계약 테스트.

고정하는 계약
  1. 두 도구가 registry·executor dispatch·action 그룹에 있고 일관성 스크립트를 통과한다.
  2. 필수 인자/키 형식(소문자 kebab, 슬래시·날짜·버전 금지)을 DB 접근 전에 거부한다.
  3. register 는 라우트와 같은 create_revision_in_tx 와 canonical_gate 를 호출한다.
  4. approve 는 어떤 경로로도 호출되지 않고 approved_revision_id 는 갱신되지 않는다.
  5. lookup 은 SELECT 만 한다.
  6. 게이트 mode=off 이면 chat_tool 진입점도 쿼리 0건, 오류는 fail-open.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from app.services import canonical_document_tools as cdt
from app.services import canonical_gate as cg

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "11111111-1111-1111-1111-111111111111"
HEAD_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
REV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_REPO = Path(__file__).resolve().parents[2]
_REAL_CHECK_CHAT_REGISTER = cg.check_chat_register


class FakeConn:
    def __init__(self, head=None, revisions=0, rows=None):
        self.head = head
        self.revisions = revisions
        self.rows = rows or []
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

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "FROM project_document_heads" in sql and "document_key=$3" in sql:
            return self.head
        if "INSERT INTO project_document_revisions" in sql:
            return {"id": REV_ID}
        if "FROM project_document_revisions" in sql:
            return {"id": REV_ID, "revision": 1, "version": "1.0.0", "title": "t", "content_hash": "h",
                    "source_path": None, "change_summary": None, "created_at": "2026-10-03"}
        return None

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        if "max(revision)" in sql:
            return self.revisions
        if "SELECT action FROM project_document_events" in sql:
            return None
        return 1

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return self.rows

    async def execute(self, sql, *args):
        self.sql.append(sql)


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.acquires = 0

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_inner):
                pool.acquires += 1
                return pool.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


def _head(generation=0, approved=None):
    return {"id": HEAD_ID, "document_key": "ovis-spec", "kind": "spec", "title": "t",
            "generation": generation, "latest_revision_id": REV_ID if generation else None,
            "approved_revision_id": approved}


def _inp(**over):
    base = {"project": "aads", "document_key": "ovis-spec", "kind": "spec", "title": "오비스 명세",
            "content": "# 본문\n내용"}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _stub_gate(monkeypatch):
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(cg, "check_chat_register", gate)
    return gate


def _register(conn, inp, **kw):
    return asyncio.run(cdt.register_document(tenant_id=TENANT, session_id=SESSION, inp=inp, pool=FakePool(conn), **kw))


# ── 1. 등록 ────────────────────────────────────────────────────────────


def test_tools_registered_in_registry_executor_and_group():
    from app.services.tool_executor import ToolExecutor
    from app.services.tool_registry import _GROUPS, ToolRegistry

    registry = ToolRegistry()
    for name in ("canonical_document_register", "canonical_document_lookup"):
        schema = registry.get_tool(name)["input_schema"]
        assert name in _GROUPS["action"] and name in _GROUPS["all"]
        assert "tenant_id" not in schema["properties"]
        assert "session_id" not in schema.get("required", [])
        assert hasattr(ToolExecutor, f"_{name}")
        assert f'"{name}":' in inspect.getsource(ToolExecutor._dispatch)
    reg = registry.get_tool("canonical_document_register")["input_schema"]
    assert set(reg["required"]) == {"project", "document_key", "kind", "title"}
    assert set(reg["properties"]["kind"]["enum"]) == set(docs.RevisionInput.model_fields["kind"].annotation.__args__)
    assert registry.get_tool("canonical_document_lookup")["input_schema"]["required"] == ["project"]


def test_no_approve_tool_or_input_exposed():
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry()
    assert not registry.get_tool("canonical_document_approve")
    props = registry.get_tool("canonical_document_register")["input_schema"]["properties"]
    assert not any("approv" in p for p in props)


def test_tool_consistency_script_passes():
    import subprocess
    import sys

    res = subprocess.run([sys.executable, str(_REPO / "scripts" / "check_tool_consistency.py")],
                         capture_output=True, text=True, cwd=_REPO)
    assert res.returncode == 0, res.stdout + res.stderr


# ── 2. 입력 검증 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("key", [
    "docs/ovis-spec", "Ovis-Spec", "ovis_spec", "ovis-spec-2026-10-03", "ovis-spec-20261003",
    "ovis-spec-202610", "ovis-spec-v2", "v1-ovis", "ovis spec", "-ovis", "ovis-", "ovis--spec", "",
    "ovis.spec", "a" * 129,
])
def test_bad_document_keys_rejected_before_any_query(key):
    conn = FakeConn()
    out = _register(conn, _inp(document_key=key))
    assert out["error"] == "invalid_input"
    assert conn.sql == []


@pytest.mark.parametrize("key", ["ovis-spec", "prd", "phase-2-plan", "go100-recipe-design", "kis-order-v-model"])
def test_good_document_keys_accepted(key):
    assert cdt.validate_document_key(key) is None


@pytest.mark.parametrize("over", [
    {"project": ""}, {"project": "bad project"}, {"content": "", "file_path": ""},
    {"kind": "diary"}, {"kind": None}, {"title": ""}, {"goal_id": "not-a-uuid"}, {"version": "v1"},
])
def test_required_and_invalid_args_rejected(over):
    conn = FakeConn()
    out = _register(conn, _inp(**over))
    assert out["error"] == "invalid_input"
    assert not any("INSERT INTO project_document_revisions" in s for s in conn.sql)


# ── 3. 생성 서비스·게이트 호출 ────────────────────────────────────────


def test_register_calls_shared_service_and_gate(monkeypatch, _stub_gate):
    created = AsyncMock(return_value={"document_id": HEAD_ID, "revision_id": REV_ID, "generation": 1, "idempotent": False})
    monkeypatch.setattr(docs, "create_revision_in_tx", created)
    conn = FakeConn(head=None)
    out = _register(conn, _inp(change_summary="최초"))

    created.assert_awaited_once()
    args = created.await_args.args
    assert args[0] is conn and args[1] == TENANT and args[2] == f"chat:{SESSION[:8]}" and args[3] == "AADS"
    body = args[4]
    assert isinstance(body, docs.RevisionInput)
    assert (body.document_key, body.kind, body.version, body.expected_generation) == ("ovis-spec", "spec", "1.0.0", 0)
    assert body.change_summary == "최초"
    assert conn.in_tx is False
    _stub_gate.assert_awaited_once()
    kwargs = _stub_gate.await_args.kwargs
    assert (kwargs["tenant_id"], kwargs["project"], kwargs["document_key"]) == (TENANT, "AADS", "ovis-spec")
    assert out["registered"] is True and out["status"] == "draft" and out["approved"] is False
    assert out["revision_id"] == str(REV_ID)


def test_existing_key_creates_next_revision_with_current_generation(monkeypatch):
    created = AsyncMock(return_value={"document_id": HEAD_ID, "revision_id": REV_ID, "generation": 4, "idempotent": False})
    monkeypatch.setattr(docs, "create_revision_in_tx", created)
    conn = FakeConn(head=_head(generation=3, approved=REV_ID), revisions=2)
    _register(conn, _inp())
    body = created.await_args.args[4]
    assert (body.version, body.expected_generation) == ("1.0.2", 3)
    assert any("FOR UPDATE" in s for s in conn.sql)


def test_gate_warning_is_attached_but_never_blocks(monkeypatch, _stub_gate):
    monkeypatch.setattr(docs, "create_revision_in_tx", AsyncMock(
        return_value={"document_id": HEAD_ID, "revision_id": REV_ID, "generation": 1, "idempotent": False}))
    _stub_gate.return_value = {"verdict": "missing_canonical"}
    out = _register(FakeConn(), _inp())
    assert out["registered"] is True and out["canonical_gate"]["verdict"] == "missing_canonical"


def test_service_http_errors_are_returned_not_raised(monkeypatch):
    monkeypatch.setattr(docs, "create_revision_in_tx", AsyncMock(side_effect=HTTPException(409, "generation_conflict")))
    out = _register(FakeConn(), _inp())
    assert out["error"] == "generation_conflict" and out["status"] == 409
    assert "registered" not in out


# ── 4. approve 미호출 ──────────────────────────────────────────────────


def test_register_never_calls_approve_and_leaves_approved_revision_untouched(monkeypatch):
    approve = AsyncMock(side_effect=AssertionError("approve must not be called"))
    monkeypatch.setattr(docs, "approve_document", approve)
    conn = FakeConn(head=_head(generation=2, approved=REV_ID), revisions=1)
    out = _register(conn, _inp(content="새 본문"))

    assert out["registered"] is True and out["approved"] is False
    approve.assert_not_awaited()
    writes = [s for s in conn.sql if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes, "실제 create_revision_in_tx 가 돌았어야 한다"
    assert not any("approved_revision_id" in s for s in writes)
    assert not any("'approved'" in s or "'archived'" in s for s in writes)


def test_tool_sources_do_not_reference_approve_paths():
    for path in ("app/services/canonical_document_tools.py",):
        src = (_REPO / path).read_text(encoding="utf-8")
        assert "approve_document(" not in src and "/approve\"" not in src
    executor = inspect.getsource(__import__("app.services.tool_executor", fromlist=["ToolExecutor"]).ToolExecutor)
    for fn in ("_canonical_document_register", "_canonical_document_lookup"):
        body = executor.split(f"async def {fn}")[1].split("    async def ")[0]
        assert "approve" not in body.lower().replace("never approves", "")


def test_router_create_route_still_first_and_delegates_to_shared_function():
    paths = [(r.path, sorted(r.methods)) for r in docs.router.routes]
    assert paths[0] == ("/projects/{project_key}/documents", ["POST"])
    assert paths[1] == ("/projects/{project_key}/documents", ["GET"])
    assert "create_revision_in_tx" in inspect.getsource(docs.create_revision)


# ── 5. lookup 읽기 전용 ───────────────────────────────────────────────


def _lookup(conn, inp):
    return asyncio.run(cdt.lookup_documents(tenant_id=TENANT, inp=inp, pool=FakePool(conn)))


def test_lookup_by_key_returns_latest_approved_generation_read_only():
    conn = FakeConn(head=_head(generation=3, approved=REV_ID))
    out = _lookup(conn, {"project": "aads", "document_key": "ovis-spec"})
    assert out["registered"] is True and out["generation"] == 3 and out["approved"] is True
    assert out["latest_revision"]["id"] == str(REV_ID) and out["approved_revision"]["status"] == "draft"
    assert conn.sql and all(s.lstrip().upper().startswith("SELECT") for s in conn.sql)
    assert conn.in_tx is False


def test_lookup_unregistered_key():
    out = _lookup(FakeConn(head=None), {"project": "AADS", "document_key": "nope"})
    assert out == {"project": "AADS", "document_key": "nope", "registered": False}


def test_lookup_query_and_path_are_select_only():
    for inp in ({"project": "AADS", "query": "오비스"}, {"project": "AADS", "file_path": "docs/plans/a.md"}):
        conn = FakeConn(rows=[_head(generation=1)])
        out = _lookup(conn, inp)
        assert out["registered"] is True and out["total"] == 1
        assert all(s.lstrip().upper().startswith("SELECT") for s in conn.sql)


def test_lookup_requires_a_selector_and_valid_project():
    conn = FakeConn()
    assert _lookup(conn, {"project": "AADS"})["error"] == "invalid_input"
    assert _lookup(conn, {"project": "x y", "query": "a"})["error"] == "invalid_input"
    assert conn.sql == []


# ── executor 핸들러: 테넌트 바인딩 ────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_handlers_require_tenant_and_delegate(monkeypatch):
    from app.services import tool_executor as te

    token_s, token_t = te.current_chat_session_id.set(""), te.current_tenant_id.set("")
    monkeypatch.setattr("app.services.agent_sdk_service.get_active_chat_session_id", lambda: "")
    try:
        for name in ("_canonical_document_register", "_canonical_document_lookup"):
            res = await getattr(te.ToolExecutor(), name)({"project": "AADS"})
            assert res["error"] == "missing_tenant_id"

        te.current_tenant_id.set(TENANT)
        reg = AsyncMock(return_value={"registered": True})
        look = AsyncMock(return_value={"registered": False})
        monkeypatch.setattr(cdt, "register_document", reg)
        monkeypatch.setattr(cdt, "lookup_documents", look)
        assert (await te.ToolExecutor()._canonical_document_register({"project": "AADS"}))["registered"] is True
        assert reg.await_args.kwargs["tenant_id"] == TENANT
        assert (await te.ToolExecutor()._canonical_document_lookup({"project": "AADS"}))["registered"] is False
        assert look.await_args.kwargs["tenant_id"] == TENANT
    finally:
        te.current_tenant_id.reset(token_t)
        te.current_chat_session_id.reset(token_s)


# ── 6. canonical_gate 4번째 진입점 ───────────────────────────────────


class GatePool:
    def __init__(self, row, fail=False):
        self.row, self.fail, self.acquires, self.events = row, fail, 0, []

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_inner):
                pool.acquires += 1
                if pool.fail:
                    raise RuntimeError("db down")

                class _C:
                    async def fetchrow(_s, sql, *a):
                        return pool.row

                    async def execute(_s, sql, *a):
                        pool.events.append(a)

                return _C()

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


def _gate(pool):
    return asyncio.run(_REAL_CHECK_CHAT_REGISTER(
        tenant_id=TENANT, project="AADS", document_key="k", source_path=None, ref="AADS/k", pool=pool))


def test_gate_chat_register_ok_records_without_warning():
    pool = GatePool({"key_match": True, "path_match": False})
    assert _gate(pool) is None
    assert pool.events and pool.events[0][:4] == ("chat_tool", "AADS", "AADS/k", "ok")


def test_gate_chat_register_missing_warns_and_never_raises():
    assert _gate(GatePool({"key_match": False, "path_match": False}))["entrypoint"] == "chat_tool"
    assert _gate(GatePool(None, fail=True)) is None


def test_gate_off_mode_makes_zero_queries(monkeypatch):
    monkeypatch.setenv("CANONICAL_GATE_MODE", "off")
    pool = GatePool({"key_match": False, "path_match": False})
    assert _gate(pool) is None
    assert pool.acquires == 0


def test_chat_tool_entrypoint_has_migration_and_rollback():
    mig = (_REPO / "migrations" / "20261003_canonical_gate_events_chat_tool.sql").read_text(encoding="utf-8")
    down = (_REPO / "migrations" / "rollback" / "20261003_canonical_gate_events_chat_tool.down.sql").read_text(encoding="utf-8")
    assert "'chat_tool'" in mig and "'chat_tool'" in down and "DELETE FROM canonical_gate_events" in down
