import importlib.util
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/baemin_ads_collector.py"
_SPEC = importlib.util.spec_from_file_location("baemin_ads_collector", _MODULE_PATH)
collector = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(collector)


def test_parse_baemin_ads_contract():
    text = """
    캠페인: 우리가게클릭
    상태: 운영중
    기간: 2026.08.01 ~ 2026.08.24
    예산: 100,000원
    소진금액: 32,000원
    노출수: 12,000
    클릭수: 400
    주문수: 35
    주문금액: 560,000원
    CTR: 3.3%
    전환율: 8.8%
    ROAS: 1750.0%
    """

    result = collector.parse_baemin_ads_text(text, "biz-mia", "열정국밥_미아점")
    ad = result["records"]["ads"][0]

    assert result["diagnostics"]["schema_version"] == "baemin_ads.v1"
    assert ad["campaign_name"] == "우리가게클릭"
    assert ad["status"] == "운영중"
    assert ad["budget"] == 100000
    assert ad["spend"] == 32000
    assert ad["impressions"] == 12000
    assert ad["clicks"] == 400
    assert ad["orders"] == 35
    assert ad["order_amount"] == 560000
    assert ad["ctr"] == 3.3
    assert ad["conversion_rate"] == 8.8
    assert ad["roas"] == 1750.0


def test_parse_baemin_ads_idempotent_id():
    text = "캠페인: 우리가게클릭\n기간: 2026.08.01 ~ 2026.08.24\n소진금액: 1,000원"
    first = collector.parse_baemin_ads_text(text, "biz-mia", "열정국밥_미아점")
    second = collector.parse_baemin_ads_text(text, "biz-mia", "열정국밥_미아점")

    assert first["records"]["ads"][0]["id"] == second["records"]["ads"][0]["id"]


def test_parse_baemin_ads_product_name_without_campaign_label():
    text = """
    우리가게클릭
    상태 운영중
    기간: 2026.08.25
    소진금액 12,300원
    노출수 1,200
    클릭수 54
    주문수 7
    ROAS 430.0%
    """

    result = collector.parse_baemin_ads_text(text, "biz-mia", "열정국밥_미아점")
    ad = result["records"]["ads"][0]

    assert ad["campaign_name"] == "우리가게클릭"
    assert ad["spend"] == 12300
    assert ad["impressions"] == 1200
    assert ad["clicks"] == 54
    assert ad["orders"] == 7
    assert ad["roas"] == 430.0
