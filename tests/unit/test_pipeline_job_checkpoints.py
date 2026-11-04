"""pipeline_job_checkpoints 마이그레이션·정리 스크립트 검증.

실 PG 가 있으면(PG_TEST_DSN + psycopg2) 7일 경계·종료 작업 삭제를 실제로 돌리고,
없으면 SQL 문자열 검증만 수행한다(해당 테스트는 skip).
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIG = ROOT / "migrations" / "20261104_pipeline_job_checkpoints.sql"
SH = ROOT / "scripts" / "prune_pipeline_job_checkpoints.sh"


@pytest.fixture(scope="module")
def sql():
    return MIG.read_text(encoding="utf-8")


def test_table_columns(sql):
    for col in ("id uuid PRIMARY KEY DEFAULT gen_random_uuid()", "job_id           text NOT NULL",
                "base_sha", "patch            bytea NOT NULL", "patch_sha256", "files",
                "bytes            int NOT NULL", "model", "attempt", "host             text NOT NULL",
                "instruction_hash text NOT NULL", "created_at       timestamptz NOT NULL DEFAULT now()"):
        assert re.sub(r"\s+", " ", col) in re.sub(r"\s+", " ", sql), col
    assert "CREATE TABLE IF NOT EXISTS pipeline_job_checkpoints" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql and "(job_id, created_at DESC)" in sql


def test_header_and_rollback(sql):
    lines = sql.splitlines()
    assert all(line.startswith("--") for line in lines[:3])
    assert "DROP TABLE IF EXISTS pipeline_job_checkpoints" in sql


def test_idempotent(sql):
    assert not re.search(r"CREATE (TABLE|INDEX)(?! IF NOT EXISTS)", sql)
    assert "CREATE OR REPLACE FUNCTION aads_pipeline_job_checkpoints_prune" in sql
    assert "interval '7 days'" in sql
    assert "created_at < now() - retention" in sql


def test_terminal_statuses(sql):
    body = sql.split("purge_terminal()", 2)[-1]
    for st in ("done", "error", "rejected_done", "cancelled", "rejected"):
        assert f"'{st}'" in body
    assert "'running'" not in body and "'queued'" not in body


def test_script_syntax_and_guards():
    assert subprocess.run(["bash", "-n", str(SH)]).returncode == 0
    txt = SH.read_text(encoding="utf-8")
    assert "timeout " in txt
    assert "aads_pipeline_job_checkpoints_prune" in txt
    assert "aads_pipeline_job_checkpoints_purge_terminal" in txt
    assert "invalid_retention" in txt


def test_terminal_statuses_match_cleanup_module(sql):
    src = (ROOT / "app" / "services" / "pipeline_cleanup.py").read_text(encoding="utf-8")
    m = re.search(r"_TERMINAL_STATUSES\s*=\s*\(([^)]*)\)", src)
    expected = set(re.findall(r'"(\w+)"', m.group(1)))
    body = sql.split("purge_terminal()", 2)[-1]
    got = set(re.findall(r"IN \(([^)]*)\)", body)[0].replace("'", "").replace(" ", "").split(","))
    assert got == expected and "rejected" in got


def _fake_docker(tmp_path, body):
    d = tmp_path / "bin"
    d.mkdir()
    f = d / "docker"
    f.write_text("#!/usr/bin/env bash\n" + body + "\n")
    f.chmod(0o755)
    return {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}


def test_script_docker_hang_times_out(tmp_path):
    import time
    env = _fake_docker(tmp_path, "exec sleep 30")
    env["CHECKPOINT_PRUNE_TIMEOUT"] = "1"
    t = time.monotonic()
    r = subprocess.run(["bash", str(SH)], env=env, capture_output=True, text=True, timeout=20)
    assert time.monotonic() - t < 10
    assert r.returncode == 1 and "docker_ps_timeout" in r.stdout


def test_script_rejects_bad_mode():
    r = subprocess.run(["bash", str(SH)], env={**os.environ, "MODE": "typo"}, capture_output=True, text=True)
    assert r.returncode != 0 and "invalid_mode" in r.stdout and "usage" in r.stdout


def test_script_rejects_bad_retention():
    r = subprocess.run(["bash", str(SH)], env={**os.environ, "CHECKPOINT_RETENTION": "1 day; DROP TABLE x"},
                       capture_output=True, text=True)
    assert r.returncode == 2 and "invalid_retention" in r.stdout


# ---- 실 PG (선택) ----
DSN = os.environ.get("PG_TEST_DSN")
psycopg2 = pytest.importorskip("psycopg2") if DSN else None


@pytest.mark.skipif(not DSN, reason="SKIP 사유: PG_TEST_DSN 미설정 — 실 PG 시험(7일 경계·종료 삭제) 미실행")
def test_real_pg_prune_and_purge(sql):
    import uuid
    schema = "pjc_test_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        # 시험 전용 스키마: 체크포인트 테이블·함수는 여기에만 생기고, 기존 public 행은 건드리지 않는다.
        # pipeline_jobs(_archive) 는 TEMP 테이블이 가려 준다. 마지막에 DROP SCHEMA + rollback.
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET LOCAL search_path = {schema}")
        cur.execute("CREATE TEMP TABLE pipeline_jobs (job_id text, status text)")
        cur.execute("CREATE TEMP TABLE pipeline_jobs_archive (job_id text, status text)")
        cur.execute(sql)
        ins = ("INSERT INTO pipeline_job_checkpoints (job_id, base_sha, patch, patch_sha256, bytes, host,"
               " instruction_hash, created_at) VALUES (%s,'b','\\x00','h',1,'h','i', now() - %s::interval)")
        cur.execute(ins, ("j-6d23h", "6 days 23 hours"))
        cur.execute(ins, ("j-7d1m", "7 days 1 minute"))
        cur.execute("SELECT aads_pipeline_job_checkpoints_prune()")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT job_id FROM pipeline_job_checkpoints")
        assert [r[0] for r in cur.fetchall()] == ["j-6d23h"]

        cur.execute("TRUNCATE pipeline_job_checkpoints")  # 시험 스키마 안의 테이블만 비운다
        for jid, st in (("a", "done"), ("b", "error"), ("c", "rejected_done"), ("d", "cancelled"),
                        ("h", "rejected"), ("e", "running"), ("f", "queued")):
            cur.execute("INSERT INTO pipeline_jobs VALUES (%s,%s)", (jid, st))
            cur.execute(ins, (jid, "1 hour"))
        cur.execute(ins, ("g", "1 hour"))
        cur.execute("INSERT INTO pipeline_jobs_archive VALUES ('g','done')")
        cur.execute("SELECT aads_pipeline_job_checkpoints_purge_terminal()")
        assert cur.fetchone()[0] == 6
        cur.execute("SELECT job_id FROM pipeline_job_checkpoints ORDER BY job_id")
        assert [r[0] for r in cur.fetchall()] == ["e", "f"]
    finally:
        conn.rollback()
        conn.close()
