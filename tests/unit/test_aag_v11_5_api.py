from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services import aag_query_v2, aag_tools
from scripts.aag_snapshot_push import _v2_statement


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
        "generated_at": datetime(2026, 9, 19, 12, tzinfo=UTC),
        "verified_at": datetime(2026, 9, 19, 12, 1, tzinfo=UTC),
        "first_published_at": datetime(2026, 9, 19, 12, tzinfo=UTC),
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
