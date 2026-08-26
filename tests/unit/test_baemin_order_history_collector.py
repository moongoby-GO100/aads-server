import importlib.util
import asyncio
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/baemin_order_history_collector.py"
_SPEC = importlib.util.spec_from_file_location("baemin_order_history_collector", _MODULE_PATH)
collector = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(collector)

SCHEMA_VERSION = collector.SCHEMA_VERSION
parse_baemin_order_history_text = collector.parse_baemin_order_history_text
collect_baemin_order_history = collector.collect_baemin_order_history
BackfillLimits = collector.BackfillLimits


SAMPLE_ORDER_HISTORY_TEXT = """
배달완료
주문번호
T2FP00000XZV
주문일시
2026. 08. 24. (월) 오전 12:04:48
주문채널
배민배달(배민클럽)
가게번호
12583925
메뉴
(단골주문1위) 열정충천해장국2+숙주2+당면2
결제방식
바로결제
배달방식
한집배달
주문금액
28,000원

주문정보
(단골주문1위) 열정충천해장국2+숙주2+당면2 1개
총 결제금액
28,000원
즉시할인
1,900원
파트너부담 쿠폰할인
14,000원
주문 추가 정보 보기

정산정보
(A)주문중개 11,064원
주문금액 28,000원
중개이용료 -936원
(B)배달 -3,400원
배달비 -3,400원
(C)그외 -288원
결제정산수수료 -288원
(D)부가세 -463원
입금예정금액
6,913원
입금예정일
2026. 08. 26. (수)

주문 추가 정보
주결제방법
배민페이(카드결제)
보조결제방법
할인쿠폰
가게 요청사항
(수저포크 X)
배달 요청사항
문 앞에 두고 초인종 눌러주세요.
처리내역
배달(픽업)이 완료되었습니다.
접수시각
2026. 08. 24. (월) 오전 12:04:50
배달시각
2026. 08. 24. (월) 오전 12:17:13
"""


def test_parse_baemin_order_history_full_order_contract():
    result = parse_baemin_order_history_text(
        SAMPLE_ORDER_HISTORY_TEXT,
        "biz-mia",
        "열정국밥_미아점",
        collected_at="2026-08-24T19:30:00+09:00",
    )

    sales = result["records"]["sales"]
    settlements = result["records"]["settlements"]

    assert result["diagnostics"]["orders_seen"] == 1
    assert len(sales) == 1
    assert len(settlements) == 1

    order = sales[0]
    assert order["schema_version"] == SCHEMA_VERSION
    assert order["business_id"] == "biz-mia"
    assert order["branch"] == "열정국밥_미아점"
    assert order["order_no"] == "T2FP00000XZV"
    assert order["ordered_at"] == "2026-08-24T00:04:48+09:00"
    assert order["accepted_at"] == "2026-08-24T00:04:50+09:00"
    assert order["delivered_at"] == "2026-08-24T00:17:13+09:00"
    assert order["order_status"] == "배달완료"
    assert order["order_channel"] == "배민배달(배민클럽)"
    assert order["store_no"] == "12583925"
    assert order["payment_type"] == "바로결제"
    assert order["delivery_type"] == "한집배달"
    assert order["order_amount"] == 28000
    assert order["payment_total_amount"] == 28000
    assert order["instant_discount_amount"] == 1900
    assert order["partner_coupon_discount_amount"] == 14000
    assert order["items"][0]["quantity"] == 1
    assert order["settlement"]["expected_deposit_amount"] == 6913
    assert order["extra"]["primary_payment_method"] == "배민페이(카드결제)"
    assert order["extra"]["delivery_request"] == "문 앞에 두고 초인종 눌러주세요."

    settlement = settlements[0]
    assert settlement["order_no"] == "T2FP00000XZV"
    assert settlement["settlement_status"] == "ready"
    assert settlement["settlement_amount"] == 6913
    assert settlement["settlement"]["delivery_fee_amount"] == -3400


def test_parse_baemin_order_history_marks_pending_settlement():
    text = """
    배달완료
    T2F00001YXZA
    2026. 08. 24. (월) 오전 12:04:48
    주문금액 12,000원
    입금예정금액은 거래일자 다음날부터 확인할 수 있어요.
    """

    result = parse_baemin_order_history_text(text, "biz-mia", "열정국밥_미아점")

    assert result["diagnostics"]["settlement_pending"] == 1
    assert result["records"]["sales"][0]["settlement"]["status"] == "pending"
    assert result["records"]["settlements"][0]["settlement_status"] == "pending"


def test_parse_baemin_order_history_accepts_non_t_order_number():
    text = """
    배달완료
    B2FQ001QU5
    2026. 08. 25. (화) 오전 05:59:09
    가게배달
    12574388
    (고단백질) 살코기수육국밥정식
    만나서결제
    배달
    24,000원
    """

    result = parse_baemin_order_history_text(text, "biz-mia", "열정국밥_미아점")

    assert result["diagnostics"]["orders_seen"] == 1
    assert result["records"]["sales"][0]["order_no"] == "B2FQ001QU5"


def test_parse_baemin_order_history_idempotent_order_id():
    first = parse_baemin_order_history_text(SAMPLE_ORDER_HISTORY_TEXT, "biz-mia", "열정국밥_미아점")
    second = parse_baemin_order_history_text(SAMPLE_ORDER_HISTORY_TEXT, "biz-mia", "열정국밥_미아점")

    assert first["records"]["sales"][0]["id"] == second["records"]["sales"][0]["id"]
    assert first["records"]["settlements"][0]["id"] == second["records"]["settlements"][0]["id"]


def test_collect_baemin_order_history_returns_checkpoint_for_non_t_order_number():
    class FakePage:
        def __init__(self):
            self.url = ""
            self.row_text = """
            배달완료
            B2FQ001QU5
            2026. 08. 25. (화) 오전 05:59:09
            주문금액 24,000원
            입금예정금액 19,000원
            """

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def evaluate(self, expression, arg=None):
            if arg and "dateFrom" in arg:
                return False
            if "orderNoPattern" in expression:
                return [self.row_text]
            if "innerText" in expression:
                return self.row_text
            if "innerHTML" in expression:
                return f"<main>{self.row_text}</main>"
            if "return clicked" in expression:
                return 0
            return 0

    result = asyncio.run(
        collect_baemin_order_history(
            FakePage(),
            {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
            "2026-08-01",
            "2026-08-25",
            {
                "limits": BackfillLimits(
                    max_records=1,
                    max_runtime_seconds=1,
                    order_detail_jitter=(0, 0),
                    page_jitter=(0, 0),
                )
            },
        )
    )

    assert result["status"] == "succeeded"
    assert result["diagnostics"]["checkpoint_out"]["last_order_no"] == "B2FQ001QU5"
    assert result["records"]["sales"][0]["order_no"] == "B2FQ001QU5"


def test_collect_baemin_order_history_applies_checkpoint_order_no():
    class FakePage:
        def __init__(self):
            self.url = ""
            self.rows = [
                "배달완료\nT2FP00000AAA\n2026. 08. 25. (화) 오전 05:59:09\n주문금액 24,000원",
                "배달완료\nT2FP00000BBB\n2026. 08. 25. (화) 오전 05:40:09\n주문금액 18,000원",
                "배달완료\nT2FP00000CCC\n2026. 08. 25. (화) 오전 05:20:09\n주문금액 12,000원",
            ]

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def evaluate(self, expression, arg=None):
            if arg and "dateFrom" in arg:
                return False
            if "orderNoPattern" in expression:
                return self.rows
            if "innerText" in expression:
                return "\n\n".join(self.rows)
            if "return clicked" in expression:
                return 0
            return 0

    result = asyncio.run(
        collect_baemin_order_history(
            FakePage(),
            {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
            "2026-08-25",
            "2026-08-25",
            {
                "checkpoint": {"last_order_no": "T2FP00000AAA"},
                "limits": BackfillLimits(max_records=3, max_runtime_seconds=1, order_detail_jitter=(0, 0), page_jitter=(0, 0)),
            },
        )
    )

    assert [row["order_no"] for row in result["records"]["sales"]] == ["T2FP00000BBB", "T2FP00000CCC"]
    assert result["diagnostics"]["checkpoint_applied"] is True
