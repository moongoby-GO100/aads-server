"""`/ohvis/llmops/status`의 프로젝트 스코프와 부분 적용 내성 회귀 테스트.

이전 구현은 `project` 인자를 받아 응답에 그대로 되돌려주기만 하고 SQL에서는
무시했다. 그래서 `?project=AADS`가 GO100/CEO/project=NULL 행까지 포함한 전역
총계를 AADS의 숫자인 것처럼 보여줬다. 또 `datasets` 테이블 하나의 존재가
`examples`/`experiments` 조회를 함께 게이트해서, 부분 적용된 DB에서 없는
테이블 하나가 나머지 집계 전부를 죽였다. `scores`/`feedback`은 아예 없었다.

여기서 지키는 계약:
- 스코프 집계는 **모든** 지표가 project 소유를 증명한 행만 센다.
- `project=None`은 예전 그대로 전역 집계다.
- 테이블이 없으면 그 지표의 **키가 빠진다**. 0은 "정말 0건"일 때만 쓴다.
- 지표 하나가 못 세어져도 나머지 집계는 살아남는다.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app.services import llmops_store
from app.services.llmops_store import (
    DATASET_TABLE,
    EXAMPLE_TABLE,
    EXPERIMENT_TABLE,
    FEEDBACK_TABLE,
    LEGACY_TRACE_TABLE,
    LLMOPS_TABLES,
    SCORE_TABLE,
    TRACE_TABLE,
    build_status_count_specs,
    reset_relation_cache,
)

ALL_TABLES = (*LLMOPS_TABLES, LEGACY_TRACE_TABLE)
CHILD_METRICS = ("examples", "experiments", "scores", "feedback")


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_relation_cache()
    yield
    reset_relation_cache()


def _present(*missing: str) -> dict[str, bool]:
    return {table: table not in missing for table in ALL_TABLES}


def _specs(project, *missing: str) -> dict[str, str]:
    return dict(build_status_count_specs(_present(*missing), project))


# ── 스코프 SQL ──────────────────────────────────────────────────────────────


def test_every_scoped_metric_constrains_by_the_requested_project() -> None:
    """어떤 지표든 project 조건 없이 세면 다른 프로젝트가 총계로 샌다."""
    specs = _specs("AADS")
    assert set(specs) >= {
        "traces_total",
        "traces_error",
        "traces_last_24h",
        "legacy_traces_total",
        "datasets",
        *CHILD_METRICS,
    }
    for key, sql in specs.items():
        assert "$1" in sql, f"{key} 집계가 project 인자를 쓰지 않는다: {sql}"


def test_scoped_child_metrics_prove_ownership_by_join_not_by_bare_count() -> None:
    """project 컬럼이 없는 원장은 소유 관계를 따라가야 한다."""
    specs = _specs("AADS")

    for key in ("examples", "experiments"):
        assert DATASET_TABLE in specs[key], f"{key}가 dataset 소유를 확인하지 않는다"
        assert "d.project = $1" in specs[key]

    # score는 experiment / example / 원본 trace 중 어느 경로로든 소유를 증명한다.
    scores = specs["scores"]
    assert scores.count("EXISTS") == 3, scores
    assert EXPERIMENT_TABLE in scores and EXAMPLE_TABLE in scores and TRACE_TABLE in scores

    # feedback은 trace를 통해서만 프로젝트에 속한다.
    assert "t.id::text = f.trace_id" in specs["feedback"]
    assert "t.project = $1" in specs["feedback"]


def test_scoped_metrics_never_emit_an_unfiltered_count() -> None:
    """`SELECT COUNT(*) FROM <표>` 로 끝나는 스코프 집계가 있으면 회귀다."""
    bare = re.compile(r"^SELECT COUNT\(\*\) FROM \w+$")
    for key, sql in _specs("AADS").items():
        assert not bare.match(sql.strip()), f"{key}가 전역 총계를 반환한다: {sql}"


def test_global_scope_keeps_the_previous_plain_totals() -> None:
    """project=None은 예전 semantics 그대로 — 고아 행까지 포함한 전역 집계."""
    specs = _specs(None)
    assert specs["examples"] == f"SELECT COUNT(*) FROM {EXAMPLE_TABLE}"
    assert specs["experiments"] == f"SELECT COUNT(*) FROM {EXPERIMENT_TABLE}"
    assert specs["scores"] == f"SELECT COUNT(*) FROM {SCORE_TABLE}"
    assert specs["feedback"] == f"SELECT COUNT(*) FROM {FEEDBACK_TABLE}"
    # traces는 같은 술어를 쓰되 $1이 NULL이면 전부 통과한다.
    assert "$1::text IS NULL" in specs["traces_total"]


def test_scores_and_feedback_are_reported_at_all() -> None:
    """이전 응답에는 두 지표가 아예 없어서 대시보드가 언제나 '미제공'이었다."""
    for project in ("AADS", None):
        specs = _specs(project)
        assert "scores" in specs and "feedback" in specs


def test_two_projects_produce_different_scoped_sql_parameters() -> None:
    """스코프는 SQL 문자열이 아니라 파라미터로 들어간다 (인젝션/캐시 안전)."""
    aads = _specs("AADS")
    go100 = _specs("GO100")
    assert aads == go100, "프로젝트 이름이 SQL에 박히면 파라미터화가 아니다"
    for sql in aads.values():
        assert "AADS" not in sql and "GO100" not in sql


# ── 부분 적용 (partial migration) ───────────────────────────────────────────


def test_a_missing_child_table_does_not_gate_its_siblings() -> None:
    """examples가 없어도 experiments 집계는 살아있어야 한다 (기존 버그)."""
    specs = _specs("AADS", EXAMPLE_TABLE)
    assert "examples" not in specs
    assert "experiments" in specs and "datasets" in specs and "traces_total" in specs
    # example 경로만 빠지고 나머지 두 소유 경로는 남는다.
    assert specs["scores"].count("EXISTS") == 2
    assert EXAMPLE_TABLE not in specs["scores"]


def test_missing_datasets_table_drops_only_the_metrics_that_need_it() -> None:
    """datasets가 없으면 소유를 증명할 수 없는 지표만 빠진다."""
    specs = _specs("AADS", DATASET_TABLE)
    assert "datasets" not in specs
    assert "examples" not in specs and "experiments" not in specs
    assert "traces_total" in specs and "legacy_traces_total" in specs
    # score는 trace 경로만으로도 소유를 증명할 수 있다.
    assert specs["scores"].count("EXISTS") == 1
    assert TRACE_TABLE in specs["scores"]


def test_missing_traces_table_makes_scoped_feedback_unavailable_not_zero() -> None:
    """trace가 없으면 feedback의 소유를 알 수 없다 — 0이라고 말하면 거짓말이다."""
    specs = _specs("AADS", TRACE_TABLE)
    assert "feedback" not in specs
    assert "scores" in specs, "dataset 경로가 남아 있으므로 score는 셀 수 있다"

    # 전역 집계는 trace가 없어도 그냥 셀 수 있다.
    assert "feedback" in _specs(None, TRACE_TABLE)


def test_datasets_table_alone_yields_no_child_metrics() -> None:
    missing = (TRACE_TABLE, EXAMPLE_TABLE, EXPERIMENT_TABLE, SCORE_TABLE, FEEDBACK_TABLE)
    specs = _specs("AADS", *missing)
    assert set(specs) == {"datasets", "legacy_traces_total"}


# ── get_status 응답 ─────────────────────────────────────────────────────────


class ScopeConn:
    """지정한 테이블만 있는 DB. 합본 집계 쿼리에 원하는 숫자를 돌려준다."""

    def __init__(self, present, counts=None, fetchrow_error: Exception | None = None):
        self.present = set(present)
        self.counts = counts or {}
        self.fetchrow_error = fetchrow_error
        self.fetchrow_queries: list[tuple] = []
        self.fetchval_queries: list[tuple] = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query: str, *args):
        return [{"table_name": name} for name in args[0] if name in self.present]

    async def fetchrow(self, query: str, *args):
        self.fetchrow_queries.append((query, args))
        if self.fetchrow_error is not None:
            raise self.fetchrow_error
        return {name: self.counts.get(name, 0) for name in re.findall(r"\bAS (\w+)", query)}

    async def fetchval(self, query: str, *args):
        if "information_schema.tables" in query:
            return args[0] in self.present
        self.fetchval_queries.append((query, args))
        raise AssertionError("합본 경로가 살아있으면 지표별 fetchval은 쓰이지 않는다")


def _status(conn, monkeypatch, project=None) -> dict:
    from app.core import db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: conn)
    return asyncio.run(llmops_store.get_status(project=project))


def test_status_reports_scoped_counts_under_the_existing_fields(monkeypatch) -> None:
    conn = ScopeConn(
        ALL_TABLES,
        {
            "traces_total": 1,
            "traces_error": 1,
            "traces_last_24h": 1,
            "legacy_traces_total": 9,
            "datasets": 3,
            "examples": 1,
            "experiments": 4,
            "scores": 24,
            "feedback": 0,
        },
    )
    status = _status(conn, monkeypatch, project="AADS")
    db = status["db"]

    assert status["project"] == "AADS"
    assert db["available"] is True and db["foundation_ready"] is True
    assert db["project_scoped"] is True
    assert db["traces"] == {"total": 1, "error": 1, "last_24h": 1}
    assert db["legacy_traces"] == {"total": 9}
    assert (db["datasets"], db["examples"], db["experiments"]) == (3, 1, 4)
    assert (db["scores"], db["feedback"]) == (24, 0)

    # 집계는 1왕복, project는 파라미터로 전달된다.
    assert len(conn.fetchrow_queries) == 1
    assert conn.fetchrow_queries[0][1] == ("AADS",)


def test_status_counts_all_metrics_in_a_single_round_trip(monkeypatch) -> None:
    """대시보드가 주기적으로 부르는 엔드포인트다 — 지표당 1왕복으로 돌아가면 안 된다."""
    conn = ScopeConn(ALL_TABLES)
    _status(conn, monkeypatch, project="AADS")
    assert len(conn.fetchrow_queries) == 1
    assert conn.fetchval_queries == []


def test_zero_is_reported_as_zero_and_missing_as_an_absent_key(monkeypatch) -> None:
    """0건과 '셀 수 없음'은 다른 상태다. 후자를 0으로 꾸며내면 안 된다."""
    populated = ScopeConn(ALL_TABLES, {"scores": 0, "feedback": 0})
    db = _status(populated, monkeypatch, project="AADS")["db"]
    assert db["scores"] == 0 and db["feedback"] == 0

    dropped = ScopeConn(set(ALL_TABLES) - {SCORE_TABLE, FEEDBACK_TABLE})
    db = _status(dropped, monkeypatch, project="AADS")["db"]
    assert "scores" not in db and "feedback" not in db
    assert db["tables"][SCORE_TABLE] is False
    assert db["foundation_ready"] is False


def test_partial_migration_keeps_the_countable_metrics(monkeypatch) -> None:
    """없는 테이블 하나가 available=False로 전체를 무너뜨리면 안 된다."""
    conn = ScopeConn(
        set(ALL_TABLES) - {EXAMPLE_TABLE},
        {"traces_total": 7, "traces_error": 2, "traces_last_24h": 7, "datasets": 3},
    )
    db = _status(conn, monkeypatch, project="AADS")["db"]

    assert db["available"] is True
    assert db["foundation_ready"] is False
    assert db["traces"]["total"] == 7 and db["datasets"] == 3
    assert "examples" not in db


def test_one_broken_metric_does_not_take_down_the_others(monkeypatch) -> None:
    """합본 쿼리가 깨지면 지표별로 나눠 세고, 실패한 것만 뺀다."""

    class DegradingConn(ScopeConn):
        async def fetchval(self, query: str, *args):
            if "information_schema.tables" in query:
                return args[0] in self.present
            self.fetchval_queries.append((query, args))
            if SCORE_TABLE in query:
                raise RuntimeError("column s.source_trace_id does not exist")
            return 5

    conn = DegradingConn(ALL_TABLES, fetchrow_error=RuntimeError("combined boom"))
    db = _status(conn, monkeypatch, project="AADS")["db"]

    assert db["available"] is True
    assert db["traces"] == {"total": 5, "error": 5, "last_24h": 5}
    assert db["feedback"] == 5
    assert "scores" not in db, "못 센 지표를 0으로 꾸며냈다"
    assert len(conn.fetchval_queries) == 9


class ArityConn(ScopeConn):
    """asyncpg처럼 인자 개수를 검사하는 DB.

    asyncpg는 쿼리의 `$N` 개수와 넘긴 인자 수가 다르면 InterfaceError를 낸다.
    `ScopeConn`은 그 검사를 하지 않아서, 파라미터 없는 전역 집계 SQL에 project를
    붙여 보내는 회귀를 잡지 못했다.
    """

    @staticmethod
    def _check(query: str, args: tuple) -> None:
        expected = len(set(re.findall(r"\$(\d+)", query)))
        if expected != len(args):
            raise RuntimeError(
                f"the server expects {expected} arguments for this query, {len(args)} was passed"
            )

    async def fetchrow(self, query: str, *args):
        self._check(query, args)
        return await super().fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        if "information_schema.tables" in query:
            return args[0] in self.present
        self._check(query, args)
        self.fetchval_queries.append((query, args))
        return self.counts.get(_metric_of(query), 0)


def _metric_of(sql: str) -> str:
    """지표별 재시도 경로에서 어떤 지표를 세는 중인지 SQL로 되짚는다."""
    for table, metric in (
        (SCORE_TABLE, "scores"),
        (FEEDBACK_TABLE, "feedback"),
        (EXAMPLE_TABLE, "examples"),
        (EXPERIMENT_TABLE, "experiments"),
    ):
        if table in sql:
            return metric
    return "datasets" if DATASET_TABLE in sql else "traces_total"


def test_global_metrics_without_a_placeholder_are_called_with_no_argument() -> None:
    """`$1`이 없는 SQL에 인자를 붙이면 asyncpg가 통째로 거절한다."""
    bare = f"SELECT COUNT(*) FROM {SCORE_TABLE}"
    assert llmops_store._project_args(bare, None) == ()
    scoped = f"SELECT COUNT(*) FROM {TRACE_TABLE} WHERE ($1::text IS NULL OR project = $1)"
    assert llmops_store._project_args(scoped, "AADS") == ("AADS",)
    assert llmops_store._project_args(scoped, None) == (None,)


def test_degraded_global_counts_survive_asyncpg_argument_checking(monkeypatch) -> None:
    """합본이 깨진 전역 집계에서 scores/feedback이 인자 개수 때문에 사라지면 안 된다."""
    conn = ArityConn(
        ALL_TABLES,
        {"scores": 36, "feedback": 2, "examples": 3, "experiments": 7, "traces_total": 133},
        fetchrow_error=RuntimeError("combined boom"),
    )
    db = _status(conn, monkeypatch, project=None)["db"]

    assert db["available"] is True
    # 파라미터가 없는 네 지표가 전부 살아있어야 한다.
    assert (db["scores"], db["feedback"]) == (36, 2)
    assert (db["examples"], db["experiments"]) == (3, 7)
    assert db["traces"]["total"] == 133


def test_a_partially_migrated_global_db_with_no_scoped_table_still_counts(monkeypatch) -> None:
    """scores/feedback만 남은 DB는 합본 쿼리에 `$1`이 하나도 없다."""
    conn = ArityConn({SCORE_TABLE, FEEDBACK_TABLE}, {"scores": 36, "feedback": 2})
    db = _status(conn, monkeypatch, project=None)["db"]

    assert db["available"] is True and db["foundation_ready"] is False
    assert (db["scores"], db["feedback"]) == (36, 2)
    # 합본 쿼리 한 번으로 끝났고, 인자는 붙이지 않았다.
    assert conn.fetchrow_queries[0][1] == ()
    assert conn.fetchval_queries == []


def test_status_is_unavailable_when_the_pool_is_down(monkeypatch) -> None:
    """DB 자체가 없으면 available=False + 사유. 0으로 채우면 안 된다."""
    from app.core import db_pool

    def _boom():
        raise RuntimeError("pool is closed")

    monkeypatch.setattr(db_pool, "get_pool", _boom)
    status = asyncio.run(llmops_store.get_status(project="AADS"))

    assert status["db"] == {"available": False, "error": "pool is closed"}
    assert status["project"] == "AADS"
    assert "traces" not in status["db"] and "scores" not in status["db"]


def test_global_status_still_reports_untouched_totals(monkeypatch) -> None:
    conn = ScopeConn(ALL_TABLES, {"traces_total": 117, "datasets": 4, "scores": 30})
    db = _status(conn, monkeypatch, project=None)["db"]

    assert db["project_scoped"] is False
    assert db["traces"]["total"] == 117
    assert db["datasets"] == 4 and db["scores"] == 30
    assert conn.fetchrow_queries[0][1] == (None,)
