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
