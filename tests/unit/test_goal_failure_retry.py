"""Failed links propose bounded rework while retaining blocked milestone evidence."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.services import goal_manager, next_step_proposals
from app.services.goal_failure_retry import ensure_retry_candidate, mark_explicit_retry_supersession

GOAL_ID = "1997c471-ba2e-4f0d-a1e3-9df0dd6d1240"
MILESTONE_ID = "b50ad968-124a-4b3d-82cd-3783c899889a"
SESSION_ID = "109dfc17-8c86-4584-9de9-b022eff40b23"


class FakeConn:
    def __init__(self, owner=SESSION_ID):
        self.owner = owner
        self.evidence = {}
        self.milestone_status = "in_progress"
        self.goal_status = "active"
        self.dispatch_note = None
        self.calls = []
        self.link_status = None
        self.role_session = None
        self.other_blocked = False
        self.lock = asyncio.Lock()
        self.link_superseded_by = None
        self.rework = None
        self.failed_links = ["job-1"]
        self.cards = {}
        self.queues = {}
        self.outer_transaction = False
        self.fail_finalize_once = False

    def is_in_transaction(self):
        return self.outer_transaction

    def transaction(self):
        conn = self
        class Transaction:
            async def __aenter__(self):
                await conn.lock.acquire()
            async def __aexit__(self, *_):
                conn.lock.release()
        return Transaction()

    async def fetch(self, query, *args):
        if "FROM goal_task_links l JOIN pipeline_jobs p" in query:
            return [self.rework] if self.rework else []
        if "SELECT DISTINCT milestone_id, goal_id" in query:
            return [{"milestone_id": MILESTONE_ID, "goal_id": GOAL_ID}]
        if "SELECT task_type, task_id, status, goal_id FROM goal_task_links" in query:
            rows = [{
                "task_type": "pipeline_job", "task_id": "job-1",
                "status": self.link_status, "goal_id": GOAL_ID,
            }]
            if self.link_superseded_by:
                rows = []
            if self.rework:
                rows.append({"task_type": "pipeline_job", "task_id": self.rework["task_id"],
                             "status": "completed", "goal_id": GOAL_ID})
            return rows
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "FROM chat_deferred_reactions q" in query:
            return self.queues.get(args[0])
        if "FROM agent_permission_requests" in query:
            return self.cards.get(args[0], {
                "decision": "pending", "expires_at": datetime.now(UTC) + timedelta(hours=1),
            })
        if "m.title, m.evidence" in query:
            return {
                "title": "실패 마일스톤", "evidence": self.evidence,
                "milestone_owner": self.owner, "goal_owner": None,
                "owner_role_key": "builder" if self.role_session else None,
                "tenant_id": GOAL_ID,
            }
        if "SELECT evidence FROM milestones" in query:
            return {"evidence": self.evidence}
        if "SELECT m.evidence, m.goal_id" in query:
            return {"evidence": self.evidence, "goal_id": GOAL_ID, "tenant_id": GOAL_ID}
        if "FROM pipeline_jobs WHERE job_id" in query:
            return {"status": "failed", "phase": "failed", "project": "GO100"}
        if "SELECT goal_id FROM milestones" in query:
            return {"goal_id": GOAL_ID}
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "FROM chat_sessions WHERE role_key" in query:
            assert "tenant_id = $2::uuid" in query
            return self.role_session
        if "FROM chat_sessions WHERE id" in query:
            assert "tenant_id = $2::uuid" in query
            return args[0] if args[0] == SESSION_ID else None
        return None

    async def execute(self, query, *args):
        self.calls.append((query, args))
        if "UPDATE goal_task_links l SET superseded_by" in query:
            self.link_superseded_by = args[3]
        elif "UPDATE milestones" in query and "status = 'blocked'" in query:
            self.milestone_status = "blocked"
        elif "UPDATE goals g SET status = 'active'" in query:
            assert "FROM goal_task_links l" in query
            assert "candidate->>'failed_task_id' = l.task_id" in query
            assert "card.decision IN ('pending', 'approved')" in query
            assert "card.expires_at IS NULL OR card.expires_at > NOW()" in query
            assert "FROM chat_deferred_reactions q" in query
            covered = all(any(
                item.get("failed_task_id") == task_id and item.get("card_id")
                and not item.get("failed") and (
                    self.queues.get(item["card_id"], {}).get("status") in
                    {"pending", "claimed", "completed"}
                    if item.get("card_kind") == "auto_queue" else
                    self.cards.get(item["card_id"], {
                        "decision": "pending", "expires_at": datetime.now(UTC) + timedelta(hours=1),
                    })["decision"] in {"pending", "approved"}
                )
                for item in self.evidence.get("retry_candidates", [])
            ) for task_id in self.failed_links)
            if not self.other_blocked and covered:
                self.goal_status = "active"
                return "UPDATE 1"
            return "UPDATE 0"
        elif "UPDATE goals" in query and "status = 'blocked'" in query:
            self.goal_status = "blocked"
        elif "SET evidence = $2::jsonb" in query:
            if self.fail_finalize_once and any(
                item.get("card_id") for item in json.loads(args[1]).get("retry_candidates", [])
            ):
                self.fail_finalize_once = False
                raise RuntimeError("finalization failed")
            self.evidence = json.loads(args[1])
        elif "SET dispatch_note" in query:
            self.dispatch_note = args[1]
        return "UPDATE 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


@pytest.fixture
def harness(monkeypatch):
    conn = FakeConn()
    machine = goal_manager.GoalStateMachine()
    async def pool():
        return FakePool(conn)
    async def columns(_conn):
        return set()
    async def no_op(*_args, **_kwargs):
        return None
    monkeypatch.setattr(machine, "_pool", pool)
    monkeypatch.setattr(machine, "_mark_superseded_failures", no_op)
    monkeypatch.setattr(machine, "_trace", no_op)
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    return conn, machine


def _fail(machine, task_id):
    return asyncio.run(machine.update_task_status("pipeline_job", task_id, "failed"))


def test_first_failure_creates_candidate_without_blocking_goal(harness, monkeypatch):
    conn, machine = harness
    proposals = []
    async def propose(**kwargs):
        proposals.append(kwargs)
        return {"cards": [{"id": "card-1"}], "auto": []}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = _fail(machine, "job-1")

    assert result["milestones_checked"][0]["retry_candidate"]["created"] is True
    assert conn.milestone_status == "blocked"
    assert conn.goal_status == "active"
    assert len(conn.evidence["retry_candidates"]) == 1
    assert proposals[0]["steps"][0]["detail"].splitlines()[2:4] == [
        f"GOAL_ID: {GOAL_ID}", f"MILESTONE_ID: {MILESTONE_ID}",
    ]


def test_same_failed_task_is_idempotent(harness, monkeypatch):
    conn, machine = harness
    proposed = []
    async def propose(**kwargs):
        proposed.append(kwargs)
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    _fail(machine, "job-1")
    result = _fail(machine, "job-1")

    assert result["milestones_checked"][0]["retry_candidate"] == {
        "created": False, "why": "already_proposed", "candidate_alive": True,
    }
    assert conn.goal_status == "active"
    assert len(conn.evidence["retry_candidates"]) == 1
    assert len(proposed) == 1


def test_third_distinct_failure_exhausts_retry_and_blocks_goal(harness, monkeypatch):
    conn, machine = harness
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    _fail(machine, "job-1")
    _fail(machine, "job-2")
    result = _fail(machine, "job-3")

    assert result["milestones_checked"][0]["retry_candidate"]["why"] == "retry_exhausted"
    assert conn.goal_status == "blocked"
    assert len(conn.evidence["retry_candidates"]) == 2
    assert conn.dispatch_note == "재작업 후보 2회 소진 — 사람이 확인해야 한다"


def test_missing_owner_preserves_goal_block(harness):
    conn, machine = harness
    conn.owner = None

    result = _fail(machine, "job-1")

    assert result["milestones_checked"][0]["retry_candidate"]["why"] == "no_owner_session"
    assert conn.milestone_status == "blocked"
    assert conn.goal_status == "blocked"


def test_proposal_exception_does_not_interrupt_block(harness, monkeypatch):
    conn, machine = harness
    async def propose(**_kwargs):
        raise RuntimeError("proposal unavailable")
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = _fail(machine, "job-1")

    assert result["milestones_checked"][0]["retry_candidate"]["created"] is False
    assert conn.milestone_status == "blocked"
    assert conn.goal_status == "blocked"
    assert conn.evidence["retry_candidates"][0]["failed"] is True
    assert conn.evidence["retry_candidates"][0]["failed_at"]
    assert conn.evidence["retry_candidates"][0]["card_id"] is None


@pytest.mark.parametrize(
    "link_status,reason",
    [("failed", "linked_task_failed"), ("pending", "pipeline_job_failed")],
)
def test_completion_check_proposes_from_both_failure_paths(
    harness, monkeypatch, link_status, reason,
):
    conn, machine = harness
    conn.link_status = link_status
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = asyncio.run(machine.check_milestone_completion(MILESTONE_ID))

    assert result["reason"] == reason
    assert result["retry_candidate"]["created"] is True
    assert conn.milestone_status == "blocked"
    assert conn.evidence["retry_candidates"][0]["reason"] == reason


def test_completed_rework_supersedes_original_failed_link(harness, monkeypatch):
    conn, machine = harness
    conn.link_status = "failed"
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": "job-1", "retry_of_link": "job-1", "card_id": "card-1",
        "at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }]}
    conn.rework = {
        "task_id": "job-2", "instruction": "GOAL_ID: " + GOAL_ID +
        "\nMILESTONE_ID: " + MILESTONE_ID + "\nRETRY_OF_LINK: job-1",
        "status": "done", "phase": "done", "project": "GO100",
        "goal_id": GOAL_ID, "milestone_id": MILESTONE_ID, "tenant_id": GOAL_ID,
        "created_at": datetime.now(UTC),
    }
    async def columns(_conn):
        return {"superseded_by"}
    async def no_op(*_args, **_kwargs):
        return None
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    monkeypatch.setattr(machine, "_advance_after_milestone", no_op)

    result = asyncio.run(machine.check_milestone_completion(MILESTONE_ID))

    assert result["completed"] is True
    assert conn.link_superseded_by == "job-2"
    assert conn.evidence["retry_candidates"][0]["superseded_by_task_id"] == "job-2"
    assert conn.milestone_status != "blocked"


@pytest.mark.parametrize("other_blocked,expected", [(False, "active"), (True, "blocked")])
def test_blocked_goal_recovers_only_when_all_blocks_have_live_candidates(
    harness, monkeypatch, other_blocked, expected,
):
    conn, _ = harness
    conn.goal_status = "blocked"
    conn.milestone_status = "blocked"
    conn.other_blocked = other_blocked
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))

    assert result["created"] is True
    assert conn.goal_status == expected


def test_concurrent_reservations_stop_at_two(harness, monkeypatch):
    conn, _ = harness
    async def propose(**kwargs):
        task_id = kwargs["steps"][0]["detail"].splitlines()[0].split(": ", 1)[1]
        assert any(
            item["failed_task_id"] == task_id and item["card_id"] is None
            for item in conn.evidence["retry_candidates"]
        )
        await asyncio.sleep(0)
        return {"cards": [{"id": kwargs["steps"][0]["title"]}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    async def run():
        return await asyncio.gather(*[
            ensure_retry_candidate(
                conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
                failed_task_id=f"job-{i}", reason="linked_task_failed",
            ) for i in range(3)
        ])

    results = asyncio.run(run())

    assert sum(item["created"] for item in results) == 2
    assert len(conn.evidence["retry_candidates"]) == 2
    assert all(item["card_id"] for item in conn.evidence["retry_candidates"])


def test_role_fallback_rejects_other_tenant_session(harness, monkeypatch):
    conn, _ = harness
    conn.owner = None
    conn.role_session = "00000000-0000-0000-0000-000000000001"
    proposed = []
    async def propose(**kwargs):
        proposed.append(kwargs)
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))

    assert result["why"] == "no_owner_session"
    assert proposed == []
    assert conn.evidence == {}


def test_distinct_failures_create_distinct_cards(harness, monkeypatch):
    conn, _ = harness
    seen = {}
    async def propose(**kwargs):
        title = kwargs["steps"][0]["title"]
        seen[title] = seen.get(title, 0) + 1
        return {"cards": [{"id": title}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    for task_id in ("abcdefgh-one", "ijklmnop-two"):
        assert asyncio.run(ensure_retry_candidate(
            conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
            failed_task_id=task_id, reason="linked_task_failed",
        ))["created"]

    assert len(seen) == 2
    assert len({item["card_id"] for item in conn.evidence["retry_candidates"]}) == 2
    assert all(len(title) <= 200 for title in seen)


def test_failed_proposal_backoff_then_retry_without_consuming_limit(harness, monkeypatch):
    conn, _ = harness
    calls = []
    async def propose(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unavailable")
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    async def attempt():
        return await ensure_retry_candidate(
            conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
            failed_task_id="job-1", reason="linked_task_failed",
        )

    assert asyncio.run(attempt())["why"] == "proposal_failed"
    assert asyncio.run(attempt())["why"] == "propose_backoff"
    assert len(calls) == 1
    conn.evidence["retry_candidates"][0]["failed_at"] = (
        datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    assert asyncio.run(attempt())["created"] is True
    assert len(calls) == 2
    assert sum(not item.get("failed") for item in conn.evidence["retry_candidates"]) == 1


def test_stale_unfinalized_reservation_can_retry(harness, monkeypatch):
    conn, _ = harness
    conn.evidence = {"retry_candidates": [{
        "reservation_id": "old", "at": (datetime.now(UTC) - timedelta(minutes=11)).isoformat(),
        "failed_task_id": "job-1", "card_id": None,
    }]}
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-new"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))
    assert result["created"] is True
    assert conn.evidence["retry_candidates"][0]["failed"] is True
    assert conn.evidence["retry_candidates"][1]["card_id"] == "card-new"


@pytest.mark.parametrize("decision,expired", [("rejected", False), ("pending", True)])
def test_dead_card_releases_retry_budget(harness, monkeypatch, decision, expired):
    conn, _ = harness
    conn.evidence = {"retry_candidates": [{
        "reservation_id": "old", "at": (datetime.now(UTC) - timedelta(minutes=11)).isoformat(),
        "failed_task_id": "job-1", "card_id": "card-old",
    }]}
    conn.cards["card-old"] = {
        "decision": decision,
        "expires_at": datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
    }
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-new"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    first = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))
    assert first["why"] == "propose_backoff"
    assert conn.evidence["retry_candidates"][0]["failed"] is True
    conn.evidence["retry_candidates"][0]["failed_at"] = (
        datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    second = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))
    assert second["created"] is True
    assert sum(not item.get("failed") for item in conn.evidence["retry_candidates"]) == 1


def test_finalize_failure_marks_reservation_failed(harness, monkeypatch):
    conn, _ = harness
    conn.fail_finalize_once = True
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    result = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))
    assert result["why"] == "proposal_failed"
    assert conn.evidence["retry_candidates"][0]["failed"] is True
    assert conn.evidence["retry_candidates"][0]["card_id"] is None


def test_three_failed_proposals_stop_same_link(harness, monkeypatch):
    conn, _ = harness
    async def propose(**_kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    async def attempt():
        return await ensure_retry_candidate(
            conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
            failed_task_id="job-1", reason="linked_task_failed",
        )
    for _ in range(3):
        assert asyncio.run(attempt())["why"] == "proposal_failed"
        conn.evidence["retry_candidates"][-1]["failed_at"] = (
            datetime.now(UTC) - timedelta(minutes=11)).isoformat()

    assert asyncio.run(attempt())["why"] == "propose_backoff"
    assert len(conn.evidence["retry_candidates"]) == 3


def test_goal_recovery_requires_candidate_for_every_failed_link(harness, monkeypatch):
    conn, _ = harness
    conn.goal_status = "blocked"
    conn.failed_links = ["job-1", "job-2"]
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    async def attempt(task_id):
        return await ensure_retry_candidate(
            conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
            failed_task_id=task_id, reason="linked_task_failed",
        )

    assert asyncio.run(attempt("job-1"))["created"]
    assert conn.goal_status == "blocked"
    assert asyncio.run(attempt("job-2"))["created"]
    assert conn.goal_status == "active"


@pytest.mark.parametrize("defect", [
    "missing_candidate", "wrong_milestone", "early_job", "other_tenant", "rejected_card",
])
def test_explicit_supersession_rejects_unrelated_job(harness, monkeypatch, caplog, defect):
    conn, _ = harness
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": "job-1", "card_id": "card-1",
        "at": datetime.now(UTC).isoformat(),
    }]}
    conn.rework = {
        "task_id": "job-2", "instruction": "RETRY_OF_LINK: job-1",
        "status": "done", "phase": "done", "project": "GO100",
        "goal_id": GOAL_ID, "milestone_id": MILESTONE_ID, "tenant_id": GOAL_ID,
        "created_at": datetime.now(UTC) + timedelta(minutes=1),
    }
    if defect == "missing_candidate":
        conn.evidence = {}
    elif defect == "wrong_milestone":
        conn.rework["milestone_id"] = SESSION_ID
    elif defect == "early_job":
        conn.rework["created_at"] = datetime.now(UTC) - timedelta(minutes=1)
    elif defect == "rejected_card":
        conn.cards["card-1"] = {
            "decision": "rejected", "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    else:
        conn.rework["tenant_id"] = SESSION_ID
    async def columns(_conn):
        return {"superseded_by"}
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)

    assert asyncio.run(mark_explicit_retry_supersession(conn, MILESTONE_ID)) == 0
    assert conn.link_superseded_by is None
    assert "goal_retry_supersede_rejected" in caplog.text


@pytest.mark.parametrize("why", ["propose_backoff", "already_proposed", "milestone_not_found", "reservation_missing"])
def test_failure_without_live_candidate_blocks_goal(harness, monkeypatch, why):
    conn, machine = harness
    async def unavailable(*_args, **_kwargs):
        return {"created": False, "why": why, "candidate_alive": False}
    monkeypatch.setattr(goal_manager, "ensure_retry_candidate", unavailable)

    _fail(machine, "job-1")
    assert conn.goal_status == "blocked"


def test_dead_card_still_consumes_cumulative_limit(harness, monkeypatch):
    conn, _ = harness
    conn.evidence = {"retry_candidates": [
        {"failed_task_id": f"job-{i}", "card_id": f"card-{i}",
         "at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
        for i in (1, 2)
    ]}
    for i in (1, 2):
        conn.cards[f"card-{i}"] = {"decision": "rejected", "expires_at": None}
    async def never(**_kwargs):
        raise AssertionError("retry budget should stop proposal")
    monkeypatch.setattr(next_step_proposals, "propose", never)

    result = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-3", reason="linked_task_failed",
    ))
    assert result["why"] == "retry_exhausted"


def test_rejected_candidate_blocks_previously_active_goal(harness, monkeypatch):
    conn, machine = harness
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": "job-1", "card_id": "card-1",
        "at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }]}
    conn.cards["card-1"] = {"decision": "rejected", "expires_at": None}
    async def never(**_kwargs):
        raise AssertionError("backoff should stop proposal")
    monkeypatch.setattr(next_step_proposals, "propose", never)

    result = _fail(machine, "job-1")
    assert result["milestones_checked"][0]["retry_candidate"]["why"] == "propose_backoff"
    assert conn.goal_status == "blocked"


def test_auto_queue_candidate_remains_alive_and_unblocks(harness, monkeypatch):
    conn, machine = harness
    conn.goal_status = "blocked"
    conn.queues["queue-1"] = {"status": "pending"}
    calls = []
    async def propose(**kwargs):
        calls.append(kwargs)
        return {"cards": [], "auto": [{"queued": True, "queue_id": "queue-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)

    first = _fail(machine, "job-1")
    second = _fail(machine, "job-1")
    assert first["milestones_checked"][0]["retry_candidate"]["created"]
    assert second["milestones_checked"][0]["retry_candidate"]["candidate_alive"]
    assert conn.goal_status == "active"
    assert len(calls) == 1
    assert conn.evidence["retry_candidates"][0]["card_kind"] == "auto_queue"


def test_null_card_expiry_stays_alive(harness):
    conn, _ = harness
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": "job-1", "card_id": "card-1", "at": datetime.now(UTC).isoformat(),
    }]}
    conn.cards["card-1"] = {"decision": "approved", "expires_at": None}
    result = asyncio.run(ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id="job-1", reason="linked_task_failed",
    ))
    assert result["candidate_alive"] is True


def test_naive_job_timestamp_can_supersede(harness, monkeypatch):
    conn, _ = harness
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": "job-1", "card_id": "card-1",
        "at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }]}
    conn.rework = {
        "task_id": "job-2", "instruction": "RETRY_OF_LINK: job-1",
        "status": "done", "phase": "done", "project": "GO100",
        "goal_id": GOAL_ID, "milestone_id": MILESTONE_ID, "tenant_id": GOAL_ID,
        "created_at": datetime.now(UTC).replace(tzinfo=None),
    }
    async def columns(_conn):
        return {"superseded_by"}
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    assert asyncio.run(mark_explicit_retry_supersession(conn, MILESTONE_ID)) == 1


def test_outer_transaction_and_missing_goal_do_not_propose(harness, monkeypatch):
    conn, _ = harness
    async def never(**_kwargs):
        raise AssertionError("must not propose")
    monkeypatch.setattr(next_step_proposals, "propose", never)
    conn.outer_transaction = True
    args = {"milestone_id": MILESTONE_ID, "goal_id": GOAL_ID,
            "failed_task_id": "job-1", "reason": "linked_task_failed"}
    assert asyncio.run(ensure_retry_candidate(conn, **args))["why"] == "outer_transaction"
    conn.outer_transaction = False
    assert asyncio.run(ensure_retry_candidate(conn, **{**args, "goal_id": ""}))["why"] == "missing_goal_id"
    assert conn.evidence == {}
