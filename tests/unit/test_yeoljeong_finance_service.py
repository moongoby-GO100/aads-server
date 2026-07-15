import os
import importlib.util
from pathlib import Path


_SERVICE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "yeoljeong_finance_service.py"
_SPEC = importlib.util.spec_from_file_location("yeoljeong_finance_service", _SERVICE_PATH)
service = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(service)


def test_import_card_csv_maps_and_classifies(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = (
        "거래일시,카드종류,승인번호,주문번호,상품명,합계금액,판매자상호,과세금액,부가세\n"
        "2026/06/12 08:49:33,쿠팡와우카드(KB국민),30018576,3100197534848,"
        "자연이삭 백미 보통등급,119800,쿠팡(주),0,0\n"
    )

    result = service.import_file("card.csv", csv_text.encode("utf-8-sig"), "card")

    assert result["import"]["imported_rows"] == 1
    row = result["rows"][0]
    assert row["source_type"] == "card"
    assert row["transaction_date"] == "2026-06-12 08:49:33"
    assert row["amount"] == 119800
    assert row["category"] == "식자재"
    assert row["approval_number"] == "30018576"


def test_import_bank_csv_uses_income_expense_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = "거래일자,적요,입금액,출금액,계좌명\n2026-07-01,배달의민족 정산,55000,,국민은행\n2026-07-02,가스요금,,12000,국민은행\n"

    result = service.import_file("bank.csv", csv_text.encode("utf-8-sig"), "bank")
    rows = result["rows"]

    assert len(rows) == 2
    assert rows[0]["direction"] == "income"
    assert rows[0]["category"] == "배달앱"
    assert rows[1]["direction"] == "expense"
    assert rows[1]["category"] == "공과금"


def test_duplicate_import_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = "거래일자,적요,출금액\n2026-07-02,가스요금,12000\n"

    first = service.import_file("bank.csv", csv_text.encode("utf-8-sig"), "bank")
    second = service.import_file("bank.csv", csv_text.encode("utf-8-sig"), "bank")

    assert first["import"]["imported_rows"] == 1
    assert second["import"]["imported_rows"] == 0
    assert second["import"]["duplicate_rows"] == 1
    assert len(service.list_transactions()) == 1


def test_env_data_dir_does_not_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service.create_transaction({"transaction_date": "2026-07-14", "amount": 1, "description": "테스트"})

    assert os.path.exists(tmp_path / "transactions.json")
