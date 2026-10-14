"""R-DOC M8a: the mockup approval gate on every write path, persisted verdicts and the AC traceability.

Paths covered here: runner submit / pre_execution (existing), chat direct writes (phase=chat_write) and git pushes
(phase=pre_push). No database: SQL is answered by the in-memory FakeConn from test_mockup_execution_gate. The real
SQL is exercised by tests/integration/test_mockup_gate_decisions_postgres.py (needs AADS_MOCKUP_TEST_DATABASE_URL).
"""
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.services import mockup_gate_rules as rules
from app.services import mockup_review_service as svc
from tests.unit.test_mockup_execution_gate import (
    BACKEND_TASK, BUNDLE, GOAL, HASH, REVIEW, REVISION, TENANT, UI_TASK, FakeConn, head_row, load_cli, run,
)

ROOT = Path(__file__).resolve().parents[2]
OLD_REVISION = "77777777-7777-4777-8777-777777777777"
SHADOW, ENFORCE = {"MOCKUP_GATE_MODE": "shadow"}, {"MOCKUP_GATE_MODE": "enforce"}
UI_FILES = ["src/components/Panel.tsx"]
REVISION_ROW = {"id": UUID(REVISION), "manifest_hash": HASH, "doc_refs": "[]",
                "manifest": json.dumps({"assets": [{"asset_id": "a", "sha256": "0" * 64, "byte_size": 1}], "screens": []})}


class Recorder:
    """FakeConn handler pair: remembers every mockup_gate_decisions row."""

    def __init__(self, fail=False):
        self.rows, self.fail = [], fail

    def __call__(self, *args):
        if self.fail:
            raise ConnectionError("db down")
        self.rows.append(args)
        return "INSERT 0 1"

    @property
    def last(self):
        keys = ("phase", "mode", "allowed", "blocking", "shadow", "reasons", "ui_reasons", "bundle_declared",
                "task_id", "job_id", "project", "tenant", "review", "revision", "audit_event", "actor")
        return dict(zip(keys, self.rows[-1]))


def gate_paths(paths, message="", phase="chat_write", environ=None, recorder=None, extra=()):
    recorder = recorder if recorder is not None else Recorder()
    conn = FakeConn([("INSERT INTO mockup_gate_decisions", recorder), *extra])
    info = rules.classify_paths(paths, message, environ if environ is not None else SHADOW)
    result = run(svc.gate_check(conn, phase=phase, tenant=TENANT, project="AADS", task_id="chat:abc", instruction=message,
                                goal_id=GOAL, actor="chat:abc", info=info))
    return result, recorder


def denial_conn(head, recorder=None, revision=REVISION_ROW):
    """Everything verify_bundle touches on the denial path (audit event + notification fan-out)."""
    return FakeConn([
        ("INSERT INTO mockup_gate_decisions", recorder if recorder is not None else Recorder()),
        ("FOR UPDATE", lambda *a: head),
        ("FROM goal_task_links", lambda *a: 1),
        ("FROM goals WHERE id=$1::uuid", lambda *a: 1),
        ("FROM mockup_review_revisions", lambda *a: revision),
        ("FROM mockup_review_task_bindings", lambda *a: None),
        ("nextval", lambda *a: 5),
        ("INSERT INTO mockup_review_events", lambda *a: None),
        ("FROM tenant_memberships", lambda *a: []),
        ("FROM chat_sessions", lambda *a: "creator"),
        ("INSERT INTO ohvis_notifications", lambda *a: "INSERT 0 0"),
    ])


def real_gate(conn, message, phase, paths=UI_FILES, environ=ENFORCE):
    info = rules.classify_paths(paths, message, environ)
    return run(svc.gate_check(conn, phase=phase, tenant=TENANT, project="AADS", task_id="chat:abc", instruction=message,
                              goal_id=GOAL, actor="chat:abc", info=info))


DECLARATION = f"{BUNDLE}GOAL_ID: {GOAL}\n"


# ------------------------------------------------------------------ rules

def test_classify_paths_uses_the_same_ui_predicate_as_task_targets():
    assert rules.classify_paths(UI_FILES, "", SHADOW)["ui_reasons"] == ["ui_file:src/components/Panel.tsx"]
    assert rules.classify_paths(["app/api/x.py", "docs/a.md", "tests/unit/t.tsx"], "", SHADOW)["applicable"] is False
    assert rules.classify_paths(["style/main.css"], "", SHADOW)["applicable"] is True
    dash = rules.classify_paths(["aads-dashboard/src/lib/api.ts"], "", SHADOW)
    assert dash["ui_reasons"] == ["dashboard_file:aads-dashboard/src/lib/api.ts"]
    assert rules.classify_paths(["aads-dashboard/docs/readme.md", "aads-dashboard/package.json"], "", SHADOW)["applicable"] is False
    assert rules.classify_paths(UI_FILES, "", {"MOCKUP_GATE_MODE": "off"})["applicable"] is False
    assert rules.classify_paths(UI_FILES, "", {})["mode"] == "shadow"  # default stays shadow


def test_classify_paths_reads_the_bundle_declaration_from_the_message():
    declared = rules.classify_paths(["app/api/x.py"], f"fix\n\n{BUNDLE}", SHADOW)
    assert declared["declared"] and declared["applicable"] and declared["bundle"]["review_id"] == REVIEW
    fenced = rules.classify_paths(["app/api/x.py"], f"```\n{BUNDLE}```\n", SHADOW)
    assert fenced["declared"] is False and fenced["applicable"] is False


def test_a_denial_only_stops_chat_and_push_work_under_enforce():
    for phase in rules.ALWAYS_BLOCKING_PHASES:
        assert rules.stops_action(phase, "shadow") is True
    for phase in rules.DECLARATION_SCOPE_PHASES:
        assert rules.stops_action(phase, "shadow") is False and rules.stops_action(phase, "enforce") is True
    assert set(rules.PHASES) == set(rules.ALWAYS_BLOCKING_PHASES) | set(rules.DECLARATION_SCOPE_PHASES)


def test_every_gate_phase_is_known_to_the_model_and_the_migration():
    from typing import get_args

    from app.models.mockup_review import VerifyBundle

    assert set(get_args(VerifyBundle.model_fields["phase"].annotation)) == set(rules.PHASES)
    sql = (ROOT / "migrations" / "20261014_mockup_gate_decisions.sql").read_text(encoding="utf-8")
    check = re.search(r"phase text NOT NULL CHECK \(phase IN \(([^)]*)\)\)", sql).group(1)
    assert set(re.findall(r"'(\w+)'", check)) == set(rules.PHASES)


# --------------------------------------------------- shadow verdicts are persisted

def test_shadow_ui_work_without_a_declaration_is_allowed_and_recorded():
    result, rec = gate_paths(UI_FILES, "그냥 고침")
    assert result["allowed"] is True and result["shadow"] is True and result["blocking"] is False
    assert result["recorded"] is True and len(rec.rows) == 1
    row = rec.last
    assert (row["phase"], row["mode"], row["allowed"], row["blocking"], row["shadow"]) == (
        "chat_write", "shadow", True, False, True)
    assert json.loads(row["reasons"]) == ["mockup_bundle_required"]
    assert json.loads(row["ui_reasons"]) == ["ui_file:src/components/Panel.tsx"]
    assert (row["bundle_declared"], row["task_id"], row["job_id"], row["project"], row["tenant"]) == (
        False, "chat:abc", None, "AADS", TENANT)


def test_a_runner_job_id_is_stored_as_job_id():
    rec = Recorder()
    conn = FakeConn([("INSERT INTO mockup_gate_decisions", rec)])
    run(svc.gate_check(conn, phase="submit", tenant=TENANT, project="AADS", task_id="runner-aaaa1111",
                       instruction=UI_TASK, goal_id=None, actor="u1", environ=SHADOW))
    assert rec.last["job_id"] == "runner-aaaa1111" and rec.last["phase"] == "submit" and rec.last["allowed"] is True


def test_enforce_blocks_and_records_the_same_verdict():
    result, rec = gate_paths(UI_FILES, "", environ=ENFORCE)
    assert result["allowed"] is False and result["blocking"] is True and result["reasons"] == ["mockup_bundle_required"]
    assert rec.last["mode"] == "enforce" and rec.last["allowed"] is False and rec.last["blocking"] is True


def test_every_phase_is_recorded_with_its_own_name():
    for phase in rules.PHASES:
        _, rec = gate_paths(UI_FILES, phase=phase)
        assert rec.last["phase"] == phase


def test_unrelated_work_leaves_no_record_and_never_touches_the_database():
    result, rec = gate_paths(["app/api/x.py"], "backend only", environ=ENFORCE)
    assert result["applicable"] is False and result["allowed"] is True and rec.rows == []
    assert "recorded" not in result


def test_a_failing_insert_never_changes_the_verdict():
    shadow, rec = gate_paths(UI_FILES, recorder=Recorder(fail=True))
    assert shadow["allowed"] is True and shadow["shadow"] is True and shadow["recorded"] is False
    enforce, _ = gate_paths(UI_FILES, environ=ENFORCE, recorder=Recorder(fail=True))
    assert enforce["allowed"] is False and enforce["blocking"] is True and enforce["recorded"] is False
    declared, _ = gate_paths(UI_FILES, DECLARATION, environ=ENFORCE, recorder=Recorder(fail=True),
                             extra=[("FOR UPDATE", lambda *a: None)])
    assert declared["allowed"] is False and declared["reasons"] == ["review_not_found"]


def test_the_insert_runs_in_a_savepoint_so_a_missing_table_cannot_poison_the_gate_transaction():
    source = inspect.getsource(svc.record_gate_decision)
    assert "conn.transaction()" in source and "except Exception" in source


# ---------------------------------------------- chat_write / pre_push verdict rules

def test_shadow_still_denies_a_bad_declaration_but_only_stops_runner_phases():
    verdicts = {}
    for phase in ("submit", "chat_write", "pre_push"):
        verdicts[phase], rec = gate_paths(UI_FILES, f"MOCKUP_REVIEW_ID: {REVIEW}\n", phase=phase)
        assert rec.last["allowed"] is False and rec.last["bundle_declared"] is True
    assert all(v["allowed"] is False for v in verdicts.values())
    assert verdicts["submit"]["blocking"] is True
    assert verdicts["chat_write"]["blocking"] is False and verdicts["pre_push"]["blocking"] is False


def test_database_failure_fails_closed_for_declared_bundles_and_enforce_in_every_phase(monkeypatch):
    async def down(*a, **k):
        raise ConnectionError("db down")

    monkeypatch.setattr(svc, "verify_bundle", down)
    for phase in ("chat_write", "pre_push"):
        declared, _ = gate_paths(UI_FILES, DECLARATION, phase=phase, environ=SHADOW)
        assert declared["allowed"] is False and declared["reasons"] == ["gate_unavailable"]
        assert declared["recorded"] is False  # the caller retries on a fresh connection
        assert declared["blocking"] is False   # shadow: warn only for chat/push ...
        enforced, _ = gate_paths(UI_FILES, DECLARATION, phase=phase, environ=ENFORCE)
        assert enforced["allowed"] is False and enforced["blocking"] is True  # ... enforce stops
    still_shadow = rules.unavailable_decision(rules.classify_paths(UI_FILES, "", SHADOW))
    assert still_shadow["allowed"] is True  # an undeclared UI task in shadow does not need the database
    assert rules.unavailable_decision(rules.classify_paths(UI_FILES, "", ENFORCE))["allowed"] is False


def test_direct_edits_use_the_review_goal_as_scope_when_no_goal_is_declared():
    inserted = []
    head = head_row(goal_id=UUID(GOAL))
    conn = FakeConn([
        ("INSERT INTO mockup_gate_decisions", Recorder()),
        ("FOR UPDATE", lambda *a: head), ("FROM goal_task_links", lambda *a: None),
        ("FROM goals WHERE id=$1::uuid", lambda *a: 1),
        ("FROM mockup_review_revisions", lambda *a: {**REVISION_ROW, "manifest": json.dumps({"assets": [], "screens": []})}),
        ("FROM mockup_review_task_bindings", lambda *a: None),
        ("INSERT INTO mockup_review_task_bindings", lambda *a: inserted.append(a)),
        ("nextval", lambda *a: 99), ("INSERT INTO mockup_review_events", lambda *a: None),
    ])
    info = rules.classify_paths(UI_FILES, BUNDLE, ENFORCE)
    out = run(svc.gate_check(conn, phase="pre_push", tenant=TENANT, project="AADS", task_id="push-abc", instruction=BUNDLE,
                             goal_id=None, actor="u1", info=info))
    assert out["allowed"] is True and out["binding_status"] == "running" and len(inserted) == 1


# ------------------------------------------------------------ AC-M07 / M08 / M10 / M11

def test_ac_m07_one_changed_byte_blocks_execution(monkeypatch):
    original = b"body{color:blue}"
    manifest = {"assets": [{"asset_id": "style", "sha256": __import__("hashlib").sha256(original).hexdigest(),
                            "byte_size": len(original)}]}

    async def read_all(_assets):
        return [read_all.data]

    monkeypatch.setattr(svc, "_read_all", read_all)
    read_all.data = original
    assert run(svc.recheck_assets(manifest)) == []
    read_all.data = b"body{color:blue}\n"  # one extra byte
    assert run(svc.recheck_assets(manifest)) == [{"asset_id": "style", "reason": "hash_changed"}]
    read_all.data = b"body{color:bluf}"    # same length, one byte different
    assert run(svc.recheck_assets(manifest)) == [{"asset_id": "style", "reason": "hash_changed"}]

    rec = Recorder()
    revision = {**REVISION_ROW, "manifest": json.dumps({**manifest, "screens": []})}
    result = real_gate(denial_conn(head_row(), rec, revision), DECLARATION, "chat_write")
    assert result["allowed"] is False and "asset_tampered" in result["reasons"]
    assert result["blocking"] is True and rec.last["allowed"] is False and rec.last["audit_event"] == 5


def test_ac_m07_a_changed_document_revision_blocks_execution(monkeypatch):
    async def stale(*a, **k):
        raise HTTPException(409, {"code": "document_revision_changed"})

    async def intact(_manifest):
        return []

    monkeypatch.setattr(svc, "recheck_assets", intact)
    monkeypatch.setattr(svc, "validate_doc_refs", stale)
    result = real_gate(denial_conn(head_row()), DECLARATION, "pre_push")
    assert result["allowed"] is False and result["reasons"] == ["document_revision_changed"]


@pytest.mark.parametrize("phase", ["submit", "pre_execution", "checkpoint", "chat_write", "pre_push"])
def test_ac_m08_every_path_gets_the_same_denial_and_an_audit_row(phase):
    head = head_row(status="revoked", approved_revision_id=None, approval_event_id=None)
    rec = Recorder()
    result = real_gate(denial_conn(head, rec), DECLARATION, phase)
    assert result["allowed"] is False and result["reasons"] == ["approval_revoked"] and result["audit_event_id"] == 5
    assert rec.last["phase"] == phase and rec.last["audit_event"] == 5 and json.loads(rec.last["reasons"]) == ["approval_revoked"]


def test_ac_m08_the_cli_stops_runner_work_with_the_same_answer_the_api_gives():
    answer = {"allowed": False, "reasons": ["approval_revoked"], "audit_event_id": 5}
    code, out = load_cli().run(["--job-id", "runner-aaaa1111", "--api-url", "http://x"], stdin_text=DECLARATION + UI_TASK,
                               environ=ENFORCE, ask=lambda *a, **k: answer)
    assert code == 10 and out == answer


def test_ac_m10_a_review_of_another_tenant_or_without_permission_is_refused_without_leaking():
    rec = Recorder()
    other_tenant = FakeConn([("INSERT INTO mockup_gate_decisions", rec), ("FOR UPDATE", lambda *a: None)])
    result = real_gate(other_tenant, DECLARATION, "chat_write")
    assert result["allowed"] is False and result["reasons"] == ["review_not_found"]
    assert set(result) >= {"allowed", "reasons"} and "revision_id" not in result and "approval_id" not in result
    assert rec.last["tenant"] == TENANT and rec.last["review"] is None

    async def forbidden():
        raise HTTPException(403, "project_access_denied")

    info = rules.classify_paths(UI_FILES, DECLARATION, ENFORCE)
    denied = run(svc.gate_check(FakeConn([("INSERT INTO mockup_gate_decisions", rec)]), phase="chat_write", tenant=TENANT,
                                project="AADS", task_id="chat:abc", instruction="", goal_id=None, actor="u1",
                                authorize=forbidden, info=info))
    assert denied["allowed"] is False and denied["reasons"] == ["project_access_denied"]


def test_ac_m11_a_new_draft_after_revoke_inherits_no_approval():
    """Revoked head, then a new draft: nothing is approved, so neither the old nor the new revision may run."""
    revoked = head_row(status="revoked", approved_revision_id=None, approval_event_id=None)
    assert real_gate(denial_conn(revoked), DECLARATION, "pre_execution")["reasons"] == ["approval_revoked"]
    new_draft = head_row(status="draft", approved_revision_id=None, approval_event_id=None)
    assert real_gate(denial_conn(new_draft), DECLARATION, "pre_execution")["reasons"] == ["not_approved"]
    # approval kept on the old revision while a newer revision exists: the newer bundle is not approved ...
    kept = head_row(status="review_ready", approved_revision_id=UUID(OLD_REVISION))
    assert real_gate(denial_conn(kept), DECLARATION, "pre_push")["reasons"] == ["revision_mismatch"]
    # ... and the old bundle is superseded
    old_bundle = DECLARATION.replace(REVISION, OLD_REVISION)
    superseded = denial_conn(kept, revision={**REVISION_ROW, "id": UUID(OLD_REVISION)})
    assert real_gate(superseded, old_bundle, "pre_push")["reasons"] == ["superseded_by_newer_revision"]


# ----------------------------------------------- running work is flagged, never killed

def test_the_gate_never_touches_running_jobs():
    from app.api import pipeline_runner

    banned = ("kill", "terminate", "subprocess", "pipeline_jobs SET", "UPDATE pipeline_jobs", "_fail_job")
    for fn in (svc.gate_check, svc._decide, svc.record_gate_decision, pipeline_runner._run_mockup_gate,
               pipeline_runner._record_gate_decision_fresh, pipeline_runner.mockup_gate_for_job,
               pipeline_runner.mockup_gate_for_push):
        source = inspect.getsource(fn)
        assert not [w for w in banned if w in source], fn.__name__
    assert "SELECT project, instruction FROM pipeline_jobs" in inspect.getsource(pipeline_runner.mockup_gate_for_job)


def test_a_revoked_approval_at_a_checkpoint_denies_but_issues_no_job_sql():
    sql_seen = []
    head = head_row(status="revoked", approved_revision_id=None, approval_event_id=None)
    conn = denial_conn(head)
    real_answer = conn._answer
    conn._answer = lambda sql, args: (sql_seen.append(sql), real_answer(sql, args))[1]
    result = real_gate(conn, DECLARATION, "checkpoint")
    assert result["allowed"] is False and result["blocking"] is True
    assert sql_seen and not [s for s in sql_seen if "pipeline_jobs" in s or "task_logs" in s]


# ------------------------------------------------------------------ chat write tools

class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return Ctx()


@pytest.fixture
def chat(monkeypatch, request):
    from app.api import ceo_chat_tools

    rec = Recorder()
    conn = FakeConn([("INSERT INTO mockup_gate_decisions", rec)])
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _Pool(conn))

    async def tenant(*a, **k):
        return TENANT

    monkeypatch.setattr("app.services.tool_executor.resolve_bound_tenant_id", tenant)
    from app.services.tool_executor import current_chat_session_id

    token = current_chat_session_id.set("11111111-aaaa-4bbb-8ccc-222222222222")
    request.addfinalizer(lambda: current_chat_session_id.reset(token))
    writes = []

    async def impl(project, path, content, backup=True):
        writes.append((project, path))
        return f"[{project} 파일 쓰기 완료 — {path}]"

    monkeypatch.setattr(ceo_chat_tools, "_write_remote_file_impl", impl)
    return type("Chat", (), {"tools": ceo_chat_tools, "rec": rec, "writes": writes, "conn": conn, "mp": monkeypatch})


def test_chat_write_to_a_ui_file_in_shadow_records_warns_and_still_writes(chat, monkeypatch):
    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")
    out = run(chat.tools.tool_write_remote_file("GO100", "src/components/Panel.tsx", "x"))
    assert out.startswith("[GO100 파일 쓰기 완료") and "목업 승인 게이트 shadow" in out
    assert chat.writes == [("GO100", "src/components/Panel.tsx")]
    row = chat.rec.last
    assert (row["phase"], row["mode"], row["allowed"], row["blocking"], row["project"]) == (
        "chat_write", "shadow", True, False, "GO100")
    assert row["task_id"].startswith("chat:") and row["actor"].startswith("chat:")


def test_chat_write_to_a_ui_file_under_enforce_is_blocked_before_any_write(chat, monkeypatch):
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    out = run(chat.tools.tool_write_remote_file("GO100", "src/components/Panel.tsx", "x"))
    assert out.startswith("[ERROR] 목업 승인 게이트 차단") and "mockup_bundle_required" in out
    assert chat.writes == [] and chat.rec.last["blocking"] is True


def test_chat_write_to_a_backend_file_never_reaches_the_gate(chat, monkeypatch):
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    out = run(chat.tools.tool_write_remote_file("AADS", "app/api/x.py", "x"))
    assert out.startswith("[AADS 파일 쓰기 완료") and "게이트" not in out
    assert chat.rec.rows == [] and chat.conn.calls == []


def test_chat_write_is_not_gated_when_the_mode_is_off(chat, monkeypatch):
    monkeypatch.setenv("MOCKUP_GATE_MODE", "off")
    out = run(chat.tools.tool_write_remote_file("GO100", "src/components/Panel.tsx", "x"))
    assert out.startswith("[GO100 파일 쓰기 완료") and chat.conn.calls == []


def test_chat_write_declares_the_bundle_through_tool_parameters(chat, monkeypatch):
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")

    async def verify(conn, tenant, project, review_id, actor, body, declared_goal_id=None):
        verify.seen = (str(review_id), body.phase, body.task_id.split(":")[0])
        return {"allowed": True, "review_id": REVIEW, "revision_id": REVISION, "binding_status": "running"}

    monkeypatch.setattr(svc, "verify_bundle", verify)
    params = {"mockup_review_id": REVIEW, "mockup_revision_id": REVISION, "mockup_manifest_hash": HASH}
    declaration = chat.tools.mockup_declaration_from_params(params)
    assert rules.parse_bundle(declaration)["review_id"] == REVIEW and chat.tools.mockup_declaration_from_params({}) == ""
    out = run(chat.tools.tool_write_remote_file("GO100", "src/components/Panel.tsx", "x", mockup_declaration=declaration))
    assert out.startswith("[GO100 파일 쓰기 완료") and "게이트" not in out
    assert verify.seen == (REVIEW, "chat_write", "chat") and chat.rec.last["allowed"] is True


def test_chat_gate_failure_fails_closed_under_enforce_and_open_in_shadow(chat, monkeypatch):
    async def no_tenant(*a, **k):
        return ""

    monkeypatch.setattr("app.services.tool_executor.resolve_bound_tenant_id", no_tenant)
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    assert run(chat.tools.tool_write_remote_file("GO100", "src/components/Panel.tsx", "x")).startswith("[ERROR] 목업")
    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")
    assert run(chat.tools.tool_write_remote_file("GO100", "src/components/Panel.tsx", "x")).startswith("[GO100 파일 쓰기")
    assert chat.writes == [("GO100", "src/components/Panel.tsx")]


def test_patch_gates_once_and_writes_through_the_ungated_impl(chat, monkeypatch):
    async def raw(_project, _path):
        return "const a = 1\n"

    monkeypatch.setattr(chat.tools, "_read_raw_file", raw)
    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")
    out = run(chat.tools.tool_patch_remote_file("AADS", "src/app/page.tsx", "a = 1", "a = 2"))
    assert "파일 패치 완료" in out and "목업 승인 게이트 shadow" in out
    assert len(chat.rec.rows) == 1 and chat.writes == [("AADS", "src/app/page.tsx")]
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    blocked = run(chat.tools.tool_patch_remote_file("AADS", "src/app/page.tsx", "a = 1", "a = 2"))
    assert blocked.startswith("[ERROR] 목업 승인 게이트 차단") and len(chat.writes) == 1


def test_git_commit_of_staged_ui_files_goes_through_the_gate(chat, monkeypatch):
    commands = []

    async def remote(project, command):
        commands.append(command)
        return "src/components/Panel.tsx\napp/api/x.py\n" if "--name-only" in command else "[master abc] ok"

    monkeypatch.setattr(chat.tools, "tool_run_remote_command", remote)
    monkeypatch.setenv("MOCKUP_GATE_MODE", "enforce")
    out = run(chat.tools.tool_git_remote_commit("GO100", "화면 수정"))
    assert out.startswith("[ERROR] 목업 승인 게이트 차단") and not [c for c in commands if c.startswith("git commit")]

    async def review(**k):
        return type("V", (), {"verdict": "APPROVE", "score": 1.0, "issues": []})()

    monkeypatch.setattr("app.services.code_reviewer.review_code_diff", review)
    monkeypatch.setenv("MOCKUP_GATE_MODE", "shadow")
    shadow = run(chat.tools.tool_git_remote_commit("GO100", "화면 수정"))
    assert "[master abc] ok" in shadow and "목업 승인 게이트 shadow" in shadow
    assert chat.rec.last["phase"] == "chat_write" and chat.rec.last["allowed"] is True


def _tool_schemas():
    """(출처, 도구명, input_schema) — 모델에 실제로 전송되는 두 정의 표면 모두."""
    from app.api import ceo_chat_tools
    from app.services import tool_registry

    names = ("write_remote_file", "patch_remote_file", "git_remote_commit")
    by_definition = {t["name"]: t for t in ceo_chat_tools.TOOL_DEFINITIONS}
    return [(src, name, table[name]["input_schema"])
            for src, table in (("tool_registry", tool_registry._TOOLS), ("TOOL_DEFINITIONS", by_definition))
            for name in names]


def test_the_write_tools_advertise_the_optional_bundle_parameters():
    bundle = set(rules.BUNDLE_TOOL_PROPERTIES)
    for src, name, schema in _tool_schemas():
        where = f"{src}.{name}"
        if name == "git_remote_commit":
            assert not bundle & set(schema["properties"]), where  # 커밋은 메시지의 MOCKUP_* 선언으로 읽는다
        else:
            assert bundle <= set(schema["properties"]), where
            for key in bundle:
                assert schema["properties"][key] == rules.BUNDLE_TOOL_PROPERTIES[key], where
        assert not bundle & set(schema["required"]), where


def test_bundle_keys_never_leak_into_another_propertys_schema():
    bundle = set(rules.BUNDLE_TOOL_PROPERTIES)
    for src, name, schema in _tool_schemas():
        for prop, definition in schema["properties"].items():
            if prop in bundle:
                continue
            where = f"{src}.{name}.{prop}"
            assert isinstance(definition, dict) and "type" in definition, where
            assert not bundle & set(definition), where
            assert set(definition) <= {"type", "description", "enum", "default"}, where


def test_tool_definitions_serialize_for_the_anthropic_api():
    from app.api import ceo_chat_tools

    json.dumps(ceo_chat_tools.TOOL_DEFINITIONS)
    for tool in ceo_chat_tools.TOOL_DEFINITIONS:
        props = tool["input_schema"]["properties"]
        assert all(isinstance(v, dict) and "type" in v for v in props.values()), tool["name"]
        assert set(tool["input_schema"].get("required", [])) <= set(props), tool["name"]


# ------------------------------------------------------------------ CLI in pre_push mode

def _push_args(tmp_path, files=("src/components/Panel.tsx",), extra=()):
    changed = tmp_path / "changed.txt"
    changed.write_text("\n".join(files) + "\n", encoding="utf-8")
    return ["--phase", "pre_push", "--task-id", "push-abc123", "--changed-files", str(changed), "--project", "AADS",
            "--api-url", "http://x", *extra]


def test_cli_pre_push_ignores_commits_without_ui_files_and_makes_no_call(tmp_path):
    code, out = load_cli().run(_push_args(tmp_path, ("app/api/x.py",), ()), stdin_text="fix", environ=ENFORCE,
                               ask=lambda *a, **k: pytest.fail("API must not be called"))
    assert code == 0 and out["applicable"] is False


def test_cli_pre_push_shadow_warns_but_exits_zero_and_records(tmp_path):
    seen = []

    def ask(api, task, phase, timeout, **extra):
        seen.append((task, phase, extra))
        raise OSError("unreachable")

    code, out = load_cli().run(_push_args(tmp_path), stdin_text="ui change", environ=SHADOW, ask=ask)
    assert code == 0 and out["allowed"] is True and out["reasons"] == ["mockup_bundle_required"]
    assert seen and seen[0][1] == "pre_push" and seen[0][2]["paths"] == UI_FILES  # the verdict is still sent for recording


def test_cli_pre_push_enforce_stops_undeclared_ui_commits(tmp_path):
    code, out = load_cli().run(_push_args(tmp_path), stdin_text="ui change", environ=ENFORCE, ask=lambda *a, **k: {})
    assert code == 10 and out["reasons"] == ["mockup_bundle_required"]


def test_cli_pre_push_declared_bundle_denial_only_stops_under_enforce(tmp_path):
    answer = {"allowed": False, "reasons": ["approval_revoked"], "audit_event_id": 9}
    code, out = load_cli().run(_push_args(tmp_path), stdin_text=DECLARATION, environ=SHADOW, ask=lambda *a, **k: answer)
    assert code == 0 and out["blocking"] is False and out["would_exit"] == 10 and out["reasons"] == ["approval_revoked"]
    code, out = load_cli().run(_push_args(tmp_path), stdin_text=DECLARATION, environ=ENFORCE, ask=lambda *a, **k: answer)
    assert code == 10 and out == answer


def test_cli_pre_push_api_down_fails_closed_for_declared_bundles_only_under_enforce(tmp_path):
    def down(*a, **k):
        raise OSError("down")

    assert load_cli().run(_push_args(tmp_path), stdin_text=DECLARATION, environ=ENFORCE, ask=down)[0] == 20
    code, out = load_cli().run(_push_args(tmp_path), stdin_text=DECLARATION, environ=SHADOW, ask=down)
    assert code == 0 and out["would_exit"] == 20 and out["blocking"] is False
    assert load_cli().run(_push_args(tmp_path), stdin_text="ui change", environ=SHADOW, ask=down)[0] == 0


# ------------------------------------------------------------------ the git hook

HOOK = ROOT / "scripts" / "hooks" / "pre-push"


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
@pytest.mark.parametrize("mode,message,expect_rc", [
    ("shadow", "ui change", 0),
    ("enforce", "ui change", 1),
    ("enforce", "backend only", 0),
])
def test_pre_push_hook_runs_the_mockup_gate_per_commit(tmp_path, mode, message, expect_rc):
    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "app" / "services").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "verify_mockup_approval.py", repo / "scripts")
    shutil.copy(ROOT / "app" / "services" / "mockup_gate_rules.py", repo / "app" / "services")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "MOCKUP_GATE_MODE": mode, "AADS_API_URL": "http://127.0.0.1:9"}
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    target = "src/components/Panel.tsx" if message == "ui change" else "app/api/x.py"
    (repo / target).parent.mkdir(parents=True, exist_ok=True)
    (repo / target).write_text("x", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", message)
    head = git("rev-parse", "HEAD")
    proc = subprocess.run(["bash", str(HOOK), "origin", "file:///nowhere"], cwd=repo, env=env, capture_output=True, text=True,
                          input=f"refs/heads/main {head} refs/heads/main {base}\n", timeout=60)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == expect_rc, combined
    if mode == "shadow":
        assert "mockup" in combined.lower()


# ------------------------------------------------------------------ migration

def test_the_migration_covers_every_column_the_recorder_writes_and_touches_nothing_else():
    sql = (ROOT / "migrations" / "20261014_mockup_gate_decisions.sql").read_text(encoding="utf-8")
    columns = re.search(r"INSERT INTO mockup_gate_decisions\(([^)]*)\)", svc._RECORD_SQL).group(1).split(",")
    for column in columns:
        assert re.search(rf"^\s+{column}\s", sql, re.M), column
    assert len(columns) == len(set(re.findall(r"\$(\d+)", svc._RECORD_SQL)))
    for statement in re.findall(r"^(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE)\b[^;]*;", sql, re.M | re.S):
        assert statement == "CREATE"
    assert re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql) == ["mockup_gate_decisions"]
    assert all("IF NOT EXISTS" in line for line in sql.splitlines() if line.startswith("CREATE INDEX"))
    assert "ALTER TABLE" not in sql and "DROP" not in sql and "mode" in sql
    down = (ROOT / "migrations" / "rollback" / "20261014_mockup_gate_decisions.down.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS mockup_gate_decisions" in down
