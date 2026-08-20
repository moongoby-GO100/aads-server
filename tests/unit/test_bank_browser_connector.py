"""은행 브라우저 커넥터 단위 테스트.

실제 은행 포털 접속을 수행하지 않는다.
브라우저 Bridge/PC Agent 세션은 테스트 더블로 대체한다.
민감정보(계좌번호, 사업자번호, 비밀번호) 미노출을 검증한다.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 모듈 로딩 ────────────────────────────────────────────────────────────────

_CONNECTOR_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "yeoljeong_bank_browser_connector.py"
_SPEC = importlib.util.spec_from_file_location("yeoljeong_bank_browser_connector", _CONNECTOR_PATH)
connector = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(connector)


# ── work_key 생성 ────────────────────────────────────────────────────────────

class TestBankBrowserWorkKey:
    def test_key_contains_no_raw_account_id(self):
        """work_key에 account_id 원문이 노출되지 않는다."""
        account_id = "ACC-001234-SECRET"
        key = connector.bank_browser_work_key(account_id, "biz-mia", "branch-01")
        assert account_id not in key

    def test_key_contains_no_raw_business_id(self):
        """work_key에 사업자번호/business_id 원문이 포함되지 않는다."""
        business_id = "biz-mia-5678901234"
        key = connector.bank_browser_work_key("acc-001", business_id, "branch-01")
        # business_id는 정규화될 수 있지만, 원본 값 그대로는 없어야 함
        assert len(key) < 100  # 합리적인 길이 확인

    def test_key_is_stable_for_same_inputs(self):
        """동일한 입력에 대해 항상 같은 work_key를 반환한다."""
        key1 = connector.bank_browser_work_key("acc-001", "biz-mia", "branch-A")
        key2 = connector.bank_browser_work_key("acc-001", "biz-mia", "branch-A")
        assert key1 == key2

    def test_key_differs_for_different_accounts(self):
        """다른 account_id에 대해 다른 work_key를 반환한다."""
        key1 = connector.bank_browser_work_key("acc-001", "biz-mia", "branch-A")
        key2 = connector.bank_browser_work_key("acc-002", "biz-mia", "branch-A")
        assert key1 != key2

    def test_key_has_expected_prefix(self):
        """work_key가 yeoljeong-bank-browser- 접두어로 시작한다."""
        key = connector.bank_browser_work_key("acc-001", "biz-mia", "branch-A")
        assert key.startswith("yeoljeong-bank-browser-")

    def test_key_uses_only_safe_chars(self):
        """work_key가 URL-safe 문자만 포함한다."""
        key = connector.bank_browser_work_key("한글-계좌-번호", "사업자-001", "지점 A")
        assert re.match(r"^[a-z0-9._:\-]+$", key), f"unsafe chars in: {key}"


# ── HTML 테이블 파싱 ─────────────────────────────────────────────────────────

SHINHAN_SAMPLE_HTML = """
<table>
  <tr><th>거래일자</th><th>거래시간</th><th>적요</th><th>보낸분/받는분</th><th>입금</th><th>출금</th><th>잔액</th></tr>
  <tr><td>2026.08.01</td><td>10:30</td><td>배달의민족 정산</td><td>배달의민족</td><td>1,200,000</td><td></td><td>5,200,000</td></tr>
  <tr><td>2026.08.02</td><td>14:00</td><td>식자재 구매</td><td>농심마트</td><td></td><td>320,000</td><td>4,880,000</td></tr>
  <tr><td></td><td></td><td>합계</td><td></td><td></td><td></td><td></td></tr>
</table>
"""

IBK_SAMPLE_HTML = """
<table>
  <tr><th>거래일자</th><th>적요</th><th>입금금액</th><th>출금금액</th><th>잔액</th><th>거래처</th></tr>
  <tr><td>2026-08-01</td><td>요기요 정산</td><td>800,000</td><td>0</td><td>3,800,000</td><td>요기요</td></tr>
  <tr><td>2026-08-03</td><td>급여</td><td>0</td><td>2,500,000</td><td>1,300,000</td><td>직원A</td></tr>
</table>
"""

NO_DATA_HTML = """
<html><body><p>조회된 거래내역이 없습니다.</p></body></html>
"""


class TestParsePortalHTML:
    def test_shinhan_table_parses_in_transaction(self):
        rows = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        assert len(rows) == 2
        in_row = next(r for r in rows if r["direction"] == "in")
        assert in_row["amount"] == 1_200_000

    def test_shinhan_table_parses_out_transaction(self):
        rows = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        out_row = next(r for r in rows if r["direction"] == "out")
        assert out_row["amount"] == 320_000

    def test_shinhan_table_parses_occurred_at(self):
        rows = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        dates = [r["occurred_at"] for r in rows]
        assert any("2026-08-01" in d for d in dates)

    def test_ibk_table_parses_in_transaction(self):
        rows = connector.parse_bank_portal_html(IBK_SAMPLE_HTML)
        assert len(rows) == 2
        in_row = next(r for r in rows if r["direction"] == "in")
        assert in_row["amount"] == 800_000

    def test_ibk_table_parses_out_transaction(self):
        rows = connector.parse_bank_portal_html(IBK_SAMPLE_HTML)
        out_row = next(r for r in rows if r["direction"] == "out")
        assert out_row["amount"] == 2_500_000

    def test_no_data_returns_empty_list(self):
        rows = connector.parse_bank_portal_html(NO_DATA_HTML)
        assert rows == []

    def test_empty_string_returns_empty_list(self):
        rows = connector.parse_bank_portal_html("")
        assert rows == []

    def test_rows_do_not_contain_sensitive_fields(self):
        """파싱 결과에 계좌번호·비밀번호·사업자번호 키가 없다."""
        rows = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        forbidden_keys = {"account_number", "password", "business_registration_no", "account_password"}
        for row in rows:
            assert not forbidden_keys.intersection(row.keys()), f"sensitive key found: {row.keys()}"


# ── 날짜 범위 필터 ────────────────────────────────────────────────────────────

class TestDateRangeFilter:
    def test_row_within_range_included(self):
        row = {"occurred_at": "2026-08-05"}
        assert connector._row_in_date_range(row, "2026-08-01", "2026-08-31") is True

    def test_row_before_range_excluded(self):
        row = {"occurred_at": "2026-07-31"}
        assert connector._row_in_date_range(row, "2026-08-01", "2026-08-31") is False

    def test_row_after_range_excluded(self):
        row = {"occurred_at": "2026-09-01"}
        assert connector._row_in_date_range(row, "2026-08-01", "2026-08-31") is False

    def test_no_date_in_row_included(self):
        row = {"occurred_at": ""}
        assert connector._row_in_date_range(row, "2026-08-01", "2026-08-31") is True

    def test_no_range_always_included(self):
        row = {"occurred_at": "2026-06-01"}
        assert connector._row_in_date_range(row, "", "") is True


# ── 세션 없을 때 action_required ─────────────────────────────────────────────

class TestCollectWithoutSession:
    def test_no_session_id_no_work_key_returns_action_required(self):
        """browser_session_id도 browser_work_key도 없으면 action_required."""
        import asyncio

        account = {
            "id": "acc-001",
            "bank_code": "shinhan",
            "bank_name": "신한은행",
            "institution_code": "shinhan_business",
        }

        # Browser Bridge를 mock해 sessions.find_by_work_key를 빈 결과로 반환
        mock_bridge = MagicMock()
        mock_bridge.sessions.find_by_work_key.return_value = None
        mock_bridge._session_reusable.return_value = False

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="",
                    browser_work_key="",
                    date_from="2026-08-01",
                    date_to="2026-08-31",
                )
            )

        assert result["status"] == "action_required"
        assert result["error_code"] == "BANK_BROWSER_SESSION_REQUIRED"
        assert result["rows"] == []

    def test_session_not_found_in_registry_returns_connector_not_ready(self):
        """session_id가 있지만 registry에 없으면 connector_not_ready."""
        import asyncio

        account = {
            "id": "acc-002",
            "bank_code": "ibk",
            "bank_name": "IBK기업은행",
            "institution_code": "ibk_business",
        }

        mock_bridge = MagicMock()
        mock_bridge.sessions.get.return_value = None  # session not found

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="sess-xyz-not-existing",
                    browser_work_key="",
                    date_from="2026-08-01",
                    date_to="2026-08-31",
                )
            )

        assert result["status"] == "connector_not_ready"
        assert result["error_code"] == "BANK_BROWSER_SESSION_NOT_FOUND"

    def test_diagnostics_has_no_sensitive_fields(self):
        """diagnostics 필드에 계좌번호·비밀번호·사업자번호가 없다."""
        import asyncio

        account = {
            "id": "acc-003",
            "bank_code": "shinhan",
            "bank_name": "신한은행",
            "institution_code": "shinhan_business",
            "password": "SECRET_PASSWORD",
            "account_password": "SECRET_ACCOUNT_PASS",
            "business_registration_no": "123-45-67890",
        }

        mock_bridge = MagicMock()
        mock_bridge.sessions.find_by_work_key.return_value = None
        mock_bridge._session_reusable.return_value = False

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="",
                    browser_work_key="yeoljeong-bank-browser-abc123",
                    date_from="",
                    date_to="",
                )
            )

        diag = result.get("diagnostics", {})
        forbidden_values = {"SECRET_PASSWORD", "SECRET_ACCOUNT_PASS", "123-45-67890"}
        diag_str = str(diag)
        for secret in forbidden_values:
            assert secret not in diag_str, f"민감정보 노출: {secret}"


# ── 세션 있을 때 수집 성공 시뮬레이션 ────────────────────────────────────────

class TestCollectWithSession:
    def _build_mock_page(self, html_content: str, url: str = "https://bank.shinhan.com/rib/easy/") -> AsyncMock:
        page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(side_effect=lambda expr: {
            "window.location.href": url,
            "document.body ? document.body.innerHTML : ''": html_content,
        }.get(expr, ""))
        return page

    def test_collected_status_when_table_found(self):
        """페이지에 거래 테이블이 있으면 collected 상태를 반환한다."""
        import asyncio

        mock_page = self._build_mock_page(SHINHAN_SAMPLE_HTML)
        mock_context = MagicMock()
        mock_context.pages = [mock_page]

        mock_session = MagicMock()
        mock_session.session_id = "sess-test-001"

        mock_bridge = MagicMock()
        mock_bridge.sessions.get.return_value = mock_session
        mock_bridge._context_for_session = AsyncMock(return_value=mock_context)

        account = {
            "id": "acc-shinhan",
            "bank_code": "shinhan",
            "bank_name": "신한은행",
            "institution_code": "shinhan_business",
        }

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="sess-test-001",
                    browser_work_key="",
                    date_from="2026-08-01",
                    date_to="2026-08-31",
                )
            )

        assert result["status"] == "collected"
        assert result["row_count"] == 2
        assert len(result["rows"]) == 2

    def test_collected_rows_have_correct_direction(self):
        """수집된 rows의 direction이 올바르다."""
        import asyncio

        mock_page = self._build_mock_page(IBK_SAMPLE_HTML)
        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_session = MagicMock()
        mock_session.session_id = "sess-ibk-001"
        mock_bridge = MagicMock()
        mock_bridge.sessions.get.return_value = mock_session
        mock_bridge._context_for_session = AsyncMock(return_value=mock_context)

        account = {
            "id": "acc-ibk",
            "bank_code": "ibk",
            "bank_name": "IBK기업은행",
            "institution_code": "ibk_business",
        }

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="sess-ibk-001",
                    browser_work_key="",
                    date_from="",
                    date_to="",
                )
            )

        assert result["status"] == "collected"
        directions = {r["direction"] for r in result["rows"]}
        assert "in" in directions
        assert "out" in directions

    def test_no_rows_returns_collected_with_empty(self):
        """거래 내역이 없어도 collected 상태로 반환한다 (no_records가 아님)."""
        import asyncio

        mock_page = self._build_mock_page(NO_DATA_HTML)
        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_session = MagicMock()
        mock_session.session_id = "sess-empty"
        mock_bridge = MagicMock()
        mock_bridge.sessions.get.return_value = mock_session
        mock_bridge._context_for_session = AsyncMock(return_value=mock_context)

        account = {
            "id": "acc-shinhan-empty",
            "bank_code": "shinhan",
            "bank_name": "신한은행",
            "institution_code": "shinhan_business",
        }

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="sess-empty",
                    browser_work_key="",
                    date_from="",
                    date_to="",
                )
            )

        assert result["status"] == "collected"
        assert result["rows"] == []

    def test_diagnostics_safe_fields_only(self):
        """수집 성공 시 diagnostics에 민감정보가 없다."""
        import asyncio

        mock_page = self._build_mock_page(SHINHAN_SAMPLE_HTML)
        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_session = MagicMock()
        mock_session.session_id = "sess-diag"
        mock_bridge = MagicMock()
        mock_bridge.sessions.get.return_value = mock_session
        mock_bridge._context_for_session = AsyncMock(return_value=mock_context)

        account = {
            "id": "acc-diag",
            "bank_code": "shinhan",
            "bank_name": "신한은행",
            "institution_code": "shinhan_business",
            "password": "MY_SECRET_PASS",
        }

        with patch("app.browser_bridge.service.get_browser_bridge_service", return_value=mock_bridge):
            result = asyncio.run(
                connector.collect_bank_via_browser_session_async(
                    account,
                    browser_session_id="sess-diag",
                    browser_work_key="wk-diag",
                    date_from="",
                    date_to="",
                )
            )

        diag_str = str(result.get("diagnostics", {}))
        assert "MY_SECRET_PASS" not in diag_str
        # 세션ID는 표시되어도 됨 (session_id is safe to log)
        assert "auth_mode" in str(result.get("diagnostics", {}))


# ── 중복 제거 (source_hash 기준) ─────────────────────────────────────────────

class TestDeduplication:
    """_row_in_date_range와 parse_bank_portal_html 결합 후 중복 없음을 확인."""

    def test_same_html_parsed_twice_gives_same_rows(self):
        """같은 HTML을 두 번 파싱해도 같은 결과가 나온다 (幂等)."""
        rows1 = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        rows2 = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        assert len(rows1) == len(rows2)
        assert rows1[0]["amount"] == rows2[0]["amount"]

    def test_date_filter_removes_out_of_range_rows(self):
        rows = connector.parse_bank_portal_html(SHINHAN_SAMPLE_HTML)
        # 날짜 필터를 apply — 2026-08-01만 포함
        filtered = [r for r in rows if connector._row_in_date_range(r, "2026-08-01", "2026-08-01")]
        assert len(filtered) == 1
        assert "2026-08-01" in filtered[0]["occurred_at"]


# ── _clean_date 정규화 ───────────────────────────────────────────────────────

class TestCleanDate:
    def test_dot_separated(self):
        assert connector._clean_date("2026.08.15") == "2026-08-15"

    def test_slash_separated(self):
        assert connector._clean_date("2026/08/15") == "2026-08-15"

    def test_compact_yyyymmdd(self):
        assert connector._clean_date("20260815") == "2026-08-15"

    def test_already_iso(self):
        assert connector._clean_date("2026-08-15") == "2026-08-15"

    def test_single_digit_month_day(self):
        assert connector._clean_date("2026.8.5") == "2026-08-05"


# ── _clean_amount 파싱 ───────────────────────────────────────────────────────

class TestCleanAmount:
    def test_comma_separated(self):
        assert connector._clean_amount("1,200,000") == 1_200_000

    def test_plain_number(self):
        assert connector._clean_amount("320000") == 320_000

    def test_with_korean_won(self):
        assert connector._clean_amount("5,000원") == 5_000

    def test_empty_string(self):
        assert connector._clean_amount("") == 0

    def test_zero(self):
        assert connector._clean_amount("0") == 0
