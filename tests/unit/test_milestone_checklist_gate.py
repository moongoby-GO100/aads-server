"""완료 체크리스트 게이트와 비파괴 정정 경로 (AADS-RDOC-M4-CHECKLIST-FAILED-LINK-RETRY)."""
import asyncio
import copy
import json

import pytest
from fastapi import HTTPException

from app.routers import goals
from app.services import goal_manager, milestone_checklist as mc

TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GOAL = "11111111-1111-4111-8111-111111111111"
MS = "33333333-3333-4333-8333-333333333333"
NOW = "2026-10-20T10:00:00+00:00"


def _pending_checklist():
    return [
        {"step": "4.1", "owner": "PM", "title": "판정표", "status": "pending", "verify": "표"},
        {"step": "4.2", "owner": "PM", "title": "파일럿", "status": "pending", "verify": "10건"},
        {"step": "4.3", "owner": "CEO", "title": "배치 승인", "status": "pending", "verify": "승인 기록"},
        {"step": "4.4", "owner": "CTO", "title": "이중 정본", "status": "pending", "verify": "0건"},
    ]


class Store:
    """milestones 1행 + goals 1행을 가진 상태형 가짜 커넥션. transaction 은 예외 시 되돌린다."""

    def __init__(self, checklist=None, status="completed", version=16, goal_status="active"):
        self.ms = {
            "id": MS, "goal_id": GOAL, "tenant_id": TENANT_A, "status": status,
            "version": version, "completed_at": "2026-10-20T06:11:57+00:00",
            "review_note": "기존 메모", "evidence": {"retry_candidates": ["keep"]},
            "dispatch_count": 3, "completion_checklist": checklist,
        }
        self.goal = {"id": GOAL, "tenant_id": TENANT_A, "status": goal_status}
        self.writes = 0
        self.fail_update = False
        self.links = [{"task_type": "pipeline_job", "task_id": "j1", "status": "completed",
                       "goal_id": GOAL}]
        self.advanced = []
        self.row_lock = None  # asyncio.Lock — FOR UPDATE 행잠금 흉내 (None 이면 잠금 없는 환경)
        self.before_complete = None  # 완료 UPDATE 직전에 끼울 코루틴 (인터리빙 재현)

    def transaction(self):
        store = self

        class _Tx:
            async def __aenter__(self):
                if store.row_lock is not None:
                    await store.row_lock.acquire()
                self.snap = copy.deepcopy((store.ms, store.writes))

            async def __aexit__(self, exc_type, *_):
                if exc_type:
                    store.ms, store.writes = self.snap
                if store.row_lock is not None:
                    store.row_lock.release()
                return False

        return _Tx()

    def _checklist_raw(self):
        value = self.ms["completion_checklist"]
        return None if value is None else json.dumps(value, ensure_ascii=False)

    async def fetchrow(self, query, *args):
        await asyncio.sleep(0)
        if "completion_checklist" in query and "FOR UPDATE" in query and len(args) == 1:
            return {"status": self.ms["status"], "version": self.ms["version"],
                    "completion_checklist": self._checklist_raw()}
        if "completion_checklist" in query and "FOR UPDATE" in query:
            mid, gid, tid = args
            m = self.ms
            if (m["id"], m["goal_id"], m["tenant_id"]) != (mid, gid, tid):
                return None
            return {k: m[k] for k in ("status", "version", "completed_at", "review_note")} | {
                "completion_checklist": self._checklist_raw()}
        if "SELECT goal_id FROM milestones" in query:
            return {"goal_id": GOAL}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        assert "FROM goal_task_links" in query
        return self.links

    async def fetchval(self, query, *args):
        if query.lstrip().startswith("SELECT status FROM goals"):
            return self.goal["status"] if args[1] == self.goal["tenant_id"] else None
        if query.lstrip().startswith("UPDATE milestones"):
            if self.fail_update:
                raise RuntimeError("db down")
            mid, gid, tid = args[:3]
            m = self.ms
            expected = args[-1]
            if (m["id"], m["goal_id"], m["tenant_id"]) != (mid, gid, tid) or m["version"] != expected:
                return None
            if "completion_checklist = $4" in query:
                m["completion_checklist"] = json.loads(args[3])
            else:
                assert "status = 'in_progress'" in query and m["status"] == "completed"
                m["status"], m["completed_at"], m["review_note"] = "in_progress", None, args[3]
            m["version"] += 1
            self.writes += 1
            return m["version"]
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "SET status = 'completed'" in query:
            if self.before_complete:
                await self.before_complete()
            m = self.ms
            if m["status"] == "completed" or m["version"] != args[1]:
                return "UPDATE 0"
            m["status"] = "completed"
            m["version"] += 1
            self.writes += 1
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_):
                return False

        return _Acq()


@pytest.fixture
def machine_for(monkeypatch):
    def build(store):
        machine = goal_manager.GoalStateMachine()

        async def pool():
            return _Pool(store)

        async def columns(_conn):
            return set()

        async def noop(*_a, **_k):
            return None

        async def advance(goal_id, milestone_id):
            store.advanced.append(milestone_id)

        monkeypatch.setattr(machine, "_pool", pool)
        monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
        monkeypatch.setattr(machine, "_mark_superseded_failures", noop)
        monkeypatch.setattr(goal_manager, "mark_explicit_retry_supersession", noop)
        monkeypatch.setattr(goal_manager, "archive_exhaustion_note", noop)
        monkeypatch.setattr(machine, "_advance_after_milestone", advance)
        return machine

    return build


def _check(machine):
    return asyncio.run(machine.check_milestone_completion(MS))


# ── check_milestone_completion 게이트 ─────────────────────────────────────

def test_unmet_checklist_with_all_links_done_does_not_complete_or_advance(machine_for):
    store = Store(_pending_checklist(), status="in_progress")
    result = _check(machine_for(store))
    assert result["completed"] is False
    assert result["reason"] == "checklist_unmet"
    assert [u["step"] for u in result["checklist_unmet"]] == ["4.1", "4.2", "4.3", "4.4"]
    assert store.ms["status"] == "in_progress"
    assert store.writes == 0 and store.advanced == []


@pytest.mark.parametrize("checklist", [None, [], {}])
def test_milestone_without_checklist_keeps_previous_completion_behaviour(machine_for, checklist):
    store = Store(checklist, status="in_progress")
    result = _check(machine_for(store))
    assert result["completed"] is True
    assert store.ms["status"] == "completed"
    assert store.advanced == [MS]


def test_satisfied_checklist_completes_with_done_and_completed_normalised(machine_for):
    checklist = [
        {"step": "1", "owner": "PM", "status": "done"},
        {"step": "2", "owner": "CTO", "status": "completed"},
        {"step": "3", "owner": "CEO", "status": "done", "approved_by": "ceo-user-1"},
    ]
    store = Store(checklist, status="in_progress")
    result = _check(machine_for(store))
    assert result["completed"] is True
    assert store.ms["status"] == "completed" and store.advanced == [MS]


def test_already_completed_with_unmet_evidence_is_reported_not_advanced(machine_for):
    store = Store(_pending_checklist(), status="completed")
    result = _check(machine_for(store))
    assert result["completed"] is False
    assert result["stale_completion"] is True
    assert store.ms["status"] == "completed"  # 자동 정정하지 않는다 — 정정은 명시 경로
    assert store.advanced == [] and store.writes == 0


@pytest.mark.parametrize("approved_by", [None, "", "runner-9155eb0e", "system:pipeline-runner"])
def test_approver_item_is_not_satisfied_by_runner_done(machine_for, approved_by):
    item = {"step": "4.3", "owner": "CEO", "status": "done"}
    if approved_by is not None:
        item["approved_by"] = approved_by
    store = Store([item], status="in_progress")
    result = _check(machine_for(store))
    assert result["completed"] is False
    assert result["checklist_unmet"][0]["reason"] == "approval_missing"
    assert store.advanced == []


def test_unparseable_explicit_checklist_fails_closed(machine_for):
    store = Store("garbage{", status="in_progress")
    store.ms["completion_checklist"] = None
    store._checklist_raw = lambda: "garbage{"
    result = _check(machine_for(store))
    assert result["completed"] is False
    assert result["checklist_unmet"][0]["reason"] == "unparseable"


def test_dict_shaped_checklist_is_evaluated():
    verdict = mc.evaluate_checklist({"4.1": "done", "4.2": {"status": "pending"}})
    assert verdict["explicit"] and not verdict["satisfied"]
    assert [u["step"] for u in verdict["unmet"]] == ["4.2"]


# ── 완료 판정과 쓰기의 원자성 (동시성) ──────────────────────────────────

def _done_pm_checklist():
    return [{"step": "4.1", "owner": "PM", "title": "판정표", "status": "done",
             "evidence": "docs/reports/판정표.md 308행 대조 완료"}]


def test_checklist_reverted_between_judgement_and_write_does_not_complete_or_advance(machine_for):
    store = Store(_done_pm_checklist(), status="in_progress", version=16)

    async def revert_concurrently():
        store.before_complete = None
        await mc.update_checklist_item(
            store, goal_id=GOAL, milestone_id=MS, tenant_id=TENANT_A, step="4.1",
            status="pending", evidence=None, expected_version=16,
            request_id="concurrent-review-1", actor="user-pm", actor_is_approver=False,
            now_iso=NOW,
        )

    store.before_complete = revert_concurrently
    result = _check(machine_for(store))
    assert result["completed"] is False and result["reason"] == "version_conflict"
    assert store.ms["status"] == "in_progress"
    assert store.ms["version"] == 17 and store.ms["completion_checklist"][0]["status"] == "pending"
    assert store.advanced == []
    # 다음 호출은 되돌려진 체크리스트를 보고 미충족으로 판정한다.
    again = _check(machine_for(store))
    assert again["reason"] == "checklist_unmet" and store.advanced == []


def test_completion_bumps_version_so_stale_checklist_edit_is_rejected(machine_for):
    store = Store(_done_pm_checklist(), status="in_progress", version=16)
    assert _check(machine_for(store))["completed"] is True
    assert store.ms["version"] == 17
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, status="pending", evidence=None, expected_version=16)
    assert exc.value.code == "version_conflict" and exc.value.extra["current_version"] == 17


def test_repeated_call_on_completed_milestone_does_not_advance_again(machine_for):
    store = Store(_done_pm_checklist(), status="in_progress", version=16)
    machine = machine_for(store)
    first = _check(machine)
    second = _check(machine)
    assert first["completed"] is True and "already_completed" not in first
    assert second["completed"] is True and second["already_completed"] is True
    assert store.advanced == [MS] and store.writes == 1 and store.ms["version"] == 17


def test_repeated_call_without_explicit_checklist_does_not_advance_again(machine_for):
    store = Store(None, status="in_progress")
    machine = machine_for(store)
    _check(machine)
    _check(machine)
    assert store.advanced == [MS]


def test_two_concurrent_completion_calls_advance_exactly_once(machine_for):
    store = Store(_done_pm_checklist(), status="in_progress", version=16)
    store.row_lock = asyncio.Lock()
    machine = machine_for(store)

    async def both():
        return await asyncio.gather(
            machine.check_milestone_completion(MS), machine.check_milestone_completion(MS),
        )

    results = asyncio.run(both())
    assert all(r["completed"] is True for r in results)
    assert sum(1 for r in results if r.get("already_completed")) == 1
    assert store.advanced == [MS] and store.writes == 1


def test_advance_runs_after_the_lock_is_released(machine_for):
    """잠금을 쥔 채 advance(새 conn)로 들어가면 교착한다 — 커밋 뒤에 호출돼야 한다."""
    store = Store(_done_pm_checklist(), status="in_progress", version=16)
    store.row_lock = asyncio.Lock()
    machine = machine_for(store)
    held = []

    async def advance(goal_id, milestone_id):
        held.append(store.row_lock.locked())
        store.advanced.append(milestone_id)

    machine._advance_after_milestone = advance
    assert _check(machine)["completed"] is True
    assert held == [False]


# ── 항목 갱신 ───────────────────────────────────────────────────────────

def _update(store, **overrides):
    kwargs = dict(
        goal_id=GOAL, milestone_id=MS, tenant_id=TENANT_A, step="4.1", status="done",
        evidence="docs/reports/판정표.md 308행 대조 완료", expected_version=16,
        request_id="req-0001-abcd", actor="user-pm", actor_is_approver=False, now_iso=NOW,
    )
    kwargs.update(overrides)
    return asyncio.run(mc.update_checklist_item(store, **kwargs))


def test_item_update_changes_only_that_item_and_keeps_history():
    store = Store(_pending_checklist())
    before = copy.deepcopy(store.ms)
    result = _update(store)
    assert result["changed"] and result["version"] == 17
    items = {i["step"]: i for i in store.ms["completion_checklist"]}
    assert items["4.1"]["status"] == "done" and items["4.1"]["evidence"].startswith("docs/")
    assert items["4.1"]["history"][0]["from"] == "pending"
    assert all(items[s]["status"] == "pending" for s in ("4.2", "4.3", "4.4"))
    assert store.ms["status"] == before["status"] == "completed"  # 상태는 건드리지 않는다
    assert store.ms["evidence"] == before["evidence"]
    assert result["checklist"]["satisfied"] is False


def test_completed_alias_is_stored_as_done():
    store = Store(_pending_checklist())
    _update(store, status="completed")
    assert store.ms["completion_checklist"][0]["status"] == "done"


def test_item_update_requires_evidence_for_done():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, evidence="짧음")
    assert exc.value.code == "evidence_required" and store.writes == 0


def test_stale_version_is_rejected_without_write():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, expected_version=15)
    assert exc.value.status_code == 409 and exc.value.extra["current_version"] == 16
    assert store.writes == 0 and store.ms["version"] == 16


def test_duplicate_request_is_idempotent():
    store = Store(_pending_checklist())
    first = _update(store)
    second = _update(store)  # 같은 request_id, 같은(이제 낡은) expected_version
    assert first["changed"] and second["changed"] is False and second["idempotent"]
    assert store.writes == 1 and store.ms["version"] == 17
    assert len(store.ms["completion_checklist"][0]["history"]) == 1


def test_other_tenant_gets_not_found_without_write():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, tenant_id=TENANT_B)
    assert exc.value.status_code == 404 and store.writes == 0


def test_unknown_step_is_not_found():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, step="9.9")
    assert exc.value.code == "checklist_step_not_found"


@pytest.mark.parametrize("actor,is_approver", [
    ("user-pm", False), ("system:pipeline-runner", True), ("runner-9155eb0e", True),
])
def test_approver_item_cannot_be_marked_done_by_non_approver_or_runner(actor, is_approver):
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, step="4.3", actor=actor, actor_is_approver=is_approver)
    assert exc.value.status_code == 403 and store.writes == 0
    assert store.ms["completion_checklist"][2]["status"] == "pending"


def test_ceo_principal_records_approval_and_human_approver_revoking_keeps_prior_audit():
    store = Store(_pending_checklist())
    _update(store, step="4.3", actor="ceo-user-1", actor_is_approver=True)
    item = store.ms["completion_checklist"][2]
    assert item["status"] == "done" and item["approved_by"] == "ceo-user-1"
    approved_at = item["approved_at"]
    _update(store, step="4.3", status="pending", evidence="승인 철회 사유: 배치 구성 변경",
            expected_version=17, request_id="req-0002-abcd",
            actor="ceo-user-1", actor_is_approver=True)
    item = store.ms["completion_checklist"][2]
    assert item["status"] == "pending" and "approved_by" not in item
    assert [h["to"] for h in item["history"]] == ["done", "pending"]
    revoke = item["history"][1]
    assert revoke["prev_approved_by"] == "ceo-user-1" and revoke["prev_approved_at"] == approved_at
    assert revoke["prev_evidence"].startswith("docs/reports/") and revoke["actor_is_approver"] is True
    assert revoke["note"] == "승인 철회 사유: 배치 구성 변경"


def _approved_store():
    checklist = _pending_checklist()
    checklist[2] = {
        "step": "4.3", "owner": "CEO", "title": "배치 승인", "status": "done",
        "evidence": "existing approval evidence", "approved_by": "original-ceo",
        "approved_at": "2026-10-19T01:00:00Z",
    }
    return Store(checklist)


@pytest.mark.parametrize("actor,is_approver", [
    ("user-pm", False), ("system:pipeline-runner", True), ("runner-9155eb0e", True),
    ("", True),
])
@pytest.mark.parametrize("status,evidence", [
    ("pending", None), ("in_progress", None), ("blocked", None),
    ("done", "교체한 다른 근거 문장입니다"),
])
def test_non_approver_cannot_revoke_or_alter_existing_approval(actor, is_approver, status, evidence):
    store = _approved_store()
    before = copy.deepcopy(store.ms)
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, step="4.3", status=status, evidence=evidence,
                actor=actor, actor_is_approver=is_approver)
    assert exc.value.status_code == 403 and exc.value.code == "approver_required"
    assert store.ms == before and store.writes == 0


def test_non_approver_cannot_move_unapproved_approver_item_either():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, step="4.3", status="in_progress", evidence=None)
    assert exc.value.status_code == 403 and store.writes == 0


def test_non_approver_no_op_on_approved_item_changes_nothing():
    store = _approved_store()
    before = copy.deepcopy(store.ms)
    result = _update(store, step="4.3", evidence="existing approval evidence")
    assert result["changed"] is False and store.ms == before


def test_human_approver_revocation_preserves_original_approval_in_history():
    store = _approved_store()
    _update(store, step="4.3", status="pending", evidence="승인 철회: 증빙 재검토 필요",
            actor="second-ceo", actor_is_approver=True)
    item = store.ms["completion_checklist"][2]
    assert item["status"] == "pending" and "approved_by" not in item and "approved_at" not in item
    entry = item["history"][-1]
    assert entry["prev_approved_by"] == "original-ceo"
    assert entry["prev_approved_at"] == "2026-10-19T01:00:00Z"
    assert entry["prev_evidence"] == "existing approval evidence"
    assert entry["from"] == "done" and entry["to"] == "pending" and entry["actor"] == "second-ceo"
    assert mc.evaluate_checklist(store.ms["completion_checklist"])["satisfied"] is False


def test_request_id_reuse_by_other_actor_or_state_is_rejected_not_idempotent():
    store = _approved_store()
    _update(store, step="4.3", status="pending", evidence="승인 철회: 증빙 재검토 필요",
            actor="second-ceo", actor_is_approver=True, request_id="req-shared-01")
    before = copy.deepcopy(store.ms)
    # 철회한 주체가 아닌 PM 이 같은 request_id 로 접근 — 성공 응답을 받으면 안 된다.
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, step="4.3", status="pending", evidence=None, expected_version=17,
                actor="user-pm", actor_is_approver=False, request_id="req-shared-01")
    assert exc.value.code == "request_id_reused" and exc.value.status_code == 409
    # 같은 주체가 같은 request_id 로 다른 목표 상태를 요구해도 재사용 거절.
    with pytest.raises(mc.ChecklistError) as exc:
        _update(store, step="4.3", status="done", expected_version=17,
                actor="second-ceo", actor_is_approver=True, request_id="req-shared-01")
    assert exc.value.code == "request_id_reused"
    assert store.ms == before
    # 같은 주체·같은 목표의 재전송만 멱등이다.
    again = _update(store, step="4.3", status="pending", evidence="승인 철회: 증빙 재검토 필요",
                    expected_version=16, actor="second-ceo", actor_is_approver=True,
                    request_id="req-shared-01")
    assert again["idempotent"] is True and again["changed"] is False and store.ms == before


def test_failed_write_rolls_back_everything():
    store = Store(_pending_checklist())
    before = copy.deepcopy(store.ms)
    store.fail_update = True
    with pytest.raises(RuntimeError):
        _update(store)
    assert store.ms == before and store.writes == 0


# ── 잘못된 완료 정정 ────────────────────────────────────────────────────

def _reopen(store, **overrides):
    kwargs = dict(
        goal_id=GOAL, milestone_id=MS, tenant_id=TENANT_A, expected_version=16,
        request_id="fix-0001-abcd", reason="체크리스트 4건 pending 인데 완료 처리됨", actor="admin-1",
    )
    kwargs.update(overrides)
    return asyncio.run(mc.reopen_unverified_completion(store, **kwargs))


def test_reopen_corrects_unverified_completion_non_destructively():
    store = Store(_pending_checklist())
    before = copy.deepcopy(store.ms)
    result = _reopen(store)
    assert result["changed"] and result["status"] == "in_progress" and result["version"] == 17
    ms = store.ms
    assert ms["status"] == "in_progress" and ms["completed_at"] is None
    assert ms["evidence"] == before["evidence"] and ms["dispatch_count"] == 3
    assert ms["completion_checklist"] == before["completion_checklist"]
    assert ms["review_note"].startswith("기존 메모\n[완료 정정 fix-0001-abcd]")
    assert "version=16" in ms["review_note"] and "미충족=4.1,4.2,4.3,4.4" in ms["review_note"]


def test_reopen_is_idempotent_for_same_request():
    store = Store(_pending_checklist())
    _reopen(store)
    again = _reopen(store)
    assert again["changed"] is False and again["idempotent"] and store.writes == 1


def test_reopen_rejects_stale_version():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _reopen(store, expected_version=3)
    assert exc.value.code == "version_conflict" and store.ms["status"] == "completed"


def test_reopen_refuses_when_evidence_is_satisfied_or_no_checklist_or_not_completed():
    done = [{"step": "1", "owner": "PM", "status": "completed"}]
    for store, code in (
        (Store(done), "checklist_satisfied"),
        (Store(None), "no_explicit_checklist"),
        (Store(_pending_checklist(), status="in_progress"), "not_completed"),
    ):
        with pytest.raises(mc.ChecklistError) as exc:
            _reopen(store)
        assert exc.value.code == code and store.writes == 0


def test_reopen_other_tenant_and_closed_goal_and_short_reason():
    store = Store(_pending_checklist())
    with pytest.raises(mc.ChecklistError) as exc:
        _reopen(store, tenant_id=TENANT_B)
    assert exc.value.status_code == 404
    with pytest.raises(mc.ChecklistError) as exc:
        _reopen(store, reason="짧다")
    assert exc.value.code == "reason_required"
    closed = Store(_pending_checklist(), goal_status="completed")
    with pytest.raises(mc.ChecklistError) as exc:
        _reopen(closed)
    assert exc.value.code == "goal_closed" and closed.writes == 0


def test_reopen_db_failure_rolls_back():
    store = Store(_pending_checklist())
    before = copy.deepcopy(store.ms)
    store.fail_update = True
    with pytest.raises(RuntimeError):
        _reopen(store)
    assert store.ms == before


# ── 라우터: 권한·tenant·오류 매핑 ───────────────────────────────────────

def _ctx(tenant=TENANT_A, user_id="user-pm", internal=False, role="user"):
    return {"tenant": {"id": tenant},
            "user": {"user_id": user_id, "is_internal_admin": internal, "user_role": role}}


def _patch_pool(monkeypatch, store):
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _Pool(store))


def test_router_maps_checklist_errors_and_scopes_tenant(monkeypatch):
    store = Store(_pending_checklist())
    _patch_pool(monkeypatch, store)
    req = goals.ChecklistItemUpdateRequest(
        status="done", evidence="충분히 긴 근거 문장입니다", expected_version=16,
        request_id="req-router-01",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.update_milestone_checklist_item(
            GOAL, MS, "4.1", req, context=_ctx(tenant=TENANT_B)))
    assert exc.value.status_code == 404 and store.writes == 0
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.update_milestone_checklist_item(
            GOAL, MS, "4.3", req, context=_ctx(internal=True, user_id="system:pipeline-runner",
                                                role="system")))
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "approver_required"
    ok = asyncio.run(goals.update_milestone_checklist_item(
        GOAL, MS, "4.3", req, context=_ctx(internal=True, user_id="ceo-1", role="admin")))
    assert ok["changed"] and store.ms["completion_checklist"][2]["approved_by"] == "ceo-1"


def test_reopen_route_requires_admin_role():
    import inspect

    default = inspect.signature(goals.reopen_unverified_milestone).parameters["context"].default
    assert default.dependency.__qualname__ == goals.require_tenant_admin.__qualname__
    routes = {r.path: r for r in goals.router.routes}
    assert "/goals/{goal_id}/milestones/{milestone_id}/reopen-unverified" in routes
    assert "/goals/{goal_id}/milestones/{milestone_id}/checklist/{step}" in routes
