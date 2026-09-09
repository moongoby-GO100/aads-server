import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[2] / "app/services/yeoljeong_dashboard_service.py"
SPEC = importlib.util.spec_from_file_location("yeoljeong_dashboard_service_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


def _row(day: str, platform: str, total: int, orders: int) -> dict:
    return {
        "sale_date": day,
        "service": platform,
        "total": total,
        "orders": orders,
    }


def test_aggregate_sales_rows_daily_preserves_platform_totals() -> None:
    result = service._aggregate_sales_rows(
        [
            _row("2026-09-07", "baemin", 10_000, 2),
            _row("2026-09-07", "yogiyo", 5_000, 1),
        ],
        "daily",
    )

    assert result == [
        {
            "date": "2026-09-07",
            "period": "daily",
            "total": 15_000,
            "baemin": 10_000,
            "coupangeats": 0,
            "yogiyo": 5_000,
            "ddangyo": 0,
            "orders": 3,
        }
    ]


def test_aggregate_sales_rows_weekly_uses_monday_bucket() -> None:
    result = service._aggregate_sales_rows(
        [
            _row("2026-09-07", "baemin", 10_000, 2),
            _row("2026-09-13", "ddangyo", 7_000, 1),
            _row("2026-09-14", "coupangeats", 20_000, 4),
        ],
        "weekly",
    )

    assert [item["date"] for item in result] == ["2026-09-07", "2026-09-14"]
    assert result[0]["total"] == 17_000
    assert result[0]["orders"] == 3
    assert result[1]["coupangeats"] == 20_000


def test_aggregate_sales_rows_monthly_uses_first_day_bucket() -> None:
    result = service._aggregate_sales_rows(
        [
            _row("2026-08-31", "baemin", 4_000, 1),
            _row("2026-09-01", "baemin", 6_000, 1),
            _row("2026-09-09", "yogiyo", 9_000, 2),
        ],
        "monthly",
    )

    assert [item["date"] for item in result] == ["2026-08-01", "2026-09-01"]
    assert result[1]["total"] == 15_000


def test_business_filter_is_parameterized() -> None:
    assert service._business_filter("biz-mia", 3) == ("AND business_id = $3", ("biz-mia",))
    assert service._business_filter("") == ("", ())


def test_preservation_symbols_remain_available() -> None:
    for symbol in ("_run_async", "_query", "_query_one", "_parse_payload"):
        assert callable(getattr(service, symbol))


def test_aggregate_sales_rows_rejects_unknown_period() -> None:
    with pytest.raises(ValueError, match="unsupported sales period"):
        service._aggregate_sales_rows([], "quarterly")


def test_expense_range_returns_date_objects_not_strings() -> None:
    from datetime import date

    start, end = service._expense_range("2026-09-01", "2026-09-09")
    assert (start, end) == (date(2026, 9, 1), date(2026, 9, 9))
    assert not isinstance(start, str) and not isinstance(end, str)


def test_expense_range_defaults_to_current_month_kst() -> None:
    today = service._today_kst()
    start, end = service._expense_range()
    assert start == today.replace(day=1)
    assert end == today


def test_expense_range_rejects_inverted_or_invalid_dates() -> None:
    with pytest.raises(ValueError, match="after date_to"):
        service._expense_range("2026-09-10", "2026-09-01")
    with pytest.raises(ValueError):
        service._expense_range("not-a-date", "2026-09-01")
