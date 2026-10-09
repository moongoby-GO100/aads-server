"""Deployed screen jobs are verified by the watchdog (verifying -> passed/failed/unverifiable); status is never touched."""
import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import e2e_verify
from app.services.e2e_verify import (
    classify_screen_evidence,
    instruction_is_push_only,
    parse_e2e_verify_directive,
    run_post_deploy_cycle,
    verify_screen_job,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
SCREEN_FILES = ["aads-dashboard/src/app/ops/ai-errors/page.tsx"]


def passing_evidence():
    return {
        "schema": "aads.e2e_verify.v1", "passed": True,
        "stages": {
            "preflight": {"success": True, "status_code": 200},
            "dom_assertion": {"passed": True, "assertions": [{"selector": "body", "found": True}]},
            "screenshot": {"success": True, "url": "https://x/shot.png"},
        },
    }


def failing_evidence():
    return {
        "schema": "aads.e2e_verify.v1", "passed": False,
        "stages": {
            "preflight": {"success": True, "status_code": 200},
            "dom_assertion": {
                "passed": False, "final_url": "https://aads.newtalk.kr/ops/ai-errors",
                "assertions": [{"selector": "#table", "found": False}],
            },
        },
    }


def job(job_id, *, state=None, deployed=True, files=SCREEN_FILES, instruction="화면 추가", project="AADS", phase="done", spec=None, age_min=10):
    completed = NOW - timedelta(minutes=age_min)
    return {
        "job_id": job_id, "project": project, "tenant_id": "t-1", "instruction": instruction,
        "actual_changed_files": json.dumps(files), "e2e_spec": json.dumps(spec) if spec else None,
        "chat_session_id": "sess-1", "status": "done", "phase": phase, "screen_evidence_state": state,
        "completed_at": completed, "deployed_at": completed + timedelta(minutes=2) if deployed else None,
        "updated_at": completed, "logs": [],
    }


class FakeConn:
    """pipeline_jobs stand-in; dispatches on the SQL fragments the module emits."""

    def __init__(self, *jobs, columns=2):
        self.jobs = {j["job_id"]: j for j in jobs}
        self.columns = columns
        self.sql: list[str] = []

    def _row(self, j):
        return {
            "job_id": j["job_id"], "project": j["project"], "tenant_id": j["tenant_id"],
            "instruction": j["instruction"], "actual_changed_files": j["actual_changed_files"],
            "e2e_spec": j["e2e_spec"], "chat_session_id": j["chat_session_id"],
        }

    async def fetchval(self, query, *args):
        assert "information_schema.columns" in query
        return self.columns

    async def fetch(self, query, *args):
        self.sql.append(query)
        if "phase='push_only_by_directive'" in query:
            return [self._row(j) for j in self.jobs.values()
                    if j["phase"] == "push_only_by_directive" and j["screen_evidence_state"] is None]
        if "UPDATE pipeline_jobs p" in query:
            limit = args[0]
            picked = []
            for j in sorted(self.jobs.values(), key=lambda x: x["completed_at"]):
                if j["screen_evidence_state"] != "pending_release" or j["status"] != "done":
                    continue
                if any(d["project"] == j["project"] and d["status"] == "done" and d["deployed_at"]
                       and d["deployed_at"] > j["completed_at"] for d in self.jobs.values()):
                    picked.append(j)
            for j in picked[:limit]:
                j["screen_evidence_state"] = "verifying"
            return [self._row(j) for j in picked[:limit]]
        if "make_interval(hours" in query:
            hours, exclude = args
            return [self._row(j) for j in self.jobs.values()
                    if j["status"] == "done" and j["deployed_at"]
                    and j["screen_evidence_state"] in (None, "pending_release") and j["job_id"] not in exclude]
        if "make_interval(mins" in query:
            return []
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        assert "SET screen_evidence_state = 'verifying'" in query
        j = self.jobs[args[0]]
        if j["screen_evidence_state"] not in (None, "pending_release"):
            return None
        j["screen_evidence_state"] = "verifying"
        return self._row(j)

    async def execute(self, query, *args):
        self.sql.append(query)
        j = self.jobs[args[0]]
        if "'screen_verify'" in query:
            j["screen_evidence_state"] = args[1]
            j["logs"].append({"event": "screen_verify", "state": args[1], "reason": args[2]})
        elif "COALESCE(e2e_spec" in query:
            j["screen_evidence_state"] = args[1]
        elif "'not_required'" in query:
            j["screen_evidence_state"] = "not_required"
        else:
            raise AssertionError(query)
        return "UPDATE 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return None

        return _Ctx()


@pytest.fixture(autouse=True)
def _reset_column_cache(monkeypatch):
    monkeypatch.setattr(e2e_verify, "_screen_columns_cached", False)


class Harness:
    def __init__(self, conn, verify):
        self.conn, self.verify_fn = conn, verify
        self.alerts: list[dict] = []
        self.sleeps: list[float] = []
        self.inflight: set[str] = set()
        self.pool = FakePool(conn)

    async def notify(self, **kwargs):
        self.alerts.append(kwargs)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    async def _verify_job(self, j):
        await verify_screen_job(j, pool=self.pool, notify=self.notify, verify=self.verify_fn, retry_delay=7, sleep=self.sleep)

    async def cycle(self):
        tasks = await run_post_deploy_cycle(self.conn, inflight=self.inflight, verify_job=self._verify_job)
        await asyncio.gather(*tasks)
        return tasks


def recorder(results):
    calls = []

    async def verify(**kwargs):
        calls.append(kwargs)
        result = results[min(len(calls), len(results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    verify.calls = calls
    return verify


@pytest.mark.asyncio
async def test_deployed_screen_job_goes_verifying_then_passed_and_status_stays_done():
    conn = FakeConn(job("runner-a"))
    verify = recorder([passing_evidence()])
    h = Harness(conn, verify)
    tasks = await run_post_deploy_cycle(conn, inflight=h.inflight, verify_job=h._verify_job)
    assert conn.jobs["runner-a"]["screen_evidence_state"] == "verifying"
    assert h.inflight == {"runner-a"}
    await asyncio.gather(*tasks)
    j = conn.jobs["runner-a"]
    assert j["screen_evidence_state"] == "passed" and j["status"] == "done"
    assert h.inflight == set() and h.alerts == []
    assert verify.calls[0]["url"].endswith("/ops/ai-errors") and verify.calls[0]["tenant_id"] == "t-1"
    assert verify.calls[0]["selectors"] == ["body"]


@pytest.mark.asyncio
async def test_failed_alerts_once_keeps_done_and_never_rolls_back():
    conn = FakeConn(job("runner-a"))
    h = Harness(conn, recorder([failing_evidence()]))
    await h.cycle()
    j = conn.jobs["runner-a"]
    assert j["screen_evidence_state"] == "failed" and j["status"] == "done"
    assert [a["state"] for a in h.alerts] == ["failed"] and h.alerts[0]["summary"]["missing_selectors"] == ["#table"]
    assert h.sleeps == []
    await h.cycle()
    assert len(h.alerts) == 1


@pytest.mark.asyncio
async def test_unverifiable_retries_once_then_alerts_with_fallback_attached():
    fallback_evidence = {
        "schema": "aads.e2e_verify.v1", "passed": False,
        "stages": {
            "preflight": {"success": True, "status_code": 200},
            "fallback": {"browser_error": "playwright down", "api_health": {"status_code": 200}, "process": {"success": True}},
        },
    }
    conn = FakeConn(job("runner-a"))
    verify = recorder([RuntimeError("boom"), fallback_evidence])
    h = Harness(conn, verify)
    await h.cycle()
    j = conn.jobs["runner-a"]
    assert len(verify.calls) == 2 and h.sleeps == [7]
    assert j["screen_evidence_state"] == "unverifiable" and j["status"] == "done"
    [alert] = h.alerts
    assert alert["state"] == "unverifiable"
    assert alert["summary"]["fallback"] == {"browser_error": "playwright down", "api_health": 200, "process_ok": True}


@pytest.mark.asyncio
async def test_unverifiable_then_pass_on_retry_is_passed_without_alert():
    conn = FakeConn(job("runner-a"))
    h = Harness(conn, recorder([RuntimeError("flaky"), passing_evidence()]))
    await h.cycle()
    assert conn.jobs["runner-a"]["screen_evidence_state"] == "passed" and h.alerts == []


@pytest.mark.asyncio
async def test_rejected_url_is_unverifiable_without_retry():
    conn = FakeConn(job("runner-a"))
    verify = recorder([ValueError("url_not_allowed:blocked")])
    h = Harness(conn, verify)
    await h.cycle()
    assert len(verify.calls) == 1 and conn.jobs["runner-a"]["screen_evidence_state"] == "unverifiable"


@pytest.mark.asyncio
async def test_missing_plan_is_unverifiable_without_running_e2e():
    conn = FakeConn(job("runner-a", files=["frontend/components/A.tsx"], project="GO100"))
    verify = recorder([passing_evidence()])
    h = Harness(conn, verify)
    await h.cycle()
    assert verify.calls == []
    assert conn.jobs["runner-a"]["screen_evidence_state"] == "unverifiable"
    assert h.alerts[0]["reason"] == "screen_e2e_plan_missing"


@pytest.mark.asyncio
async def test_approval_time_spec_is_used_over_derivation():
    spec = {"url": "https://aads.newtalk.kr/custom", "selectors": ["#a", ".b"], "source": "instruction"}
    conn = FakeConn(job("runner-a", spec=spec))
    verify = recorder([passing_evidence()])
    await Harness(conn, verify).cycle()
    assert verify.calls[0]["url"] == spec["url"] and verify.calls[0]["selectors"] == ["#a", ".b"]


@pytest.mark.asyncio
async def test_non_screen_deployed_job_is_not_required_and_not_verified():
    conn = FakeConn(job("runner-a", files=["scripts/aads-runner.service", "app/x.py"]))
    verify = recorder([passing_evidence()])
    h = Harness(conn, verify)
    assert await h.cycle() == []
    assert conn.jobs["runner-a"]["screen_evidence_state"] == "not_required" and verify.calls == []


@pytest.mark.asyncio
async def test_push_only_job_waits_then_verifies_after_same_project_release():
    push_only = job("runner-po", state="pending_release", deployed=False, age_min=60)
    conn = FakeConn(push_only)
    verify = recorder([passing_evidence()])
    h = Harness(conn, verify)
    assert await h.cycle() == []
    assert conn.jobs["runner-po"]["screen_evidence_state"] == "pending_release" and verify.calls == []

    other_project = job("runner-other", state="not_required", project="KIS", age_min=5)
    conn.jobs["runner-other"] = other_project
    assert await h.cycle() == []
    assert conn.jobs["runner-po"]["screen_evidence_state"] == "pending_release"

    before_job = job("runner-old-release", state="not_required", age_min=120)
    before_job["deployed_at"] = push_only["completed_at"] - timedelta(minutes=1)
    conn.jobs["runner-old-release"] = before_job
    assert await h.cycle() == []

    release = job("runner-release", state="not_required", files=["app/x.py"], age_min=5)
    conn.jobs["runner-release"] = release
    tasks = await run_post_deploy_cycle(conn, inflight=h.inflight, verify_job=h._verify_job)
    assert len(tasks) == 1 and conn.jobs["runner-po"]["screen_evidence_state"] == "verifying"
    await asyncio.gather(*tasks)
    assert conn.jobs["runner-po"]["screen_evidence_state"] == "passed" and conn.jobs["runner-po"]["status"] == "done"


@pytest.mark.asyncio
async def test_runner_labeled_push_only_done_job_becomes_pending_release():
    conn = FakeConn(
        job("runner-po", deployed=False, phase="push_only_by_directive"),
        job("runner-po2", deployed=False, phase="push_only_by_directive", files=["app/x.py"]),
    )
    h = Harness(conn, recorder([passing_evidence()]))
    await h.cycle()
    assert conn.jobs["runner-po"]["screen_evidence_state"] == "pending_release"
    assert conn.jobs["runner-po2"]["screen_evidence_state"] == "not_required"


@pytest.mark.asyncio
async def test_at_most_two_concurrent_verifications_and_no_slot_status():
    conn = FakeConn(job("runner-a", age_min=30), job("runner-b", age_min=20), job("runner-c", age_min=10))
    gate = asyncio.Event()
    started: list[str] = []

    async def verify(**kwargs):
        started.append(kwargs["url"])
        await gate.wait()
        return passing_evidence()

    h = Harness(conn, verify)
    tasks = await run_post_deploy_cycle(conn, inflight=h.inflight, verify_job=h._verify_job)
    assert len(tasks) == 2 and len(h.inflight) == 2
    states = {k: v["screen_evidence_state"] for k, v in conn.jobs.items()}
    assert sorted(states.values(), key=str) == [None, "verifying", "verifying"]
    assert await run_post_deploy_cycle(conn, inflight=h.inflight, verify_job=h._verify_job) == []
    assert {v["status"] for v in conn.jobs.values()} == {"done"}
    gate.set()
    await asyncio.gather(*tasks)
    third = await run_post_deploy_cycle(conn, inflight=h.inflight, verify_job=h._verify_job)
    await asyncio.gather(*third)
    assert {v["screen_evidence_state"] for v in conn.jobs.values()} == {"passed"}
    assert {v["status"] for v in conn.jobs.values()} == {"done"}


@pytest.mark.asyncio
async def test_cycle_is_noop_until_migration_applied():
    conn = FakeConn(job("runner-a"), columns=0)
    h = Harness(conn, recorder([passing_evidence()]))
    assert await h.cycle() == []
    assert conn.sql == []


def test_verdict_writes_never_touch_pipeline_job_status():
    source = inspect.getsource(e2e_verify._persist_screen_state)
    set_clause = source.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "status" not in set_clause.replace("screen_evidence_state", "")


def test_watchdog_runs_post_deploy_verify_instead_of_deferred_alert():
    from app.services import pipeline_runner_service as svc

    loop_source = inspect.getsource(svc._watchdog_loop)
    assert "_run_post_deploy_screen_verify" in loop_source
    assert "_check_deferred_screen_evidence" not in loop_source
    assert not hasattr(svc, "_check_deferred_screen_evidence")


@pytest.mark.asyncio
@pytest.mark.parametrize("state, expected", [("failed", "자동 롤백은 하지 않습니다"), ("unverifiable", "판단 불가")])
async def test_alert_text_and_idempotency(monkeypatch, state, expected):
    from app.services import pipeline_runner_service as svc

    sent = []

    async def fake_report(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("app.services.session_reporter.post_session_report", fake_report)
    await svc._notify_post_deploy_screen_result(
        job_id="runner-a", session_id="sess-1", project="AADS", state=state, reason="r",
        plan={"url": "https://aads.newtalk.kr/x"}, summary={"preflight_status": 200},
    )
    [call] = sent
    assert expected in call["body"]
    assert call["idempotency_key"] == f"screen_verify_{state}:runner-a"
    await svc._notify_post_deploy_screen_result(
        job_id="runner-a", session_id=None, project="AADS", state=state, reason="r", plan=None, summary=None,
    )
    assert len(sent) == 1


def test_classify_screen_evidence():
    plan = {"url": "https://aads.newtalk.kr/ops/x"}
    assert classify_screen_evidence(passing_evidence(), plan)[0] == "passed"
    assert classify_screen_evidence(failing_evidence(), plan) == ("failed", "dom_assertion_failed")
    http500 = {"stages": {"preflight": {"success": False, "status_code": 502}}}
    assert classify_screen_evidence(http500, plan) == ("failed", "preflight_http_502")
    unreachable = {"stages": {"preflight": {"success": False, "status_code": None}}}
    assert classify_screen_evidence(unreachable, plan)[0] == "unverifiable"
    login = failing_evidence()
    login["stages"]["dom_assertion"]["final_url"] = "https://aads.newtalk.kr/login?next=/ops/x"
    assert classify_screen_evidence(login, plan) == ("unverifiable", "login_redirect")
    assert classify_screen_evidence(None, plan)[0] == "unverifiable"
    screenshot_missing = passing_evidence()
    screenshot_missing["stages"]["screenshot"] = {"success": False}
    screenshot_missing["passed"] = False
    assert classify_screen_evidence(screenshot_missing, plan)[0] == "unverifiable"


@pytest.mark.parametrize(
    "instruction, expected",
    [
        ("PUSH_ONLY:true\nDEPLOY_POLICY:push_only", True),
        ("TITLE: x (PUSH_ONLY)", True),
        ("push_only: false", False),
        ("커밋까지만 수행", True),
        ("배포·재시작 금지", True),
        ("deploy: false", True),
        ("일반 배포 지시서", False),
        ("", False),
    ],
)
def test_instruction_is_push_only(instruction, expected):
    assert instruction_is_push_only(instruction) is expected


def test_parse_e2e_verify_directive():
    plan = parse_e2e_verify_directive("본문\nE2E_VERIFY: url=https://aads.newtalk.kr/a?b=1 selectors=#app | .row > td\n")
    assert plan == {"url": "https://aads.newtalk.kr/a?b=1", "selectors": ["#app", ".row > td"], "source": "instruction"}
    reordered = parse_e2e_verify_directive("e2e_verify: selectors=#a, #b url=https://h.example/p")
    assert reordered["url"] == "https://h.example/p" and reordered["selectors"] == ["#a", "#b"]
    assert parse_e2e_verify_directive("E2E_VERIFY: url=https://h.example/p")["selectors"] == ["body"]
    assert parse_e2e_verify_directive("E2E_VERIFY: url=ftp://h/p selectors=#a") is None
    assert parse_e2e_verify_directive("no directive here") is None
