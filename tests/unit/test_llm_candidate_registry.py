"""M9 후보 모델 대장 게이트 회귀시험.

DB 게이트(migrations/20260930_llm_m9_candidate_registry.sql)와 서비스 사본
(app/services/llm_candidate_registry.py)이 같은 규칙을 지키는지 본다.
"""
import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import llm_candidate_registry as reg

ROOT = Path(__file__).resolve().parents[2]
UP_SQL = ROOT / "migrations" / "20260930_llm_m9_candidate_registry.sql"
DOWN_SQL = ROOT / "migrations" / "rollback" / "20260930_llm_m9_candidate_registry.down.sql"

# 지시서 "이미 확보한 공식 자료" — https://dev.meta.ai/docs/pricing-rate-limits (2026-09-30 KST)
OFFICIAL = {
    "muse-spark-1.3": {
        "price_input_per_1m": "1.25", "price_cached_input_per_1m": "0.15",
        "price_output_per_1m": "4.25", "quota_rpm": "3000", "quota_tpm": "4000000",
        "training_use": "not_used", "price_status": "official_verified",
        "pricing_kind": "api_per_token", "excluded": "false",
    },
    "muse-spark-1.3-contributor": {
        "price_input_per_1m": "0.10", "price_cached_input_per_1m": "0.002",
        "price_output_per_1m": "0.20", "quota_rpm": "100", "quota_tpm": "3000000",
        "training_use": "used_for_training", "price_status": "official_verified",
        "pricing_kind": "api_per_token", "excluded": "true",
    },
    "muse-code": {
        "price_input_per_1m": "NULL", "price_cached_input_per_1m": "NULL",
        "price_output_per_1m": "NULL", "quota_rpm": "NULL", "quota_tpm": "NULL",
        "subscription_price_per_month": "NULL",
        "training_use": "unknown", "price_status": "unverified_official",
        "pricing_kind": "subscription", "excluded": "false",
    },
}

_SEED_COLUMNS = [
    "provider", "model_id", "product_name", "surface_scope", "official_url", "verified_at", "model_version",
    "region_scope", "quota_rpm", "quota_tpm",
    "price_input_per_1m", "price_cached_input_per_1m", "price_output_per_1m", "price_currency",
    "subscription_price_per_month", "price_status", "pricing_kind",
    "training_use", "training_terms_url", "retention_note", "extra_charges",
    "excluded_from_private_eval", "exclusion_reason", "status", "notes",
]


def _split_top_level(values: str):
    """VALUES ( ... ) 본문을 최상위 쉼표로 나눈다. 따옴표/괄호 안의 쉼표는 무시."""
    out, buf, depth, quoted = [], [], 0, False
    for ch in values:
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch in "([":
            depth += 1
        elif not quoted and ch in ")]":
            depth -= 1
        elif not quoted and ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf).strip())
    return out


def _seed_rows():
    rows = {}
    for block in UP_SQL.read_text(encoding="utf-8").split("INSERT INTO llm_model_candidates")[1:]:
        body = block.split(") VALUES (", 1)[1].split(") ON CONFLICT", 1)[0]
        cols = _split_top_level(block.split(") VALUES (", 1)[0].split("(", 1)[1])
        assert cols == _SEED_COLUMNS
        vals = dict(zip(cols, _split_top_level(body)))
        rows[vals["model_id"].strip("'")] = vals
    return rows


# ── 시드 ────────────────────────────────────────────────────────────────────

def test_seed_has_exactly_three_muse_rows():
    rows = _seed_rows()
    assert set(rows) == set(OFFICIAL)
    assert all(r["provider"] == "'meta'" for r in rows.values())


@pytest.mark.parametrize("model_id", sorted(OFFICIAL))
def test_seed_prices_match_official_values(model_id):
    row = _seed_rows()[model_id]
    want = OFFICIAL[model_id]
    for col in ("price_input_per_1m", "price_cached_input_per_1m", "price_output_per_1m", "quota_rpm", "quota_tpm"):
        if want[col] == "NULL":
            assert row[col] == "NULL", col
        else:
            assert Decimal(row[col]) == Decimal(want[col]), col
    assert row["training_use"] == f"'{want['training_use']}'"
    assert row["price_status"] == f"'{want['price_status']}'"
    assert row["pricing_kind"] == f"'{want['pricing_kind']}'"
    assert row["excluded_from_private_eval"] == want["excluded"]


def test_muse_code_subscription_price_is_null_and_unverified():
    row = _seed_rows()["muse-code"]
    assert row["subscription_price_per_month"] == "NULL"
    assert row["price_status"] == "'unverified_official'"


def test_contributor_seed_exclusion_reason():
    row = _seed_rows()["muse-spark-1.3-contributor"]
    assert row["exclusion_reason"] == "'입력/출력이 Meta 모델 학습에 사용됨 — 비공개 코드·운영/고객 데이터 평가 기본 제외'"


def test_seed_rows_pass_service_gate():
    for model_id, vals in _seed_rows().items():
        row = {
            "pricing_kind": vals["pricing_kind"].strip("'"),
            "price_status": vals["price_status"].strip("'"),
            "training_use": vals["training_use"].strip("'"),
            "official_url": vals["official_url"].strip("'"),
            "verified_at": vals["verified_at"].strip("'"),
            "surface_scope": [s.strip("'") for s in vals["surface_scope"][6:-1].split(",")],
        }
        for col in ("price_input_per_1m", "price_cached_input_per_1m", "price_output_per_1m",
                    "subscription_price_per_month"):
            row[col] = None if vals[col] == "NULL" else Decimal(vals[col])
        assert reg.normalize_candidate(row)["pricing_kind"] == OFFICIAL[model_id]["pricing_kind"]


def test_no_incumbent_seeded_and_llm_models_untouched():
    sql = UP_SQL.read_text(encoding="utf-8")
    assert "INSERT INTO llm_models" not in sql
    assert "UPDATE llm_models" not in sql
    assert "ALTER TABLE llm_models" not in sql


# ── 학습 사용 → 비공개 평가 자동 제외 ───────────────────────────────────────

def test_used_for_training_forces_private_eval_exclusion():
    row = reg.normalize_candidate({
        "pricing_kind": "api_per_token", "training_use": "used_for_training",
        "excluded_from_private_eval": False,
    })
    assert row["excluded_from_private_eval"] is True
    assert row["exclusion_reason"] == reg.DEFAULT_EXCLUSION_REASON


def test_used_for_training_keeps_explicit_reason():
    row = reg.normalize_candidate({
        "pricing_kind": "api_per_token", "training_use": "used_for_training",
        "exclusion_reason": "custom",
    })
    assert row["excluded_from_private_eval"] is True and row["exclusion_reason"] == "custom"


def test_not_used_does_not_force_exclusion():
    row = reg.normalize_candidate({"pricing_kind": "api_per_token", "training_use": "not_used"})
    assert not row.get("excluded_from_private_eval")


def test_db_trigger_and_check_enforce_exclusion():
    sql = UP_SQL.read_text(encoding="utf-8")
    assert "BEFORE INSERT OR UPDATE ON llm_model_candidates" in sql
    assert "NEW.excluded_from_private_eval := true" in sql
    assert "CHECK (training_use <> 'used_for_training' OR excluded_from_private_eval)" in sql


# ── 비교: 표본 미달 / 비열등 한계 ────────────────────────────────────────────

def _cmp(**kw):
    base = {"surface": "runner", "min_sample_size": 50, "sample_size": 100,
            "noninferiority_margin": {"pass_rate_delta": -0.02}, "verdict": "equivalent"}
    base.update(kw)
    return base


@pytest.mark.parametrize("verdict", ["equivalent", "candidate_better", "candidate_worse"])
def test_undersampled_conclusion_rejected(verdict):
    with pytest.raises(reg.CandidateRegistryError, match="insufficient_sample"):
        reg.validate_comparison(_cmp(sample_size=49, verdict=verdict))


def test_undersampled_insufficient_sample_allowed():
    assert reg.validate_comparison(_cmp(sample_size=10, verdict="insufficient_sample"))["verdict"] == "insufficient_sample"


def test_enough_samples_equivalent_allowed():
    assert reg.validate_comparison(_cmp(sample_size=50))["verdict"] == "equivalent"


@pytest.mark.parametrize("margin", [None, {}, "", "{}"])
def test_verdict_without_margin_rejected(margin):
    with pytest.raises(reg.CandidateRegistryError, match="noninferiority_margin"):
        reg.validate_comparison(_cmp(noninferiority_margin=margin))


def test_pending_row_without_margin_allowed():
    assert reg.validate_comparison(_cmp(noninferiority_margin=None, verdict=None))["verdict"] is None


def test_min_sample_size_must_be_declared():
    with pytest.raises(reg.CandidateRegistryError, match="min_sample_size"):
        reg.validate_comparison(_cmp(min_sample_size=None))


def test_db_checks_for_comparisons_present():
    sql = UP_SQL.read_text(encoding="utf-8")
    assert "ck_llm_model_comparisons_margin_first" in sql
    assert "ck_llm_model_comparisons_min_sample" in sql
    assert "OR verdict IN ('insufficient_sample','not_run')" in sql


# ── 금액축 분리 ─────────────────────────────────────────────────────────────

def test_mixed_pricing_kind_comparison_rejected():
    rows = [
        {"model_id": "a", "pricing_kind": "api_per_token", "price_input_per_1m": Decimal("1.25")},
        {"model_id": "b", "pricing_kind": "subscription", "subscription_price_per_month": None},
    ]
    with pytest.raises(reg.PriceAxisMismatch):
        reg.ensure_same_price_axis(rows)
    with pytest.raises(reg.PriceAxisMismatch):
        reg.sort_by_price(rows)


def test_same_pricing_kind_sorted_by_price():
    rows = [
        {"model_id": "std", "pricing_kind": "api_per_token", "price_input_per_1m": Decimal("1.25")},
        {"model_id": "contrib", "pricing_kind": "api_per_token", "price_input_per_1m": Decimal("0.10")},
    ]
    assert [r["model_id"] for r in reg.sort_by_price(rows)] == ["contrib", "std"]


def test_row_cannot_mix_subscription_and_token_prices():
    with pytest.raises(reg.PriceAxisMismatch):
        reg.normalize_candidate({"pricing_kind": "subscription", "price_input_per_1m": Decimal("1")})
    with pytest.raises(reg.PriceAxisMismatch):
        reg.normalize_candidate({"pricing_kind": "api_per_token", "subscription_price_per_month": Decimal("20")})


def test_api_rejects_price_sort_without_pricing_kind():
    from app.api import ops

    with pytest.raises(HTTPException) as exc:
        asyncio.run(ops.ops_llm_candidates(surface=None, pricing_kind=None, status=None, sort="price"))
    assert exc.value.status_code == 400


# ── 조회 API ─────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows

    async def close(self):
        return None


def test_candidates_api_exposes_price_status_and_training_use(monkeypatch):
    from app.api import ops

    conn = _Conn([
        {"id": 3, "provider": "meta", "model_id": "muse-code", "pricing_kind": "subscription",
         "price_status": "unverified_official", "training_use": "unknown",
         "subscription_price_per_month": None, "surface_scope": ["terminal_cli"],
         "extra_charges": '{"plans": ["Everyday Usage"]}', "excluded_from_private_eval": False},
        {"id": 2, "provider": "meta", "model_id": "muse-spark-1.3-contributor", "pricing_kind": "api_per_token",
         "price_status": "official_verified", "training_use": "used_for_training",
         "price_input_per_1m": Decimal("0.100000"), "surface_scope": ["chat"],
         "extra_charges": "{}", "excluded_from_private_eval": True},
    ])

    async def _get_conn():
        return conn

    monkeypatch.setattr(ops, "_get_conn", _get_conn)
    out = asyncio.run(ops.ops_llm_candidates(surface=None, pricing_kind=None, status=None, sort=None))
    assert out["count"] == 2
    code, contrib = out["items"]
    assert code["price_status"] == "unverified_official" and code["price_verified"] is False
    assert code["subscription_price_per_month"] is None
    assert code["extra_charges"] == {"plans": ["Everyday Usage"]}
    assert contrib["training_use"] == "used_for_training" and contrib["excluded_from_private_eval"] is True
    assert contrib["price_input_per_1m"] == "0.100000" and contrib["price_verified"] is True


def test_comparisons_api_includes_candidate_price_status_and_training_use(monkeypatch):
    from app.api import ops

    conn = _Conn([{"id": 1, "surface": "runner", "verdict": "insufficient_sample",
                   "price_status": "official_verified", "training_use": "not_used",
                   "noninferiority_margin": '{"pass_rate_delta": -0.02}', "evidence_refs": []}])

    async def _get_conn():
        return conn

    monkeypatch.setattr(ops, "_get_conn", _get_conn)
    out = asyncio.run(ops.ops_llm_comparisons(surface="runner", since=None, until=None, limit=50))
    item = out["items"][0]
    assert item["price_status"] == "official_verified" and item["training_use"] == "not_used"
    assert item["noninferiority_margin"] == {"pass_rate_delta": -0.02}
    sql, _args = conn.calls[0]
    assert "c.price_status" in sql and "c.training_use" in sql and "DESC" in sql


# ── 마이그레이션 쌍 ─────────────────────────────────────────────────────────

def test_down_migration_drops_both_tables_and_function():
    down = DOWN_SQL.read_text(encoding="utf-8")
    assert down.index("DROP TABLE IF EXISTS llm_model_comparisons") < down.index("DROP TABLE IF EXISTS llm_model_candidates")
    assert "DROP FUNCTION IF EXISTS llm_model_candidates_enforce_exclusion()" in down
    assert "llm_models " not in down.replace("llm_models 는", "")
