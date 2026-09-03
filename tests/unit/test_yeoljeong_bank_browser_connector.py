"""Unit tests for the bank browser connector and service integration."""
from __future__ import annotations

import asyncio
import base64
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


class _ChallengePage:
    def __init__(self, url: str, text: str = ""):
        self.url = url
        self.text = text
        self.frames = []

    async def evaluate(self, expression, *args, **kwargs):
        if expression == "window.location.href":
            return self.url
        return self.text


class _PcAgentTabsChallengePage(_ChallengePage):
    async def _run_browser_command(self, command_type, params, **kwargs):
        assert command_type == "browser_tabs"
        return {
            "tabs": [
                {
                    "title": "간편조회서비스 | 신한은행 개인뱅킹",
                    "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
                    "type": "page",
                },
                {
                    "title": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html",
                    "url": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html",
                    "type": "iframe",
                },
            ]
        }


class _PcAgentNestedTabsChallengePage(_ChallengePage):
    async def _run_browser_command(self, command_type, params, **kwargs):
        assert command_type == "browser_tabs"
        return {
            "status": "success",
            "data": {
                "tabs": [
                    {
                        "title": "YESKEY",
                        "url": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html",
                        "type": "page",
                    }
                ]
            },
        }


class _ShinhanFincertThenIdpwPage(_ChallengePage):
    def __init__(self):
        super().__init__("https://bank.shinhan.com/rib/easy/index.jsp")
        self.frames = [type("Frame", (), {"url": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html"})()]
        self.closed_certificate_tab = False
        self.goto_calls = []

    async def _run_browser_command(self, command_type, params, **kwargs):
        if command_type == "browser_tabs":
            tabs = [
                {
                    "title": "간편조회서비스 | 신한은행 개인뱅킹",
                    "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
                    "type": "page",
                }
            ]
            if not self.closed_certificate_tab:
                tabs.append(
                    {
                        "title": "YESKEY",
                        "url": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html",
                        "type": "page",
                    }
                )
            return {"status": "success", "data": {"tabs": tabs}}
        if command_type == "browser_close_tab":
            assert params["url_pattern"] == "fincert|yeskey|cert"
            self.closed_certificate_tab = True
            self.frames = []
            return {"status": "success", "data": {"closed": 1, "remaining": 1}}
        return {"status": "success", "data": {}}

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url
        self.frames = []

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def evaluate(self, expression, *args, **kwargs):
        if expression == "window.location.href":
            return self.url
        if "querySelectorAll('table')" in expression:
            return []
        if "idpw|idlogin" in expression:
            return {"selected": "1"}
        if "document.body ? String(document.body.innerText" in expression:
            return "이용자 ID 로그인 아이디 비밀번호"
        if expression == "document.body ? document.body.innerHTML : ''":
            return "<html><body>이용자 ID 로그인<input id='ibx_loginId'><input id='비밀번호' type='password'></body></html>"
        return []


class _ShinhanPostIdpwFincertPage(_ChallengePage):
    def __init__(self):
        super().__init__("https://bank.shinhan.com/rib/easy/index.jsp")
        self.challenge_visible = False
        self.close_count = 0
        self.goto_calls = []

    async def _run_browser_command(self, command_type, params, **kwargs):
        if command_type == "browser_tabs":
            tabs = [
                {
                    "title": "간편조회서비스 | 신한은행 개인뱅킹",
                    "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
                    "type": "page",
                }
            ]
            if self.challenge_visible:
                tabs.append(
                    {
                        "title": "YESKEY",
                        "url": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html",
                        "type": "page",
                    }
                )
            return {"status": "success", "data": {"tabs": tabs}}
        if command_type == "browser_close_tab":
            self.challenge_visible = False
            self.close_count += 1
            return {"status": "success", "data": {"closed": 1, "remaining": 1}}
        return {"status": "success", "data": {}}

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def evaluate(self, expression, *args, **kwargs):
        if expression == "window.location.href":
            return self.url
        if "querySelectorAll('table')" in expression:
            return []
        if "idpw|idlogin" in expression:
            return {"selected": "1"}
        if "document.body ? String(document.body.innerText" in expression or "document.body.innerText" in expression:
            return "이용자 ID 로그인 아이디 비밀번호"
        if expression == "document.body ? document.body.innerHTML : ''":
            return "<html><body>이용자 ID 로그인<input id='ibx_loginId'><input id='비밀번호' type='password'></body></html>"
        return []


@pytest.mark.asyncio
async def test_shinhan_fincert_iframe_is_detected_without_reading_secret():
    page = _ChallengePage("https://bank.shinhan.com/rib/easy/index.jsp")
    page.frames = [type("Frame", (), {"url": "https://4user.yeskey.or.kr/fincert/web/v1/fincert.html"})()]

    result = await connector._detect_shinhan_auth_challenge(page, [page])

    assert result == {
        "screen_state": "certificate_password_required",
        "screen_reason_code": "SHINHAN_FINCERT_IFRAME_DETECTED",
        "screen_suggested_action": "complete_financial_certificate_then_retry_same_work_key",
        "suggested_action": "complete_financial_certificate_then_retry_same_work_key",
        "screen_requires_operator": "1",
        "last_observed_stage": "financial certificate iframe",
    }
    assert "password" not in result


@pytest.mark.asyncio
async def test_shinhan_fincert_iframe_is_detected_from_pc_agent_tabs():
    page = _PcAgentTabsChallengePage("https://bank.shinhan.com/rib/easy/index.jsp")

    result = await connector._detect_shinhan_auth_challenge(page, [page])

    assert result["screen_state"] == "certificate_password_required"
    assert result["screen_reason_code"] == "SHINHAN_FINCERT_IFRAME_DETECTED"
    assert result["screen_requires_operator"] == "1"
    assert result["suggested_action"] == "complete_financial_certificate_then_retry_same_work_key"
    assert "password" not in result


@pytest.mark.asyncio
async def test_shinhan_fincert_iframe_is_detected_from_nested_pc_agent_tabs():
    page = _PcAgentNestedTabsChallengePage("https://bank.shinhan.com/rib/easy/index.jsp")

    result = await connector._detect_shinhan_auth_challenge(page, [page])

    assert result["screen_state"] == "certificate_password_required"
    assert result["screen_reason_code"] == "SHINHAN_FINCERT_IFRAME_DETECTED"
    assert result["screen_requires_operator"] == "1"
    assert "password" not in result


def test_collect_async_shinhan_saved_idpw_prefers_id_login_over_fincert():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    mock_page = _ShinhanFincertThenIdpwPage()
    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-shinhan-idpw"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge, patch.object(
        connector,
        "_try_shinhan_individual_login_step",
        AsyncMock(
            return_value={
                "attempted": "1",
                "stage": "login",
                "username": "1",
                "login_secret": "1",
                "keyboard_secret": "1",
                "navigation_clicked": "1",
                "websquare_triggered": "1",
            }
        ),
    ) as mock_login:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-shinhan-idpw",
                browser_work_key="yeoljeong-bank-shinhan-idpw",
                date_from="2026-08-01",
                date_to="2026-08-31",
                login_username="bank-user",
                login_password="bank-pass",
                account_no="110123456789",
                account_password="4321",
                business_registration_no="1234567890",
            )
        )

    assert mock_login.await_count >= 1
    assert result.get("error_code") != "BANK_BROWSER_AUTH_CHALLENGE_DETECTED"
    assert result["diagnostics"]["shinhan_auth_challenge_policy"] == "prefer_saved_idpw_login"
    assert result["diagnostics"]["shinhan_idpw_login_reset"]["certificate_tab_closed"] == "1"
    assert mock_page.goto_calls == ["https://bank.shinhan.com/rib/easy/index.jsp"]
    assert "bank-pass" not in str(result["diagnostics"])
    assert "4321" not in str(result["diagnostics"])


def test_collect_async_shinhan_retries_idpw_after_post_login_fincert():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    mock_page = _ShinhanPostIdpwFincertPage()
    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-shinhan-idpw-retry"
    login_calls = {"count": 0}

    async def login_step(*args, **kwargs):
        login_calls["count"] += 1
        if login_calls["count"] == 1:
            mock_page.challenge_visible = True
        return {
            "attempted": "1",
            "stage": "login",
            "username": "1",
            "login_secret": "1",
            "navigation_clicked": "1",
            "websquare_triggered": "1",
        }

    mock_login = AsyncMock(side_effect=login_step)
    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge, patch.object(
        connector,
        "_try_shinhan_individual_login_step",
        mock_login,
    ):
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-shinhan-idpw-retry",
                browser_work_key="yeoljeong-bank-shinhan-idpw-retry",
                date_from="2026-08-01",
                date_to="2026-08-31",
                login_username="bank-user",
                login_password="bank-pass",
                account_no="110123456789",
                account_password="4321",
                business_registration_no="1234567890",
            )
        )

    assert mock_login.await_count >= 2
    assert mock_page.close_count == 1
    assert result["diagnostics"]["shinhan_auth_challenge_policy"] == "retry_saved_idpw_login"
    assert result["diagnostics"]["shinhan_idpw_login_retried_after_certificate"] == "1"
    assert result.get("error_code") != "BANK_BROWSER_AUTH_CHALLENGE_DETECTED"
    assert "bank-pass" not in str(result["diagnostics"])
    assert "4321" not in str(result["diagnostics"])


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


def test_shinhan_individual_browser_work_key_is_scope_stable_and_opaque():
    key1 = connector.shinhan_individual_browser_work_key("biz-mia", "branch-gangbuk-mia")
    key2 = connector.shinhan_individual_browser_work_key("biz-mia", "branch-gangbuk-mia")
    key3 = connector.shinhan_individual_browser_work_key("biz-mia", "branch-other")

    assert key1 == key2
    assert key1 != key3
    assert key1.startswith("yeoljeong-bank-shinhan-individual-")
    assert "biz-mia" not in key1
    assert "branch" not in key1


def test_bank_eval_timeout_uses_bank_safe_defaults(monkeypatch):
    monkeypatch.delenv("YEOLJEONG_BANK_BROWSER_EVAL_TIMEOUT_MULTIPLIER", raising=False)
    monkeypatch.delenv("YEOLJEONG_BANK_BROWSER_MIN_EVAL_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("YEOLJEONG_BANK_BROWSER_MAX_EVAL_TIMEOUT_MS", raising=False)

    assert connector._bank_eval_timeout_ms(8000) == 12000
    assert connector._bank_eval_timeout_ms(25000) == 25000
    assert connector._bank_eval_timeout_ms(60000) == 45000


def test_bank_eval_timeout_respects_env_caps(monkeypatch):
    monkeypatch.setenv("YEOLJEONG_BANK_BROWSER_EVAL_TIMEOUT_MULTIPLIER", "4")
    monkeypatch.setenv("YEOLJEONG_BANK_BROWSER_MIN_EVAL_TIMEOUT_MS", "20000")
    monkeypatch.setenv("YEOLJEONG_BANK_BROWSER_MAX_EVAL_TIMEOUT_MS", "45000")

    assert connector._bank_eval_timeout_ms(8000) == 32000
    assert connector._bank_eval_timeout_ms(30000) == 45000


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


def test_parse_shinhan_sortable_header_without_rows_is_not_failure():
    """신한 WebSquare 정렬 헤더만 있고 거래행이 없으면 0건으로 판정한다."""
    html_content = """
    <table>
      <tr>
        <th>거래일자오름차순 정렬</th><th>시간</th><th>거래일시오름차순 정렬</th>
        <th>적요오름차순 정렬</th><th>출금(원)오름차순 정렬</th>
        <th>입금(원)오름차순 정렬</th><th>내용오름차순 정렬</th><th>잔액(원)오름차순 정렬</th>
      </tr>
    </table>
    """
    rows, diag = connector.parse_bank_portal_html_with_diagnostics(html_content)
    assert rows == []
    assert diag["table_count"] == 1
    assert diag["transaction_header_found"] is True
    assert diag["parse_failure"] is False


def test_parse_shinhan_sortable_header_rows():
    """신한 WebSquare 정렬 문구가 붙은 헤더의 실제 거래행을 파싱한다."""
    html_content = """
    <table>
      <tr>
        <th>거래일자오름차순 정렬</th><th>시간</th><th>적요오름차순 정렬</th>
        <th>출금(원)오름차순 정렬</th><th>입금(원)오름차순 정렬</th><th>잔액(원)오름차순 정렬</th>
      </tr>
      <tr><td>2026.08.24</td><td>09:15:00</td><td>배달정산</td><td></td><td>123,000</td><td>1,000,000</td></tr>
    </table>
    """
    rows, diag = connector.parse_bank_portal_html_with_diagnostics(html_content)
    assert diag["parse_failure"] is False
    assert len(rows) == 1
    assert rows[0]["occurred_at"] == "2026-08-24 09:15:00"
    assert rows[0]["direction"] == "in"
    assert rows[0]["amount"] == 123000


def test_parse_shinhan_download_csv_extracts_rows():
    csv_text = "\n".join(
        [
            "거래일자,거래시간,기재내용,맡기신금액,찾으신금액,잔액",
            "2026.08.25,10:11:12,배달정산,123000,,1000000",
            "2026.08.26,09:00:00,식자재,,45000,955000",
        ]
    )

    rows, diag = connector.parse_bank_download_content(csv_text, "shinhan-statement.csv")

    assert diag["download_parser"] == "delimited"
    assert diag["download_parse_failure"] is False
    assert len(rows) == 2
    assert rows[0]["source"] == "bank-browser-download"
    assert rows[0]["occurred_at"] == "2026-08-25 10:11:12"
    assert rows[0]["direction"] == "in"
    assert rows[1]["direction"] == "out"
    assert rows[1]["amount"] == 45000


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
    return asyncio.run(coro)


def test_password_manager_fallback_only_focuses_login_field():
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=True)

    assert _run(connector._trigger_password_manager_fallback(page)) is True
    script = page.evaluate.await_args.args[0]
    assert ".focus()" in script
    assert ".click()" not in script
    assert "value" not in script
    assert "submit" not in script.lower()


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
    assert result["error_code"] == "PC_AGENT_LOGIN_REQUIRED"
    assert result["rows"] == []
    assert "PC Agent" in result["message"]


def test_collect_async_auto_opens_bank_work_session_when_enabled():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    mock_page = AsyncMock()
    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return "로그인 필요"
        return []

    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]

    mock_session = MagicMock()
    mock_session.session_id = "auto-session-001"
    mock_session.endpoint.metadata = {
        "window_position": {"x": 0, "y": 0},
        "window_size": {"width": 1280, "height": 960},
        "window_layout_policy": "bank_dedicated_left",
    }

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.find_by_work_key.return_value = None
        bridge_inst.ensure_work_session = AsyncMock(return_value=mock_session)
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="",
                browser_work_key="yeoljeong-bank-browser-auto",
                date_from="2026-08-01",
                date_to="2026-08-31",
                auto_open_browser=True,
            )
        )

    assert result["status"] == "action_required"
    assert result["error_code"] == "BANK_BROWSER_OPERATOR_ACTION_REQUIRED"
    assert result["diagnostics"]["browser_session_id"] == "auto-session-001"
    assert result["diagnostics"]["auto_opened_session"] == "1"
    assert result["diagnostics"]["browser_window_layout_policy"] == "bank_dedicated_left"
    assert result["diagnostics"]["browser_focus_policy"] == "cdp_target_by_portal_url_not_os_foreground"
    assert any(
        item.get("stage") == "shinhan_browser_focus_guard"
        for item in result["diagnostics"]["shinhan_stage_logs"]
    )
    bridge_inst.ensure_work_session.assert_awaited_once()
    mock_page.goto.assert_not_called()


def test_collect_async_auto_open_reused_login_page_requires_operator_action():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    mock_page = AsyncMock()
    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            return [[["회원구분", "이용가능범위"], ["인증서", "계좌조회"]]]
        if "document.body.innerText" in expr:
            return "회원구분 이용가능범위"
        return []

    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]

    mock_session = MagicMock()
    mock_session.session_id = "reused-session-001"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.find_by_work_key.return_value = mock_session
        bridge_inst._session_reusable.return_value = True
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="",
                browser_work_key="yeoljeong-bank-browser-reused",
                date_from="2026-08-01",
                date_to="2026-08-31",
                auto_open_browser=True,
            )
        )

    assert result["status"] == "action_required"
    assert result["error_code"] == "BANK_BROWSER_OPERATOR_ACTION_REQUIRED"
    assert result["diagnostics"]["browser_session_id"] == "reused-session-001"
    bridge_inst.ensure_work_session.assert_not_called()
    mock_page.goto.assert_not_called()


def test_collect_async_auto_open_preserves_route_error_diagnostics():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    class RouteError(RuntimeError):
        error_code = "COMMAND_TIMEOUT"
        detail = {"status": "error", "error_code": "COMMAND_TIMEOUT", "message": "browser launch timed out"}

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.find_by_work_key.return_value = None
        bridge_inst.ensure_work_session = AsyncMock(side_effect=RouteError("browser launch timed out"))

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="",
                browser_work_key="yeoljeong-bank-browser-timeout",
                date_from="2026-08-01",
                date_to="2026-08-31",
                auto_open_browser=True,
            )
        )

    assert result["status"] == "action_required"
    assert result["error_code"] == "PC_AGENT_LOGIN_REQUIRED"
    assert result["diagnostics"]["auto_open_browser"] == "failed"
    assert result["diagnostics"]["auto_open_error"] == "COMMAND_TIMEOUT"
    assert result["diagnostics"]["auto_open_error_detail"]["error_code"] == "COMMAND_TIMEOUT"


def test_collect_async_infers_portal_from_bank_name_when_codes_missing():
    account = {"id": "acct-1", "bank_name": "신한은행 기업", "bank_code": "", "institution_code": ""}

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
    assert result["diagnostics"]["institution_code"] == "shinhan_business"


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


def test_collect_async_no_records_is_collected_empty():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if expr == "document.body ? document.body.innerHTML : ''":
            return "<html><body>조회된 거래내역이 없습니다.</body></html>"
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-no-records"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-no-records",
                browser_work_key="yeoljeong-bank-browser-no-records",
                date_from="2026-08-01",
                date_to="2026-08-31",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 0
    assert result["diagnostics"]["screen_state"] == "no_records"


def test_collect_async_auth_challenge_focuses_and_returns_safe_diagnostics():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return ""
        if expr == "document.body ? document.body.innerHTML : ''":
            return "<html><body>OTP 인증번호를 입력하세요<input name='otpCode'></body></html>"
        if args and args[0] == "otp_required":
            return True
        return [{"tag": "input", "type": "text", "name": "otpCode", "label": "인증번호"}]

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-otp"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-otp",
                browser_work_key="yeoljeong-bank-browser-otp",
                date_from="2026-08-01",
                date_to="2026-08-31",
                auto_open_browser=True,
            )
        )

    assert result["status"] == "action_required"
    assert result["error_code"] == "BANK_BROWSER_AUTH_CHALLENGE_DETECTED"
    assert result["diagnostics"]["screen_state"] == "otp_required"
    assert result["diagnostics"]["auth_challenge_focus"] == "triggered"
    assert "selector_candidates" in result["diagnostics"]
    assert "110-123" not in str(result["diagnostics"])


def test_collect_async_parse_failed_clicks_transaction_view_and_reparses():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    html_pages = [
        "<html><body><table><tr><th>메뉴</th></tr><tr><td>빠른조회</td></tr></table><button>거래내역조회</button></body></html>",
        """
        <html><body>
          <table>
            <tr><th>거래일자</th><th>적요</th><th>입금금액</th><th>출금금액</th><th>거래후잔액</th></tr>
            <tr><td>2026.08.24</td><td>카드정산</td><td>120,000</td><td></td><td>500,000</td></tr>
          </table>
        </body></html>
        """,
    ]

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp#210000000000"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return ""
        if expr == "document.body ? document.body.innerHTML : ''":
            return html_pages.pop(0)
        if "scoreFor" in expr:
            return {"clicked": True, "label": "거래내역조회"}
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-nav"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-nav",
                browser_work_key="yeoljeong-bank-browser-nav",
                date_from="2026-08-01",
                date_to="2026-08-31",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert result["diagnostics"]["transaction_view_navigation"] == "triggered"
    assert result["diagnostics"]["screen_state"] == "transaction_table"


def test_collect_async_reuses_existing_bank_tab_without_goto():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp#already-open"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return "조회된 거래내역이 없습니다."
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-reuse"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-reuse",
                browser_work_key="yeoljeong-bank-browser-reuse",
                date_from="2026-08-01",
                date_to="2026-08-31",
                portal_url="https://bank.shinhan.com/rib/easy/index.jsp",
            )
        )

    mock_page.goto.assert_not_called()
    assert result["status"] == "collected"
    assert result["diagnostics"]["browser_tab_reused"] == "1"
    assert result["diagnostics"]["portal_navigation"] == "skipped_reusable_tab"


def test_collect_async_login_required_uses_saved_credentials_without_leaking():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    logged_in = {"value": False}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            if logged_in["value"]:
                return [
                    [
                        ["거래일자", "적요", "입금금액", "출금금액", "거래후잔액"],
                        ["2026.08.24", "카드정산", "120,000", "", "500,000"],
                    ]
                ]
            return []
        if "document.body.innerText" in expr:
            return "거래일자 입금금액" if logged_in["value"] else "login 아이디 비밀번호"
        if "setValue" in expr and args:
            creds = args[0]
            assert creds["username"] == "bank-user"
            assert creds["password"] == "bank-pass"
            assert creds["accountPassword"] == "4321"
            logged_in["value"] = True
            return {"username": True, "password": True, "accountPassword": True, "submitted": True}
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-login"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-login",
                browser_work_key="yeoljeong-bank-browser-login",
                date_from="2026-08-01",
                date_to="2026-08-31",
                login_username="bank-user",
                login_password="bank-pass",
                account_password="4321",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert result["diagnostics"]["bank_login_auto_fill"]["submitted"] == "1"
    assert "bank-pass" not in str(result["diagnostics"])
    assert "4321" not in str(result["diagnostics"])


def test_collect_async_shinhan_individual_flow_selects_account_and_date_range():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    query_submitted = {"value": False}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            if query_submitted["value"]:
                return [
                    [
                        ["거래일자", "적요", "입금금액", "출금금액", "거래후잔액"],
                        ["2026.08.24", "카드정산", "120,000", "", "500,000"],
                    ]
                ]
            return []
        if "document.body.innerText" in expr:
            return "거래일자 입금금액" if query_submitted["value"] else "간편조회서비스 계좌조회 아이디 비밀번호"
        if "shinhanQueryFlow" in expr and args:
            payload = args[0]
            assert payload["mode"] == "individual_simple"
            assert payload["username"] == "bank-user"
            assert payload["password"] == "bank-pass"
            assert payload["accountNo"] == "110123456789"
            assert payload["accountPassword"] == "4321"
            assert payload["dateFrom"] == "2026-08-01"
            assert payload["dateTo"] == "2026-08-31"
            query_submitted["value"] = True
            return {
                "attempted": "1",
                "mode": "individual_simple",
                "navigation_clicked": "1",
                "username": "1",
                "login_secret": "1",
                "account_selected": "1",
                "account_secret": "1",
                "date_from": "1",
                "date_to": "1",
                "query_submitted": "1",
            }
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-shinhan-individual"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-shinhan-individual",
                browser_work_key="yeoljeong-bank-browser-shinhan-individual",
                date_from="2026-08-01",
                date_to="2026-08-31",
                login_username="bank-user",
                login_password="bank-pass",
                account_no="110123456789",
                account_password="4321",
                business_entity_type="individual",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert result["diagnostics"]["shinhan_query_flow_mode"] == "individual_simple"
    assert result["diagnostics"]["shinhan_query_flow"]["account_selected"] == "1"
    assert result["diagnostics"]["shinhan_query_flow"]["date_from"] == "1"
    assert "bank-pass" not in str(result["diagnostics"])
    assert "4321" not in str(result["diagnostics"])
    assert "110123456789" not in str(result["diagnostics"])


def test_collect_async_shinhan_individual_flow_runs_state_machine_steps():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    step = {"value": "login"}
    stages: list[str] = []

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            if step["value"] == "done":
                return [
                    [
                        ["거래일자", "적요", "입금금액", "출금금액", "거래후잔액"],
                        ["2026.08.24", "카드정산", "120,000", "", "500,000"],
                    ]
                ]
            return []
        if "document.body.innerText" in expr:
            return {
                "login": "간편조회서비스 이용자ID 로그인 아이디 비밀번호",
                "account_page": "계좌조회 메뉴",
                "query": "계좌선택 계좌비밀번호 조회기간",
                "done": "거래일자 입금금액",
            }[step["value"]]
        if "shinhanQueryFlow" in expr and args:
            if step["value"] == "login":
                step["value"] = "account_page"
                stages.append("login")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "login",
                    "navigation_clicked": "1",
                    "username": "1",
                    "login_secret": "1",
                }
            if step["value"] == "account_page":
                step["value"] = "query"
                stages.append("account_page_navigation")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "account_page_navigation",
                    "navigation_clicked": "1",
                    "account_page_navigation": "1",
                }
            if step["value"] == "query":
                step["value"] = "done"
                stages.append("account_query")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "account_query",
                    "account_selected": "1",
                    "account_secret": "1",
                    "date_from": "1",
                    "date_to": "1",
                    "query_submitted": "1",
                }
            return {"attempted": "0", "mode": "individual_simple", "stage": "done"}
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-shinhan-state-machine"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-shinhan-state-machine",
                browser_work_key="yeoljeong-bank-browser-shinhan-state-machine",
                date_from="2026-08-24",
                date_to="2026-08-24",
                login_username="bank-user",
                login_password="bank-pass",
                account_no="110123456789",
                account_password="4321",
                business_entity_type="individual",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert stages == ["login", "account_page_navigation", "account_query"]
    flow = result["diagnostics"]["shinhan_query_flow"]
    assert flow["username"] == "1"
    assert flow["account_page_navigation"] == "1"
    assert flow["account_selected"] == "1"
    assert flow["query_submitted"] == "1"
    assert [item["stage"] for item in result["diagnostics"]["shinhan_query_flow_steps"]] == stages
    assert "bank-pass" not in str(result["diagnostics"])
    assert "4321" not in str(result["diagnostics"])
    assert "110123456789" not in str(result["diagnostics"])


def test_collect_async_shinhan_individual_flow_confirms_notice_before_account_query():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    step = {"value": "login"}
    stages: list[str] = []

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp#210000000000"
        if "querySelectorAll('table')" in expr:
            if step["value"] == "done":
                return [
                    [
                        ["거래일자", "시간", "적요", "출금(원)", "입금(원)", "내용", "잔액(원)"],
                        ["2026-08-24", "10:17:12", "FB자금", "0", "33,906", "741346195BC", "3,261,428"],
                    ]
                ]
            return []
        if "document.body.innerText" in expr:
            return {
                "login": "간편조회서비스 이용자ID 로그인 아이디 비밀번호",
                "notice": "ID로그인 시에는 이용 가능한 서비스가 제한되오니 업무에 참고 부탁드립니다. 확인",
                "account_page": "간편조회서비스 계좌조회",
                "query": "조회 계좌번호 직접입력 조회 계좌비밀번호 숫자 4자리 조회기간",
                "done": "거래일자 입금(원) 출금(원)",
            }[step["value"]]
        if "shinhanQueryFlow" in expr and args:
            if step["value"] == "login":
                step["value"] = "notice"
                stages.append("login")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "login",
                    "navigation_clicked": "1",
                    "username": "1",
                    "login_secret": "1",
                }
            if step["value"] == "notice":
                step["value"] = "account_page"
                stages.append("login_notice_confirm")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "login_notice_confirm",
                    "navigation_clicked": "1",
                    "notice_confirm": "1",
                }
            if step["value"] == "account_page":
                step["value"] = "query"
                stages.append("account_page_navigation")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "account_page_navigation",
                    "navigation_clicked": "1",
                    "account_page_navigation": "1",
                }
            if step["value"] == "query":
                step["value"] = "done"
                stages.append("account_query")
                return {
                    "attempted": "1",
                    "mode": "individual_simple",
                    "stage": "account_query",
                    "account_no": "1",
                    "account_direct_input": "1",
                    "account_selected": "1",
                    "account_secret": "1",
                    "date_from": "1",
                    "date_to": "1",
                    "query_submitted": "1",
                }
            return {"attempted": "0", "mode": "individual_simple", "stage": "done"}
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.on = MagicMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-shinhan-notice"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-shinhan-notice",
                browser_work_key="yeoljeong-bank-shinhan-individual-notice",
                date_from="2026-08-24",
                date_to="2026-08-24",
                login_username="bank-user",
                login_password="bank-pass",
                account_no="110298730240",
                account_password="4321",
                business_entity_type="individual",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert result["rows"][0]["amount"] == 33906
    assert stages == ["login", "login_notice_confirm", "account_page_navigation", "account_query"]
    flow = result["diagnostics"]["shinhan_query_flow"]
    assert flow["notice_confirm"] == "1"
    assert flow["account_direct_input"] == "1"
    assert flow["query_submitted"] == "1"
    assert result["diagnostics"]["shinhan_dialog_auto_accept"] == "installed"
    mock_page.on.assert_called()
    assert "bank-pass" not in str(result["diagnostics"])
    assert "4321" not in str(result["diagnostics"])
    assert "110298730240" not in str(result["diagnostics"])


def test_prepare_shinhan_individual_flow_uses_websquare_component_login_trigger():
    page = AsyncMock()

    async def evaluate(expr, *args, **kwargs):
        assert "componentById" in expr
        assert "triggerWebSquareEvent" in expr
        assert "sameOriginDocuments" in expr
        assert "websquare_triggered" in expr
        payload = args[0]
        assert payload["mode"] == "individual_simple"
        assert payload["username"] == "bank-user"
        assert payload["password"] == "bank-pass"
        return {
            "attempted": "1",
            "mode": "individual_simple",
            "stage": "login",
            "navigation_clicked": "1",
            "websquare_triggered": "1",
            "username": "1",
            "login_secret": "1",
        }

    page.evaluate = AsyncMock(side_effect=evaluate)

    result = _run(
        connector._try_prepare_shinhan_query_flow(
            page,
            flow_mode="individual_simple",
            username="bank-user",
            password="bank-pass",
            account_no="110123456789",
            account_password="4321",
            business_registration_no="1234567890",
            date_from="2026-08-24",
            date_to="2026-08-24",
        )
    )

    assert result["stage"] == "login"
    assert result["username"] == "1"
    assert result["login_secret"] == "1"
    assert result["navigation_clicked"] == "1"
    assert result["websquare_triggered"] == "1"
    assert "bank-pass" not in str(result)
    assert "4321" not in str(result)
    assert "110123456789" not in str(result)


def test_prepare_shinhan_individual_flow_reports_login_success_and_transkey_account_secret():
    page = AsyncMock()

    async def evaluate(expr, *args, **kwargs):
        assert "sameOriginDocuments" in expr
        assert "setTransKeyPassword" in expr
        assert "login_success" in expr
        payload = args[0]
        assert payload["accountPassword"] == "4321"
        return {
            "attempted": "1",
            "mode": "individual_simple",
            "stage": "account_query",
            "login_success": "1",
            "account_page_direct_hash": "1",
            "account_selected": "1",
            "account_resolved": "1",
            "account_secret": "1",
            "date_from": "1",
            "date_to": "1",
            "query_submitted": "1",
        }

    page.evaluate = AsyncMock(side_effect=evaluate)

    result = _run(
        connector._try_prepare_shinhan_query_flow(
            page,
            flow_mode="individual_simple",
            username="bank-user",
            password="bank-pass",
            account_no="110123456789",
            account_password="4321",
            business_registration_no="1234567890",
            date_from="2026-08-24",
            date_to="2026-08-24",
        )
    )

    assert result["stage"] == "account_query"
    assert result["login_success"] == "1"
    assert result["account_page_direct_hash"] == "1"
    assert result["account_selected"] == "1"
    assert result["account_resolved"] == "1"
    assert result["account_secret"] == "1"
    assert result["query_submitted"] == "1"
    assert "bank-pass" not in str(result)
    assert "4321" not in str(result)
    assert "110123456789" not in str(result)


def test_close_shinhan_security_notice_prefers_visible_popup_close():
    page = AsyncMock()

    async def evaluate(expr, *args, **kwargs):
        assert "btnTotalClose" in expr
        if "noticePatterns" in expr:
            assert "componentById" in expr
            return {"closed": "1", "notice": "1", "tag": "A", "id": "CO00038RP_1_btnmakedpopupclose"}
        return {"present": "0"}

    page.evaluate = AsyncMock(side_effect=evaluate)

    assert _run(connector._close_shinhan_security_notice(page)) is True
    assert page.evaluate.await_count == 2


def test_shinhan_keyboard_login_does_not_navigate_before_hidden_idpw_fill():
    page = AsyncMock()
    page._run_browser_command = AsyncMock()

    async def evaluate(expr, *args, **kwargs):
        assert "!loginIdEl && !passwordEl" in expr
        assert "mf_wfm_main_ibx_loginId" in expr
        assert "wq_uuid_769_scr_pwd" in expr
        return {
            "attempted": "1",
            "stage": "login_keyboard_prepare",
            "username": "1",
            "password_focused": "0",
            "navigation_clicked": "0",
            "websquare_triggered": "0",
        }

    page.evaluate = AsyncMock(side_effect=evaluate)

    result = _run(
        connector._try_shinhan_individual_keyboard_login_step(
            page,
            username="bank-user",
            password="bank-pass",
        )
    )

    assert result["stage"] == "login_keyboard_prepare"
    assert result["navigation_clicked"] == "0"
    assert result["keyboard_secret"] == "0"
    assert "bank-pass" not in str(result)
    page._run_browser_command.assert_not_awaited()


def test_shinhan_idpw_login_reports_success_marker_and_elapsed_time(monkeypatch):
    page = AsyncMock()

    monkeypatch.setattr(
        connector,
        "_close_shinhan_security_notice",
        AsyncMock(return_value=False),
    )

    async def evaluate(expr, *args, **kwargs):
        assert "loggedInMarker" in expr
        assert "fincert" in expr
        assert "yeskey" in expr
        assert "빠른조회|조회기간" not in expr
        payload = args[0]
        assert payload["username"] == "bank-user"
        return {
            "attempted": "1",
            "stage": "login",
            "username": "1",
            "login_secret": "1",
            "transkey_secret": "1",
            "navigation_clicked": "1",
            "websquare_triggered": "1",
            "login_submitted": "1",
            "login_success": "1",
            "login_success_reason": "post_login_text",
            "login_elapsed_ms": "2345",
        }

    page.evaluate = AsyncMock(side_effect=evaluate)

    result = _run(
        connector._try_shinhan_individual_login_step(
            page,
            username="bank-user",
            password="bank-pass",
        )
    )

    assert result["login_submitted"] == "1"
    assert result["login_success"] == "1"
    assert result["login_success_reason"] == "post_login_text"
    assert result["login_elapsed_ms"] == "2345"
    assert "bank-pass" not in str(result)


def test_shinhan_flow_stage_logs_are_secret_free_and_granular():
    logs = []

    connector._append_shinhan_flow_stage_logs(
        logs,
        step_result={
            "attempted": "1",
            "stage": "account_query",
            "account_selected": "1",
            "account_secret": "1",
            "date_from": "1",
            "date_to": "1",
            "query_submitted": "1",
        },
        started_at=connector.time.monotonic(),
        attempt_index=0,
    )

    assert [item["stage"] for item in logs] == [
        "shinhan_account_select",
        "shinhan_account_password_input",
        "shinhan_period_select",
        "shinhan_query_submit",
    ]
    assert all(item["status"] == "success" for item in logs)
    assert all("elapsed_ms" in item for item in logs)
    assert all("recorded_at" in item for item in logs)
    assert "bank-pass" not in str(logs)


def test_browser_collection_audit_redacts_secret_stage_fields():
    logs = []

    entry = connector.append_site_stage_log(
        logs,
        stage="portal_login",
        status="failed",
        started_at=connector.time.monotonic(),
        error_code="TIMEOUT",
        reason="login_form_wait_timeout",
        password="bank-pass",
        account_no="110123456789",
        success_condition="form_visible",
        timeout_ms=45000,
    )

    assert entry["password"] == "[REDACTED]"
    assert entry["account_no"] == "[REDACTED]"
    assert entry["success_condition"] == "form_visible"
    assert entry["timeout_ms"] == "45000"
    assert "bank-pass" not in str(logs)
    assert "110123456789" not in str(logs)


def test_browser_collection_audit_writes_secret_free_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "browser_stage_logs.jsonl"
    monkeypatch.setenv("AADS_BROWSER_STAGE_LOG_PATH", str(log_path))
    logs = []

    entry = connector.append_site_stage_log(
        logs,
        stage="shinhan_login_submit",
        status="failed",
        started_at=connector.time.monotonic(),
        event_name="shinhan_bank_collection_stage",
        error_code="COMMAND_TIMEOUT",
        password="bank-pass",
        account_no="110123456789",
        success_condition="login_button_clicked",
    )

    raw = log_path.read_text(encoding="utf-8")
    assert "shinhan_bank_collection_stage" in raw
    assert "bank-pass" not in raw
    assert "110123456789" not in raw
    assert '"password": "[REDACTED]"' in raw
    assert '"account_no": "[REDACTED]"' in raw
    assert entry["error_code"] == "COMMAND_TIMEOUT"


def test_shinhan_keyboard_login_fails_fast_on_security_verification_notice():
    page = AsyncMock()
    page.click = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page._run_browser_command = AsyncMock()

    async def evaluate(expr, *args, **kwargs):
        if "login_keyboard_prepare" in expr:
            assert "mf_wfm_main_ibx_loginId" in expr
            assert "wq_uuid_769_scr_pwd" in expr
            return {
                "attempted": "1",
                "stage": "login_keyboard_prepare",
                "username": "1",
                "password_focused": "1",
                "password_selector": "[id=\"비밀번호\"]",
            }
        if "fncIdLogin" in expr:
            assert "mf_wfm_main_btn_login" in expr
            return {"clicked": "1", "method": "fncIdLogin"}
        if "SHINHAN_KEYBOARD_VERIFICATION_FAILED" in expr:
            return {
                "present": "1",
                "error_code": "SHINHAN_KEYBOARD_VERIFICATION_FAILED",
                "notice_type": "SHINHAN_KEYBOARD_VERIFICATION_FAILED",
            }
        if "noticePatterns" in expr:
            return {"closed": "1", "notice": "1"}
        raise AssertionError("unexpected evaluate expression")

    page.evaluate = AsyncMock(side_effect=evaluate)

    result = _run(
        connector._try_shinhan_individual_keyboard_login_step(
            page,
            username="bank-user",
            password="bank-pass",
        )
    )

    assert result["attempted"] == "failed"
    assert result["stage"] == "login_keyboard_verification_failed"
    assert result["error_code"] == "SHINHAN_KEYBOARD_VERIFICATION_FAILED"
    assert result["keyboard_secret"] == "0"
    assert "bank-pass" not in str(result)
    page._run_browser_command.assert_any_await("keyboard_type", {"text": "bank-pass"}, command_timeout_seconds=21.5, queue_wait_timeout_seconds=10.0)


def test_prepare_shinhan_corporate_quick_flow_returns_safe_diagnostics():
    page = AsyncMock()

    async def evaluate(expr, *args):
        assert "shinhanQueryFlow" in expr
        payload = args[0]
        assert payload["mode"] == "corporate_quick"
        assert payload["accountNo"] == "110123456789"
        assert payload["accountPassword"] == "4321"
        assert payload["businessRegistrationNo"] == "1234567890"
        return {
            "attempted": "1",
            "mode": "corporate_quick",
            "navigation_clicked": "1",
            "account_no": "1",
            "account_secret": "1",
            "business_registration_no": "1",
            "date_from": "1",
            "date_to": "1",
            "query_submitted": "1",
        }

    page.evaluate = AsyncMock(side_effect=evaluate)

    result = _run(
        connector._try_prepare_shinhan_query_flow(
            page,
            flow_mode="corporate_quick",
            username="bank-user",
            password="bank-pass",
            account_no="110123456789",
            account_password="4321",
            business_registration_no="1234567890",
            date_from="2026-08-01",
            date_to="2026-08-31",
        )
    )

    assert result["mode"] == "corporate_quick"
    assert result["business_registration_no"] == "1"
    assert result["query_submitted"] == "1"
    assert "bank-pass" not in str(result)
    assert "4321" not in str(result)
    assert "110123456789" not in str(result)


def test_collect_async_login_recheck_uses_latest_challenge_state():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    logged_in = {"value": False}
    focus_calls = []
    password_manager_calls = []

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return "OTP 인증번호 입력" if logged_in["value"] else "login 아이디 비밀번호"
        if "setValue" in expr and args:
            logged_in["value"] = True
            return {"username": True, "password": True, "submitted": True}
        if "byState" in expr and args:
            focus_calls.append(args[0])
            return True
        if "autocomplete='username'" in expr:
            password_manager_calls.append(True)
            return True
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "sess-login-otp"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="sess-login-otp",
                browser_work_key="yeoljeong-bank-browser-login-otp",
                date_from="2026-08-01",
                date_to="2026-08-31",
                login_username="bank-user",
                login_password="bank-pass",
            )
        )

    assert result["status"] == "action_required"
    assert result["error_code"] == "BANK_BROWSER_AUTH_CHALLENGE_DETECTED"
    assert result["diagnostics"]["screen_state"] == "otp_required"
    assert result["diagnostics"]["auth_challenge_focus"] == "triggered"
    assert focus_calls == ["otp_required"]
    assert password_manager_calls == []


def test_collect_async_session_success_returns_parsed_rows():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return ""
        if expr == "document.body ? document.body.innerHTML : ''":
            return _SHINHAN_SAMPLE_HTML
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
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


def test_collect_async_uses_download_file_when_table_snapshot_has_no_rows():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}
    csv_text = "거래일자,거래시간,적요,입금액,출금액\n2026.08.25,10:11,배달정산,123000,\n"
    encoded = base64.b64encode(csv_text.encode("cp949")).decode("ascii")

    async def evaluate(expr, *args, **kwargs):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp#210101000000"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return "계좌조회 거래내역 엑셀 다운로드"
        if "data-aads-bank-download" in expr:
            return ['[data-aads-bank-download="aads-bank-download-0"]']
        if "aads-bank-statement-download" in expr:
            return ""
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.download = AsyncMock(
        return_value={
            "filename": "shinhan-statement.csv",
            "content_base64": encoded,
            "size": len(csv_text.encode("cp949")),
        }
    )
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "live-session-download"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="live-session-download",
                browser_work_key="yeoljeong-bank-browser-download",
                date_from="2026-08-01",
                date_to="2026-08-31",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert result["rows"][0]["source"] == "bank-browser-download"
    assert result["diagnostics"]["download_status"] == "parsed"
    assert result["diagnostics"]["screen_state"] == "transaction_download"


def test_collect_async_ibk_quick_flow_submits_query_and_parses_rows():
    account = {"id": "acct-ibk", "bank_name": "IBK기업은행", "bank_code": "003", "institution_code": "ibk_business"}
    state = {"queried": False}

    async def evaluate(expr, *args, **kwargs):
        if expr == "window.location.href":
            return "https://mybank.ibk.co.kr/uib/jsp/guest/qcs/qcs10/qcs1020/PQCS102000_i.jsp"
        if "ibkQuickFlow" in expr:
            state["queried"] = True
            return {
                "attempted": "1",
                "mode": "ibk_quick",
                "stage": "quick_query",
                "account_no": "1",
                "account_secret": "1",
                "business_registration_no": "1",
                "date_from": "1",
                "date_to": "1",
                "navigation_clicked": "1",
                "query_submitted": "1",
            }
        if "querySelectorAll('table')" in expr:
            if state["queried"]:
                return [
                    [
                        ["거래일자", "적요", "출금금액", "입금금액", "잔액"],
                        ["2026-08-10", "이체입금", "", "500,000", "2,000,000"],
                    ]
                ]
            return []
        if "document.body.innerText" in expr:
            return "IBK기업은행 빠른조회 계좌번호 계좌비밀번호 조회"
        if "querySelectorAll('input,button,select,a')" in expr:
            return []
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "live-session-ibk"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.return_value = mock_session
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="live-session-ibk",
                browser_work_key="yeoljeong-bank-ibk-business-abc123",
                date_from="2026-08-01",
                date_to="2026-08-31",
                account_no="saved-account",
                account_password="saved-secret",
                business_registration_no="saved-business-no",
            )
        )

    assert result["status"] == "collected"
    assert result["row_count"] == 1
    assert result["diagnostics"]["ibk_query_flow"]["query_submitted"] == "1"
    assert result["diagnostics"]["screen_reason_code"] == "TRANSACTION_TABLE_VISIBLE_AFTER_IBK_QUERY"


def test_collect_async_missing_explicit_session_recovers_same_work_key():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return "조회된 거래내역이 없습니다."
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()
    mock_session.session_id = "recovered-session-001"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.side_effect = [None]
        bridge_inst.ensure_work_session = AsyncMock(return_value=mock_session)
        bridge_inst._context_for_session = AsyncMock(return_value=mock_context)

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="stale-session",
                browser_work_key="yeoljeong-bank-shinhan-individual-abc123",
                date_from="2026-08-24",
                date_to="2026-08-24",
                auto_open_browser=True,
                portal_url="https://bank.shinhan.com/rib/easy/index.jsp",
                business_entity_type="individual",
            )
        )

    assert result["status"] == "collected"
    assert result["diagnostics"]["browser_session_id"] == "recovered-session-001"
    assert result["diagnostics"]["session_recovery"] == "recreated_from_missing_session"
    bridge_inst.ensure_work_session.assert_awaited_once()
    assert bridge_inst.ensure_work_session.await_args.kwargs["work_key"] == "yeoljeong-bank-shinhan-individual-abc123"


def test_collect_async_recovers_after_cdp_disconnect_during_context_open():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    class CDPError(RuntimeError):
        error_code = "CDP_NOT_READY"

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return "조회된 거래내역이 없습니다."
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    stale_session = MagicMock()
    stale_session.session_id = "stale-session"
    recovered_session = MagicMock()
    recovered_session.session_id = "recovered-session-cdp"

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge:
        bridge_inst = mock_bridge.return_value
        bridge_inst.sessions.get.side_effect = [stale_session, recovered_session]
        bridge_inst.sessions.find_by_work_key.return_value = None
        bridge_inst.ensure_work_session = AsyncMock(return_value=recovered_session)
        bridge_inst._context_for_session = AsyncMock(side_effect=[CDPError("cdp disconnected"), mock_context])

        result = _run(
            connector.collect_bank_via_browser_session_async(
                account,
                browser_session_id="stale-session",
                browser_work_key="yeoljeong-bank-browser-cdp",
                date_from="2026-08-01",
                date_to="2026-08-31",
                auto_open_browser=True,
            )
        )

    assert result["status"] == "collected"
    assert result["diagnostics"]["browser_session_id"] == "recovered-session-cdp"
    assert result["diagnostics"]["previous_browser_session_id"] == "stale-session"
    assert result["diagnostics"]["session_recovery"] == "recreated_after_runtime_error"
    assert result["diagnostics"]["session_recovery_error"] == "CDP_NOT_READY"
    bridge_inst.ensure_work_session.assert_awaited_once()
    assert bridge_inst.ensure_work_session.await_args.kwargs["force_recreate"] is True


def test_collect_async_diagnostics_has_no_credentials():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return ""
        if expr == "document.body ? document.body.innerHTML : ''":
            return _SHINHAN_SAMPLE_HTML
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
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


def test_collect_async_treats_bank_account_inputs_as_login_required():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/rib/easy/index.jsp#210101000001"
        if "querySelectorAll('table')" in expr:
            return [[["구분", "내용"], ["계좌조회", "간편조회서비스"]]]
        if "document.body.innerText" in expr:
            return "간편조회서비스 계좌번호 계좌비밀번호"
        if "querySelectorAll('input,button,select,a')" in expr:
            return [
                {"tag": "input", "type": "text", "id": "ibx_계좌번호"},
                {"tag": "input", "type": "password", "id": "계좌비밀번호", "label": "숫자 4자리"},
            ]
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()

    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_session = MagicMock()

    with patch("app.browser_bridge.service.get_browser_bridge_service") as mock_bridge, patch.object(
        connector,
        "_try_fill_bank_login",
        new_callable=AsyncMock,
    ) as mock_fill:
        mock_fill.return_value = {"attempted": "1", "account_no": "1", "account_secret": "1", "submitted": "0"}
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
                account_no="saved-account",
                account_password="saved-secret",
            )
        )

    assert result["status"] == "action_required"
    assert result["diagnostics"]["screen_state"] == "login_required"
    assert result["diagnostics"]["screen_reason_code"] == "BANK_LOGIN_INPUTS_VISIBLE"
    assert mock_fill.await_count == 1
    assert result["diagnostics"]["bank_login_auto_fill"]["attempted"] == "1"


def test_collect_async_date_filter_applied():
    account = {"id": "acct-1", "bank_name": "신한은행", "bank_code": "088", "institution_code": "shinhan_business"}

    async def evaluate(expr, *args):
        if expr == "window.location.href":
            return "https://bank.shinhan.com/"
        if "querySelectorAll('table')" in expr:
            return []
        if "document.body.innerText" in expr:
            return ""
        if expr == "document.body ? document.body.innerHTML : ''":
            return _SHINHAN_SAMPLE_HTML
        return []

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=evaluate)
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


def test_collect_bank_account_browser_forwards_auto_open_controls(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)
    captured = {}

    async def fake_collect(account_arg, **kwargs):
        captured["account"] = account_arg
        captured["kwargs"] = kwargs
        return {
            "status": "action_required",
            "error_code": "BANK_BROWSER_OPERATOR_ACTION_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": {"browser_work_key": kwargs["browser_work_key"]},
            "message": "기업페이지 승인 필요",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", lambda coro: asyncio.run(coro))

    with patch("app.services.yeoljeong_bank_browser_connector.collect_bank_via_browser_session_async", fake_collect):
        result = service.collect_bank_account_transactions(
            account["id"],
            {
                "business_id": "biz-mia",
                "branch_id": "branch-gangbuk-mia",
                "auto_open_browser": True,
                "browser_agent_id": "oby-ceo",
                "browser_preferred_port": "9333",
                "force_recreate_browser": True,
            },
            ADMIN_USER,
        )

    kwargs = captured["kwargs"]
    assert captured["account"]["id"] == account["id"]
    assert kwargs["auto_open_browser"] is True
    assert kwargs["browser_agent_id"] == "oby-ceo"
    assert kwargs["browser_preferred_port"] == 9333
    assert kwargs["force_recreate_browser"] is True
    assert result["collection"]["error_code"] == "BANK_BROWSER_OPERATOR_ACTION_REQUIRED"


def test_collect_bank_account_browser_forwards_saved_bank_quick_credentials(tmp_path, monkeypatch):
    account = _make_browser_bank_account(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: str(value).replace("encrypted:", "", 1))
    service.upsert_account(
        {
            "service": "shinhan_business",
            "username": "bank-user",
            "password": "bank-pass",
            "account_no": "110123456789",
            "account_password": "4321",
            "business_registration_no": "1234567890",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "collection_mode": "bank-quick-service",
            "login_url": "https://bank.shinhan.com/rib/easy/index.jsp",
        },
        ADMIN_USER,
    )
    captured = {}

    async def fake_collect(account_arg, **kwargs):
        captured["kwargs"] = kwargs
        return {
            "status": "action_required",
            "error_code": "PC_AGENT_LOGIN_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": {"browser_work_key": kwargs["browser_work_key"]},
            "message": "로그인 필요",
        }

    monkeypatch.setattr(service, "_run_bank_browser_async", lambda coro: asyncio.run(coro))

    with patch("app.services.yeoljeong_bank_browser_connector.collect_bank_via_browser_session_async", fake_collect):
        service.collect_bank_account_transactions(
            account["id"],
            {
                "business_id": "biz-mia",
                "branch_id": "branch-gangbuk-mia",
                "auto_open_browser": True,
            },
            ADMIN_USER,
        )

    kwargs = captured["kwargs"]
    assert kwargs["login_username"] == "bank-user"
    assert kwargs["login_password"] == "bank-pass"
    assert kwargs["account_no"] == "110123456789"
    assert kwargs["account_password"] == "4321"
    assert kwargs["business_registration_no"] == "1234567890"
    assert kwargs["business_entity_type"] == "individual"
    assert kwargs["portal_url"] == "https://bank.shinhan.com/rib/easy/index.jsp"
    assert kwargs["browser_work_key"].startswith("yeoljeong-bank-shinhan-individual-")
    assert account["id"] not in kwargs["browser_work_key"]


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
