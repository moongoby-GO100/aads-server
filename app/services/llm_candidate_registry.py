"""M9 후보 모델 대장(llm_model_candidates)·비교 이력(llm_model_comparisons).

DB(migrations/20260930_llm_m9_candidate_registry.sql)의 CHECK/트리거가 정본 게이트다.
여기 있는 함수는 같은 규칙을 쓰기 전에 먼저 걸러 주고, 조회 API 가 금액을
서로 다른 축(구독료 vs 토큰 단가)으로 섞어 보여주지 않게 막는다.
현행 모델은 llm_models 가 정본이다 — 이 모듈은 후보만 다룬다.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional

SURFACES = ("chat", "runner", "terminal_cli", "service")
PRICE_STATUSES = ("official_verified", "unverified_official", "estimated")
PRICING_KINDS = ("api_per_token", "subscription", "per_image", "per_minute")
TRAINING_USES = ("not_used", "used_for_training", "unknown")
CANDIDATE_STATUSES = ("candidate", "testing", "approved", "rejected", "retired")
# CLI 발견·실호출 검증 상태 (migrations/20260930_cli_model_autoreg.sql)
CLI_PROBE_STATUSES = ("discovered", "verified", "blocked_account", "probe_failed")
CANDIDATE_STATUSES = CANDIDATE_STATUSES + CLI_PROBE_STATUSES
VERDICTS = ("equivalent", "candidate_better", "candidate_worse", "insufficient_sample", "not_run")
# 표본이 선언된 최소값 미달일 때 허용되는 판정. 결론(동등/우세/열위)은 미검증이다.
UNDERSAMPLED_VERDICTS = ("insufficient_sample", "not_run")

DEFAULT_EXCLUSION_REASON = "입력/출력이 모델 학습에 사용됨 — 비공개 코드·운영/고객 데이터 평가 기본 제외"

# pricing_kind 별로 정렬에 쓰는 금액 칸. 서로 다른 kind 는 같은 축에 올리지 않는다.
PRICE_SORT_FIELD = {
    "api_per_token": "price_input_per_1m",
    "subscription": "subscription_price_per_month",
}

_DECIMAL_FIELDS = (
    "price_input_per_1m", "price_cached_input_per_1m", "price_output_per_1m",
    "subscription_price_per_month", "cost_per_success_incumbent",
    "cost_per_success_candidate", "savings_pct",
)
_JSON_FIELDS = (
    "extra_charges", "noninferiority_margin", "metrics_incumbent", "metrics_candidate",
)


class CandidateRegistryError(ValueError):
    """대장 규칙 위반."""


class PriceAxisMismatch(CandidateRegistryError):
    """pricing_kind 가 다른 후보를 같은 금액축으로 비교하려 함."""


def normalize_candidate(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """INSERT/UPDATE 전 후보 행을 정규화한다. 학습 사용 후보는 비공개 평가에서 자동 제외."""
    row = dict(candidate)
    if row.get("training_use", "unknown") not in TRAINING_USES:
        raise CandidateRegistryError(f"unknown training_use: {row.get('training_use')!r}")
    if row.get("pricing_kind") not in PRICING_KINDS:
        raise CandidateRegistryError(f"unknown pricing_kind: {row.get('pricing_kind')!r}")
    if row.get("price_status", "estimated") not in PRICE_STATUSES:
        raise CandidateRegistryError(f"unknown price_status: {row.get('price_status')!r}")
    bad_surfaces = set(row.get("surface_scope") or ()) - set(SURFACES)
    if bad_surfaces:
        raise CandidateRegistryError(f"unknown surface_scope: {sorted(bad_surfaces)}")

    if row.get("pricing_kind") == "subscription":
        token_prices = [k for k in ("price_input_per_1m", "price_cached_input_per_1m", "price_output_per_1m")
                        if row.get(k) is not None]
        if token_prices:
            raise PriceAxisMismatch(f"subscription 후보에 토큰 단가를 적을 수 없음: {token_prices}")
    elif row.get("subscription_price_per_month") is not None:
        raise PriceAxisMismatch("토큰/건당 과금 후보에 구독료를 적을 수 없음")

    if row.get("price_status") == "official_verified" and not (row.get("official_url") and row.get("verified_at")):
        raise CandidateRegistryError("official_verified 는 official_url 과 verified_at 이 필요함")

    if row.get("training_use") == "used_for_training":
        row["excluded_from_private_eval"] = True
        if not (row.get("exclusion_reason") or "").strip():
            row["exclusion_reason"] = DEFAULT_EXCLUSION_REASON
    return row


def validate_comparison(comparison: Mapping[str, Any]) -> Dict[str, Any]:
    """비교 행 검증. 비열등 한계가 먼저, 표본 미달이면 결론 금지."""
    row = dict(comparison)
    if row.get("surface") not in SURFACES:
        raise CandidateRegistryError(f"unknown surface: {row.get('surface')!r}")
    verdict = row.get("verdict")
    if verdict is not None and verdict not in VERDICTS:
        raise CandidateRegistryError(f"unknown verdict: {verdict!r}")
    min_sample = row.get("min_sample_size")
    if not isinstance(min_sample, int) or min_sample <= 0:
        raise CandidateRegistryError("min_sample_size 는 시험 전에 양의 정수로 선언해야 함")
    sample = row.get("sample_size") or 0
    if verdict is None:
        return row

    margin = row.get("noninferiority_margin")
    if isinstance(margin, str):
        margin = json.loads(margin) if margin.strip() else None
    if not isinstance(margin, dict) or not margin:
        raise CandidateRegistryError("noninferiority_margin 없이 verdict 를 저장할 수 없음(시험 전 선언 필수)")
    if sample < min_sample and verdict not in UNDERSAMPLED_VERDICTS:
        raise CandidateRegistryError(
            f"sample_size {sample} < min_sample_size {min_sample}: verdict 는 insufficient_sample 만 허용"
        )
    return row


def ensure_same_price_axis(candidates: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """후보들이 모두 같은 pricing_kind 인지 확인하고 그 kind 를 돌려준다."""
    kinds = {c.get("pricing_kind") for c in candidates}
    if len(kinds) > 1:
        raise PriceAxisMismatch(f"pricing_kind 가 다른 후보는 같은 금액축으로 비교할 수 없음: {sorted(map(str, kinds))}")
    return next(iter(kinds), None)


def sort_by_price(candidates: List[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """같은 pricing_kind 안에서만 금액순 정렬. 금액 NULL 은 뒤로."""
    kind = ensure_same_price_axis(candidates)
    if kind is None:
        return []
    field = PRICE_SORT_FIELD.get(kind)
    if field is None:
        raise PriceAxisMismatch(f"pricing_kind={kind} 는 정렬 기준 금액 칸이 정의되지 않음")
    return sorted(candidates, key=lambda c: (c.get(field) is None, c.get(field) or 0))


def _plain(value: Any) -> Any:
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def serialize_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """asyncpg Record → JSON. 금액은 문자열(정밀도 보존), jsonb 는 객체로."""
    out: Dict[str, Any] = {}
    for key, value in dict(row).items():
        if key in _JSON_FIELDS and isinstance(value, str):
            value = json.loads(value)
        elif key in _DECIMAL_FIELDS and value is not None:
            value = str(value)
        elif isinstance(value, (list, tuple)):
            value = list(value)
        else:
            value = _plain(value)
        out[key] = value
    # 미검증 금액이 검증된 금액처럼 보이지 않게 한 칸 더 명시한다.
    if "price_status" in out:
        out["price_verified"] = out["price_status"] == "official_verified"
    return out
