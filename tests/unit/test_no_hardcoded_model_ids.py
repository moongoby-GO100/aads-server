"""모델 ID 하드코딩 회귀 방지 + 역할 표(llm_model_roles) 조회 fail-safe 검사."""
import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import model_roles
from app.services.model_roles_seed import ROLE_SEED

ROOT = Path(__file__).resolve().parents[2]
MODEL_ID_RE = re.compile(r"(claude|gpt|gemini|codex|deepseek|kimi|o[0-9])-[a-z0-9.-]+")
# 리뷰 지적 2: 지시서 정규식에 없던 qwen-* 도 chat_service 에 고정돼 있었다 — 같은 검사에 포함한다.
EXTRA_MODEL_ID_RE = re.compile(r"\bqwen[0-9.]*-[a-z0-9.-]+")
TARGETS = [
    "app/services/model_selector.py",
    "app/services/chat_service.py",
    "app/services/pipeline_runner_service.py",
    "app/services/loop_executor.py",
    "app/core/anthropic_client.py",
]
# 모델 ID 가 아니라 파일 경로·URL 경로 조각인 문자열(허용 목록). 시드는 model_roles_seed.py 에 있다.
ALLOWED_NON_MODEL_STRINGS = {
    "/app/scripts/claude-oauth-wrapper.sh",
    "/codex-stream",
    "{}/codex-usage",
}


def _docstring_ids(tree: ast.AST) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


@pytest.mark.parametrize("rel", TARGETS)
def test_no_model_id_string_literals(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    docs = _docstring_ids(tree)
    hits = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docs
        and node.value not in ALLOWED_NON_MODEL_STRINGS
        and (MODEL_ID_RE.search(node.value) or EXTRA_MODEL_ID_RE.search(node.value))
    ]
    assert hits == [], f"{rel}: 모델 ID 문자열 리터럴 {len(hits)}건 — DB 역할 표(llm_model_roles)로 옮겨라: {hits[:5]}"


def test_migration_seed_matches_code_seed():
    sql = (ROOT / "migrations/20261111_llm_model_roles.sql").read_text(encoding="utf-8")
    for marker in ("대상:", "조건:", "금지:", "롤백:", "lock_timeout", "statement_timeout"):
        assert marker in sql
    assert "BEGIN;" not in sql, "적용기 트랜잭션과 충돌 방지를 위해 BEGIN 금지"
    assert "COMMIT;" not in sql, "적용기 트랜잭션과 충돌 방지를 위해 COMMIT 금지"
    assert "SET LOCAL lock_timeout" in sql
    assert "SET LOCAL statement_timeout" in sql

    rollback_path = ROOT / "migrations/rollback/20261111_llm_model_roles.down.sql"
    assert rollback_path.exists()
    rollback_sql = rollback_path.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS llm_model_roles" in rollback_sql
    assert "백업" in rollback_sql
    assert "코드" in rollback_sql

    # (item_key, model_id, priority, attrs, role_key) 순서 파싱 (gitleaks 오탐 회피 열 순서)
    rows = re.findall(
        r"^  \('((?:[^']|'')*)', '((?:[^']|'')*)', (\d+), '((?:[^']|'')*)'::jsonb, '((?:[^']|'')*)'\)",
        sql,
        re.MULTILINE,
    )
    sql_keys = {(rk.replace("''", "'"), ik.replace("''", "'"), mid.replace("''", "'")) for ik, mid, _p, _a, rk in rows}
    seed_keys = {(role, ik, mid) for role, items in ROLE_SEED.items() for ik, mid, _p, _a in items}
    assert sql_keys == seed_keys
    total_seed_rows = sum(len(items) for items in ROLE_SEED.values())
    assert len(rows) == total_seed_rows


def test_model_roles_read_columns_match_migration():
    """요구사항 4: model_roles 가 읽는 열 이름 == 마이그레이션 열."""
    sql = (ROOT / "migrations/20261111_llm_model_roles.sql").read_text(encoding="utf-8")

    select_match = re.search(r"SELECT\s+(.*?)\s+FROM\s+llm_model_roles", model_roles._SELECT_SQL, re.IGNORECASE)
    assert select_match, "model_roles._SELECT_SQL 에서 SELECT 절을 찾을 수 없음"
    read_columns = [c.strip().lower() for c in select_match.group(1).split(",")]

    table_match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?llm_model_roles\s*\((.*?)\);",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert table_match, "마이그레이션 SQL 에서 CREATE TABLE llm_model_roles 를 찾을 수 없음"
    table_body = table_match.group(1)

    table_columns = []
    for line in table_body.splitlines():
        line = line.strip()
        if not line or line.upper().startswith(("PRIMARY", "UNIQUE", "CONSTRAINT", "CHECK", "FOREIGN")):
            continue
        col_name = line.split()[0].lower()
        table_columns.append(col_name)

    for col in read_columns:
        assert col in table_columns, f"model_roles 가 읽는 열 '{col}' 이 마이그레이션 정의에 없습니다 ({table_columns})"


@pytest.fixture
def roles_db(monkeypatch):
    model_roles.reset_for_tests()
    conn = Mock()
    conn.fetch = AsyncMock()
    pool = Mock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)

    async def _no_conn():
        raise RuntimeError("test: no direct connection")

    monkeypatch.setattr(model_roles, "_fetch_with_own_connection", _no_conn)
    yield conn.fetch
    model_roles.reset_for_tests()


def _row(role, item="", model="", pri=0, attrs=None):
    return {"role_key": role, "item_key": item, "model_id": model, "priority": pri, "attrs": attrs or {}}


@pytest.mark.asyncio
async def test_failsafe_order_seed_then_db_then_last_success(roles_db):
    seed_default = ROLE_SEED["default_chat_model"][0][1]
    roles_db.side_effect = RuntimeError("db down")
    assert await model_roles.refresh_roles(force=True) is False
    assert model_roles.role_scalar("default_chat_model") == seed_default  # DB 실패 → 코드 시드

    roles_db.side_effect = None
    roles_db.return_value = [_row("default_chat_model", model="db-chosen-model")]
    assert await model_roles.refresh_roles(force=True) is True
    assert model_roles.role_scalar("default_chat_model") == "db-chosen-model"
    # DB 성공인데 해당 역할 행이 없으면 코드 목록으로 우회하지 않는다.
    assert model_roles.role_list("codex_models") == []
    assert "gpt-5.5" not in model_roles.live_set("codex_models")

    roles_db.side_effect = RuntimeError("db down again")
    assert await model_roles.refresh_roles(force=True) is False
    assert model_roles.role_scalar("default_chat_model") == "db-chosen-model"  # 마지막 성공 캐시 유지


@pytest.mark.asyncio
async def test_value_map_and_union(roles_db):
    roles_db.return_value = [
        _row("model_cost_per_mtok", item="m-a", attrs={"v": [1.5, 2.5]}),
        _row("gemini_models", model="g-1"),
        _row("groq_models", model="q-1"),
    ]
    await model_roles.refresh_roles(force=True)
    assert model_roles.live_value_map("model_cost_per_mtok")["m-a"] == (1.5, 2.5)
    union = model_roles.live_union(model_roles.live_set("gemini_models"), model_roles.live_set("groq_models"))
    assert union == {"g-1", "q-1"}
    assert "g-1" in union and "zzz" not in union


def test_selector_constants_follow_snapshot():
    model_roles.reset_for_tests()
    from app.services import model_selector

    assert "gpt-5.5" in model_selector._CODEX_MODELS  # 시드 폴백
    assert model_selector._COST_MAP["claude-opus"] == (4.0, 20.0)


def _patch_own_conn(monkeypatch, fetched=None, error=None):
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        if error:
            raise error
        return fetched

    monkeypatch.setattr(model_roles, "_fetch_with_own_connection", fake_fetch)
    return calls


def test_first_access_reads_db_before_seed(monkeypatch):
    """리뷰 지적 1: 스냅샷이 없는 첫 읽기도 DB 를 먼저 조회한다(루프 유무 무관)."""
    model_roles.reset_for_tests()
    calls = _patch_own_conn(monkeypatch, fetched=[_row("default_chat_model", model="db-first")])
    assert model_roles.role_scalar("default_chat_model") == "db-first"
    # DB 성공 + 해당 역할 비어 있음 → 시드로 우회하지 않는다.
    assert model_roles.role_list("codex_models") == []
    assert calls["n"] == 1
    model_roles.reset_for_tests()


@pytest.mark.asyncio
async def test_first_access_inside_running_loop_reads_db(monkeypatch):
    model_roles.reset_for_tests()
    _patch_own_conn(monkeypatch, fetched=[])
    # 루프 스레드에서 적재 전 동기 조회는 차단 대기 대신 예외, async API 는 DB 를 읽는다.
    with pytest.raises(model_roles.ModelRolesNotLoadedError):
        model_roles.role_list("codex_models")
    assert await model_roles.role_list_async("codex_models") == []  # DB 성공(빈 표)
    assert model_roles.role_list("codex_models") == []
    model_roles.reset_for_tests()


def test_first_access_db_failure_uses_seed_then_backs_off(monkeypatch):
    model_roles.reset_for_tests()
    calls = _patch_own_conn(monkeypatch, error=RuntimeError("db down"))
    assert model_roles.role_scalar("default_chat_model") == ROLE_SEED["default_chat_model"][0][1]
    model_roles.role_scalar("default_chat_model")
    assert calls["n"] == 1  # 실패 직후 재시도 폭주 없음
    model_roles.reset_for_tests()


def test_qwen_defaults_come_from_roles():
    """리뷰 지적 2: 추천 보강·최종보고 재작성 기본 모델과 자동 라우팅 기본값은 DB 역할이다."""
    model_roles.reset_for_tests()
    assert model_roles.role_scalar("recommendation_synth_model")
    assert model_roles.role_scalar("final_report_rewrite_model")
    from app.services import chat_service

    assert set(model_roles.role_list("auto_routed_db_default_models")) == set(chat_service._AUTO_ROUTED_DB_DEFAULT_MODELS)
