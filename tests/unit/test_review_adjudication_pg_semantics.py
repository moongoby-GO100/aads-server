"""무응답 재검수 승계(원 세션 → CTO) 판정의 실제 PostgreSQL 검증.

대상: app/api/pipeline_runner.py adjudicate_review_from_origin_session 의 SQL 과
scripts/review-hold-sweeper.sh 전체 실행(이관·만료·CTO 승계·멱등). 대역 연결이 아니라 진짜 PostgreSQL 에서
돌린다 — 행 잠금·조건부 갱신·멱등 키는 대역으로 증명되지 않는다.

운영 DB 는 건드리지 않는다. M3_REVIEW_PG_ADMIN_URL(CREATEDB 가능한 접속 URL)로 접속해 일회용 DB 를 만들고
최소 스키마를 깐 뒤 끝나면 DROP 한다. 환경변수가 없으면 이 파일은 skip 된다.

    M3_REVIEW_PG_ADMIN_URL=postgresql://postgres@127.0.0.1:5432/postgres \\
        bash scripts/run_unit_tests.sh tests/unit/test_review_adjudication_pg_semantics.py
"""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ADMIN_URL = os.getenv("M3_REVIEW_PG_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="M3_REVIEW_PG_ADMIN_URL (CREATEDB 가능한 접속 URL) 필요 — 일회용 DB 를 만든다")

REPO = Path(__file__).resolve().parents[2]
SWEEPER = REPO / "scripts" / "review-hold-sweeper.sh"
T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIFF = "diff --git a/x b/x\n+한글 변경\n"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    role_key TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE pipeline_jobs (
    job_id TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT 'AADS', status TEXT NOT NULL,
    phase TEXT, chat_session_id TEXT, tenant_id UUID, commit_hash TEXT, git_diff TEXT,
    review_flag_category TEXT, review_needs_retry BOOLEAN DEFAULT FALSE,
    review_retry_count INTEGER NOT NULL DEFAULT 0, review_retry_last_at TIMESTAMPTZ,
    review_request_id UUID, review_verdict TEXT, review_score NUMERIC, review_feedback TEXT,
    error_detail TEXT, completed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE chat_deferred_reactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    system_message TEXT NOT NULL, ohvis_task_id TEXT, runner_job_id TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
    claimed_by TEXT, lease_expires_at TIMESTAMPTZ, error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ);
CREATE TABLE code_reviews (
    id SERIAL PRIMARY KEY, job_id TEXT, project TEXT, verdict TEXT, score NUMERIC, feedback JSONB,
    diff_size INT, model_used TEXT, cost NUMERIC, flag_category TEXT, failure_stage TEXT,
    needs_retry BOOLEAN, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE pipeline_runner_events (
    id SERIAL PRIMARY KEY, job_id TEXT, tenant_id UUID, project TEXT, event_type TEXT,
    status TEXT, phase TEXT, metadata JSONB, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""

FAKE_PSQL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # psql 대역 — 스위퍼가 쓰는 -q -t -A -F 출력 형식만 흉내 낸다. 접속은 M3_PG_DSN.
    import asyncio, os, sys
    import asyncpg

    argv = sys.argv[1:]
    sep = "|"
    for i, a in enumerate(argv):
        if a == "-F":
            sep = argv[i + 1]

    def fmt(v):
        if v is None:
            return ""
        if v is True:
            return "t"
        if v is False:
            return "f"
        return str(v)

    async def main():
        sql = sys.stdin.read()
        conn = await asyncpg.connect(os.environ["M3_PG_DSN"])
        try:
            try:
                rows = await conn.fetch(sql)
            except asyncpg.exceptions.PostgresSyntaxError as e:
                if "multiple commands" not in str(e):
                    raise
                await conn.execute(sql)
                rows = []
            for r in rows:
                print(sep.join(fmt(v) for v in r.values()))
        finally:
            await conn.close()

    try:
        asyncio.run(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    """
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(tmp_path):
    import asyncpg

    parts = urlsplit(ADMIN_URL)
    name = f"m3_review_{uuid.uuid4().hex[:12]}"
    test_url = urlunsplit(parts._replace(path=f"/{name}"))

    async def setup():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(test_url)
        try:
            await conn.execute(SCHEMA)
        finally:
            await conn.close()

    async def teardown():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    _run(setup())
    try:
        yield test_url
    finally:
        _run(teardown())


class Env:
    """한 테스트의 일회용 DB 에 대한 얇은 헬퍼."""

    def __init__(self, url: str):
        self.url = url
        self.sessions: dict[str, str] = {}

    async def exec(self, sql: str, *args):
        import asyncpg

        conn = await asyncpg.connect(self.url)
        try:
            return await conn.execute(sql, *args)
        finally:
            await conn.close()

    async def fetch(self, sql: str, *args):
        import asyncpg

        conn = await asyncpg.connect(self.url)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    async def val(self, sql: str, *args):
        rows = await self.fetch(sql, *args)
        return rows[0][0] if rows else None

    async def session(self, name: str, tenant: str, role: str | None) -> str:
        sid = str(uuid.uuid4())
        await self.exec("INSERT INTO chat_sessions (id, tenant_id, role_key) VALUES ($1::uuid,$2::uuid,$3)",
                        sid, tenant, role)
        self.sessions[name] = sid
        return sid

    async def job(self, job_id: str, origin: str | None, **kw):
        cols = dict(status="review_hold", phase="review_hold", tenant_id=T1,
                    commit_hash=COMMIT, git_diff=DIFF,
                    review_flag_category="REVIEW_MODEL_NO_RESPONSE", review_retry_count=3,
                    error_detail=None)
        cols.update(kw)
        cols["job_id"] = job_id
        cols["chat_session_id"] = origin
        names = list(cols)
        ph = ", ".join(f"${i + 1}" + ("::uuid" if n == "tenant_id" else "") for i, n in enumerate(names))
        await self.exec(f"INSERT INTO pipeline_jobs ({', '.join(names)}) VALUES ({ph})", *cols.values())

    async def reaction(self, session_id: str, job_id: str, status: str = "claimed",
                       commit: str = COMMIT, diff: str = DIFF, minutes_old: int = 1) -> str:
        import hashlib

        key = f"review-adjudication-cto:{job_id}:{commit}:{hashlib.sha256(diff.encode()).hexdigest()}"
        return str(await self.val(
            """INSERT INTO chat_deferred_reactions
               (session_id, system_message, ohvis_task_id, status, created_at)
               VALUES ($1::uuid, 'm', $2, $3, NOW() - ($4::int * INTERVAL '1 minute')) RETURNING id::text""",
            session_id, key, status, minutes_old))


def _diff_hash(text: str = DIFF) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _call(env: Env, job_id: str, caller: str, verdict: str = "APPROVE", *, tenant: str = T1,
          commit: str = COMMIT, diff_hash: str | None = None, findings: str = "근거"):
    """실제 엔드포인트 함수를 격리 DB 풀로 호출한다. push/deploy 계열 호출이 있으면 즉시 실패한다."""
    import asyncio as aio
    import subprocess as sp

    import asyncpg

    from app.api import pipeline_runner as pr
    from app.core import db_pool

    forbidden: list[str] = []

    async def _boom(*a, **k):
        forbidden.append("create_subprocess")
        raise AssertionError("판정 API 가 subprocess 를 호출했다")

    async def go():
        pool = await asyncpg.create_pool(env.url, min_size=1, max_size=4)
        old = db_pool._pool
        db_pool._pool = pool
        try:
            req = pr.PipelineReviewAdjudicateRequest(
                caller_session_id=caller, expected_commit_sha=commit,
                expected_diff_sha256=diff_hash or _diff_hash(), verdict=verdict, findings=findings)
            return await pr.adjudicate_review_from_origin_session(
                job_id, req, context={"tenant": {"id": tenant}})
        finally:
            db_pool._pool = old
            await pool.close()

    orig = (aio.create_subprocess_exec, aio.create_subprocess_shell, sp.run, sp.Popen)
    aio.create_subprocess_exec = aio.create_subprocess_shell = _boom  # type: ignore[assignment]
    sp.run = sp.Popen = _boom  # type: ignore[assignment]
    try:
        return _run(go())
    finally:
        aio.create_subprocess_exec, aio.create_subprocess_shell, sp.run, sp.Popen = orig  # type: ignore[assignment]
        assert not forbidden


def _status(exc_info) -> int:
    return exc_info.value.status_code


async def _cto_world(env: Env, job_id: str = "runner-aaaaaa01"):
    origin = await env.session("origin", T1, "CEO")
    cto = await env.session("cto", T1, "CTO")
    await env.job(job_id, origin, error_detail="review_cto_adjudication_pending")
    rid = await env.reaction(cto, job_id)
    return origin, cto, rid


def test_assigned_cto_approves_to_awaiting_approval_and_consumes_request(db):
    env = Env(db)
    job = "runner-aaaaaa01"
    _, cto, rid = _run(_cto_world(env, job))

    out = _call(env, job, cto, "APPROVE")

    assert out["status"] == "awaiting_approval" and out["phase"] == "awaiting_approval"
    row = _run(env.fetch("SELECT status, phase, error_detail, review_verdict, review_feedback FROM pipeline_jobs WHERE job_id=$1", job))[0]
    assert (row["status"], row["phase"], row["review_verdict"]) == ("awaiting_approval", "awaiting_approval", "APPROVE")
    assert row["error_detail"] is None and "[CTO 판정] APPROVE" in row["review_feedback"]
    assert _run(env.val("SELECT status FROM chat_deferred_reactions WHERE id=$1::uuid", rid)) == "completed"
    review = _run(env.fetch("SELECT model_used, verdict FROM code_reviews WHERE job_id=$1", job))
    assert [(r["model_used"], r["verdict"]) for r in review] == [("cto_session_adjudicator", "APPROVE")]
    event = _run(env.val("SELECT event_type FROM pipeline_runner_events WHERE job_id=$1", job))
    assert event == "cto_review_adjudicated"

    # 재사용: 소비된 요청·이미 전이한 작업은 다시 판정할 수 없다.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(env, job, cto, "REJECT")
    assert _status(exc) == 409
    assert _run(env.val("SELECT status FROM pipeline_jobs WHERE job_id=$1", job)) == "awaiting_approval"


def test_assigned_cto_reject_is_terminal_error(db):
    env = Env(db)
    job = "runner-aaaaaa02"
    _, cto, _ = _run(_cto_world(env, job))
    out = _call(env, job, cto, "REJECT")
    assert out["status"] == "error" and out["phase"] == "review_failed"
    row = _run(env.fetch("SELECT error_detail, completed_at FROM pipeline_jobs WHERE job_id=$1", job))[0]
    assert row["error_detail"] == "review_failed: source=cto_session_adjudicator" and row["completed_at"] is not None
    assert _run(env.val("SELECT COUNT(*) FROM code_reviews WHERE job_id=$1", job)) == 0


def test_cto_unknown_consumes_request_but_keeps_expiry_clock_and_hold(db):
    env = Env(db)
    job = "runner-aaaaaa03"
    _, cto, rid = _run(_cto_world(env, job))
    _run(env.exec("UPDATE pipeline_jobs SET updated_at = NOW() - INTERVAL '90 minutes' WHERE job_id=$1", job))
    before = _run(env.val("SELECT updated_at FROM pipeline_jobs WHERE job_id=$1", job))

    out = _call(env, job, cto, "UNKNOWN")

    assert out["status"] == "review_hold"
    row = _run(env.fetch("SELECT status, error_detail, updated_at, review_retry_count FROM pipeline_jobs WHERE job_id=$1", job))[0]
    assert row["status"] == "review_hold" and row["error_detail"] == "review_cto_adjudication_pending"
    assert row["updated_at"] == before and row["review_retry_count"] == 3
    assert _run(env.val("SELECT status FROM chat_deferred_reactions WHERE id=$1::uuid", rid)) == "completed"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(env, job, cto, "APPROVE")
    assert _status(exc) == 403


def test_cto_negative_matrix_is_rejected_with_403_or_409(db):
    from fastapi import HTTPException

    env = Env(db)
    job = "runner-aaaaaa04"
    origin, cto, rid = _run(_cto_world(env, job))
    other_cto = _run(env.session("other_cto", T1, "CTO"))
    dev = _run(env.session("dev", T1, "DEV"))
    foreign_cto = _run(env.session("foreign_cto", T2, "CTO"))
    # 다른 role/tenant 세션에도 같은 키의 요청을 심어 "요청 존재" 만으로는 통과하지 못함을 본다.
    _run(env.reaction(dev, job))
    _run(env.reaction(foreign_cto, job))

    def expect(code, *a, **k):
        with pytest.raises(HTTPException) as exc:
            _call(env, job, *a, **k)
        assert _status(exc) == code, exc.value.detail

    expect(403, other_cto)          # 미배정 CTO
    expect(403, dev)                # 다른 role
    expect(403, foreign_cto)        # 다른 tenant 의 CTO
    expect(403, str(uuid.uuid4()))  # 존재하지 않는 세션
    expect(403, origin)             # 승계된 뒤 원 세션
    expect(409, cto, tenant=T2)     # 호출 컨텍스트 tenant 불일치
    expect(409, cto, commit="f" * 40)
    expect(409, cto, diff_hash="0" * 64)

    # 증거가 요청 이후 바뀌면(요청 키 불일치) 새 hash 로도 거부된다.
    _run(env.exec("UPDATE pipeline_jobs SET git_diff=git_diff || 'x' WHERE job_id=$1", job))
    expect(403, cto, diff_hash=_diff_hash(DIFF + "x"))
    _run(env.exec("UPDATE pipeline_jobs SET git_diff=$2 WHERE job_id=$1", job, DIFF))

    # 취소·소비·만료된 요청
    for status in ("failed", "completed", "skipped_stale"):
        _run(env.exec("UPDATE chat_deferred_reactions SET status=$2 WHERE id=$1::uuid", rid, status))
        expect(403, cto)
    _run(env.exec("UPDATE chat_deferred_reactions SET status='claimed', created_at=NOW() - INTERVAL '121 minutes' WHERE id=$1::uuid", rid))
    expect(403, cto)
    _run(env.exec("UPDATE chat_deferred_reactions SET created_at=NOW() WHERE id=$1::uuid", rid))

    # 표식이 CTO 대기가 아니면 CTO 는 판정할 수 없다.
    _run(env.exec("UPDATE pipeline_jobs SET error_detail='review_origin_adjudication_pending' WHERE job_id=$1", job))
    expect(403, cto)
    _run(env.exec("UPDATE pipeline_jobs SET error_detail='review_cto_adjudication_pending', review_flag_category='CODE_QUALITY' WHERE job_id=$1", job))
    expect(409, cto)
    _run(env.exec("UPDATE pipeline_jobs SET review_flag_category='REVIEW_TIMEOUT', review_retry_count=2 WHERE job_id=$1", job))
    expect(409, cto)

    # 모든 거부 뒤에도 작업·요청은 손대지 않았다.
    assert _run(env.val("SELECT status FROM pipeline_jobs WHERE job_id=$1", job)) == "review_hold"
    assert _run(env.val("SELECT COUNT(*) FROM code_reviews WHERE job_id=$1", job)) == 0

    # 모든 조건을 되돌리면 같은 CTO 는 통과한다(거부가 우연이 아님).
    _run(env.exec("UPDATE pipeline_jobs SET review_retry_count=3 WHERE job_id=$1", job))
    assert _call(env, job, cto, "APPROVE")["status"] == "awaiting_approval"


def test_origin_session_rules_are_unchanged(db):
    from fastapi import HTTPException

    env = Env(db)
    job = "runner-aaaaaa05"
    origin = _run(env.session("origin", T1, "CEO"))
    stranger = _run(env.session("stranger", T1, "CTO"))
    _run(env.job(job, origin, error_detail="review_origin_adjudication_pending"))

    # 원 세션 대기 중에는 CTO 라도 원 세션이 아니면 403 (아직 승계 전).
    with pytest.raises(HTTPException) as exc:
        _call(env, job, stranger)
    assert _status(exc) == 403
    with pytest.raises(HTTPException) as exc:
        _call(env, job, origin, commit="f" * 40)
    assert _status(exc) == 409
    out = _call(env, job, origin, "APPROVE")
    assert out["status"] == "awaiting_approval"
    assert _run(env.val("SELECT model_used FROM code_reviews WHERE job_id=$1", job)) == "origin_session_adjudicator"
    assert _run(env.val("SELECT event_type FROM pipeline_runner_events WHERE job_id=$1", job)) == "origin_review_adjudicated"


def test_concurrent_double_adjudication_has_exactly_one_winner(db):
    import asyncpg
    from fastapi import HTTPException

    from app.api import pipeline_runner as pr
    from app.core import db_pool

    env = Env(db)
    job = "runner-aaaaaa06"
    _, cto, rid = _run(_cto_world(env, job))

    async def go():
        pool = await asyncpg.create_pool(env.url, min_size=2, max_size=8)
        old = db_pool._pool
        db_pool._pool = pool
        try:
            def call(verdict):
                req = pr.PipelineReviewAdjudicateRequest(
                    caller_session_id=cto, expected_commit_sha=COMMIT,
                    expected_diff_sha256=_diff_hash(), verdict=verdict, findings="동시")
                return pr.adjudicate_review_from_origin_session(
                    job, req, context={"tenant": {"id": T1}})
            return await asyncio.gather(*[call("APPROVE") for _ in range(4)] + [call("REJECT")],
                                        return_exceptions=True)
        finally:
            db_pool._pool = old
            await pool.close()

    results = _run(go())
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, HTTPException)]
    assert len(wins) == 1 and len(losses) == 4, results
    assert {loss.status_code for loss in losses} == {409}
    final = _run(env.val("SELECT status FROM pipeline_jobs WHERE job_id=$1", job))
    assert final == wins[0]["status"]
    assert _run(env.val("SELECT COUNT(*) FROM chat_deferred_reactions WHERE id=$1::uuid AND status='completed'", rid)) == 1
    assert _run(env.val("SELECT COUNT(*) FROM pipeline_runner_events WHERE job_id=$1", job)) == 1
    assert _run(env.val("SELECT COUNT(*) FROM code_reviews WHERE job_id=$1", job)) == (1 if final == "awaiting_approval" else 0)


# ── 스위퍼 전체 실행: 이관 → 만료 → CTO 승계 → 종결 ─────────────────────────────


@pytest.fixture()
def sweeper_env(db, tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    psql = shim / "psql"
    psql.write_text(FAKE_PSQL, encoding="utf-8")
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env.update(
        PATH=f"{shim}:{env['PATH']}", M3_PG_DSN=db, DB_MODE="psql", PGHOST="127.0.0.1",
        LOG_DIR=str(tmp_path / "log"), SWEEP_LOCK_FILE=str(tmp_path / "sweeper.lock"),
        AADS_API_URL="http://127.0.0.1:9", REVIEW_RETRY_MAX="3",
        REVIEW_ORIGIN_ADJUDICATION_RETRY_THRESHOLD="3", REVIEW_ORIGIN_ADJUDICATION_TIMEOUT_MIN="120",
        REVIEW_CTO_ADJUDICATION_SESSION_ID="",
    )
    env.setdefault("PYTHONPATH", "")
    return env


def _sweep(sweeper_env) -> subprocess.CompletedProcess:
    # 같은 인터프리터를 psql 대역이 쓰도록 PATH 의 python3 을 그대로 쓴다.
    out = subprocess.run(["bash", str(SWEEPER)], env=sweeper_env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    return out


async def _job_state(env: Env, job: str):
    row = (await env.fetch("SELECT status, phase, error_detail FROM pipeline_jobs WHERE job_id=$1", job))[0]
    return row["status"], row["phase"], row["error_detail"]


async def _age(env: Env, job: str, minutes: int = 130):
    await env.exec("UPDATE pipeline_jobs SET updated_at = NOW() - ($2::int * INTERVAL '1 minute') WHERE job_id=$1", job, minutes)


def test_third_no_response_hands_to_origin_then_succeeds_to_cto_then_fails_closed(db, sweeper_env):
    env = Env(db)
    job = "runner-bbbbbb01"
    origin = _run(env.session("origin", T1, "CEO"))
    cto = _run(env.session("cto", T1, "CTO"))
    _run(env.session("other_tenant_cto", T2, "CTO"))
    _run(env.job(job, origin, review_retry_count=3, error_detail=None))

    # sweep 1: 상한(3)=임계값(3) 이어도 종결이 아니라 원 세션 이관.
    _sweep(sweeper_env)
    assert _run(_job_state(env, job)) == ("review_hold", "review_hold", "review_origin_adjudication_pending")
    origin_key = f"review-adjudication:{job}:{COMMIT}:{_diff_hash()}"
    assert _run(env.val("SELECT COUNT(*) FROM chat_deferred_reactions WHERE ohvis_task_id=$1 AND status='pending' AND session_id=$2::uuid",
                        origin_key, origin)) == 1

    # sweep 2 (곧바로): 실제 pending 이므로 exhausted 처리가 종결하지 않고, 요청도 중복되지 않는다.
    _sweep(sweeper_env)
    assert _run(_job_state(env, job))[2] == "review_origin_adjudication_pending"
    assert _run(env.val("SELECT COUNT(*) FROM chat_deferred_reactions WHERE ohvis_task_id=$1", origin_key)) == 1

    # 원 세션 판정 만료 → 원 요청은 닫히고 같은 tenant 의 다른 실제 CTO 1명에게 1회 이관.
    _run(_age(env, job))
    _sweep(sweeper_env)
    assert _run(_job_state(env, job)) == ("review_hold", "review_hold", "review_cto_adjudication_pending")
    assert _run(env.val("SELECT status FROM chat_deferred_reactions WHERE ohvis_task_id=$1", origin_key)) == "failed"
    cto_key = f"review-adjudication-cto:{job}:{COMMIT}:{_diff_hash()}"
    cto_rows = _run(env.fetch("SELECT session_id::text AS sid, status FROM chat_deferred_reactions WHERE ohvis_task_id=$1", cto_key))
    assert [(r["sid"], r["status"]) for r in cto_rows] == [(cto, "pending")]

    # 이어지는 sweep: 같은 증거에 두 번째 CTO 요청이 생기지 않는다(멱등).
    _sweep(sweeper_env)
    _sweep(sweeper_env)
    assert _run(env.val("SELECT COUNT(*) FROM chat_deferred_reactions WHERE ohvis_task_id=$1", cto_key)) == 1
    assert _run(_job_state(env, job))[2] == "review_cto_adjudication_pending"

    # 이 요청으로 실제 판정 API 가 CTO 승인을 받는다(스위퍼가 만든 행 그대로).
    # CTO 만료 → fail-closed 종결, 승인 없음.
    _run(_age(env, job))
    _sweep(sweeper_env)
    status, phase, detail = _run(_job_state(env, job))
    assert (status, phase) == ("error", "review_cto_adjudication_expired")
    assert detail.startswith("review_cto_adjudication_expired")
    assert _run(env.val("SELECT status FROM chat_deferred_reactions WHERE ohvis_task_id=$1", cto_key)) == "failed"
    assert _run(env.val("SELECT COUNT(*) FROM pipeline_jobs WHERE status='awaiting_approval'")) == 0


def test_sweeper_created_cto_request_is_accepted_by_the_api(db, sweeper_env):
    env = Env(db)
    job = "runner-bbbbbb02"
    origin = _run(env.session("origin", T1, "CEO"))
    cto = _run(env.session("cto", T1, "CTO"))
    _run(env.job(job, origin, review_retry_count=3, error_detail="review_origin_adjudication_pending"))
    _run(_age(env, job))

    _sweep(sweeper_env)
    assert _run(_job_state(env, job))[2] == "review_cto_adjudication_pending"

    out = _call(env, job, cto, "APPROVE")
    assert out["status"] == "awaiting_approval"
    assert _run(env.val("SELECT status FROM chat_deferred_reactions WHERE session_id=$1::uuid", cto)) == "completed"


def test_pinned_cto_env_cannot_bypass_tenant_or_role(db, sweeper_env):
    env = Env(db)
    job = "runner-bbbbbb03"
    origin = _run(env.session("origin", T1, "CEO"))
    real_cto = _run(env.session("cto", T1, "CTO"))
    foreign = _run(env.session("foreign", T2, "CTO"))
    dev = _run(env.session("dev", T1, "DEV"))
    _run(env.job(job, origin, review_retry_count=3, error_detail="review_origin_adjudication_pending"))
    _run(_age(env, job))

    for pinned in (foreign, dev, origin):
        sweeper_env["REVIEW_CTO_ADJUDICATION_SESSION_ID"] = pinned
        _run(env.exec("UPDATE pipeline_jobs SET error_detail='review_origin_adjudication_pending', status='review_hold', updated_at=NOW() - INTERVAL '130 minutes' WHERE job_id=$1", job))
        _run(env.exec("DELETE FROM chat_deferred_reactions"))
        _sweep(sweeper_env)
        sid = _run(env.val("SELECT session_id::text FROM chat_deferred_reactions WHERE ohvis_task_id LIKE 'review-adjudication-cto:%'"))
        assert sid == real_cto, f"pinned={pinned} 가 검증을 우회했다"


def test_origin_expiry_without_any_cto_session_terminates_fail_closed(db, sweeper_env):
    env = Env(db)
    job = "runner-bbbbbb04"
    origin = _run(env.session("origin", T1, "CEO"))
    _run(env.session("only_foreign_cto", T2, "CTO"))
    _run(env.job(job, origin, review_retry_count=3, error_detail="review_origin_adjudication_pending"))
    _run(_age(env, job))
    _sweep(sweeper_env)
    status, phase, _ = _run(_job_state(env, job))
    assert (status, phase) == ("error", "review_origin_adjudication_expired")


def test_missing_origin_session_or_evidence_keeps_existing_safe_termination(db, sweeper_env):
    env = Env(db)
    origin = _run(env.session("origin", T1, "CEO"))
    _run(env.session("cto", T1, "CTO"))
    _run(env.job("runner-bbbbbb05", None, review_retry_count=3))                      # 원 세션 없음
    _run(env.job("runner-bbbbbb06", origin, review_retry_count=3, commit_hash="zz"))  # 증거(commit) 없음
    _run(env.job("runner-bbbbbb07", origin, review_retry_count=3, git_diff=""))       # 증거(diff) 없음
    _sweep(sweeper_env)
    for job in ("runner-bbbbbb05", "runner-bbbbbb06"):
        assert _run(_job_state(env, job))[:2] == ("error", "review_retry_exhausted"), job
    # diff 가 비어 있으면 모델에 보낼 수 없는 산출물 없음 규칙이 먼저 적용된다 — needs_retry 일 때만.
    assert _run(env.val("SELECT COUNT(*) FROM chat_deferred_reactions")) == 0


def test_expired_origin_marker_left_by_a_previous_sweep_is_picked_up_once(db, sweeper_env):
    env = Env(db)
    job = "runner-bbbbbb08"
    origin = _run(env.session("origin", T1, "CEO"))
    cto = _run(env.session("cto", T1, "CTO"))
    _run(env.job(job, origin, review_retry_count=3, error_detail="review_origin_adjudication_expired"))
    _sweep(sweeper_env)
    _sweep(sweeper_env)
    assert _run(_job_state(env, job))[2] == "review_cto_adjudication_pending"
    assert _run(env.val("SELECT COUNT(*) FROM chat_deferred_reactions WHERE session_id=$1::uuid", cto)) == 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
