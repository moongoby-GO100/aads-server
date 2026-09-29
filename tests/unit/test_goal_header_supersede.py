"""SUPERSEDES / AUTO_REWORK_OF 헤더로 실패 링크를 승계한다 (instruction_hash 무관)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.services import goal_manager
from app.services.goal_binding import RELEASE_CERTIFIED_PHASE
from app.services.goal_failure_retry import (
    _completion_accept_reason,
    _has_release_evidence_column,
    mark_explicit_retry_supersession,
    parse_supersede_markers,
)

GOAL_ID = "1997c471-ba2e-4f0d-a1e3-9df0dd6d1240"
MILESTONE_ID = "b50ad968-124a-4b3d-82cd-3783c899889a"
OTHER_ID = "109dfc17-8c86-4584-9de9-b022eff40b23"
FAILED = "runner-aabbccdd"
REWORK = "runner-11223344"
T0 = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)


_EVIDENCE_KEYS = ("release_deploy_run_id", "deploy_status", "deploy_phase",
                  "image_digest", "standby_digest")


class FakeConn:
    """goal_task_links 한 행(실패 링크)과 pipeline_jobs 몇 행을 흉내 낸다."""

    def __init__(self):
        self.evidence = {}
        self.links = {FAILED: {"status": "failed", "superseded_by": None,
                               "milestone_id": MILESTONE_ID, "goal_id": GOAL_ID}}
        self.jobs = {FAILED: {"created_at": T0, "instruction_hash": "hash-a"}}
        self.replacements = []
        self.release_column = False
        self.probe_error = None
        self.probes = 0
        self.queries = []
        self.evidence_writes = 0
        self.lock = asyncio.Lock()

    def transaction(self):
        conn = self

        class Transaction:
            async def __aenter__(self):
                await conn.lock.acquire()

            async def __aexit__(self, *_):
                conn.lock.release()
        return Transaction()

    async def fetch(self, query, *args):
        if "information_schema.columns" in query:
            assert "column_name = 'release_deploy_run_id'" in query
            self.probes += 1
            if self.probe_error:
                raise self.probe_error
            return [{"?column?": 1}] if self.release_column else []
        if "FROM goal_task_links l JOIN pipeline_jobs p" in query:
            self.queries.append(query)
            if self.release_column:
                return list(self.replacements)
            return [{k: v for k, v in row.items() if k not in _EVIDENCE_KEYS}
                    for row in self.replacements]
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "SELECT m.evidence, m.goal_id" in query:
            return {"evidence": self.evidence, "goal_id": GOAL_ID, "tenant_id": GOAL_ID}
        if "SELECT evidence FROM milestones" in query:
            return {"evidence": self.evidence}
        if "SELECT p.created_at FROM pipeline_jobs p" in query:
            assert args[1] == GOAL_ID
            job = self.jobs.get(args[0])
            return {"created_at": job["created_at"]} if job else None
        if "FROM agent_permission_requests" in query:
            return {"decision": "pending", "expires_at": datetime.now(UTC) + timedelta(hours=1)}
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "UPDATE goal_task_links l SET superseded_by" in query:
            milestone_id, goal_id, _tenant, replacement, failed_id = args
            link = self.links.get(failed_id)
            if (link and link["status"] == "failed" and link["superseded_by"] is None
                    and link["milestone_id"] == milestone_id and link["goal_id"] == goal_id):
                link["superseded_by"] = replacement
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET evidence = $2::jsonb" in query:
            self.evidence_writes += 1
            self.evidence = json.loads(args[1])
            return "UPDATE 1"
        raise AssertionError(query)


def _replacement(instruction, **overrides):
    row = {
        "task_id": REWORK, "instruction": instruction,
        "status": "done", "phase": RELEASE_CERTIFIED_PHASE, "project": "AADS",
        "goal_id": GOAL_ID, "milestone_id": MILESTONE_ID, "tenant_id": GOAL_ID,
        "created_at": T0 + timedelta(minutes=5),
    }
    row.update(overrides)
    return row


@pytest.fixture
def conn(monkeypatch):
    async def columns(_conn):
        return {"superseded_by"}
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    return FakeConn()


def _run(conn):
    return asyncio.run(mark_explicit_retry_supersession(conn, MILESTONE_ID))


# 1) 파서 ----------------------------------------------------------------------

def test_parse_auto_rework_of_single():
    text = "ALLOW_DUP_JOB\nAUTO_REWORK_OF: runner-aabbccdd\nAUTO_REWORK_ROUND: 1/2\n"
    assert parse_supersede_markers(text) == [("runner-aabbccdd", "auto_rework_of")]


def test_parse_auto_rework_of_takes_first_token_only():
    text = "AUTO_REWORK_OF: runner-aabbccdd runner-11223344\n"
    assert parse_supersede_markers(text) == [("runner-aabbccdd", "auto_rework_of")]


def test_parse_supersedes_strips_parenthetical_comments():
    text = ("TASK_ID: X\n"
            "SUPERSEDES: runner-aabbccdd(review_failed 0.65, 커밋 eeca930a — runner-deadbeef 참고),"
            " runner-11223344\n본문")
    assert parse_supersede_markers(text) == [
        ("runner-aabbccdd", "supersedes"), ("runner-11223344", "supersedes"),
    ]


@pytest.mark.parametrize("text", [
    "본문에서 SUPERSEDES: runner-aabbccdd 를 언급한다",
    "설명: AUTO_REWORK_OF: runner-aabbccdd",
    "SUPERSEDES: job-1, runner-XYZ12345, runner-aabbccd",
    "SUPERSEDES: runner-aabbccddee",
    "SUPERSEDES: (runner-aabbccdd 는 제외)",
])
def test_parse_ignores_mid_text_and_malformed(text):
    assert parse_supersede_markers(text) == []


def test_parse_keeps_retry_of_link_and_dedupes():
    text = "RETRY_OF_LINK: runner-aabbccdd\nSUPERSEDES: runner-aabbccdd, runner-11223344\n"
    assert parse_supersede_markers(text) == [
        ("runner-aabbccdd", "retry_of_link"), ("runner-11223344", "supersedes"),
    ]
    assert parse_supersede_markers("RETRY_OF_LINK: job-1") == [("job-1", "retry_of_link")]


# 2) 해시가 달라도 헤더로 승계 / 3) 카드 없이 승계 ----------------------------------

def test_auto_rework_of_supersedes_despite_different_instruction_hash(conn, caplog):
    conn.jobs[REWORK] = {"created_at": T0 + timedelta(minutes=5), "instruction_hash": "hash-b"}
    assert conn.jobs[REWORK]["instruction_hash"] != conn.jobs[FAILED]["instruction_hash"]
    conn.replacements = [_replacement(f"ALLOW_DUP_JOB\nAUTO_REWORK_OF: {FAILED}\n")]

    with caplog.at_level(logging.INFO):
        assert _run(conn) == 1

    assert conn.links[FAILED]["superseded_by"] == REWORK
    assert "goal_header_supersede_without_candidate" in caplog.text
    assert conn.evidence_writes == 0


def test_supersedes_multiple_failed_jobs(conn):
    other = "runner-99887766"
    conn.links[other] = dict(conn.links[FAILED])
    conn.jobs[other] = {"created_at": T0}
    conn.replacements = [_replacement(f"SUPERSEDES: {FAILED}(review_failed, 주석), {other}\n")]

    assert _run(conn) == 2
    assert conn.links[FAILED]["superseded_by"] == REWORK
    assert conn.links[other]["superseded_by"] == REWORK


def test_header_with_candidate_updates_evidence(conn, caplog):
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": FAILED, "card_id": "card-1", "at": (T0 + timedelta(hours=1)).isoformat(),
    }]}
    conn.replacements = [_replacement(f"AUTO_REWORK_OF: {FAILED}\n")]

    with caplog.at_level(logging.INFO):
        assert _run(conn) == 1

    assert conn.links[FAILED]["superseded_by"] == REWORK
    assert conn.evidence["retry_candidates"][0]["superseded_by_task_id"] == REWORK
    assert "goal_header_supersede_without_candidate" not in caplog.text


# 4) 거절 ------------------------------------------------------------------------

@pytest.mark.parametrize("defect,reason", [
    ("other_milestone", "link_scope_mismatch"),
    ("other_goal", "link_scope_mismatch"),
    ("other_tenant", "tenant_mismatch"),
    ("self", "self_replacement"),
    ("created_before_failed", "created_before_failed"),
    ("same_created_at", "created_before_failed"),
    ("failed_job_missing", "failed_job_missing"),
    ("not_completed", "replacement_not_completed"),
])
def test_header_rejections(conn, caplog, defect, reason):
    replacement = _replacement(f"SUPERSEDES: {FAILED}\n")
    if defect == "other_milestone":
        replacement["milestone_id"] = OTHER_ID
    elif defect == "other_goal":
        replacement["goal_id"] = OTHER_ID
    elif defect == "other_tenant":
        replacement["tenant_id"] = OTHER_ID
    elif defect == "self":
        replacement["task_id"] = FAILED
    elif defect == "created_before_failed":
        replacement["created_at"] = T0 - timedelta(minutes=1)
    elif defect == "same_created_at":
        replacement["created_at"] = T0
    elif defect == "failed_job_missing":
        del conn.jobs[FAILED]
    else:
        replacement["status"] = replacement["phase"] = "running"
    conn.replacements = [replacement]

    with caplog.at_level(logging.DEBUG):
        assert _run(conn) == 0

    assert conn.links[FAILED]["superseded_by"] is None
    assert "goal_retry_supersede_rejected" in caplog.text
    assert f"reason={reason}" in caplog.text
    assert "marker=supersedes" in caplog.text


# 5) 멱등 ------------------------------------------------------------------------

def test_second_call_is_idempotent(conn):
    conn.replacements = [_replacement(f"AUTO_REWORK_OF: {FAILED}\n")]
    assert _run(conn) == 1
    later = "runner-55667788"
    conn.replacements.append(_replacement(f"SUPERSEDES: {FAILED}\n", task_id=later,
                                          created_at=T0 + timedelta(hours=1)))
    assert _run(conn) == 0
    assert conn.links[FAILED]["superseded_by"] == REWORK


# 6) RETRY_OF_LINK 는 여전히 카드를 요구한다 ------------------------------------------

def test_retry_of_link_still_requires_candidate(conn, caplog):
    conn.replacements = [_replacement(f"RETRY_OF_LINK: {FAILED}\n")]
    assert _run(conn) == 0
    assert conn.links[FAILED]["superseded_by"] is None
    assert "reason=candidate_missing" in caplog.text
    assert "marker=retry_of_link" in caplog.text


def test_retry_of_link_with_candidate_still_supersedes(conn):
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": FAILED, "card_id": "card-1", "at": (T0 + timedelta(minutes=1)).isoformat(),
    }]}
    conn.replacements = [_replacement(f"RETRY_OF_LINK: {FAILED}\n")]
    assert _run(conn) == 1
    assert conn.evidence["retry_candidates"][0]["superseded_by_task_id"] == REWORK


# 7) 릴리스 증거로 완료 인정 (job phase 가 release_certified 가 아니어도) ------------------

def _evidence_replacement(instruction=None, **overrides):
    row = _replacement(instruction or f"AUTO_REWORK_OF: {FAILED}\n",
                       status="done", phase="review_passed")
    row.update({
        "link_status": "completed", "release_deploy_run_id": 77,
        "deploy_status": "success", "deploy_phase": "completed",
        "image_digest": "sha256:aaa", "standby_digest": "sha256:aaa",
    })
    row.update(overrides)
    return row


def test_release_evidence_supersedes_and_logs_reason(conn, caplog):
    conn.release_column = True
    conn.replacements = [_evidence_replacement()]

    with caplog.at_level(logging.INFO):
        assert _run(conn) == 1

    assert conn.links[FAILED]["superseded_by"] == REWORK
    assert "accept_reason=release_evidence" in caplog.text
    assert "l.release_deploy_run_id" in conn.queries[0]
    assert "LEFT JOIN deploy_runs d ON d.id = l.release_deploy_run_id" in conn.queries[0]


def test_release_evidence_reason_recorded_on_candidate(conn):
    conn.release_column = True
    conn.evidence = {"retry_candidates": [{
        "failed_task_id": FAILED, "card_id": "card-1", "at": (T0 + timedelta(minutes=1)).isoformat(),
    }]}
    conn.replacements = [_evidence_replacement(f"RETRY_OF_LINK: {FAILED}\n")]

    assert _run(conn) == 1
    candidate = conn.evidence["retry_candidates"][0]
    assert candidate["superseded_by_task_id"] == REWORK
    assert candidate["superseded_accept_reason"] == "release_evidence"


def test_job_phase_path_still_reports_job_phase(conn, caplog):
    conn.release_column = True
    conn.replacements = [_evidence_replacement(
        phase=RELEASE_CERTIFIED_PHASE, link_status="pending", release_deploy_run_id=None,
    )]
    with caplog.at_level(logging.INFO):
        assert _run(conn) == 1
    assert "accept_reason=job_phase" in caplog.text


@pytest.mark.parametrize("overrides,reason", [
    ({"release_deploy_run_id": None, "deploy_status": None, "deploy_phase": None,
      "image_digest": None, "standby_digest": None}, "replacement_not_completed"),
    ({"link_status": "pending"}, "replacement_not_completed"),
    ({"link_status": "failed"}, "replacement_not_completed"),
    ({"deploy_status": "failed"}, "release_deploy_not_certified"),
    ({"deploy_status": "rolled_back"}, "release_deploy_not_certified"),
    ({"deploy_phase": "rollback"}, "release_deploy_not_certified"),
    ({"standby_digest": "sha256:bbb"}, "release_deploy_not_certified"),
    ({"image_digest": None}, "release_deploy_not_certified"),
    ({"deploy_status": None, "deploy_phase": None, "image_digest": None,
      "standby_digest": None}, "release_deploy_not_certified"),
])
def test_release_evidence_rejections(conn, caplog, overrides, reason):
    conn.release_column = True
    conn.replacements = [_evidence_replacement(**overrides)]

    with caplog.at_level(logging.DEBUG):
        assert _run(conn) == 0

    assert conn.links[FAILED]["superseded_by"] is None
    assert f"reason={reason}" in caplog.text


@pytest.mark.parametrize("defect,reason", [
    ("other_tenant", "tenant_mismatch"),
    ("self", "self_replacement"),
    ("other_milestone", "link_scope_mismatch"),
    ("created_before_failed", "created_before_failed"),
])
def test_release_evidence_path_keeps_safety_checks(conn, caplog, defect, reason):
    conn.release_column = True
    replacement = _evidence_replacement()
    if defect == "other_tenant":
        replacement["tenant_id"] = OTHER_ID
    elif defect == "self":
        replacement["task_id"] = FAILED
    elif defect == "other_milestone":
        replacement["milestone_id"] = OTHER_ID
    else:
        replacement["created_at"] = T0 - timedelta(minutes=1)
    conn.replacements = [replacement]

    with caplog.at_level(logging.INFO):
        assert _run(conn) == 0

    assert conn.links[FAILED]["superseded_by"] is None
    assert f"reason={reason}" in caplog.text


def test_release_evidence_path_still_requires_candidate_for_retry_of_link(conn, caplog):
    conn.release_column = True
    conn.replacements = [_evidence_replacement(f"RETRY_OF_LINK: {FAILED}\n")]
    with caplog.at_level(logging.INFO):
        assert _run(conn) == 0
    assert "reason=candidate_missing" in caplog.text


def test_schema_without_release_column_works_and_job_phase_path_unaffected(conn):
    conn.release_column = False
    conn.replacements = [_evidence_replacement()]
    assert _run(conn) == 0
    assert "release_deploy_run_id" not in conn.queries[0]
    assert "deploy_runs" not in conn.queries[0]
    assert conn.links[FAILED]["superseded_by"] is None

    conn.replacements = [_evidence_replacement(phase=RELEASE_CERTIFIED_PHASE)]
    assert _run(conn) == 1
    assert conn.links[FAILED]["superseded_by"] == REWORK


# 8) 컬럼 탐지: 캐시하지 않는다 ----------------------------------------------------------

def test_column_probe_true_false_and_error(conn):
    assert asyncio.run(_has_release_evidence_column(conn)) is False
    conn.release_column = True
    assert asyncio.run(_has_release_evidence_column(conn)) is True
    conn.probe_error = RuntimeError("catalog unavailable")
    assert asyncio.run(_has_release_evidence_column(conn)) is False
    assert conn.probes == 3


def test_migration_applied_after_first_call_enables_evidence_path(conn):
    conn.replacements = [_evidence_replacement()]
    assert _run(conn) == 0
    conn.release_column = True
    assert _run(conn) == 1
    assert conn.links[FAILED]["superseded_by"] == REWORK


# 9) 완료 인정 판정 단위 ------------------------------------------------------------------

def test_completion_accept_reason_values():
    assert _completion_accept_reason(_replacement("x")) == "job_phase"
    assert _completion_accept_reason(_evidence_replacement()) == "release_evidence"
    assert _completion_accept_reason(_evidence_replacement(link_status="completed ")) == "release_evidence"
    assert _completion_accept_reason(_evidence_replacement(deploy_status="failed")) is None
    assert _completion_accept_reason(_evidence_replacement(release_deploy_run_id=None)) is None
    assert _completion_accept_reason(_evidence_replacement(link_status=None)) is None
    assert _completion_accept_reason(_replacement("x", status="running", phase="running")) is None
