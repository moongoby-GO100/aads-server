from app.services import yeoljeong_delivery_collectors as collectors


class _FakeElement:
    def __init__(self, visible, click_error=False):
        self.visible = visible
        self.click_error = click_error
        self.clicked = False

    def is_visible(self, timeout):
        return self.visible

    def click(self, timeout, force=False):
        if self.click_error:
            raise RuntimeError("covered element")
        self.clicked = True


class _FakeMatches:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


def test_four_delivery_portals_are_configured():
    assert set(collectors.PORTAL_CONFIG) == {"baemin", "coupangeats", "yogiyo", "ddangyo"}
    assert all(config["login_url"].startswith("https://") for config in collectors.PORTAL_CONFIG.values())
    assert all("ads" in config["sections"] for config in collectors.PORTAL_CONFIG.values())


def test_normalize_record_is_deterministic_and_scoped():
    source = {
        "정산번호": "SET-1",
        "정산일": "2026.07.10",
        "매출액": "15,000원",
        "수수료": "1,000원",
        "부가세": "100원",
        "정산금액": "13,900원",
    }

    first = collectors.normalize_record("baemin", "settlements", source, "biz-mia", "열정국밥_미아점")
    second = collectors.normalize_record("baemin", "settlements", source, "biz-mia", "열정국밥_미아점")

    assert first["id"] == second["id"]
    assert first["business_id"] == "biz-mia"
    assert first["branch"] == "열정국밥_미아점"
    assert first["occurred_on"] == "2026-07-10"
    assert first["settlement_amount"] == 13900
    assert "password" not in first


def test_normalize_ddangyo_settlement_headers():
    record = collectors.normalize_record(
        "ddangyo",
        "settlements",
        {
            "입금상태": "입금완료",
            "입금(예정)일": "2026.07.16(목)",
            "입금(예정)금액": "62,543원",
            "정산유형": "일반정산",
        },
        "biz-mia",
        "열정국밥_미아점",
    )

    assert record["occurred_on"] == "2026-07-16"
    assert record["settlement_amount"] == 62543
    assert record["settlement_status"] == "입금완료"


def test_collect_account_requires_credential_without_opening_browser():
    result = collectors.collect_account(
        {
            "service": "baemin",
            "username": "test-user",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
        },
        "",
        "2026-07-01",
        "2026-07-20",
    )

    assert result == {
        "status": "credential_required",
        "error_code": "CREDENTIAL_REQUIRED",
        "records": {},
    }


def test_storage_state_path_prefers_existing_account_file(tmp_path):
    state_file = tmp_path / "baemin-state.json"
    state_file.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    assert collectors._storage_state_path({"storage_state_path": str(state_file)}) == str(state_file)
    assert collectors._storage_state_path({"storage_state_path": str(tmp_path / "missing.json")}) == ""


def test_click_first_skips_hidden_duplicate_and_clicks_visible_match():
    hidden = _FakeElement(False)
    covered = _FakeElement(True, click_error=True)
    visible = _FakeElement(True)

    class FakePage:
        def get_by_text(self, pattern, exact):
            return _FakeMatches([hidden, covered, visible])

        def wait_for_timeout(self, timeout):
            return None

    assert collectors._click_first(FakePage(), ("정산내역",)) is True
    assert hidden.clicked is False
    assert visible.clicked is True


def test_page_state_rejects_logged_out_landing_page_without_password_input():
    class FakeLocator:
        def inner_text(self, timeout):
            return "요기요 사장님 반갑습니다. 로그인해주세요 :) 사장님 로그인"

        def count(self):
            return 0

    class FakePage:
        url = "https://ceo.yogiyo.co.kr/"

        def locator(self, selector):
            return FakeLocator()

    assert collectors._page_state(FakePage()) == ("failed", "PORTAL_LOGIN_NOT_COMPLETED")


def test_page_state_marks_ddangyo_numeric_captcha_as_action_required():
    class FakeLocator:
        def inner_text(self, timeout):
            return "자동입력방지 숫자를 입력해 주세요"

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    assert collectors._page_state(FakePage(), "ddangyo") == (
        "portal_action_required",
        "DDANGYO_NUMERIC_CAPTCHA_REQUIRED",
    )


def test_fill_login_uses_dom_fallback_for_websquare_portal():
    class EmptyLocator:
        @property
        def first(self):
            return self

        def count(self):
            return 0

    class FakePage:
        def __init__(self):
            self.evaluate_arg = None
            self.timeout_ms = 0

        def wait_for_load_state(self, *args, **kwargs):
            return None

        def wait_for_selector(self, *args, **kwargs):
            return None

        def locator(self, selector):
            return EmptyLocator()

        def evaluate(self, expression, arg=None):
            self.evaluate_arg = arg
            return {"filled": True, "clicked": True, "reason": ""}

        def wait_for_timeout(self, timeout):
            self.timeout_ms = timeout

    page = FakePage()

    assert collectors._fill_login(page, "owner", "secret", "ddangyo") is True
    assert page.evaluate_arg["username"] == "owner"
    assert page.evaluate_arg["password"] == "secret"
    assert "#mf_btn_webLogin" in page.evaluate_arg["submitSelectors"]
    assert page.timeout_ms == 5000


def test_security_block_result_detects_baemin_block_page():
    class FakeBody:
        def inner_text(self, timeout):
            return "죄송합니다. 올바르지 않은 요청으로 페이지를 보실 수 없습니다. 보안 위배 접근 제한 페이지"

    class FakePage:
        def locator(self, selector):
            return FakeBody()

    class FakeResponse:
        status = 403

    result = collectors._security_block_result(FakePage(), FakeResponse())

    assert result["status"] == "portal_action_required"
    assert result["error_code"] == "BAEMIN_SECURITY_BLOCKED"
    assert "records" in result


def test_security_block_result_detects_baemin_abnormal_activity_page():
    class FakeBody:
        def inner_text(self, timeout):
            return "잠시 이용이 제한돼요 비정상 동작이 감지되어 잠시 이용이 제한돼요 잠시 후 다시 시도해 주세요."

    class FakePage:
        def locator(self, selector):
            return FakeBody()

    result = collectors._security_block_result(FakePage(), None)

    assert result["status"] == "portal_action_required"
    assert result["error_code"] == "BAEMIN_SECURITY_BLOCKED"


def test_parse_baemin_pc_html_table_settlements():
    html = """
    <html><body>
      <table>
        <thead><tr><th>정산일</th><th>매출액</th><th>수수료</th><th>부가세</th><th>정산금액</th><th>상태</th></tr></thead>
        <tbody><tr><td>2026.08.01</td><td>80,000원</td><td>7,000원</td><td>700원</td><td>72,300원</td><td>입금예정</td></tr></tbody>
      </table>
    </body></html>
    """

    result = collectors.parse_portal_export("baemin", "settlements", html, "biz-junghwa", "중화점")

    assert result["status"] == "succeeded"
    record = result["records"]["settlements"][0]
    assert record["business_id"] == "biz-junghwa"
    assert record["branch"] == "중화점"
    assert record["occurred_on"] == "2026-08-01"
    assert record["settlement_amount"] == 72300


def test_normalize_record_tolerates_none_header_key():
    record = collectors.normalize_record(
        "baemin",
        "sales",
        {None: "extra", "주문일": "2026.08.19", "주문금액": "17,000원"},
        "biz-junghwa",
        "중화점",
    )

    assert record["gross_amount"] == 17000
    assert record["occurred_on"] == "2026-08-19"


def test_parse_baemin_pc_copied_review_table():
    copied = "작성일\t평점\t리뷰내용\t답글상태\n2026-08-02\t5\t냉면이 맛있어요\t미답변\n"

    result = collectors.parse_portal_export("baemin", "reviews", copied, "biz-junghwa", "중화점")

    assert result["status"] == "succeeded"
    review = result["records"]["reviews"][0]
    assert review["rating"] == 5
    assert review["review_text"] == "냉면이 맛있어요"
    assert review["reply_status"] == "미답변"


def test_parse_baemin_pc_copied_ad_table():
    copied = "일자\t캠페인명\t광고비\t노출수\t클릭수\t주문수\t광고매출\n2026-08-02\t우리가게클릭\t3,000원\t120\t8\t2\t21,000원\n"

    result = collectors.parse_portal_export("baemin", "ads", copied, "biz-junghwa", "중화점")

    assert result["status"] == "succeeded"
    ad = result["records"]["ads"][0]
    assert ad["record_type"] == "ads"
    assert ad["campaign_name"] == "우리가게클릭"
    assert ad["cost_amount"] == 3000
    assert ad["impressions"] == 120
    assert ad["clicks"] == 8
    assert ad["orders"] == 2
    assert ad["sales_amount"] == 21000


def _candidate_account(monkeypatch):
    import hashlib
    account = {"service": "coupangeats", "business_id": "biz-mia",
               "username": "sample-user", "branch": "sample-store"}
    monkeypatch.setattr(collectors, "COUPANGEATS_SALES_SCHEMA", {
        **collectors.COUPANGEATS_SALES_SCHEMA,
        "username_sha256": hashlib.sha256(account["username"].encode()).hexdigest(),
        "branch_sha256": hashlib.sha256(account["branch"].encode()).hexdigest(),
    })
    return account

def test_coupangeats_candidate_reads_screen_totals_and_deduplicates(tmp_path, monkeypatch):
    import json
    import os
    from pathlib import Path

    account = _candidate_account(monkeypatch)
    row = {"주문번호": "ORDER-1", "주문일": "2026-09-01", "주문금액": "12,000원",
           "할인": "1,000원", "배달팁": "2,000원", "주문자명": "private customer"}
    monkeypatch.setattr(collectors, "_page_state", lambda page, service, timeout=5000: ("authenticated", ""))
    monkeypatch.setattr(collectors, "_scrape_table", lambda page, timeout=None: [row, row])
    import sys
    llm_calls = []
    def count_llm_calls(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "call_llm_with_fallback":
            llm_calls.append(1)

    class Control:
        def __init__(self, value=""):
            self.value = value
        def count(self):
            return 1
        @property
        def first(self):
            return self
        def nth(self, index):
            return self
        def is_visible(self, timeout):
            return True
        def click(self, timeout):
            pass
        def fill(self, value, timeout=None):
            pass
        def inner_text(self, timeout):
            return self.value

    class Page:
        def get_by_role(self, role, name, exact):
            return Control()
        def get_by_text(self, label, exact):
            class Empty:
                @property
                def first(self):
                    return self
                def count(self):
                    return 0
                def is_visible(self, timeout):
                    return False
            return Empty()
        def locator(self, selector):
            if selector == "input[type='date']":
                class Dates(Control):
                    def count(self):
                        return 2
                return Dates()
            if selector == "[data-testid='sales-summary']":
                return Control("주문건수 1건\n총매출 12,000원\n할인액 1,000원\n배달비 2,000원")
            return Control()
        def set_default_timeout(self, timeout):
            pass
        def set_default_navigation_timeout(self, timeout):
            pass

    root = tmp_path / "private"
    import threading
    results = []
    errors = []
    def collect_in_worker():
        sys.setprofile(count_llm_calls)
        try:
            results.append(collectors.coupangeats_sales_candidate(Page(), account, "2026-09-01", "2026-09-01", root))
            results.append(collectors.coupangeats_sales_candidate(Page(), account, "2026-09-01", "2026-09-01", root))
        except Exception as exc:
            errors.append(exc)
        finally:
            sys.setprofile(None)
    worker = threading.Thread(target=collect_in_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors
    first, second = results
    assert first["path"] == second["path"]
    assert first["llm_calls"] == 0 and not llm_calls
    saved = json.loads(Path(first["path"]).read_text())
    assert saved["aggregate"]["count"] == 1
    assert saved["aggregate"]["totals"] == {"gross_amount": 12000, "discount_amount": 1000, "delivery_fee": 2000}
    assert "private customer" not in json.dumps(saved)
    assert "ORDER-1" not in json.dumps(saved)
    assert os.stat(first["path"]).st_mode & 0o077 == 0


def test_coupangeats_rejects_bad_amounts_and_screen_totals(tmp_path):
    import pytest
    row = {"주문번호": "ORDER-1", "주문일": "2026-09-01", "주문금액": "12,000원",
           "할인": "broken", "배달팁": "2,000원"}
    with pytest.raises(ValueError, match="SALES_AMOUNT_INVALID"):
        collectors.coupangeats_aggregate_sales([row], "2026-09-01", "2026-09-01")
    row["할인"] = "1,000원"
    aggregate = collectors.coupangeats_aggregate_sales([row], "2026-09-01", "2026-09-01")
    class Page:
        def locator(self, selector):
            class Widget:
                def count(self):
                    return 1
                @property
                def first(self):
                    return self
                def inner_text(self, timeout):
                    return "주문건수 1건\n총매출 12,000원\n할인액 999원\n배달비 2,000원"
            return Widget()
    with pytest.raises(ValueError, match="SALES_SCREEN_TOTAL_MISMATCH"):
        import time
        collectors.coupangeats_verify_sales(Page(), aggregate, time.monotonic() + 60)


def test_coupangeats_lock_symlink_is_rejected(tmp_path, monkeypatch):
    import pytest

    account = _candidate_account(monkeypatch)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (root / ".sales.lock").symlink_to(victim)
    with pytest.raises(ValueError, match="SALES_STORAGE_LOCK_UNSAFE"):
        collectors.coupangeats_sales_candidate(object(), account, "2026-09-01", "2026-09-01", root)
    assert victim.read_text() == "untouched"


def test_coupangeats_account_shape_passes_scope_without_vault_account(monkeypatch):
    import pytest

    account = {**_candidate_account(monkeypatch), "password_source": "agent_vault",
               "agent_vault_origin": "https://store.coupangeats.com"}
    collectors._coupangeats_scope(account, "2026-09-01", "2026-09-02")
    with pytest.raises(ValueError, match="COUPANGEATS_ACCOUNT_SCOPE_MISMATCH"):
        collectors._coupangeats_scope({**account, "username": "other"}, "2026-09-01", "2026-09-02")
    with pytest.raises(ValueError, match="COUPANGEATS_ACCOUNT_SCOPE_MISMATCH"):
        collectors._coupangeats_scope({**account, "branch": "other"}, "2026-09-01", "2026-09-02")


def test_coupangeats_candidate_does_not_replace_existing_records(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    class Context:
        def new_page(self):
            return SimpleNamespace(goto=lambda *a, **k: None)
        def close(self):
            pass
    class Browser:
        def new_context(self, **kwargs):
            return Context()
        def close(self):
            pass
    class Playwright:
        chromium = SimpleNamespace(launch=lambda **kwargs: Browser())
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: Playwright()))
    monkeypatch.setattr(collectors, "_storage_state_path", lambda account: "session.json")
    monkeypatch.setattr(collectors, "_page_state", lambda page, service: ("authenticated", ""))
    monkeypatch.setattr(collectors, "_security_block_result", lambda page, response: None)
    monkeypatch.setattr(collectors, "_dismiss_optional_prompts", lambda page, config: None)
    monkeypatch.setattr(collectors, "_save_session_state", lambda context, service, account: None)
    monkeypatch.setattr(collectors, "_collect_section", lambda *args: ([{"주문번호": "one", "주문일": "2026-09-01", "주문금액": "12000"}], "table"))
    monkeypatch.setattr(collectors, "normalize_record", lambda service, kind, row, business, branch: {"kind": kind})
    monkeypatch.setattr(collectors, "coupangeats_sales_candidate", lambda *args: (_ for _ in ()).throw(PermissionError("private root")))
    monkeypatch.setenv("YEOLJEONG_COUPANGEATS_SALES_PRIVATE_ROOT", str(tmp_path))
    account = _candidate_account(monkeypatch)
    result = collectors.collect_account(account, "", "2026-09-01", "2026-09-01")
    assert result["status"] == "succeeded"
    assert set(result["records"]) == {"sales", "settlements", "reviews", "ads"}
    assert all(result["records"][kind] for kind in result["records"])
    assert result["diagnostics"]["sales_candidate_error"] == "SALES_CANDIDATE_PERMISSIONERROR"


def test_coupangeats_timeout_in_non_main_thread(tmp_path, monkeypatch):
    import threading
    import time
    monkeypatch.setattr(collectors, "COUPANGEATS_SALES_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(collectors, "coupangeats_enter_sales", lambda page, deadline: time.sleep(0.03))
    account = _candidate_account(monkeypatch)
    class Page:
        def set_default_timeout(self, timeout):
            pass
        def set_default_navigation_timeout(self, timeout):
            pass
    errors = []
    def collect_in_worker():
        try:
            collectors.coupangeats_sales_candidate(Page(), account, "2026-09-01", "2026-09-01", tmp_path / "private")
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=collect_in_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert str(errors[0]) == "SALES_COLLECTION_TIMEOUT"


def test_coupangeats_aggregate_optional_zero_and_negative_summary():
    import time

    row = {"주문번호": "sample-order", "주문일": "2026-09-01", "주문금액": "-1,200원"}
    aggregate = collectors.coupangeats_aggregate_sales([row], "2026-09-01", "2026-09-01")
    assert aggregate["totals"] == {"gross_amount": -1200, "discount_amount": 0, "delivery_fee": 0}

    class Summary:
        def count(self):
            return 1
        @property
        def first(self):
            return self
        def inner_text(self, timeout):
            return "주문건수 1건\n총매출 -1,200원\n할인액 0원\n배달비 0원"
    class Page:
        def locator(self, selector):
            return Summary()
    collectors.coupangeats_verify_sales(Page(), aggregate, time.monotonic() + 1)


def test_coupangeats_download_uses_original_browser_artifact(tmp_path, monkeypatch):
    import time

    import pytest

    monkeypatch.setattr(collectors, "_page_state", lambda *a, **k: ("authenticated", ""))
    class Download:
        suggested_filename = "sales.csv"
        deleted = False
        delay = 0
        @property
        def _impl_obj(self):
            return self
        def _sync(self, coroutine):
            import asyncio
            return asyncio.run(coroutine)
        async def save_as(self, target):
            import asyncio
            from pathlib import Path
            await asyncio.sleep(self.delay)
            Path(target).write_text("주문일,주문금액\n2026-09-01,1200\n")
        async def delete(self):
            self.deleted = True
    download = Download()
    class Button:
        def count(self):
            return 1
        def nth(self, index):
            return self
        @property
        def first(self):
            return self
        def is_visible(self, timeout):
            return True
        def click(self, timeout):
            pass
    class Event:
        value = download
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Page:
        def get_by_role(self, role, name, exact):
            return Button()
        def get_by_text(self, label, exact):
            return Button()
        def expect_download(self, timeout):
            return Event()
    rows, source = collectors.coupangeats_query_or_download(Page(), tmp_path, time.monotonic() + 1)
    assert source == "download" and len(rows) == 1
    assert download.deleted
    assert not list(tmp_path.iterdir())
    download.delay = 0.05
    with pytest.raises(TimeoutError):
        collectors.coupangeats_query_or_download(Page(), tmp_path, time.monotonic() + 0.02)
    assert not list(tmp_path.iterdir())
