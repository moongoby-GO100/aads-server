"""Unit tests for the bank browser connector and service integration."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Load modules under test ──────────────────────────────────────────────────

_CONNECTOR_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "yeoljeong_bank_browser_connector.py"
_CONNECTOR_SPEC = importlib.util.spec_from_file_location("yeoljeong_bank_browser_connector", _CONNECTOR_PATH)
connector = importlib.util.module_from_spec(_CONNECTOR_SPEC)
assert _CONNECTOR_SPEC and _CONNECTOR_SPEC.loader
_CONNECTOR_SPEC.loader.exec_module(connector)

_SERVICE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "yeoljeong_finance_service.py"
_SERVICE_SPEC = importlib.util.spec_from_file_location("yeoljeong_finance_service", _SERVICE_PATH)
service = importlib.util.module_from_spec(_SERVICE_SPEC)
assert _SERVICE_SPEC and _SERVICE_SPEC.loader
_SERVICE_SPEC.loader.exec_module(service)


# ── Fixtures ─────────────────────────────────────────────────────────────────

ADMIN_USER = {"email": "owner@example.com", "is_admin": True}


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    def disable_db(coroutine):
        close = getattr(coroutine, "close", None)
        if close:
            close()
        return None

    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(service, "_run_db", disable_db)
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))


def _make_browser_bank_account(monkeypatch, tmp_path, **overrides):
    payload = {
        "business_id": "biz-mia",
        "branch_id": "branch-gangbuk-mia",
        "bank_code": "088",
        "bank_name": "신한은행",
        "account_number": "110-123-456789",
        "account_holder": "최미미",
        "account_alias": "미아점 브라우저계좌",
        "connection_type": "browser",
        "status": "active",
        "institution_code": "shinhan_business",
    }
    payload.update(overrides)
    return service.create_bank_account(payload, ADMIN_USER)


# ── Work-key tests ────────────────────────────────────────────────────────────

def test_bank_browser_work_key_is_opaque():
    key = connector.bank_browser_work_key("acct-001", "biz-mia", "branch-gangbuk-mia")
    assert key.startswith("yeoljeong-bank-browser-")
    assert "acct-001" not in key
    assert "biz-mia" not in key
    assert "branch" not in key


def test_bank_browser_work_key_deterministic():
    k1 = connector.bank_browser_work_key("a", "b", "c")
    k2 = connector.bank_browser_work_key("a", "b", "c")
    assert k1 == k2


def test_bank_browser_work_key_differs_for_different_inputs():
    k1 = connector.bank_browser_work_key("acct-1", "biz-mia", "branch-gangbuk-mia")
    k2 = connector.bank_browser_work_key("acct-2", "biz-mia", "branch-gangbuk-mia")
    assert k1 != k2


def test_bank_browser_work_key_no_korean_chars():
    key = connector.bank_browser_work_key("acct", "biz", "열정국밥_미아점")
    for char in "열정국밥미아점":
        assert char not in key


# ── HTML parser tests ─────────────────────────────────────────────────────────

_SHINHAN_SAMPLE_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>거래일자</th><th>거래시간</th><th>적요</th><th>보낸분/받는분</th>
      <th>입금</th><th>출금</th><th>잔액</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>2026.08.05</td><td>13:20</td><td>배민 정산</td><td>우아한형제들</td>
      <td>120,000</td><td></td><td>550,000</td>
    </tr>
    <tr>
      <td>2026.08.06</td><td>09:10</td><td>식자재 결제</td><td>마켓봄</td>
      <td></td><td>45,000</td><td>505,000</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

_IBK_SAMPLE_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>거래일자</th><th>적요</th><th>출금금액</th><th>입금금액</th><th>잔액</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>2026-08-10</td><td>이체입금</td><td></td><td>500,000</td><td>2,000,000</td>
    </tr>
    <tr>
      <td>2026-08-11</td><td>공과금</td><td>80,000</td><td></td><td>1,920,000</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

_EMPTY_HTML = "<html><body><p>No data available.</p></body></html>"


def test_parse_shinhan_quick_html_extracts_rows():
    rows = connector.parse_bank_portal_html(_SHINHAN_SAMPLE_HTML)
    assert len(rows) == 2
    assert rows[0]["direction"] == "in"
    assert rows[0]["amount"] == 120000
    assert rows[0]["occurred_at"].startswith("2026-08-05")
    assert rows[1]["direction"] == "out"
    assert rows[1]["amount"] == 45000


def test_parse_shinhan_html_includes_balance_and_counterparty():
    rows = connector.parse_bank_portal_html(_SHINHAN_SAMPLE_HTML)
    assert rows[0]["balance"] == 550000
    assert rows[0]["counterparty"] == "우아한형제들"
    assert rows[0]["memo"] == "배민 정산"


def test_parse_ibk_quick_html_extracts_rows():
    rows = connector.parse_bank_portal_html(_IBK_SAMPLE_HTML)
    assert len(rows) == 2
    assert rows[0]["direction"] == "in"
    assert rows[0]["amount"] == 500000
    assert rows[1]["direction"] == "out"
    assert rows[1]["amount"] == 80000


def test_parse_empty_html_returns_empty_list():
    rows = connector.parse_bank_portal_html(_EMPTY_HTML)
    assert rows == []


def test_parse_html_no_sensitive_data_in_output():
    rows = connector.parse_bank_portal_html(_SHINHAN_SAMPLE_HTML)
    for row in rows:
        dumped = str(row)
        assert "110-123" not in dumped
        assert "456789" not in dumped
        assert "password" not in dumped.lower()
        assert "secret" not in dumped.lower()


def test_parse_date_normalisation():
    html_content = """<table>
      <tr><th>거래일자</th><th>입금</th><th>출금</th></tr>
      <tr><td>2026.08.01</td><td>10000</td><td></td></tr>
      <tr><td>20260802</td><td>20000</td><td></td></tr>
    </table>"""
    rows = connector.parse_bank_portal_html(html_content)
    assert rows[0]["occurred_at"] == "2026-08-01"
    assert rows[1]["occurred_at"] == "2026-08-02"


# ── 중첩 테이블·span 포함 파싱 정확도 테스트 (실제 포털 레이아웃 대응) ──────────

_NESTED_TABLE_HTML = """
<html><body>
<div>
  <table class="layout-wrapper">
    <tr><td>
      <!-- 실제 은행 포털에서 레이아웃 용도로 outer table 감싸는 패턴 -->
      <table class="tx-table">
        <thead>
          <tr>
            <th><span>거래일자</span></th>
            <th><span>적요</span></th>
            <th><span>입금금액</span></th>
            <th><span>출금금액</span></th>
            <th><span>잔액</span></th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><span>2026-08-15</span></td>
            <td><span>카드매출</span></td>
            <td><span>350,000</span></td>
            <td></td>
            <td><span>1,350,000</span></td>
          </tr>
          <tr>
            <td>2026-08-16</td>
            <td>임대료</td>
            <td></td>
            <td>200,000</td>
            <td>1,150,000</td>
          </tr>
        </tbody>
      </table>
    </td></tr>
  </table>
</div>
</body></html>
"""

_SPAN_HEADER_HTML = """
<table>
  <tr>
    <th><span class="col">거래일자</span></th>
    <th><span class="col">보낸분/받는분</span></th>
    <th><span class="col">입금</span></th>
    <th><span class="col">출금</span></th>
  </tr>
  <tr>
    <td><span>2026-08-20</span></td>
    <td><span>우아한형제들</span></td>
    <td><span>88,000</span></td>
    <td></td>
  </tr>
</table>
"""

_NO_DATE_COL_HTML = """
<table>
  <tr><th>상품명</th><th>수량</th><th>단가</th></tr>
  <tr><td>볶음밥</td><td>10</td><td>8000</td></tr>
</table>
"""


def test_parse_nested_table_skips_outer_layout_table():
    """외부 레이아웃 테이블이 중첩되어 있어도 내부 거래 테이블을 파싱해야 한다."""
    rows = connector.parse_bank_portal_html(_NESTED_TABLE_HTML)
    # 외부 layout table이 inner table 파싱을 방해해서는 안 됨
    assert len(rows) == 2
    assert rows[0]["direction"] == "in"
    assert rows[0]["amount"] == 350000
    assert rows[0]["occurred_at"] == "2026-08-15"
    assert rows[1]["direction"] == "out"
    assert rows[1]["amount"] == 200000


def test_parse_span_wrapped_cells_and_headers():
    """th/td 안에 span 태그로 감싼 헤더/값도 정상 파싱돼야 한다."""
    rows = connector.parse_bank_portal_html(_SPAN_HEADER_HTML)
    assert len(rows) == 1
    assert rows[0]["amount"] == 88000
    assert rows[0]["direction"] == "in"
    assert rows[0]["counterparty"] == "우아한형제들"


def test_parse_with_diagnostics_returns_table_count():
    """진단 정보에 테이블 수가 정확히 포함돼야 한다."""
    rows, diag = connector.parse_bank_portal_html_with_diagnostics(_SHINHAN_SAMPLE_HTML)
    assert len(rows) == 2
    assert diag["table_count"] == 1
    assert diag["parse_failure"] is False


def test_parse_with_diagnostics_on_unrecognised_table():
    """날짜 컬럼이 없는 테이블 → parse_failure=True, 빈 rows."""
    rows, diag = connector.parse_bank_portal_html_with_diagnostics(_NO_DATE_COL_HTML)
    assert rows == []
    assert diag["table_count"] == 1
    assert diag["parse_failure"] is True


def test_parse_with_diagnostics_no_tables():
    """테이블이 전혀 없는 HTML → table_count=0, parse_failure=False."""
    rows, diag = connector.parse_bank_portal_html_with_diagnostics("<html><body><p>로딩중</p></body></html>")
    assert rows == []
    assert diag["table_count"] == 0
    assert diag["parse_failure"] is False


# ── Async browser collector tests ─────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_collect_async_no_session_returns_action_required():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        mock_bridge.return_value.sessions.find_by_work_key.return_value = None
        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="",
                browser_work_key="yeoljeong-bank-browser-abc123",
                date_from="2026-08-01",
                date_to="2026-08-31",
            )
        )

    assert result["status"] == "action_required"
    assert result["error_code"] == "BANK_BROWSER_SESSION_REQUIRED"
    assert result["rows"] == []
    assert "PC Agent" in result["message"]


def test_collect_async_explicit_session_not_found_returns_connector_not_ready():
    account = {"id": "acct-1", "bank_name": "IBK기업은행", "bank_code": "003", "institution_code": "ibk_business"}

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        mock_bridge.return_value.sessions.get.return_value = None
        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="expired-session-id",
                browser_work_key="yeoljeong-bank-browser-xyz",
                date_from="",
                date_to="",
            )
        )

    assert result["status"] == "connector_not_ready"
    assert result["error_code"] == "BANK_BROWSER_SESSION_NOT_FOUND"
    assert result["rows"] == []


def test_collect_async_session_success_returns_parsed_rows():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=["https://bank.shinhan.com/", _SHINHAN_SAMPLE_HTML])
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]

    mock_session = MagicMock()
    mock_session.session_id = "live-session-abc"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="live-session-abc",
                browser_work_key="yeoljeong-bank-browser-aaa",
                date_from="",
                date_to="",
            )
        )

    assert result["status"] == "collected"
    assert len(result["rows"]) == 2
    assert result["row_count"] == 2
    assert result["diagnostics"]["auth_mode"] == "pc_agent_browser"
    assert result["diagnostics"]["browser_session_id"] == "live-session-abc"


def test_collect_async_diagnostics_has_no_credentials():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=["https://bank.shinhan.com/", _SHINHAN_SAMPLE_HTML])
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="live-session-abc",
                browser_work_key="yeoljeong-bank-browser-aaa",
                date_from="",
                date_to="",
            )
        )

    diag = result.get("diagnostics") or {}
    diag_str = str(diag)
    assert "password" not in diag_str.lower()
    assert "110-123" not in diag_str
    assert "456789" not in diag_str
    assert "사업자" not in diag_str


def test_collect_async_date_filter_applied():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=["https://bank.shinhan.com/", _SHINHAN_SAMPLE_HTML])
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="live-session-abc",
                browser_work_key="yeoljeong-bank-browser-aaa",
                date_from="2026-08-06",
                date_to="2026-08-06",
            )
        )

    assert result["status"] == "collected"
    assert len(result["rows"]) == 1
    assert result["rows"][0]["occurred_at"].startswith("2026-08-06")


# ── Service integration tests ─────────────────────────────────────────────────

def test_collect_bank_account_browser_no_session_returns_action_required(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)

    def fake_run_async(coro):
        close = getattr(coro, "close", None)
        if close:
            close()
        return {
            "status": "action_required",
            "error_code": "BANK_BROWSER_SESSION_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": {"browser_work_key": "wk-test"},
            "message": "PC Agent 세션 필요",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", fake_run_async)

    result = service.collect_bank_account_transactions(
        account["id"],
        {"business_id": "biz-mia", "branch_id": "branch-gangbuk-mia"},
        ADMIN_USER,
    )

    assert result["collection"]["status"] == "action_required"
    assert result["collection"]["connector_status"] == "ACTION_REQUIRED"
    assert result["collection"]["connection_type"] == "browser"
    assert result["collection"]["imported_rows"] == 0
    assert result["transactions"] == []


def test_collect_bank_account_browser_with_session_imports_rows(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)

    def fake_run_async(coro):
        close = getattr(coro, "close", None)
        if close:
            close()
        return {
            "status": "collected",
            "rows": [
                {"occurred_at": "2026-08-05 13:20", "direction": "in", "amount": 120000, "memo": "배민 정산"},
                {"occurred_at": "2026-08-06 09:10", "direction": "out", "amount": 45000, "memo": "식자재 결제"},
            ],
            "row_count": 2,
            "diagnostics": {"auth_mode": "pc_agent_browser", "browser_session_id": "sess-001"},
            "message": "2건 수집",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", fake_run_async)

    result = service.collect_bank_account_transactions(
        account["id"],
        {
            "business_id": "biz-mia",
            "branch_id": "branch-gangbuk-mia",
            "browser_session_id": "sess-001",
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
        },
        ADMIN_USER,
    )

    assert result["collection"]["status"] == "completed"
    assert result["collection"]["connection_type"] == "browser"
    assert result["collection"]["imported_rows"] == 2
    assert result["collection"]["total_in"] == 120000
    assert result["collection"]["total_out"] == 45000
    assert result["collection"]["net_amount"] == 75000


def test_collect_bank_account_browser_idempotent(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)

    rows = [
        {"occurred_at": "2026-08-10 10:00", "direction": "in", "amount": 500000, "memo": "이체입금"},
    ]

    def fake_run_async(coro):
        close = getattr(coro, "close", None)
        if close:
            close()
        return {
            "status": "collected",
            "rows": rows,
            "row_count": len(rows),
            "diagnostics": {},
            "message": "1건",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", fake_run_async)

    payload = {
        "business_id": "biz-mia",
        "branch_id": "branch-gangbuk-mia",
        "browser_session_id": "sess-002",
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
    }
    first = service.collect_bank_account_transactions(account["id"], payload, ADMIN_USER)
    second = service.collect_bank_account_transactions(account["id"], payload, ADMIN_USER)

    assert first["collection"]["imported_rows"] == 1
    assert second["collection"]["imported_rows"] == 0
    assert second["collection"]["duplicate_rows"] == 1
    assert second["collection"]["status"] == "no_records"


def test_collect_bank_account_browser_work_key_in_collection_diagnostics(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)

    def fake_run_async(coro):
        close = getattr(coro, "close", None)
        if close:
            close()
        return {
            "status": "action_required",
            "error_code": "BANK_BROWSER_SESSION_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": {"browser_work_key": "custom-work-key"},
            "message": "세션 필요",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", fake_run_async)

    result = service.collect_bank_account_transactions(
        account["id"],
        {
            "business_id": "biz-mia",
            "branch_id": "branch-gangbuk-mia",
            "browser_work_key": "custom-work-key",
        },
        ADMIN_USER,
    )

    collection = result["collection"]
    assert collection["status"] == "action_required"
    # browser_work_key should appear in the collection block
    assert "browser_work_key" in str(collection)


def test_collect_bank_account_browser_connector_status_on_failure(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)

    def fake_run_async(coro):
        close = getattr(coro, "close", None)
        if close:
            close()
        return {
            "status": "failed",
            "error_code": "BANK_BROWSER_PAGE_ERROR",
            "rows": [],
            "row_count": 0,
            "diagnostics": {},
            "message": "페이지 접근 실패",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", fake_run_async)

    result = service.collect_bank_account_transactions(
        account["id"],
        {"business_id": "biz-mia", "branch_id": "branch-gangbuk-mia", "browser_session_id": "sess-x"},
        ADMIN_USER,
    )

    assert result["collection"]["status"] == "failed"
    assert result["collection"]["connector_status"] == "FAILED"


def test_collect_bank_account_non_browser_still_works(tmp_path, monkeypatch):
    """Verify mock connection_type is unaffected by browser connector changes."""
    from pathlib import Path as _Path

    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    account = service.create_bank_account(
        {
            "business_id": "biz-mia",
            "branch_id": "branch-gangbuk-mia",
            "bank_code": "088",
            "bank_name": "신한은행",
            "account_number": "110-999-111111",
            "connection_type": "mock",
            "status": "active",
        },
        ADMIN_USER,
    )

    result = service.collect_bank_account_transactions(
        account["id"],
        {"business_id": "biz-mia", "branch_id": "branch-gangbuk-mia"},
        ADMIN_USER,
    )

    assert result["collection"]["connection_type"] == "mock"
    assert result["collection"]["status"] in {"completed", "no_records"}


def test_browser_connection_type_accepted_in_create(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    account = service.create_bank_account(
        {
            "business_id": "biz-mia",
            "bank_code": "003",
            "bank_name": "IBK기업은행",
            "account_number": "123-456-789",
            "connection_type": "browser",
            "status": "active",
        },
        ADMIN_USER,
    )

    assert account["connection_type"] == "browser"


def test_sensitive_data_not_in_collection_response(tmp_path, monkeypatch):
    """Verify raw account numbers/passwords never appear in collect response."""
    account = _make_browser_bank_account(monkeypatch, tmp_path)

    def fake_run_async(coro):
        close = getattr(coro, "close", None)
        if close:
            close()
        return {
            "status": "collected",
            "rows": [{"occurred_at": "2026-08-05", "direction": "in", "amount": 50000, "memo": "test"}],
            "row_count": 1,
            "diagnostics": {"auth_mode": "pc_agent_browser"},
            "message": "1건",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", fake_run_async)

    result = service.collect_bank_account_transactions(
        account["id"],
        {"business_id": "biz-mia", "branch_id": "branch-gangbuk-mia", "browser_session_id": "safe"},
        ADMIN_USER,
    )

    result_str = str(result)
    assert "456789" not in result_str
    assert "110-123" not in result_str
    assert "account_number" not in result_str or "masked" in result_str
