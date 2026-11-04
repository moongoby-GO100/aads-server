"""오비서 근로소득 간이세액표(홈택스 2024-03-01판·2026-03-01판) 조회 단위 테스트.

기대값은 CEO 첨부 엑셀의 본표 행에서 그대로 옮겼다(행 인용은 각 테스트 주석).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services import yeoljeong_wage_tax_table as wt
from app.services import yeoljeong_withholding as wh


def _lookup(month, wage, dependents, children=0, ratio=100):
    return wt.lookup_monthly_wage_tax(month, wage, dependents, children, ratio)


def test_t1_row_2000_2010_family1():
    # 표 2000~2010천원 행: 가족 1명 19,520
    r = _lookup("2026-09", 2_000_000, 1)
    assert (r["income_tax"], r["local_income_tax"]) == (19_520, 1_950)
    assert r["table_version"] == "2026-03-01"
    assert r["bracket"] == "2,000~2,010천원"
    assert r["needs_input"] is False


def test_t2_row_3000_3020_family2():
    # 표 3000~3020천원 행: 가족 2명 56,850
    assert _lookup("2026-09", 3_000_000, 2)["income_tax"] == 56_850


def test_t3_child_deduction_uses_version_of_payroll_month():
    # 표 3000~3020천원 행: 가족 3명 31,940
    old = _lookup("2025-09", 3_000_000, 3, children=1)
    new = _lookup("2026-09", 3_000_000, 3, children=1)
    assert old["table_version"] == "2024-03-01" and old["income_tax"] == 31_940 - 12_500 == 19_440
    assert new["table_version"] == "2026-03-01" and new["income_tax"] == 31_940 - 20_830 == 11_110


@pytest.mark.parametrize(
    "month, children, deduction",
    [
        ("2025-09", 2, 29_160),
        ("2025-09", 3, 29_160 + 25_000),
        ("2025-09", 5, 29_160 + 25_000 * 3),
        ("2026-09", 2, 45_830),
        ("2026-09", 3, 45_830 + 33_330),
        ("2026-09", 5, 45_830 + 33_330 * 3),
    ],
)
def test_child_deduction_two_or_more(month, children, deduction):
    # 표 3000~3020천원 행 가족 1명 74,350 에서 별표2 3호 금액을 뺀다(음수면 0)
    assert _lookup(month, 3_000_000, 1, children=children)["income_tax"] == max(74_350 - deduction, 0)


def test_version_switch_at_effective_month():
    assert _lookup("2026-02", 3_000_000, 3, 1)["table_version"] == "2024-03-01"
    assert _lookup("2026-03", 3_000_000, 3, 1)["table_version"] == "2026-03-01"
    assert _lookup(date(2024, 3, 31), 3_000_000, 3)["table_version"] == "2024-03-01"


def test_t4_negative_after_child_deduction_is_zero():
    # 표 2000~2010천원 행 가족 3명 6,600 - 자녀 공제 12,500 < 0
    r = _lookup("2025-09", 2_000_000, 3, children=1)
    assert (r["income_tax"], r["local_income_tax"]) == (0, 0)


@pytest.mark.parametrize("dependents", [None, 0])
def test_t5_missing_dependents_needs_input(dependents):
    r = _lookup("2026-09", 3_000_000, dependents)
    assert r["needs_input"] is True
    assert r["reason"] == "공제대상가족 수 미등록"
    assert r["income_tax"] is None and r["local_income_tax"] is None


def test_t6_withholding_ratio():
    # 표 3000~3020천원 행 가족 1명 74,350
    r80 = _lookup("2026-09", 3_000_000, 1, ratio=80)
    r120 = _lookup("2026-09", 3_000_000, 1, ratio=120)
    assert (r80["income_tax"], r80["local_income_tax"]) == (59_480, 5_940)  # 74,350*0.8 = 59,480
    assert (r120["income_tax"], r120["local_income_tax"]) == (89_220, 8_920)  # 74,350*1.2 = 89,220
    assert _lookup("2026-09", 3_000_000, 1, ratio=100)["income_tax"] == 74_350
    for bad in (90, 0, 110, "abc"):
        with pytest.raises(ValueError):
            _lookup("2026-09", 3_000_000, 1, ratio=bad)


def test_t6_ratio_applies_after_child_deduction_and_floors_to_10_won():
    # (74,350 - 20,830) * 0.8 = 42,816 -> 10원 미만 절사 42,810
    assert _lookup("2026-09", 3_000_000, 1, children=1, ratio=80)["income_tax"] == 42_810


def test_t7_dash_rows_are_zero_and_first_taxed_row():
    # 표 1055~1060천원 행: 전 열 '-'
    assert _lookup("2026-09", 1_055_000, 1)["income_tax"] == 0
    # 표 1060~1065천원 행: 가족 1명 1,040
    assert _lookup("2026-09", 1_060_000, 1)["income_tax"] == 1_040
    assert _lookup("2026-09", 1_064_999, 1)["income_tax"] == 1_040
    assert _lookup("2026-09", 1_060_000, 2)["income_tax"] == 0


def test_below_table_minimum_is_zero():
    r = _lookup("2026-09", 769_999, 1)
    assert r["income_tax"] == 0 and r["bracket"] == "770천원 미만"
    assert _lookup("2026-09", 0, 1)["income_tax"] == 0
    assert _lookup("2026-09", -5, 1)["income_tax"] == 0


def test_range_is_inclusive_min_exclusive_max():
    # 표 3000~3020천원 가족 1명 74,350 / 3020~3040천원 76,060
    assert _lookup("2026-09", 3_019_999, 1)["income_tax"] == 74_350
    assert _lookup("2026-09", 3_020_000, 1)["income_tax"] == 76_060


def test_t8_over_10_000_thousand_won_formula():
    # 표 10,000천원 행 가족 1명 1,507,400 / 가족 2명 1,431,570.
    # 12,000천원, 가족 1: 1,507,400 + 12,000,000 초과분(2,000,000)*98%*35% (=686,000) + 25,000 = 2,218,400
    r = _lookup("2026-09", 12_000_000, 1)
    assert r["income_tax"] == 2_218_400 and r["local_income_tax"] == 221_840
    assert r["bracket"] == "10,000천원 초과 14,000천원 이하 (공식)"
    # 가족 2: 1,431,570 + 686,000 + 25,000 = 2,142,570
    assert _lookup("2026-09", 12_000_000, 2)["income_tax"] == 2_142_570
    # 50,000천원, 가족 1: 1,507,400 + 13,394,600 + (50,000,000-45,000,000)*42% (=2,100,000) = 17,002,000
    assert _lookup("2026-09", 50_000_000, 1)["income_tax"] == 17_002_000
    # 30,000천원 초과 구간은 98% 를 곱하지 않는다: 40,000천원 -> 1,507,400 + 7,394,600 + 10,000,000*40% = 12,902,000
    assert _lookup("2026-09", 40_000_000, 1)["income_tax"] == 12_902_000
    # 100,000천원: 1,507,400 + 31,034,600 + 13,000,000*45% (=5,850,000) = 38,392,000
    assert _lookup("2026-09", 100_000_000, 1)["income_tax"] == 38_392_000


def test_t8_boundaries_between_formula_brackets_are_continuous():
    # 10,000천원 정확히는 표의 점 행(가족 1명 1,507,400), 14,000천원 이하/초과 두 공식이 같은 값 2,904,400
    assert _lookup("2026-09", 10_000_000, 1)["income_tax"] == 1_507_400
    assert _lookup("2026-09", 10_000_000, 1)["bracket"] == "10,000천원"
    # 14,000천원 = 1,507,400 + 25,000 + 4,000,000*98%*35%(=1,372,000) = 2,904,400
    assert _lookup("2026-09", 14_000_000, 1)["income_tax"] == 2_904_400
    # 14,000천원 초과 공식: 1,507,400 + 1,397,000 = 2,904,400
    assert _lookup("2026-09", 14_000_001, 1)["income_tax"] == 2_904_400
    # 28,000천원: 1,507,400 + 1,397,000 + 14,000,000*98%*38%(=5,213,600) = 8,118,000 = 1,507,400 + 6,610,600
    assert _lookup("2026-09", 28_000_000, 1)["income_tax"] == 8_118_000
    # 30,000천원: 8,118,000 + 2,000,000*98%*40%(=784,000) = 8,902,000 = 1,507,400 + 7,394,600
    assert _lookup("2026-09", 30_000_000, 1)["income_tax"] == 8_902_000
    # 45,000천원: 1,507,400 + 7,394,600 + 15,000,000*40% = 14,902,000 = 1,507,400 + 13,394,600
    assert _lookup("2026-09", 45_000_000, 1)["income_tax"] == 14_902_000
    # 87,000천원: 1,507,400 + 13,394,600 + 42,000,000*42% = 32,542,000 = 1,507,400 + 31,034,600
    assert _lookup("2026-09", 87_000_000, 1)["income_tax"] == 32_542_000


def test_over_11_dependents_uses_item4_formula():
    # 표 9980~10000천원 행: 가족 10명 988,650 / 11명 958,650 (차 30,000)
    assert _lookup("2026-09", 9_990_000, 11)["income_tax"] == 958_650
    assert _lookup("2026-09", 9_990_000, 12)["income_tax"] == 958_650 - 30_000
    assert _lookup("2026-09", 9_990_000, 14)["income_tax"] == 958_650 - 30_000 * 3
    # 표 3000~3020천원 행: 10명 3,600 / 11명 0 -> 음수가 되지 않는다
    assert _lookup("2026-09", 3_000_000, 12)["income_tax"] == 0


def test_t9_month_before_first_table_is_unregistered():
    r = _lookup("2024-02", 3_000_000, 1)
    assert r["income_tax"] is None and r["local_income_tax"] is None
    assert r["table_version"] is None
    assert "세액표 미등록" in r["reason"]
    assert _lookup("2024-03", 3_000_000, 1)["table_version"] == "2024-03-01"


def test_invalid_inputs():
    with pytest.raises(ValueError):
        _lookup("2026-13", 3_000_000, 1)
    with pytest.raises(ValueError):
        _lookup("garbage", 3_000_000, 1)
    with pytest.raises(ValueError):
        _lookup("2026-09", 3_000_000, -1)
    with pytest.raises(ValueError):
        _lookup("2026-09", 3_000_000, 1, children=-1)


@pytest.mark.parametrize("version", wt.TABLE_VERSIONS)
def test_t10_csv_rows_and_continuity(version):
    rows = wt.load_table(version)
    # 770~10,000천원 본표 646행 + 10,000천원 점 행 1
    assert len(rows) == 647
    assert rows[0][0] == 770_000
    assert rows[-1][:2] == (10_000_000, 10_000_001)
    for prev, nxt in zip(rows, rows[1:]):
        assert prev[1] == nxt[0], (prev[:2], nxt[:2])
        assert prev[0] < prev[1]
    for _, _, vals in rows:
        assert len(vals) == 11
        assert all(v >= 0 and v % 10 == 0 for v in vals)
        # 가족 수가 늘수록 세액은 줄거나 같다
        assert list(vals) == sorted(vals, reverse=True)


def test_t10_main_table_same_in_both_versions():
    assert wt.load_table("2024-03-01") == wt.load_table("2026-03-01")


def test_t11_compute_withholding_wage_income_is_wired():
    rule = wh.get_rule(wh.WAGE_INCOME)
    assert rule.verified is True
    joined = " ".join(rule.sources)
    assert "2024.03.01" in joined and "2026.03.01" in joined
    assert "소득세법 시행령 제189조 별표2" in rule.sources

    r = wh.compute_withholding(wh.WAGE_INCOME, 3_000_000, payroll_month="2026-09", dependents=2)
    assert (r["income_tax"], r["local_income_tax"], r["total"]) == (56_850, 5_680, 62_530)
    assert r["table_version"] == "2026-03-01" and r["needs_input"] is False

    child = wh.compute_withholding(
        wh.WAGE_INCOME, 3_000_000, payroll_month="2026-09", dependents=3, children_8_20=1
    )
    assert child["income_tax"] == 11_110


def test_t11_wage_income_without_dependents_returns_needs_input():
    r = wh.compute_withholding(wh.WAGE_INCOME, 3_000_000)
    assert r["needs_input"] is True and r["reason"] == "공제대상가족 수 미등록"
    assert r["income_tax"] is None and r["total"] is None
    r2 = wh.compute_withholding(wh.WAGE_INCOME, 3_000_000, payroll_month="2026-09")
    assert r2["needs_input"] is True and r2["income_tax"] is None
    r3 = wh.compute_withholding(wh.WAGE_INCOME, 3_000_000, dependents=2)
    assert r3["needs_input"] is True and r3["reason"] == "급여월 미지정"
    r4 = wh.compute_withholding(wh.WAGE_INCOME, 3_000_000, payroll_month="2024-02", dependents=2)
    assert r4["needs_input"] is False and r4["income_tax"] is None and "세액표 미등록" in r4["reason"]


def test_t11_other_kinds_unchanged():
    biz = wh.compute_withholding(wh.BUSINESS_INCOME, 1_000_000)
    assert (biz["income_tax"], biz["local_income_tax"], biz["total"]) == (30_000, 3_000, 33_000)
    assert biz["rounding_verified"] is False
    with pytest.raises(wh.WithholdingUnverified) as exc:
        wh.compute_withholding(wh.DAILY_WAGE_INCOME, 1_000_000)
    assert "검증 필요" in exc.value.reason
    with pytest.raises(wh.WithholdingKindUnknown):
        wh.compute_withholding("other", 1)
