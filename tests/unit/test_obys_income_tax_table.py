"""오비서 급여명세 — 근로소득 간이세액표(2026.03.01) 등록과 소득세·지방소득세 자동 산정."""
import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.services import yeoljeong_payroll_deductions as ded

TABLE_FILE = ded._TAX_TABLE_DIR / "income_tax_simple_table_20260301.csv"


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(ded, "_loaded_tables", {})


def test_registered_table_covers_official_range():
    table = ded.load_income_tax_table(str(TABLE_FILE))
    assert len(table) == 646
    assert table[0].salary_from == 770_000 and table[-1].salary_to == 10_000_000
    assert ded.lookup_income_tax(table, 9_999_999, 11) == 958_650  # 원본 마지막 행 9,980~10,000천원


def test_hong_seokbin_confirmed_withholding_matches_table():
    # 홍석빈 10월 확정 명세: 총지급 3,200,000 − 비과세 식대 200,000 = 과세 3,000,000, 원천세 81,780원.
    items = ded.income_tax_items("2026-10", 3_000_000)
    assert items["income_tax"]["amount"] == 74_350
    assert items["local_income_tax"]["amount"] == 7_430
    assert items["income_tax"]["amount"] + items["local_income_tax"]["amount"] == 81_780
    assert "본인 1명 기준" in items["income_tax"]["calc_note"]
    assert not any(item.get("needs_input") for item in items.values())


def test_family_count_selects_column_and_rejects_bad_values():
    assert ded.income_tax_items("2026-10", 3_000_000, 2)["income_tax"]["amount"] == 56_850
    assert ded.income_tax_items("2026-10", 3_000_000, "3")["income_tax"]["amount"] == 31_940
    for bad in (0, 12, "두명", -1):
        assert ded.income_tax_items("2026-10", 3_000_000, bad)["income_tax"]["needs_input"] is True


def test_outside_table_or_before_effective_month_needs_input():
    assert ded.income_tax_items("2026-02", 3_000_000)["income_tax"]["needs_input"] is True
    over = ded.income_tax_items("2026-10", 10_000_000)
    assert over["income_tax"]["needs_input"] is True and "구간 밖" in over["income_tax"]["calc_note"]
    low = ded.income_tax_items("2026-10", 500_000)
    assert low["income_tax"]["amount"] == 0 and not low["income_tax"].get("needs_input")
    assert ded.pending_input_codes(list(low.values())) == []  # 표가 근거인 0원은 확정을 막지 않는다


def test_failed_load_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(ded, "_TAX_TABLE_DIR", tmp_path)
    assert ded.income_tax_table_for("2026-10") is None  # 파일 없음
    (tmp_path / "income_tax_simple_table_20260301.csv").write_bytes(TABLE_FILE.read_bytes())
    found = ded.income_tax_table_for("2026-10")
    assert found is not None and found[0] == "2026.03.01"


def test_apply_fills_six_items_and_records_basis():
    items, basis = ded.apply_statutory_deductions([], month="2026-10", taxable_pay=3_000_000, tax_family_count=None)
    by_code = {item["code"]: item for item in items}
    assert set(ded.REQUIRED_FOUR_INSURANCE_CODES) <= set(by_code)
    assert sum(by_code[c]["amount"] for c in ded.INSURANCE_CODES) == 291_520  # 홍석빈 확정 보험 합계
    assert by_code["income_tax"]["amount"] == 74_350 and by_code["local_income_tax"]["amount"] == 7_430
    assert basis["income_tax_table"] == "2026.03.01" and basis["tax_family_count"] == 1
    assert ded.four_insurance_deduction_errors(items) == []
    # 관리자가 고친 소득세는 다시 계산해도 보존된다.
    edited = [dict(item, amount=70_000) if item["code"] == "income_tax" else item for item in items]
    again, _ = ded.apply_statutory_deductions(edited, month="2026-10", taxable_pay=3_000_000, previous_basis=basis)
    assert {i["code"]: i for i in again}["income_tax"]["amount"] == 70_000


def test_lump_tax_total_is_not_double_counted_with_table_local_tax():
    items, _ = ded.apply_statutory_deductions([], month="2026-10", taxable_pay=2_600_000, totals={"tax_withholding": 50_000})
    by_code = {item["code"]: item for item in items}
    assert by_code["income_tax"]["amount"] == 50_000 and by_code["income_tax"]["needs_input"]
    assert by_code["local_income_tax"]["amount"] == 0 and by_code["local_income_tax"]["needs_input"]
    assert sum(by_code[c]["amount"] for c in ded.TAX_CODES) == 50_000
