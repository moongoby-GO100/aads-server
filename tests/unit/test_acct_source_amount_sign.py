"""ACCT 원천 행의 금액 부호 계약.

`obys_workspaces._amount()` 는 `direction == "out"` 이면 금액을 음수로 뒤집는다.
그 플래그는 통장(bank) 입출금에만 의미가 있는데, 매출·매입·카드·세무 전표에는
`deposit_amount` 원자가 아예 없어 항상 0 -> "out" 으로 떨어졌다.

2026-09-23 실측: 라일론 매입 110,000원(공급가 100,000 + 세액 10,000)이 화면에
`-110,000원` 으로 표시됐다. 통장 외 업무에는 direction 을 붙이지 않는다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.api import acct_source_ledger as mod
from app.api import obys_workspaces as api


PURCHASE_ROW = {
    "source_file_id": 16005,
    "rec_idx": 3202,
    "occurred_on": "20260831",
    "counterparty": "노무법인 로앤",
    "item_name": "인사노무자문료",
    "detail_type": "51",
    "supply_amount": Decimal("100000"),
    "tax_amount": Decimal("10000"),
    "total_amount": Decimal("110000"),
}


@pytest.mark.parametrize("category", ["purchase", "sales", "card", "tax"])
def test_non_bank_rows_have_no_direction_flag(category):
    row = mod._row(dict(PURCHASE_ROW), category)
    assert "direction" not in row, "통장이 아닌 업무에 direction 이 붙으면 금액이 뒤집힌다"


@pytest.mark.parametrize("category", ["purchase", "sales", "card", "tax"])
def test_non_bank_amounts_stay_positive(category):
    row = mod._row(dict(PURCHASE_ROW), category)
    assert api._amount(row) == Decimal("110000")


def test_bank_withdraw_still_negative():
    row = mod._row(
        {**PURCHASE_ROW, "deposit_amount": Decimal("0"), "withdraw_amount": Decimal("110000")},
        "bank",
    )
    assert row["direction"] == "out"
    assert api._amount(row) < 0


def test_bank_deposit_stays_positive():
    row = mod._row(
        {**PURCHASE_ROW, "deposit_amount": Decimal("110000"), "withdraw_amount": Decimal("0")},
        "bank",
    )
    assert row["direction"] == "in"
    assert api._amount(row) > 0


def test_display_amount_is_not_negative_for_purchase():
    row = mod._row(dict(PURCHASE_ROW), "purchase")
    record = api._record("acct-source", row, "주식회사 라일론")
    assert record["display"]["금액"] == "110,000원"
    assert record["display"]["공급가"] == "100,000원"
    assert record["display"]["세액"] == "10,000원"
    # 통장 전용 컬럼은 매입 행에서 오해를 주지 않아야 한다.
    assert record["display"]["거래유형"] == "해당 없음"
