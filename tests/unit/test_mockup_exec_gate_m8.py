"""M8: the mockup approval gate on every submission path, fail-closed runner, offline CI check (no database).

What this file pins down:
- submit_batch runs the same submit-phase gate as submit_job for every job, and one denial rejects the whole batch.
- the approval-bundle contract: tampered / revoked / superseded / other tenant-project / DB down are denied. A declared
  bundle is verified in every mode (M8a rule: shadow does not relax it); only an undeclared UI task is
  allowed-but-recorded under shadow.
- a running task is only marked for checkpoint review when its approval changes; nothing is killed.
- pipeline-runner.sh decides what a crashed gate script means from the server rules (classify), never from prose.
- the chat-tool DB fallback cannot bypass the gate, and the CI check never claims a bundle is approved.
"""
import asyncio
import importlib.util
import inspect
import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.services import mockup_gate_rules as rules
from app.services import mockup_review_service as svc

ROOT = Path(__file__).resolve().parents[2]
REVIEW = "11111111-1111-4111-8111-111111111111"
REVISION = "22222222-2222-4222-8222-222222222222"
NEWER = "77777777-7777-4777-8777-777777777777"
HASH = "a" * 64
TENANT = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "66666666-6666-4666-8666-666666666666"
BUNDLE = f"MOCKUP_REVIEW_ID: {REVIEW}\nMOCKUP_REVISION_ID: {REVISION}\nMOCKUP_MANIFEST_HASH: {HASH}\n"
UI_TASK = "TARGET: /root/aads/aads-dashboard\nTARGET_FILES: src/components/Panel.tsx\n"
TSX_ONLY_TASK = "작업: 패널 색 변경\nTARGET_FILES: src/components/Panel.tsx\n"  # no MOCKUP_/dashboard/UI_TASK words
BACKEND_TASK = "TARGET: /root/aads/aads-server\nTARGET_FILES: app/api/x.py\n"
ENFORCE = {"MOCKUP_GATE_MODE": "enforce"}
SHADOW = {"MOCKUP_GATE_MODE": "shadow"}
CONTEXT = {"user": {"user_id": "u1", "is_internal_admin": True}, "tenant": {"id": TENANT},
           "membership": {"role": "owner"}}


def run(coro):
    return asyncio.run(coro)


class FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """First handler whose substring is in the SQL answers; anything unexpected fails the test."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def _answer(self, sql, args):
        self.calls.append((sql, args))
        for needle, fn in self.handlers:
            if needle in sql:
                return fn(*args)
        raise AssertionError(f"unexpected SQL: {sql[:90]}")

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args)

    async def fetch(self, sql, *args):
        return self._answer(sql, args)

    async def fetchval(self, sql, *args):
        return self._answer(sql, args)

    async def execute(self, sql, *args):
        return self._answer(sql, args)

    def transaction(self):
        return FakeTx()

    def sql(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]


def head_row(**over):
    base = {"id": UUID(REVIEW), "tenant_id": UUID(TENANT), "project_key": "AADS", "title": "목업", "change_type": "modify",
            "goal_id": None, "session_id": UUID(SESSION_ID), "status": "approved", "latest_revision_id": UUID(REVISION),
            "approved_revision_id": UUID(REVISION), "approval_event_id": 7, "generation": 3, "pointer_generation": 0,
            "created_by": "creator"}
    return {**base, **over}


def revision_row(**over):
    return {"id": UUID(REVISION), "manifest_hash": HASH, "manifest": json.dumps({"assets": [], "screens": []}),
            "doc_refs": "[]", **over}


def db(head=None, revision=None, binding=None, lock_error=None):
    """A FakeConn for verify_bundle. head=None means "no such review for this tenant/project"."""
    def lock(*a):
        if lock_error:
            raise lock_error
        return head

    return FakeConn([
        ("FOR UPDATE", lock),
        ("FROM goal_task_links", lambda *a: 1),
        ("FROM mockup_review_revisions WHERE id=$1 AND head_id", lambda *a: revision),
        ("SELECT revision FROM mockup_review_revisions", lambda *a: 1),
        ("FROM mockup_review_task_bindings", lambda *a: binding),
        ("INSERT INTO mockup_review_task_bindings", lambda *a: "INSERT 0 1"),
        ("UPDATE mockup_review_task_bindings", lambda *a: "UPDATE 1"),
        ("nextval", lambda *a: 41),
        ("INSERT INTO mockup_review_events", lambda *a: None),
        ("FROM tenant_memberships", lambda *a: []),
        ("SELECT DISTINCT requested_by", lambda *a: []),
        ("FROM chat_sessions", lambda *a: "creator"),
        ("INSERT INTO ohvis_notifications", lambda *a: "INSERT 0 0"),
        ("INSERT INTO mockup_gate_decisions", lambda *a: "INSERT 0 1"),
    ])


def gate(conn, environ, phase="pre_execution", instruction=UI_TASK + BUNDLE):
    return run(svc.gate_check(conn, phase=phase, tenant=TENANT, project="AADS", task_id="runner-aaaa1111",
                              instruction=instruction, goal_id=None, actor="system:pipeline-runner",
                              environ=environ))


# ------------------------------------------------------- approval bundle contract (AC: tamper/revoke/new/scope/DB)

CONTRACT = {
    "manifest_tampered": (lambda: db(head_row(), revision_row(manifest_hash="b" * 64)), "manifest_hash_mismatch", True),
    "revoked": (lambda: db(head_row(status="revoked", approved_revision_id=None), revision_row()),
                "approval_revoked", True),
    "newer_revision": (lambda: db(head_row(latest_revision_id=UUID(NEWER)), revision_row()),
                       "superseded_by_newer_revision", True),
    "other_tenant_or_project": (lambda: db(None), "review_not_found", False),
    "db_down": (lambda: db(lock_error=ConnectionError("db down")), "gate_unavailable", False),
}


@pytest.mark.parametrize("case", sorted(CONTRACT))
def test_bundle_contract_enforce_denies(case):
    make, reason, _ = CONTRACT[case]
    out = gate(make(), ENFORCE)
    assert out["allowed"] is False and reason in out["reasons"] and not out.get("shadow")


@pytest.mark.parametrize("case", sorted(CONTRACT))
def test_bundle_contract_shadow_still_denies_a_declared_bundle_and_records(case):
    make, reason, audited = CONTRACT[case]
    conn = make()
    out = gate(conn, SHADOW)
    # A declared bundle opted into the gate: shadow does not relax its verification, and pre_execution stops the work.
    assert out["allowed"] is False and not out.get("shadow") and reason in out["reasons"] and out["blocking"] is True
    events = [args for sql, args in conn.calls if "INSERT INTO mockup_review_events" in sql]
    if audited:  # the review exists: the denial is written to mockup_review_events
        assert [a[5] for a in events] == ["verify_denied"]
        assert reason in json.loads(events[0][10])["reasons"]
    else:  # no head row to attach an event to (or no DB): the reasons travel back and the API logs them
        assert events == []
    assert not conn.sql("mockup_review_task_bindings(")  # nothing is bound on a failed verification


def test_shadow_never_writes_a_binding_for_a_failed_bundle():
    conn = db(head_row(), revision_row(manifest_hash="b" * 64))
    gate(conn, SHADOW, phase="submit")
    assert not conn.sql("INSERT INTO mockup_review_task_bindings")


def test_a_malformed_declaration_is_denied_even_in_shadow():
    out = gate(FakeConn([]), SHADOW, instruction=f"MOCKUP_REVIEW_ID: {REVIEW}\n")
    assert out["allowed"] is False and "bundle_revision_id_missing" in out["reasons"]


def test_shadow_relaxes_only_undeclared_ui_work():
    out = gate(db(None), SHADOW, instruction=UI_TASK)
    assert out["allowed"] is True and out["shadow"] is True and out["reasons"] == ["mockup_bundle_required"]
    assert gate(db(None), ENFORCE, instruction=UI_TASK)["allowed"] is False


def test_api_gate_logs_shadow_violations_and_db_failures(monkeypatch):
    from app.api import canonical_documents, pipeline_runner

    logged = []

    class Log:
        def warning(self, event, **kw):
            logged.append(event)

    async def no_auth(*a, **k):
        return None

    class Pool:
        def __init__(self, conn=None):
            self.conn = conn

        def acquire(self):
            if self.conn is None:
                raise ConnectionError("pool exhausted")
            conn = self.conn

            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    monkeypatch.setattr(pipeline_runner, "logger", Log())
    monkeypatch.setattr(canonical_documents, "_authorize", no_auth)
    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")

    def submit(instruction):
        return run(pipeline_runner._run_mockup_gate(CONTEXT, phase="submit", project="AADS", task_id="runner-aaaa1111",
                                                    instruction=instruction, goal_id=None))

    conn = db(None)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: Pool(conn))
    out = submit(UI_TASK)
    assert out["allowed"] is True and out["reasons"] == ["mockup_bundle_required"] and logged == ["mockup_gate_shadow"]
    assert conn.sql("INSERT INTO mockup_gate_decisions")  # the shadow verdict is recorded (M8a table)
    out = submit(UI_TASK + BUNDLE)  # a declared bundle that does not exist is denied even in shadow
    assert out["allowed"] is False and out["reasons"] == ["review_not_found"]

    logged.clear()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: Pool())
    out = submit(UI_TASK)
    assert out["allowed"] is True and out["reasons"] == ["gate_unavailable"]
    assert logged[0] == "mockup_gate_unavailable" and logged[-1] == "mockup_gate_shadow"
    assert submit(UI_TASK + BUNDLE)["allowed"] is False  # declared + DB down: fail closed in every mode

    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    out = submit(UI_TASK)
    assert out["allowed"] is False and out["reasons"] == ["gate_unavailable"]


# ------------------------------------------------------------ running tasks are marked, never killed

def test_revoking_an_approval_marks_running_tasks_for_review_and_holds_unstarted_ones():
    conn = FakeConn([("UPDATE mockup_review_task_bindings", lambda *a: "UPDATE 2")])
    assert run(svc._hold_bindings(conn, UUID(REVIEW))) == 2
    (sql,) = conn.sql("UPDATE")
    assert "WHEN 'running' THEN 'review_required'" in sql and "WHEN 'bound' THEN 'held'" in sql
    assert "pipeline_jobs" not in sql


@pytest.mark.parametrize("binding_status", ["review_required", "held"])
def test_checkpoint_on_a_flagged_running_task_only_reports_hold(binding_status):
    binding = {"id": 5, "status": binding_status}
    conn = db(head_row(), revision_row(), binding=binding)
    out = gate(conn, ENFORCE, phase="checkpoint")
    assert out["allowed"] is False and out["reasons"] == ["binding_held"]
    # The checkpoint writes an audit event and nothing else: the binding keeps its hold, the job row is untouched.
    assert not [sql for sql, _ in conn.calls if sql.lstrip().startswith("UPDATE")]
    assert not any("pipeline_jobs" in sql for sql, _ in conn.calls)


def test_nothing_on_the_gate_path_can_stop_a_job():
    from app.api import pipeline_runner

    endpoint = inspect.getsource(pipeline_runner.mockup_gate_for_job)
    assert "SELECT project, instruction FROM pipeline_jobs" in endpoint and "UPDATE" not in endpoint
    for fn in (svc.gate_check, svc.verify_bundle, svc._hold_bindings, pipeline_runner._run_mockup_gate,
               pipeline_runner._run_batch_mockup_gate):
        source = inspect.getsource(fn).replace("Nothing is killed", "")
        for word in ("pipeline_jobs SET", "kill", "cancel", "supervisorctl", "systemctl"):
            assert word not in source, (fn.__name__, word)


# ------------------------------------------------------------------- submit_batch: all or nothing

def batch_request(*jobs):
    from app.api.pipeline_runner import BatchJobItem, BatchSubmitRequest

    return BatchSubmitRequest(project="AADS", session_id=SESSION_ID,
                              jobs=[BatchJobItem(key=k, instruction=i) for k, i in jobs])


class NoDB:
    def acquire(self):
        raise AssertionError("a rejected batch must not reach the job INSERT transaction")


class ReachedDB(Exception):
    pass


def test_batch_runs_the_submit_gate_for_every_job_and_rejects_the_whole_batch(monkeypatch):
    from app.api import pipeline_runner

    seen = []

    async def fake_gate(context, *, phase, project, task_id, instruction, goal_id):
        seen.append((phase, task_id))
        if "DENY" in instruction:
            return {"applicable": True, "allowed": False, "mode": "enforce", "phase": phase,
                    "reasons": ["approval_revoked"]}
        return {"applicable": False, "allowed": True, "mode": "enforce", "phase": phase, "reasons": []}

    monkeypatch.setattr(pipeline_runner, "_run_mockup_gate", fake_gate)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: NoDB())
    req = batch_request(("A", BACKEND_TASK), ("B", UI_TASK + "DENY\n"), ("C", BACKEND_TASK))
    with pytest.raises(HTTPException) as exc:
        run(pipeline_runner.submit_batch(req, CONTEXT))
    detail = exc.value.detail
    assert exc.value.status_code == 409 and detail["code"] == "mockup_approval_required"
    assert detail["batch_rejected"] is True and detail["jobs_submitted"] == 0
    assert [(r["key"], r["reasons"]) for r in detail["rejected"]] == [("B", ["approval_revoked"])]
    assert [p for p, _ in seen] == ["submit"] * 3 and len({t for _, t in seen}) == 3
    assert detail["rejected"][0]["job_id"] == seen[1][1]


def test_batch_with_the_real_gate_enforce_rejects_shadow_lets_it_through(monkeypatch):
    from app.api import pipeline_runner

    class GatePoolDown:
        """The gate's own connection fails; the batch INSERT connection is the second acquire."""

        def __init__(self):
            self.calls = 0

        def acquire(self):
            self.calls += 1
            raise ConnectionError("db down") if self.calls == 1 else ReachedDB()

    async def no_record(*a, **k):
        return None

    # The gate's second-chance recorder would take another connection; keep the acquire count about the batch.
    monkeypatch.setattr(pipeline_runner, "_record_gate_decision_fresh", no_record)
    req = batch_request(("A", BACKEND_TASK), ("B", TSX_ONLY_TASK))
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: GatePoolDown())
    with pytest.raises(HTTPException) as exc:
        run(pipeline_runner.submit_batch(req, CONTEXT))
    assert exc.value.status_code == 409
    assert exc.value.detail["rejected"] == [
        {"key": "B", "job_id": exc.value.detail["rejected"][0]["job_id"], "reasons": ["gate_unavailable"]}]

    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")
    pool = GatePoolDown()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    with pytest.raises((ReachedDB, HTTPException)) as reached:
        run(pipeline_runner.submit_batch(req, CONTEXT))
    # shadow: an undeclared UI job's gate failure is recorded and the batch proceeds to its INSERT transaction
    assert pool.calls == 2
    if isinstance(reached.value, HTTPException):
        assert reached.value.status_code == 500


def test_submit_batch_calls_the_mockup_gate_before_its_insert_transaction():
    from app.api import pipeline_runner

    source = inspect.getsource(pipeline_runner.submit_batch)
    assert source.index("_run_batch_mockup_gate(") < source.index("pool.acquire()")
    assert source.index("_run_batch_mockup_gate(") < source.index("INSERT INTO pipeline_jobs")


# ------------------------------------------------------------ chat tool DB fallback cannot bypass the gate

def test_db_fallback_refuses_gated_jobs_under_enforce_and_never_enqueues_part_of_a_batch(monkeypatch):
    from app.services.tool_executor import ToolExecutor

    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: NoDB())
    executor = ToolExecutor.__new__(ToolExecutor)
    single = run(executor._pipeline_runner_submit_db_fallback(
        {"project": "AADS", "instruction": TSX_ONLY_TASK, "session_id": SESSION_ID}, "http_401"))
    assert single["code"] == "mockup_approval_required" and single["rejected"][0]["reasons"] == ["gate_unavailable"]
    batch = run(executor._pipeline_runner_submit_batch_db_fallback(
        {"project": "AADS", "session_id": SESSION_ID,
         "jobs": [{"key": "A", "instruction": BACKEND_TASK}, {"key": "B", "instruction": UI_TASK}]}, "http_403"))
    assert batch["jobs_submitted"] == 0 and [r["key"] for r in batch["rejected"]] == ["B"]


def test_db_fallback_in_shadow_only_logs_and_malformed_bundles_are_always_refused(monkeypatch):
    from app.services.tool_executor import ToolExecutor

    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")
    assert ToolExecutor._mockup_fallback_denial({"A": UI_TASK, "B": BACKEND_TASK}, "r") is None
    denial = ToolExecutor._mockup_fallback_denial({"A": f"MOCKUP_REVIEW_ID: {REVIEW}\n"}, "r")
    assert denial["rejected"][0]["reasons"] == ["bundle_revision_id_missing", "bundle_manifest_hash_missing"]


# ------------------------------------------------------------- runner: crashed gate script is fail-closed

def _helper_script():
    lines = (ROOT / "scripts" / "pipeline-runner.sh").read_text().splitlines(keepends=True)
    start = lines.index("_mockup_gate_on_script_error() {\n")
    end = lines.index("}\n", start)
    return "".join(lines[start:end + 1])


def run_helper(tmp_path, *, mode, classify_rc=None, gate_script=None, instruction=UI_TASK):
    if gate_script is None:
        gate_script = tmp_path / "fake_gate.py"
        gate_script.write_text(f"import sys\nsys.exit({classify_rc})\n")
    script = tmp_path / "helper.sh"
    script.write_text('log() { echo "$*"; }\n' + _helper_script() +
                      f'printf "%s" "$INSTR" | _mockup_gate_on_script_error 124 "{gate_script}" runner-aaaa1111\n'
                      'echo "RC=$?"\n')
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "INSTR": instruction}
    if mode is not None:
        env["MOCKUP_GATE_MODE"] = mode
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=30, check=False).stdout
    return int(out.strip().rsplit("RC=", 1)[1]), out


@pytest.mark.parametrize("mode,classify_rc,expected", [
    ("enforce", 11, 1),      # applicable -> blocked
    ("enforce", 1, 1),       # classify itself crashed -> cannot decide -> blocked (fail-closed)
    ("enforce", 0, 0),       # server rules say not applicable -> continue
    ("garbage", 11, 1),      # unreadable mode is enforce, as in mockup_gate_rules.gate_mode
    ("shadow", 11, 0),
    ("shadow", 1, 0),
    (None, 11, 0),           # default is shadow
    ("off", 11, 0),
])
def test_runner_script_error_decision(tmp_path, mode, classify_rc, expected):
    code, out = run_helper(tmp_path, mode=mode, classify_rc=classify_rc)
    assert code == expected, out
    if mode in ("shadow", None) and classify_rc != 0:
        assert "MOCKUP_GATE_SHADOW" in out and "violation=gate_script_error" in out


def test_runner_uses_the_real_server_rules_for_a_tsx_only_task(tmp_path):
    gate_script = ROOT / "scripts" / "verify_mockup_approval.py"
    assert run_helper(tmp_path, mode="enforce", gate_script=gate_script, instruction=TSX_ONLY_TASK)[0] == 1
    assert run_helper(tmp_path, mode="enforce", gate_script=gate_script, instruction=BACKEND_TASK)[0] == 0


def test_runner_no_longer_passes_on_missing_ui_words():
    text = (ROOT / "scripts" / "pipeline-runner.sh").read_text()
    assert "MOCKUP_REVIEW_ID|aads-dashboard|UI_TASK" not in text
    block = text[text.index("_mockup_out=$("):text.index("check_duplicate \"$job_id\"")]
    assert "_mockup_gate_on_script_error" in block and "MOCKUP_GATE_SHADOW" in block


# ------------------------------------------------------------------------ CLI: classify-only / offline

def load_cli():
    path = ROOT / "scripts" / "verify_mockup_approval.py"
    spec = importlib.util.spec_from_file_location("verify_mockup_approval_m8", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(args, text, env=None):
    return load_cli().run(args, stdin_text=text, environ=env or {},
                          ask=lambda *a: pytest.fail("offline/classify must not call the API"))


def test_classify_only_exit_codes():
    assert cli(["--classify-only"], TSX_ONLY_TASK)[0] == 11
    assert cli(["--classify-only"], BACKEND_TASK)[0] == 0
    assert cli(["--classify-only"], TSX_ONLY_TASK, {"MOCKUP_GATE_MODE": "off"})[0] == 0


def test_offline_reports_ui_changes_without_a_bundle_and_never_claims_approval(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text("app/api/x.py\nsrc/components/Panel.tsx\n")
    code, out = cli(["--offline", "--changed-files", str(changed)], "fix panel\n")
    assert code == 0 and out["shadow"] is True and out["reasons"] == ["mockup_bundle_required"]
    assert out["approval_verified"] is False
    assert cli(["--offline", "--changed-files", str(changed)], "fix panel\n", ENFORCE)[0] == 10
    code, out = cli(["--offline", "--changed-files", str(changed)], BUNDLE, ENFORCE)
    assert code == 0 and out["approval_verified"] is False
    assert out["reasons"] == ["bundle_declared_not_verified_offline"]
    assert cli(["--offline"], f"MOCKUP_REVIEW_ID: {REVIEW}\n")[0] == 10
    backend = tmp_path / "backend.txt"
    backend.write_text("app/api/x.py\ndocs/a.md\n")
    assert cli(["--offline", "--changed-files", str(backend)], "", ENFORCE)[1]["applicable"] is False


def test_job_id_is_still_required_for_the_worker_gate():
    with pytest.raises(SystemExit):
        cli([], UI_TASK)


def test_ci_runs_the_offline_check_as_a_warning():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    block = ci[ci.index("mockup-gate:"):]
    assert "verify_mockup_approval.py --offline" in block and "--changed-files" in block
    assert "continue-on-error: true" in block and "MOCKUP_GATE_MODE: shadow" in block
