"""목업 승인 후 다음 단계 안내 (in-memory fake, DB 없음).

fake 는 SQL 이 바인딩하는 인자(목표·tenant·역할)로 필터를 흉내 낸다. SQL 문장 자체는 모양만 검사한다.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api import mockup_reviews as router
from app.models.mockup_review import ApproveReview
from app.services import goal_manager
from app.services import mockup_approval_handoff as handoff

TENANT, GOAL, REVIEW, REVISION = uuid4(), uuid4(), uuid4(), uuid4()
S_MS, S_ROLE, S_LEAD, S_HEAD = (str(uuid4()) for _ in range(4))
EVENT = 77
NO_ACCESS_USERS: set[str] = set()


def ms(mid, title, seq, status="in_progress", linked=False, owner=None, role=None):
    return {"id": mid, "title": title, "sequence_order": seq, "status": status, "has_progress": linked,
            "owner_session_id": owner, "owner_role_key": role}


def sess(user="u1", project="AADS", name="[AADS] 작업"):
    return {"user_id": user, "project_key": project, "workspace_name": name, "workspace_display_name": None}


class Conn:
    def __init__(self, *, goal_id=GOAL, milestones=None, sessions=None, role_sessions=None, lead=S_LEAD,
                 head_session=S_HEAD, status="approved", event=EVENT):
        self.head = {"id": REVIEW, "tenant_id": TENANT, "project_key": "AADS", "title": "대시보드 목업",
                     "goal_id": goal_id, "session_id": head_session, "status": status, "approval_event_id": event,
                     "approved_revision_id": REVISION, "revision": 2}
        self.milestones = milestones if milestones is not None else [ms("m1", "화면 목업", 0, "completed"),
                                                                     ms("m2", "화면 구현", 1)]
        if milestones is not None and not any("목업" in str(row["title"]) for row in self.milestones):
            self.milestones.insert(0, ms("fixture-mockup", "화면 목업", 0, "completed"))
        # 세션 id -> 작업공간 프로젝트·소유자. 기본은 전부 AADS 프로젝트 세션.
        self.sessions = sessions if sessions is not None else {
            sid: sess() for sid in (S_MS, S_ROLE, S_LEAD, S_HEAD)}
        self.role_sessions = role_sessions or {}  # role -> 최근 갱신 순 세션 id 목록
        self.lead = lead
        self.messages: list[str] = []
        self.notes: list[tuple] = []
        self.sql: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "FROM mockup_review_heads" in sql:
            return self.head
        if "FROM chat_sessions s JOIN chat_workspaces" in sql:
            row = self.sessions.get(args[0])
            return {"id": args[0], **row} if row else None
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "FROM milestones m" in sql:
            assert args[2] == "AADS"  # 목표의 project 로 한정
            return self.milestones
        if "cs.role_key = $1" in sql:
            return [{"id": sid, **self.sessions[sid]} for sid in self.role_sessions.get(args[0], [])
                    if sid in self.sessions]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        if "FROM goals" in sql:
            return self.lead
        if "FROM chat_messages" in sql:
            return 1 if any(args[1] in m for m in self.messages) else None
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        assert sql.startswith("UPDATE milestones SET dispatch_note")
        self.notes.append(args)


@pytest.fixture
def env(monkeypatch):
    posts = []

    def make(conn, post=None):
        @asynccontextmanager
        async def acquire():
            yield conn

        monkeypatch.setattr(handoff, "get_pool", lambda: SimpleNamespace(acquire=acquire))

        async def fake_post(**kw):
            if post:
                raise post
            key = kw["idempotency_key"]
            if any(p["idempotency_key"] == key for p in posts):
                return SimpleNamespace(posted=False, skipped_reason="duplicate_idempotency_key")
            posts.append(kw)
            conn.messages.append(kw["body"])
            return SimpleNamespace(posted=True, skipped_reason="")

        monkeypatch.setattr(handoff, "post_session_report", fake_post)
        return posts

    async def no_columns(_conn):
        return {"link_state", "superseded_by"}

    async def fake_eligible(_conn, tenant, project, user_ids, access, session_owner):
        assert project == "AADS" and "read" in access
        return [u for u in user_ids if u not in NO_ACCESS_USERS]

    NO_ACCESS_USERS.clear()
    monkeypatch.setattr(goal_manager, "link_optional_columns", no_columns)
    monkeypatch.setattr(handoff, "_eligible", fake_eligible)
    return make


def run(conn):
    return asyncio.run(handoff.announce_approved_review(REVIEW, EVENT))


def test_no_goal_posts_nothing(env):
    conn = Conn(goal_id=None)
    posts = env(conn)
    assert run(conn) is False
    assert posts == [] and conn.notes == []


@pytest.mark.parametrize("label,ms_owner,role,role_map,lead,head_session,expected", [
    ("milestone owner", S_MS, "dev", {"dev": [S_ROLE]}, S_LEAD, S_HEAD, S_MS),
    ("role session", None, "dev", {"dev": [S_ROLE]}, S_LEAD, S_HEAD, S_ROLE),
    ("goal lead", None, None, {}, S_LEAD, S_HEAD, S_LEAD),
    ("head session", None, None, {}, None, S_HEAD, S_HEAD),
])
def test_session_priority(env, label, ms_owner, role, role_map, lead, head_session, expected):
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, owner=ms_owner, role=role)],
                role_sessions=role_map, lead=lead, head_session=head_session)
    posts = env(conn)
    assert run(conn) is True, label
    assert [p["session_id"] for p in posts] == [expected]
    assert conn.notes == []


def test_dead_owner_session_falls_through(env):
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, owner=S_MS)], sessions={S_LEAD: sess()})
    posts = env(conn)
    assert run(conn) is True
    assert posts[0]["session_id"] == S_LEAD


def test_role_session_from_other_project_is_never_used(env):
    # 같은 tenant·같은 role_key 라도 다른 프로젝트 작업공간 세션이 가장 최근이면 건너뛴다(지적 1).
    other = str(uuid4())
    sessions = {other: sess(project="KIS", name="[KIS] 다른 프로젝트"), S_ROLE: sess(), S_LEAD: sess()}
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, role="dev")], sessions=sessions,
                role_sessions={"dev": [other, S_ROLE]})
    posts = env(conn)
    assert run(conn) is True
    assert [p["session_id"] for p in posts] == [S_ROLE]


def test_role_session_only_in_other_project_falls_to_lead(env):
    other = str(uuid4())
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, role="dev")],
                sessions={other: sess(project="KIS", name="[KIS] 다른"), S_LEAD: sess()},
                role_sessions={"dev": [other]})
    posts = env(conn)
    assert run(conn) is True
    assert [p["session_id"] for p in posts] == [S_LEAD]
    assert all(other not in p["session_id"] for p in posts)


@pytest.mark.parametrize("field", ["owner", "lead", "head"])
def test_explicit_session_in_other_project_is_rejected(env, field):
    other = str(uuid4())
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, owner=other if field == "owner" else None)],
                sessions={other: sess(project="KIS", name="[KIS] 다른")},
                lead=other if field == "lead" else None, head_session=other if field == "head" else None)
    posts = env(conn)
    assert run(conn) is False
    assert posts == [] and conn.notes == [("m1", handoff.NO_OWNER_NOTE)]


def test_ceo_integrated_workspace_is_in_scope(env):
    s_ceo = str(uuid4())
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, owner=s_ceo)],
                sessions={s_ceo: sess(project="CEO", name="[CEO] 통합지시")})
    posts = env(conn)
    assert run(conn) is True
    assert posts[0]["session_id"] == s_ceo


def test_session_owner_without_project_access_is_skipped(env):
    # 세션 소유자에게 이 프로젝트 열람 권한이 없으면 _eligible 과 같은 기준으로 건너뛴다(지적 1).
    sessions = {S_MS: sess(user="intruder"), S_LEAD: sess(user="u1")}
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, owner=S_MS)], sessions=sessions)
    posts = env(conn)
    NO_ACCESS_USERS.add("intruder")
    assert run(conn) is True
    assert [p["session_id"] for p in posts] == [S_LEAD]


def test_role_session_owner_without_access_is_skipped(env):
    second = str(uuid4())
    sessions = {S_ROLE: sess(user="intruder"), second: sess(), S_LEAD: sess()}
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, role="dev")], sessions=sessions,
                role_sessions={"dev": [S_ROLE, second]})
    posts = env(conn)
    NO_ACCESS_USERS.add("intruder")
    assert run(conn) is True
    assert [p["session_id"] for p in posts] == [second]


def test_session_without_owner_user_is_allowed_by_project_scope(env):
    conn = Conn(milestones=[ms("m1", "화면 구현", 1, owner=S_MS)], sessions={S_MS: sess(user=None)})
    posts = env(conn)
    NO_ACCESS_USERS.add(None)
    assert run(conn) is True
    assert posts[0]["session_id"] == S_MS


def test_no_owner_anywhere_leaves_note_only(env):
    conn = Conn(lead=None, head_session=None)
    posts = env(conn)
    assert run(conn) is False
    assert posts == []
    assert conn.notes == [("m2", handoff.NO_OWNER_NOTE)]


def test_skips_milestone_with_linked_work(env):
    conn = Conn(milestones=[
        ms("m1", "화면 구현 1차", 1, linked=True),
        ms("m2", "화면 구현", 2, "pending", owner=S_MS),
        ms("m3", "검증", 3, "pending")])
    posts = env(conn)
    assert run(conn) is True
    assert "「화면 구현」" in posts[0]["body"] and "milestone_id: m2" in posts[0]["body"]
    sql = next(s for s in conn.sql if "FROM milestones m" in s)
    assert "l.task_type = 'pipeline_job'" in sql and "EXISTS" in sql
    assert "COALESCE(l.link_state, 'active') = 'active'" in sql and "l.superseded_by IS NULL" in sql
    assert "ORDER BY m.sequence_order" in sql
    assert "m.goal_id = $1::uuid" in sql and "m.tenant_id = $2::uuid" in sql
    assert "m.project = $3" in sql and "g.tenant_id = $2::uuid" in sql and "g.project = $3" in sql


def test_earlier_design_and_mockup_stages_are_not_targets(env):
    # 지적 2: 연결 작업이 없는 앞선 설계·목업 단계가 아니라 그 뒤의 구현 단계에 안내한다.
    conn = Conn(milestones=[
        ms("m1", "요구사항 설계", 1, owner="d1"),
        ms("m2", "화면 목업 제작·승인", 2, owner="d2"),
        ms("m3", "화면 구현", 3, owner=S_MS),
        ms("m4", "통합 검증", 4, "pending")])
    posts = env(conn)
    assert run(conn) is True
    assert [p["session_id"] for p in posts] == [S_MS]
    assert "milestone_id: m3" in posts[0]["body"]


def test_implementation_stage_before_mockup_stage_is_not_target(env):
    conn = Conn(milestones=[ms("m1", "기반 API 구현", 1), ms("m2", "화면 목업", 2),
                            ms("m3", "화면 구현", 3, "pending", owner=S_MS)])
    posts = env(conn)
    assert run(conn) is True
    assert "milestone_id: m3" in posts[0]["body"]


def test_post_mockup_stage_without_implementation_keyword_is_target(env):
    conn = Conn(milestones=[ms("m1", "요구사항 설계", 1), ms("m2", "화면 목업", 2), ms("m3", "통합 검증", 3)])
    posts = env(conn)
    assert run(conn) is True
    assert len(posts) == 1 and "milestone_id: m3" in posts[0]["body"]


def test_stage_picker_unit():
    pick = handoff.pick_next_milestone
    rows = [ms("a", "UI 설계", 1), ms("b", "목업", 2, "completed"), ms("c", "화면 구현", 3, "completed"),
            ms("d", "API 구현", 4, "pending")]
    assert pick(rows)["id"] == "d"
    assert pick([ms("a", "목업", 2), ms("b", "구현", 1)]) is None  # 목업 단계 앞 구현은 대상이 아니다
    assert handoff.is_implementation_stage("Frontend implement") and not handoff.is_implementation_stage("검증")


def test_target_milestone_follows_mockup_boundary_without_implementation_keyword():
    rows = [ms("1", "M1 PRD 확정·목업 2장(띠 칩, 정리 카드)", 1, "blocked"),
            ms("2", "M2 서버 판정: 휴면·P0 상한·자동승인 grants 이관·게이트 status 조건", 2, "completed"),
            ms("3", "M3 화면 표시: 칩 아이콘·정렬·자동승인 칩·정리 카드", 3, "blocked", linked=True),
            ms("4", "M4 첫 정리: P0 3개 선택·휴면 후보 일괄 카드·보류 목표 자동승인 정지", 4)]
    assert handoff.pick_next_milestone(rows)["id"] == "4"


def test_no_mockup_stage_has_no_target():
    assert handoff.pick_next_milestone([ms("1", "PRD 확정", 1), ms("2", "통합 검증", 2)]) is None


def test_pending_before_mockup_boundary_is_not_target():
    rows = [ms("1", "미연결 pending 정리", 1, "pending"), ms("2", "화면 목업", 2),
            ms("3", "후속 점검", 3, "completed")]
    assert handoff.pick_next_milestone(rows) is None


def test_multiple_mockup_boundaries_fail_closed_even_when_one_is_completed():
    rows = [ms("mockup-a", "목업 A", 1, "completed"),
            ms("impl-a", "첫 단계", 2, "pending"),
            ms("mockup-b", "목업 B", 3, "pending"),
            ms("impl-b", "두 번째 단계", 4, "pending")]
    assert handoff.pick_next_milestone(rows) is None


@pytest.mark.parametrize("approved_id", ["mockup-a", "mockup-b"])
def test_approval_does_not_guess_which_of_multiple_mockups_was_approved(env, approved_id, caplog):
    caplog.set_level(logging.INFO, logger=handoff.__name__)
    rows = [ms("mockup-a", "목업 A", 1, "pending"),
            ms("impl-a", "후속 A", 2, "pending"),
            ms("mockup-b", "목업 B", 3, "pending"),
            ms("impl-b", "후속 B", 4, "pending")]
    conn = Conn(milestones=rows)
    posts = env(conn)
    assert handoff.pick_next_milestone(conn.milestones) is None
    assert run(conn) is False
    assert posts == []
    assert "reason=ambiguous_mockup_boundary" in caplog.text


def test_single_mockup_boundary_keeps_m4_candidate():
    rows = [ms("m1", "M1 PRD 확정·목업 2장", 1, "blocked"),
            ms("m2", "M2 서버 판정", 2, "completed"),
            ms("m3", "M3 화면 표시", 3, "blocked", linked=True),
            ms("m4", "M4 첫 정리", 4)]
    assert handoff.pick_next_milestone(rows)["id"] == "m4"


def test_no_target_milestone_posts_nothing(env):
    conn = Conn(milestones=[ms("mockup", "화면 목업", 0, "completed"),
                            ms("m1", "화면 구현", 1, linked=True), ms("m2", "화면 구현 2", 2, "done")])
    posts = env(conn)
    assert run(conn) is False
    assert posts == [] and conn.notes == []


def test_same_event_posts_once(env):
    conn = Conn()
    posts = env(conn)
    assert run(conn) is True
    assert run(conn) is False
    assert len(posts) == 1
    assert handoff.marker(EVENT) in posts[0]["body"]
    assert posts[0]["idempotency_key"] == f"mockup-approved:{EVENT}"


def test_stale_approval_event_is_ignored(env):
    conn = Conn(status="revoked")
    posts = env(conn)
    assert run(conn) is False
    assert posts == []
    other = Conn(event=EVENT + 1)
    posts = env(other)
    assert run(other) is False and posts == []


def test_post_never_triggers_ai_turn(env, monkeypatch):
    called = []
    import app.services.chat_service as chat_service

    for name in ("trigger_ai_reaction", "send_message_stream"):
        monkeypatch.setattr(chat_service, name, lambda *a, **k: called.append(name), raising=False)
    conn = Conn()
    posts = env(conn)
    assert run(conn) is True
    assert posts[0]["trigger_reaction"] is False and posts[0]["plain_body"] is True
    assert called == []
    src = open(handoff.__file__, encoding="utf-8").read()
    assert "trigger_ai_reaction" not in src.replace("trigger_reaction", "") and "send_message_stream" not in src


def test_message_text_and_link(env):
    conn = Conn()
    posts = env(conn)
    run(conn)
    body = posts[0]["body"]
    assert "🖼 [목업 승인] '대시보드 목업' r2 승인됨" in body and "(자동 제출하지 않음)" in body
    assert f"review_id: {REVIEW}" in body and f"revision_id: {REVISION}" in body
    assert f"mockup_review={REVIEW}" in body and f"mockup_revision={REVISION}" in body


def test_post_failure_is_swallowed(env):
    conn = Conn()
    env(conn, post=RuntimeError("db down"))
    assert run(conn) is False


def test_approve_response_unaffected_by_handoff_failure(env, monkeypatch):
    response = {"review_id": str(REVIEW), "approval_id": EVENT, "status": "approved", "idempotent": False}
    calls = []

    async def fake_run(project_key, context, access, work):
        calls.append("approval_transaction_committed")
        return dict(response)

    monkeypatch.setattr(router, "_run", fake_run)
    async def fail_handoff(review_id, event_id):
        calls.append((review_id, event_id))
        return False

    monkeypatch.setattr(router.mockup_approval_handoff, "announce_approved_review", fail_handoff)
    body = ApproveReview.model_construct(revision_id=REVISION, manifest_hash="a" * 64, expected_generation=1,
                                         idempotency_key="k")
    out = asyncio.run(router.approve_review("AADS", REVIEW, body, context={}))
    assert out == response
    assert calls == ["approval_transaction_committed", (REVIEW, EVENT)]


def test_approve_route_skips_handoff_without_approved_event(monkeypatch):
    calls = []

    async def fake_run(project_key, context, access, work):
        return {"review_id": str(REVIEW), "status": "review_ready", "idempotent": False}

    async def announce(*args):
        calls.append(args)

    monkeypatch.setattr(router, "_run", fake_run)
    monkeypatch.setattr(router.mockup_approval_handoff, "announce_approved_review", announce)
    body = ApproveReview.model_construct(revision_id=REVISION, manifest_hash="a" * 64, expected_generation=1,
                                         idempotency_key="k")
    asyncio.run(router.approve_review("AADS", REVIEW, body, context={}))
    assert calls == []


def test_handoff_holds_review_lock_until_selection_and_post(env):
    conn = Conn()
    posts = env(conn)
    assert run(conn) is True
    head_read = next(sql for sql in conn.sql if "FROM mockup_review_heads" in sql)
    assert "FOR SHARE OF h" in head_read
    assert len(posts) == 1


def test_pool_failure_is_swallowed(monkeypatch):
    def boom():
        raise RuntimeError("no pool")

    monkeypatch.setattr(handoff, "get_pool", boom)
    assert asyncio.run(handoff.announce_approved_review(REVIEW, EVENT)) is False
