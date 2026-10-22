"""scripts/aag_restore_verify_pg.py 의 순수 로직 — DB·docker 없이 돈다."""
from __future__ import annotations

import hashlib
import sys
import textwrap

import pytest

from scripts import aag_restore_verify_pg as pg


def obs(oid, *, scope=("GO100", "repo", "refs/heads/main", "default"), verified_at="2026-10-22T00:00:00.000001",
        created_at="2026-10-22T00:00:00.000001", authoritative=True, status="verified", run_exists=True,
        snapshot_exists=True, commit="c1", snapshot="s1", run="r1"):
    return {"id": oid, "run_id": run, "snapshot_id": snapshot, "project": scope[0], "repository_id": scope[1],
            "target_ref": scope[2], "governance_scope": scope[3], "resolved_commit_sha": commit,
            "expected_target_ref_head_sha": commit, "authoritative": authoritative,
            "verification_status": status, "verified_at": verified_at, "created_at": created_at,
            "run_exists": run_exists, "snapshot_exists": snapshot_exists}


def pointer_from(o, generated_at="2026-10-21T00:00:00.000000"):
    return {"project": o["project"], "repository_id": o["repository_id"], "target_ref": o["target_ref"],
            "governance_scope": o["governance_scope"], "snapshot_id": o["snapshot_id"],
            "observation_id": o["id"], "run_id": o["run_id"], "resolved_commit_sha": o["resolved_commit_sha"],
            "expected_target_ref_head_sha": o["expected_target_ref_head_sha"], "generated_at": generated_at,
            "verified_at": o["verified_at"]}


def head_from(o, generated_at="2026-10-21T00:00:00.000000"):
    return {"project": o["project"], "repository_id": o["repository_id"], "target_ref": o["target_ref"],
            "governance_scope": o["governance_scope"], "head_commit_sha": o["resolved_commit_sha"],
            "observation_id": o["id"], "generated_at": generated_at}


# ── 카운트·스키마 ────────────────────────────────────────────────────────────────────
def test_compare_counts_reports_each_kind_of_mismatch():
    source = {t: {"n": 1, "digest": "a"} for t in pg.AAG_TABLES}
    restored = {t: {"n": 1, "digest": "a"} for t in pg.AAG_TABLES}
    rows, failures = pg.compare_counts(source, restored)
    assert failures == [] and len(rows) == 16 and all(r["count_ok"] and r["digest_ok"] for r in rows)

    restored["aag_scan_runs"] = {"n": 0, "digest": "a"}
    restored["aag_ref_heads"] = {"n": 1, "digest": "b"}
    del restored["aag_audit_events"]
    _, failures = pg.compare_counts(source, restored)
    assert {(f["code"], f["detail"].split(":")[0]) for f in failures} == {
        ("row_count_mismatch", "aag_scan_runs"), ("content_digest_mismatch", "aag_ref_heads"),
        ("table_missing", "aag_audit_events")}


def test_aag_table_list_is_the_sixteen_documented_tables():
    assert len(pg.AAG_TABLES) == len(set(pg.AAG_TABLES)) == 16
    assert all(t.startswith("aag_") for t in pg.AAG_TABLES)


def test_compare_schema():
    base = {"aag_x": {"columns": ["a"], "constraints": ["c"], "indexes": ["i"]}}
    assert pg.compare_schema(base, {"aag_x": dict(base["aag_x"])}) == []
    changed = {"aag_x": {"columns": ["a"], "constraints": [], "indexes": ["i"]}}
    assert [f["code"] for f in pg.compare_schema(base, changed)] == ["schema_constraints_mismatch"]
    assert pg.compare_schema(base, {})[0]["code"] == "schema_table_missing"


# ── 트리거 ───────────────────────────────────────────────────────────────────────────
def trigger_rows():
    rows = []
    for (table, name), events in pg.EXPECTED_TRIGGERS.items():
        rows.append({"table": table, "name": name, "enabled": "O", "before": True,
                     "ins": "INSERT" in events, "upd": "UPDATE" in events, "del": "DELETE" in events})
    return rows


def test_triggers_ok_and_each_defect_is_named():
    assert pg.check_triggers(trigger_rows(), trigger_rows()) == []

    missing = [r for r in trigger_rows() if r["table"] != "aag_audit_events"]
    codes = {f["code"] for f in pg.check_triggers(trigger_rows(), missing)}
    assert {"trigger_missing", "trigger_set_differs_from_source"} <= codes

    disabled = trigger_rows()
    disabled[0]["enabled"] = "D"
    assert "trigger_disabled" in {f["code"] for f in pg.check_triggers(disabled, disabled)}

    weak = trigger_rows()
    weak[0]["del"] = False
    assert "trigger_events_mismatch" in {f["code"] for f in pg.check_triggers(trigger_rows(), weak)}

    after = trigger_rows()
    after[0]["before"] = False
    assert "trigger_not_before" in {f["code"] for f in pg.check_triggers(after, after)}


def test_orphans():
    assert pg.check_orphans({"a": 0, "b": 0}) == []
    assert pg.check_orphans({"a": 0, "b": 3}) == [{"code": "orphan_reference", "detail": "b: 3"}]


# ── pointer ──────────────────────────────────────────────────────────────────────────
def test_rebuild_picks_latest_authoritative_verified_per_scope():
    other = ("AADS", "aads-server", "refs/heads/main", "default")
    observations = [
        obs("o1", verified_at="2026-10-22T00:00:00.000001", commit="c1", snapshot="s1", run="r1"),
        obs("o2", verified_at="2026-10-22T00:00:00.000009", commit="c2", snapshot="s2", run="r2"),
        obs("o3", verified_at="2026-10-22T00:00:00.000020", commit="c3", status="unknown", run="r3"),
        obs("o4", verified_at="2026-10-22T00:00:00.000030", commit="c4", authoritative=False, run="r4"),
        obs("o5", verified_at="2026-10-22T00:00:00.000040", commit="c5", run_exists=False, run="r5"),
        obs("o6", scope=other, commit="c6", snapshot="s6", run="r6"),
    ]
    rebuilt = pg.rebuild_pointers(observations)
    assert set(rebuilt) == {pg.scope_of(observations[0]), other}
    assert rebuilt[pg.scope_of(observations[0])]["observation_id"] == "o2"
    assert rebuilt[other]["run_id"] == "r6"


def test_rebuild_tie_breaks_on_created_at_then_id():
    a = obs("a", created_at="2026-10-22T00:00:00.000001")
    b = obs("b", created_at="2026-10-22T00:00:00.000002")
    c = obs("c", created_at="2026-10-22T00:00:00.000002")
    assert pg.rebuild_pointers([c, a, b])[pg.scope_of(a)]["observation_id"] == "c"


def test_compare_pointers_match_and_diffs():
    o = obs("o1")
    rebuilt = pg.rebuild_pointers([o])
    rows, failures = pg.compare_pointers(rebuilt, [pointer_from(o)])
    assert failures == [] and rows[0]["match"]

    stale = pointer_from(o)
    stale["snapshot_id"] = "other"
    rows, failures = pg.compare_pointers(rebuilt, [stale])
    assert failures == [{"code": "pointer_rebuild_mismatch", "detail": f"{'/'.join(pg.scope_of(o))}.snapshot_id"}]

    _, failures = pg.compare_pointers({}, [pointer_from(o)])
    assert failures[0]["code"] == "pointer_rebuild_missing_scope"
    _, failures = pg.compare_pointers(rebuilt, [])
    assert failures[0]["code"] == "pointer_rebuild_extra_scope"


def test_pointer_consistency():
    o = obs("o1")
    assert pg.check_pointer_consistency([pointer_from(o)], [head_from(o)], [o]) == []

    codes = lambda p, h, ob: {f["code"] for f in pg.check_pointer_consistency(p, h, ob)}  # noqa: E731
    assert codes([pointer_from(o)], [head_from(o)], []) == {"pointer_observation_not_authoritative"}
    assert "ref_head_mismatch" in codes([pointer_from(o)], [], [o])
    assert "ref_head_mismatch" in codes([pointer_from(o)], [head_from(o, "2020-01-01T00:00:00.000000")], [o])
    assert "pointer_observation_dangling" in codes([pointer_from(o)], [head_from(o)], [obs("o1", snapshot_exists=False)])
    assert "pointer_observation_not_verified" in codes([pointer_from(o)], [head_from(o)], [obs("o1", status="unknown")])
    moved = pointer_from(o)
    moved["resolved_commit_sha"] = "zzz"
    assert "pointer_commit_mismatch" in codes([moved], [head_from(o)], [o])
    relinked = pointer_from(o)
    relinked["run_id"] = "rX"
    assert "pointer_link_mismatch" in codes([relinked], [head_from(o)], [o])


# ── advisory lock ────────────────────────────────────────────────────────────────────
def test_lock_probe_classification():
    assert pg.classify_lock_probe(True, False, True, True, 1) == []
    assert [f["code"] for f in pg.classify_lock_probe(True, True, True, True, 1)] == [
        "lock_second_session_also_acquired"]
    codes = {f["code"] for f in pg.classify_lock_probe(False, False, False, False, 0)}
    assert codes == {"lock_first_session_not_acquired", "lock_other_scope_blocked",
                     "lock_not_released_on_commit", "lock_granted_count"}


# ── 비밀값 ───────────────────────────────────────────────────────────────────────────
SECRET = hashlib.sha256(b"aag-restore-drill-unit-fixture").hexdigest()


def test_secret_scan_finds_value_split_across_chunks_and_counts_without_returning_it():
    payload = b"xxxx" + SECRET.encode() + b"yyyy"
    for cut in range(1, len(payload)):
        assert pg.find_secret_leaks([payload[:cut], payload[cut:]], [SECRET]) == 1, cut
    assert pg.find_secret_leaks([b"nothing here", b"at all"], [SECRET]) == 0
    assert pg.find_secret_leaks([payload], []) == 0
    assert pg.find_secret_leaks([payload], [SECRET, "f" * 64]) == 1


def test_secret_scan_reads_files(tmp_path):
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    clean.write_bytes(b"abc" * 1000)
    dirty.write_bytes(b"abc" * 1000 + SECRET.encode())
    assert pg.find_secret_leaks(pg.iter_file_chunks([clean], size=7), [SECRET]) == 0
    assert pg.find_secret_leaks(pg.iter_file_chunks([clean, dirty], size=7), [SECRET]) == 1


def test_mask_expression_is_derived_from_id_not_from_token():
    sql = pg.mask_token_sql()
    assert "c.id" in sql and "token_hash" not in sql


def test_digest_expression_drops_secret_columns_only_where_declared():
    assert "token_hash" in pg._digest_expr("aag_scanner_credentials")
    assert pg._digest_expr("aag_scan_runs") == "t::text"


# ── 운영 DB 쓰기 차단 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sql", [
    "INSERT INTO aag_scan_runs VALUES (1)", "update aag_scan_runs set result='x'", "DELETE FROM aag_audit_events",
    "DROP TABLE aag_scan_runs", "CREATE TABLE x(a int)", "ALTER TABLE aag_scan_runs ADD c int",
    "TRUNCATE aag_scan_runs", "COPY aag_scan_runs TO PROGRAM 'x'", "SELECT 1; DELETE FROM t",
    "GRANT ALL ON t TO u", "DO $$ BEGIN END $$", "SET ROLE postgres",
])
def test_read_only_guard_rejects_writes(sql):
    assert not pg.is_read_only_sql(sql)


def test_read_only_guard_allows_every_query_the_collector_sends():
    class Recorder:
        read_only = True

        def __init__(self):
            self.sent = []

        def text(self, sql):
            self.sent.append(sql)
            return "1" if "count(*)" in sql and "jsonb_build_object" not in sql else "15.17"

        def json(self, sql):
            self.sent.append(sql)
            return {"n": 0, "digest": "d"} if "'digest'" in sql else []

    rec = Recorder()
    pg.collect_state(rec)  # type: ignore[arg-type]
    pg.masked_credentials_json(rec)  # type: ignore[arg-type]
    assert len(rec.sent) > 16 * 2
    assert all(pg.is_read_only_sql(sql) for sql in rec.sent), [s for s in rec.sent if not pg.is_read_only_sql(s)]
    assert "'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;'" not in rec.sent


def test_read_only_guard_literals_and_column_names_are_not_false_positives():
    assert pg.is_read_only_sql("SELECT updated_at, created_at FROM aag_latest_pointers WHERE reason='DELETE me'")
    assert pg.is_read_only_sql("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;")


# ── 보조 ────────────────────────────────────────────────────────────────────────────
def test_deadline_expires():
    now = [0.0]
    deadline = pg.Deadline(10, clock=lambda: now[0])
    assert deadline.remaining() == 10
    now[0] = 9.5
    assert deadline.remaining() == pytest.approx(0.5)
    now[0] = 10
    with pytest.raises(pg.DrillTimeout):
        deadline.remaining()


def test_runner_job_label():
    assert pg.runner_job_label({"AADS_HEAVY_JOB_ID": "runner-abc_1"}, "/x") == "runner-abc_1"
    assert pg.runner_job_label({}, "/tmp/aads-wt-runner-639e8fd9") == "runner-639e8fd9"
    assert pg.runner_job_label({"AADS_HEAVY_JOB_ID": "bad id"}, "/root/aads") == ""


def test_ignorable_restore_errors_are_only_the_default_objects():
    assert pg.ignorable_restore_error('pg_restore: error: could not execute query: ERROR:  schema "public" already exists')
    assert not pg.ignorable_restore_error('pg_restore: error: could not execute query: ERROR:  relation "aag_x" does not exist')


# ── PsqlSession 프로토콜 (가짜 psql) ───────────────────────────────────────────────────
FAKE_PSQL = textwrap.dedent(r"""
    import sys
    for line in sys.stdin:
        line = line.rstrip("\n")
        if line.startswith("\\echo "):
            print(line[6:], flush=True)
        elif line == "\\q":
            break
        elif line.startswith("DIE"):
            sys.exit(3)
        elif line.startswith("SELECT"):
            print('{"a": [1, 2]}', flush=True)
""")


def fake_session(deadline=None, read_only=False):
    return pg.PsqlSession([sys.executable, "-u", "-c", FAKE_PSQL], deadline or pg.Deadline(30), read_only=read_only)


def test_session_roundtrip_and_empty_response():
    session = fake_session()
    try:
        assert session.json("SELECT 1;") == {"a": [1, 2]}
        assert session.lines("BEGIN;") == []
        assert session.text("SELECT 2;") == '{"a": [1, 2]}'
        with pytest.raises(pg.SessionError):
            session.text("BEGIN;")
    finally:
        session.close()


def test_session_reports_psql_death():
    session = fake_session()
    try:
        with pytest.raises(pg.SessionError):
            session.lines("DIE")
    finally:
        session.close()


def test_read_only_session_refuses_write_before_sending():
    session = fake_session(read_only=True)
    try:
        with pytest.raises(pg.SessionError):
            session.lines("DELETE FROM aag_audit_events;")
        assert session.json("SELECT 1;") == {"a": [1, 2]}
    finally:
        session.close()


# ── 보고서 ───────────────────────────────────────────────────────────────────────────
def sample_result(ok=True):
    o = obs("o1")
    rows, _ = pg.compare_counts({t: {"n": 2, "digest": "a"} for t in pg.AAG_TABLES},
                                {t: {"n": 2, "digest": "a"} for t in pg.AAG_TABLES})
    pointer_rows, _ = pg.compare_pointers(pg.rebuild_pointers([o]), [pointer_from(o)])
    return {
        "ok": ok, "started_at": "20261022000000", "elapsed_seconds": 12.5, "snapshot": "00000003-1", "container": "aag-restore-drill-x",
        "image": "pgvector/pgvector:pg15", "phases": {"pg_dump": 1.0}, "table_rows": rows, "pointer_rows": pointer_rows,
        "authoritative_observations": 1, "orphans_source": {"a": 0}, "orphans_restored": {"a": 0},
        "triggers": trigger_rows(), "probes": {"audit_update": {"blocked": True, "message": "ERROR: append-only"}},
        "lock": {"second_session_while_held": False, "other_scope_while_held": True,
                 "second_session_after_commit": True, "advisory_granted_on_key": 1},
        "secrets": {"count": 1, "leaked_in_dump_files": 0, "leaked_in_restored_db": 0,
                    "scan_control_expected": 1, "scan_control_found": 1},
        "restore": {"schema_restore_rc": 0, "schema_restore_ignored": 1, "schema_restore_errors": [], "data_restore_rc": 0},
        "failures": [] if ok else [{"code": "row_count_mismatch", "detail": "aag_scan_runs: 2 != 1"}],
        "cleanup": {"remaining_containers": 0, "workdir_exists": False},
        "server_versions": {"source": "15.17", "restored": "15.17"},
    }


def test_report_has_sixteen_table_rows_and_never_prints_a_secret():
    text = pg.render_report(sample_result())
    assert "# GO100 AAG 운영 DB 복구 검증" in text
    assert text.count("| aag_") >= 16
    assert "결과: **통과**" in text and "- 없음" in text
    assert SECRET not in text
    assert "0건" in text
    assert "1/1 발견" in text


def test_report_marks_failure():
    text = pg.render_report(sample_result(ok=False))
    assert "결과: **실패**" in text and "row_count_mismatch" in text
