"""정본 문서 승인 위임 계약 테스트 (DB 없이 FakeConn)."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from app.services import canonical_document_delegation as deleg
from app.services import canonical_document_tools as cdt
from app.services.doc_review_rubric import REVIEW_MARKER, SCORE_KEYS

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "11111111-1111-1111-1111-111111111111"
HEAD_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
REV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
GOAL = uuid.UUID("44444444-4444-4444-4444-444444444444")
OTHER_GOAL = uuid.UUID("55555555-5555-5555-5555-555555555555")
REASON = "교차검수 두 회사 통과, 위임 범위 안이라 승인합니다"
NOW = datetime.now(timezone.utc)


def review(provider, model, verdict="pass", issues=None, rid=1):
    data = {"checks": [], "scores": {k: 0.9 for k in SCORE_KEYS}, "score": 0.9,
            "issues": issues or [], "unverified": [], "ceo_brief": "검토 결과 승인할 수 있습니다"}
    return {"id": rid, "reviewer_provider": provider, "reviewer_model": model, "verdict": verdict,
            "reason": "요약" + REVIEW_MARKER + json.dumps(data)}


PASS_BOTH = [review("anthropic", "claude-x", rid=1), review("openai", "gpt-x", rid=2)]


def delegation(goal_id=None, grantee=SESSION, expires=NOW + timedelta(days=5), revoked=None):
    return {"id": uuid.uuid4(), "goal_id": goal_id, "grantee": grantee, "granted_by": "human-1",
            "expires_at": expires, "revoked_at": revoked, "created_at": NOW}


class FakeConn:
    def __init__(self, *, delegations=(), reviews=PASS_BOTH, goal_links=(GOAL,), owner=False,
                 last="review", approved=None, generation=3):
        self.head = {"id": HEAD_ID, "tenant_id": TENANT, "project_key": "AADS", "document_key": "doc-a",
                     "kind": "spec", "latest_revision_id": REV_ID, "approved_revision_id": approved,
                     "generation": generation}
        self.delegations, self.reviews, self.goal_links, self.owner, self.last = delegations, reviews, goal_links, owner, last
        self.executed: list[tuple] = []
        self.calls = []
        self.in_tx = False

    def transaction(self):
        class _Tx:
            async def __aenter__(s): self.in_tx = True
            async def __aexit__(s, *e): self.in_tx = False; return False
        return _Tx()

    async def fetch(self, sql, *args):
        if "project_document_goal_links" in sql:
            return [{"goal_id": g} for g in self.goal_links]
        if "approval_delegations" in sql:
            # SQL 이 revoked/expired 를 거르므로 같은 조건을 흉내낸다.
            return [d for d in self.delegations if d["revoked_at"] is None and d["expires_at"] > NOW]
        if "project_document_reviews" in sql:
            return self.reviews
        return []

    async def fetchval(self, sql, *args):
        if "FROM goals" in sql:
            return self.owner
        if "project_document_events" in sql:
            return self.last
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, self.in_tx))
        if "approval_delegations" in sql:
            d = next((d for d in self.delegations if d["id"] == args[0]), None)
            if "UPDATE" in sql and sql.startswith("UPDATE"):
                return {"id": d["id"]} if d else None
            return {**d, "unexpired": d["expires_at"] > datetime.now(timezone.utc)} if d else None
        if "FROM chat_sessions" in sql:
            return {"session_id": SESSION, "role_key": "PM", "user_id": None, "project": "AADS",
                    "workspace_name": "AADS", "display_name": "AADS"}
        if "FROM project_document_heads" in sql:
            return self.head
        if "FROM project_document_revisions" in sql:
            return {"id": REV_ID, "doc_tier": None, "title": "정본 문서", "revision": 1, "version": "1.0.0"}  # enforce 는 tier 없으면 summary 만 본다
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, self.in_tx))
        self.executed.append((sql, args))


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self):
        conn = self.conn
        class _A:
            async def __aenter__(s): return conn
            async def __aexit__(s, *e): return False
        return _A()


@pytest.fixture(autouse=True)
def _no_enforce(monkeypatch):
    async def fake_enforce(*a, **k):
        return {}
    monkeypatch.setattr("app.services.doc_cross_review.enforce", fake_enforce)

    async def fake_head(conn, tenant, project, key, lock=False):
        return conn.head
    monkeypatch.setattr(docs, "_head", fake_head)


def approve(conn, **over):
    inp = {"project": "AADS", "document_key": "doc-a", "revision_id": str(REV_ID),
           "expected_generation": 3, "reason": REASON, **over}
    return asyncio.run(cdt.approve_document_delegated(tenant_id=TENANT, session_id=SESSION, inp=inp, pool=Pool(conn)))


def test_no_delegation_denied():
    conn = FakeConn()
    res = approve(conn)
    assert res["approved"] is False and res["code"] == "delegation_missing"
    assert conn.executed == []


def test_delegation_and_both_pass_approves_with_audit():
    conn = FakeConn(delegations=[delegation()])
    res = approve(conn)
    assert res["approved"] is True and res["granted_by"] == "human-1"
    insert = [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]][0]
    assert "'approved'" in insert[0]
    assert insert[1][4] == f"chat:{SESSION}"
    meta = json.loads(insert[1][5])
    assert meta["granted_by"] == "human-1" and meta["delegation_id"] == res["delegation_id"]


def test_major_issue_denied():
    major = [{"severity": "major", "criterion": "c", "location": "l", "problem": "p", "fix": "수정"}]
    conn = FakeConn(delegations=[delegation()], reviews=[review("anthropic", "claude-x", issues=major, rid=1),
                                                          review("openai", "gpt-x", rid=2)])
    res = approve(conn)
    assert res["code"] == "cross_review_major_present" and not any("UPDATE" in e[0] for e in conn.executed)


def test_blocker_and_reject_and_incomplete():
    blocker = [{"severity": "blocker", "criterion": "c", "location": "l", "problem": "p", "fix": "수정"}]
    assert deleg.cross_review_problem([review("anthropic", "a", issues=blocker), review("openai", "b", rid=2)]) == "cross_review_blocker_present"
    assert deleg.cross_review_problem([review("anthropic", "a"), review("openai", "b", "reject", rid=2)]) == "cross_review_reject_present"
    assert deleg.cross_review_problem([review("anthropic", "a")]) == "cross_review_incomplete"
    assert deleg.cross_review_problem(PASS_BOTH) is None
    legacy = {"id": 3, "reviewer_provider": "openai", "reviewer_model": "gpt-x", "verdict": "pass", "reason": "통과"}
    assert deleg.cross_review_problem([review("anthropic", "a"), legacy]) == "cross_review_unreliable"


def test_expired_and_revoked_denied():
    assert approve(FakeConn(delegations=[delegation(expires=NOW - timedelta(days=1))]))["code"] == "delegation_missing"
    assert approve(FakeConn(delegations=[delegation(revoked=NOW)]))["code"] == "delegation_missing"


def test_other_goal_denied_and_scoped_goal_allowed():
    assert approve(FakeConn(delegations=[delegation(goal_id=OTHER_GOAL)]))["code"] == "goal_out_of_scope"
    assert approve(FakeConn(delegations=[delegation(goal_id=GOAL)]))["approved"] is True


def test_session_must_match_grantee_even_for_goal_owner():
    other = delegation(grantee="someone-else")
    assert approve(FakeConn(delegations=[other]))["code"] == "not_delegated_session"
    assert approve(FakeConn(delegations=[other], owner=True))["code"] == "not_delegated_session"
    assert approve(FakeConn(delegations=[delegation(grantee="pm")]))["approved"] is True  # role_key 일치


def test_stale_generation_already_approved_not_latest():
    d = [delegation()]
    assert approve(FakeConn(delegations=d), expected_generation=2)["code"] == "generation_conflict"
    assert approve(FakeConn(delegations=d, approved=REV_ID))["code"] == "already_approved"
    other = str(uuid.uuid4())
    assert approve(FakeConn(delegations=d), revision_id=other)["code"] == "not_latest_revision"


def test_not_in_review_blocked_by_core():
    res = approve(FakeConn(delegations=[delegation()], last="draft"))
    assert res["approved"] is False and res["code"] == "revision_not_in_review"


def test_invalid_inputs():
    conn = FakeConn(delegations=[delegation()])
    assert approve(conn, reason="짧음")["error"] == "invalid_input"
    assert approve(conn, expected_generation=0)["error"] == "invalid_input"


def _pool_guard(monkeypatch):
    monkeypatch.setattr(docs, "get_pool", lambda: Pool(FakeConn()))


def test_agent_context_cannot_grant_delegation(monkeypatch):
    _pool_guard(monkeypatch)
    ctx = {"user": {"user_id": "agent-1", "auth_type": "api_key"}, "membership": {"status": "active", "role": "member"},
           "tenant": {"id": TENANT}}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs.grant_approval_delegation("AADS", docs.DelegationInput(grantee="PM"), ctx))
    assert exc.value.status_code == 403 and exc.value.detail == "human_login_required"


def test_agent_context_cannot_revoke_delegation(monkeypatch):
    _pool_guard(monkeypatch)
    ctx = {"user": {"user_id": "agent-1", "auth_type": "api_key"}, "membership": {"status": "active"},
           "tenant": {"id": TENANT}}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs.revoke_approval_delegation("AADS", uuid.uuid4(), ctx))
    assert exc.value.detail == "human_login_required"


def test_default_approve_sql_unchanged_without_metadata():
    conn = FakeConn()
    asyncio.run(docs.approve_revision_in_tx(conn, TENANT, "AADS", conn.head, REV_ID, "human-1"))
    insert = [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]][0]
    assert "metadata" not in insert[0] and len(insert[1]) == 5


def test_tool_registered():
    from app.services.tool_executor import ToolExecutor
    from app.services.tool_registry import _GROUPS, ToolRegistry
    import inspect
    tool = ToolRegistry().get_tool("canonical_document_approve")
    assert tool and "canonical_document_approve" in _GROUPS["action"] and "canonical_document_approve" in _GROUPS["all"]
    assert hasattr(ToolExecutor, "_canonical_document_approve")
    assert '"canonical_document_approve":' in inspect.getsource(ToolExecutor._dispatch)


@pytest.mark.parametrize("issues", [None, "문제 없음"])
def test_malformed_issues_denied(issues):
    bad = dict(PASS_BOTH[1])
    prefix, body = bad["reason"].split(REVIEW_MARKER)
    data = json.loads(body)
    if issues is None:
        del data["issues"]
    else:
        data["issues"] = issues
    bad["reason"] = prefix + REVIEW_MARKER + json.dumps(data)
    assert approve(FakeConn(delegations=[delegation()], reviews=[PASS_BOTH[0], bad]))["code"] == "cross_review_unreliable"


def test_goal_b_owner_cannot_use_goal_a_delegation():
    conn = FakeConn(delegations=[delegation(goal_id=GOAL, grantee="other")],
                    goal_links=(GOAL, OTHER_GOAL), owner=True)
    assert approve(conn)["code"] == "not_delegated_session"
    assert conn.executed == []


def test_approval_locks_delegation_before_core_update():
    conn = FakeConn(delegations=[delegation()])
    assert approve(conn)["approved"]
    lock = next(i for i, (sql, _) in enumerate(conn.calls)
                if "approval_delegations" in sql and "FOR UPDATE" in sql)
    update = next(i for i, (sql, _) in enumerate(conn.calls) if sql.startswith("UPDATE project_document_heads"))
    assert lock < update and conn.calls[lock][1] and conn.calls[update][1]


@pytest.mark.parametrize("change", ["revoked", "expired"])
def test_delegation_rechecked_after_lock(change):
    class RacingConn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "approval_delegations" in sql and "FOR UPDATE" in sql:
                if change == "revoked":
                    self.delegations[0]["revoked_at"] = NOW
                else:
                    self.delegations[0]["expires_at"] = NOW - timedelta(days=1)
            return await super().fetchrow(sql, *args)
    conn = RacingConn(delegations=[delegation()])
    assert approve(conn)["code"] == "delegation_missing"
    assert conn.executed == []


def test_revoke_locks_same_row_before_update():
    conn = FakeConn(delegations=[delegation()])
    async def run():
        async with conn.transaction():
            return await deleg.revoke_delegation(conn, TENANT, "AADS", conn.delegations[0]["id"], "human-1")
    assert asyncio.run(run())
    assert "FOR UPDATE" in conn.calls[0][0]
    assert conn.calls[1][0].startswith("UPDATE project_document_approval_delegations")
    assert all(in_tx for _, in_tx in conn.calls)


@pytest.mark.parametrize("selector", [{"document_key": "doc-a"}, {"query": "문서"}, {"file_path": "docs/a.md"}])
def test_lookup_server_card_accepts_delegation_but_rejects_forgery(selector):
    class LookupConn(FakeConn):
        async def fetch(self, sql, *args):
            if "project_document_heads" in sql:
                return [self.head]
            return await super().fetch(sql, *args)
    d = delegation()
    conn = LookupConn(delegations=[d])
    conn.head["title"] = "정본 문서"
    result = asyncio.run(cdt.lookup_documents(tenant_id=TENANT, session_id=SESSION,
                                              inp={"project": "AADS", **selector}, pool=Pool(conn)))
    card = result.get("approval_card") or result["documents"][0]["approval_card"]
    assert card["can_approve"] is True
    assert card["approve_via"] == "delegation" and card["delegation_id"] == str(d["id"])
    assert cdt.validated_approval_cards([json.loads(json.dumps(card))]) == [card]
    forged = {k: v for k, v in card.items() if k != "_delegation_proof"}
    assert cdt.validated_approval_cards([forged]) == []
    assert cdt.validated_approval_cards([{**card, "generation": 999}]) == []
    assert conn.executed == []


def test_lookup_does_not_trust_session_or_capability_from_model_input():
    conn = FakeConn(delegations=[delegation()])
    conn.head["title"] = "정본 문서"
    result = asyncio.run(cdt.lookup_documents(tenant_id=TENANT,
        inp={"project": "AADS", "document_key": "doc-a", "session_id": SESSION, "can_approve": True}, pool=Pool(conn)))
    assert result["approval_card"]["can_approve"] is False


@pytest.mark.parametrize("delegations", [[], [delegation(grantee="other")], [delegation(goal_id=OTHER_GOAL)]])
def test_lookup_card_remains_disabled_without_eligible_grantee(delegations):
    conn = FakeConn(delegations=delegations)
    conn.head["title"] = "정본 문서"
    result = asyncio.run(cdt.lookup_documents(tenant_id=TENANT, session_id=SESSION,
        inp={"project": "AADS", "document_key": "doc-a"}, pool=Pool(conn)))
    card = result["approval_card"]
    assert card["can_approve"] is False and "delegation_id" not in card
    assert cdt.validated_approval_cards([card]) == [card]


@pytest.mark.parametrize("field,value", [("score", float("inf")), ("dropped_issues", 1), ("scores", {})])
def test_invalid_truncated_nonfinite_reviews_fail_closed(field, value):
    bad = dict(PASS_BOTH[1])
    prefix, body = bad["reason"].split(REVIEW_MARKER)
    data = json.loads(body)
    data[field] = value
    bad["reason"] = prefix + REVIEW_MARKER + json.dumps(data)
    assert deleg.cross_review_problem([PASS_BOTH[0], bad]) == "cross_review_unreliable"
