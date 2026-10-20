from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from scripts import aag_restore_verify as rv

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def source(tmp_path: Path):
    conn = rv.connect(tmp_path / "source.sqlite")
    rv.build_fixture(conn)
    yield conn
    conn.close()


@pytest.fixture()
def bundle(source, tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    rv.backup(source, path)
    return path


def test_backup_writes_real_files_with_matching_checksums(bundle: Path):
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["schema"] == rv.BUNDLE_SCHEMA
    assert set(manifest["tables"]) == set(rv.TABLES)
    for table, meta in manifest["tables"].items():
        raw = (bundle / meta["file"]).read_bytes()
        assert rv._sha256(raw) == meta["sha256"], table
        assert len(raw.splitlines()) == meta["rows"], table
    assert manifest["tables"]["aag_audit_events"]["rows"] == 3


def test_round_trip_restores_and_every_cross_check_agrees(bundle: Path, source, tmp_path: Path):
    restored = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        assert rv.verify_integrity(restored) == []
        for table in rv.TABLES:
            before = source.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            after = restored.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert before == after, table
        # failed / rejected runs survive restore and never became pointers
        results = {r["id"]: r["result"] for r in restored.execute("SELECT id, result FROM aag_scan_runs")}
        assert results["run-go-4"] == "source_behind"
        assert results["run-go-5"] == "ingest_failed"
        pointer = restored.execute(
            "SELECT run_id FROM aag_latest_pointers WHERE project='GO100'").fetchone()
        assert pointer["run_id"] == "run-go-3"
    finally:
        restored.close()


def test_restore_refuses_existing_destination(bundle: Path, tmp_path: Path):
    dest = tmp_path / "already.sqlite"
    dest.write_bytes(b"live data")
    with pytest.raises(rv.BundleError) as exc:
        rv.restore(bundle, dest)
    assert exc.value.code == "destination_exists"
    assert dest.read_bytes() == b"live data"


def test_restored_schema_keeps_audit_append_only_and_ready_immutable(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE aag_audit_events SET reason='edited'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM aag_audit_events")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE aag_graph_snapshots_v2 SET finding_count=99")
    finally:
        conn.close()


@pytest.mark.parametrize(("name", "expected", "mutate"), rv.CORRUPTIONS, ids=[c[0] for c in rv.CORRUPTIONS])
def test_corrupt_bundle_is_rejected_with_specific_code(name, expected, mutate, bundle: Path, tmp_path: Path):
    copy = tmp_path / f"corrupt-{name}"
    shutil.copytree(bundle, copy)
    mutate(copy)
    dest = tmp_path / "out.sqlite"
    outcome = rv.restore_and_verify(copy, dest)
    assert outcome["ok"] is False
    assert expected in outcome["codes"], outcome
    assert not dest.exists(), "a rejected restore must not leave a usable database behind"


def test_every_corruption_code_is_distinct_enough_to_diagnose():
    expected = [c[1] for c in rv.CORRUPTIONS]
    assert {"checksum_mismatch", "audit_chain_mismatch", "snapshot_content_fingerprint",
            "stable_key_mismatch", "pointer_not_authoritative"} <= set(expected)


def test_pointer_rebuild_equals_original_identity(source, bundle: Path, tmp_path: Path):
    originals = {r["project"]: dict(r) for r in source.execute("SELECT * FROM aag_latest_pointers")}
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        rebuilt = rv.rebuild_pointers(conn)
        conn.execute("DELETE FROM aag_latest_pointers")
        conn.execute("DELETE FROM aag_ref_heads")
        rv.apply_rebuilt_pointers(conn, rebuilt)
        assert {p["project"] for p in rebuilt} == set(originals)
        for pointer in rebuilt:
            original = originals[pointer["project"]]
            for column in ("snapshot_id", "observation_id", "run_id", "resolved_commit_sha", "verified_at"):
                assert pointer[column] == original[column], column
        assert rv.verify_integrity(conn) == []
    finally:
        conn.close()


def test_pointer_rebuild_never_selects_source_behind_or_failed_runs(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        runs = {p["run_id"] for p in rv.rebuild_pointers(conn)}
        assert "run-go-4" not in runs
        assert "run-go-5" not in runs
    finally:
        conn.close()


def test_rebuild_without_ref_head_degrades_generated_at_and_says_so(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        conn.execute("DELETE FROM aag_latest_pointers")
        conn.execute("DELETE FROM aag_ref_heads")
        rebuilt = {p["project"]: p for p in rv.rebuild_pointers(conn)}
        assert rebuilt["GO100"]["generated_at_source"] == "snapshot_first_seen"
        assert rebuilt["GO100"]["generated_at"] == rv.ts(rv.BASE + timedelta(minutes=120))
    finally:
        conn.close()


def test_rebuild_of_republished_snapshot_uses_lower_bound_generated_at(tmp_path: Path):
    conn = rv.connect(tmp_path / "s.sqlite")
    rv.create_schema(conn)
    sha = "1" * 40
    graph = rv.fixture_graph("GO100", 1)
    rv.ingest_artifact(conn, graph=graph, **rv.GO100, resolved_commit_sha=sha, expected_head_sha=sha,
                       generated_at=rv.BASE, now=rv.BASE, run_id="r1")
    rv.ingest_artifact(conn, graph=graph, **rv.GO100, resolved_commit_sha=sha, expected_head_sha=sha,
                       generated_at=rv.BASE + timedelta(minutes=60),
                       now=rv.BASE + timedelta(minutes=61), run_id="r2")
    live = conn.execute("SELECT generated_at FROM aag_latest_pointers").fetchone()[0]
    conn.execute("DELETE FROM aag_ref_heads WHERE 1=1")
    conn.execute("DELETE FROM aag_latest_pointers")
    rebuilt = rv.rebuild_pointers(conn)[0]
    assert live == rv.ts(rv.BASE + timedelta(minutes=60))
    assert rebuilt["generated_at"] == rv.ts(rv.BASE)
    assert rebuilt["generated_at_source"] == "snapshot_first_seen"
    assert rebuilt["observation_id"] == "obs-r2" and rebuilt["verified_at"] == rv.ts(rv.BASE + timedelta(minutes=61))
    conn.close()


def test_artifact_reprocess_same_content_converges_without_new_snapshot(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        out = rv.reprocess_artifact(
            conn, rv.fixture_graph("GO100", 2), rv.GO100, commit_sha="2" * 40,
            generated_at=rv.BASE + timedelta(minutes=120), now=rv.BASE + timedelta(minutes=300),
            run_id="run-again")
        assert out["outcome"]["result"] == "no_change_success"
        assert out["outcome"]["created_snapshot"] is False
        assert out["row_delta"]["aag_graph_snapshots_v2"] == 0
        assert out["row_delta"]["aag_snapshot_observations"] == 1
        assert rv.verify_integrity(conn) == []
    finally:
        conn.close()


def test_artifact_reprocess_same_run_id_is_idempotent_and_conflict_is_rejected(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        kw = dict(commit_sha="2" * 40, generated_at=rv.BASE + timedelta(minutes=120),
                  now=rv.BASE + timedelta(minutes=300), run_id="run-idem")
        rv.reprocess_artifact(conn, rv.fixture_graph("GO100", 2), rv.GO100, **kw)
        replay = rv.reprocess_artifact(conn, rv.fixture_graph("GO100", 2), rv.GO100, **kw)
        assert replay["outcome"]["replayed"] is True
        assert not any(replay["row_delta"].values())
        with pytest.raises(ValueError, match="different input"):
            rv.reprocess_artifact(conn, rv.fixture_graph("GO100", 2), rv.GO100,
                                  **{**kw, "commit_sha": "7" * 40})
    finally:
        conn.close()


def test_artifact_reprocess_changed_content_creates_new_snapshot(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        graph = rv.fixture_graph("GO100", 2)
        graph["findings"][0]["rule"] = "SOMETHING_ELSE"
        out = rv.reprocess_artifact(
            conn, graph, rv.GO100, commit_sha="2" * 40, generated_at=rv.BASE + timedelta(minutes=121),
            now=rv.BASE + timedelta(minutes=301), run_id="run-changed")
        assert out["outcome"]["created_snapshot"] is True
        assert out["row_delta"]["aag_graph_snapshots_v2"] == 1
        assert rv.verify_integrity(conn) == []
    finally:
        conn.close()


def test_out_of_order_artifact_does_not_move_pointer(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        before = dict(conn.execute("SELECT * FROM aag_latest_pointers WHERE project='GO100'").fetchone())
        out = rv.ingest_artifact(
            conn, graph=rv.fixture_graph("GO100", 1), **rv.GO100, resolved_commit_sha="1" * 40,
            expected_head_sha="1" * 40, generated_at=rv.BASE - timedelta(minutes=30),
            now=rv.BASE + timedelta(minutes=400), run_id="run-old")
        after = dict(conn.execute("SELECT * FROM aag_latest_pointers WHERE project='GO100'").fetchone())
        assert out["result"] == "out_of_order" and out["authoritative"] is False
        assert before == after
        assert rv.verify_integrity(conn) == []
    finally:
        conn.close()


def test_two_projects_stay_isolated_through_backup_and_restore(bundle: Path, tmp_path: Path):
    conn = rv.restore(bundle, tmp_path / "restored.sqlite")
    try:
        rows = conn.execute("SELECT project, snapshot_id FROM aag_latest_pointers ORDER BY project").fetchall()
        assert [r["project"] for r in rows] == ["AADS", "GO100"]
        assert rows[0]["snapshot_id"] != rows[1]["snapshot_id"]
        foreign = conn.execute(
            "SELECT count(*) FROM aag_snapshot_observations o JOIN aag_graph_snapshots_v2 s"
            " ON s.id=o.snapshot_id WHERE o.project != s.project").fetchone()[0]
        assert foreign == 0
    finally:
        conn.close()


def test_drill_reports_ok_and_names_its_limitations(tmp_path: Path):
    report = rv.run_drill(tmp_path)
    assert report["ok"] is True
    assert all(c["rejected"] and c["expected_code_seen"] for c in report["corruption"])
    assert report["steps"]["destination_guard"]["ok"] is False
    joined = " ".join(report["limitations"])
    assert "PostgreSQL" in joined and "pg_dump" in joined
    assert "DSN" in joined


def test_cli_drill_exit_code_and_report_file(tmp_path: Path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "aag_restore_verify.py"), "drill",
         "--workdir", str(tmp_path / "work"), "--report", str(out)],
        capture_output=True, text=True, timeout=120, cwd=REPO)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text())["ok"] is True


def test_cli_restore_rejects_corrupt_bundle_with_nonzero_exit(bundle: Path, tmp_path: Path):
    (bundle / "aag_graph_snapshots_v2.jsonl").write_bytes(b"{}\n")
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "aag_restore_verify.py"), "restore",
         "--bundle", str(bundle), "--dest", str(tmp_path / "x.sqlite")],
        capture_output=True, text=True, timeout=60, cwd=REPO)
    assert proc.returncode == 1
    assert "checksum_mismatch" in proc.stdout


def test_tool_has_no_database_connection_surface():
    source = (REPO / "scripts" / "aag_restore_verify.py").read_text()
    for forbidden in ("asyncpg", "psycopg", "DATABASE_URL", "dsn"):
        assert forbidden not in source, forbidden
    assert "ANTHROPIC_API_KEY" not in source
