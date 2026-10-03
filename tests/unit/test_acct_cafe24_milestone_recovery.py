"""ACCT 카페24 마일스톤 복구 스크립트 — 멱등성·tenant 격리·허위 완료 방지만 고정한다."""
from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "acct_cafe24_milestone_recovery.py"
_spec = importlib.util.spec_from_file_location("acct_cafe24_milestone_recovery", SCRIPT)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)

OWNER = "acc75e55-0917-4a01-9a01-000000000002"
STALE = "8ad08cc2-620c-4a70-8305-74a8d9b43c4e"


def _ms(seq, status, **kw):
    base = {
        "id": f"m{seq}", "title": f"M{seq} t", "sequence_order": seq, "status": status,
        "auto_advance": False, "owner_session_id": STALE, "evidence": {}, "reported_at": None,
        "review_note": None, "dispatch_note": None, "owner_linked_same_goal": True,
    }
    base.update(kw)
    return base


def _link(lid, mid, task, status, state="active"):
    return {"id": lid, "milestone_id": mid, "task_type": "pipeline_job", "task_id": task,
            "status": status, "link_state": state}


def _state(**over):
    state = {
        "goal": {"id": rec.GOAL_ID, "tenant_id": rec.TENANT_ID, "status": "blocked", "owner_session_id": OWNER},
        "milestones": [
            _ms(0, "completed", evidence={"summary": "ok"}, reported_at="2026-09-23"),
            _ms(1, "blocked"), _ms(2, "blocked"), _ms(3, "completed"), _ms(4, "pending"),
        ],
        "links": [
            _link("l1", "m1", "runner-4b4e75f4", "failed"),
            _link("l2", "m2", "runner-6aec5740", "failed"),
            _link("l3", "m3", rec.M3_FALSE_COMPLETION_JOB, "completed"),
            _link("l4", "m1", rec.M1_STALE_PENDING_JOB, "pending"),
        ],
        "jobs": {rec.M1_STALE_PENDING_JOB: {"status": "done"}},
        "owner_linked": True, "live_markers": {}, "live_jobs": {},
    }
    state.update(over)
    return state


def test_false_completion_rewound_but_verified_milestone_kept():
    plan = rec.build_plan(_state())
    rewinds = [a["milestone_id"] for a in plan if a["kind"] == "rewind"]
    assert rewinds == ["m3"]  # 증거 있는 M0 은 건드리지 않는다
    detached = {a["link_id"] for a in plan if a["kind"] == "detach_link"}
    assert detached == {"l3", "l4"}  # 허위 완료 링크 + stale pending 링크만
    assert not any(a["kind"] == "remediate" and a["milestone_id"] == "m0" for a in plan)


def test_recovery_bookkeeping_is_not_completion_evidence():
    ms = _ms(3, "completed", evidence={"acct_cafe24_recovery": [{"k": 1}], "retry_candidates": [{"x": 1}]})
    assert rec.is_false_completion(ms)
    assert not rec.is_false_completion(_ms(3, "completed", evidence={"summary": "real"}))
    assert not rec.is_false_completion(_ms(3, "completed", reported_at="2026-10-01"))


def test_second_run_on_converged_state_is_noop():
    state = _state()
    for m in state["milestones"]:
        m["auto_advance"] = True
        if m["status"] != "completed":
            m["owner_session_id"] = OWNER
    state["milestones"][3].update(status="pending", owner_session_id=OWNER,
                                  dispatch_note=f"{rec.RECOVERY_KEY}: rewind")
    state["links"][2]["link_state"] = "detached"
    state["links"][3]["link_state"] = "detached"
    state["live_markers"] = {f"{rec.RECOVERY_KEY}:M{i}": True for i in (1, 2, 3)}
    assert rec.build_plan(state) == []


def test_live_job_or_queue_blocks_duplicate_remediation():
    state = _state(live_jobs={"m1": ["runner-live"]}, live_markers={f"{rec.RECOVERY_KEY}:M2": True})
    keys = [a["key"] for a in rec.build_plan(state) if a["kind"] == "remediate"]
    assert keys == [f"{rec.RECOVERY_KEY}:M3"]


def test_foreign_tenant_goal_aborts_before_any_action():
    state = _state()
    state["goal"]["tenant_id"] = "00000000-0000-0000-0000-000000000000"
    assert rec.build_plan(state) == [{"kind": "abort", "why": "tenant_or_goal_mismatch"}]


def test_every_write_is_scoped_by_tenant_and_goal():
    src = SCRIPT.read_text(encoding="utf-8")
    updates = re.findall(r'"(UPDATE (?:milestones|goal_task_links) .*?)"\s*,', src, flags=re.S)
    assert len(updates) >= 4
    for stmt in updates:
        assert "tenant_id" in stmt, stmt[:80]


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, locked=None):
        self.sql: list[str] = []
        self._locked = locked

    def transaction(self):
        return _FakeTx()

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return self._locked

    async def execute(self, sql, *args):
        self.sql.append(sql)
        return "UPDATE 0"


def test_rewind_skipped_when_evidence_appeared_after_planning():
    conn = _FakeConn(locked={"status": "completed", "evidence": {"summary": "x"}, "reported_at": None, "review_note": None})
    out = asyncio.run(rec.apply_action(conn, {"kind": "rewind", "milestone_id": "m3", "reason": "r"},
                                        rec.TENANT_ID, rec.GOAL_ID))
    assert out == {"rewind": "skipped_precondition_changed"}
    assert not any(s.lstrip().startswith("UPDATE") for s in conn.sql)
