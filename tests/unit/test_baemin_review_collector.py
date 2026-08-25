import importlib.util
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/baemin_review_collector.py"
_SPEC = importlib.util.spec_from_file_location("baemin_review_collector", _MODULE_PATH)
collector = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(collector)


def test_parse_baemin_review_contract():
    text = """
    리뷰 ID: RV-100
    주문번호 T2FP00000XZV
    별점 5점
    작성일 2026.08.24
    메뉴: 열정충천해장국
    리뷰 내용: 국물이 진하고 맛있어요
    사장님 답글: 감사합니다
    사진 2장
    """

    result = collector.parse_baemin_review_text(text, "biz-mia", "열정국밥_미아점")
    review = result["records"]["reviews"][0]

    assert result["diagnostics"]["schema_version"] == "baemin_review.v1"
    assert review["order_no"] == "T2FP00000XZV"
    assert review["rating"] == 5
    assert review["review_text"] == "국물이 진하고 맛있어요"
    assert review["owner_reply_text"] == "감사합니다"
    assert review["reply_status"] == "답변완료"
    assert review["image_count"] == 2


def test_parse_baemin_review_idempotent_id():
    text = "리뷰 ID: RV-100\n별점 4점\n리뷰 내용: 좋아요"
    first = collector.parse_baemin_review_text(text, "biz-mia", "열정국밥_미아점")
    second = collector.parse_baemin_review_text(text, "biz-mia", "열정국밥_미아점")

    assert first["records"]["reviews"][0]["id"] == second["records"]["reviews"][0]["id"]
