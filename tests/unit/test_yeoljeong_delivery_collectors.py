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
