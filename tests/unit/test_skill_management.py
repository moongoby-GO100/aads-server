"""스킬 등록·관리 서버 계약 테스트 (AADS-SKILL-MGMT-SERVER-20261028).

고정하는 계약
  1. 목록 API: 기존 필드 유지 + version_count/active_version/latest_run/run_count/source/file_drift,
     필터(project/risk_tier/enabled/source/q) 전달·검증.
  2. 상세 API: 버전 이력 + 최근 10건 실행(요약·마스킹), 잘못된 id 422, 없는 id 404.
  3. 파일 스킬 import: dry-run 기본, 멱등, 기존 내용 비덮어쓰기, 해시 불일치 → file_drift 만.
  4. 채팅 도구: read 즉시 / write·deploy·auth·financial 승인 게이트 / destructive·candidate 거부.
  5. validate·promote·rollback·disable 은 elevated(CEO) 전용 — 비CEO 403.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.services import ohvis_harness as harness  # noqa: E402
from app.services import skill_chat_tools as sct  # noqa: E402
from app.services import skill_file_import as sfi  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
TENANT = "00000000-0000-0000-0000-000000000001"
SKILL_ID = "11111111-1111-1111-1111-111111111111"
SESSION = "22222222-2222-2222-2222-222222222222"


def run(coro):
    return asyncio.run(coro)


class FakeConn:
    def __init__(self, fetch_rows=None, fetchrow_map=None):
        self.fetch_rows = fetch_rows or {}
        self.fetchrow_map = fetchrow_map or {}
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        for needle, rows in self.fetch_rows.items():
            if needle in sql:
                return rows
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        for needle, row in self.fetchrow_map.items():
            if needle in sql:
                return row
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acquire()


@pytest.fixture
def use_pool(monkeypatch):
    def _use(conn):
        import app.core.db_pool as db_pool

        monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))
        return conn

    return _use


# ---------------------------------------------------------------- 1. 목록 API


def _list_row(**changes):
    row = {
        "skill_id": SKILL_ID, "slug": "alpha", "title": "Alpha", "description": "d",
        "projects": ["AADS"], "intents": ["ops"], "enabled": True, "risk_tier": "read",
        "metadata_text": "{}", "version_id": "v1", "version": "1.0.0", "status": "active",
        "manifest": {}, "content_sha256": "abc", "created_at": "2026-10-28",
        "version_count": 3, "active_version": "1.0.0", "run_count": 7,
        "latest_run_status": "succeeded", "latest_run_started_at": "2026-10-28T01:00:00",
    }
    row.update(changes)
    return row


def test_list_adds_catalogue_fields_and_keeps_legacy_fields(use_pool):
    conn = use_pool(FakeConn(fetch_rows={"FROM ops_skill_library l": [_list_row()]}))
    items = run(harness.list_skill_manifests(tenant_id=TENANT))
    item = items[0]
    for legacy in ("skill_id", "slug", "title", "description", "projects", "intents", "enabled",
                   "version_id", "version", "status", "manifest", "content_sha256", "created_at"):
        assert legacy in item
    assert item["version_count"] == 3
    assert item["active_version"] == "1.0.0"
    assert item["run_count"] == 7
    assert item["latest_run"] == {"status": "succeeded", "started_at": "2026-10-28T01:00:00"}
    assert item["source"] == "db"
    assert item["file_drift"] is False
    assert "metadata_text" not in item
    assert "latest_run_status" not in item
    assert conn.calls[0][1][0] == TENANT


def test_list_without_runs_has_null_latest_run(use_pool):
    use_pool(FakeConn(fetch_rows={"FROM ops_skill_library l": [
        _list_row(latest_run_status=None, latest_run_started_at=None, run_count=0, version_count=0,
                  active_version=None, version=None, status=None)]}))
    item = run(harness.list_skill_manifests(tenant_id=TENANT))[0]
    assert item["latest_run"] is None
    assert item["run_count"] == 0 and item["version_count"] == 0


def test_list_passes_all_filters_to_sql_in_order(use_pool):
    conn = use_pool(FakeConn())
    run(harness.list_skill_manifests(
        tenant_id=TENANT, slug="s", status="active", project="aads", risk_tier="write",
        enabled=False, source="file", q="수집",
    ))
    sql, args = conn.calls[0]
    assert args == (TENANT, "s", "active", "aads", "write", False, "file", "수집")
    assert "unnest(l.projects)" in sql and "l.risk_tier=$5" in sql and "l.enabled=$6" in sql
    assert "metadata->>'source_path'" in sql and "strpos(lower(" in sql
    assert " LIKE " not in sql.upper().replace("ILIKE", "")  # q 는 와일드카드를 해석하지 않는다


def test_list_rejects_unknown_filter_values_before_db(use_pool):
    conn = use_pool(FakeConn())
    for kwargs in ({"risk_tier": "root"}, {"source": "cloud"}):
        with pytest.raises(harness.SkillRegistryError) as exc:
            run(harness.list_skill_manifests(tenant_id=TENANT, **kwargs))
        assert exc.value.status_code == 422
    assert conn.calls == []


def test_list_marks_file_source_and_live_drift(use_pool, tmp_path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("---\nname: alpha\n---\nbody\n", encoding="utf-8")
    good = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    meta_ok = json.dumps({"source_path": str(skill_file), "content_sha256": good})
    meta_bad = json.dumps({"source_path": str(skill_file), "content_sha256": "0" * 64})
    use_pool(FakeConn(fetch_rows={"FROM ops_skill_library l": [
        _list_row(metadata_text=meta_ok), _list_row(slug="beta", metadata_text=meta_bad)]}))
    ok, bad = run(harness.list_skill_manifests(tenant_id=TENANT, source="file"))
    assert ok["source"] == "file" and ok["file_drift"] is False
    assert bad["source"] == "file" and bad["file_drift"] is True
    assert ok["file_path"] == str(skill_file)


def test_unreadable_file_falls_back_to_stored_drift_flag():
    meta = {"source_path": "/nonexistent/SKILL.md", "content_sha256": "a" * 64, "file_drift": True}
    assert sfi.compute_file_drift(meta) is True
    assert sfi.compute_file_drift({**meta, "file_drift": False}) is False
    assert sfi.compute_file_drift({}) is False


# ---------------------------------------------------------------- 2. 상세 API


def test_detail_returns_versions_and_last_runs_with_masked_summaries(use_pool):
    big_input = {"query": "x" * 600, "api_key": "placeholder-value", "nested": {"password": "pw-placeholder"}}
    runs = [{
        "run_id": f"r{i}", "version": "1.0.0", "status": "succeeded", "input": json.dumps(big_input),
        "output": {"count": i, "token": "tok-placeholder"}, "error": None, "cost_usd": "0.000100",
        "latency_ms": 12, "input_hash": "h", "result_hash": "r", "approval_id": None,
        "started_at": "2026-10-28", "completed_at": "2026-10-28",
    } for i in range(10)]
    runs[0]["error"] = "E" * 500
    conn = use_pool(FakeConn(
        fetchrow_map={"FROM ops_skill_library l": {
            "skill_id": SKILL_ID, "slug": "alpha", "title": "Alpha", "description": "", "projects": [],
            "intents": [], "risk_tier": "read", "allowed_tools": [], "enabled": True,
            "source_path": "builtin:alpha", "metadata_text": "{}", "created_at": "t", "updated_at": "t"}},
        fetch_rows={
            "FROM ops_skill_versions v": [
                {"version_id": "a", "version": "1.1.0", "status": "candidate", "content_sha256": "x",
                 "manifest": {}, "created_at": "t2", "promoted_at": None, "promoted_by": None},
                {"version_id": "b", "version": "1.0.0", "status": "active", "content_sha256": "y",
                 "manifest": {}, "created_at": "t1", "promoted_at": "t", "promoted_by": "ceo"}],
            "FROM ops_skill_runs r": runs}))
    detail = run(harness.get_skill_detail(tenant_id=TENANT, skill_id=SKILL_ID))
    assert detail["version_count"] == 2 and detail["active_version"] == "1.0.0"
    assert [v["version"] for v in detail["versions"]] == ["1.1.0", "1.0.0"]
    assert len(detail["runs"]) == 10
    first = detail["runs"][0]
    assert len(first["input_summary"]) <= 240 and len(first["error"]) <= 240
    assert "placeholder-value" not in first["input_summary"] and "pw-placeholder" not in first["input_summary"]
    assert "tok-placeholder" not in first["result_summary"]
    assert first["cost_usd"] == pytest.approx(0.0001) and first["latency_ms"] == 12
    assert "input" not in first and "output" not in first
    runs_sql = [c for c in conn.calls if "FROM ops_skill_runs r" in c[0]][0][0]
    assert "LIMIT 10" in runs_sql and "tenant_id=$2" in runs_sql


def test_detail_validates_id_and_reports_missing(use_pool):
    use_pool(FakeConn())
    with pytest.raises(harness.SkillRegistryError) as bad:
        run(harness.get_skill_detail(tenant_id=TENANT, skill_id="not-a-uuid"))
    assert bad.value.status_code == 422
    with pytest.raises(harness.SkillRegistryError) as missing:
        run(harness.get_skill_detail(tenant_id=TENANT, skill_id=SKILL_ID))
    assert missing.value.status_code == 404 and missing.value.code == "skill_not_found"


# ---------------------------------------------------------------- 3. import


def _write_skill(base: Path, name: str, body: str = "body\n", frontmatter_name: str | None = None) -> Path:
    path = base / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    fm = f"name: {frontmatter_name}\n" if frontmatter_name else ""
    path.write_text(f"---\n{fm}description: >-\n  설명 한 줄\n  이어지는 줄\n---\n{body}", encoding="utf-8")
    return path


def test_scan_reads_frontmatter_hash_and_defaults(tmp_path):
    path = _write_skill(tmp_path, "dirname", frontmatter_name="real-name")
    _write_skill(tmp_path, "no-name")
    entries = {e["slug"]: e for e in sfi.scan_skill_files([tmp_path])}
    assert set(entries) == {"real-name", "no-name"}
    entry = entries["real-name"]
    assert entry["description"] == "설명 한 줄 이어지는 줄"
    assert entry["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["risk_tier"] == "read" and entry["enabled"] is True
    assert entry["source_path"] == str(path)


def test_scan_first_directory_wins_for_duplicate_slug(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    first = _write_skill(a, "dup")
    _write_skill(b, "dup", body="other\n")
    entries = sfi.scan_skill_files([a, b])
    assert len(entries) == 1 and entries[0]["source_path"] == str(first)


def test_plan_is_idempotent_and_detects_drift_without_overwriting(tmp_path):
    path = _write_skill(tmp_path, "alpha")
    entries = sfi.scan_skill_files([tmp_path])
    assert [p["action"] for p in sfi.plan_import(entries, {})] == ["insert"]

    stored = {"alpha": {"metadata": {"source_path": str(path), "content_sha256": entries[0]["content_sha256"],
                                      "file_drift": False}}}
    assert [p["action"] for p in sfi.plan_import(entries, stored)] == ["unchanged"]
    assert [p["action"] for p in sfi.plan_import(entries, stored)] == ["unchanged"]  # 재실행도 동일

    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    drifted = sfi.plan_import(sfi.scan_skill_files([tmp_path]), stored)
    assert [p["action"] for p in drifted] == ["drift"]

    stored["alpha"]["metadata"]["file_drift"] = True
    stored["alpha"]["metadata"]["content_sha256"] = sfi.scan_skill_files([tmp_path])[0]["content_sha256"]
    assert [p["action"] for p in sfi.plan_import(sfi.scan_skill_files([tmp_path]), stored)] == ["resync_flag"]


def test_plan_skips_db_origin_rows_with_same_slug(tmp_path):
    _write_skill(tmp_path, "runner-recovery")
    existing = {"runner-recovery": {"metadata": json.dumps({"harness": "ops"})}}
    plan = sfi.plan_import(sfi.scan_skill_files([tmp_path]), existing)
    assert plan[0]["action"] == "skip_db_origin"


def _load_import_script():
    spec = importlib.util.spec_from_file_location("import_file_skills_under_test",
                                                  _REPO / "scripts" / "import_file_skills.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_import_script_defaults_to_dry_run_and_never_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PGHOST", "unit-test-host")
    _write_skill(tmp_path, "alpha")
    script = _load_import_script()
    import error_book

    statements: list[str] = []
    monkeypatch.setattr(error_book, "psql", lambda sql: statements.append(sql) or "")
    assert script.main(["--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "mode=dry-run" in out and "insert" in out and "alpha" in out
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")


def test_apply_sql_inserts_only_missing_and_never_updates_content(tmp_path):
    path = _write_skill(tmp_path, "alpha")
    script = _load_import_script()
    entries = sfi.scan_skill_files([tmp_path])
    sql = script.build_sql(sfi.plan_import(entries, {}))
    assert "INSERT INTO ops_skill_library" in sql and "ON CONFLICT (slug) DO NOTHING" in sql
    assert "'read'" in sql and str(path) in sql and entries[0]["content_sha256"] in sql

    stored = {"alpha": {"metadata": {"source_path": str(path), "content_sha256": "0" * 64}}}
    drift_sql = script.build_sql(sfi.plan_import(entries, stored))
    assert "jsonb_set(metadata,'{file_drift}'" in drift_sql and "INSERT" not in drift_sql
    for forbidden in ("title=", "description=", "risk_tier=", "enabled=", "DELETE"):
        assert forbidden not in drift_sql
    assert "--apply" in (_REPO / "scripts" / "import_file_skills.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 4. 채팅 도구 게이트


def _manifest(tier: str = "read", status: str = "active", **changes):
    value = {
        "skill_id": SKILL_ID, "version": "1.0.0",
        "input_schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                         "properties": {"query": {"type": "string"}}, "required": ["query"],
                         "additionalProperties": False},
        "output_schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                          "properties": {"count": {"type": "integer"}}, "required": ["count"],
                          "additionalProperties": False},
        "executor": "test.skill-mgmt-count", "allowed_tools": ["site.search"], "timeout_seconds": 10,
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "idempotency": {"mode": "required"},
        "preconditions": ["authenticated"], "postconditions": ["recorded"], "evidence": ["result_hash"],
        "risk_tier": tier, "status": status, "provenance": {"source": "unit", "project": "AADS"},
    }
    value.update(changes)
    return value


def _skill_rows(tier="read", status="active", library_tier=None, enabled=True, version="1.0.0"):
    manifest = _manifest(tier, status, version=version)
    return [{"skill_id": SKILL_ID, "slug": "alpha", "library_tier": library_tier or tier, "enabled": enabled,
             "version": version, "status": status, "manifest_text": json.dumps(manifest)}]


@pytest.fixture
def gate(monkeypatch):
    harness.register_skill_executor("test.skill-mgmt-count", lambda payload: {"count": 1})
    from app.services import live_trading_guard as guard

    state = type("S", (), {})()
    state.executed = AsyncMock(return_value={"id": "run-1", "status": "succeeded", "output": {"count": 1},
                                             "result_hash": "rh", "latency_ms": 3, "error": None})
    state.is_approved = AsyncMock(return_value=False)
    state.request_approval = AsyncMock(return_value="req-1")
    monkeypatch.setattr(harness, "execute_skill", state.executed)
    monkeypatch.setattr(guard, "is_approved", state.is_approved)
    monkeypatch.setattr(guard, "request_approval", state.request_approval)
    state.pool_rows = []

    class _Pool(FakePool):
        pass

    def make_pool():
        conn = FakeConn(fetch_rows={"FROM ops_skill_library l": state.pool_rows})
        return _Pool(conn)

    state.make_pool = make_pool
    yield state
    harness.unregister_skill_executor("test.skill-mgmt-count")


def _exec(state, rows, **inp):
    state.pool_rows = rows
    payload = {"skill": "alpha", "input": {"query": "q"}, **inp}
    return run(sct.skill_execute(tenant_id=TENANT, session_id=SESSION, inp=payload, pool=state.make_pool()))


def test_read_tier_executes_immediately_without_approval(gate):
    result = _exec(gate, _skill_rows("read"))
    assert result["run_id"] == "run-1" and result["status"] == "succeeded" and result["output"] == {"count": 1}
    gate.is_approved.assert_not_awaited()
    gate.request_approval.assert_not_awaited()
    kwargs = gate.executed.await_args.kwargs
    assert kwargs["gateway_approved"] is False
    assert kwargs["skill_id"] == SKILL_ID and kwargs["version"] == "1.0.0"
    assert kwargs["idempotency_key"].startswith("chat:")
    intent = kwargs["action_intent"]
    assert intent.source == "internal_control" and intent.tenant_id == TENANT
    assert dict(intent.payload) == {"skill_id": SKILL_ID, "version": "1.0.0", "input": {"query": "q"}}


@pytest.mark.parametrize("tier,level", [("write", "high"), ("deploy", "high"),
                                        ("auth", "critical"), ("financial", "critical")])
def test_high_risk_tiers_require_guard_approval_and_do_not_execute(gate, tier, level):
    result = _exec(gate, _skill_rows(tier))
    assert result["status"] == "approval_required" and result["approval_request_id"] == "req-1"
    assert result["risk_tier"] == tier
    gate.executed.assert_not_awaited()
    args, kwargs = gate.request_approval.await_args
    assert args[0] == "skill_execute"
    assert kwargs["tier"] == "approve" and kwargs["risk_level"] == level
    assert kwargs["gate_source"] == "skill_execute" and kwargs["session_id"] == SESSION
    target = args[1]["target"]
    assert target.startswith(f"skill:{SKILL_ID}:1.0.0:sha256:") or target.startswith(f"skill:{SKILL_ID}:1.0.0:")


@pytest.mark.parametrize("tier", ["write", "deploy", "auth", "financial"])
def test_approved_high_risk_executes_with_gateway_flag(gate, tier):
    gate.is_approved.return_value = True
    result = _exec(gate, _skill_rows(tier))
    assert result["run_id"] == "run-1"
    gate.request_approval.assert_not_awaited()
    assert gate.executed.await_args.kwargs["gateway_approved"] is True


def test_approval_request_failure_fails_closed(gate):
    gate.request_approval.return_value = None
    result = _exec(gate, _skill_rows("write"))
    assert result["error"] == "approval_request_failed"
    gate.executed.assert_not_awaited()


def test_library_tier_stricter_than_manifest_still_gates(gate):
    result = _exec(gate, _skill_rows("read", library_tier="auth"))
    assert result["status"] == "approval_required" and result["risk_tier"] == "auth"
    assert sct.stricter_tier("read", "financial") == "financial"
    assert sct.stricter_tier("write", "mystery") == "mystery"


def test_destructive_is_rejected_even_when_guard_would_approve(gate):
    gate.is_approved.return_value = True
    result = _exec(gate, _skill_rows("destructive"))
    assert result["error"] == "skill_policy_rejected"
    gate.is_approved.assert_not_awaited()
    gate.executed.assert_not_awaited()


def test_unknown_library_tier_is_rejected(gate):
    result = _exec(gate, _skill_rows("read", library_tier="mystery"))
    assert result["error"] == "unknown_risk_tier"
    gate.executed.assert_not_awaited()


def test_candidate_versions_are_never_executed(gate):
    only_candidate = _skill_rows("read", status="candidate")
    assert _exec(gate, only_candidate)["error"] == "no_active_version"
    explicit = _exec(gate, only_candidate, version="1.0.0")
    assert explicit["error"] == "version_not_active" and explicit["version_status"] == "candidate"
    both = _skill_rows("read", status="candidate", version="2.0.0") + _skill_rows("read", version="1.0.0")
    assert _exec(gate, both)["version"] == "1.0.0"  # active 가 있으면 candidate 를 건너뛴다
    assert _exec(gate, both, version="2.0.0")["error"] == "version_not_active"
    gate.is_approved.assert_not_awaited()
    assert gate.executed.await_count == 1


def test_invalid_input_is_rejected_before_asking_for_approval(gate):
    result = _exec(gate, _skill_rows("write"), input={"query": 3})
    assert result["error"] == "input_schema_validation_failed"
    gate.request_approval.assert_not_awaited()
    gate.executed.assert_not_awaited()


def test_missing_skill_disabled_skill_and_missing_session(gate):
    assert _exec(gate, [])["error"] == "skill_not_found"
    assert _exec(gate, _skill_rows(enabled=False))["error"] == "skill_disabled"
    gate.pool_rows = _skill_rows()
    result = run(sct.skill_execute(tenant_id=TENANT, session_id="", inp={"skill": "alpha"},
                                   pool=gate.make_pool()))
    assert result["error"] == "missing_session_id"
    assert run(sct.skill_execute(tenant_id=TENANT, session_id=SESSION, inp={}, pool=gate.make_pool())
               )["error"] == "invalid_input"
    gate.executed.assert_not_awaited()


def test_registry_error_from_executor_is_returned_as_tool_error(gate):
    gate.executed.side_effect = harness.SkillRegistryError("skill_execution_failed", "boom", status_code=502)
    result = _exec(gate, _skill_rows("read"))
    assert result == {"error": "skill_execution_failed", "message": "boom", "status_code": 502}


def test_skill_find_delegates_to_harness_find_skills(monkeypatch):
    monkeypatch.setattr(harness, "find_skills", AsyncMock(return_value={"skills": [{"slug": "alpha"}]}))
    result = run(sct.skill_find(inp={"query": "수집", "project": "AADS", "limit": "3"}))
    assert result["skills"][0]["slug"] == "alpha"
    harness.find_skills.assert_awaited_once_with(query="수집", project="AADS", intent=None, limit=3)
    assert run(sct.skill_find(inp={}))["error"] == "invalid_input"


# --- execute_skill 의 gateway_approved: 승인 범위 일치만 대체하고 정책 거부는 우회하지 못한다


def _persisted_row(tier: str):
    manifest = harness.validate_skill_manifest(_manifest(tier))
    return {"slug": "alpha", "risk_tier": tier, "version_id": "ver-1", "status": "active",
            "manifest": manifest, "content": harness._canonical_json(manifest),
            "content_sha256": harness._sha256(manifest)}


def _intent():
    return sct._build_intent(TENANT, SESSION, {"skill_id": SKILL_ID, "version": "1.0.0",
                                               "input": {"query": "q"}}, "corr-1")


def _exec_conn(tier):
    updated = {"id": "run-1", "status": "succeeded", "output": {"count": 1}}
    return FakeConn(fetchrow_map={
        "FROM ops_skill_library l JOIN ops_skill_versions v": _persisted_row(tier),
        "INSERT INTO ops_skill_runs": {"id": "run-1"},
        "UPDATE ops_skill_runs": updated,
    })


@pytest.fixture
def real_executor():
    harness.register_skill_executor("test.skill-mgmt-count", lambda payload: {"count": 1})
    yield
    harness.unregister_skill_executor("test.skill-mgmt-count")


def test_execute_skill_requires_approval_id_without_gateway_flag(use_pool, real_executor):
    use_pool(_exec_conn("write"))
    with pytest.raises(harness.SkillRegistryError) as exc:
        run(harness.execute_skill(tenant_id=TENANT, skill_id=SKILL_ID, version="1.0.0",
                                  input_data={"query": "q"}, idempotency_key="idem-12345",
                                  action_intent=_intent()))
    assert exc.value.code == "human_gateway_approval_required"


def test_execute_skill_gateway_flag_replaces_only_the_scope_match(use_pool, real_executor):
    conn = use_pool(_exec_conn("write"))
    result = run(harness.execute_skill(tenant_id=TENANT, skill_id=SKILL_ID, version="1.0.0",
                                       input_data={"query": "q"}, idempotency_key="idem-12345",
                                       action_intent=_intent(), gateway_approved=True))
    assert result["status"] == "succeeded"
    assert any("INSERT INTO ops_skill_runs" in sql for sql, _ in conn.calls)
    assert not any("agent_permission_requests" in sql for sql, _ in conn.calls)


def test_execute_skill_gateway_flag_cannot_run_destructive(use_pool, real_executor):
    use_pool(_exec_conn("destructive"))
    with pytest.raises(harness.SkillRegistryError) as exc:
        run(harness.execute_skill(tenant_id=TENANT, skill_id=SKILL_ID, version="1.0.0",
                                  input_data={"query": "q"}, idempotency_key="idem-12345",
                                  action_intent=_intent(), gateway_approved=True))
    assert exc.value.code == "skill_policy_rejected"


# ---------------------------------------------------------------- 도구 등록 정합성


def test_chat_tools_are_registered_dispatched_and_classified():
    from app.services import plan_mode_guard, tool_executor, tool_registry

    for name in ("skill_find", "skill_execute"):
        assert name in tool_registry._TOOLS
        assert name in tool_registry._GROUPS["action"]
        assert name in tool_executor._DATABASE_TOOLS
        assert hasattr(tool_executor.ToolExecutor, f"_{name}")
        assert f'"{name}":' in inspect.getsource(tool_executor.ToolExecutor._dispatch)
    assert "skill_find" in plan_mode_guard._READ_ONLY_TOOLS
    assert "skill_execute" not in plan_mode_guard._READ_ONLY_TOOLS
    assert tool_registry._TOOLS["skill_execute"]["input_schema"]["required"] == ["skill"]
    source = (_REPO / "app/services/skill_chat_tools.py").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in source


def test_skill_execute_handler_requires_tenant_binding():
    from app.services import tool_executor as te

    token_s, token_t = te.current_chat_session_id.set(""), te.current_tenant_id.set("")
    try:
        result = run(te.ToolExecutor()._skill_execute({"skill": "alpha"}))
    finally:
        te.current_tenant_id.reset(token_t)
        te.current_chat_session_id.reset(token_s)
    assert result["error"] == "missing_tenant_id"


def test_skill_execute_handler_passes_bound_tenant_and_session(monkeypatch):
    from app.services import tool_executor as te

    fake = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(sct, "skill_execute", fake)
    token_s, token_t = te.current_chat_session_id.set(SESSION), te.current_tenant_id.set(TENANT)
    try:
        assert run(te.ToolExecutor()._skill_execute({"skill": "alpha"})) == {"ok": True}
    finally:
        te.current_tenant_id.reset(token_t)
        te.current_chat_session_id.reset(token_s)
    assert fake.await_args.kwargs["tenant_id"] == TENANT and fake.await_args.kwargs["session_id"] == SESSION


# ---------------------------------------------------------------- 5. API 권한·노출


def _context(role: str, *, internal_admin: bool = False):
    return {"tenant": {"id": TENANT},
            "user": {"id": "u-1", "user_id": "u-1", "email": "u@example.test", "is_internal_admin": internal_admin},
            "membership": {"user_id": "u-1", "role": role, "status": "active"}}


@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ohvis_harness as api_module
    from app.auth import get_current_tenant_context

    app = FastAPI()
    app.include_router(api_module.router)
    holder = {"ctx": _context("member")}
    app.dependency_overrides[get_current_tenant_context] = lambda: holder["ctx"]
    mocks = {}
    for name in ("validate_stored_skill", "promote_skill_version", "rollback_skill_version",
                 "disable_skill", "list_skill_manifests", "get_skill_detail"):
        mocks[name] = AsyncMock(return_value={"ok": True} if name != "list_skill_manifests" else [])
        monkeypatch.setattr(api_module, name, mocks[name])
    client = TestClient(app)
    return type("Api", (), {"app": app, "client": client, "holder": holder, "mocks": mocks})


_PROMOTE_BODY = {"idempotency_key": "idem-12345", "evidence": [{"k": "v"}], "focused_results": {},
                 "affected_regressions": {}, "candidate_metrics": {}, "active_metrics": {}}
_ROLLBACK_BODY = {"idempotency_key": "idem-12345", "reason": "r", "evidence": [{"k": "v"}]}
_ELEVATED_CALLS = (
    ("post", f"/ohvis/harness/skills/{SKILL_ID}/versions/1.0.0/validate", None, "validate_stored_skill"),
    ("post", f"/ohvis/harness/skills/{SKILL_ID}/versions/1.0.0/promote", _PROMOTE_BODY, "promote_skill_version"),
    ("post", f"/ohvis/harness/skills/{SKILL_ID}/rollback", _ROLLBACK_BODY, "rollback_skill_version"),
    ("delete", f"/ohvis/harness/skills/{SKILL_ID}", None, "disable_skill"),
)


@pytest.mark.parametrize("role", ["viewer", "member"])
@pytest.mark.parametrize("method,path,body,service", _ELEVATED_CALLS)
def test_non_ceo_gets_403_on_validate_promote_rollback_disable(api, role, method, path, body, service):
    api.holder["ctx"] = _context(role)
    response = getattr(api.client, method)(path, **({"json": body} if body is not None else {}))
    assert response.status_code == 403
    api.mocks[service].assert_not_awaited()


@pytest.mark.parametrize("method,path,body,service", _ELEVATED_CALLS)
def test_admin_owner_and_internal_admin_pass_the_elevated_gate(api, method, path, body, service):
    for ctx in (_context("admin"), _context("owner"), _context("member", internal_admin=True)):
        api.holder["ctx"] = ctx
        response = getattr(api.client, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code == 200, (ctx["membership"], response.text)
    assert api.mocks[service].await_count == 3


def test_viewer_can_still_read_list_and_detail(api):
    api.holder["ctx"] = _context("viewer")
    api.mocks["list_skill_manifests"].return_value = [{"slug": "alpha"}]
    response = api.client.get("/ohvis/harness/skills", params={
        "project": "AADS", "risk_tier": "read", "enabled": "true", "source": "file", "q": "수집", "status": "active"})
    assert response.status_code == 200 and response.json() == {"items": [{"slug": "alpha"}]}
    kwargs = api.mocks["list_skill_manifests"].await_args.kwargs
    assert kwargs == {"tenant_id": TENANT, "slug": None, "status": "active", "project": "AADS",
                      "risk_tier": "read", "enabled": True, "source": "file", "q": "수집"}
    assert api.client.get(f"/ohvis/harness/skills/{SKILL_ID}").status_code == 200
    api.mocks["get_skill_detail"].assert_awaited_once_with(tenant_id=TENANT, skill_id=SKILL_ID)


def test_list_rejects_bad_source_and_maps_registry_errors(api):
    assert api.client.get("/ohvis/harness/skills", params={"source": "cloud"}).status_code == 422
    api.mocks["get_skill_detail"].side_effect = harness.SkillRegistryError("skill_not_found", status_code=404)
    response = api.client.get(f"/ohvis/harness/skills/{SKILL_ID}")
    assert response.status_code == 404 and response.json()["detail"]["code"] == "skill_not_found"


def test_openapi_exposes_detail_route_and_list_filters(api):
    schema = api.app.openapi()
    assert "get" in schema["paths"]["/ohvis/harness/skills/{skill_id}"]
    params = {p["name"] for p in schema["paths"]["/ohvis/harness/skills"]["get"]["parameters"]}
    assert {"project", "risk_tier", "enabled", "source", "q", "slug", "status"} <= params
