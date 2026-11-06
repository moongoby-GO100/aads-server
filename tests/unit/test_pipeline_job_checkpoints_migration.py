"""migrations/20261104_pipeline_job_checkpoints.sql — 자동 검증 통과, 근거 주석, 동명 객체 보호.

정적 검사(1~3)는 항상 돈다. 4 이후는 임시 PostgreSQL 이 필요하다 — initdb/pg_ctl/psql 이 PATH 나
/usr/lib/postgresql/*/bin 에 없으면 건너뛴다(운영 DB 에는 절대 접속하지 않는다).
  AADS_TEST_PG_BIN  PostgreSQL bin 디렉터리를 직접 지정(지정하면 권한 전환은 그 도구들이 책임진다)
  AADS_TEST_PG_UID  root 로 실행될 때 서버를 돌릴 비root uid (기본 65534)
"""
import glob
import hashlib
import importlib.util
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/deferred/20261104_pipeline_job_checkpoints.sql"
SQL = MIGRATION.read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("autoverify", ROOT / "scripts/release_migration_autoverify.py")
av = importlib.util.module_from_spec(spec)
spec.loader.exec_module(av)

FUNCS = (
    "aads_pipeline_job_checkpoints_prune",
    "aads_pipeline_job_checkpoints_purge_terminal",
)


def _function_bodies() -> dict[str, str]:
    out = {}
    for st in av.split_statements(SQL):
        m = re.match(r"^CREATE OR REPLACE FUNCTION (\w+)\(", st["shape"], re.I)
        if m:
            out[m.group(1).lower()] = st["bodies"][0]
    return out


# ── 1. 자동 검증 분류 ──────────────────────────────────────────────────────

def test_autoverify_classify_cli_passes():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release_migration_autoverify.py"), "classify", str(MIGRATION)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AUTOVERIFY-UNSAFE" not in r.stdout
    assert r.stdout.strip() == ""


def test_lock_and_statement_timeout_come_first():
    stmts = av.split_statements(SQL)
    assert stmts[0]["shape"] == "SET LOCAL LOCK_TIMEOUT = 'S'"
    assert "SET LOCAL lock_timeout = '5s';" in SQL
    assert stmts[1]["shape"] == "SET LOCAL STATEMENT_TIMEOUT = 'S'"
    assert "SET LOCAL statement_timeout = '60s';" in SQL
    assert "CONCURRENTLY" not in "\n".join(s["shape"] for s in stmts)


def test_guard_blocks_precede_their_create_statements():
    shapes = [s["shape"] for s in av.split_statements(SQL)]
    first_create_table = next(i for i, s in enumerate(shapes) if s.startswith("CREATE TABLE"))
    first_create_func = next(i for i, s in enumerate(shapes) if s.startswith("CREATE OR REPLACE FUNCTION"))
    do_idx = [i for i, s in enumerate(shapes) if s.startswith("DO ")]
    assert len(do_idx) == 2
    assert do_idx[0] < first_create_table
    assert do_idx[1] < first_create_func


# ── 2. 근거 주석 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("needle", [
    "■ 목적",
    "■ 만드는 객체",
    "■ 기존 객체 영향과 잠금",
    "■ API 롤백 시 무해성",
    "■ 저장 내용의 시크릿·개인정보 처리와 접근 범위",
    "■ 정리 함수의 호출자·주기·범위",
    "■ 롤백 SQL",
    "lock_timeout",
    "2026-11-05 12:54 KST",
    "to_regclass('public.pipeline_job_checkpoints')",
    "트리거가 없고",
    "저장 전 시크릿 검사",
    "아예 저장하지 않는다",
    "보존 7일",
    "채팅 SELECT 도구도 aads 로 접속",
    "patch 열은 암호문(v1)이다",
    "채팅 SQL 경로는 암호문만 보인다",
    "셸 접근으로 키를 일부러 읽는 것은 막지 않는다(잔여 위험)",
    "migrations/ 로 옮기는 것은 키 설치 뒤 별도 단계",
    "매시 17분",
    "scripts/prune_pipeline_job_checkpoints.sh",
    "SECURITY DEFINER",
    "DROP TABLE IF EXISTS pipeline_job_checkpoints;",
])
def test_header_comment_documents_rationale(needle):
    header = SQL.split("\nSET LOCAL lock_timeout", 1)[0]
    assert needle in header


# ── 3. md5 상수와 함수 본문 ────────────────────────────────────────────────

def test_guard_md5_constants_match_function_bodies():
    bodies = _function_bodies()
    assert set(bodies) == set(FUNCS)
    consts = dict(re.findall(r"\('(aads_pipeline_job_checkpoints_\w+)',\s*'([0-9a-f]{32})'", SQL))
    assert set(consts) == set(FUNCS)
    for name in FUNCS:
        assert consts[name] == hashlib.md5(bodies[name].encode("utf-8")).hexdigest(), name


def test_guard_do_blocks_have_no_data_changing_words():
    for st in av.split_statements(SQL):
        if st["shape"].startswith("DO "):
            for body in st["bodies"]:
                assert not av.BANNED_WORDS.search(av._norm(re.sub(r"--[^\n]*", " ", body)))


# ── 4. 임시 PostgreSQL ─────────────────────────────────────────────────────

def _pg_bin() -> Path | None:
    override = os.environ.get("AADS_TEST_PG_BIN")
    if override:
        return Path(override)
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    cands = sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    return Path(cands[0]) if cands else None


class TempPg:
    def __init__(self, bindir: Path, base: Path, prefix: list[str]):
        self.bindir, self.base, self.prefix = bindir, base, prefix
        self.data = base / "data"
        self.sock = base / "sock"
        self.log = base / "server.log"
        self.started = False
        self._n = itertools.count()

    def _run(self, tool: str, *args: str, sql: str | None = None, timeout: int = 60):
        return subprocess.run(
            [*self.prefix, str(self.bindir / tool), *args],
            input=sql, capture_output=True, text=True, timeout=timeout,
            stdin=None if sql is not None else subprocess.DEVNULL,
        )

    def start(self):
        r = self._run("initdb", "-D", str(self.data), "-U", "postgres", "-A", "trust", "--no-sync", "-E", "UTF8")
        assert r.returncode == 0, r.stdout + r.stderr
        # -l 로 서버 stdout/stderr 를 파일로 보낸다. 안 그러면 서버가 캡처 파이프를 상속해 run() 이 EOF 를 기다리며 멈춘다.
        opts = f"-c listen_addresses='' -c unix_socket_directories={self.sock} -c fsync=off -c max_connections=20"
        self.started = True
        r = self._run("pg_ctl", "-D", str(self.data), "-l", str(self.log), "-o", opts, "-w", "-t", "30", "start",
                      timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr + self._log_tail()

    def stop(self):
        if self.started:
            try:
                self._run("pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "-t", "20", "stop", timeout=40)
            finally:
                self.started = False

    def _log_tail(self) -> str:
        try:
            return "\n--- server.log ---\n" + self.log.read_text(errors="replace")[-2000:]
        except OSError:
            return ""

    def psql(self, db: str, sql: str):
        return self._run("psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", str(self.sock),
                         "-U", "postgres", "-d", db, sql=sql)

    def new_db(self) -> str:
        name = f"t{next(self._n)}"
        r = self.psql("postgres", f"CREATE DATABASE {name} TEMPLATE template0;")
        assert r.returncode == 0, r.stderr
        return name


@pytest.fixture(scope="module")
def pg():
    bindir = _pg_bin()
    if bindir is None or not (bindir / "initdb").exists():
        pytest.skip("임시 PostgreSQL(initdb/pg_ctl/psql) 없음")
    prefix: list[str] = []
    uid = None
    if os.geteuid() == 0:
        uid = int(os.environ.get("AADS_TEST_PG_UID", "65534"))
        if not os.environ.get("AADS_TEST_PG_BIN"):
            if not shutil.which("setpriv"):
                pytest.skip("root 실행: 비root 전환용 setpriv 없음")
            prefix = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups"]
    base = Path(tempfile.mkdtemp(prefix="pjc-pg-", dir="/tmp"))
    server = TempPg(bindir, base, prefix)
    try:
        if uid is not None:
            os.chown(base, uid, uid)
        os.chmod(base, 0o700)
        server.sock.mkdir(mode=0o700)
        if uid is not None:
            os.chown(server.sock, uid, uid)
        server.start()
        yield server
    finally:
        try:
            server.stop()
        finally:
            shutil.rmtree(base, ignore_errors=True)


def apply_in_txn(pg_: TempPg, db: str, sql: str = SQL):
    """apply_migration.sh 처럼 BEGIN … COMMIT 으로 감싸 한 번에 적용한다."""
    return pg_.psql(db, "BEGIN;\n" + sql + "\nCOMMIT;\n")


def scalar(pg_: TempPg, db: str, q: str) -> str:
    r = pg_.psql(db, q)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


STUBS = """
CREATE TABLE pipeline_jobs (job_id text PRIMARY KEY, status text);
CREATE TABLE pipeline_jobs_archive (job_id text PRIMARY KEY, status text);
"""


def test_applies_twice_idempotently(pg):
    db = pg.new_db()
    assert pg.psql(db, STUBS).returncode == 0
    for _ in range(2):
        r = apply_in_txn(pg, db)
        assert r.returncode == 0, r.stderr
    assert scalar(pg, db, "SELECT to_regclass('pipeline_job_checkpoints') IS NOT NULL;") == "t"
    assert scalar(pg, db, "SELECT count(*) FROM pg_proc WHERE proname LIKE 'aads_pipeline_job_checkpoints_%';") == "2"
    assert scalar(pg, db, "SELECT count(*) FROM pg_indexes WHERE indexname='idx_pjc_job_created';") == "1"


def test_cleanup_functions_touch_only_checkpoints(pg):
    db = pg.new_db()
    assert pg.psql(db, STUBS).returncode == 0
    assert apply_in_txn(pg, db).returncode == 0
    ins = ("INSERT INTO pipeline_job_checkpoints (job_id, base_sha, patch, patch_sha256, bytes, host, instruction_hash, created_at) "
           "VALUES ('{j}', 'b', 'x'::bytea, 's', 1, 'h', 'i', {t});")
    sql = "INSERT INTO pipeline_jobs VALUES ('done1','done'),('run1','running'),('queued1','pending');\n"
    sql += "INSERT INTO pipeline_jobs_archive VALUES ('arch1','error'),('arch2','running');\n"
    for j in ("done1", "run1", "queued1", "arch1", "arch2", "gone1"):
        sql += ins.format(j=j, t="now()") + "\n"
    sql += ins.format(j="old1", t="now() - interval '8 days'") + "\n"
    assert pg.psql(db, sql).returncode == 0
    assert scalar(pg, db, "SELECT aads_pipeline_job_checkpoints_prune();") == "1"
    assert scalar(pg, db, "SELECT aads_pipeline_job_checkpoints_purge_terminal();") == "2"
    left = scalar(pg, db, "SELECT string_agg(job_id, ',' ORDER BY job_id) FROM pipeline_job_checkpoints;")
    assert left == "arch2,gone1,queued1,run1"
    assert scalar(pg, db, "SELECT count(*) FROM pipeline_jobs;") == "3"
    assert scalar(pg, db, "SELECT count(*) FROM pipeline_jobs_archive;") == "2"


def test_foreign_function_with_same_name_is_not_replaced(pg):
    db = pg.new_db()
    assert pg.psql(db, STUBS).returncode == 0
    foreign = ("CREATE FUNCTION aads_pipeline_job_checkpoints_purge_terminal() RETURNS bigint "
               "LANGUAGE plpgsql AS $$ BEGIN RETURN 42; END $$;")
    assert pg.psql(db, foreign).returncode == 0
    r = apply_in_txn(pg, db)
    assert r.returncode != 0
    assert "교체하지 않고 멈춥니다" in r.stderr
    assert scalar(pg, db, "SELECT aads_pipeline_job_checkpoints_purge_terminal();") == "42"
    assert scalar(pg, db, "SELECT to_regclass('pipeline_job_checkpoints') IS NULL;") == "t"


@pytest.mark.parametrize("alter,probe", [
    ("ALTER FUNCTION aads_pipeline_job_checkpoints_prune(interval) SET search_path = pg_catalog",
     "SELECT proconfig::text FROM pg_proc WHERE proname='aads_pipeline_job_checkpoints_prune'"),
    ("ALTER FUNCTION aads_pipeline_job_checkpoints_purge_terminal() SET search_path = pg_catalog",
     "SELECT proconfig::text FROM pg_proc WHERE proname='aads_pipeline_job_checkpoints_purge_terminal'"),
    ("ALTER FUNCTION aads_pipeline_job_checkpoints_purge_terminal() SECURITY DEFINER",
     "SELECT prosecdef::text FROM pg_proc WHERE proname='aads_pipeline_job_checkpoints_purge_terminal'"),
    ("ALTER FUNCTION aads_pipeline_job_checkpoints_prune(interval) STRICT",
     "SELECT proisstrict::text FROM pg_proc WHERE proname='aads_pipeline_job_checkpoints_prune'"),
    ("ALTER FUNCTION aads_pipeline_job_checkpoints_prune(interval) STABLE",
     "SELECT provolatile::text FROM pg_proc WHERE proname='aads_pipeline_job_checkpoints_prune'"),
])
def test_same_body_but_changed_attributes_is_not_replaced(pg, alter, probe):
    db = pg.new_db()
    assert pg.psql(db, STUBS).returncode == 0
    assert apply_in_txn(pg, db).returncode == 0
    assert pg.psql(db, alter + ";").returncode == 0
    before = scalar(pg, db, probe + ";")
    r = apply_in_txn(pg, db)
    assert r.returncode != 0
    assert "교체하지 않고 멈춥니다" in r.stderr
    assert scalar(pg, db, probe + ";") == before


def test_overload_with_same_name_stops(pg):
    db = pg.new_db()
    assert pg.psql(db, STUBS).returncode == 0
    other = ("CREATE FUNCTION aads_pipeline_job_checkpoints_prune(retention integer) RETURNS bigint "
             "LANGUAGE plpgsql AS $$ BEGIN RETURN 0; END $$;")
    assert pg.psql(db, other).returncode == 0
    r = apply_in_txn(pg, db)
    assert r.returncode != 0
    assert "교체하지 않고 멈춥니다" in r.stderr


def test_table_with_different_columns_stops(pg):
    db = pg.new_db()
    assert pg.psql(db, STUBS + "CREATE TABLE pipeline_job_checkpoints (id int);").returncode == 0
    r = apply_in_txn(pg, db)
    assert r.returncode != 0
    assert "열 구성이 다릅니다" in r.stderr
    assert scalar(pg, db, "SELECT count(*) FROM pg_proc WHERE proname LIKE 'aads_pipeline_job_checkpoints_%';") == "0"


def test_index_name_taken_by_other_table_stops(pg):
    db = pg.new_db()
    assert pg.psql(db, STUBS + "CREATE TABLE other_t (job_id text); CREATE INDEX idx_pjc_job_created ON other_t (job_id);").returncode == 0
    r = apply_in_txn(pg, db)
    assert r.returncode != 0
    assert "idx_pjc_job_created" in r.stderr
