"""자동화 초안 상태 머신·비밀값 거부·테넌트 격리·화면 문장 사전 (T1)."""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import uuid
from pathlib import Path

import pytest

from app.services import automation_draft_service as svc

TENANT_A = "2d701a8c-9596-4757-8588-faa4f7837112"
TENANT_B = "7a1c3f0e-1111-4222-8333-944455556666"
FORBIDDEN_TERMS = ("work_key", "lane", "credential_scope", "session_id", "recipe", "registration", "e2e")
# 소스에 비밀값 모양 리터럴을 남기지 않기 위해 조립한다.
JWT_LIKE = "ey" + "J" + "hbGciOiJIUzI1NiJ9abcdefghij"
SK_LIKE = "sk" + "-" + "ant-oat01-abcdefghijklmnop"


class FakeDB:
    """automation_drafts / automation_draft_events 를 흉내 낸다. 쿼리의 tenant 조건도 그대로 따른다."""

    def __init__(self) -> None:
        self.drafts: dict[str, dict] = {}
        self.events: list[dict] = []
        self.in_tx = False

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return _Tx(self)

    def add(self, tenant: str, state: str) -> str:
        draft_id = str(uuid.uuid4())
        self.drafts[draft_id] = {"id": draft_id, "tenant_id": tenant, "state": state, "failure_code": None}
        return draft_id

    async def fetchrow(self, query, *args):
        text = " ".join(query.split())
        if text.startswith("INSERT INTO automation_drafts"):
            draft_id = str(uuid.uuid4())
            row = {
                "id": draft_id, "tenant_id": args[0], "owner_user": args[1], "session_id": args[2],
                "title": args[3], "intent_text": args[4], "intent": json.loads(args[5]),
                "aliases": args[6], "state": args[7], "lane": args[8], "failure_code": None,
            }
            self.drafts[draft_id] = row
            return row
        if text.startswith("UPDATE automation_drafts"):
            draft_id, tenant, from_state, to_state, failure_code = args
            row = self.drafts.get(draft_id)
            if not row or row["tenant_id"] != tenant or row["state"] != from_state:
                return None
            row.update(state=to_state, failure_code=failure_code)
            return dict(row)
        if text.startswith("SELECT state FROM automation_drafts"):
            row = self.drafts.get(args[0])
            return {"state": row["state"]} if row and row["tenant_id"] == args[1] else None
        if text.startswith("SELECT * FROM automation_drafts"):
            row = self.drafts.get(args[0])
            return dict(row) if row and row["tenant_id"] == args[1] else None
        raise AssertionError(f"unexpected fetchrow: {text[:80]}")

    async def execute(self, query, *args):
        assert "INSERT INTO automation_draft_events" in query
        draft_id, event_type, *rest = args
        if len(rest) == 4:  # transition: from, to, actor, detail
            from_state, to_state, actor, detail = rest
        else:  # create: to, actor, detail
            from_state, (to_state, actor, detail) = None, rest
        self.events.append({
            "draft_id": draft_id, "event_type": event_type, "from_state": from_state,
            "to_state": to_state, "actor": actor, "detail": json.loads(detail),
        })

    async def fetch(self, query, *args):
        draft_id, tenant = args
        row = self.drafts.get(draft_id)
        if not row or row["tenant_id"] != tenant:
            return []
        return [e for e in self.events if e["draft_id"] == draft_id]


class _Tx:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    async def __aenter__(self):
        self.snapshot = ({k: dict(v) for k, v in self.db.drafts.items()}, list(self.db.events))

    async def __aexit__(self, exc_type, *_):
        if exc_type:  # 롤백
            self.db.drafts, self.db.events = self.snapshot
        return False


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(svc, "get_pool", lambda: fake)
    monkeypatch.setenv(svc.DRAFTS_FLAG, "true")
    return fake


def run(coro):
    return asyncio.run(coro)


def go(db, draft_id, frm, to, *, tenant=TENANT_A, **kw):
    return run(svc.transition(draft_id, frm, to, "tester", "테스트", tenant_id=tenant, **kw))


# ── 전이표 ────────────────────────────────────────────────────────────────

def test_design_has_thirteen_states_and_transition_table_matches():
    assert len(svc.STATES) == 13
    assert svc.INITIAL_STATE in svc.STATES
    assert set(svc.TERMINAL_STATES) == {"cancelled", "expired", "rejected", "broken", "paused"}
    for src, targets in svc.TRANSITIONS.items():
        for dst in targets:
            assert dst in svc.ALL_STATES
    assert sum(len(t) for t in svc.TRANSITIONS.values()) == 28


ALLOWED = [(src, dst) for src, targets in svc.TRANSITIONS.items() for dst in targets]
FORBIDDEN = [
    (src, dst) for src, dst in itertools.product(svc.ALL_STATES, svc.ALL_STATES)
    if dst not in svc.TRANSITIONS.get(src, ())
]


@pytest.mark.parametrize("src,dst", ALLOWED)
def test_every_allowed_transition_updates_state_and_records_event(db, src, dst):
    draft_id = db.add(TENANT_A, src)
    row = go(db, draft_id, src, dst)
    assert row["state"] == dst
    assert db.drafts[draft_id]["state"] == dst
    assert len(db.events) == 1
    event = db.events[0]
    assert (event["from_state"], event["to_state"], event["actor"]) == (src, dst, "tester")
    assert event["detail"]["reason"] == "테스트"


@pytest.mark.parametrize("src,dst", FORBIDDEN)
def test_every_other_pair_is_rejected_without_touching_db(db, src, dst):
    draft_id = db.add(TENANT_A, src)
    with pytest.raises(svc.InvalidTransition):
        go(db, draft_id, src, dst)
    assert db.drafts[draft_id]["state"] == src
    assert db.events == []


def test_unknown_states_are_invalid(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    for frm, to in (("nope", "active"), ("intent_draft", "nope")):
        with pytest.raises(svc.InvalidTransition):
            go(db, draft_id, frm, to)


def test_terminal_states_have_no_exit(db):
    for state in svc.TERMINAL_STATES:
        assert svc.TRANSITIONS.get(state, ()) == ()


# ── 경합(409) ─────────────────────────────────────────────────────────────

def test_second_click_gets_conflict_and_leaves_one_event(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    go(db, draft_id, "intent_draft", "intent_confirmed")
    with pytest.raises(svc.TransitionConflict) as err:
        go(db, draft_id, "intent_draft", "intent_confirmed")
    assert err.value.expected == "intent_draft"
    assert err.value.actual == "intent_confirmed"
    assert len(db.events) == 1


def test_conflict_when_state_moved_to_a_different_branch(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    go(db, draft_id, "intent_draft", "cancelled")
    with pytest.raises(svc.TransitionConflict):
        go(db, draft_id, "intent_draft", "intent_confirmed")
    assert db.drafts[draft_id]["state"] == "cancelled"


def test_concurrent_transitions_only_one_wins(db):
    draft_id = db.add(TENANT_A, "intent_draft")

    async def both():
        return await asyncio.gather(
            svc.transition(draft_id, "intent_draft", "intent_confirmed", "a", "x", tenant_id=TENANT_A),
            svc.transition(draft_id, "intent_draft", "cancelled", "b", "y", tenant_id=TENANT_A),
            return_exceptions=True,
        )

    results = run(both())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, svc.TransitionConflict) for r in results) == 1
    assert len(db.events) == 1


def test_event_failure_rolls_back_state(db, monkeypatch):
    draft_id = db.add(TENANT_A, "intent_draft")

    async def boom(*a, **k):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(db, "execute", boom)
    with pytest.raises(RuntimeError):
        go(db, draft_id, "intent_draft", "intent_confirmed")
    assert db.drafts[draft_id]["state"] == "intent_draft"


# ── 실패 분류 ────────────────────────────────────────────────────────────

def test_failure_code_is_kept_only_while_failed(db):
    draft_id = db.add(TENANT_A, "demonstrating")
    row = go(db, draft_id, "demonstrating", "failed", failure_code="auth_failed")
    assert row["failure_code"] == "auth_failed"
    assert db.events[0]["detail"]["failure_code"] == "auth_failed"
    row = go(db, draft_id, "failed", "credential_ready")
    assert row["failure_code"] is None


def test_unknown_failure_code_is_rejected(db):
    draft_id = db.add(TENANT_A, "demonstrating")
    with pytest.raises(svc.InvalidTransition):
        go(db, draft_id, "demonstrating", "failed", failure_code="made_up")


# ── 테넌트 격리 ──────────────────────────────────────────────────────────

def test_other_tenant_cannot_transition_or_read(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    with pytest.raises(svc.DraftNotFound):
        go(db, draft_id, "intent_draft", "intent_confirmed", tenant=TENANT_B)
    assert db.drafts[draft_id]["state"] == "intent_draft"
    assert db.events == []
    assert run(svc.get_draft(draft_id, tenant_id=TENANT_B)) is None
    assert run(svc.get_draft(draft_id, tenant_id=TENANT_A))["id"] == draft_id


def test_unknown_draft_is_not_found(db):
    with pytest.raises(svc.DraftNotFound):
        go(db, str(uuid.uuid4()), "intent_draft", "cancelled")


def test_events_are_tenant_scoped(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    go(db, draft_id, "intent_draft", "cancelled")
    assert run(svc.list_events(draft_id, tenant_id=TENANT_B)) == []
    assert len(run(svc.list_events(draft_id, tenant_id=TENANT_A))) == 1


def test_every_query_has_a_tenant_condition():
    source = Path(svc.__file__).read_text(encoding="utf-8")
    for statement in re.findall(r'"""\s*((?:SELECT|UPDATE).*?)"""', source, flags=re.S):
        assert "tenant_id" in statement, statement[:60]
    assert re.search(r'"SELECT \* FROM automation_drafts WHERE[^"]*tenant_id', source)
    assert re.search(r'"SELECT state FROM automation_drafts WHERE[^"]*tenant_id', source)


# ── 생성 ─────────────────────────────────────────────────────────────────

def test_create_draft_starts_at_intent_draft_with_event(db):
    draft = run(svc.create_draft(
        tenant_id=TENANT_A, owner_user="kim", title="쿠팡이츠 어제 매출 받기",
        intent_text="쿠팡이츠 어제 매출 받아줘", intent={"domain": "store.coupangeats.com"},
    ))
    assert draft["state"] == "intent_draft"
    assert [e["event_type"] for e in db.events] == ["draft_created"]
    assert db.events[0]["actor"] == "kim"


def test_create_draft_requires_feature_flag(db, monkeypatch):
    monkeypatch.delenv(svc.DRAFTS_FLAG)
    with pytest.raises(svc.DraftsDisabled):
        run(svc.create_draft(tenant_id=TENANT_A, owner_user="kim", title="t", intent_text="x"))
    assert db.drafts == {}


def test_flags_default_to_false(monkeypatch):
    monkeypatch.delenv(svc.DRAFTS_FLAG, raising=False)
    monkeypatch.delenv(svc.AUTO_APPROVE_READ_FLAG, raising=False)
    assert svc.drafts_enabled() is False
    assert svc.auto_approve_read_enabled() is False
    monkeypatch.setenv(svc.AUTO_APPROVE_READ_FLAG, "1")
    assert svc.auto_approve_read_enabled() is True
    monkeypatch.setenv(svc.AUTO_APPROVE_READ_FLAG, "false")
    assert svc.auto_approve_read_enabled() is False


# ── 비밀값 마스킹 거부 ───────────────────────────────────────────────────

SECRET_DETAILS = [
    {"note": f"Authorization: Bearer {JWT_LIKE}"},
    {"cookie": "sid=abc123"},
    {"nested": {"steps": [{"password": "hunter2"}]}},
    {"msg": "password=hunter2"},
    {"msg": "token: abcdef"},
    {"msg": f"값 {JWT_LIKE}"},
    {"msg": SK_LIKE},
    {"msg": "비밀번호: hunter2"},
    {"msg": "쿠키=abc"},
    {"headers": "Set-Cookie: a=b"},
    {"cookies": [{"name": "a", "value": "b"}]},
]


@pytest.mark.parametrize("detail", SECRET_DETAILS)
def test_secret_in_detail_is_refused_and_nothing_is_stored(db, detail):
    draft_id = db.add(TENANT_A, "intent_draft")
    with pytest.raises(svc.SecretDetected) as err:
        go(db, draft_id, "intent_draft", "intent_confirmed", detail=detail)
    assert "hunter2" not in str(err.value) and JWT_LIKE not in str(err.value)
    assert db.drafts[draft_id]["state"] == "intent_draft"
    assert db.events == []


def test_secret_in_reason_is_refused(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    with pytest.raises(svc.SecretDetected):
        run(svc.transition(draft_id, "intent_draft", "cancelled", "t", "password=hunter2", tenant_id=TENANT_A))
    assert db.events == []


def test_secret_references_and_plain_text_are_allowed(db):
    draft_id = db.add(TENANT_A, "intent_draft")
    go(db, draft_id, "intent_draft", "intent_confirmed", detail={
        "step": {"action": "fill", "password": "{{secret.password}}", "selector": "#pw"},
        "credential_id": str(uuid.uuid4()), "token_count": 3, "rows": 5,
    })
    assert len(db.events) == 1


def test_create_draft_refuses_secret_in_input(db):
    with pytest.raises(svc.SecretDetected):
        run(svc.create_draft(
            tenant_id=TENANT_A, owner_user="kim", title="t", intent_text="로그인은 password=hunter2",
        ))
    assert db.drafts == {}


# ── 화면 문장 사전 ───────────────────────────────────────────────────────

def test_every_state_has_a_human_card():
    for state in svc.ALL_STATES:
        card = svc.build_card(state, title="쿠팡이츠 어제 매출 받기")
        assert card["headline"] and card["body"] and card["state"] == state
        assert card["title"] == "쿠팡이츠 어제 매출 받기"
        for button in card["buttons"]:
            assert button["id"] and button["label"]


def test_headlines_follow_design_table():
    expected = {
        "intent_draft": "이렇게 이해했어요", "intent_confirmed": "로그인 연결",
        "credential_waiting": "로그인 정보를 기다리는 중", "credential_ready": "대신 해 보는 중",
        "demonstrating": "대신 해 보는 중", "paused_risk": "확인이 필요해요",
        "result_review": "이 결과가 맞나요?", "revising": "다시 해 보는 중",
        "manual_record": "직접 보여주세요", "registering": "저장 중",
        "pending_approval": "확인 기다리는 중", "active": "사용 가능",
    }
    for state, text in expected.items():
        assert svc.STATE_HEADLINES[state] == text


def test_failed_card_uses_sentence_per_cause():
    bodies = {svc.build_card("failed", failure_code=code)["body"] for code in svc.FAILURE_CODES}
    assert len(bodies) == len(svc.FAILURE_CODES)
    assert svc.build_card("failed")["body"] == svc.GENERIC_FAILURE_BODY
    assert set(svc.FAILURE_BODIES) == set(svc.FAILURE_CODES)


def test_card_for_unknown_state_is_rejected():
    with pytest.raises(svc.InvalidTransition):
        svc.build_card("nope")


def test_paused_risk_card_offers_proceed_stop_cancel():
    ids = [b["id"] for b in svc.build_card("paused_risk")["buttons"]]
    assert ids == ["proceed", "stop_here", "cancel"]


def test_human_sentences_have_no_forbidden_terms():
    sentences = svc.human_sentences()
    assert len(sentences) > 40
    for sentence in sentences:
        lowered = sentence.lower()
        for term in FORBIDDEN_TERMS:
            assert term not in lowered, (term, sentence)


def test_built_cards_have_no_forbidden_terms():
    for state in svc.ALL_STATES:
        for code in (None, *svc.FAILURE_CODES) if state == "failed" else (None,):
            card = svc.build_card(state, failure_code=code)
            text = " ".join([card["headline"], card["body"], *(b["label"] for b in card["buttons"])]).lower()
            assert not [t for t in FORBIDDEN_TERMS if t in text]


# ── 마이그레이션 ─────────────────────────────────────────────────────────

def test_migration_is_idempotent_and_never_drops():
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "20261014_automation_drafts.sql").read_text(
        encoding="utf-8"
    )
    statements = "\n".join(line.split("--")[0] for line in sql.splitlines())
    assert "DROP" not in statements.upper()
    for create in re.findall(r"CREATE (?:UNIQUE )?(?:TABLE|INDEX)[^\n]*", sql):
        assert "IF NOT EXISTS" in create, create
    assert "automation_drafts" in sql and "automation_draft_events" in sql
    assert "tenant_id       uuid NOT NULL" in sql
