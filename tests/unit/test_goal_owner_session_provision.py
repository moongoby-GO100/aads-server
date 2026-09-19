"""담당 세션은 **승인 카드를 거쳐서만** 생긴다 (AADS-GOALOWNER-SESSION-P1).

원칙 하나를 지키면서 침묵 하나를 없애는 변경이다.

  · 지킬 것 — `add_goal_owner` 는 세션을 만들지 않는다. "채팅창이 늘어나는
    것을 대표님이 모르시는 상태가 되면 안 된다."
  · 없앨 것 — `goal_dispatch` 가 담당 세션 없는 마일스톤을 `skipped += 1` 로
    조용히 건너뛰던 것. 기록이 없어 영원히 방치됐다.

여기서 검사하는 네 가지가 무너지면 둘 중 하나가 깨진다.

  1. 같은 (프로젝트, 역할) 로 두 번 요청해도 카드는 하나 — 스케줄러는 몇 분마다
     같은 마일스톤을 다시 본다. 멱등이 아니면 대기 목록이 같은 카드로 덮인다.
  2. `approval_scope` 에 담당 정보가 남는다 — 승인 시점에 되살릴 방법이 없다.
  3. 생성도 멱등 — 이미 있는 role_key 세션이 있으면 연결만 한다. 아니면
     일괄 승인 한 번에 채팅창이 둘로 늘어난다.
  4. `goal_owner` 카드의 선택지는 승인/거절 둘뿐 — 1회성 생성에 "이 대화 동안
     최대 50회" 가 붙으면 승인 한 번으로 채팅창이 계속 늘어난다.

DB 는 붙지 않는다. 가짜 커넥션으로 질의 본문을 보고 답한다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services import owner_session_provision as osp

GOAL = "1a00d8d3-1126-4a6e-8ed2-8a43f5502250"
LEAD = "5090a247-47f7-4a05-9a1e-2f0b6c1d8e30"
WORKSPACE = "7c2f1b90-0a55-4c11-8d33-99f0a1b2c3d4"
TENANT = "0f9d4f2a-1111-4222-8333-444455556666"
EXISTING_SESSION = "abcd1234-5678-4abc-9def-000011112222"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _RequestConn:
    """요청 경로용 가짜 커넥션. 만들어진 카드를 기억한다."""

    def __init__(self):
        self.cards: list[dict] = []
        self.inserts: list[tuple] = []

    async def fetchval(self, query, *args):
        if "FROM agent_permission_requests" in query and "decision = 'pending'" in query:
            key = args[0]
            for card in self.cards:
                if card["work_key"] == key:
                    return card["id"]
            return None
        if "FROM prompt_assets" in query:
            return 1
        if "FROM chat_workspaces" in query and "project_key" in query:
            return WORKSPACE
        if "INSERT INTO agent_permission_requests" in query:
            self.inserts.append(args)
            new_id = f"card-{len(self.cards) + 1}"
            self.cards.append({"id": new_id, "work_key": args[1], "scope": args[5]})
            return new_id
        return None

    async def fetchrow(self, query, *args):
        if "FROM chat_sessions" in query:
            return {"tenant_id": TENANT}
        if "FROM goals g" in query:
            return {"goal_title": "채팅 시스템 안정화", "project": "AADS", "stuck": 3}
        return None

    async def execute(self, query, *args):
        return "OK"


def _patch_pool(monkeypatch, conn):
    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn), raising=False)


def _request(**kw):
    return asyncio.run(osp.request_owner_session(**kw))


# ─── 1) work_key 멱등 ───────────────────────────────────────────────────────
def test_same_project_and_role_reuses_one_card(monkeypatch):
    conn = _RequestConn()
    _patch_pool(monkeypatch, conn)

    first = _request(goal_id=GOAL, role_key="운영인프라담당", project="AADS",
                     requester_session_id=LEAD)
    second = _request(goal_id=GOAL, role_key="운영인프라담당", project="AADS",
                      requester_session_id=LEAD)

    assert first["reused"] is False
    assert second["reused"] is True
    assert second["request_id"] == first["request_id"]
    assert len(conn.cards) == 1, "같은 역할로 카드가 두 장 생겼다"


def test_work_key_separates_roles_and_projects():
    a = osp.work_key_for("AADS", "운영인프라담당")
    b = osp.work_key_for("AADS", "품질담당")
    c = osp.work_key_for("GO100", "운영인프라담당")
    assert len({a, b, c}) == 3
    # 프로세스가 달라도 같아야 한다 — 해시를 섞지 않는 이유다.
    assert a == "owner-session:AADS:운영인프라담당"


# ─── 2) approval_scope 필수 키 ──────────────────────────────────────────────
def test_approval_scope_carries_everything_provision_needs(monkeypatch):
    conn = _RequestConn()
    _patch_pool(monkeypatch, conn)

    _request(goal_id=GOAL, role_key="운영인프라담당", project="AADS",
             requester_session_id=LEAD)

    scope = json.loads(conn.cards[0]["scope"])
    for key in ("goal_id", "role_key", "project", "workspace_id", "has_prompt"):
        assert key in scope, f"approval_scope 에 {key} 가 없다 — 승인 시점에 되살릴 수 없다"
    assert scope["goal_id"] == GOAL
    assert scope["role_key"] == "운영인프라담당"
    assert scope["project"] == "AADS"
    assert scope["workspace_id"] == WORKSPACE
    assert scope["has_prompt"] is True
    assert scope["scope"] == "single_call"


def test_card_is_low_risk_approve_tier_from_goal_owner_gate(monkeypatch):
    conn = _RequestConn()
    _patch_pool(monkeypatch, conn)
    _request(goal_id=GOAL, role_key="운영인프라담당", project="AADS",
             requester_session_id=LEAD)

    args = conn.inserts[0]
    assert args[2] == osp.ACTION_TYPE == "create_owner_session"
    assert args[6] == osp.GATE_SOURCE == "goal_owner"
    # 대표님이 카드만 읽고 판단하실 수 있어야 한다.
    summary = args[3]
    assert "채팅 시스템 안정화" in summary
    assert "3건" in summary
    assert "운영인프라담당" in summary
    assert "되돌리는 법" in summary


def test_summary_warns_when_the_role_has_no_prompt():
    summary = osp._summary(
        goal_title="목표", role_key="품질담당", stuck=2,
        title=osp.session_title_for("품질담당"), has_prompt=False,
    )
    assert "역할 프롬프트가 없어" in summary
    assert "모르는 채로 시작합니다" in summary


def test_request_refuses_without_a_requester_session(monkeypatch):
    conn = _RequestConn()
    _patch_pool(monkeypatch, conn)
    out = _request(goal_id=GOAL, role_key="운영인프라담당", project="AADS",
                   requester_session_id="")
    assert out.get("error")
    assert not conn.cards


# ─── 3) provision 멱등 ─────────────────────────────────────────────────────
class _ProvisionConn:
    def __init__(self, *, has_session: bool):
        self.has_session = has_session
        self.executed: list[tuple] = []

    async def fetchval(self, query, *args):
        if "FROM prompt_assets" in query:
            return 1
        if "SELECT project_key FROM chat_workspaces" in query:
            return "AADS"
        if "information_schema.columns" in query:
            return None
        if "UPDATE milestones" in query:
            self.executed.append(("milestones", args))
            return 2
        return None

    async def fetchrow(self, query, *args):
        if "FROM chat_sessions" in query and "role_key = $2" in query:
            if self.has_session:
                return {"id": EXISTING_SESSION, "title": "[운영인프라담당] 담당"}
            return None
        if "FROM chat_workspaces" in query:
            return {"tenant_id": TENANT}
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


def _provision(monkeypatch, conn, created_id="new-session-id"):
    _patch_pool(monkeypatch, conn)
    made: list[dict] = []

    async def _fake_create(data, tenant_id=None, user_id=None):
        made.append({"data": data, "tenant_id": tenant_id, "user_id": user_id})
        return {"id": created_id, "title": data["title"]}

    from app.services import chat_service

    monkeypatch.setattr(chat_service, "create_session", _fake_create, raising=False)
    result = asyncio.run(osp.provision_owner_session({
        "goal_id": GOAL, "role_key": "운영인프라담당", "project": "AADS",
        "workspace_id": WORKSPACE, "requester_session_id": LEAD, "has_prompt": True,
    }))
    return result, made


def test_existing_role_session_is_linked_not_duplicated(monkeypatch):
    conn = _ProvisionConn(has_session=True)
    result, made = _provision(monkeypatch, conn)

    assert result["created"] is False, "이미 있는 담당인데 채팅창을 새로 만들었다"
    assert made == [], "create_session 이 불렸다"
    assert result["session_id"] == EXISTING_SESSION
    assert result["linked_milestones"] == 2
    # 있던 세션이라도 목표에는 붙여야 현황에 나온다.
    assert any("goal_task_links" in str(q) for q, _ in conn.executed)


def test_missing_role_session_is_created_once(monkeypatch):
    conn = _ProvisionConn(has_session=False)
    result, made = _provision(monkeypatch, conn)

    assert result["created"] is True
    assert len(made) == 1
    assert made[0]["data"]["role_key"] == "운영인프라담당"
    assert made[0]["data"]["title"] == "[운영인프라담당] 담당"
    assert made[0]["data"]["workspace_id"] == WORKSPACE
    assert made[0]["tenant_id"] == TENANT


def test_provision_refuses_an_incomplete_scope():
    with pytest.raises(ValueError):
        asyncio.run(osp.provision_owner_session({"role_key": "", "workspace_id": ""}))


# ─── 4) 선택지는 승인/거절 둘뿐 ────────────────────────────────────────────
class _PendingPool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _pending_rows(gate_source):
    return [{
        "id": "card-1", "action_type": "create_owner_session",
        "action_summary": "[담당 세션 생성] 목표", "risk_level": "low",
        "gate_source": gate_source, "tier": "approve", "requested_by": LEAD,
        "work_key": "owner-session:AADS:운영인프라담당", "at": "09-17 10:00",
        "decision": "pending", "expires_in_min": 120,
        # 카드를 어느 응답 버블 아래에 붙일지 — `approvals_pending` 이 읽는
        # 열이다(project_docs.py). 가짜 행에서 빼면 KeyError 로 죽는데, 그건
        # 선택지 규칙이 깨진 것이 아니라 이 표본이 낡은 것이다.
        "source_message_id": None,
    }]


def test_goal_owner_card_offers_only_approve_and_reject(monkeypatch):
    import app.core.db_pool as db_pool
    from app.api import project_docs

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _PendingPool(_pending_rows("goal_owner")),
        raising=False,
    )
    out = asyncio.run(project_docs.approvals_pending(limit=50, session_id=""))
    choices = out["pending"][0]["choices"]

    assert len(choices) == 2, f"1회성 생성에 반복 권한이 붙었다: {choices}"
    assert [c["key"] for c in choices] == ["single", "reject"]
    assert choices[0]["label"] == "세션 생성 승인"
    assert choices[0]["params"] == {
        "decision": "approved", "scope": "single", "hours": 2,
    }
    assert all("max_executions" not in c["params"] for c in choices)


def test_other_gates_keep_their_wider_choices(monkeypatch):
    """goal_owner 분기를 끼워 넣다가 실매매 카드의 선택지를 깎지 않았는지."""
    import app.core.db_pool as db_pool
    from app.api import project_docs

    rows = _pending_rows("live_trading")
    rows[0]["risk_level"] = "high"
    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _PendingPool(rows), raising=False,
    )
    out = asyncio.run(project_docs.approvals_pending(limit=50, session_id=""))
    keys = [c["key"] for c in out["pending"][0]["choices"]]
    assert keys == ["single", "mission", "session", "project", "reject"]
