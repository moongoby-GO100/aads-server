"""급여명세 초안의 법정 공제(4대보험) 자동 산정과 소득세 간이세액표 로더 (오비서/열정국밥).

범위: 근로자(four_insurance) 명세의 국민연금·건강보험·장기요양·고용보험 4항목을 급여월 기준 요율로 계산한다.
소득세·지방소득세는 간이세액표 데이터가 아직 없어 '입력 필요'(needs_input) 항목으로만 만든다.
표가 들어오면 load_income_tax_table 로 읽을 수 있게 로더 인터페이스만 먼저 둔다(자동 산정은 후속).

요율·상하한은 급여월 단위 표다. 표에 없는 급여월은 항목을 빼거나 제한 없이 계산하지 않고,
needs_input 항목(calc_note '요율 미등록' / '상하한 미등록')으로 만들어 확정을 막는다.

원 단위 절사: 보험료는 10원 미만 절사(국민연금법·건강보험법의 보험료 단수 처리 기준을 따른다).
장기요양은 '실제 적용하는 건강보험료'(절사 후 금액, 관리자가 고친 값 포함) × 요율 후 10원 미만 절사 — 건강보험료 확정액에 요율을 곱하는 법정 순서다.
국민연금은 과세급여의 천원 미만을 버려 기준소득월액을 만들고(천원 단위), 상·하한을 적용한 뒤 요율을 곱해 10원 미만 절사한다.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Any

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
INSURANCE_CODES = ("national_pension", "health_insurance", "long_term_care", "employment_insurance")
TAX_CODES = ("income_tax", "local_income_tax")
REQUIRED_FOUR_INSURANCE_CODES = INSURANCE_CODES + TAX_CODES
_LABELS = {
    "national_pension": "국민연금", "health_insurance": "건강보험", "long_term_care": "노인장기요양보험",
    "employment_insurance": "고용보험", "income_tax": "소득세", "local_income_tax": "지방소득세",
}
_PPM = 1_000_000  # 요율은 백만분율 정수로 둔다 — 부동소수점 오차로 절사 경계가 흔들리지 않게.

# (시작월, 끝월, 요율ppm). 출처: 서울경제 2026-01-03·치과신문 2026-01-30·파이낸셜뉴스 2026-04-11(2026),
# 2025 은 동일 보도의 전년 요율. 고용보험 근로자 0.9% 는 2025·2026 동일.
_RATES: dict[str, tuple[tuple[str, str, int], ...]] = {
    "national_pension": (("2025-01", "2025-12", 45_000), ("2026-01", "2026-12", 47_500)),
    "health_insurance": (("2025-01", "2025-12", 35_450), ("2026-01", "2026-12", 35_950)),
    "long_term_care": (("2025-01", "2025-12", 129_500), ("2026-01", "2026-12", 131_400)),  # 건강보험료 × 요율
    "employment_insurance": (("2025-01", "2026-12", 9_000),),
}
# 국민연금 기준소득월액 상·하한(적용기간 단위, 급여월로 판정). 출처: 보건복지부 발표 보도 2026-06-09·매일신문 2026-07-13.
# 2025-06 이전 기간은 근거를 확인하지 못해 넣지 않는다.
_PENSION_BOUNDS = (
    ("2025-07", "2026-06", 400_000, 6_370_000),
    ("2026-07", "2027-06", 410_000, 6_590_000),
)
# 건강보험료 근로자 부담 상·하한(월, 원). 직장가입자 월 보험료 하한 20,160·상한 9,183,480 의 50%(노사 각 50% 부담).
# 출처: 국민건강보험공단 보험료 부과 기준(건강보험료율 7.19%, 2026-01 시행). 2025 상·하한은 근거를 확인하지 못해
# 넣지 않는다 — 요율만 있고 상·하한이 없는 달은 자동 산정하지 않고 needs_input('상하한 미등록')으로 만든다.
_HEALTH_BOUNDS = (
    ("2026-01", "2026-12", 10_080, 4_591_740),
)
_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _floor10(value: int) -> int:
    return value // 10 * 10


def _lookup_rate(code: str, month: str) -> int | None:
    for start, end, ppm in _RATES.get(code, ()):
        if start <= month <= end:
            return ppm
    return None


def pension_bounds(month: str) -> tuple[int, int] | None:
    for start, end, low, high in _PENSION_BOUNDS:
        if start <= month <= end:
            return low, high
    return None


def health_bounds(month: str) -> tuple[int, int] | None:
    for start, end, low, high in _HEALTH_BOUNDS:
        if start <= month <= end:
            return low, high
    return None


def _item(code: str, amount: int, note: str, *, needs_input: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code, "label": _LABELS[code], "amount": amount, "source": SOURCE_AUTO, "calc_note": note,
    }
    if needs_input:
        item["needs_input"] = True
    return item


def long_term_care_item(month: str, health_amount: int | None) -> dict[str, Any]:
    """장기요양 자동 항목. health_amount 는 실제 적용하는 건강보험료(자동값이든 관리자 수정값이든). 모르면 None."""
    month = str(month or "").strip()
    ltc_ppm = _lookup_rate("long_term_care", month) if _MONTH_RE.fullmatch(month) else None
    if health_amount is None or ltc_ppm is None:
        return _item("long_term_care", 0, f"{month or '급여월 미상'} 장기요양 요율 미등록 — 관리자 입력 필요", needs_input=True)
    return _item(
        "long_term_care", _floor10(health_amount * ltc_ppm // _PPM),
        f"건강보험료 {health_amount:,}원 × {ltc_ppm / 10_000:g}%, 10원 미만 절사",
    )


def compute_statutory_items(month: str, taxable_pay: Any) -> dict[str, dict[str, Any]]:
    """급여월·과세급여로 4대보험 4항목 + 소득세 2항목(입력 필요)을 계산해 code → 항목으로 낸다."""
    month = str(month or "").strip()
    taxable = max(_int(taxable_pay), 0)
    valid_month = bool(_MONTH_RE.fullmatch(month))
    result: dict[str, dict[str, Any]] = {}

    ppm = _lookup_rate("national_pension", month) if valid_month else None
    bounds = pension_bounds(month) if valid_month else None
    if ppm is None:
        result["national_pension"] = _item("national_pension", 0, f"{month or '급여월 미상'} 국민연금 요율 미등록 — 관리자 입력 필요", needs_input=True)
    elif bounds is None:
        result["national_pension"] = _item("national_pension", 0, f"{month} 국민연금 기준소득월액 상하한 미등록 — 관리자 입력 필요", needs_input=True)
    else:
        # 순서: 과세급여 천원 미만 절사 → 상·하한 → 요율 → 10원 미만 절사. 기준소득월액은 천원 단위라
        # 과세 3,000,999원은 3,000,000원으로 본다. 절사 후 하한 아래가 되는 소액도 하한으로 올린다.
        base = min(max(taxable // 1000 * 1000, bounds[0]), bounds[1]) if taxable > 0 else 0
        result["national_pension"] = _item(
            "national_pension", _floor10(base * ppm // _PPM),
            f"기준소득월액 {base:,}원(과세급여 {taxable:,}원 천원 미만 절사, 상한 {bounds[1]:,}·하한 {bounds[0]:,}) × {ppm / 10_000:g}%, 10원 미만 절사",
        )

    ppm = _lookup_rate("health_insurance", month) if valid_month else None
    health_limits = health_bounds(month) if valid_month else None
    health_ready = ppm is not None and health_limits is not None
    health = 0
    if ppm is None:
        result["health_insurance"] = _item("health_insurance", 0, f"{month or '급여월 미상'} 건강보험 요율 미등록 — 관리자 입력 필요", needs_input=True)
    elif health_limits is None:
        result["health_insurance"] = _item("health_insurance", 0, f"{month} 건강보험료 상하한 미등록 — 관리자 입력 필요", needs_input=True)
    else:
        # 순서: 과세급여 × 요율 → 10원 미만 절사 → 근로자 부담 상·하한. 상·하한 값이 10원 단위라 절사 순서와 무관하다.
        health = min(max(_floor10(taxable * ppm // _PPM), health_limits[0]), health_limits[1]) if taxable > 0 else 0
        result["health_insurance"] = _item(
            "health_insurance", health,
            f"과세급여 {taxable:,}원 × {ppm / 10_000:g}%, 10원 미만 절사 (상한 {health_limits[1]:,}·하한 {health_limits[0]:,})",
        )

    result["long_term_care"] = long_term_care_item(month, health if health_ready else None)

    ppm = _lookup_rate("employment_insurance", month) if valid_month else None
    if ppm is None:
        result["employment_insurance"] = _item("employment_insurance", 0, f"{month or '급여월 미상'} 고용보험 요율 미등록 — 관리자 입력 필요", needs_input=True)
    else:
        result["employment_insurance"] = _item(
            "employment_insurance", _floor10(taxable * ppm // _PPM), f"과세급여 {taxable:,}원 × {ppm / 10_000:g}%, 10원 미만 절사",
        )

    for code in TAX_CODES:
        result[code] = _item(code, 0, "근로소득 간이세액표 미등록 — 관리자 입력 필요", needs_input=True)
    return result


def build_deduction_basis(month: str, taxable_pay: Any) -> dict[str, Any]:
    """자동 산정에 쓴 입력값. 과세급여가 바뀌었는지, 어떤 요율이었는지 나중에 대조할 수 있게 명세에 남긴다."""
    month = str(month or "").strip()
    bounds = pension_bounds(month)
    health_limits = health_bounds(month)
    return {
        "payroll_month": month,
        "taxable_pay": max(_int(taxable_pay), 0),
        "rates_ppm": {code: _lookup_rate(code, month) for code in _RATES},
        "pension_bounds": {"lower": bounds[0], "upper": bounds[1]} if bounds else None,
        "health_bounds": {"lower": health_limits[0], "upper": health_limits[1]} if health_limits else None,
    }


def _prior_auto_amount(code: str, previous_basis: dict[str, Any] | None, legacy: dict[str, dict[str, Any]]) -> int | None:
    """직전 저장에서 서버가 자동으로 넣은 금액. 이 값과 다른 금액이 오면 관리자가 고친 것이다.

    장기요양처럼 다른 항목(건강보험료)에 의존하는 자동값은 과세급여만으로 다시 계산해 비교할 수 없다 —
    건강보험료를 수동으로 바꾸면 직전 장기요양은 '그때 적용한 건강보험료' 기준이기 때문이다. 그래서 저장 시점의
    실제 자동 금액을 basis.auto_amounts 에 기록해 두고 그것과 비교한다. 기록이 없는 basis 만 재계산으로 대신한다."""
    recorded = (previous_basis or {}).get("auto_amounts")
    if isinstance(recorded, dict):
        return _int(recorded[code]) if code in recorded else None
    return legacy[code]["amount"] if code in legacy else None


def _is_unmodified_auto(
    item: dict[str, Any], code: str, previous_basis: dict[str, Any] | None, legacy: dict[str, dict[str, Any]],
    *, fallback_amount: int | None = None,
) -> bool:
    """서버가 넣은 자동값 그대로인 항목인가. 금액을 고쳤거나 확인 표시가 있으면 관리자 입력이다.

    직전 자동값 기록이 없으면 fallback_amount(같은 code 대표 항목의 금액)와 같을 때만 사본으로 본다."""
    if item.get("source") != SOURCE_AUTO or item.get("confirmed"):
        return False
    amount = _int(item.get("amount"))
    if code in TAX_CODES and amount != 0:
        return False
    prior = _prior_auto_amount(code, previous_basis, legacy)
    if prior is None:
        prior = fallback_amount
    return prior is None or amount == prior


def apply_statutory_deductions(
    deduction_items: list[dict[str, Any]] | None,
    *,
    month: str,
    taxable_pay: Any,
    previous_basis: dict[str, Any] | None = None,
    totals: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """공제 항목에 4대보험·소득세를 항목 단위로 채운다. (새 항목 목록, deduction_basis).

    - 같은 code 의 관리자 항목(source=manual, 확인 표시, 또는 직전 자동값에서 금액을 고친 항목)은 그대로 보존한다.
    - 자동 항목(source=auto)은 현재 과세급여로 매번 다시 계산한다. 장기요양은 실제 적용 건강보험료 기준이다.
    - 소득세·지방소득세는 0원이면 confirmed=true 로 확인하기 전까지 입력 대기(needs_input)다.
      합계 입력에서 옮긴 소득세는 금액을 고치거나 confirmed 로 확인하기 전까지 입력 대기가 유지된다.
    - 같은 code 의 관리자 항목이 둘 이상이면 모두 보존한다(첫 항목이 대표 항목, 나머지는 추가 항목으로 뒤에 둔다).
    - 빠진 code 만 채운다. 기타 공제 등 다른 항목이 있어도 건너뛰지 않는다.
    - totals: 항목이 없는 상태로 입력된 합계 칸(tax_withholding·insurance_deduction·other_deduction).
      기타 공제 합계는 '기타 공제' 항목, 세금 합계는 소득세 항목(입력 대기)으로 옮긴다.
      보험 합계만 있으면 나눌 근거가 없어 자동 산정이 대체하고 대체한 값을 deduction_basis.replaced_totals 에 남긴다.
    """
    items = [dict(item) for item in (deduction_items or [])]
    fresh = compute_statutory_items(month, taxable_pay)
    legacy: dict[str, dict[str, Any]] = {}
    if previous_basis and previous_basis.get("payroll_month") and not isinstance(previous_basis.get("auto_amounts"), dict):
        legacy = compute_statutory_items(previous_basis["payroll_month"], previous_basis.get("taxable_pay"))
    pending_before = (previous_basis or {}).get("pending_tax_amounts")
    pending_before = pending_before if isinstance(pending_before, dict) else {}

    kept: dict[str, dict[str, Any]] = {}
    auto_claimed: dict[str, int] = {}  # 첫 항목이 자동값이라 대표 자리를 자동으로 점유한 code → 그 항목의 금액
    others: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    for item in items:
        code = item.get("code")
        if code not in fresh:
            others.append(item)
            continue
        if code in kept or code in auto_claimed:
            # 같은 code 의 두 번째 이후 항목(예: 건강보험 + 건강보험 정산)은 관리자 입력이므로 지우지 않고 그대로 둔다.
            # 자동값 사본(source=auto, 확인 없음, 금액도 자동값 그대로)만 대표 항목과 이중 계상되지 않게 버린다.
            # 자동 항목을 복사해 금액을 고쳤다면 source 가 auto 여도 관리자 입력이므로 보존한다.
            if not _is_unmodified_auto(item, code, previous_basis, legacy, fallback_amount=(
                    auto_claimed[code] if code in auto_claimed else _int(kept[code].get("amount")))):
                extras.append(dict(item, source=SOURCE_MANUAL))
            continue
        if _is_unmodified_auto(item, code, previous_basis, legacy):
            auto_claimed[code] = _int(item.get("amount"))
            continue  # 아래에서 현재 과세급여로 다시 계산한다.
        manual = dict(item)
        manual["source"] = SOURCE_MANUAL
        amount = _int(manual.get("amount"))
        if code in TAX_CODES:
            if manual.get("confirmed"):
                manual.pop("needs_input", None)
            elif amount == 0 or amount == _int(pending_before.get(code)):
                # 확인 없는 0원, 또는 합계에서 옮겨 온 뒤 금액을 그대로 둔 값은 확정 근거가 못 된다.
                manual["needs_input"] = True
            else:
                manual.pop("needs_input", None)  # 금액을 직접 고쳤다
        elif amount != 0 or manual.get("confirmed"):
            manual.pop("needs_input", None)  # 금액을 넣었거나 0원으로 확인한 항목은 더 이상 입력 대기가 아니다.
        else:
            manual["needs_input"] = True  # 확인 없는 수동 0원 보험료(요율 미등록 월 포함)는 확정 근거가 못 된다.
        kept[code] = manual

    replaced: dict[str, int] = {}
    totals = totals or {}
    given_codes = {i.get("code") for i in items}
    tax_total = _int(totals.get("tax_withholding"))
    if tax_total and not given_codes & set(TAX_CODES):
        # 소득세·지방소득세 구분 없이 합계만 입력된 값은 버리지 않고 소득세 항목으로 옮긴다.
        # 구분이 불확실하므로 입력 대기(needs_input)로 남겨 관리자가 나누거나 확인하게 한다.
        kept["income_tax"] = {
            "code": "income_tax", "label": _LABELS["income_tax"], "amount": tax_total, "source": SOURCE_MANUAL,
            "needs_input": True, "calc_note": "소득세·지방소득세 합계 입력값을 옮김 — 구분 확인 필요",
        }
    insurance_total = _int(totals.get("insurance_deduction"))
    if insurance_total and not given_codes & set(INSURANCE_CODES):
        replaced["insurance_deduction"] = insurance_total  # 4개 보험으로 나눌 근거가 없어 자동 산정이 대체한다
    other_total = _int(totals.get("other_deduction"))
    if other_total and not any(i.get("code") == "other" for i in others):
        others.append({"code": "other", "label": "기타 공제", "amount": other_total, "source": SOURCE_MANUAL})

    if "long_term_care" not in kept:
        # 장기요양은 실제 적용하는 건강보험료 기준 — 건강보험이 관리자 수정값이면 그 값으로 계산한다.
        # 기준은 대표 건강보험 항목(자동이면 fresh, 수동이면 그 금액)뿐이다. '건강보험 정산' 같은 extras 는
        # 보험료 확정액이 아닌 별도 정산이라 장기요양 기준에 넣지 않는다.
        health = kept.get("health_insurance")
        fresh["long_term_care"] = long_term_care_item(month, _int(health.get("amount")) if health else (
            None if fresh["health_insurance"].get("needs_input") else fresh["health_insurance"]["amount"]))
    merged = [kept.get(code) or fresh[code] for code in REQUIRED_FOUR_INSURANCE_CODES]
    basis = build_deduction_basis(month, taxable_pay)
    basis["auto_amounts"] = {item["code"]: _int(item.get("amount")) for item in merged if item.get("source") == SOURCE_AUTO}
    pending_tax = {item["code"]: _int(item.get("amount")) for item in merged
                   if item["code"] in TAX_CODES and item.get("needs_input") and _int(item.get("amount")) != 0}
    if pending_tax:
        basis["pending_tax_amounts"] = pending_tax
    if replaced:
        basis["replaced_totals"] = replaced
    return merged + extras + others, basis


def pending_input_codes(deduction_items: list[dict[str, Any]] | None) -> list[str]:
    """확정 전에 관리자가 채우거나 확인해야 하는 code. 세금 0원은 confirmed 가 아니면 플래그가 없어도 대기다."""
    pending = []
    for item in deduction_items or []:
        if item.get("confirmed"):
            continue
        zero = _int(item.get("amount")) == 0
        # 세금 0원, 그리고 관리자 입력(source!=auto)인 보험 0원은 confirmed 가 아니면 플래그가 없어도 대기다.
        if item.get("needs_input") or (zero and (item.get("code") in TAX_CODES or (
                item.get("code") in INSURANCE_CODES and item.get("source") != SOURCE_AUTO))):
            pending.append(item["code"])
    return pending


def four_insurance_deduction_errors(deduction_items: list[dict[str, Any]] | None) -> list[str]:
    """four_insurance 명세 확정 전 공제 항목 검증 사유(한글). 6개 code 가 모두 있고 입력 대기가 없어야 통과."""
    present = {item.get("code") for item in deduction_items or []}
    missing = [_LABELS[code] for code in REQUIRED_FOUR_INSURANCE_CODES if code not in present]
    if missing:
        return [f"4대보험·소득세 공제 항목이 빠져 있어 확정할 수 없습니다: {', '.join(missing)}"]
    pending = pending_input_codes(deduction_items)
    if pending:
        return [f"관리자 입력이 필요한 공제 항목이 남아 있어 확정할 수 없습니다: {', '.join(_LABELS[c] for c in pending if c in _LABELS)}"]
    return []


# ---- 근로소득 간이세액표 로더 (데이터 등록·자동 산정은 후속 작업) ----------------------------------

class IncomeTaxTableError(ValueError):
    """간이세액표 파일 형식이 올바르지 않을 때. 메시지는 한글 사유다."""


@dataclass(frozen=True)
class IncomeTaxBracket:
    salary_from: int  # 월 급여액(원) 이상
    salary_to: int  # 월 급여액(원) 미만
    tax_by_family: dict[int, int]  # 공제대상가족 수 → 소득세(원)


_MAX_FAMILY = 11
_FAMILY_COLUMN = re.compile(r"family_(\d{1,2})")


def _table_int(value: Any, where: str) -> int:
    text = str(value if value is not None else "").strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise IncomeTaxTableError(f"{where}: 0 이상의 정수 원 단위여야 합니다 ({value!r})")
    return int(text)


def load_income_tax_table(path: str) -> list[IncomeTaxBracket]:
    """CSV(UTF-8, 머리글: salary_from, salary_to, family_1 … family_11) → 오름차순·연속 구간표.

    금액은 모두 원 단위. 구간은 [salary_from, salary_to) 이고 앞 구간의 끝이 다음 구간의 시작과 같아야 한다.
    형식이 틀리면 부분만 읽지 않고 IncomeTaxTableError 로 거부한다."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = [str(name).strip() for name in (reader.fieldnames or [])]
            rows = list(reader)
    except OSError as exc:
        raise IncomeTaxTableError(f"간이세액표 파일을 읽을 수 없습니다: {exc}") from None
    except UnicodeDecodeError:
        raise IncomeTaxTableError("간이세액표 파일은 UTF-8 CSV 여야 합니다") from None
    for required in ("salary_from", "salary_to"):
        if required not in fieldnames:
            raise IncomeTaxTableError(f"간이세액표 머리글에 {required} 가 없습니다")
    families = sorted(int(m.group(1)) for name in fieldnames if (m := _FAMILY_COLUMN.fullmatch(name)))
    if not families:
        raise IncomeTaxTableError("간이세액표 머리글에 family_N (공제대상가족 수) 열이 없습니다")
    if any(n < 1 or n > _MAX_FAMILY for n in families):
        raise IncomeTaxTableError(f"공제대상가족 수는 1~{_MAX_FAMILY} 이어야 합니다")
    if not rows:
        raise IncomeTaxTableError("간이세액표에 구간이 없습니다")
    table: list[IncomeTaxBracket] = []
    for line, row in enumerate(rows, 2):
        row = {str(k).strip(): v for k, v in row.items() if k is not None}
        low = _table_int(row.get("salary_from"), f"{line}행 salary_from")
        high = _table_int(row.get("salary_to"), f"{line}행 salary_to")
        if high <= low:
            raise IncomeTaxTableError(f"{line}행: salary_to 가 salary_from 보다 커야 합니다")
        if table and low != table[-1].salary_to:
            raise IncomeTaxTableError(f"{line}행: 구간이 앞 구간과 이어지지 않습니다 (겹치거나 비어 있음)")
        table.append(IncomeTaxBracket(low, high, {n: _table_int(row.get(f"family_{n}"), f"{line}행 family_{n}") for n in families}))
    return table


def lookup_income_tax(table: list[IncomeTaxBracket], monthly_salary: int, family: int) -> int | None:
    """구간표에서 소득세를 찾는다. 표 밖이거나 가족 수 열이 없으면 None(추정하지 않는다)."""
    for bracket in table:
        if bracket.salary_from <= monthly_salary < bracket.salary_to:
            return bracket.tax_by_family.get(family)
    return None
