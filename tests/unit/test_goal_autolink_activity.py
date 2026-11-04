"""M2b — 러너 제출 시 목표 자동연결(I-3) + 🔄·⏳·미연결 판정 API.

PRD goal-priority-dormancy-autoapproval-display-prd rev5 §6-1·§7 M2b.
DB 는 쓰지 않는다 — 경계(커넥션)만 대역으로 바꾸고, 연결 규칙(`_link_job_to_goal_explicit`)·
자동연결(`autolink_job`)·판정(`goal_activity`)·라우터 본문은 그대로 돈다.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import goal_activity as ga
from app.services import goal_autolink as gal
from app.services.goal_binding import BIND_SOURCE_EXPLICIT_DIRECTIVE, BIND_SOURCE_SESSION_SINGLE

REPO = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "22222222-2222-4222-8222-222222222222"
GOAL_A = "11111111-1111-4111-8111-111111111111"
GOAL_B = "11111111-1111-4111-8111-222222222222"
GOAL_X = "99999999-9999-4999-8999-999999999999"
MS_A = "55555555-5555-4555-8555-555555555551"
MS_B = "55555555-5555-4555-8555-555555555552"
MS_X = "55555555-5555-4555-8555-5555555555ff"
JOB = "runner-test0001"


class World:
    """goals/milestones/pipeline_jobs 를 메모리로 흉내 내는 커넥션 겸 풀."""

    def __init__(self):
        self.goals: dict[str, dict] = {}
        self.milestones: dict[str, dict] = {}
        self.candidates: list[dict] = []
        self.job_updates: list[tuple] = []
        self.links: list[tuple] = []
        self.log_events: list[tuple] = []
        self.job_states: list[dict] = []
        self.chat_goals: list[str] = []
        self.activity_rows: list[dict] = []
        self.unlinked = 0
        self.fail_activity = False
        self.fail_milestones = False
        self.dep_jobs: dict[str, dict] = {}
        self.active_jobs: list[dict] = []
        self.inserts: list[tuple] = []

    def add_goal(self, goal_id, *, project="AADS", status="active", tenant=TENANT):
        self.goals[goal_id] = {
            "id": goal_id, "tenant_id": tenant, "project": project, "status": status,
        }

    def add_milestone(self, ms_id, goal_id, *, status="in_progress", project="AADS", seq=1):
        self.milestones[ms_id] = {
            "goal_id": goal_id, "status": status, "project": project, "seq": seq,
        }

    # pool / conn
    def acquire(self):
        world = self

        class _Ctx:
            async def __aenter__(self):
                return world

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    def transaction(self):
        return self.acquire()

    async def fetchrow(self, query, *args):
        if "FROM goals WHERE id" in query:
            return self.goals.get(args[0])
        if "FROM milestones WHERE id" in query:
            if self.fail_milestones:
                raise RuntimeError("milestones lookup down")
            ms = self.milestones.get(args[0])
            return {"milestone_id": args[0], "goal_id": ms["goal_id"]} if ms else None
        if "sequence_order" in query:
            ms = self.milestones.get(args[0])
            if not ms or ms["goal_id"] != args[1]:
                return None
            return {"goal_id": args[1], "milestone_id": args[0], "sequence_order": ms["seq"]}
        if "SELECT job_id, status, instruction FROM pipeline_jobs" in query:
            return self.dep_jobs.get(args[0])
        return None

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM goals" in query:
            return 1 if args[0] in self.goals else None
        if "SELECT tenant_id::text FROM pipeline_jobs" in query:
            return TENANT
        if "FROM milestones" in query:
            ms = self.milestones.get(args[0])
            return ms["status"] if ms and ms["goal_id"] == args[1] else None
        if "FROM chat_sessions WHERE id" in query:
            return 1
        if "FROM pipeline_jobs j" in query and "goal_id IS NULL" in query:
            return self.unlinked
        return None

    async def fetch(self, query, *args):
        if "SELECT DISTINCT ON (ms.id)" in query:
            return self.candidates
        if "actual_changed_files" in query and "status = ANY($3::text[])" in query:
            return self.active_jobs
        if self.fail_activity and ("pipeline_jobs" in query or "chat_messages" in query):
            raise RuntimeError("activity down")
        if "ANY($2::text[])" in query and "pipeline_jobs j" in query:
            return self.job_states
        if "chat_messages m" in query:
            return [{"goal_id": g} for g in self.chat_goals]
        if "WITH g AS" in query:
            return self.activity_rows
        if "FROM chat_sessions s" in query:
            return []
        return []

    async def execute(self, query, *args):
        if "UPDATE pipeline_jobs SET goal_id" in query:
            self.job_updates.append(args)
            return "UPDATE 1"
        if "INSERT INTO goal_task_links" in query:
            self.links.append(args)
            return "INSERT 0 1"
        if "INSERT INTO pipeline_jobs" in query:
            self.inserts.append(args)
            return "INSERT 0 1"
        if "goal_autolink" in query:
            self.log_events.append(args)
        return "UPDATE 1"


@pytest.fixture
def world(monkeypatch):
    import app.core.db_pool as db_pool

    w = World()
    monkeypatch.setattr(db_pool, "get_pool", lambda: w)
    return w


def _autolink(**kwargs):
    kwargs.setdefault("session_id", SESSION)
    kwargs.setdefault("tenant_id", TENANT)
    return asyncio.run(gal.autolink_job(JOB, "AADS", **kwargs))


def _cand(goal, milestone):
    return {"goal_id": goal, "milestone_id": milestone}


# ── I-3 연결 우선순위 (a)>(b)>(c)>(d) ───────────────────────────────────────
def test_directive_submission_fills_goal_and_milestone(world):
    """완료 기준: GOAL_ID 줄이 있는 새 제출이 pipeline_jobs.goal_id/milestone_id 를 채운다."""
    world.add_goal(GOAL_A)
    world.add_milestone(MS_A, GOAL_A)
    result = _autolink(instruction=f"TASK\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n")
    assert (result.goal_id, result.milestone_id, result.source) == (GOAL_A, MS_A, "directive")
    assert world.job_updates and world.job_updates[0][:3] == (JOB, GOAL_A, MS_A)
    assert world.links[0][4] == BIND_SOURCE_EXPLICIT_DIRECTIVE
    # 연결 출처가 pipeline_jobs.logs 에 남는다.
    assert world.log_events and world.log_events[0][:2] == (JOB, "directive")


def test_explicit_argument_beats_session(world):
    world.add_goal(GOAL_A)
    world.add_goal(GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(goal_id=GOAL_A, instruction="x")
    assert (result.goal_id, result.source) == (GOAL_A, "explicit")
    assert world.job_updates[0][1] == GOAL_A


def test_argument_conflicting_with_directive_goal_is_not_linked_and_not_guessed(world):
    """인자와 지시서가 다른 목표를 가리키면 어느 쪽에도, 세션 후보에도 붙이지 않는다."""
    world.add_goal(GOAL_A)
    world.add_goal(GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(goal_id=GOAL_A, instruction=f"GOAL_ID: {GOAL_B}\n")
    assert (result.source, result.reason) == ("none", gal.REASON_EXPLICIT_REJECTED)
    assert world.job_updates == [] and world.links == []


def test_directive_beats_session_single(world):
    world.add_goal(GOAL_A)
    world.add_goal(GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(instruction=f"GOAL_ID: {GOAL_A}\n")
    assert (result.goal_id, result.source) == (GOAL_A, "directive")


def test_session_single_in_progress_milestone_links_with_own_bind_source(world):
    world.add_goal(GOAL_B)
    world.add_milestone(MS_B, GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(instruction="근거 없는 지시")
    assert (result.goal_id, result.milestone_id, result.source) == (GOAL_B, MS_B, "session_single")
    assert world.job_updates[0][:3] == (JOB, GOAL_B, MS_B)
    assert world.links[0][4] == BIND_SOURCE_SESSION_SINGLE


def test_ambiguous_session_candidates_stay_unlinked(world):
    world.add_goal(GOAL_A)
    world.add_goal(GOAL_B)
    world.candidates = [_cand(GOAL_A, MS_A), _cand(GOAL_B, MS_B)]
    result = _autolink(instruction="근거 없는 지시")
    assert (result.goal_id, result.source, result.reason) == (None, "none", gal.REASON_AMBIGUOUS)
    assert world.job_updates == [] and world.links == []


@pytest.mark.parametrize("session_id", [None, "not-a-uuid"])
def test_no_usable_session_means_unlinked(world, session_id):
    world.candidates = [_cand(GOAL_A, MS_A)]
    result = _autolink(instruction="x", session_id=session_id)
    assert (result.source, result.reason) == ("none", gal.REASON_NO_SESSION)
    assert world.job_updates == []


def test_no_candidates_means_unlinked(world):
    result = _autolink(instruction="x")
    assert (result.source, result.reason) == ("none", gal.REASON_NO_CANDIDATE)
    assert world.job_updates == []


def test_nonexistent_directive_uuid_is_ignored_and_falls_through(world):
    world.add_goal(GOAL_B)
    world.add_milestone(MS_B, GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(instruction=f"GOAL_ID: {GOAL_X}\nMILESTONE_ID: {MS_X}\n")
    assert (result.goal_id, result.source) == (GOAL_B, "session_single")


def test_nonexistent_directive_uuid_alone_leaves_job_unlinked(world):
    result = _autolink(instruction=f"GOAL_ID: {GOAL_X}\n", session_id=None)
    assert result.source == "none" and world.job_updates == []


def test_existing_but_rejected_directive_goal_fails_closed(world):
    """다른 프로젝트 목표를 지시서가 가리키면 세션 후보로 갈아타지 않는다 — 지시를 뒤집는 추정이다."""
    world.add_goal(GOAL_A, project="KIS")
    world.add_goal(GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(instruction=f"GOAL_ID: {GOAL_A}\n")
    assert (result.source, result.reason) == ("none", gal.REASON_EXPLICIT_REJECTED)
    assert world.job_updates == []


FOOD_GOAL = "f00d0916-0916-4a01-9a01-000000000001"
FOOD_MS = "f00d0916-0916-4a01-9a01-000000000011"


def test_directive_links_aads_job_to_food_goal_through_same_validation(world):
    world.add_goal(FOOD_GOAL, project="FOOD")
    world.add_milestone(FOOD_MS, FOOD_GOAL, project="FOOD")
    result = _autolink(instruction=f"GOAL_ID: {FOOD_GOAL}\nMILESTONE_ID: {FOOD_MS}\n")
    assert (result.goal_id, result.milestone_id, result.source) == (FOOD_GOAL, FOOD_MS, "directive")
    assert world.job_updates[0][:3] == (JOB, FOOD_GOAL, FOOD_MS)
    assert world.links[0][0] == TENANT and world.links[0][4] == BIND_SOURCE_EXPLICIT_DIRECTIVE


def test_explicit_argument_links_aads_job_to_food_goal(world):
    world.add_goal(FOOD_GOAL, project="FOOD")
    world.add_milestone(FOOD_MS, FOOD_GOAL, project="FOOD")
    result = _autolink(goal_id=FOOD_GOAL, milestone_id=FOOD_MS, instruction="x")
    assert (result.goal_id, result.milestone_id, result.source) == (FOOD_GOAL, FOOD_MS, "explicit")


def test_food_goal_of_other_tenant_fails_closed_without_session_fallback(world):
    world.add_goal(FOOD_GOAL, project="FOOD", tenant="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    world.add_goal(GOAL_B)
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(instruction=f"GOAL_ID: {FOOD_GOAL}\n")
    assert world.job_updates == []
    assert result.source == "none"


def test_food_goal_is_never_picked_by_session_inference(world):
    # 세션 후보 SQL 은 job.project 에 묶여 있어 AADS 작업이 FOOD 목표를 암묵 선택할 수 없다.
    assert "g.project = $3" in gal.SESSION_CANDIDATES_SQL and "ms.project = $3" in gal.SESSION_CANDIDATES_SQL
    result = _autolink(instruction="FOOD 인사 작업이지만 메타데이터는 없다")
    assert world.job_updates == [] and result.source == "none"


def test_closed_session_candidate_is_rejected_by_the_same_validation(world):
    world.add_goal(GOAL_B, status="completed")
    world.candidates = [_cand(GOAL_B, MS_B)]
    result = _autolink(instruction="x")
    assert (result.source, result.reason) == ("none", gal.REASON_CANDIDATE_REJECTED)


def test_link_failure_never_raises_into_submission(world, monkeypatch):
    import app.services.pipeline_runner_service as prs

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(prs, "_link_job_to_goal_explicit", boom)
    result = _autolink(instruction=f"GOAL_ID: {GOAL_A}\n")
    assert (result.source, result.reason) == ("none", gal.REASON_ERROR)


def test_pick_single_counts_distinct_milestones():
    assert gal.pick_single([]) is None
    assert gal.pick_single([_cand(GOAL_A, MS_A)]) == (GOAL_A, MS_A)
    assert gal.pick_single([_cand(GOAL_A, MS_A), _cand(GOAL_A, MS_A)]) == (GOAL_A, MS_A)
    assert gal.pick_single([_cand(GOAL_A, MS_A), _cand(GOAL_A, MS_B)]) is None
    assert gal.pick_single([_cand(GOAL_A, None)]) is None


def test_session_candidate_sql_encodes_the_lead_vs_assignee_rule():
    sql = gal.SESSION_CANDIDATES_SQL
    assert "ms.status = 'in_progress'" in sql
    assert "g.status IN ('draft', 'active')" in sql and "g.paused_at IS NULL" in sql
    assert "g.project = $3" in sql
    assert "g.owner_session_id = s.id" in sql and "ms.owner_session_id = s.id" in sql


# ── 제출 진입점: submit_job / submit_batch 가 실제로 연결한다 ────────────────
def _runner_env(monkeypatch, world, real_order=False):
    import app.api.pipeline_runner as pr

    async def noop(*_a, **_k):
        return None

    async def not_locked(*_a, **_k):
        return False

    async def model(*_a, **_k):
        return "claude-sonnet"

    async def no_orphans(*_a, **_k):
        return []

    async def order(*_a, **_k):
        return (0, 0, 0)

    stubbed = ["_lock_instruction_hash", "_cleanup_dead_local_runner_processes", "_find_active_duplicate"]
    if not real_order:
        stubbed.append("_find_active_file_conflict")
    for name in stubbed:
        monkeypatch.setattr(pr, name, noop)
    monkeypatch.setattr(pr, "_enforce_owner_resolved_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(pr, "check_project_lock", not_locked)
    monkeypatch.setattr(pr, "_get_model_for_size", model)
    monkeypatch.setattr(pr, "_relink_superseded_orphans", no_orphans)
    if not real_order:
        monkeypatch.setattr(pr, "_resolve_milestone_order", order)

    async def no_gate(*_a, **_k):
        return None

    import app.services.canonical_gate as cg

    monkeypatch.setattr(cg, "check_runner_submit", no_gate)
    return pr


_CTX = {"tenant": {"id": TENANT}, "user": {"user_id": "u1"}}


def test_submit_job_with_directive_persists_goal_and_milestone(world, monkeypatch):
    pr = _runner_env(monkeypatch, world)
    world.add_goal(GOAL_A)
    world.add_milestone(MS_A, GOAL_A)
    req = pr.JobSubmitRequest(
        project="AADS", session_id=SESSION,
        instruction=f"작업\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n",
    )
    resp = asyncio.run(pr.submit_job(req, context=_CTX))
    assert resp.status == "queued" and GOAL_A in resp.message
    assert any(u[1] == GOAL_A and u[2] == MS_A for u in world.job_updates)


# ── 제출 게이트 ↔ 연결 ↔ 순서 검사: 검증한 (goal_id, milestone_id) 가 끝까지 같다 ──────────
MS_A2 = "55555555-5555-4555-8555-555555555553"  # 목표 A 의 후순위(sequence 2) 마일스톤
MODES = ("single", "batch")
BIND_API = "explicit_api"
BIND_DIRECTIVE = BIND_SOURCE_EXPLICIT_DIRECTIVE


def _seed_goals(world):
    world.add_goal(GOAL_A)
    world.add_goal(GOAL_B)
    world.add_milestone(MS_A, GOAL_A, seq=1)
    world.add_milestone(MS_A2, GOAL_A, seq=2)
    world.add_milestone(MS_B, GOAL_B, seq=1)


def _shapes(goal, ms):
    """같은 (goal, ms) 를 인자/지시서/보충으로 다르게 표기한 제출 입력. 값: (지시서, goal 인자, ms 인자, 기대 bind_source)."""
    return {
        "api_milestone+directive_goal": (f"작업\nGOAL_ID: {goal}\n", "", ms, BIND_DIRECTIVE),
        "milestone_only_directive": (f"작업\nMILESTONE_ID: {ms}\n", "", "", BIND_DIRECTIVE),
        "milestone_only_api": ("작업", "", ms, BIND_API),
        "api_pair": ("작업", goal, ms, BIND_API),
        "api_goal+directive_milestone": (f"작업\nMILESTONE_ID: {ms}\n", goal, "", BIND_API),
        "directive_pair": (f"작업\nGOAL_ID: {goal}\nMILESTONE_ID: {ms}\n", "", "", BIND_DIRECTIVE),
    }


def _shape_params(goal, ms):
    return [pytest.param(*v, id=k) for k, v in _shapes(goal, ms).items()]


def _submit(pr, mode, instruction, *, goal_id="", milestone_id="", **extra):
    """같은 입력을 단건(submit_job) 또는 배치(submit_batch, 1건) 로 낸다."""
    if mode == "single":
        req = pr.JobSubmitRequest(
            project="AADS", session_id=SESSION, instruction=instruction,
            goal_id=goal_id, milestone_id=milestone_id, **extra,
        )
        return asyncio.run(pr.submit_job(req, context=_CTX))
    item = pr.BatchJobItem(key="A", instruction=instruction, goal_id=goal_id, milestone_id=milestone_id)
    req = pr.BatchSubmitRequest(project="AADS", session_id=SESSION, jobs=[item])
    return asyncio.run(pr.submit_batch(req, context=_CTX))


def _denied(pr, mode, instruction, status, code, **kwargs):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _submit(pr, mode, instruction, **kwargs)
    assert exc.value.status_code == status
    detail = exc.value.detail
    assert code in (detail["code"] if isinstance(detail, dict) else detail)
    return exc.value


def _assert_nothing_written(world):
    assert world.inserts == [] and world.job_updates == [] and world.links == []


def _assert_linked(world, goal, milestone, bind_source=None):
    """pipeline_jobs(goal_id, milestone_id) 와 goal_task_links 양쪽에 같은 검증 쌍이 저장됐다."""
    assert world.job_updates and all(u[1:3] == (goal, milestone) for u in world.job_updates)
    assert world.links and world.links[-1][1:3] == (goal, milestone)
    if bind_source:
        assert world.links[-1][4] == bind_source


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("instruction,goal_id,milestone_id,bind_source", _shape_params(GOAL_A, MS_A))
def test_verified_pair_is_stored_in_job_and_link_for_every_input_shape(
    world, monkeypatch, mode, instruction, goal_id, milestone_id, bind_source,
):
    """리뷰 지적 1·2: 인자/지시서 혼합 입력이어도 milestone_id 가 NULL 로 떨어지지 않는다."""
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    _submit(pr, mode, instruction, goal_id=goal_id, milestone_id=milestone_id)
    _assert_linked(world, GOAL_A, MS_A, bind_source)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("instruction,goal_id,milestone_id", [
    pytest.param(f"작업\nGOAL_ID: {GOAL_B}\nMILESTONE_ID: {MS_A}\n", GOAL_A, "", id="goal_conflict_ms_of_api_goal"),
    pytest.param(f"작업\nGOAL_ID: {GOAL_B}\n", GOAL_A, MS_A, id="goal_conflict"),
    pytest.param(f"작업\nMILESTONE_ID: {MS_A2}\n", GOAL_A, MS_A, id="milestone_conflict"),
])
def test_conflicting_argument_and_directive_ids_are_rejected_before_any_write(
    world, monkeypatch, mode, instruction, goal_id, milestone_id,
):
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    err = _denied(pr, mode, instruction, 422, "goal.id_conflict_on_submit",
                  goal_id=goal_id, milestone_id=milestone_id)
    assert "서로 다릅니다" in err.detail["message"]
    _assert_nothing_written(world)


@pytest.mark.parametrize("mode", MODES)
def test_goal_without_milestone_is_rejected_before_any_job_row(world, monkeypatch, mode):
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    err = _denied(pr, mode, f"작업\nGOAL_ID: {GOAL_A}\n", 422, "goal.milestone_id_missing_on_submit")
    assert "MILESTONE_ID 가 없습니다" in err.detail["message"]
    _assert_nothing_written(world)


@pytest.mark.parametrize("mode", MODES)
def test_milestone_of_other_goal_is_rejected_before_any_job_row(world, monkeypatch, mode):
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    _denied(pr, mode, f"작업\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_B}\n", 422,
            "goal.milestone_goal_mismatch_on_submit")
    _assert_nothing_written(world)


@pytest.mark.parametrize("mode", MODES)
def test_milestone_lookup_failure_aborts_before_any_write_then_resubmit_succeeds(world, monkeypatch, mode):
    """명시 ID 의 마일스톤 조회 예외는 통과가 아니다 — 503 으로 중단하고, DB 복구 뒤 같은 요청이 성공한다."""
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    wrong_pair = f"작업\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_B}\n"
    good_pair = f"작업\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n"
    world.fail_milestones = True
    for instruction in (wrong_pair, good_pair):
        err = _denied(pr, mode, instruction, 503, "goal.milestone_lookup_failed_on_submit")
        assert err.headers and err.headers.get("Retry-After")
        _assert_nothing_written(world)
    world.fail_milestones = False
    _denied(pr, mode, wrong_pair, 422, "goal.milestone_goal_mismatch_on_submit")
    _assert_nothing_written(world)
    _submit(pr, mode, good_pair)
    _assert_linked(world, GOAL_A, MS_A)


@pytest.mark.parametrize("mode", MODES)
def test_milestone_only_submission_reaches_mockup_gate_with_filled_goal(world, monkeypatch, mode):
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    seen = []

    async def record(*_a, **kwargs):
        seen.append(kwargs.get("goal_id"))
        return {"applicable": False, "allowed": True, "reasons": []}

    monkeypatch.setattr(pr, "_run_mockup_gate", record)
    _submit(pr, mode, "작업", milestone_id=MS_A)
    assert seen == [GOAL_A]


def _parent_job(ms):
    return {"job_id": "runner-parent01", "status": "running",
            "instruction": f"부모\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {ms}\n"}


@pytest.mark.parametrize("instruction,goal_id,milestone_id,_src", _shape_params(GOAL_A, MS_A))
def test_single_explicit_dependency_on_later_milestone_is_409_for_every_input_shape(
    world, monkeypatch, instruction, goal_id, milestone_id, _src,
):
    pr = _runner_env(monkeypatch, world, real_order=True)
    _seed_goals(world)
    world.dep_jobs["runner-parent01"] = _parent_job(MS_A2)
    err = _denied(pr, "single", instruction, 409, "dependency_inversion",
                  goal_id=goal_id, milestone_id=milestone_id, depends_on="runner-parent01")
    assert "sequence=2" in err.detail
    _assert_nothing_written(world)


@pytest.mark.parametrize("instruction,goal_id,milestone_id,bind_source", _shape_params(GOAL_A, MS_A2))
def test_single_explicit_dependency_in_order_keeps_milestone(
    world, monkeypatch, instruction, goal_id, milestone_id, bind_source,
):
    pr = _runner_env(monkeypatch, world, real_order=True)
    _seed_goals(world)
    world.dep_jobs["runner-parent01"] = _parent_job(MS_A)
    _submit(pr, "single", instruction, goal_id=goal_id, milestone_id=milestone_id,
            depends_on="runner-parent01")
    _assert_linked(world, GOAL_A, MS_A2, bind_source)


def _batch_with_parent(pr, parent_ms, child):
    instruction, goal_id, milestone_id, _src = child
    return pr.BatchSubmitRequest(project="AADS", session_id=SESSION, jobs=[
        pr.BatchJobItem(key="P", instruction="부모", milestone_id=parent_ms),
        pr.BatchJobItem(key="C", instruction=instruction, goal_id=goal_id,
                        milestone_id=milestone_id, depends_on_key="P"),
    ])


@pytest.mark.parametrize("instruction,goal_id,milestone_id,_src", _shape_params(GOAL_A, MS_A))
def test_batch_dependency_on_later_milestone_is_409_for_every_input_shape(
    world, monkeypatch, instruction, goal_id, milestone_id, _src,
):
    from fastapi import HTTPException

    pr = _runner_env(monkeypatch, world, real_order=True)
    _seed_goals(world)
    req = _batch_with_parent(pr, MS_A2, (instruction, goal_id, milestone_id, _src))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(pr.submit_batch(req, context=_CTX))
    assert exc.value.status_code == 409 and "dependency_inversion" in exc.value.detail
    assert world.links == [] and world.job_updates == []


@pytest.mark.parametrize("instruction,goal_id,milestone_id,bind_source", _shape_params(GOAL_A, MS_A2))
def test_batch_dependency_in_order_keeps_both_milestones(
    world, monkeypatch, instruction, goal_id, milestone_id, bind_source,
):
    pr = _runner_env(monkeypatch, world, real_order=True)
    _seed_goals(world)
    asyncio.run(pr.submit_batch(
        _batch_with_parent(pr, MS_A, (instruction, goal_id, milestone_id, bind_source)), context=_CTX,
    ))
    assert {link[2] for link in world.links} == {MS_A, MS_A2}
    assert all(link[1] == GOAL_A for link in world.links)
    assert all(u[1] == GOAL_A and u[2] in (MS_A, MS_A2) for u in world.job_updates)


def _active_job(ms):
    return {
        "job_id": "runner-active01", "status": "running", "phase": "coding",
        "actual_changed_files": None,
        "instruction": f"기존\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {ms}\nTARGET_FILES: app/shared_x.py\n",
    }


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("instruction,goal_id,milestone_id,_src", _shape_params(GOAL_A, MS_A))
def test_auto_file_conflict_with_later_milestone_is_409_for_every_input_shape(
    world, monkeypatch, mode, instruction, goal_id, milestone_id, _src,
):
    pr = _runner_env(monkeypatch, world, real_order=True)
    _seed_goals(world)
    world.active_jobs = [_active_job(MS_A2)]
    _denied(pr, mode, instruction + "TARGET_FILES: app/shared_x.py\n", 409, "dependency_inversion",
            goal_id=goal_id, milestone_id=milestone_id)
    assert world.links == [] and world.job_updates == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("instruction,goal_id,milestone_id,bind_source", _shape_params(GOAL_A, MS_A2))
def test_auto_file_conflict_in_order_serializes_and_keeps_milestone(
    world, monkeypatch, mode, instruction, goal_id, milestone_id, bind_source,
):
    pr = _runner_env(monkeypatch, world, real_order=True)
    _seed_goals(world)
    world.active_jobs = [_active_job(MS_A)]
    _submit(pr, mode, instruction + "TARGET_FILES: app/shared_x.py\n",
            goal_id=goal_id, milestone_id=milestone_id)
    assert any("runner-active01" in args for args in world.inserts)
    _assert_linked(world, GOAL_A, MS_A2, bind_source)


def test_submit_batch_rejects_whole_batch_when_one_item_lacks_milestone(world, monkeypatch):
    pr = _runner_env(monkeypatch, world)
    _seed_goals(world)
    req = pr.BatchSubmitRequest(
        project="AADS", session_id=SESSION,
        jobs=[
            pr.BatchJobItem(key="A", instruction=f"지시\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n"),
            pr.BatchJobItem(key="B", instruction=f"지시\nGOAL_ID: {GOAL_A}\n"),
        ],
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(pr.submit_batch(req, context=_CTX))
    assert exc.value.status_code == 422 and "[B]" in exc.value.detail["message"]
    _assert_nothing_written(world)


def test_submit_job_without_any_basis_stays_unlinked(world, monkeypatch):
    pr = _runner_env(monkeypatch, world)
    req = pr.JobSubmitRequest(project="AADS", session_id=SESSION, instruction="근거 없는 지시")
    resp = asyncio.run(pr.submit_job(req, context=_CTX))
    assert resp.status == "queued" and world.job_updates == []


def test_submit_batch_links_after_commit_not_inside_the_transaction(world, monkeypatch):
    """회귀: 배치는 INSERT 트랜잭션 안에서 연결을 시도해 작업 행이 안 보여 전부 미연결이었다."""
    pr = _runner_env(monkeypatch, world)
    world.add_goal(GOAL_A)
    world.add_milestone(MS_A, GOAL_A)
    state = {"in_tx": 0, "link_in_tx": []}

    class _Tx:
        async def __aenter__(s):
            state["in_tx"] += 1

        async def __aexit__(s, *exc):
            state["in_tx"] -= 1
            return False

    world.transaction = lambda: _Tx()  # type: ignore[method-assign]
    original = world.execute

    async def spy(query, *args):
        if "INSERT INTO goal_task_links" in query:
            state["link_in_tx"].append(state["in_tx"])
        return await original(query, *args)

    world.execute = spy  # type: ignore[method-assign]

    async def fetchval(query, *args):
        if "FROM chat_sessions WHERE id" in query:
            return TENANT
        return await World.fetchval(world, query, *args)

    world.fetchval = fetchval  # type: ignore[method-assign]

    req = pr.BatchSubmitRequest(
        project="AADS", session_id=SESSION,
        jobs=[
            pr.BatchJobItem(key="A", instruction=f"지시\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n"),
            pr.BatchJobItem(key="B", instruction="근거 없는 지시"),
        ],
    )
    out = asyncio.run(pr.submit_batch(req, context=_CTX))
    linked = {j["key"]: j["goal_link"] for j in out["jobs"]}
    assert linked["A"]["goal_id"] == GOAL_A and linked["A"]["source"] == "directive"
    assert linked["B"]["source"] == "none"
    # 연결 함수 자신의 트랜잭션(깊이 1)뿐이어야 한다 — 배치 INSERT 트랜잭션 안이면 깊이 2다.
    assert state["link_in_tx"] == [1], "연결은 배치 트랜잭션이 닫힌 뒤여야 한다"


def test_batch_item_goal_fields_validate_uuid():
    import app.api.pipeline_runner as pr

    with pytest.raises(ValueError):
        pr.BatchJobItem(key="A", instruction="x", goal_id="nope")
    assert pr.BatchJobItem(key="A", instruction="x").goal_id == ""


# ── 활동 판정 🔄·⏳·없음 ────────────────────────────────────────────────────
def test_three_activity_kinds():
    assert ga.decide_activity(running=True) == ga.ACTIVITY_WORKING
    assert ga.decide_activity(chat_active=True) == ga.ACTIVITY_WORKING
    assert ga.decide_activity(awaiting=True) == ga.ACTIVITY_AWAITING
    assert ga.decide_activity() is None
    # 둘이 겹치면 🔄 가 이긴다.
    assert ga.decide_activity(running=True, awaiting=True) == ga.ACTIVITY_WORKING


def test_activity_for_goals_from_job_states_and_chat(world):
    world.job_states = [
        {"goal_id": GOAL_A, "milestone_id": MS_A, "job_id": "runner-1", "status": "running", "created_at": NOW},
        {"goal_id": GOAL_B, "milestone_id": MS_B, "job_id": "runner-2", "status": "awaiting_approval", "created_at": NOW},
    ]
    third = "11111111-1111-4111-8111-333333333333"
    fourth = "11111111-1111-4111-8111-444444444444"
    world.chat_goals = [third]
    out = asyncio.run(ga.activity_for_goals(world, [GOAL_A, GOAL_B, third, fourth]))
    assert out[GOAL_A]["activity"] == "working"
    assert out[GOAL_A]["runners"][MS_A] == {"job_id": "runner-1", "status": "running"}
    assert out[GOAL_B]["activity"] == "awaiting"
    assert out[GOAL_B]["runners"][MS_B] == {"job_id": "runner-2", "status": "awaiting_approval"}
    assert out[third]["activity"] == "working" and out[third]["runners"] == {}
    assert out[fourth]["activity"] is None and out[fourth]["goal_runner"] is None


def test_activity_lookup_failure_degrades_to_no_activity(world):
    world.fail_activity = True
    out = asyncio.run(ga.activity_for_goals(world, [GOAL_A]))
    assert out[GOAL_A] == {"activity": None, "runners": {}, "goal_runner": None}


def test_chat_activity_sql_is_30_minutes_open_unpaused_only():
    sql = ga.GOAL_CHAT_ACTIVITY_SQL
    assert "make_interval(mins => $2::int)" in sql and ga.CHAT_ACTIVE_MINUTES == 30
    assert "g.paused_at IS NULL" in sql and "g.status = ANY($3::text[])" in sql
    assert "completed" not in ga._CHAT_OPEN_STATUSES


def test_current_runner_prefers_running_then_claimed_then_awaiting_and_newest():
    old, new = NOW - timedelta(hours=2), NOW
    jobs = [
        {"job_id": "a", "status": "awaiting_approval", "created_at": new},
        {"job_id": "b", "status": "claimed", "created_at": old},
        {"job_id": "c", "status": "running", "created_at": old},
        {"job_id": "d", "status": "running", "created_at": new},
        {"job_id": "e", "status": "done", "created_at": new},
    ]
    assert ga.pick_current_runner(jobs) == {"job_id": "d", "status": "running"}
    assert ga.pick_current_runner(jobs[:2]) == {"job_id": "b", "status": "claimed"}
    assert ga.pick_current_runner([jobs[-1]]) is None
    assert ga.pick_current_runner([]) is None


def test_unlinked_count_uses_session_48h_and_excludes_dedup(world):
    world.unlinked = 7
    assert asyncio.run(ga.unlinked_count(world, SESSION, TENANT)) == 7
    sql = ga.UNLINKED_COUNT_SQL
    assert "j.goal_id IS NULL" in sql and "j.chat_session_id = $1::uuid" in sql
    assert "make_interval(hours => $3::int)" in sql and ga.UNLINKED_WINDOW_HOURS == 48
    assert "dedup_blocked" in sql


def test_unlinked_count_failure_is_zero_not_an_error():
    class Broken:
        async def fetchval(self, *_a):
            raise RuntimeError("down")

    assert asyncio.run(ga.unlinked_count(Broken(), SESSION, TENANT)) == 0


# ── 정렬 🔄 → ⏳ → P0 → P1·P2 → 💤 ──────────────────────────────────────────
def test_strip_rank_order():
    assert ga.strip_rank("working", "P3", True) == ga.RANK_WORKING
    assert ga.strip_rank("awaiting", "P2", True) == ga.RANK_AWAITING
    assert ga.strip_rank(None, "P0-CRITICAL", False) == ga.RANK_P0
    assert ga.strip_rank(None, "P0", False) == ga.RANK_P0
    assert ga.strip_rank(None, "P1", False) == ga.RANK_OTHER
    assert ga.strip_rank(None, "P2", False) == ga.RANK_OTHER
    assert ga.strip_rank(None, "P0", True) == ga.RANK_DORMANT
    assert (ga.RANK_WORKING < ga.RANK_AWAITING < ga.RANK_P0 < ga.RANK_OTHER < ga.RANK_DORMANT)


def test_sort_for_strip_uses_rank_then_priority_then_title():
    items = [
        {"title": "휴면", "priority": "P0", "strip_rank": ga.RANK_DORMANT},
        {"title": "P2", "priority": "P2", "strip_rank": ga.RANK_OTHER},
        {"title": "P1", "priority": "P1", "strip_rank": ga.RANK_OTHER},
        {"title": "P0", "priority": "P0", "strip_rank": ga.RANK_P0},
        {"title": "대기", "priority": "P3", "strip_rank": ga.RANK_AWAITING},
        {"title": "작업중", "priority": "P3", "strip_rank": ga.RANK_WORKING},
    ]
    assert [i["title"] for i in ga.sort_for_strip(items)] == ["작업중", "대기", "P0", "P1", "P2", "휴면"]


# ── API 본문: GET /goals · GET /goals/for-session · get_goal_status ─────────
def _goal_row(goal_id, *, title, priority, status="active"):
    return {
        "id": goal_id, "goal_id": goal_id, "project": "AADS", "title": title,
        "priority": priority, "status": status, "progress": 0,
        "created_at": NOW, "completed_at": None, "paused_at": None, "paused_reason": None,
        "approval_policy": {},
    }


def test_list_goals_adds_activity_and_strip_rank_and_keeps_existing_fields(world):
    from app.services.goal_manager import GoalStateMachine

    rows = [
        _goal_row(GOAL_A, title="A", priority="P2"),
        _goal_row(GOAL_B, title="B", priority="P0"),
    ]
    original = world.fetch

    async def fetch(query, *args):
        if "approval_policy" in query and "FROM goals" in query:
            return rows
        return await original(query, *args)

    world.fetch = fetch  # type: ignore[method-assign]
    world.activity_rows = [
        {"goal_id": GOAL_A, "last_activity_at": NOW, "block_note": None},
        {"goal_id": GOAL_B, "last_activity_at": NOW, "block_note": None},
    ]
    world.job_states = [
        {"goal_id": GOAL_A, "milestone_id": MS_A, "job_id": "runner-1", "status": "running", "created_at": NOW},
    ]
    out = asyncio.run(GoalStateMachine().list_goals("AADS", tenant_id=TENANT))
    by_id = {g["goal_id"]: g for g in out}
    assert by_id[GOAL_A]["activity"] == "working" and by_id[GOAL_A]["strip_rank"] == ga.RANK_WORKING
    assert by_id[GOAL_A]["current_runner"] == {"job_id": "runner-1", "status": "running"}
    assert by_id[GOAL_B]["activity"] is None and by_id[GOAL_B]["strip_rank"] == ga.RANK_P0
    # M2 필드와 기존 필드는 그대로다.
    for key in ("priority_rank", "dormant", "auto_approval", "goal_paused", "progress", "title"):
        assert key in by_id[GOAL_A]


def test_goals_for_session_sorts_strip_and_reports_unlinked_count(world, monkeypatch):
    import app.services.direction_guard as direction_guard
    from app.routers import goals as goals_router

    async def halted():
        return False

    monkeypatch.setattr(direction_guard, "is_halted", halted)
    third = "11111111-1111-4111-8111-333333333333"
    fourth = "11111111-1111-4111-8111-444444444444"
    session_rows = [
        {**_goal_row(third, title="휴면", priority="P0"), "is_lead": True, "role_key": "cto",
         "milestone_id": None, "milestone": None, "dispatch_note": None, "dispatch_blocked_at": None},
        {**_goal_row(GOAL_B, title="P0 목표", priority="P0"), "is_lead": True, "role_key": "cto",
         "milestone_id": MS_B, "milestone": "마일스톤 B", "dispatch_note": None, "dispatch_blocked_at": None},
        {**_goal_row(fourth, title="승인 대기", priority="P2"), "is_lead": False, "role_key": "cto",
         "milestone_id": None, "milestone": None, "dispatch_note": None, "dispatch_blocked_at": None},
        {**_goal_row(GOAL_A, title="작업 중", priority="P2"), "is_lead": False, "role_key": "cto",
         "milestone_id": MS_A, "milestone": "마일스톤 A", "dispatch_note": None, "dispatch_blocked_at": None},
    ]
    original = world.fetch

    async def fetch(query, *args):
        if "FROM goal_task_links l" in query and "JOIN chat_sessions s" in query:
            return session_rows
        return await original(query, *args)

    world.fetch = fetch  # type: ignore[method-assign]
    world.activity_rows = [
        {"goal_id": third, "last_activity_at": NOW - timedelta(days=30), "block_note": None},
        {"goal_id": GOAL_B, "last_activity_at": NOW, "block_note": None},
        {"goal_id": fourth, "last_activity_at": NOW, "block_note": None},
        {"goal_id": GOAL_A, "last_activity_at": NOW, "block_note": None},
    ]
    world.job_states = [
        {"goal_id": GOAL_A, "milestone_id": MS_A, "job_id": "runner-9", "status": "claimed", "created_at": NOW},
        {"goal_id": fourth, "milestone_id": None, "job_id": "runner-8", "status": "awaiting_approval", "created_at": NOW},
    ]
    world.unlinked = 3

    out = asyncio.run(goals_router.goals_for_session(SESSION, _CTX))
    assert out["unlinked_count"] == 3 and out["halted"] is False
    assert [g["title"] for g in out["goals"]] == ["작업 중", "승인 대기", "P0 목표", "휴면"]
    first = out["goals"][0]
    assert first["activity"] == "working"
    assert first["current_runner"] == {"job_id": "runner-9", "status": "claimed"}
    assert first["is_lead"] is False and first["milestone"] == "마일스톤 A"
    assert out["goals"][1]["activity"] == "awaiting"
    assert out["goals"][3]["dormant"] is True
    for g in out["goals"]:
        assert "approval_policy" not in g and "paused_at" not in g


def test_milestone_current_runner_in_goal_status(world):
    from app.services.goal_manager import GoalStateMachine

    goal = {
        "id": GOAL_A, "project": "AADS", "title": "T", "priority": "P1", "status": "active",
        "description": "d", "success_criteria": "s", "progress": 0, "created_at": NOW,
        "completed_at": None, "paused_at": None, "paused_reason": None,
    }
    milestones = [
        {"id": MS_A, "title": "A", "sequence_order": 1, "status": "in_progress", "auto_advance": True,
         "completion_criteria": None, "started_at": None, "completed_at": None, "completion_checklist": None},
        {"id": MS_B, "title": "B", "sequence_order": 2, "status": "pending", "auto_advance": True,
         "completion_criteria": None, "started_at": None, "completed_at": None, "completion_checklist": None},
    ]
    original_fetch, original_row = world.fetch, world.fetchrow

    async def fetchrow(query, *args):
        if "FROM goals WHERE id = $1::uuid" in query:
            return goal
        return await original_row(query, *args)

    async def fetch(query, *args):
        if "FROM milestones m" in query:
            return milestones
        if "FROM goal_task_links" in query and "task_type, task_id" in query:
            return []
        return await original_fetch(query, *args)

    world.fetchrow, world.fetch = fetchrow, fetch  # type: ignore[method-assign]
    world.job_states = [
        {"goal_id": GOAL_A, "milestone_id": MS_A, "job_id": "runner-7", "status": "running", "created_at": NOW},
    ]

    async def columns(_conn):
        return set()

    import app.services.goal_manager as gm

    original_columns = gm.link_optional_columns
    gm.link_optional_columns = columns
    try:
        out = asyncio.run(GoalStateMachine().get_goal_status(GOAL_A))
    finally:
        gm.link_optional_columns = original_columns
    by_id = {m["id"]: m for m in out["milestones"]}
    assert out["activity"] == "working"
    assert by_id[MS_A]["current_runner"] == {"job_id": "runner-7", "status": "running"}
    assert by_id[MS_B]["current_runner"] is None


# ── 백필 스크립트: (b) 만, 기본 dry-run ─────────────────────────────────────
def _load_backfill():
    path = REPO / "scripts" / "backfill_job_goal_links.py"
    spec = importlib.util.spec_from_file_location("backfill_job_goal_links", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bf_job(instruction, project="AADS"):
    return {"job_id": "runner-1", "tenant_id": TENANT, "project": project, "instruction": instruction}


def _bf_goal(status="active", project="AADS", tenant=TENANT):
    return {"goal_id": GOAL_A, "tenant_id": tenant, "project": project, "status": status}


def _bf_ms(status="in_progress", goal=GOAL_A):
    return {"milestone_id": MS_A, "goal_id": goal, "tenant_id": TENANT, "project": "AADS", "status": status}


def test_backfill_plan_follows_the_link_rules():
    bf = _load_backfill()
    both = f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n"
    assert bf.plan_link(_bf_job(both), _bf_goal(), _bf_ms()) == ((GOAL_A, MS_A), "linkable")
    assert bf.plan_link(_bf_job("지시만"), _bf_goal(), None) == (None, "no_directive")
    assert bf.plan_link(_bf_job(both), None, None) == (None, "goal_not_found")
    assert bf.plan_link(_bf_job(both), _bf_goal(project="KIS"), _bf_ms()) == (None, "cross_project")
    assert bf.plan_link(_bf_job(both), _bf_goal(tenant="other"), _bf_ms()) == (None, "cross_tenant")
    assert bf.plan_link(_bf_job(both), _bf_goal(status="completed"), _bf_ms()) == (None, "goal_inactive")
    # 마일스톤이 다른 목표 것이면 목표에만 연결한다. 번호 없는 추정은 하지 않는다.
    assert bf.plan_link(_bf_job(both), _bf_goal(), _bf_ms(goal=GOAL_B)) == ((GOAL_A, None), "linkable")
    # blocked 목표는 열린 마일스톤이 명시돼야 한다.
    goal_only = f"GOAL_ID: {GOAL_A}\n"
    assert bf.plan_link(_bf_job(goal_only), _bf_goal("blocked"), None) == (None, "blocked_milestone_required")
    assert bf.plan_link(_bf_job(both), _bf_goal("blocked"), _bf_ms("completed")) == (
        None, "blocked_milestone_closed")
    assert bf.plan_link(_bf_job(both), _bf_goal("blocked"), _bf_ms("blocked"))[1] == "linkable"


def test_backfill_defaults_to_dry_run_and_never_touches_updated_at_or_links():
    bf = _load_backfill()
    src = (REPO / "scripts" / "backfill_job_goal_links.py").read_text(encoding="utf-8")
    args = []
    parser_calls = {}

    def fake_run(days, apply):
        parser_calls.update(days=days, apply=apply)
        return 0

    original = bf.run
    bf.run = fake_run  # type: ignore[assignment]
    original_asyncio_run = bf.asyncio.run
    bf.asyncio.run = lambda value: value  # type: ignore[assignment]
    try:
        assert bf.main(args) == 0
        assert parser_calls == {"days": 14, "apply": False}
        assert bf.main(["--apply"]) == 0
        assert parser_calls["apply"] is True
    finally:
        bf.run = original  # type: ignore[assignment]
        bf.asyncio.run = original_asyncio_run  # type: ignore[assignment]
    assert "updated_at" not in bf.APPLY_SQL
    assert "goal_task_links" not in bf.APPLY_SQL
    assert "goal_id IS NULL" in bf.APPLY_SQL
    assert "INSERT" not in src.split("APPLY_SQL")[1].split('"""')[1]


def test_backfill_dry_run_writes_nothing():
    bf = _load_backfill()

    class Conn:
        def __init__(self):
            self.writes = []

        async def fetch(self, query, *args):
            if "FROM pipeline_jobs j" in query:
                return [{"job_id": "runner-1", "tenant_id": TENANT, "project": "AADS",
                         "instruction": f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n", "created_at": NOW}]
            if "FROM goals" in query:
                return [_bf_goal()]
            if "FROM milestones" in query:
                return [_bf_ms()]
            return []

        async def execute(self, query, *args):
            self.writes.append(query)
            return "UPDATE 1"

    conn = Conn()
    plans, reasons = asyncio.run(bf._plan_all(conn, 14))
    assert plans == [{"job_id": "runner-1", "tenant_id": TENANT, "project": "AADS",
                      "goal_id": GOAL_A, "milestone_id": MS_A}]
    assert reasons == {"linkable": 1} and conn.writes == []


def test_prd_copy_matches_canonical_rev5_hash():
    import hashlib

    path = next((REPO / "docs" / "prd").glob("20261015_AADS_목표_우선순위_휴면정리_자동승인표시.md"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "db0b7f54ce1a7522889ca95a07861dfef4e798dc2e8eb0150c769dec6fa73dcc"

