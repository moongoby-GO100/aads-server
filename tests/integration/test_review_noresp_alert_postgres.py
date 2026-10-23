"""검수 무응답률 경보 — 격리 PostgreSQL 통합 검증.

실제 ``ohvis_alert.notify`` 의 SELECT(dedupe)/INSERT 와 모듈의 상태·집계 SQL 을 그대로 돌린다.
메모리 Inbox mock 은 SQL/상태 계약을 덮지 못하므로 이 파일이 그 몫을 한다.

NORESP_TEST_DATABASE_URL 을 ``_test`` 로 끝나는 일회용 DB 로 지정해야 돈다(없으면 skip).
모든 객체는 임시 스키마 ``noresp_it`` 안에만 만들고 끝에서 지운다 — public 은 건드리지 않는다.

시계: 스키마에 ``now()`` 를 직접 정의하고 search_path 를 ``noresp_it, pg_catalog`` 로 두어
notify/모듈 SQL 의 ``NOW()`` 가 시험 시계를 읽게 했다. 그래서 며칠에 걸친 장애 흐름을
운영 SQL 변경 없이 결정적으로 재생한다.

    docker run --rm -d --name noresp-it-pg -e POSTGRES_PASSWORD=x -e POSTGRES_DB=noresp_it_test \\
        pgvector/pgvector:pg15
    NORESP_TEST_DATABASE_URL=postgresql://postgres:x@<host>:5432/noresp_it_test \\
        python3 -m pytest tests/integration/test_review_noresp_alert_postgres.py
"""
from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.core import db_pool  # noqa: E402
from app.services import ohvis_alert  # noqa: E402
from app.services import review_noresp_alert as nr  # noqa: E402

DATABASE_URL = os.getenv("NORESP_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="NORESP_TEST_DATABASE_URL is not configured"
)
SCHEMA = "noresp_it"
KST = nr.KST
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=KST)
DEN = 200  # 기본 최소 표본 100 이상

DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
SET search_path = {SCHEMA}, pg_catalog;
CREATE TABLE sim_clock(t timestamptz NOT NULL);
INSERT INTO sim_clock VALUES ('2026-10-22 12:00:00+09');
CREATE FUNCTION now() RETURNS timestamptz LANGUAGE sql STABLE
    AS $$ SELECT t FROM {SCHEMA}.sim_clock $$;
-- migrations/023_alert_history.sql 와 같은 정의
CREATE TABLE alert_history (
    id SERIAL PRIMARY KEY, severity VARCHAR(20) NOT NULL, category VARCHAR(30) NOT NULL,
    title TEXT NOT NULL, message TEXT NOT NULL, server VARCHAR(20), project VARCHAR(50),
    acknowledged BOOLEAN DEFAULT FALSE, acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW());
-- migrations/023_ai_feedback.sql + flag_category
CREATE TABLE code_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), job_id TEXT NOT NULL, project TEXT NOT NULL,
    verdict TEXT NOT NULL, score REAL NOT NULL, feedback JSONB DEFAULT '{{}}', diff_size INT,
    review_cycle INT DEFAULT 1, model_used TEXT, cost NUMERIC(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW(), flag_category TEXT);
"""


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("NORESP_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


class FlakyPool:
    """asyncpg 풀 위임자. fail_inserts>0 이면 alert_history INSERT 를 실패시킨다(실제 notify 가 삼킨다)."""

    def __init__(self, pool):
        self._pool = pool
        self.fail_inserts = 0

    async def execute(self, sql, *args):
        if self.fail_inserts and "INSERT INTO alert_history" in sql:
            self.fail_inserts -= 1
            raise RuntimeError("injected insert failure")
        return await self._pool.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._pool, name)


class Env:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.now = T0
        self.pool = None

    async def open(self):
        admin = await asyncpg.connect(_url())
        try:
            await admin.execute(DDL)
        finally:
            await admin.close()
        await self.reconnect()

    async def reconnect(self):
        """프로세스 재시작 흉내: 풀을 새로 만든다."""
        if self.pool is not None:
            await self.pool._pool.close()
        raw = await asyncpg.create_pool(
            _url(), min_size=1, max_size=2,
            server_settings={"search_path": f"{SCHEMA}, pg_catalog"},
        )
        self.pool = FlakyPool(raw)
        self.monkeypatch.setattr(db_pool, "get_pool", lambda: self.pool)

    async def close(self):
        if self.pool is not None:
            await self.pool._pool.close()
        admin = await asyncpg.connect(_url())
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await admin.close()

    async def set_clock(self, t):
        self.now = t
        await self.pool.execute("UPDATE sim_clock SET t = $1", t)

    async def seed_reviews(self, den=DEN):
        at = self.now - timedelta(days=1)
        await self.pool._pool.executemany(
            "INSERT INTO code_reviews(job_id, project, verdict, score, model_used, created_at) "
            "VALUES ($1, 'AADS', 'REJECT', 0, 'qwen', $2)",
            [(f"j{i}", at) for i in range(den)],
        )

    async def noresp(self, count):
        """무응답 행 수를 count 로 맞춘다(0이면 정상)."""
        await self.pool.execute(
            "UPDATE code_reviews SET flag_category = CASE "
            "WHEN substr(job_id, 2)::int < $1 THEN 'REVIEW_MODEL_NO_RESPONSE' END",
            count,
        )

    async def tick(self, minutes, on, base=T0, cfg=None):
        await self.set_clock(base + timedelta(minutes=minutes))
        await self.noresp(10 if on else 0)  # 10/200 = 5.0%
        out = await nr.evaluate_and_notify(pool=self.pool, now=self.now, cfg=cfg or nr.AlertConfig())
        return out["action"]

    async def inbox(self, category=nr.ALERT_CATEGORY):
        rows = await self.pool.fetch(
            "SELECT severity, category, title, message, project, acknowledged, created_at "
            "FROM alert_history WHERE category = $1 ORDER BY id", category,
        )
        return [dict(r) for r in rows]


def run(monkeypatch, scenario):
    async def main():
        env = Env(monkeypatch)
        await env.open()
        try:
            await scenario(env)
        finally:
            await env.close()

    asyncio.run(main())


def _sev(rows):
    return [r["severity"] for r in rows]


async def lifecycle_open(env):
    """알림함 두 category 의 가장 최근 severity 로 본 장애 중 여부(리터럴 SQL — 모듈 함수에 의존하지 않는다)."""
    sev = await env.pool.fetchval(
        "SELECT severity FROM alert_history "
        "WHERE category IN ('review_noresp_rate', 'review_noresp_state') "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    return sev in ("warning", "critical")


async def state_label(env):
    return nr.lifecycle_label(await nr._latest_state(env.pool, env.now))


def cap(n):
    return nr.AlertConfig(max_daily_incidents=n)


# ── 시험 환경 자체 검증 ──

def test_sim_clock_drives_default_and_notify_sql(monkeypatch):
    async def scenario(env):
        assert await env.pool.fetchval("SELECT now()") == T0
        await env.seed_reviews()
        await env.tick(0, True)
        rows = await env.inbox()
        assert len(rows) == 1
        # created_at DEFAULT NOW() 가 시험 시계를 읽었다 → 실제 notify INSERT 가 돈 증거
        assert rows[0]["created_at"] == T0

    run(monkeypatch, scenario)


# ── 핵심: 같은 날 경보→해소→재발→재해소 (검수 재현 순서) ──

def test_same_day_fire_resolve_recur_resolve_with_real_notify_sql(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        got = [await env.tick(m, on) for m, on in ((0, True), (30, False), (60, True), (90, False), (120, False))]
        assert got == ["fire_sent", "resolve_sent", "fire_sent", "resolve_sent", "none"]
        rows = await env.inbox()
        assert _sev(rows) == ["warning", "info", "warning", "info"]
        assert {r["category"] for r in rows} == {"review_noresp_rate"}
        assert {r["project"] for r in rows} == {"AADS"}
        assert not any(r["acknowledged"] for r in rows)
        assert len({r["title"] for r in rows}) == 4
        assert "재발 1회" in rows[2]["title"] and "2회차" in rows[3]["title"]
        assert [r["created_at"] for r in rows] == [T0 + timedelta(minutes=m) for m in (0, 30, 60, 90)]

    run(monkeypatch, scenario)


def test_repeated_evaluation_is_deduped_by_real_notify(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        assert await env.tick(0, True) == "fire_sent"
        assert await env.tick(30, True) == "fire_deduped"
        assert await env.tick(60, True) == "fire_deduped"
        assert len(await env.inbox()) == 1
        assert await env.tick(90, False) == "resolve_sent"
        assert await env.tick(120, False) == "none"
        assert await env.tick(150, False) == "none"
        assert len(await env.inbox()) == 2

    run(monkeypatch, scenario)


@pytest.mark.parametrize("failing_stage", [0, 1, 2, 3])
def test_insert_failure_at_each_stage_is_retried_without_duplicate(monkeypatch, failing_stage):
    async def scenario(env):
        await env.seed_reviews()
        for stage, on in enumerate((True, False, True, False)):
            env.pool.fail_inserts = 1 if stage == failing_stage else 0
            first = await env.tick(stage * 60, on)
            if stage == failing_stage:
                assert first.endswith("_failed")
                assert len(await env.inbox()) == stage       # 실패는 행을 남기지 않는다
                assert (await env.tick(stage * 60 + 30, on)).endswith("_sent")
            else:
                assert first.endswith("_sent")
            assert await env.tick(stage * 60 + 45, on) in ("fire_deduped", "none")
        rows = await env.inbox()
        assert _sev(rows) == ["warning", "info", "warning", "info"]
        assert len({r["title"] for r in rows}) == 4

    run(monkeypatch, scenario)


def test_state_recovers_after_module_and_pool_restart(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        await env.tick(0, True)
        await env.tick(30, False)
        await env.tick(60, True)                              # 두 번째 장애 진행 중
        importlib.reload(ohvis_alert)
        importlib.reload(nr)
        await env.reconnect()
        assert await env.tick(90, False) == "resolve_sent"    # 재시작 후에도 장애 중으로 복구
        importlib.reload(nr)
        await env.reconnect()
        assert await env.tick(120, False) == "none"           # 정상 상태도 복구 — 중복 해소 없음
        assert _sev(await env.inbox()) == ["warning", "info", "warning", "info"]

    run(monkeypatch, scenario)


def test_restart_between_fire_failure_and_retry(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        env.pool.fail_inserts = 1
        assert await env.tick(0, True) == "fire_failed"
        importlib.reload(nr)
        await env.reconnect()
        assert await env.tick(30, True) == "fire_sent"
        assert len(await env.inbox()) == 1

    run(monkeypatch, scenario)


# ── KST 날짜 경계 ──

def test_kst_day_boundary_counts_in_kst_not_utc_and_resets_at_midnight(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        base = datetime(2026, 10, 22, 7, 0, tzinfo=KST)       # 08:30 KST = 전날 23:30 UTC
        await env.set_clock(base)
        assert await env.tick(0, True, base) == "fire_sent"
        assert await env.tick(90, False, base) == "resolve_sent"
        assert await env.tick(180, True, base) == "fire_sent"
        rows = await env.inbox()
        assert "재발 1회" in rows[2]["title"]                  # UTC 일 경계였다면 1회차로 오집계

        late = datetime(2026, 10, 22, 23, 50, tzinfo=KST)
        # 00:10 KST 다음 날, 장애는 이어지는 중 — 날짜가 바뀌어 새 날짜 1회차 제목이므로 새로 나간다
        assert await env.tick(20, True, late) == "fire_sent"
        rows = await env.inbox()
        assert len(rows) == 4 and "2026-10-23" in rows[-1]["title"]
        assert "재발" not in rows[-1]["title"]
        assert await env.tick(50, False, late) == "resolve_sent"
        rows = await env.inbox()
        assert "2026-10-23" in rows[-1]["title"] and "회차" not in rows[-1]["title"]

    run(monkeypatch, scenario)


def test_daily_cap_keeps_incident_state_open_then_closes_once_with_real_sql(monkeypatch):
    """P1-1: 상한 뒤 재발 중에도 영속 상태는 장애 중, 해소는 사용자 알림 없이 한 번만 닫힌다."""
    async def scenario(env):
        await env.seed_reviews()
        actions = []
        for i in range(4):
            actions.append(await env.tick(i * 60, True))
            if i == 3:
                assert await lifecycle_open(env) is True            # 예전: 마지막 행이 info 라 정상 오판
                assert (await state_label(env)).startswith("장애 중 (일일 상한으로 경보 억제됨")
            actions.append(await env.tick(i * 60 + 30, False))
        assert actions == ["fire_sent", "resolve_sent"] * 3 + ["fire_suppressed", "resolve_suppressed"]
        sent = await env.inbox()
        assert len(sent) == 6 and not any("억제" in r["title"] for r in sent)   # 미발송이 발송 행으로 기록되지 않는다
        recs = await env.inbox(nr.STATE_CATEGORY)
        assert _sev(recs) == ["warning", "info"] and all(r["acknowledged"] for r in recs)
        assert await lifecycle_open(env) is False and await state_label(env) == "정상"
        assert await env.tick(4 * 60, False) == "none"
        assert len(await env.inbox(nr.STATE_CATEGORY)) == 2
        # 장애가 이어지는 다음 KST 날짜에는 새 날짜 제목으로 다시 나간다
        assert await env.tick(24 * 60, True) == "fire_sent"
        assert "2026-10-23" in (await env.inbox())[-1]["title"]

    run(monkeypatch, scenario)


def test_suppressed_state_survives_module_and_pool_restart(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        c = cap(1)
        assert await env.tick(0, True, cfg=c) == "fire_sent"
        assert await env.tick(30, False, cfg=c) == "resolve_sent"
        assert await env.tick(60, True, cfg=c) == "fire_suppressed"
        importlib.reload(ohvis_alert)
        importlib.reload(nr)
        await env.reconnect()
        assert await lifecycle_open(env) is True
        assert await env.tick(90, True, cfg=c) == "fire_suppressed"        # 재시작 뒤에도 행이 쌓이지 않는다
        assert len(await env.inbox(nr.STATE_CATEGORY)) == 1
        assert await env.tick(120, False, cfg=c) == "resolve_suppressed"
        importlib.reload(nr)
        await env.reconnect()
        assert await env.tick(150, False, cfg=c) == "none"
        assert len(await env.inbox()) == 2 and len(await env.inbox(nr.STATE_CATEGORY)) == 2

    run(monkeypatch, scenario)


@pytest.mark.parametrize("failing_stage", [2, 3])
def test_suppressed_stage_insert_failure_is_retried_without_loss_or_duplicate(monkeypatch, failing_stage):
    async def scenario(env):
        await env.seed_reviews()
        c = cap(1)
        want = ["fire_sent", "resolve_sent", "fire_suppressed", "resolve_suppressed"]
        for stage, on in enumerate((True, False, True, False)):
            env.pool.fail_inserts = 1 if stage == failing_stage else 0
            first = await env.tick(stage * 60, on, cfg=c)
            if stage == failing_stage:
                assert first == want[stage] + "_failed"
                rows = len(await env.inbox()) + len(await env.inbox(nr.STATE_CATEGORY))
                assert rows == stage                                       # 실패는 행을 남기지 않는다
                assert await env.tick(stage * 60 + 30, on, cfg=c) == want[stage]
            else:
                assert first == want[stage]
        assert _sev(await env.inbox()) == ["warning", "info"]
        assert _sev(await env.inbox(nr.STATE_CATEGORY)) == ["warning", "info"]
        assert await lifecycle_open(env) is False

    run(monkeypatch, scenario)


def test_daily_budget_is_per_kst_day_not_consumed_by_prior_day_resolution_p1_2(monkeypatch):
    """cap=1, KST 10/22 23:30 장애 → 10/23 00:00 해소 → 00:30 재발: 10/23 첫 경보는 발송되어야 한다."""
    async def scenario(env):
        await env.seed_reviews()
        base = datetime(2026, 10, 22, 23, 30, tzinfo=KST)
        await env.set_clock(base)
        c = cap(1)
        got = [await env.tick(m, on, base, c) for m, on in ((0, True), (30, False), (60, True))]
        assert got == ["fire_sent", "resolve_sent", "fire_sent"]
        rows = await env.inbox()
        assert _sev(rows) == ["warning", "info", "warning"] and len({r["title"] for r in rows}) == 3
        assert "2026-10-22" in rows[0]["title"] and "2026-10-23" in rows[2]["title"]
        assert await env.tick(90, False, base, c) == "resolve_sent"
        assert await env.tick(120, True, base, c) == "fire_suppressed"   # 10/23 두 번째 경보부터 상한
        assert len(await env.inbox(nr.STATE_CATEGORY)) == 1

    run(monkeypatch, scenario)


def test_default_cap_prior_day_unresolved_incident_resolved_today_keeps_full_budget(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        base = datetime(2026, 10, 22, 20, 0, tzinfo=KST)
        await env.set_clock(base)
        for m, on in ((0, True), (30, False), (60, True), (90, False), (120, True)):
            assert (await env.tick(m, on, base)).endswith("_sent")        # 10/22 경보 3회, 22:00 미해소
        assert await env.tick(300, False, base) == "resolve_sent"          # 10/23 01:00 해소
        for k in range(3):
            assert await env.tick(360 + 120 * k, True, base) == "fire_sent"
            assert await env.tick(420 + 120 * k, False, base) == "resolve_sent"
        assert await env.tick(720, True, base) == "fire_suppressed"
        rows = await env.inbox()
        assert len([r for r in rows if "2026-10-23" in r["title"] and r["severity"] == "warning"]) == 3
        assert len({r["title"] for r in rows}) == len(rows)

    run(monkeypatch, scenario)


# ── 시험 알림은 상태·회차 판정과 분리 ──

def test_test_alert_does_not_change_lifecycle_or_ordinal(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        assert await env.tick(0, False) == "none"
        assert await nr.send_test_alert(env.now) is True
        assert await env.tick(10, False) == "none"            # 시험 알림(info)이 해소로 읽히지 않음
        assert await env.tick(30, True) == "fire_sent"
        assert await nr.send_test_alert(env.now + timedelta(minutes=1)) is True
        assert await env.tick(60, False) == "resolve_sent"    # 시험 알림이 장애를 닫아버리지 않음
        assert "회차" not in (await env.inbox())[-1]["title"]  # 시험 알림이 회차를 올리지 않음
        test_rows = await env.inbox(nr.TEST_ALERT_CATEGORY)
        assert len(test_rows) == 2 and _sev(test_rows) == ["info", "info"]
        assert all(r["title"].startswith("[시험]") for r in test_rows)

    run(monkeypatch, scenario)


# ── 집계 SQL ──

def test_fetch_window_rows_sql_matches_independent_sql_aggregate(monkeypatch):
    async def scenario(env):
        pool = env.pool
        now = T0
        ins = (
            "INSERT INTO code_reviews(job_id, project, verdict, score, model_used, created_at, flag_category) "
            "VALUES ($1, 'AADS', 'REJECT', 0, $2, $3, $4)"
        )
        data = []
        for i in range(120):
            data.append((f"a{i}", "qwen", now - timedelta(days=1), None))
        for i in range(8):
            data.append((f"n{i}", "qwen", now - timedelta(hours=2 + i), "REVIEW_MODEL_NO_RESPONSE"))
        data += [("n0", "gemini", now - timedelta(days=2), "REVIEW_MODEL_NO_RESPONSE")]     # 재시도 행
        data += [(f"p{i}", "precheck", now - timedelta(days=1), "REVIEW_MODEL_NO_RESPONSE") for i in range(30)]
        data += [("nm", None, now - timedelta(days=1), None)]                                   # model NULL → 분모
        data += [("t", "qwen", now - timedelta(days=1), "REVIEW_TIMEOUT")]
        data += [("u", "qwen", now - timedelta(days=1), "REVIEW_API_UNAVAILABLE")]
        data += [("edge_in", "qwen", now - timedelta(days=7), None)]                           # 시작 포함
        data += [("edge_out", "qwen", now - timedelta(days=7, seconds=1), "REVIEW_MODEL_NO_RESPONSE")]
        data += [("future", "qwen", now, "REVIEW_MODEL_NO_RESPONSE")]                           # 끝 제외
        for row in data:
            await pool.execute(ins, *row)

        rows = await nr.fetch_window_rows(pool, now - timedelta(days=7), now)
        stats = nr.compute_noresp_rate(rows, now, 7)
        agg = await pool.fetchrow(
            "SELECT count(*) FILTER (WHERE model_used IS DISTINCT FROM 'precheck') AS den, "
            "count(*) FILTER (WHERE model_used IS DISTINCT FROM 'precheck' "
            "                 AND flag_category = 'REVIEW_MODEL_NO_RESPONSE') AS num, "
            "count(DISTINCT job_id) FILTER (WHERE model_used IS DISTINCT FROM 'precheck') AS jden, "
            "count(DISTINCT job_id) FILTER (WHERE model_used IS DISTINCT FROM 'precheck' "
            "                 AND flag_category = 'REVIEW_MODEL_NO_RESPONSE') AS jnum "
            "FROM code_reviews WHERE created_at >= $1 AND created_at < $2",
            now - timedelta(days=7), now,
        )
        assert (stats.numerator, stats.denominator) == (agg["num"], agg["den"]) == (9, 120 + 9 + 1 + 1 + 1 + 1)
        assert (stats.job_numerator, stats.job_denominator) == (agg["jnum"], agg["jden"])
        assert (stats.api_unavailable, stats.timeout) == (1, 1)

    run(monkeypatch, scenario)


def test_message_check_sql_runs_for_each_window(monkeypatch):
    async def scenario(env):
        await env.seed_reviews()
        await env.noresp(10)
        for days in (7, 14):
            rows = await env.pool.fetch(nr.check_sql(days).rstrip(";"))
            assert len(rows) == 10
        # 창을 줄이면 오래된 행이 빠진다 — SQL 의 창이 실제로 인자를 따른다
        await env.pool.execute(
            "UPDATE code_reviews SET created_at = $1 WHERE job_id = 'j0'", T0 - timedelta(days=10)
        )
        assert len(await env.pool.fetch(nr.check_sql(7).rstrip(";"))) == 9
        assert len(await env.pool.fetch(nr.check_sql(14).rstrip(";"))) == 10

    run(monkeypatch, scenario)
