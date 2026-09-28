"""Approved document context must fail closed at both injection points."""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.api import canonical_documents as docs
from app.services import context_builder

TENANT = "00000000-0000-0000-0000-000000000001"


class BriefConnection:
    def __init__(self, *, scope=None, grant=True, rows=(), fail=False):
        self.scope = scope
        self.grant = grant
        self.rows = rows
        self.fail = fail
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("db unavailable")
        return self.scope

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.grant

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


def _scope(**changes):
    return {"tenant_id": TENANT, "project_key": "AADS", "user_id": "user-1", **changes}


def _row(**changes):
    return {"document_key": "plan", "title": "Plan", "version": "1.0.0",
            "excerpt": "approved text", **changes}


def test_approved_brief_uses_only_scoped_approved_revision():
    conn = BriefConnection(rows=[_row()])
    assert asyncio.run(docs.approved_brief(conn, TENANT, "aads")) == [_row()]
    sql, args = conn.calls[-1]
    assert "r.id=h.approved_revision_id" in sql
    assert "r.head_id=h.id AND r.tenant_id=h.tenant_id AND r.project_key=h.project_key" in sql
    assert "h.tenant_id=$1::uuid AND h.project_key=$2" in sql
    assert args == (TENANT, "AADS", 8)


@pytest.mark.parametrize("scope,grant,rows,fail", [
    (None, True, (), False),
    (_scope(project_key=None), True, (), False),
    (_scope(user_id=None), True, (), False),
    (_scope(), False, [_row()], False),
    (_scope(), True, (), False),
    (_scope(), True, [_row()], True),
])
def test_session_brief_unavailable_without_full_scope_grant_or_database(scope, grant, rows, fail):
    conn = BriefConnection(scope=scope, grant=grant, rows=rows, fail=fail)
    result = asyncio.run(context_builder._build_approved_document_layer("session-id", conn))
    assert result == "승인된 정본 문서 없음/조회 불가"
    if scope is None or not grant:
        assert not any("project_document_heads" in sql for sql, _ in conn.calls)


def test_session_brief_uses_persisted_scope_and_grant():
    conn = BriefConnection(scope=_scope(), rows=[_row()])
    result = asyncio.run(context_builder._build_approved_document_layer("session-id", conn))
    assert "approved text" in result
    assert conn.calls[0][1] == ("session-id",)
    assert "w.tenant_id=s.tenant_id" in conn.calls[0][0]
    assert conn.calls[1][1][:3] == (TENANT, "AADS", "user-1")
    assert conn.calls[2][1][:2] == (TENANT, "AADS")


def test_draft_archive_and_secret_content_are_never_promoted():
    assert docs.format_approved_brief([]) == "승인된 정본 문서 없음/조회 불가"
    assert docs.format_approved_brief([_row(excerpt="password=hidden")]) == "승인된 정본 문서 없음/조회 불가"
    # Draft and archived revisions have no approved pointer and yield no rows.
    conn = BriefConnection(scope=_scope(), rows=[])
    assert asyncio.run(context_builder._build_approved_document_layer("session-id", conn)) == "승인된 정본 문서 없음/조회 불가"


def test_brief_has_strict_character_budget():
    text = docs.format_approved_brief([_row(document_key=str(i), excerpt="x" * 4000) for i in range(8)])
    assert len(text) <= 6000
    assert "x" * 1201 not in text


def test_message_assembly_includes_scoped_brief_without_replacing_other_layers(monkeypatch):
    async def empty(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(context_builder, "build_layer1", lambda *_args, **_kwargs: "BASE")
    monkeypatch.setattr(context_builder, "_build_layer4", lambda *_args, **_kwargs: "LAYER4")
    monkeypatch.setattr(context_builder, "_build_layer2_dynamic", empty)
    monkeypatch.setattr(context_builder, "_build_memory_layer", empty)
    monkeypatch.setattr(context_builder, "_build_auto_rag_layer_bounded", empty)
    monkeypatch.setattr(context_builder, "_build_workspace_preload_layer", empty)
    monkeypatch.setattr(context_builder, "_build_artifact_context_layer", empty)
    conn = BriefConnection(scope=_scope(), rows=[_row()])
    messages, prompt = asyncio.run(context_builder.build_messages_context(
        "OTHER DISPLAY NAME", "session-id", [{"role": "user", "content": "question"}],
        db_conn=conn, apply_prompt_assets=False,
    ))
    assert "BASE" in prompt and "LAYER4" in prompt and "approved text" in prompt
    assert messages and messages[-1]["content"] == "question"
    assert conn.calls[-1][1][:2] == (TENANT, "AADS")


def test_both_runner_templates_inject_after_aag_and_scope_to_job():
    root = Path(__file__).resolve().parents[2]
    primary = (root / "scripts/pipeline-runner.sh").read_text()
    local = (root / "scripts/pipeline-runner.sh.local").read_text()
    assert primary == local
    for script in (primary, local):
        assert "approved_document_brief=$(approved_document_runner_brief \"$job_id\" \"$project\")" in script
        assert script.index("${aag_brief}\n[프로젝트 문서 조회 결과]\n${approved_document_brief}") > script.index("AAG 착수 브리프")
        assert "h.tenant_id=p.tenant_id AND h.project_key=p.project" in script
        assert "r.id=h.approved_revision_id" in script
        assert "s.tenant_id=p.tenant_id" in script
        assert "g.tenant_id=p.tenant_id" in script
        assert "g.project_key=p.project AND g.user_id=s.user_id::text" in script


def test_runner_brief_formats_bounded_rows_and_fails_closed():
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/pipeline-runner.sh").read_text()
    function = script.split("approved_document_runner_brief() {", 1)[1].split("\n# 실패 지점", 1)[0]
    command = "db_exec() { printf '%s' \"$BRIEF_ROWS\"; }\napproved_document_runner_brief() {" + function + "\napproved_document_runner_brief \"$1\" \"$2\""

    def run(rows, job="job-1", project="AADS"):
        env = {**os.environ, "BRIEF_ROWS": json.dumps(rows)}
        return subprocess.run(["bash", "-c", command, "bash", job, project],
                              text=True, capture_output=True, env=env, check=True).stdout

    assert "approved text" in run([{"key": "plan", "title": "Plan", "version": "1.0.0", "excerpt": "approved text"}])
    assert run([]).strip() == "승인된 정본 문서 없음/조회 불가"
    assert run([{"key": "plan", "title": "Plan", "version": "1.0.0", "excerpt": "password=hidden"}]).strip() == "승인된 정본 문서 없음/조회 불가"
    assert run([{"key": "plan", "title": "Plan", "version": "1.0.0", "excerpt": "x" * 4000}]).count("x") <= 1200
    assert run([{"key": "plan", "title": "Plan", "version": "1.0.0", "excerpt": "approved text"}], project="AADS';SELECT").strip() == "승인된 정본 문서 없음/조회 불가"
