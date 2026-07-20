from app.services import yeoljeong_delivery_collectors as collectors


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
