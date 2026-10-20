from __future__ import annotations

from datetime import UTC, datetime
import inspect
from uuid import uuid4

import pytest

from app.api import aag as aag_api
from app.services import aag_query_v2, aag_tools
from app.services.aag_governance import ScannerPrincipal
from scripts.aag_snapshot_push import _v2_statement


@pytest.mark.asyncio
async def test_authoritative_read_joins_scan_run_for_analyzer_metadata(monkeypatch):
    """Snapshot content is deduplicated; scanner identity belongs to the run."""
    statements = []

    class Connection:
        async def fetchrow(self, statement, *_args):
            statements.append(statement)
            return None

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    monkeypatch.setattr(aag_query_v2, "get_pool", lambda: Pool())
    assert await aag_query_v2.load_authoritative_snapshot(project="AADS") is None

    # Both session tools and the public latest endpoint must use the run that
    # published the pointer, not fields absent from the immutable graph table.
    api_source = inspect.getsource(aag_api.get_latest_snapshot_v2)
    for source in (statements[0], api_source):
        assert "JOIN aag_scan_runs r" in source
        assert "r.id=p.run_id" in source
        assert "r.resolved_commit_sha=p.resolved_commit_sha" in source
        for field in ("scanner_version", "ruleset_digest", "scan_scope_digest"):
            assert f"r.{field}" in source
            assert f"s.{field}" not in source


def _snapshot(snapshot_id=None):
    return {
        "snapshot_id": snapshot_id or uuid4(),
        "observation_id": uuid4(),
        "run_id": uuid4(),
        "project": "AADS",
        "repository_id": "aads-server",
        "target_ref": "refs/heads/main",
        "governance_scope": "default",
        "resolved_commit_sha": "1" * 40,
        "expected_target_ref_head_sha": "1" * 40,
        "generated_at": datetime.now(UTC),
        "verified_at": datetime.now(UTC),
        "first_published_at": datetime.now(UTC),
        "content_fingerprint": "a" * 64,
        "scanner_version": "aag-v1.1",
        "ruleset_digest": "b" * 64,
        "scan_scope_digest": "c" * 64,
        "canonicalization_version": "aag-c14n-v1",
        "stable_key_version": "aag-stable-key-v1",
        "stats": {}, "nodes": [], "edges": [], "unresolved": [],
        "node_count": 0, "edge_count": 0, "finding_count": 3,
        "findings": [
            {"rule": "Z_RULE", "severity": "P2", "path": "z.py", "stable_finding_key": "z"},
            {"rule": "A_RULE", "severity": "P0", "path": "b.py", "stable_finding_key": "b"},
            {"rule": "A_RULE", "severity": "P0", "path": "a.py", "stable_finding_key": "a"},
        ],
    }


@pytest.mark.asyncio
async def test_findings_pagination_is_deterministic_and_snapshot_pinned(monkeypatch):
    pinned = uuid4()
    seen = []

    async def fake_load(**kwargs):
        seen.append(kwargs.get("snapshot_id"))
        return _snapshot(pinned)

    monkeypatch.setattr(aag_query_v2, "load_authoritative_snapshot", fake_load)

    first = await aag_query_v2.query_findings_page(project="AADS", page_size=2)
    second = await aag_query_v2.query_findings_page(
        project="AADS", page_size=2, cursor=first["pagination"]["next_cursor"],
    )

    assert [item["stable_finding_key"] for item in first["findings"]] == ["a", "b"]
    assert [item["stable_finding_key"] for item in second["findings"]] == ["z"]
    assert first["snapshot_id"] == second["snapshot_id"] == str(pinned)
    assert seen == [None, str(pinned)]


@pytest.mark.asyncio
async def test_cursor_rejects_filter_change(monkeypatch):
    async def fake_load(**kwargs):
        return _snapshot()

    monkeypatch.setattr(aag_query_v2, "load_authoritative_snapshot", fake_load)
    first = await aag_query_v2.query_findings_page(project="AADS", page_size=1)

    with pytest.raises(aag_query_v2.AAGQueryError, match="does not match filters"):
        await aag_query_v2.query_findings_page(
            project="AADS", page_size=1,
            cursor=first["pagination"]["next_cursor"], severity="P0",
        )


@pytest.mark.asyncio
async def test_session_findings_v2_flag_and_v1_rollback(monkeypatch):
    events = []

    async def fake_record(**kwargs):
        events.append(kwargs)

    async def fake_page(**kwargs):
        snap = _snapshot()
        return {
            "ref": {key: snap[key] for key in ("project", "repository_id", "target_ref", "governance_scope")},
            "snapshot_id": str(snap["snapshot_id"]),
            "commit_sha": snap["resolved_commit_sha"],
            "generated_at": snap["generated_at"], "verified_at": snap["verified_at"],
            "findings": snap["findings"][:1],
            "pagination": {"pinned": True, "total": 3},
        }

    monkeypatch.setattr(aag_tools, "record_consumer_event", fake_record)
    monkeypatch.setattr(aag_tools, "query_findings_page", fake_page)
    monkeypatch.setenv("AAG_V2_CONSUMERS_ENABLED", "1")
    v2 = await aag_tools.get_findings_data("AADS")
    assert v2["source"] == "central_db_v2"
    assert v2["authoritative"] is True
    assert events[-1]["api_version"] == "v2"

    async def fake_legacy(project):
        return {
            "project": project, "generated_at": datetime.now(UTC),
            "findings": [], "unresolved": [], "stats": {},
        }

    monkeypatch.setenv("AAG_V2_CONSUMERS_ENABLED", "0")
    monkeypatch.setattr(aag_tools, "latest_snapshot", fake_legacy)
    legacy = await aag_tools.get_findings_data("AADS")
    assert legacy["source"] == "database"
    assert events[-1]["api_version"] == "v1"


def test_v11_5_migration_is_additive_and_preserves_v1():
    sql = (
        aag_tools.REPO_ROOT / "migrations/20260919_aag_v1_1_api_consumer_telemetry.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS aag_api_consumer_events" in sql
    assert "DROP TABLE" not in sql
    assert "ALTER TABLE aag_graph_snapshots" not in sql

    pointer_sql = (
        aag_tools.REPO_ROOT / "migrations/20260919_aag_v1_1_pointer_cutover.sql"
    ).read_text(encoding="utf-8")
    assert "INSERT INTO aag_latest_pointers" in pointer_sql
    assert "INSERT INTO aag_ref_heads" in pointer_sql
    assert "o.authoritative=TRUE" in pointer_sql
    assert "o.verification_status='verified'" in pointer_sql
    assert "DROP TABLE" not in pointer_sql


def test_hourly_pusher_maintains_atomic_authoritative_pointer():
    graph = {
        "generated_at": "2026-09-19T23:00:00+09:00",
        "source_identity": {
            "repository_id": "kis-autotrade-v4",
            "target_ref": "refs/heads/main",
            "resolved_commit_sha": "1" * 40,
            "expected_target_ref_head_sha": "1" * 40,
            "scanner_version": "aag-scanner-v1.1",
            "ruleset_digest": "a" * 64,
            "scan_scope_digest": "b" * 64,
            "normalization_version": "aag-c14n-v1",
            "stable_key_version": "aag-stable-key-v1",
            "parser_versions": {"python_ast": "3.12"},
        },
        "stats": {}, "nodes": [], "edges": [], "findings": [], "unresolved": [],
    }

    sql = _v2_statement("GO100", graph)

    assert sql is not None
    assert "pg_advisory_xact_lock" in sql
    assert "INSERT INTO aag_latest_pointers" in sql
    assert "INSERT INTO aag_ref_heads" in sql
    assert "THEN 'out_of_order'" in sql
    assert "WHERE EXCLUDED.generated_at >= aag_latest_pointers.generated_at" in sql
    assert "SELECT COUNT(*) AS pointers_published FROM new_pointer" in sql
    assert "FROM aag_snapshot_observations o" in sql
    assert "WHERE r.id=" in sql


def test_release_enables_v2_with_environment_rollback_switches():
    compose = (aag_tools.REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert compose.count("AAG_V2_ENABLED=${AAG_V2_ENABLED:-true}") == 2
    assert compose.count("AAG_V2_CONSUMERS_ENABLED=${AAG_V2_CONSUMERS_ENABLED:-true}") == 2


@pytest.mark.asyncio
async def test_runner_brief_uses_project_scoped_credential_and_pinned_snapshot(monkeypatch):
    snap = _snapshot()
    events = []

    async def fake_auth(token, project):
        assert token == "project-scoped-secret"
        assert project == "AADS"
        return ScannerPrincipal(uuid4(), project, "scanner:test")

    async def fake_load(**kwargs):
        assert kwargs["project"] == "AADS"
        return snap

    async def fake_record(**kwargs):
        events.append(kwargs)

    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "authenticate_scanner", fake_auth)
    monkeypatch.setattr(aag_api, "load_authoritative_snapshot", fake_load)
    monkeypatch.setattr(aag_api, "record_consumer_event", fake_record)

    result = await aag_api.get_runner_brief_v2(
        body=aag_api.RunnerBriefRequest(project="aads", target="a.py"),
        x_aag_scanner_token="project-scoped-secret",
    )

    assert result["fallback_used"] is False
    assert result["brief"]["snapshot"]["snapshot_id"] == str(snap["snapshot_id"])
    assert result["brief"]["state"] == "detected"
    assert events[-1]["consumer"].startswith("pipeline-runner:")
    assert events[-1]["snapshot_id"] == str(snap["snapshot_id"])


def test_pipeline_runner_v2_cutover_is_explicit_and_fail_closed():
    runner = (aag_tools.REPO_ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")
    assert 'AAG_V2_RUNNER_ENABLED:-0' in runner
    assert 'AAG_SCANNER_TOKEN_${project^^}' in runner
    assert 'aag_token_file_var="${aag_token_var}_FILE"' in runner
    assert 'IFS= read -r aag_scanner_token < "$aag_scanner_token_file"' in runner
    assert '/aag/v2/runner-brief' in runner
    assert 'reason=project_credential_missing' in runner
    assert 'never silently presents a' in runner
    assert 'AAG_V2_RUNNER_ENABLED=0' in runner
    assert "umask 077" in runner
    assert '-H "X-AAG-Scanner-Token: ${aag_scanner_token}"' not in runner


# --- V11-5 consumer finish: provenance, freshness and non-absence guarantees ---

from datetime import timedelta  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from app.services import aag_brief_v2  # noqa: E402

_RUNNER = (aag_tools.REPO_ROOT / "scripts/pipeline-runner.sh")


def _runner_call(monkeypatch, snap, target):
    async def fake_auth(token, project):
        return ScannerPrincipal(uuid4(), project, "scanner:test")

    async def fake_load(**_kwargs):
        return snap

    async def fake_record(**_kwargs):
        return None

    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "authenticate_scanner", fake_auth)
    monkeypatch.setattr(aag_api, "load_authoritative_snapshot", fake_load)
    monkeypatch.setattr(aag_api, "record_consumer_event", fake_record)
    return aag_api.get_runner_brief_v2(
        body=aag_api.RunnerBriefRequest(project="aads", target=target),
        x_aag_scanner_token="t",
    )


@pytest.mark.parametrize(("target", "paths", "expect_hit"), [
    ("a.py", ["a.py"], True),
    ("app/api/aag.py", ["app/api/aag.py"], True),
    ("aag.py", ["app/api/aag.py"], True),
    ("app/api", ["app/api/aag.py"], True),
    ("app/api/", ["app/api/aag.py"], True),
    ("aag.py", ["app/api/data_aag.py"], False),
    ("aag.py", ["app/api/aag.pyc"], False),
    ("app/ap", ["app/api/aag.py"], False),
    ("../../etc", ["app/api/aag.py"], False),
    ("", ["app/api/aag.py"], False),
    ("app/api/aag.py를 수정하고 tests/unit/x.py도 본다", ["app/api/aag.py"], True),
    ("아무 파일도 언급하지 않는 지시", ["app/api/aag.py"], False),
])
def test_target_matching_is_segment_aware(target, paths, expect_hit):
    findings = [{"rule": "R", "severity": "P1", "file": path} for path in paths]
    assert bool(aag_brief_v2.match_findings(findings, target)) is expect_hit


def test_target_matching_scales_on_adversarial_instruction():
    hostile = ("a." * 12_000) + ("/" * 5_000) + ("x" * 20_000)
    started = time.monotonic()
    aag_brief_v2.match_findings([{"file": "app/x.py"}], hostile)
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_runner_brief_matches_files_named_inside_an_instruction(monkeypatch):
    """A runner sends the whole job text; only the paths it names may select findings."""
    snap = _snapshot()
    snap["findings"] = [
        {"rule": "ROUTE_MISSING", "severity": "P0", "file": "app/api/aag.py", "detail": "x"},
        {"rule": "OTHER", "severity": "P1", "file": "app/api/other.py"},
    ]
    result = await _runner_call(monkeypatch, snap, "app/api/aag.py 의 라우트를 고쳐라. tests/unit/t.py 도 갱신.")
    brief = result["brief"]
    assert [f["file"] for f in brief["findings"]] == ["app/api/aag.py"]
    assert brief["state"] == "detected"
    assert "ROUTE_MISSING" in result["brief_text"]
    assert result["brief_text"].splitlines()[0].startswith("- ")
    assert len(brief["target"]) <= 500


@pytest.mark.asyncio
@pytest.mark.parametrize(("mutation", "expected_state"), [
    ({"verified_at": datetime.now(UTC) - timedelta(minutes=181)}, "stale"),
    ({"verified_at": None}, "stale"),
    ({"resolved_commit_sha": "2" * 40}, "mismatch"),
    ({"expected_target_ref_head_sha": ""}, "mismatch"),
])
async def test_runner_brief_never_reports_stale_or_mismatched_snapshot_as_clean(
    monkeypatch, mutation, expected_state,
):
    snap = {**_snapshot(), **mutation, "findings": []}
    result = await _runner_call(monkeypatch, snap, "z/unrelated.py")
    brief = result["brief"]
    assert brief["state"] == expected_state
    assert brief["negative_assertion_allowed"] is False
    assert brief["conclusion"] == "not_proven"
    assert brief["required_actions"] and brief["miss_review_required"] is True
    assert "부재/무영향 단정 금지" in result["brief_text"]


@pytest.mark.asyncio
async def test_runner_brief_coverage_not_proven_is_called_out(monkeypatch):
    """Inventory 0/0 (the live GO100 state) must read as unproven, not as a pass."""
    snap = {**_snapshot(), "findings": [], "unresolved": [{"kind": "dynamic"}],
            "stats": {"coverage": {"inventory_scanned": 0, "inventory_total": 0}}}
    result = await _runner_call(monkeypatch, snap, "z/unrelated.py")
    brief = result["brief"]
    assert brief["state"] == "partial"
    assert brief["coverage"]["inventory_ratio"] == 0.0
    assert "close inventory coverage gaps" in brief["required_actions"]
    assert "inventory 0/0" in result["brief_text"]


def test_freshness_and_limitation_helpers_do_not_hardcode_fresh():
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    assert aag_brief_v2.snapshot_freshness(now - timedelta(minutes=10), now)["status"] == "fresh"
    assert aag_brief_v2.snapshot_freshness(now - timedelta(minutes=181), now)["status"] == "stale"
    assert aag_brief_v2.snapshot_freshness(None, now)["status"] == "unknown"
    assert aag_brief_v2.snapshot_freshness("2026-10-20T11:59:00", now)["age_minutes"] == 1

    clean = {"source": "central_db", "authoritative": True, "verified_at": now,
             "resolved_commit_sha": "1" * 40, "expected_target_ref_head_sha": "1" * 40,
             "stats": {"coverage": {"inventory_scanned": 5, "inventory_total": 5,
                                     "risk_scanned": 2, "risk_total": 2}}}
    assert aag_brief_v2.snapshot_limitations(clean, now) == []
    assert aag_brief_v2.snapshot_limitations({**clean, "stats": {}}, now)
    assert any("stale" in x or "refresh" in x for x in aag_brief_v2.snapshot_limitations(
        {**clean, "verified_at": now - timedelta(hours=5)}, now))
    assert aag_brief_v2.snapshot_limitations({**clean, "unresolved": [{"k": 1}]}, now)


@pytest.mark.asyncio
async def test_latest_endpoint_reports_stale_and_unproven_coverage(monkeypatch):
    snap = {**_snapshot(), "verified_at": datetime.now(UTC) - timedelta(hours=7)}

    class Connection:
        async def fetchrow(self, *_args):
            return snap

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "get_pool", lambda: Pool())
    monkeypatch.setattr(aag_api, "require_project_role", allow)
    result = await aag_api.get_latest_snapshot_v2(
        project="aads", repository_id="aads-server", target_ref="refs/heads/main",
        user={"user_id": "viewer"},
    )
    assert result["freshness"]["status"] == "stale"
    assert result["freshness"]["age_minutes"] >= 420
    assert result["limitation"], "stale snapshot with no coverage must carry limitations"


@pytest.mark.asyncio
async def test_full_pagination_returns_every_finding_exactly_once(monkeypatch):
    pinned = uuid4()
    snap = _snapshot(pinned)
    snap["findings"] = [
        {"rule": "R", "severity": "P2", "path": f"m{idx:04d}.py", "stable_finding_key": f"k{idx:04d}"}
        for idx in range(450)
    ]
    loads = []

    async def fake_load(**kwargs):
        loads.append(kwargs.get("snapshot_id"))
        return snap

    monkeypatch.setattr(aag_query_v2, "load_authoritative_snapshot", fake_load)
    seen, cursor = [], None
    while True:
        page = await aag_query_v2.query_findings_page(project="AADS", page_size=500, cursor=cursor)
        assert page["pagination"]["page_size"] == 200
        assert page["snapshot_id"] == str(pinned)
        seen += [item["stable_finding_key"] for item in page["findings"]]
        cursor = page["pagination"]["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 450
    assert loads == [None, str(pinned), str(pinned)]
    assert page["freshness"]["status"] == "fresh"
    assert page["limitation"], "empty coverage must surface as a limitation on every page"


@pytest.mark.asyncio
async def test_pagination_fails_closed_when_pinned_snapshot_is_superseded(monkeypatch):
    calls = []

    async def fake_load(**kwargs):
        calls.append(kwargs.get("snapshot_id"))
        return _snapshot() if kwargs.get("snapshot_id") is None else None

    monkeypatch.setattr(aag_query_v2, "load_authoritative_snapshot", fake_load)
    first = await aag_query_v2.query_findings_page(project="AADS", page_size=1)
    with pytest.raises(aag_query_v2.AAGQueryError, match="restart pagination"):
        await aag_query_v2.query_findings_page(
            project="AADS", page_size=1, cursor=first["pagination"]["next_cursor"],
        )


@pytest.mark.asyncio
async def test_cursor_from_another_snapshot_or_ref_is_rejected(monkeypatch):
    other = uuid4()

    async def fake_load(**kwargs):
        return _snapshot(uuid4())

    monkeypatch.setattr(aag_query_v2, "load_authoritative_snapshot", fake_load)
    first = await aag_query_v2.query_findings_page(project="AADS", page_size=1)
    with pytest.raises(aag_query_v2.AAGQueryError, match="does not match snapshot_id"):
        await aag_query_v2.query_findings_page(
            project="AADS", page_size=1, snapshot_id=str(other),
            cursor=first["pagination"]["next_cursor"],
        )
    with pytest.raises(aag_query_v2.AAGQueryError, match="invalid pagination cursor"):
        await aag_query_v2.query_findings_page(project="AADS", cursor="not-a-cursor")


@pytest.mark.asyncio
async def test_session_findings_empty_page_is_not_an_all_clear(monkeypatch):
    async def fake_record(**_kwargs):
        return None

    async def fake_page(**_kwargs):
        snap = _snapshot()
        return {
            "ref": {key: snap[key] for key in ("project", "repository_id", "target_ref", "governance_scope")},
            "snapshot_id": str(snap["snapshot_id"]), "commit_sha": snap["resolved_commit_sha"],
            "generated_at": snap["generated_at"], "verified_at": snap["verified_at"],
            "findings": [], "pagination": {"pinned": True, "total": 0},
            "freshness": {"status": "fresh", "age_minutes": 1},
            "limitation": ["close inventory coverage gaps"],
        }

    monkeypatch.setattr(aag_tools, "record_consumer_event", fake_record)
    monkeypatch.setattr(aag_tools, "query_findings_page", fake_page)
    monkeypatch.setenv("AAG_V2_CONSUMERS_ENABLED", "1")
    text = await aag_tools.aag_findings_text(project="AADS", rule="NOPE")
    assert "proof=not_proven" in text
    assert "freshness=fresh" in text
    assert "무결함의 증거가 아님" in text
    assert "close inventory coverage gaps" in text
    assert "스냅샷에 결함이 없습니다." not in text


@pytest.mark.asyncio
async def test_session_brief_v2_uses_pinned_identity_and_flag_off_rolls_back(monkeypatch):
    snap = {**_snapshot(), "verified_at": datetime.now(UTC) - timedelta(hours=9)}
    events = []

    async def fake_load(**_kwargs):
        return snap

    async def fake_record(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(aag_tools, "load_authoritative_snapshot", fake_load)
    monkeypatch.setattr(aag_tools, "record_consumer_event", fake_record)
    monkeypatch.setenv("AAG_V2_CONSUMERS_ENABLED", "1")
    brief = json.loads(await aag_tools.aag_brief_text("AADS", "z.py를 수정한다"))
    assert brief["state"] == "stale"
    assert brief["negative_assertion_allowed"] is False
    assert brief["snapshot"]["snapshot_id"] == str(snap["snapshot_id"])
    assert events[-1]["api_version"] == "v2" and events[-1]["outcome"] == "success"

    monkeypatch.setenv("AAG_V2_CONSUMERS_ENABLED", "0")
    seen = []

    async def fake_to_thread(fn, *args, **kwargs):
        seen.append(args[0][1])
        return subprocess.CompletedProcess(args[0], 0, stdout="legacy-brief", stderr="")

    monkeypatch.setattr(aag_tools.asyncio, "to_thread", fake_to_thread)
    assert await aag_tools.aag_brief_text("AADS", "z.py를 수정한다") == "legacy-brief"
    assert seen[0].endswith("tools/aag/brief.py")
    assert events[-1]["api_version"] == "v1"


def test_pipeline_runner_forwards_only_server_rendered_non_fallback_brief():
    runner = _RUNNER.read_text(encoding="utf-8")
    match = re.search(r"python3 -c '(import json,sys; d=json\.load\(sys\.stdin\);[^']*)'", runner)
    assert match, "runner must extract brief_text from the v2 response"
    code = match.group(1)

    def run(payload):
        return subprocess.run(
            ["python3", "-c", code], input=payload, capture_output=True, text=True, check=False,
        )

    ok = run(json.dumps({"brief_text": "- 상태: detected", "fallback_used": False}))
    assert ok.stdout == "- 상태: detected" and ok.returncode == 0
    assert run(json.dumps({"brief_text": "- x", "fallback_used": True})).stdout == ""
    assert run(json.dumps({"brief_text": "- x"})).stdout == ""
    assert run("<html>502</html>").stdout == ""
    assert 'aag_skip_reason="project_credential_missing"' in runner
    assert 'aag_skip_reason="central_v2_unavailable_or_no_snapshot"' in runner
    assert '\\"skipped_reason\\":\\"${aag_skip_reason}\\"' in runner
    # the v1 renderer is reachable only through the explicit rollback branch
    v2_block = runner[runner.index('if [[ "$aag_v2_enabled" =~'):runner.index("        else\n            if [[ -f \"$main_workdir/tools/aag/brief.py\"")]
    assert "brief.py" not in v2_block and "aag-brief.py" not in v2_block


@pytest.mark.parametrize("target", [
    "app/",
    "./app/api/aag.py",
    "././app/api/",
    "/root/aads/aads-server/app/api/aag.py",
    "/home/other/checkout/aads-server/app/api/aag.py",
    "TARGET: /root/aads/aads-server/app/ 를 수정한다",
    str(aag_brief_v2.REPO_ROOT / "app" / "api" / "aag.py"),
    "app\\api\\aag.py",
])
def test_target_forms_reach_existing_relative_findings(target):
    findings = [{"rule": "R", "severity": "P1", "file": "app/api/aag.py"}]
    assert aag_brief_v2.match_findings(findings, target, "aads-server") == findings


@pytest.mark.parametrize("target", [
    "/root/aads/aads-dashboard/app/api/aag.py",
    "/etc/app/api/aag.py",
    "../app/api/aag.py",
    "app/../../etc/passwd",
    "/root/aads/aads-server",
    "/root/aads/aads-server/",
    "./",
    "/root/aads/aads-server/../aads-dashboard/x/y.py",
    "api/aag.pyc",
    "xapp/",
    "app",
])
def test_target_forms_do_not_widen_beyond_the_repository(target):
    findings = [{"rule": "R", "severity": "P1", "file": "app/api/aag.py"}]
    assert aag_brief_v2.match_findings(findings, target, "aads-server") == []


def test_absolute_target_without_repo_identity_is_not_reinterpreted():
    findings = [{"rule": "R", "severity": "P1", "file": "app/api/aag.py"}]
    assert aag_brief_v2.match_findings(findings, "/root/aads/aads-server/app/api/aag.py") == []
    assert aag_brief_v2.target_tokens("/root/aads/aads-server/app/api/aag.py", "aads-server") == [
        "app/api/aag.py",
    ]


def test_route_identity_in_an_instruction_keeps_matching_route_findings():
    findings = [{"rule": "ROUTE_MISSING", "severity": "P0", "key": "GET /api/v1/ops/runner-host-policy"}]
    assert aag_brief_v2.match_findings(findings, "/api/v1/ops/runner-host-policy 를 구현", "aads-server")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["app/", "./app/api/aag.py", "/root/aads/aads-server/app/api/aag.py"])
async def test_runner_brief_finds_findings_for_directory_dotted_and_absolute_targets(monkeypatch, target):
    snap = _snapshot()
    snap["findings"] = [
        {"rule": "ROUTE_MISSING", "severity": "P0", "file": "app/api/aag.py", "detail": "x"},
        {"rule": "OTHER", "severity": "P1", "file": "scripts/other.py"},
    ]
    brief = (await _runner_call(monkeypatch, snap, target))["brief"]
    assert brief["state"] == "detected"
    assert [f["file"] for f in brief["findings"]][0] == "app/api/aag.py"


def test_brief_text_keeps_p0_p1_behind_thirty_p2_and_names_what_was_omitted():
    findings = [{"rule": "P2_RULE", "severity": "P2", "path": f"p2_{i:02}.py"} for i in range(30)]
    findings += [
        {"rule": "P1_RULE", "severity": "P1", "path": "late_p1.py"},
        {"rule": "P0_RULE", "severity": "P0", "path": "late_p0.py"},
        {"rule": "P2_RULE", "severity": "P2", "path": "p2_extra.py"},
    ]
    sid = "snap-123"
    brief = {
        "state": "detected", "conclusion": "findings_present",
        "snapshot": {"project": "AADS", "repository_id": "aads-server", "target_ref": "refs/heads/main",
                     "resolved_commit_sha": "a" * 40, "snapshot_id": sid},
        "coverage": {}, "freshness": {"age_minutes": 3}, "findings": findings,
    }
    text = aag_brief_v2.render_brief_text(brief)
    lines = text.splitlines()
    finding_lines = [line for line in lines if line.startswith("- [")]
    assert len(finding_lines) == aag_brief_v2.MAX_BRIEF_TEXT_FINDINGS
    assert "late_p0.py" in finding_lines[0] and "late_p1.py" in finding_lines[1]
    assert "p2_extra.py" not in text
    omitted = lines[-1]
    assert omitted.startswith("- 외 3건 생략") and "P2 3건" in omitted
    assert sid in omitted and "a" * 12 in omitted and "refs/heads/main" in omitted
    assert [f["path"] for f in brief["findings"]][-1] == "p2_extra.py", "input must not be mutated"


def test_brief_text_reports_omitted_p0_p1_counts_when_they_alone_exceed_the_cap():
    findings = [{"rule": "R", "severity": "P0" if i < 20 else "P1", "path": f"f{i}.py"} for i in range(45)]
    brief = {"state": "detected", "conclusion": "findings_present",
             "snapshot": {"snapshot_id": "s", "resolved_commit_sha": "b" * 40}, "findings": findings}
    omitted = aag_brief_v2.render_brief_text(brief).splitlines()[-1]
    assert "P1 15건" in omitted and "P0/P1 포함" in omitted
