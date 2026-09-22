"""ACCT 원천 매출·매입 읽기 계약.

핵심은 두 가지다.
1. 같은 전표가 여러 스냅샷 파일에 중복 적재돼 있으므로 반드시 전표키로 dedupe 한다.
   (2026-09-22 라일론 실측: 매입 원본 26,723행 → 전표키 기준 4,210건)
2. 매출/매입은 계정과목 추정이 아니라 위하고 전표유형 원값으로 가른다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import acct_source_ledger as mod


def test_pivot_sql_dedupes_by_voucher_key():
    sql = mod._pivot_sql(11, "2", None, None)
    assert "DISTINCT ON (occurred_on, voucher_key)" in sql
    # 같은 전표키면 가장 나중 스냅샷 파일만 남는다.
    assert "ORDER BY occurred_on, voucher_key, source_file_id DESC" in sql
    assert "sf.company_id = 11" in sql
    assert "sf.is_current IS TRUE" in sql
    assert "entry_type = '2'" in sql


def test_pivot_sql_applies_date_window_in_source_format():
    sql = mod._pivot_sql(11, "1", "2026-08-01", "2026-08-31")
    assert "occurred_on >= '20260801'" in sql
    assert "occurred_on <= '20260831'" in sql


def test_full_totals_are_computed_before_row_limit():
    sql = mod._pivot_sql(11, "2", None, None)
    assert "count(*) OVER() AS source_total_count" in sql
    assert "OVER() AS source_total_amount" in sql
    assert sql.index("source_total_amount") < sql.index("LIMIT 2000")


def test_detail_id_is_filtered_after_deduplication_before_limit():
    sql = mod._pivot_sql(11, "2", None, None, "acct:100:7")
    assert "FROM deduped WHERE source_file_id = 100 AND rec_idx = 7" in sql
    with pytest.raises(HTTPException):
        mod._pivot_sql(11, "2", None, None, "acct:1:1 OR 1=1")


def test_sales_and_purchase_use_distinct_entry_types():
    assert mod._ENTRY_TYPE["sales"] == "1"
    assert mod._ENTRY_TYPE["purchase"] == "2"
    assert "'1'" in mod._pivot_sql(11, mod._ENTRY_TYPE["sales"], None, None)


def test_row_keeps_source_values_without_guessing():
    row = mod._row(
        {
            "source_file_id": 16179,
            "rec_idx": 2642,
            "detail_type": "57",
            "evidence_code": "15",
            "occurred_on": "20260902",
            "counterparty": "주식회사 한국사이버결제",
            "nm_good": None,
            "supply_amount": Decimal("27000"),
            "tax_amount": Decimal("2700"),
            "total_amount": Decimal("29700"),
            "status_code": "1",
        },
        "purchase",
    )
    assert row["id"] == "acct:16179:2642"
    assert row["occurred_on"] == "2026-09-02"
    assert row["detail_type_label"] == "카드매입"
    assert row["evidence_label"] == "신용카드"
    assert row["total_amount"] == Decimal("29700")
    assert row["status"] == "확정"


@pytest.mark.asyncio
async def test_source_transactions_rejects_unknown_category():
    with pytest.raises(HTTPException) as excinfo:
        await mod.source_transactions({}, "biz-lylon-e2e", "margin")
    assert excinfo.value.status_code == 400
