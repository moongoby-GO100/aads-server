import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.services import e2e_verify
from app.services.e2e_verify import (
    assert_screen_evidence_gate,
    check_overdue_deferred_evidence,
)

SCREEN_INSTRUCTION = "UI 변경: 새 화면 페이지 추가"
SCREEN_FILES = ["public/ext-auth.html"]
JOB = "runner-4d94bec8"

# task_logs_log_type_check (live DB constraint) and column widths: log_type varchar(20), phase varchar(50)
ALLOWED_LOG_TYPES = {"info", "command", "output", "error", "phase_change", "e2e_evidence"}

PASSING_EVIDENCE = {
    "schema": "aads.e2e_verify.v1",
    "passed": True,
    "stages": {"dom_assertion": {"passed": True}, "screenshot": {"success": True}},
}


class FakeConn:
    """In-memory task_logs/pipeline_jobs.logs stand-in keyed on the SQL text."""

    def __init__(self, evidence=None):
        self.task_logs: list[dict] = []
        self.job_logs: dict[str, list[dict]] = {}
        self.job_sessions: dict[str, tuple[str, str]] = {}
        if evidence is not None:
            self.task_logs.append({"task_id": JOB, "log_type": "e2e_evidence", "metadata": {"evidence": evidence}})

    def _latest(self, job_id, log_type, phase=None):
        rows = [
            r for r in self.task_logs
            if r["task_id"] == job_id and r["log_type"] == log_type and r.get("phase") == phase
        ]
        return rows[-1] if rows else None

    async def fetchrow(self, query, *args):
        if "UPDATE pipeline_jobs" in query:
            job_id = args[0]
            logs = self.job_logs.setdefault(job_id, [])
            if any(item.get("event") == "screen_evidence_overdue" for item in logs):
                return None
            logs.append({"event": "screen_evidence_overdue", "deadline_at": args[1]})
            session, project = self.job_sessions.get(job_id, ("sess-1", "AADS"))
            return {"chat_session_id": session, "project": project}
        if "log_type='e2e_evidence'" in query:
            row = self._latest(args[0], "e2e_evidence")
            return {"metadata": row["metadata"]} if row else None
        if "log_type=$2 AND phase=$3" in query:
            row = self._latest(args[0], args[1], args[2])
            return {"metadata": row["metadata"]} if row else None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        deferred_type, deferred_phase, overdue_type, overdue_phase = args
        done = {
            r["task_id"] for r in self.task_logs
            if r["log_type"] == overdue_type and r.get("phase") == overdue_phase
        }
        return [
            {"job_id": r["task_id"], "metadata": r["metadata"]}
            for r in self.task_logs
            if r["log_type"] == deferred_type and r.get("phase") == deferred_phase and r["task_id"] not in done
        ]

    async def execute(self, query, *args):
        task_id, log_type, _content, phase, metadata = args
        assert log_type in ALLOWED_LOG_TYPES and len(log_type) <= 20
        assert len(phase) <= 50
        self.task_logs.append({"task_id": task_id, "log_type": log_type, "phase": phase, "metadata": json.loads(metadata)})
        return "INSERT 0 1"


def _gate(conn, **kwargs):
    return assert_screen_evidence_gate(
        conn, job_id=JOB, instruction=SCREEN_INSTRUCTION, changed_files=SCREEN_FILES, **kwargs
    )


@pytest.mark.asyncio
async def test_a_no_defer_flag_stays_fail_closed():
    conn = FakeConn()
    with pytest.raises(ValueError, match="screen_e2e_evidence_required"):
        await _gate(conn)
    assert conn.task_logs == []


@pytest.mark.asyncio
async def test_b_defer_with_reason_passes_and_records():
    conn = FakeConn()
    await _gate(conn, defer_screen_evidence=True, defer_reason="신규 공개 페이지라 배포 후에만 검증 가능", approver="ceo-1")
    [row] = conn.task_logs
    assert row["log_type"] == e2e_verify.DEFERRED_LOG_TYPE == "info"
    assert row["phase"] == e2e_verify.DEFERRED_PHASE == "e2e_screen_evidence_deferred"
    meta = row["metadata"]
    assert meta["approver"] == "ceo-1"
    assert "배포 후에만" in meta["reason"]
    deferred_at = datetime.fromisoformat(meta["deferred_at"])
    assert datetime.fromisoformat(meta["deadline_at"]) - deferred_at == timedelta(minutes=60)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "짧음", "         x         "])
async def test_c_short_reason_is_rejected_and_nothing_recorded(reason):
    conn = FakeConn()
    with pytest.raises(ValueError, match="screen_evidence_defer_reason_required"):
        await _gate(conn, defer_screen_evidence=True, defer_reason=reason)
    assert conn.task_logs == []


@pytest.mark.asyncio
async def test_d_done_transition_passes_with_deferred_record(monkeypatch):
    from app.services.pipeline_runner_service import PipelineCJob

    conn = FakeConn()
    await _gate(conn, defer_screen_evidence=True, defer_reason="신규 공개 페이지라 배포 후에만 검증 가능")

    async def fetchval(query, job_id):
        return json.dumps(SCREEN_FILES)

    conn.fetchval = fetchval

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: types.SimpleNamespace(acquire=lambda: _Acquire()))
    job = types.SimpleNamespace(job_id=JOB, instruction=SCREEN_INSTRUCTION)
    await PipelineCJob._require_screen_evidence(job)

    blocked = types.SimpleNamespace(job_id="runner-other000", instruction=SCREEN_INSTRUCTION)
    with pytest.raises(ValueError, match="screen_e2e_evidence_required"):
        await PipelineCJob._require_screen_evidence(blocked)


@pytest.mark.asyncio
async def test_e_overdue_without_evidence_alerts_exactly_once():
    conn = FakeConn()
    start = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    await e2e_verify.record_screen_evidence_deferral(conn, job_id=JOB, approver="ceo", reason="배포 후에만 검증 가능", now=start)
    sent = []

    async def notify(**kwargs):
        sent.append(kwargs)

    assert await check_overdue_deferred_evidence(conn, now=start + timedelta(minutes=59), notify=notify) == []
    assert await check_overdue_deferred_evidence(conn, now=start + timedelta(minutes=61), notify=notify) == [JOB]
    assert await check_overdue_deferred_evidence(conn, now=start + timedelta(minutes=70), notify=notify) == []
    assert len(sent) == 1 and sent[0]["job_id"] == JOB and sent[0]["session_id"] == "sess-1"
    assert any(item["event"] == "screen_evidence_overdue" for item in conn.job_logs[JOB])


@pytest.mark.asyncio
async def test_e2_alert_not_repeated_even_if_job_logs_were_overwritten():
    conn = FakeConn()
    start = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    await e2e_verify.record_screen_evidence_deferral(conn, job_id=JOB, approver="ceo", reason="배포 후에만 검증 가능", now=start)
    sent = []

    async def notify(**kwargs):
        sent.append(kwargs)

    await check_overdue_deferred_evidence(conn, now=start + timedelta(hours=2), notify=notify)
    conn.job_logs[JOB] = []
    await check_overdue_deferred_evidence(conn, now=start + timedelta(hours=3), notify=notify)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_f_passing_evidence_suppresses_alert():
    conn = FakeConn(evidence=PASSING_EVIDENCE)
    start = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    await e2e_verify.record_screen_evidence_deferral(conn, job_id=JOB, approver="ceo", reason="배포 후에만 검증 가능", now=start)
    sent = []

    async def notify(**kwargs):
        sent.append(kwargs)

    assert await check_overdue_deferred_evidence(conn, now=start + timedelta(hours=3), notify=notify) == []
    assert sent == []
    assert JOB not in conn.job_logs


@pytest.mark.asyncio
async def test_defer_not_recorded_when_evidence_already_passes_or_not_screen_work():
    conn = FakeConn(evidence=PASSING_EVIDENCE)
    await _gate(conn, defer_screen_evidence=True, defer_reason="배포 후에만 검증 가능")
    assert [r["log_type"] for r in conn.task_logs] == ["e2e_evidence"]

    conn = FakeConn()
    await assert_screen_evidence_gate(
        conn, job_id=JOB, instruction="backend fix", changed_files=["app/x.py"],
        defer_screen_evidence=True, defer_reason="배포 후에만 검증 가능",
    )
    assert conn.task_logs == []


def test_approve_request_defaults_and_mcp_schema():
    from app.api.ceo_chat_tools import TOOL_DEFINITIONS
    from app.api.pipeline_runner import JobApproveRequest

    req = JobApproveRequest(action="approve")
    assert req.defer_screen_evidence is False and req.defer_reason == ""

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "pipeline_runner_approve")
    props = tool["input_schema"]["properties"]
    assert props["defer_screen_evidence"]["type"] == "boolean"
    assert props["defer_reason"]["type"] == "string"
    assert "CEO 명시 승인" in tool["description"]
    assert tool["input_schema"]["required"] == ["job_id", "action"]


def test_log_types_fit_db_check_constraint_and_column_widths():
    assert e2e_verify.DEFERRED_LOG_TYPE in ALLOWED_LOG_TYPES
    assert e2e_verify.OVERDUE_LOG_TYPE in ALLOWED_LOG_TYPES
    assert e2e_verify.DEFERRED_LOG_TYPE != "e2e_evidence" and e2e_verify.OVERDUE_LOG_TYPE != "e2e_evidence"
    for log_type in (e2e_verify.DEFERRED_LOG_TYPE, e2e_verify.OVERDUE_LOG_TYPE):
        assert len(log_type) <= 20
    assert e2e_verify.DEFERRED_PHASE != e2e_verify.OVERDUE_PHASE
    for phase in (e2e_verify.DEFERRED_PHASE, e2e_verify.OVERDUE_PHASE):
        assert len(phase) <= 50


@pytest.mark.asyncio
async def test_overdue_row_uses_allowed_log_type_and_overdue_phase():
    conn = FakeConn()
    start = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    await e2e_verify.record_screen_evidence_deferral(conn, job_id=JOB, approver="ceo", reason="배포 후에만 검증 가능", now=start)
    await check_overdue_deferred_evidence(conn, now=start + timedelta(hours=2))
    assert [(r["log_type"], r["phase"]) for r in conn.task_logs] == [
        ("info", "e2e_screen_evidence_deferred"),
        ("info", "e2e_screen_evidence_overdue"),
    ]
