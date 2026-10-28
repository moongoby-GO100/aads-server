"""M7: 설계 턴 분류(사실·제안·확정·미결)와 목표/테넌트 오연결 차단."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException

from app.api import directive_drafts as api
from app.services import directive_draft_service as service
from app.services.directive_draft_service import DraftSource, classify_turn
from tests.unit.test_directive_draft_turn_dedup import (
    TENANT_A,
    TENANT_B,
    FakeConn,
    FakePool,
    build_fallback_directive,
)

REAL_RESOLVE = service._resolve_goal_binding


# ── 규칙 기반 분류 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("확정합니다.", "confirmed"),
        ("확정", "confirmed"),
        ("진행해", "confirmed"),
        ("그렇게 해줘", "confirmed"),
        ("이대로 진행하세요", "confirmed"),
        ("이 목표를 완료로 바꾸면 어떨까요?", "open"),
        ("마일스톤 3을 취소하면?", "open"),
        ("이 방향 검토해봐", "open"),
        ("만약 테넌트를 분리한다고 가정하면 비용이 얼마야", "open"),
        ("확정할까요?", "open"),
        ("진행해도 될까요", "open"),
        ("아직 확정하지 마", "open"),
        ("진행하지 마세요", "open"),
        ("A 말고 B로 진행해", "open"),
        ("마일스톤 순서를 바꾸는 게 좋겠어", "proposal"),
        ("완료 기준을 추가해줘", "proposal"),
        ("현재 목표는 진행률 40%이고 마일스톤 2개가 남았다.", "fact"),
        ("", "open"),
        (None, "open"),
    ],
)
def test_turn_kind_rules(text, kind):
    assert classify_turn(text)["turn_kind"] == kind


def test_evidence_and_decisions_carry_trigger_phrase_and_source_message():
    message_id = uuid.uuid4()
    result = classify_turn("지금 상태는 정상이다. 그렇게 해줘", source_message_id=message_id)
    assert result["turn_kind"] == "confirmed"
    assert result["turn_kind_evidence"] == ["그렇게 해"]
    assert result["decisions"] == [
        {"kind": "fact", "text": "지금 상태는 정상이다.", "source_message_id": str(message_id)},
        {"kind": "confirmed", "text": "그렇게 해줘", "source_message_id": str(message_id)},
    ]


def test_mixed_turn_without_approval_is_open_not_confirmed():
    result = classify_turn("완료 기준을 추가해줘. 그런데 이건 확정해도 될까?")
    assert result["turn_kind"] == "open"
    assert [d["kind"] for d in result["decisions"]] == ["proposal", "open"]


def test_gate_allows_only_confirmed():
    service.require_confirmed_turn("confirmed")
    for kind in ("fact", "proposal", "open", None):
        with pytest.raises(service.GoalChangeNotConfirmedError):
            service.require_confirmed_turn(kind)


# ── create_draft 흐름 ─────────────────────────────────────────────────────────

class GoalConn(FakeConn):
    """FakeConn + goals/milestones/goal_task_links. goals·milestones 쓰기는 모두 기록한다."""

    def __init__(self):
        super().__init__()
        self.goals: dict[uuid.UUID, uuid.UUID] = {}
        self.milestones: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
        self.links: list[tuple[uuid.UUID, str]] = []
        self.formal_goal_writes: list[str] = []

    def _record(self, query):
        compact = " ".join(query.split())
        if ("goals" in compact or "milestones" in compact) and compact.lstrip().split()[0] in {
            "INSERT", "UPDATE", "DELETE",
        }:
            self.formal_goal_writes.append(compact)

    async def fetchval(self, query, *args):
        if "FROM goals" in query:
            return self.goals.get(args[0])
        return await super().fetchval(query, *args)

    async def fetchrow(self, query, *args):
        if "FROM milestones" in query:
            found = self.milestones.get(args[0])
            return {"goal_id": found[0], "tenant_id": found[1]} if found else None
        return await super().fetchrow(query, *args)

    async def fetch(self, query, *args):
        assert "goal_task_links" in query, query
        session, tenant = args
        ids = {g for g, s in self.links if s == session and self.goals.get(g) == tenant}
        return [{"goal_id": g} for g in ids]

    async def execute(self, query, *args):
        self._record(query)
        return await super().execute(query, *args)


class Harness:
    def __init__(self, monkeypatch):
        self.conn = GoalConn()
        self.llm_calls = 0
        self.body = "첫 생성"
        self.source: DraftSource | None = None
        monkeypatch.setattr(service, "get_pool", lambda: FakePool(self.conn))
        monkeypatch.setattr(service, "_load_source", self._load_source)
        monkeypatch.setattr(service, "generate_directive_content", self._generate)
        monkeypatch.setattr(service, "_resolve_goal_binding", REAL_RESOLVE)

        async def no_optional_columns(_conn):
            return set()

        monkeypatch.setattr("app.services.goal_manager.link_optional_columns", no_optional_columns)

    async def _load_source(self, **kwargs):
        composer = (kwargs.get("composer_draft") or "").strip() or None
        return DraftSource(**{**self.source.__dict__, "composer_draft": composer})

    async def _generate(self, source, risk_level, **_kwargs):
        self.llm_calls += 1
        content = build_fallback_directive(source, risk_level).replace(
            "DESCRIPTION:", f"DESCRIPTION:\n{self.body}", 1
        )
        return content, "generated", "test-model"

    def set_source(self, user_text="상태를 확인해줘", session_id=None) -> DraftSource:
        self.source = DraftSource(
            session_id=session_id or (self.source.session_id if self.source else uuid.uuid4()),
            workspace_id=uuid.uuid4(), session_title="세션", workspace_name="워크스페이스",
            project_key="AADS",
            messages=[
                {"id": uuid.uuid4(), "role": "user", "content": user_text},
                {"id": uuid.uuid4(), "role": "assistant", "content": "답변입니다."},
            ],
        )
        return self.source

    def create(self, tenant=TENANT_A, **kwargs):
        return asyncio.run(service.create_draft(
            tenant_id=tenant, user_id=None, session_id=str(self.source.session_id), **kwargs
        ))

    def add_goal(self, tenant=TENANT_A, milestones=1):
        goal_id = uuid.uuid4()
        self.conn.goals[goal_id] = uuid.UUID(tenant)
        milestone_ids = []
        for _ in range(milestones):
            milestone_id = uuid.uuid4()
            self.conn.milestones[milestone_id] = (goal_id, uuid.UUID(tenant))
            milestone_ids.append(milestone_id)
        return goal_id, milestone_ids


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def test_question_only_turn_changes_no_goal_or_milestone(h):
    goal_id, [milestone_id] = h.add_goal()
    h.set_source()
    created = h.create(
        composer_draft="이 목표를 완료로 바꾸면 어떨까요?",
        goal_id=str(goal_id), milestone_id=str(milestone_id),
    )
    classification = h.conn.drafts[0]["classification"]
    assert classification["turn_kind"] == "open"
    assert classification["turn_kind_evidence"]
    assert [d["kind"] for d in classification["decisions"]] == ["open"]
    assert created["goal_change_allowed"] is False
    assert h.conn.formal_goal_writes == []
    with pytest.raises(service.GoalChangeNotConfirmedError):
        service.require_confirmed_turn(classification["turn_kind"])
    assert len(h.conn.drafts) == len(h.conn.revisions) == 1


def test_assumption_and_review_turns_never_allow_goal_change(h):
    h.set_source("가정해서 마일스톤을 두 개로 쪼개면 어때?")
    first = h.create()
    h.set_source("방향 검토해봐", session_id=h.source.session_id)
    second = h.create()
    assert first["goal_change_allowed"] is second["goal_change_allowed"] is False
    assert {d["classification"]["turn_kind"] for d in h.conn.drafts} == {"open"}
    assert h.conn.formal_goal_writes == []


def test_confirmed_turn_raises_the_revision_and_opens_the_gate(h):
    h.set_source()
    first = h.create(composer_draft="마일스톤 순서를 바꾸는 게 좋겠어")
    assert first["goal_change_allowed"] is False
    h.body = "확정 반영본"
    second = h.create(composer_draft="그렇게 해. 확정합니다.")
    assert second["id"] == first["id"] and second["turn_dedup"] == "revised"
    assert second["current_revision"] == 2
    assert h.conn.drafts[0]["classification"]["turn_kind"] == "confirmed"
    assert second["goal_change_allowed"] is True
    assert [r["metadata"]["turn_kind"] for r in h.conn.revisions] == ["proposal", "confirmed"]
    service.require_confirmed_turn(h.conn.drafts[0]["classification"]["turn_kind"])
    assert h.conn.formal_goal_writes == []


def test_latest_user_message_is_the_utterance_when_no_composer_text(h):
    source = h.set_source("확정합니다")
    created = h.create()
    decision = h.conn.drafts[0]["classification"]["decisions"][0]
    assert decision["source_message_id"] == str(source.messages[0]["id"])
    assert created["goal_change_allowed"] is True


def test_other_tenant_goal_is_rejected_before_any_write_or_llm_call(h):
    foreign_goal, _ = h.add_goal(tenant=TENANT_B)
    h.set_source()
    with pytest.raises(service.GoalBindingError):
        h.create(goal_id=str(foreign_goal))
    assert h.llm_calls == 0 and not h.conn.drafts and not h.conn.revisions


def test_unknown_goal_is_indistinguishable_from_foreign_goal(h):
    h.set_source()
    with pytest.raises(service.GoalBindingError) as missing:
        h.create(goal_id=str(uuid.uuid4()))
    foreign_goal, _ = h.add_goal(tenant=TENANT_B)
    with pytest.raises(service.GoalBindingError) as foreign:
        h.create(goal_id=str(foreign_goal))
    assert str(missing.value) == str(foreign.value)


def test_milestone_of_another_goal_is_rejected(h):
    goal_a, _ = h.add_goal()
    _, [milestone_of_b] = h.add_goal()
    h.set_source()
    with pytest.raises(service.GoalBindingError):
        h.create(goal_id=str(goal_a), milestone_id=str(milestone_of_b))
    assert not h.conn.drafts


def test_milestone_without_goal_is_a_validation_error(h):
    _, [milestone_id] = h.add_goal()
    h.set_source()
    with pytest.raises(ValueError):
        h.create(milestone_id=str(milestone_id))


def test_explicit_goal_and_milestone_are_recorded_as_explicit(h):
    goal_id, [milestone_id] = h.add_goal()
    h.set_source()
    h.create(goal_id=str(goal_id), milestone_id=str(milestone_id))
    classification = h.conn.drafts[0]["classification"]
    assert classification["goal_id"] == str(goal_id)
    assert classification["milestone_id"] == str(milestone_id)
    assert classification["goal_binding"] == "explicit"


def test_session_bound_goal_is_used_only_when_unambiguous(h):
    goal_id, _ = h.add_goal()
    h.set_source()
    h.conn.links.append((goal_id, str(h.source.session_id)))
    h.create()
    classification = h.conn.drafts[0]["classification"]
    assert classification["goal_id"] == str(goal_id) and classification["goal_binding"] == "session"
    assert "milestone_id" not in classification

    other_goal, _ = h.add_goal()
    h.set_source("다른 요청")
    h.conn.links.append((other_goal, str(h.source.session_id)))
    second_goal, _ = h.add_goal()
    h.conn.links.append((second_goal, str(h.source.session_id)))
    h.create()
    ambiguous = h.conn.drafts[1]["classification"]
    assert "goal_id" not in ambiguous and "goal_binding" not in ambiguous


def test_foreign_tenant_goal_linked_to_session_is_never_bound(h):
    foreign_goal, _ = h.add_goal(tenant=TENANT_B)
    h.set_source()
    h.conn.links.append((foreign_goal, str(h.source.session_id)))
    h.create()
    assert "goal_id" not in h.conn.drafts[0]["classification"]


def test_no_goal_selected_leaves_goal_fields_out(h):
    h.set_source()
    h.create()
    assert not {"goal_id", "milestone_id", "goal_binding"} & set(h.conn.drafts[0]["classification"])


# ── 원문 message → draft revision → 변경안 추적 ──────────────────────────────

class TraceConn:
    def __init__(self, conn: GoalConn, source: DraftSource):
        self.conn, self.source = conn, source

    async def fetchrow(self, query, *args):
        assert "FROM directive_drafts" in query
        draft = self.conn.drafts[0]
        draft_id, tenant = args
        if draft["id"] != draft_id or draft["tenant_id"] != tenant:
            return None
        return {**draft, "created_at": None, "classification": json.dumps(draft["classification"])}

    async def fetch(self, query, *args):
        if "FROM chat_messages" in query:
            return [
                {"id": m["id"], "session_id": self.source.session_id,
                 "tenant_id": self.conn.drafts[0]["tenant_id"], "role": m["role"],
                 "created_at": None, "preview": m["content"][:120]}
                for m in self.source.messages if m["id"] in args[0]
            ]
        if "FROM directive_draft_revisions" in query:
            return [{**r, "created_at": None, "metadata": json.dumps(r["metadata"])}
                    for r in self.conn.revisions]
        if "FROM directive_draft_events" in query:
            return [{**e, "created_at": None} for e in self.conn.events]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        assert "FROM goals" in query
        goal_id, tenant, milestone_id, _session, explicit = args
        goal = uuid.UUID(goal_id)
        return (
            self.conn.goals.get(goal) == tenant and explicit
            and (milestone_id is None
                 or self.conn.milestones.get(uuid.UUID(milestone_id), (None,))[0] == goal)
        )


def test_trace_follows_message_to_revision_to_decision(h):
    goal_id, [milestone_id] = h.add_goal()
    source = h.set_source("마일스톤 순서를 바꾸는 게 좋겠어")
    h.create(goal_id=str(goal_id), milestone_id=str(milestone_id))
    h.body = "확정본"
    h.create(goal_id=str(goal_id), milestone_id=str(milestone_id), composer_draft="그렇게 해")
    assert len(h.conn.drafts) == 1
    draft = h.conn.drafts[0]

    trace = asyncio.run(service.load_draft_trace(
        TraceConn(h.conn, h.source), tenant_id=TENANT_A, draft_id=str(draft["id"]),
    ))
    assert trace["draft"]["turn_kind"] == "confirmed"
    assert trace["draft"]["decisions"][0]["kind"] == "confirmed"
    assert [r["turn_kind"] for r in trace["revisions"]] == ["proposal", "confirmed"]
    assert trace["source_messages"][0]["id"] == str(source.messages[0]["id"])
    assert trace["draft"]["goal_id"] == str(goal_id)
    assert trace["draft"]["milestone_id"] == str(milestone_id)
    assert trace["draft"]["decisions"]
    assert [r["revision"] for r in trace["revisions"]] == list(range(1, len(h.conn.revisions) + 1))
    assert all(r["turn_kind"] for r in trace["revisions"])
    assert all(r["source_message_ids"] for r in trace["revisions"])
    assert trace["integrity"]["goal_mismatch"] == 0 and trace["linked"] is True
    assert h.conn.formal_goal_writes == []


# ── API ──────────────────────────────────────────────────────────────────────

def test_api_maps_goal_binding_error_to_409(monkeypatch):
    async def reject(**_kwargs):
        raise service.GoalBindingError("선택한 목표를 이 테넌트에서 사용할 수 없습니다.")

    monkeypatch.setattr(api.drafts, "create_draft", reject)
    context = {"tenant": {"id": TENANT_A}, "user": {}}
    body = api.DraftCreateRequest(goal_id=uuid.uuid4())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api.create_directive_draft(uuid.uuid4(), body, context))
    assert caught.value.status_code == 409


def test_api_passes_goal_and_milestone_through(monkeypatch):
    seen = {}

    async def capture(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(api.drafts, "create_draft", capture)
    goal_id, milestone_id = uuid.uuid4(), uuid.uuid4()
    asyncio.run(api.create_directive_draft(
        uuid.uuid4(), api.DraftCreateRequest(goal_id=goal_id, milestone_id=milestone_id),
        {"tenant": {"id": TENANT_A}, "user": {}},
    ))
    assert seen["goal_id"] == str(goal_id) and seen["milestone_id"] == str(milestone_id)


@pytest.mark.parametrize('mode', ['identical', 'changed', 'frozen'])
@pytest.mark.parametrize('different_milestone', [False, True])
def test_same_turn_cannot_silently_change_binding(h, mode, different_milestone):
    goal, milestones = h.add_goal(milestones=2)
    other_goal, [other_milestone] = h.add_goal()
    h.set_source()
    first = h.create(goal_id=str(goal), milestone_id=str(milestones[0]))
    if mode == 'changed':
        h.body = '변경된 생성 내용'
    elif mode == 'frozen':
        h.conn.drafts[0]['status'] = 'approved'
    calls = h.llm_calls
    with pytest.raises(service.GoalBindingError):
        h.create(
            goal_id=str(goal if different_milestone else other_goal),
            milestone_id=str(milestones[1] if different_milestone else other_milestone),
        )
    assert h.llm_calls == calls
    assert str(h.conn.drafts[0]['id']) == first['id']
    assert h.conn.drafts[0]['classification']['goal_id'] == str(goal)
    assert h.conn.drafts[0]['classification']['milestone_id'] == str(milestones[0])
    assert len(h.conn.drafts) == len(h.conn.revisions) == 1
    assert h.conn.formal_goal_writes == []
