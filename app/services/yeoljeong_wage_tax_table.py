"""근로소득 간이세액표 조회 (오비서 세금신고 — 소득세법 시행령 제189조 별표2).

데이터 출처: 홈택스 "근로소득 간이세액표" 엑셀 2종(CEO 첨부, AADS DB chat_messages
id=cabcdc65-df72-4973-a331-48121957f130 attachments).
  - 근로소득 간이세액표_2024.03.01.xlsx  (적용 시작 2024-03-01)
  - 근로소득 간이세액표_2026.03.01.xlsx  (적용 시작 2026-03-01)
두 판의 본표(시트 "근로소득간이세액표")는 같고, 시트 "소득령 별표2" 의 8세 이상 20세 이하
자녀 공제액만 다르다. 본표는 app/data/obys_wage_tax_table/wage_tax_table_<적용일>.csv 로
옮겼다(열: min_krw, max_krw, f1..f11 — 원 단위, 원문 "-" 는 0). 세액 숫자는 첨부 원문에서만
가져왔고 기억·웹으로 채우지 않았다.
"""
from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "obys_wage_tax_table"

# (적용 시작일, 판 이름, 8세 이상 20세 이하 자녀 공제액: 1명, 2명, 2명 초과 1명당 추가)
# 별표2 3호 원문(2026-03-01판):
#   가. 8세 이상 20세 이하 자녀가 1명인 경우 : 20,830원
#   나. 8세 이상 20세 이하 자녀가 2명인 경우 : 45,830원
#   다. 8세 이상 20세 이하 자녀가 3명 이상인 경우 : 45,830원 + 2명 초과 자녀 1명당 33,330원
# 2024-03-01판은 12,500원 / 29,160원 / 29,160원 + 2명 초과 자녀 1명당 25,000원.
# "다만, 공제한 금액이 음수인 경우의 세액은 0원으로 함."
_VERSIONS: tuple[tuple[date, str, int, int, int], ...] = (
    (date(2024, 3, 1), "2024-03-01", 12_500, 29_160, 25_000),
    (date(2026, 3, 1), "2026-03-01", 20_830, 45_830, 33_330),
)
TABLE_VERSIONS: tuple[str, ...] = tuple(v[1] for v in _VERSIONS)
EARLIEST_EFFECTIVE_DATE = _VERSIONS[0][0]

FAMILY_COLUMNS = 11
TABLE_TOP_KRW = 10_000_000  # 본표 마지막 행 "10,000천원"(한 점). 이를 초과하면 아래 공식.
ALLOWED_RATIOS = (80, 100, 120)  # 소득세법 시행령 제194조 선택 비율(%)
LOCAL_TAX_RATIO = Decimal("0.1")  # 지방세법 제103조의13
_TEN = Decimal(10)

# 월급여 10,000천원 초과 구간 원문(시트 "근로소득간이세액표" 끝부분). "해당 세액" 은 가족 수별 열의
# "10,000천원" 행 값이다. 구간 경계는 아래 (초과 하한, 이하 상한) 이고 표에서는
# "X천원 초과" 행에 공식, 다음 행에 "Y천원 이하" 로 적혀 있다.
#   10,000천원 초과 14,000천원 이하:
#     (10,000천원인 경우의 해당 세액) + (10,000천원을 초과하는 금액에 98%를 곱한 금액의 35% 상당액) + (25,000원)
#   14,000천원 초과 28,000천원 이하:
#     (10,000천원인 경우의 해당 세액) + (1,397,000원) + (14,000천원을 초과하는 금액에 98%를 곱한 금액의 38% 상당액)
#   28,000천원 초과 30,000천원 이하:
#     (10,000천원인 경우의 해당 세액) + (6,610,600원) + (28,000천원을 초과하는 금액에 98%를 곱한 금액의 40% 상당액)
#   30,000천원 초과 45,000천원 이하:
#     (10,000천원인 경우의 해당 세액) + (7,394,600원) + (30,000천원을 초과하는 금액의 40% 상당액)
#   45,000천원 초과 87,000천원 이하:
#     (10,000천원인 경우의 해당 세액) + (13,394,600원) + (45,000천원을 초과하는 금액의 42% 상당액)
#   87,000천원 초과:
#     (10,000천원인 경우의 해당 세액) + (31,034,600원) + (87,000천원을 초과하는 금액의 45% 상당액)
# 각 항: (초과 하한 원, 고정 가산액 원, 초과분 곱할 계수, 상당액 비율)
_OVER_TOP_FORMULAS: tuple[tuple[int, int, Decimal, Decimal], ...] = (
    (10_000_000, 25_000, Decimal("0.98"), Decimal("0.35")),
    (14_000_000, 1_397_000, Decimal("0.98"), Decimal("0.38")),
    (28_000_000, 6_610_600, Decimal("0.98"), Decimal("0.40")),
    (30_000_000, 7_394_600, Decimal(1), Decimal("0.40")),
    (45_000_000, 13_394_600, Decimal(1), Decimal("0.42")),
    (87_000_000, 31_034_600, Decimal(1), Decimal("0.45")),
)
_FORMULA_LOWER_KRW = tuple(f[0] for f in _OVER_TOP_FORMULAS)

# 별표2 4호 원문: 공제대상가족의 수가 11명을 초과하는 경우의 세액은 다음 "가목"의 금액에서 "나목"의 금액을 공제한 금액으로 함.
#   가. 공제대상가족의 수가 11명인 경우의 세액
#   나. (공제대상가족의 수가 10명인 경우의 세액 - 공제대상가족의 수가 11명인 경우의 세액) × 11명을 초과하는 가족의 수
# 별표2 2호: 공제대상가족의 수를 산정할 때 본인 및 배우자도 각각 1명으로 보아 계산함.
# 별표2 5호: 학자금을 지급하는 때에는 해당 학자금을 제외한 월급여액(비과세 제외)을 기준으로 계산할 수 있음.


def _won_floor10(value: Decimal) -> int:
    """10원 미만 절사. 본표 세액이 전부 10원 단위라는 것은 첨부로 확인했으나, 공식 구간·비율 적용값의 단수처리 조문은 확인하지 못했다 — 검증 필요."""
    return int((value / _TEN).to_integral_value(rounding=ROUND_DOWN)) * 10


@lru_cache(maxsize=None)
def load_table(version: str) -> tuple[tuple[int, int, tuple[int, ...]], ...]:
    """본표 행 (min_krw 이상, max_krw 미만, 가족 1..11명 세액). min_krw 오름차순."""
    if version not in TABLE_VERSIONS:
        raise ValueError(f"알 수 없는 세액표 판: {version}")
    path = DATA_DIR / f"wage_tax_table_{version}.csv"
    rows: list[tuple[int, int, tuple[int, ...]]] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            rows.append(
                (
                    int(rec["min_krw"]),
                    int(rec["max_krw"]),
                    tuple(int(rec[f"f{i}"]) for i in range(1, FAMILY_COLUMNS + 1)),
                )
            )
    return tuple(rows)


@lru_cache(maxsize=None)
def _row_mins(version: str) -> tuple[int, ...]:
    return tuple(r[0] for r in load_table(version))


def _first_of_month(payroll_month: Any) -> date:
    if isinstance(payroll_month, datetime):
        return payroll_month.date().replace(day=1)
    if isinstance(payroll_month, date):
        return payroll_month.replace(day=1)
    text = str(payroll_month or "").strip()
    try:
        parts = text.split("-")
        return date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"급여월 형식 오류(YYYY-MM): {payroll_month!r}") from exc


def _version_for(month_start: date) -> tuple[str, int, int, int] | None:
    chosen = None
    for start, name, one, two, extra in _VERSIONS:
        if month_start >= start:
            chosen = (name, one, two, extra)
    return chosen


def _child_deduction(one: int, two: int, extra: int, children: int) -> int:
    if children <= 0:
        return 0
    if children == 1:
        return one
    return two + extra * (children - 2)


def _column_tax(version: str, wage: Decimal, family: int) -> tuple[int, str]:
    """가족 1..11명 열의 간이세액표 세액(자녀 공제·비율 적용 전)과 구간 표기."""
    col = family - 1
    rows = load_table(version)
    if wage > TABLE_TOP_KRW:
        top = rows[-1][2][col]
        idx = bisect_left(_FORMULA_LOWER_KRW, wage) - 1
        lower, fixed, factor, pct = _OVER_TOP_FORMULAS[idx]
        tax = _won_floor10(Decimal(top) + fixed + (wage - lower) * factor * pct)
        upper = _OVER_TOP_FORMULAS[idx + 1][0] if idx + 1 < len(_OVER_TOP_FORMULAS) else None
        label = f"{lower // 1000:,}천원 초과" + (f" {upper // 1000:,}천원 이하" if upper else "") + " (공식)"
        return tax, label
    if wage < rows[0][0]:
        return 0, f"{rows[0][0] // 1000:,}천원 미만"
    i = bisect_right(_row_mins(version), wage) - 1
    lo, hi, vals = rows[i]
    if lo == TABLE_TOP_KRW:
        return vals[col], f"{lo // 1000:,}천원"
    return vals[col], f"{lo // 1000:,}~{hi // 1000:,}천원"


def lookup_monthly_wage_tax(
    payroll_month: Any,
    monthly_taxable_wage: Any,
    dependents: int | None,
    children_8_20: int = 0,
    withholding_ratio: int | Decimal | str = 100,
) -> dict[str, Any]:
    """월 급여에 대한 소득세·지방소득세(근로소득 간이세액표).

    monthly_taxable_wage: 비과세·학자금을 제외한 월급여(원). 구간은 이상~미만이며 표 최저 구간 미만은 0.
    dependents: 본인 포함 공제대상가족 수. None·0 이면 needs_input (1명으로 추정하지 않는다).
    payroll_month: 급여월. 이 달이 속한 판(2024-03-01 / 2026-03-01)의 세액표와 자녀 공제액을 쓴다.
        지급일 기준이 아닌 급여월 기준이다 — 지급월 기준 여부는 검증 필요.
    withholding_ratio: 80/100/120 만 허용(소득세법 시행령 제194조 선택 비율), 그 외 ValueError.

    반환: income_tax, local_income_tax, table_version, bracket, basis_note, needs_input, reason.
    세액을 낼 수 없으면(가족 수 미등록·세액표 미등록) income_tax/local_income_tax 는 None.
    """
    try:
        ratio = Decimal(str(withholding_ratio).strip())
    except Exception as exc:  # decimal.InvalidOperation
        raise ValueError(f"원천징수 비율은 80/100/120 만 허용: {withholding_ratio!r}") from exc
    if ratio not in ALLOWED_RATIOS:
        raise ValueError(f"원천징수 비율은 80/100/120 만 허용: {withholding_ratio!r}")

    def _none(reason: str, *, version: str | None = None, needs_input: bool = False) -> dict[str, Any]:
        return {
            "income_tax": None,
            "local_income_tax": None,
            "table_version": version,
            "bracket": None,
            "basis_note": "",
            "needs_input": needs_input,
            "reason": reason,
        }

    month_start = _first_of_month(payroll_month)
    version_info = _version_for(month_start)
    if version_info is None:
        return _none(f"세액표 미등록 — 급여월 {month_start:%Y-%m} 은 최초 등록판 적용일({EARLIEST_EFFECTIVE_DATE}) 이전")
    version, one, two, extra = version_info

    if dependents is None or dependents == 0:
        return _none("공제대상가족 수 미등록", version=version, needs_input=True)
    family = int(dependents)
    if family < 0 or family != dependents:
        raise ValueError(f"공제대상가족 수는 1 이상의 정수: {dependents!r}")
    children = int(children_8_20 or 0)
    if children < 0:
        raise ValueError(f"8세 이상 20세 이하 자녀 수는 0 이상: {children_8_20!r}")

    wage = Decimal(str(monthly_taxable_wage or 0))
    if wage < 0:
        wage = Decimal(0)

    if family <= FAMILY_COLUMNS:
        base_tax, bracket = _column_tax(version, wage, family)
        over_note = ""
    else:
        t11, bracket = _column_tax(version, wage, 11)
        t10, _ = _column_tax(version, wage, 10)
        base_tax = max(t11 - (t10 - t11) * (family - FAMILY_COLUMNS), 0)
        over_note = f", 11명 초과 {family - FAMILY_COLUMNS}명(별표2 4호: 11명 세액 - (10명 세액 - 11명 세액) × 초과 인원)"

    child_deduction = _child_deduction(one, two, extra, children)
    after_child = max(base_tax - child_deduction, 0)
    income_tax = _won_floor10(Decimal(after_child) * ratio / 100)
    local_tax = _won_floor10(Decimal(income_tax) * LOCAL_TAX_RATIO)

    note = f"간이세액표 {version}판 가족 {family}명 {bracket} 세액 {base_tax:,}원"
    if children:
        note += f" - 8~20세 자녀 {children}명 공제 {child_deduction:,}원(음수면 0) = {after_child:,}원"
    if ratio != 100:
        note += f" × {ratio}%"
    note += f" → 소득세 {income_tax:,}원, 지방소득세(10%) {local_tax:,}원{over_note}"
    return {
        "income_tax": income_tax,
        "local_income_tax": local_tax,
        "table_version": version,
        "bracket": bracket,
        "basis_note": note,
        "needs_input": False,
        "reason": "",
    }
