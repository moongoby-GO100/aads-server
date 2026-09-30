"""AADS-COST-CATALOG-MODELNAME-NORMALIZE-BACKFILL-20260930.

oauth_usage_log.cost_usd_catalog 가 NULL 이던 두 원인의 회귀시험.
  A. Codex 표시명("GPT-6 Sol (Codex CLI)")이 model 컬럼에 들어가 model_id 조인이 깨졌다.
  B. 마이그레이션 이전 행이 백필되지 않았다.

DB 시험은 운영 DB 를 건드리지 않는다. aads-postgres 컨테이너에 임시 DB 를 만들어
마이그레이션 파일 자체를 적용하고 지운다. 컨테이너를 못 쓰는 환경(단위시험 이미지)에서는 건너뛴다.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UP = ROOT / "migrations" / "20260930_oauth_usage_catalog_model_normalize_backfill.sql"
DOWN = ROOT / "migrations" / "rollback" / "20260930_oauth_usage_catalog_model_normalize_backfill.down.sql"
BASE_MIGRATION = ROOT / "migrations" / "20260930_oauth_usage_cost_basis.sql"
TAG = "model_normalize_backfill_20260930"

DISPLAY_TO_ID = {
    "GPT-6 Sol (Codex CLI)": "gpt-6-sol",
    "GPT-6 Astra (Codex CLI)": "gpt-6-astra",
    "GPT-5.6 Sol (Codex CLI)": "gpt-5.6-sol",
}


def _load_runner_helper():
    spec = importlib.util.spec_from_file_location("runner_cli_usage_norm", ROOT / "scripts" / "runner_cli_usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 기록 경로: 표시명이 model 컬럼에 다시 들어가지 않는다 ─────────────────

@pytest.mark.parametrize("display,model_id", DISPLAY_TO_ID.items())
def test_codex_relay_canonical_id_from_display_name(display, model_id):
    from app.services.model_selector import _canonical_codex_model_id

    assert _canonical_codex_model_id(display) == model_id
    assert _canonical_codex_model_id(model_id) == model_id


def test_codex_relay_usage_row_records_model_id_not_display_name():
    src = (ROOT / "app" / "services" / "model_selector.py").read_text(encoding="utf-8")
    start = src.index('call_source="codex_relay"')
    call = src[src.rindex("_log_oauth_usage(", 0, start):start]
    assert "model=_canonical_codex_model_id(model)" in call
    assert "model=display_model" not in call


@pytest.mark.parametrize("display,model_id", DISPLAY_TO_ID.items())
def test_runner_helper_normalizes_display_name(display, model_id):
    mod = _load_runner_helper()
    assert mod.canonical_model_id(display) == model_id
    assert mod.canonical_model_id("codex:" + model_id) == model_id
    sql = mod.build_insert_sql(
        [mod.unmeasured_row(display)], job_id="j1", call_source="runner_codex_cli", account_slot="codex"
    )
    assert "'%s'" % model_id in sql
    assert display not in sql
    assert "llm_catalog_cost_usd('%s', 0, 0)" % model_id in sql


@pytest.mark.parametrize("name", ["gpt-5.6-sol", "gpt-5.6-luna", "claude-opus-5", "claude-haiku-4-5-20251001", "unknown", ""])
def test_runner_helper_leaves_plain_ids_alone(name):
    mod = _load_runner_helper()
    assert mod.canonical_model_id(name) == name


# ── 마이그레이션 정적 계약 ────────────────────────────────────────────────

def test_migration_pair_and_safety():
    up = UP.read_text(encoding="utf-8")
    down = DOWN.read_text(encoding="utf-8")
    for bad in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert bad not in up.upper()
    assert "FUNCTION public.llm_resolve_model_id" in up
    assert "FUNCTION public.llm_catalog_cost_usd" in up
    assert TAG in up and TAG in down
    assert re.search(r"SET\s+cost_usd_catalog\s*=\s*NULL", down)
    # 백필 표지는 별도 열이다. cost_usd 의 출처인 cost_source 는 갱신하지 않는다.
    assert not re.search(r"SET[^;]*\bcost_source\s*=", up, re.IGNORECASE | re.DOTALL)
    # 단가가 없으면 0 이 아니라 NULL 로 남긴다.
    assert "r.in_rate IS NOT NULL" in up and "r.out_rate IS NOT NULL" in up
    assert "COALESCE(public.llm_resolve_model_id" in up


def test_base_migration_still_states_null_not_zero():
    base = BASE_MIGRATION.read_text(encoding="utf-8")
    assert "모델이 카탈로그에 없거나 단가가 비어 있으면 NULL" in base


# ── DB 시험: 임시 DB 에 마이그레이션 파일을 그대로 적용 ─────────────────────

_PG = ["docker", "exec", "-i", "aads-postgres", "psql", "-U", "aads", "-v", "ON_ERROR_STOP=1", "-q", "-At"]


def _pg_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        out = subprocess.run(
            ["docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "postgres", "-Atc", "select 1"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "1"


def _psql(db: str, sql: str) -> str:
    r = subprocess.run(_PG + ["-d", db], input=sql, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


_FIXTURE = """
CREATE TABLE llm_models (id serial primary key, provider varchar(50), model_id varchar(150),
    display_name varchar(200), input_cost numeric(12,6), output_cost numeric(12,6), is_active boolean);
INSERT INTO llm_models (provider, model_id, display_name, input_cost, output_cost, is_active) VALUES
 ('codex','gpt-6-sol','GPT-6 Sol (Codex CLI)',2,10,true),
 ('openai','gpt-6-sol','GPT-6 Sol',2,10,true),
 ('codex','gpt-6-astra','GPT-6 Astra (Codex CLI)',10,50,true),
 ('codex','gpt-5.6-sol','GPT-5.6 Sol (Codex CLI)',4,20,true),
 ('openai','gpt-5.6-sol','GPT-5.6 Sol (Codex CLI)',4,20,true),
 ('anthropic','claude-haiku-4-5-20251001','Claude Haiku 4.5',1,5,true),
 ('litellm','claude-haiku-4-5-20251001','Claude Haiku 4.5 (litellm)',NULL,NULL,true),
 ('litellm','no-price-model','No Price Model',NULL,NULL,true);
CREATE TABLE oauth_usage_log (id bigserial primary key, model varchar(60) not null,
    input_tokens int not null default 0, output_tokens int not null default 0,
    cost_usd numeric(10,6), cost_source varchar(20) not null default 'unknown',
    cost_usd_catalog numeric);
"""

_OLD_FUNCTION = """
CREATE FUNCTION public.llm_catalog_cost_usd(p_model TEXT, p_input_tokens BIGINT, p_output_tokens BIGINT)
RETURNS NUMERIC LANGUAGE sql STABLE AS $$
    SELECT ROUND((COALESCE(p_input_tokens, 0) * m.input_cost + COALESCE(p_output_tokens, 0) * m.output_cost) / 1000000.0, 6)
      FROM llm_models m
     WHERE m.model_id = split_part(COALESCE(p_model, ''), '[', 1)
       AND m.input_cost IS NOT NULL AND m.output_cost IS NOT NULL
     ORDER BY (m.provider = 'anthropic') DESC, m.is_active DESC NULLS LAST, m.id LIMIT 1
$$;
"""

_ROWS = """
INSERT INTO oauth_usage_log (model, input_tokens, output_tokens, cost_usd, cost_source) VALUES
 ('GPT-6 Sol (Codex CLI)', 1000000, 0, 2.0, 'catalog_estimated'),
 ('GPT-6 Astra (Codex CLI)', 0, 1000000, 50.0, 'catalog_estimated'),
 ('GPT-5.6 Sol (Codex CLI)', 1000000, 1000000, 24.0, 'catalog_estimated'),
 ('claude-haiku-4-5-20251001', 1000000, 1000000, 0.5, 'relay_reported'),
 ('gpt-5.6-sol', 1000000, 0, NULL, 'unknown'),
 ('no-price-model', 100, 100, NULL, 'unknown'),
 ('not-in-catalog', 100, 100, NULL, 'unknown');
"""


@pytest.fixture()
def scratch_db():
    if not _pg_available():
        pytest.skip("aads-postgres 컨테이너를 쓸 수 없어 DB 시험을 건너뛴다")
    name = "aads_bf_test_" + uuid.uuid4().hex[:8]
    _psql("postgres", "CREATE DATABASE %s" % name)
    try:
        _psql(name, _FIXTURE + _OLD_FUNCTION + _ROWS)
        yield name
    finally:
        subprocess.run(
            ["docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "postgres", "-c",
             "DROP DATABASE IF EXISTS %s WITH (FORCE)" % name],
            capture_output=True, text=True, timeout=60,
        )


def _apply(db: str, path: Path) -> None:
    r = subprocess.run(_PG + ["-d", db], input=path.read_text(encoding="utf-8"),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_db_display_names_resolve_and_price(scratch_db):
    _apply(scratch_db, UP)
    got = _psql(scratch_db, "SELECT " + ", ".join(
        "llm_resolve_model_id('%s')" % d for d in DISPLAY_TO_ID) + ";")
    assert got.split("|") == list(DISPLAY_TO_ID.values())
    assert _psql(scratch_db, "SELECT llm_catalog_cost_usd('GPT-6 Sol (Codex CLI)', 1000000, 0)") == "2.000000"
    assert _psql(scratch_db, "SELECT llm_catalog_cost_usd('GPT-6 Astra (Codex CLI)', 0, 1000000)") == "50.000000"
    assert _psql(scratch_db, "SELECT llm_catalog_cost_usd('GPT-5.6 Sol (Codex CLI)', 1000000, 1000000)") == "24.000000"


def test_db_boundary_forms_and_exact_match_kept(scratch_db):
    _apply(scratch_db, UP)
    q = lambda s: _psql(scratch_db, "SELECT " + s)  # noqa: E731
    assert q("llm_catalog_cost_usd('gpt-5.6-sol', 1000000, 0)") == "4.000000"
    assert q("llm_catalog_cost_usd('gpt-5.6-sol[1m]', 1000000, 0)") == "4.000000"
    assert q("llm_resolve_model_id('gpt-6 sol (codex cli)')") == "gpt-6-sol"
    assert q("llm_resolve_model_id('codex:gpt-6-sol')") == "gpt-6-sol"
    # 정확일치가 있으면 그대로다(단가 있는 anthropic 행 선택).
    assert q("llm_catalog_cost_usd('claude-haiku-4-5-20251001', 1000000, 1000000)") == "6.000000"


def test_db_unknown_or_unpriced_stays_null_not_zero(scratch_db):
    _apply(scratch_db, UP)
    for model in ("not-in-catalog", "no-price-model", "", "GPT-9 Nova (Codex CLI)"):
        assert _psql(scratch_db, "SELECT llm_catalog_cost_usd('%s', 100, 100) IS NULL" % model) == "t"
    assert _psql(scratch_db, "SELECT llm_resolve_model_id('not-in-catalog') IS NULL") == "t"


def test_db_backfill_tags_only_priced_rows_and_keeps_cost_source(scratch_db):
    before = _psql(scratch_db, "SELECT string_agg(id||cost_source, ',' ORDER BY id) FROM oauth_usage_log")
    _apply(scratch_db, UP)
    assert _psql(scratch_db, "SELECT string_agg(id||cost_source, ',' ORDER BY id) FROM oauth_usage_log") == before
    tagged = _psql(scratch_db, "SELECT model||'='||cost_usd_catalog FROM oauth_usage_log "
                               "WHERE catalog_backfill_tag = '%s' ORDER BY id" % TAG)
    assert tagged.splitlines() == [
        "GPT-6 Sol (Codex CLI)=2.000000",
        "GPT-6 Astra (Codex CLI)=50.000000",
        "GPT-5.6 Sol (Codex CLI)=24.000000",
        "claude-haiku-4-5-20251001=6.000000",
        "gpt-5.6-sol=4.000000",
    ]
    assert _psql(scratch_db, "SELECT string_agg(model, ',' ORDER BY id) FROM oauth_usage_log "
                             "WHERE cost_usd_catalog IS NULL") == "no-price-model,not-in-catalog"
    assert _psql(scratch_db, "SELECT count(*) FROM oauth_usage_log WHERE cost_usd_catalog = 0") == "0"


def test_db_backfill_is_idempotent_and_does_not_overwrite_existing_catalog(scratch_db):
    _psql(scratch_db, "UPDATE oauth_usage_log SET cost_usd_catalog = 99 WHERE model = 'gpt-5.6-sol'")
    _apply(scratch_db, UP)
    assert _psql(scratch_db, "SELECT cost_usd_catalog FROM oauth_usage_log WHERE model = 'gpt-5.6-sol'") == "99"
    snap = _psql(scratch_db, "SELECT string_agg(coalesce(cost_usd_catalog::text,'n'), ',' ORDER BY id) FROM oauth_usage_log")
    _apply(scratch_db, UP)
    assert _psql(scratch_db, "SELECT string_agg(coalesce(cost_usd_catalog::text,'n'), ',' ORDER BY id) FROM oauth_usage_log") == snap


def test_db_rollback_restores_tagged_rows_only(scratch_db):
    _psql(scratch_db, "UPDATE oauth_usage_log SET cost_usd_catalog = 99 WHERE model = 'gpt-5.6-sol'")
    _apply(scratch_db, UP)
    _apply(scratch_db, DOWN)
    assert _psql(scratch_db, "SELECT count(*) FROM oauth_usage_log WHERE cost_usd_catalog IS NOT NULL") == "1"
    assert _psql(scratch_db, "SELECT cost_usd_catalog FROM oauth_usage_log WHERE model = 'gpt-5.6-sol'") == "99"
    assert _psql(scratch_db, "SELECT to_regprocedure('llm_resolve_model_id(text)') IS NULL") == "t"
    # 옛 정확일치 함수로 돌아온다.
    assert _psql(scratch_db, "SELECT llm_catalog_cost_usd('GPT-6 Sol (Codex CLI)', 1000000, 0) IS NULL") == "t"
    assert _psql(scratch_db, "SELECT llm_catalog_cost_usd('gpt-6-sol', 1000000, 0)") == "2.000000"
