"""defer_screen_evidence is kept for API compatibility only: the approval gate checks the E2E plan, never evidence."""
import json
import logging
import types

import pytest

from app.services import e2e_verify
from app.services.e2e_verify import assert_screen_evidence_gate, assert_screen_verification_plan, apply_screen_approval_gate

DEFER_KWARGS = {"defer_screen_evidence": True, "defer_reason": "신규 공개 페이지라 배포 후에만 검증 가능"}
PLAN_INSTRUCTION = "UI 변경: 새 화면 페이지 추가\nE2E_VERIFY: url=https://aads.newtalk.kr/ops/new-page selectors=#app | .title"
NO_PLAN_INSTRUCTION = "UI 변경: 새 화면 페이지 추가"
JOB = "runner-4d94bec8"


class FakeConn:
    def __init__(self, columns_ready=True):
        self.columns_ready = columns_ready
        self.updates: list[tuple] = []
        self.task_logs: list[tuple] = []

    async def fetchval(self, query, *args):
        if "information_schema.columns" in query:
            return 2 if self.columns_ready else 0
        if "actual_changed_files" in query:
            return json.dumps(["public/ext-auth.html"])
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "UPDATE pipeline_jobs" in query:
            self.updates.append(args)
        else:
            self.task_logs.append(args)
        return "OK"


@pytest.fixture(autouse=True)
def _reset_column_cache(monkeypatch):
    monkeypatch.setattr(e2e_verify, "_screen_columns_cached", False)


@pytest.mark.asyncio
async def test_defer_flag_does_not_bypass_missing_plan_and_records_nothing():
    conn = FakeConn()
    with pytest.raises(ValueError, match="screen_e2e_plan_required"):
        await apply_screen_approval_gate(
            conn, job_id=JOB, instruction=NO_PLAN_INSTRUCTION, changed_files=["public/ext-auth.html"], **DEFER_KWARGS
        )
    assert conn.task_logs == [] and conn.updates == []


@pytest.mark.asyncio
async def test_defer_flag_is_noop_with_plan_and_logged(caplog):
    plain, deferred = FakeConn(), FakeConn()
    files = ["public/ext-auth.html"]
    await apply_screen_approval_gate(plain, job_id=JOB, instruction=PLAN_INSTRUCTION, changed_files=files)
    with caplog.at_level(logging.INFO, logger=e2e_verify.logger.name):
        await apply_screen_approval_gate(
            deferred, job_id=JOB, instruction=PLAN_INSTRUCTION, changed_files=files, approver="ceo-1", **DEFER_KWARGS
        )
    assert plain.updates == deferred.updates and len(plain.updates) == 1
    assert deferred.task_logs == []
    assert any("screen_evidence_defer_ignored" in r.getMessage() and "ceo-1" in r.getMessage() for r in caplog.records)
    job_id, spec, state = deferred.updates[0]
    assert job_id == JOB and state is None
    assert json.loads(spec) == {
        "url": "https://aads.newtalk.kr/ops/new-page", "selectors": ["#app", ".title"], "source": "instruction",
    }


@pytest.mark.asyncio
async def test_not_screen_work_passes_without_plan_or_writes():
    conn = FakeConn()
    result = await apply_screen_approval_gate(
        conn, job_id=JOB, instruction="backend fix", changed_files=["app/x.py"], **DEFER_KWARGS
    )
    assert result == {"required": False, "state": None, "plan": None}
    assert conn.updates == [] and conn.task_logs == []


@pytest.mark.asyncio
async def test_push_only_job_skips_plan_gate_and_waits_for_release():
    conn = FakeConn()
    instruction = "PUSH_ONLY:true\nDEPLOY_POLICY:push_only\n" + NO_PLAN_INSTRUCTION
    result = await apply_screen_approval_gate(
        conn, job_id=JOB, instruction=instruction, changed_files=["aads-dashboard/src/components/A.tsx"]
    )
    assert result["state"] == "pending_release" and result["plan"] is None
    assert conn.updates == [(JOB, None, "pending_release")]


@pytest.mark.asyncio
async def test_missing_columns_still_validate_plan_but_do_not_write():
    conn = FakeConn(columns_ready=False)
    with pytest.raises(ValueError, match="screen_e2e_plan_required"):
        await apply_screen_approval_gate(conn, job_id=JOB, instruction=NO_PLAN_INSTRUCTION, changed_files=["a/pages/x.tsx"])
    await apply_screen_approval_gate(conn, job_id=JOB, instruction=PLAN_INSTRUCTION, changed_files=["a/pages/x.tsx"])
    assert conn.updates == []


@pytest.mark.asyncio
async def test_compat_entry_is_read_only_plan_gate():
    conn = FakeConn()
    await assert_screen_evidence_gate(conn, job_id=JOB, instruction=PLAN_INSTRUCTION, changed_files=["a/pages/x.tsx"], **DEFER_KWARGS)
    assert conn.updates == []
    with pytest.raises(ValueError, match="screen_e2e_plan_required"):
        await assert_screen_evidence_gate(conn, job_id=JOB, instruction=NO_PLAN_INSTRUCTION, changed_files=["a/pages/x.tsx"])


def test_plan_derived_from_dashboard_route():
    plan = assert_screen_verification_plan("", ["aads-dashboard/src/app/ops/ai-errors/page.tsx"], "AADS")
    assert plan["url"].endswith("/ops/ai-errors") and plan["selectors"] == ["body"]
    assert assert_screen_verification_plan("", ["aads-dashboard/src/app/page.tsx"], "AADS")["url"].rstrip("/").endswith("aads.newtalk.kr")
    grouped = assert_screen_verification_plan("", ["aads-dashboard/src/app/(admin)/admin/deploy/page.tsx"], "AADS")
    assert grouped["url"].endswith("/admin/deploy")


@pytest.mark.parametrize(
    "path",
    ["aads-dashboard/src/app/ops/[id]/page.tsx", "aads-dashboard/src/components/Chart.tsx", "frontend/src/app/x/page.tsx"],
)
def test_plan_not_derivable_raises(path):
    with pytest.raises(ValueError, match="screen_e2e_plan_required"):
        assert_screen_verification_plan("", [path], "GO100")


def test_plan_gate_returns_none_for_non_screen_work():
    assert assert_screen_verification_plan("UI 변경", ["app/services/x.py"]) is None


@pytest.mark.asyncio
async def test_pipelinec_gate_requires_plan_not_evidence(monkeypatch):
    from app.services.pipeline_runner_service import PipelineCJob

    conn = FakeConn()

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: types.SimpleNamespace(acquire=lambda: _Acquire()))
    ok = types.SimpleNamespace(job_id=JOB, instruction=PLAN_INSTRUCTION, project="GO100")
    await PipelineCJob._require_screen_evidence(ok)
    assert len(conn.updates) == 1

    blocked = types.SimpleNamespace(job_id="runner-other000", instruction=NO_PLAN_INSTRUCTION, project="GO100")
    with pytest.raises(ValueError, match="screen_e2e_plan_required"):
        await PipelineCJob._require_screen_evidence(blocked)


def test_approve_request_defaults_and_mcp_schema():
    from app.api.ceo_chat_tools import TOOL_DEFINITIONS
    from app.api.pipeline_runner import JobApproveRequest

    req = JobApproveRequest(action="approve")
    assert req.defer_screen_evidence is False and req.defer_reason == ""
    assert JobApproveRequest(action="approve", **DEFER_KWARGS).defer_screen_evidence is True

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "pipeline_runner_approve")
    props = tool["input_schema"]["properties"]
    assert props["defer_screen_evidence"]["type"] == "boolean"
    assert props["defer_reason"]["type"] == "string"
    assert "호환용" in props["defer_screen_evidence"]["description"]
    assert tool["input_schema"]["required"] == ["job_id", "action"]


class ExplodingConn:
    def __getattr__(self, name):
        raise AssertionError(f"DB must not be touched: {name}")


@pytest.mark.asyncio
async def test_overdue_alert_shim_is_noop_and_never_notifies(caplog):
    notified = []

    async def notify(**kwargs):
        notified.append(kwargs)

    with caplog.at_level(logging.WARNING):
        result = await e2e_verify.check_overdue_deferred_evidence(ExplodingConn(), notify=notify)
    assert result == []
    assert notified == []
    assert "deprecated" in caplog.text


@pytest.mark.asyncio
async def test_record_deferral_shim_is_noop_without_db_write(caplog):
    with caplog.at_level(logging.WARNING):
        result = await e2e_verify.record_screen_evidence_deferral(
            ExplodingConn(), job_id=JOB, approver="ceo-1", reason=DEFER_KWARGS["defer_reason"]
        )
    assert result is None
    assert "deprecated" in caplog.text


def test_validate_defer_reason_shim_keeps_min_length_rule():
    assert e2e_verify.validate_defer_reason("  " + DEFER_KWARGS["defer_reason"] + "  ") == DEFER_KWARGS["defer_reason"]
    with pytest.raises(ValueError, match="screen_evidence_defer_reason_required"):
        e2e_verify.validate_defer_reason("짧음")
    with pytest.raises(ValueError, match="screen_evidence_defer_reason_required"):
        e2e_verify.validate_defer_reason(None)
