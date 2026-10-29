"""채팅 도구 canonical_document_review 계약 테스트.

고정하는 계약
  1. 도구가 registry·executor dispatch·action 그룹에 있고 입력은 project/document_key/reason 뿐이다.
  2. 초안→검토 성공: generation+1, review 이벤트 1건, actor 는 chat:{session[:8]}.
  3. 이미 review 면 멱등(쓰기 0건). approved/archived 판본은 revision_already_decided.
  4. reason 10자 미만·누락은 DB 접근 전에 거절한다.
  5. PM 역할·CEO 통합지시 세션만 허용하고 그 외 역할은 한글 사유로 거절한다(쓰기 0건).
  6. 도구·API 모두 공용 review_revision_in_tx 를 쓰고, 도구에는 approve/archive/본문 수정 경로가 없다.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api import canonical_documents as docs
from app.services import canonical_document_tools as cdt

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "11111111-1111-1111-1111-111111111111"
HEAD_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
REV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
REASON = "PRD 게이트 점검을 마쳐 CEO 검토를 요청합니다"
_REPO = Path(__file__).resolve().parents[2]


class FakeConn:
    def __init__(self, *, head, last=None, role="PM", project="AADS", workspace="AADS 기획", session=True):
        self.head = head
        self.last = last
        self.session = ({"session_id": SESSION, "role_key": role, "user_id": None, "project": project,
                         "workspace_name": workspace, "display_name": workspace} if session else None)
        self.sql: list[str] = []
        self.args: list[tuple] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "FROM chat_sessions" in sql:
            return self.session
        if "FROM project_document_heads" in sql:
            return self.head
        return None

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return self.last

    async def execute(self, sql, *args):
        self.sql.append(sql)
        self.args.append(args)

    def writes(self):
        return [s for s in self.sql if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


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


def _head(generation=3, approved=None, latest=REV_ID):
    return {"id": HEAD_ID, "tenant_id": TENANT, "document_key": "ovis-spec", "generation": generation,
            "latest_revision_id": latest, "approved_revision_id": approved}


def _inp(**over):
    base = {"project": "aads", "document_key": "ovis-spec", "reason": REASON}
    base.update(over)
    return base


def _review(conn, inp=None, session_id=SESSION):
    return asyncio.run(cdt.review_document(
        tenant_id=TENANT, session_id=session_id, inp=_inp() if inp is None else inp, pool=FakePool(conn)))


def test_draft_to_review_succeeds_and_records_event():
    conn = FakeConn(head=_head(generation=3))
    out = _review(conn)
    assert out["reviewed"] is True and out["status"] == "review" and out["idempotent"] is False
    assert out["approved"] is False and out["revision_id"] == str(REV_ID) and out["reason"] == REASON
    assert any("FOR UPDATE" in s for s in conn.sql)
    writes = conn.writes()
    assert len(writes) == 2
    assert "generation=generation+1" in writes[0] and "'review'" in writes[1]
    assert conn.args[-1][-1] == f"chat:{SESSION[:8]}"
    assert not any("approved" in w or "archived" in w for w in writes)


def test_repeat_call_is_idempotent_without_writes():
    conn = FakeConn(head=_head(), last="review")
    out = _review(conn)
    assert out["reviewed"] is True and out["idempotent"] is True and out["status"] == "review"
    assert conn.writes() == []


@pytest.mark.parametrize("last", ["approved", "archived"])
def test_decided_revision_is_rejected(last):
    conn = FakeConn(head=_head(approved=REV_ID if last == "approved" else None), last=last)
    out = _review(conn)
    assert out["error"] == "revision_already_decided" and out["status"] == 409
    assert "reviewed" not in out and conn.writes() == []


@pytest.mark.parametrize("reason", [None, "", "   ", "짧은사유", "123456789"])
def test_missing_or_short_reason_is_rejected_before_db(reason):
    conn = FakeConn(head=_head())
    inp = _inp()
    inp["reason"] = reason
    out = _review(conn, inp)
    assert out["error"] == "invalid_input" and "reason" in out["message"]
    assert conn.sql == []


def test_invalid_project_or_key_rejected_before_db():
    conn = FakeConn(head=_head())
    assert _review(conn, _inp(project="x y"))["error"] == "invalid_input"
    assert _review(conn, _inp(document_key="a/b"))["error"] == "invalid_input"
    assert conn.sql == []


@pytest.mark.parametrize("role", ["PM", "pm", "Project-Manager", "CanonicalManager"])
def test_pm_roles_allowed(role):
    assert _review(FakeConn(head=_head(), role=role))["reviewed"] is True


def test_ceo_integrated_session_allowed():
    conn = FakeConn(head=_head(), role="CEO", project="CEO", workspace="[CEO] 통합지시")
    assert _review(conn)["reviewed"] is True


@pytest.mark.parametrize("role,project,workspace", [
    ("CTO", "AADS", "AADS 개발"),
    ("Developer", "AADS", "AADS 개발"),
    ("QA", "AADS", "AADS 개발"),
    ("", "AADS", "AADS 개발"),
    ("CEO", "AADS", "AADS 개발"),
    ("CEO", "CEO", "[CEO] 다른방"),
])
def test_other_roles_rejected_in_korean_without_writes(role, project, workspace):
    conn = FakeConn(head=_head(), role=role, project=project, workspace=workspace)
    out = _review(conn)
    assert out["error"] == "role_not_allowed" and "PM" in out["message"] and "CEO" in out["message"]
    assert conn.writes() == []
    assert not any("project_document_heads" in s for s in conn.sql)


def test_unknown_or_missing_session_rejected():
    conn = FakeConn(head=_head(), session=False)
    assert _review(conn)["error"] == "role_not_allowed"
    conn = FakeConn(head=_head())
    assert _review(conn, session_id="")["error"] == "role_not_allowed"
    assert conn.writes() == [] and conn.sql == []


def test_missing_document_or_revision():
    assert _review(FakeConn(head=None))["error"] == "document_not_found"
    assert _review(FakeConn(head=_head(latest=None)))["error"] == "revision_not_found"


def test_tool_registered_with_minimal_schema():
    from app.services.tool_executor import ToolExecutor
    from app.services.tool_registry import _GROUPS, ToolRegistry

    tool = ToolRegistry().get_tool("canonical_document_review")
    schema = tool["input_schema"]
    assert "canonical_document_review" in _GROUPS["action"] and "canonical_document_review" in _GROUPS["all"]
    assert set(schema["properties"]) == {"session_id", "project", "document_key", "reason"}
    assert set(schema["required"]) == {"project", "document_key", "reason"}
    assert "승인은 CEO 만 합니다" in tool["description"]
    assert hasattr(ToolExecutor, "_canonical_document_review")
    assert '"canonical_document_review":' in inspect.getsource(ToolExecutor._dispatch)


def test_plan_mode_blocks_review_tool():
    from app.services import plan_mode_guard

    assert plan_mode_guard.classify("canonical_document_review", {})[0] is False


def test_no_approve_archive_or_edit_path_in_tool():
    src = inspect.getsource(cdt.review_document) + inspect.getsource(cdt._review_role_denial)
    for banned in ("approve_document", "archive_document", "_archive_approved", "approved_revision_id=",
                   "create_revision_in_tx", "'approved'", "'archived'", "UPDATE project_document_revisions"):
        assert banned not in src
    executor = inspect.getsource(
        __import__("app.services.tool_executor", fromlist=["ToolExecutor"]).ToolExecutor._canonical_document_review)
    assert "approve_document" not in executor and "archive" not in executor
    assert "review_revision_in_tx" in inspect.getsource(cdt.review_document)
    assert "review_revision_in_tx" in inspect.getsource(docs.mark_review)


@pytest.mark.asyncio
async def test_executor_handler_requires_tenant_and_delegates(monkeypatch):
    from app.services import tool_executor as te

    token_s, token_t = te.current_chat_session_id.set(""), te.current_tenant_id.set("")
    monkeypatch.setattr("app.services.agent_sdk_service.get_active_chat_session_id", lambda: "")
    try:
        res = await te.ToolExecutor()._canonical_document_review(_inp())
        assert res["error"] == "missing_tenant_id"
        te.current_tenant_id.set(TENANT)
        te.current_chat_session_id.set(SESSION)
        rev = AsyncMock(return_value={"reviewed": True, "project": "AADS", "document_key": "design"})
        monkeypatch.setattr(cdt, "lookup_documents", AsyncMock(return_value={"registered": True}))
        monkeypatch.setattr(cdt, "review_document", rev)
        assert (await te.ToolExecutor()._canonical_document_review(_inp()))["reviewed"] is True
        assert rev.await_args.kwargs["tenant_id"] == TENANT and rev.await_args.kwargs["session_id"] == SESSION
    finally:
        te.current_tenant_id.reset(token_t)
        te.current_chat_session_id.reset(token_s)
