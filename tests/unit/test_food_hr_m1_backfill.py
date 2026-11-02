"""FOOD HR M1 소급 연결 스크립트 — 계획 규칙 · 기본 dry-run · 멱등 · 롤백.

DB 없이 돈다. asyncpg 는 가짜 모듈로 바꾸고 스크립트의 SQL 문자열로 분기하는 인메모리 커넥션을 쓴다.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _load():
    path = REPO / "scripts" / "backfill_food_hr_m1_links.py"
    spec = importlib.util.spec_from_file_location("backfill_food_hr_m1_links", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bf = _load()
JOB_A, JOB_B = sorted(bf.TARGETS)
DIRECTIVE = f"GOAL_ID: {bf.GOAL_ID}\nMILESTONE_ID: {bf.MILESTONE_ID}\nPUSH_ONLY:true\n"


def _job(job_id=JOB_A, **over):
    row = {
        "job_id": job_id, "tenant_id": TENANT, "project": "AADS", "status": "done", "phase": "done",
        "commit_hash": bf.TARGETS[job_id], "goal_id": None, "milestone_id": None,
        "instruction": DIRECTIVE, "deployed_at": None,
    }
    row.update(over)
    return row


def _goal(**over):
    return {"goal_id": bf.GOAL_ID, "tenant_id": TENANT, "project": "FOOD", "status": "active", **over}


def _ms(**over):
    return {
        "milestone_id": bf.MILESTONE_ID, "goal_id": bf.GOAL_ID, "tenant_id": TENANT,
        "project": "FOOD", "status": "in_progress", **over,
    }


def _plan(job=None, goal=None, ms=None, links=(), job_id=JOB_A):
    return bf.plan_target(
        job_id, _job(job_id) if job is None else job, _goal() if goal is None else goal,
        _ms() if ms is None else ms, list(links),
    )


def test_targets_are_exactly_the_two_known_jobs():
    assert bf.TARGETS == {
        "runner-09c0fef5": "1ac76d9faeb2cd6869a2cae979b8d9f04295c0ba",
        "runner-e7791459": "6cfd72fe9f2ea4507c57accb4448065af2f5d1df",
    }


def test_valid_target_is_linkable():
    assert _plan() == ("link", "linkable")


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"job": _job(status="error")}, "job_status=error"),
        ({"job": _job(commit_hash="0" * 40)}, "commit_mismatch"),
        ({"job": _job(project="FOOD")}, "job_project=FOOD"),
        ({"job": _job(instruction="GOAL_ID: 11111111-1111-4111-8111-111111111111\n")}, "directive_mismatch"),
        ({"job": _job(instruction="지시만")}, "directive_mismatch"),
        ({"goal": _goal(tenant_id="other")}, "cross_tenant_goal"),
        ({"goal": _goal(project="GO100")}, "cross_project goal=GO100"),
        ({"goal": _goal(status="completed")}, "goal_status=completed"),
        ({"ms": _ms(goal_id="11111111-1111-4111-8111-111111111111")}, "milestone_mismatch"),
        ({"ms": _ms(tenant_id="other")}, "milestone_mismatch"),
        ({"job": _job(goal_id="11111111-1111-4111-8111-111111111111", milestone_id=None)},
         "conflict_job_bound_elsewhere"),
    ],
)
def test_invalid_inputs_are_skipped(kwargs, reason):
    assert _plan(**kwargs) == ("skip", reason)


def test_missing_rows_are_skipped():
    assert bf.plan_target(JOB_A, None, _goal(), _ms(), []) == ("skip", "job_not_found")
    assert bf.plan_target(JOB_A, _job(), None, _ms(), []) == ("skip", "goal_not_found")
    assert bf.plan_target(JOB_A, _job(), _goal(), None, []) == ("skip", "milestone_mismatch")


def test_already_linked_is_noop_and_half_linked_is_repaired():
    done = _job(goal_id=bf.GOAL_ID, milestone_id=bf.MILESTONE_ID)
    link = {"goal_id": bf.GOAL_ID, "milestone_id": bf.MILESTONE_ID, "link_state": "active"}
    assert _plan(job=done, links=[link]) == ("skip", "existing_link_preserved")
    assert _plan(job=done, links=[]) == ("link", "linkable")
    assert _plan(job=done, links=[{**link, "link_state": "detached"}]) == ("skip", "existing_link_preserved")


def test_sql_never_writes_goal_milestone_or_release_state():
    writes = [bf.JOB_UPDATE_SQL, bf.LINK_UPSERT_SQL, bf.LINK_DETACH_SQL, bf.JOB_REVERT_SQL]
    for sql in writes:
        assert "UPDATE goals" not in sql and "UPDATE milestones" not in sql
        assert "dispatch_blocked_at" not in sql
        assert "deployed_at" not in sql
    assert "release_sha" not in bf.LINK_UPSERT_SQL and "status = 'completed'" not in bf.LINK_UPSERT_SQL
    assert "'pending'" in bf.LINK_UPSERT_SQL
    assert "updated_at" not in bf.JOB_UPDATE_SQL and "goal_id IS NULL" in bf.JOB_UPDATE_SQL


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.before = copy.deepcopy((self.conn.jobs, self.conn.links, self.conn.goal, self.conn.milestone))
        return self

    async def __aexit__(self, *exc):
        if exc[0]:
            self.conn.jobs, self.conn.links, self.conn.goal, self.conn.milestone = self.before
        return False


class _Conn:
    def __init__(self, goal_status="active"):
        self.jobs = {j: _job(j) for j in bf.TARGETS}
        self.links: list[dict] = []
        self.goal = {"id": bf.GOAL_ID, "status": goal_status, "updated_at": "t0"}
        self.milestone = {"id": bf.MILESTONE_ID, "status": "in_progress", "dispatch_blocked_at": "t0"}
        self.writes: list[str] = []
        self.closed = False

    def transaction(self):
        return _Tx(self)

    async def close(self):
        self.closed = True

    async def fetch(self, query, *args):
        if query == bf.LINK_COLUMNS_SQL:
            return [{"column_name": c} for c in (
                "bind_source", "bound_by", "link_state", "detach_reason", "last_job_status",
                "release_deploy_run_id")]
        if query == bf.JOB_LOCK_SQL:
            return []
        if query == bf.JOB_SQL:
            return [dict(self.jobs[j]) for j in sorted(self.jobs)]
        if query == bf.LINKS_SQL:
            return [dict(lk) for lk in self.links]
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if query == bf.GUARD_SQL:
            return {"goal": json.dumps(self.goal), "milestone": json.dumps(self.milestone)}
        if query == bf.GOAL_SQL:
            return _goal(status=self.goal["status"])
        if query == bf.MILESTONE_SQL:
            return _ms()
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.writes.append(query)
        if query == bf.JOB_UPDATE_SQL:
            job = self.jobs[args[0]]
            if job["goal_id"]:
                return "UPDATE 0"
            job["goal_id"], job["milestone_id"] = args[1], args[2]
            return "UPDATE 1"
        if query == bf.LINK_UPSERT_SQL:
            existing = next((lk for lk in self.links if lk["task_id"] == args[3] and lk["goal_id"] == args[1]), None)
            if existing:
                return "INSERT 0 0"
            row = {
                "id": args[3], "tenant_id": args[0], "last_job_status": "pending", "goal_id": args[1], "milestone_id": args[2], "task_id": args[3],
                "status": "pending", "link_state": "active", "bind_source": args[4],
                "bound_by": args[5], "release_deploy_run_id": None,
            }
            if existing:
                existing.update(row)
            else:
                self.links.append(row)
            return "INSERT 0 1"
        if query == bf.LINK_DETACH_SQL:
            for lk in self.links:
                if lk["task_id"] == args[0] and lk["bound_by"] == args[2]:
                    lk["link_state"] = "detached"
            return "UPDATE 1"
        if query == bf.JOB_REVERT_SQL:
            self.jobs[args[0]]["goal_id"] = self.jobs[args[0]]["milestone_id"] = None
            return "UPDATE 1"
        raise AssertionError(query)


@pytest.fixture
def conn(monkeypatch):
    c = _Conn()

    async def connect(_dsn):
        return c

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    return c


def test_default_is_dry_run_and_writes_nothing(conn, capsys):
    assert bf.main([]) == 0
    assert conn.writes == [] and conn.closed
    out = capsys.readouterr().out
    assert "mode=DRY-RUN" in out and out.count("plan runner-") == 2 and "link (linkable)" in out
    assert "--- before ---" in out and "dispatch_blocked_at=t0" in out


def test_apply_links_both_jobs_pending_without_release_evidence(conn):
    assert bf.main(["--apply"]) == 0
    assert all(j["goal_id"] == bf.GOAL_ID and j["milestone_id"] == bf.MILESTONE_ID for j in conn.jobs.values())
    assert len(conn.links) == 2
    assert {lk["status"] for lk in conn.links} == {"pending"}
    assert all(lk["bound_by"] == bf.BOUND_BY and lk["release_deploy_run_id"] is None for lk in conn.links)
    assert all(j["deployed_at"] is None and j["status"] == "done" for j in conn.jobs.values())
    assert conn.goal["status"] == "active" and conn.milestone["dispatch_blocked_at"] == "t0"


def test_apply_is_idempotent(conn):
    bf.main(["--apply"])
    conn.writes.clear()
    assert bf.main(["--apply"]) == 0
    assert conn.writes == [] and len(conn.links) == 2


def test_revert_dry_run_then_apply(conn):
    bf.main(["--apply"])
    conn.writes.clear()
    assert bf.main(["--revert"]) == 0
    assert conn.writes == []
    assert bf.main(["--revert", "--apply"]) == 0
    assert all(j["goal_id"] is None for j in conn.jobs.values())
    assert {lk["link_state"] for lk in conn.links} == {"detached"}


def test_guard_rolls_back_if_goal_row_changes(conn, monkeypatch):
    original = conn.execute

    async def tamper(query, *args):
        result = await original(query, *args)
        if query == bf.LINK_UPSERT_SQL:
            conn.goal["status"] = "completed"
        return result

    monkeypatch.setattr(conn, "execute", tamper)
    with pytest.raises(RuntimeError, match="goals/milestones"):
        bf.main(["--apply"])


def test_blocked_goal_is_still_linked_but_never_unblocked(conn):
    conn.goal["status"] = "blocked"
    assert bf.main(["--apply"]) == 0
    assert conn.goal["status"] == "blocked"
    assert len(conn.links) == 2


def test_other_active_goal_link_is_conflict():
    assert _plan(links=[{"goal_id": "other", "link_state": "active"}]) == (
        "skip", "conflict_link_bound_elsewhere")


def test_existing_job_binding_survives_link_only_revert(conn):
    conn.jobs[JOB_A].update(goal_id=bf.GOAL_ID, milestone_id=bf.MILESTONE_ID)
    bf.main(["--apply"])
    assert next(lk for lk in conn.links if lk["task_id"] == JOB_A)["bound_by"] == bf.LINK_ONLY_BOUND_BY
    bf.main(["--revert", "--apply"])
    assert conn.jobs[JOB_A]["goal_id"] == bf.GOAL_ID
    assert conn.jobs[JOB_B]["goal_id"] is None


@pytest.mark.parametrize("state", ["detached", "active"])
@pytest.mark.parametrize("status", ["pending", "completed", "active"])
@pytest.mark.parametrize("milestone", [bf.MILESTONE_ID, "old"])
@pytest.mark.parametrize("owner", ["other", "backfill:food-hr-m1:job-created"])
def test_existing_link_all_fields_preserved(conn, state, status, milestone, owner):
    conn.links.append({"task_id": JOB_A, "goal_id": bf.GOAL_ID, "milestone_id": milestone,
                       "tenant_id": TENANT, "link_state": state, "status": status,
                       "last_job_status": "done", "detach_reason": "manual", "updated_at": "original",
                       "bind_source": "old", "bound_by": owner, "release_deploy_run_id": None})
    before = copy.deepcopy(conn.links)
    job_before = copy.deepcopy(conn.jobs[JOB_A])
    bf.main(["--apply"])
    bf.main(["--revert", "--apply"])
    assert [lk for lk in conn.links if lk["task_id"] == JOB_A] == before
    assert conn.jobs[JOB_A] == job_before


def test_release_evidence_with_wrong_milestone_is_skipped(conn):
    conn.links.append({"task_id": JOB_A, "goal_id": bf.GOAL_ID, "milestone_id": "old",
                       "link_state": "active", "status": "completed", "bind_source": "old",
                       "bound_by": "other", "release_deploy_run_id": "release"})
    bf.main(["--apply"])
    assert conn.jobs[JOB_A]["goal_id"] is None


@pytest.mark.parametrize("mode", ["zero_rows", "wrong_milestone", "wrong_tenant"])
def test_failed_upsert_or_final_mismatch_aborts(conn, monkeypatch, mode):
    original = conn.execute

    async def broken(query, *args):
        if query == bf.LINK_UPSERT_SQL and mode == "zero_rows":
            return "INSERT 0 0"
        result = await original(query, *args)
        if query == bf.LINK_UPSERT_SQL:
            conn.links[-1]["milestone_id" if mode == "wrong_milestone" else "tenant_id"] = "wrong"
        return result

    monkeypatch.setattr(conn, "execute", broken)
    with pytest.raises(RuntimeError, match="link upsert failed|final binding mismatch"):
        bf.main(["--apply"])

    assert all(job["goal_id"] is None for job in conn.jobs.values())
    assert conn.links == []


@pytest.mark.parametrize("field,value", [("tenant_id", "other"), ("goal_id", "other"),
                                        ("release_deploy_run_id", "release")])
def test_foreign_or_evidenced_link_unchanged(conn, field, value):
    conn.links.append({"task_id": JOB_A, "goal_id": bf.GOAL_ID, "milestone_id": bf.MILESTONE_ID,
                       "tenant_id": TENANT, "link_state": "active", "status": "completed",
                       "bind_source": "explicit_directive", "bound_by": bf.BOUND_BY,
                       "release_deploy_run_id": None, field: value})
    before = copy.deepcopy(conn.links)
    bf.main(["--apply"])
    bf.main(["--revert", "--apply"])
    assert conn.links[0] == before[0]
    assert conn.jobs[JOB_A]["goal_id"] is None


@pytest.mark.parametrize("failure", ["race_insert", "update_zero"])
def test_conflict_rolls_back_job_update(conn, monkeypatch, failure):
    original = conn.execute
    before = copy.deepcopy(conn.jobs)

    async def execute(query, *args):
        if failure == "race_insert" and query == bf.LINK_UPSERT_SQL:
            return "INSERT 0 0"
        if failure == "update_zero" and query == bf.JOB_UPDATE_SQL:
            return "UPDATE 0"
        return await original(query, *args)

    monkeypatch.setattr(conn, "execute", execute)
    with pytest.raises(RuntimeError):
        bf.main(["--apply"])
    assert conn.jobs == before
    assert conn.links == []


def test_partial_existing_job_milestone_is_preserved(conn):
    conn.jobs[JOB_A]["milestone_id"] = bf.MILESTONE_ID
    before = copy.deepcopy(conn.jobs[JOB_A])
    bf.main(["--apply"])
    bf.main(["--revert", "--apply"])
    assert conn.jobs[JOB_A] == before


def test_insert_conflict_sql_cannot_update_existing_row():
    assert "DO NOTHING" in bf.LINK_UPSERT_SQL
    assert "DO UPDATE" not in bf.LINK_UPSERT_SQL


@pytest.mark.parametrize("statement", [bf.LINK_DETACH_SQL, bf.JOB_REVERT_SQL])
def test_revert_zero_rows_rolls_back_everything(conn, monkeypatch, statement):
    bf.main(["--apply"])
    before = copy.deepcopy((conn.jobs, conn.links))
    original = conn.execute

    async def execute(query, *args):
        if query == statement:
            return "UPDATE 0"
        return await original(query, *args)

    monkeypatch.setattr(conn, "execute", execute)
    with pytest.raises(RuntimeError, match="detach failed|revert failed"):
        bf.main(["--revert", "--apply"])
    assert (conn.jobs, conn.links) == before


def test_revert_and_apply_retries_never_reactivate_detached_rows(conn):
    bf.main(["--apply"])
    bf.main(["--revert", "--apply"])
    before = copy.deepcopy((conn.jobs, conn.links))
    conn.writes.clear()
    bf.main(["--revert", "--apply"])
    bf.main(["--apply"])
    assert (conn.jobs, conn.links) == before
    assert conn.writes == []
