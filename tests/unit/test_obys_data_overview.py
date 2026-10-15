"""오비서 '오늘 한눈에'·배달앱 현황·검토함 메모/기간 계약: 집계 규칙, 권한 게이트, 화면 배선 정적 검사."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import obys_collections as api
from app.auth import get_current_user
from app.services import obys_collection_service as svc
from app.services import obys_data_overview_service as ov

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
TODAY = date(2026, 10, 14)
ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "modules" / "data-overview.js").read_text(encoding="utf-8")
CSS = (ROOT / "modules" / "data-overview.css").read_text(encoding="utf-8")
CLOBE_JS = (ROOT / "modules" / "clobe-collection.js").read_text(encoding="utf-8")
SERVICE_SRC = (Path(__file__).resolve().parents[2] / "app" / "services" / "obys_data_overview_service.py").read_text(encoding="utf-8")


class FakeConn:
    """SQL 문구로 어떤 조회인지 가려 미리 정한 행을 돌려준다."""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.queries: list[tuple[str, tuple]] = []
        self.closed = False

    def transaction(self, **_):
        @asynccontextmanager
        async def ctx():
            yield

        return ctx()

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        if "FROM yeoljeong_businesses" in sql:
            return self.tables.get("businesses", [])
        if "UNION ALL" in sql:
            return self.tables.get("fresh", [])
        if "FROM yeoljeong_delivery_sales" in sql:
            return self.tables.get("sales", [])
        if "FROM yeoljeong_delivery_settlements" in sql:
            return self.tables.get("settlements", [])
        if "FROM yeoljeong_delivery_reviews" in sql:
            return self.tables.get("reviews", [])
        if "FROM yeoljeong_delivery_ads" in sql:
            return self.tables.get("ads", [])
        if "FROM yeoljeong_delivery_collection_status" in sql:
            return self.tables.get("collection", [])
        if "WITH latest" in sql:
            return self.tables.get("channel", [])
        if "stage IN ('review_box'" in sql:
            return self.tables.get("stages", [])
        if "FROM obys_clobe_item" in sql:
            return self.tables.get("items", [])
        raise AssertionError(f"unexpected query: {sql[:80]}")

    async def close(self):
        self.closed = True


def _install(monkeypatch, tables):
    conn = FakeConn(tables)

    async def connect():
        return conn

    monkeypatch.setattr(ov, "_connect", connect)
    monkeypatch.setattr(ov, "_today", lambda: TODAY)
    return conn


BIZ = [{"id": "biz-1", "name": "단하루"}]
DELIVERY_TABLES = {
    "businesses": BIZ,
    "sales": [
        {"business_id": "biz-1", "branch": "본점", "service": "baemin", "day": "2026-10-13", "orders": 10, "gross": Decimal("300000"), "discount": Decimal(0)},
        {"business_id": "biz-1", "branch": "본점", "service": "baemin", "day": "2026-10-12", "orders": 5, "gross": Decimal("100000"), "discount": Decimal(0)},
        {"business_id": "biz-1", "branch": "본점", "service": "coupangeats", "day": "2026-10-01", "orders": 4, "gross": Decimal("80000"), "discount": Decimal(0)},
    ],
    "settlements": [
        {"business_id": "biz-1", "branch": "본점", "service": "baemin", "status": "입금완료", "n": 3, "sales": Decimal("200000"), "fee": Decimal("20000"), "payout": Decimal("170000")},
        {"business_id": "biz-1", "branch": "본점", "service": "baemin", "status": "입금예정", "n": 2, "sales": Decimal("100000"), "fee": Decimal("10000"), "payout": Decimal("85000")},
        {"business_id": "biz-1", "branch": "본점", "service": "coupangeats", "status": "", "n": 1, "sales": Decimal("80000"), "fee": Decimal("8000"), "payout": Decimal("70000")},
    ],
    "reviews": [{"business_id": "biz-1", "service": "baemin", "n": 20, "rating": Decimal("4.5"), "waiting": 3}],
    "ads": [{"business_id": "biz-1", "service": "baemin", "n": 2, "cost": Decimal("15000"), "orders": Decimal(7), "sales": Decimal("90000")}],
    "fresh": [
        {"kind": "sales", "business_id": "biz-1", "service": "baemin", "n": 15, "last_day": "2026-10-13", "last_collected": "2026-10-14T03:00:00+09:00"},
        {"kind": "sales", "business_id": "biz-1", "service": "coupangeats", "n": 4, "last_day": "2026-10-01", "last_collected": "2026-10-02T03:00:00+09:00"},
    ],
    "collection": [
        {"business_id": "biz-1", "branch": "본점", "service": "coupangeats", "status": "action_required", "error_code": "LOGIN_BLOCKED", "at": "2026-10-02T03:00:00+09:00"},
        {"business_id": "biz-1", "branch": "본점", "service": "baemin", "status": "succeeded", "error_code": "", "at": "2026-10-14T03:00:00+09:00"},
    ],
}


# ── 순수 규칙 ─────────────────────────────────────────────

def test_service_label_uses_korean_names_and_never_leaks_unknown_ids():
    assert ov.service_label("baemin") == "배달의민족"
    assert ov.service_label("coupangeats") == "쿠팡이츠"
    assert ov.service_label("my_internal_key") == "기타 배달앱"
    assert ov.service_label("자체앱") == "자체앱"


@pytest.mark.parametrize("status,bucket", [
    ("입금완료", "paid"), ("입금예정", "scheduled"), ("정산대기", "scheduled"), ("미입금", "scheduled"),
    ("paid", "paid"), ("pending", "scheduled"), ("", "unknown"), (None, "unknown"), ("보류", "unknown")])
def test_settlement_bucket(status, bucket):
    assert ov.settlement_bucket(status) == bucket


def test_resolve_period_defaults_to_30_days_and_rejects_bad_ranges(monkeypatch):
    monkeypatch.setattr(ov, "_today", lambda: TODAY)
    assert ov.resolve_period(None, None) == (date(2026, 9, 15), TODAY)
    with pytest.raises(svc.CollectionError) as inverted:
        ov.resolve_period(date(2026, 10, 5), date(2026, 10, 1))
    assert (inverted.value.code, inverted.value.status) == ("invalid_period", 400)
    with pytest.raises(svc.CollectionError):
        ov.resolve_period(date(2024, 1, 1), date(2026, 1, 1))


def test_sql_never_casts_raw_payload_text_to_numeric_or_date():
    """깨진 값 한 행이 전체 조회를 죽이지 않게 모든 수치·일자는 정규식 가드를 거친다."""
    assert "::numeric" in SERVICE_SRC
    for match in re.finditer(r"::numeric", SERVICE_SRC):
        window = SERVICE_SRC[max(0, match.start() - 160):match.start()]
        assert "THEN" in window and "~" in window, window
    assert "::date" not in SERVICE_SRC.replace("$6::date", "").replace("$7::date", "")


# ── 배달앱 현황 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delivery_overview_rolls_up_by_app_branch_and_day(monkeypatch):
    conn = _install(monkeypatch, DELIVERY_TABLES)
    out = await ov.delivery_overview(TENANT, "biz-1")
    assert conn.closed
    assert out["period"] == {"date_from": "2026-09-15", "date_to": "2026-10-14"}
    assert out["totals"] == {"gross": "480000", "orders": 19, "fee": "38000", "payout": "325000"}
    by = {s["service"]: s for s in out["services"]}
    baemin, coupang = by["baemin"], by["coupangeats"]
    assert baemin["label"] == "배달의민족" and baemin["sales"] == {"gross": "400000", "orders": 15, "last_date": "2026-10-13"}
    assert baemin["settlements"]["paid"] == "170000" and baemin["settlements"]["scheduled"] == "85000"
    assert baemin["settlements"]["fee_rate"] == 10.0
    assert baemin["reviews"] == {"count": 20, "waiting": 3, "rating": 4.5}
    assert baemin["ads"]["cost"] == "15000"
    assert baemin["stale"] is False and baemin["csv_upload_available"] is False
    assert coupang["settlements"]["unknown"] == "70000"
    assert [(b["branch"], b["service"], b["gross"]) for b in out["branches"]] == [
        ("본점", "baemin", "400000"), ("본점", "coupangeats", "80000")]
    assert out["trend"][0] == {"date": "2026-10-01", "by_service": {"coupangeats": "80000"}}
    assert out["trend"][-1]["date"] == "2026-10-13"


@pytest.mark.asyncio
async def test_stale_service_reports_last_date_next_action_and_no_internal_message(monkeypatch):
    _install(monkeypatch, DELIVERY_TABLES)
    out = await ov.delivery_overview(TENANT, "biz-1")
    coupang = next(s for s in out["services"] if s["service"] == "coupangeats")
    assert coupang["stale"] is True and coupang["stale_days"] == 13
    assert coupang["last_data_date"] == "2026-10-01"
    assert coupang["collection_failed"] is True and coupang["collection_error_code"] == "LOGIN_BLOCKED"
    assert coupang["csv_upload_available"] is True
    assert "collection_message" not in coupang


@pytest.mark.asyncio
async def test_delivery_overview_empty_tenant_data_is_explicit(monkeypatch):
    _install(monkeypatch, {"businesses": BIZ})
    out = await ov.delivery_overview(TENANT, "biz-1")
    assert out["has_data"] is False and out["services"] == [] and out["totals"]["gross"] == "0"


@pytest.mark.asyncio
async def test_business_scope_is_enforced(monkeypatch):
    _install(monkeypatch, {"businesses": []})
    with pytest.raises(svc.CollectionError) as missing:
        await ov.delivery_overview(TENANT, "biz-of-someone-else")
    assert (missing.value.code, missing.value.status) == ("business_not_found", 404)
    with pytest.raises(svc.CollectionError) as outside:
        await ov.delivery_overview("11111111-1111-1111-1111-111111111111", "biz-1")
    assert outside.value.status == 403
    with pytest.raises(svc.CollectionError) as bad:
        await ov.delivery_overview("not-a-uuid", "biz-1")
    assert bad.value.status == 403


@pytest.mark.asyncio
async def test_queries_are_tenant_scoped_and_parameterized(monkeypatch):
    conn = _install(monkeypatch, DELIVERY_TABLES)
    await ov.delivery_overview(TENANT, "biz-1")
    biz_sql, biz_args = conn.queries[0]
    assert "tenant_id=$1" in biz_sql and biz_args[0].hex == TENANT.replace("-", "")
    for sql, _ in conn.queries[1:]:
        assert "business_id = ANY($1::text[])" in sql and "deleted_at IS NULL" in sql
        assert "biz-1" not in sql


# ── 오늘 한눈에 ────────────────────────────────────────────

TODAY_TABLES = {
    **DELIVERY_TABLES,
    "items": [
        {"data_kind": "cash_receipt", "direction": "SALES", "n": 40, "amount": Decimal("1200000")},
        {"data_kind": "cash_receipt", "direction": "PURCHASE", "n": 9, "amount": Decimal("300000")},
        {"data_kind": "card_approval", "direction": "OUT", "n": 12, "amount": Decimal("500000")},
    ],
    "channel": [
        {"channel": "CARD", "date_basis": "SETTLEMENT", "n": 8, "gross": Decimal("7000000")},
        {"channel": "CARD", "date_basis": "SALE", "n": 8, "gross": Decimal("7200000")},
    ],
    "stages": [{"stage": "review_box", "n": 120}, {"stage": "reviewed", "n": 30}],
}


@pytest.mark.asyncio
async def test_today_overview_keeps_cash_card_and_delivery_separate(monkeypatch):
    _install(monkeypatch, TODAY_TABLES)
    out = await ov.today_overview(TENANT, None)
    assert out["delivery"]["gross"] == "480000" and out["delivery"]["orders"] == 19
    assert [s["service"] for s in out["delivery"]["services"]] == ["baemin", "coupangeats"]
    assert out["delivery"]["services"][1]["stale"] is True
    assert out["cash_receipt_sales"] == {"amount": "1200000", "count": 40}
    assert out["card_spend"] == {"amount": "500000", "count": 12}
    assert out["card_sales"]["gross"] == "7200000" and out["card_sales"]["date_basis"] == "SALE"
    assert out["settlement"]["paid"] == {"amount": "170000", "count": 3}
    assert out["settlement"]["scheduled"] == {"amount": "85000", "count": 2}
    assert out["review_waiting"] == {"review_box": 120, "reviewed": 30, "total": 150}
    assert out["period"]["settlement_to"] == "2026-10-28"
    assert not any(key in out for key in ("total_sales", "combined"))


@pytest.mark.asyncio
async def test_today_overview_without_channel_data_reports_no_card_sales(monkeypatch):
    _install(monkeypatch, {"businesses": BIZ})
    out = await ov.today_overview(TENANT, "biz-1")
    assert out["card_sales"] is None and out["channel_revenue"] == []
    assert out["review_waiting"]["total"] == 0


@pytest.mark.asyncio
async def test_today_overview_only_counts_live_clobe_stages(monkeypatch):
    conn = _install(monkeypatch, TODAY_TABLES)
    await ov.today_overview(TENANT, None)
    item_sql, item_args = next((s, a) for s, a in conn.queries if "stage = ANY($4" in s)
    assert ["review_box", "reviewed", "confirmed"] in item_args
    latest_sql, _ = next((s, a) for s, a in conn.queries if "WITH latest" in s)
    assert "stage <> 'superseded'" in latest_sql


# ── API ───────────────────────────────────────────────────

def _client(role="viewer", status="active"):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    user = {"tenant_id": TENANT, "email": "u@example.com", "is_internal_admin": False,
            "current_membership": {"tenant_id": TENANT, "role": role, "status": status}}
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_overview_endpoints_pass_tenant_and_period(monkeypatch):
    seen = {}

    async def fake_today(tenant, business, **kw):
        seen["today"] = (tenant, business, kw)
        return {"ok": "today"}

    async def fake_delivery(tenant, business, **kw):
        seen["delivery"] = (tenant, business, kw)
        return {"ok": "delivery"}

    monkeypatch.setattr(api.overview, "today_overview", fake_today)
    monkeypatch.setattr(api.overview, "delivery_overview", fake_delivery)
    client = _client("viewer")
    assert client.get("/api/v1/obys-collections/today-overview?business_id=biz-1").json() == {"ok": "today"}
    res = client.get("/api/v1/obys-collections/businesses/biz-1/delivery-overview?date_from=2026-10-01&date_to=2026-10-14")
    assert res.json() == {"ok": "delivery"}
    assert seen["today"][:2] == (TENANT, "biz-1")
    assert seen["delivery"] == (TENANT, "biz-1", {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 14)})


@pytest.mark.parametrize("path", ["/api/v1/obys-collections/today-overview", "/api/v1/obys-collections/businesses/biz-1/delivery-overview"])
@pytest.mark.parametrize("role,status", [("guest", "active"), ("owner", "suspended"), ("employee", "active")])
def test_overview_endpoints_reject_non_members(path, role, status):
    res = _client(role, status).get(path)
    assert res.status_code == 403 and res.json()["detail"] == "role_not_allowed"


def test_overview_endpoints_map_collection_errors(monkeypatch):
    async def boom(*_a, **_k):
        raise svc.CollectionError("business_not_found", 404)

    monkeypatch.setattr(api.overview, "delivery_overview", boom)
    res = _client("owner").get("/api/v1/obys-collections/businesses/nope/delivery-overview")
    assert res.status_code == 404 and res.json()["detail"] == "business_not_found"


def test_invalid_date_is_rejected_before_the_database():
    res = _client("owner").get("/api/v1/obys-collections/today-overview?date_from=not-a-date")
    assert res.status_code == 422


# ── 검토함: 적요·기간 ──────────────────────────────────────

class ItemsConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 2 if sql.startswith("SELECT count") else True

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        base = {"item_id": "11111111-1111-1111-1111-111111111111", "data_kind": "bank_transaction", "source_key": "k",
                "institution": "국민은행", "occurred_on": date(2026, 10, 1), "amount": Decimal("5000"), "direction": "OUT",
                "stage": "review_box", "source_as_of": None, "first_seen_at": None, "supersedes_item_id": None}
        return [{**base, "counterparty": None, "memo": "10월 월세"}, {**base, "counterparty": "한전", "memo": None}]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_list_items_returns_memo_and_filters_period_server_side(monkeypatch):
    conn = ItemsConn()

    async def connect():
        return conn

    monkeypatch.setattr(svc, "_connect", connect)
    out = await svc.list_items(TENANT, "biz-1", stage="review_box", date_from="2026-10-01", date_to=date(2026, 10, 31))
    assert [i["memo"] for i in out["items"]] == ["10월 월세", None]
    assert out["items"][0]["counterparty"] is None
    assert out["total"] == 2
    list_sql, list_args = next((s, a) for s, a in conn.calls if "ORDER BY occurred_on" in s)
    assert "transactionDescription" in list_sql and "occurred_on >= $6" in list_sql and "occurred_on <= $7" in list_sql
    assert date(2026, 10, 1) in list_args and date(2026, 10, 31) in list_args
    count_sql, count_args = next((s, a) for s, a in conn.calls if s.startswith("SELECT count"))
    assert "occurred_on >= $6" in count_sql and date(2026, 10, 1) in count_args


@pytest.mark.asyncio
async def test_list_items_rejects_inverted_period(monkeypatch):
    with pytest.raises(svc.CollectionError) as err:
        await svc.list_items(TENANT, "biz-1", date_from="2026-10-31", date_to="2026-10-01")
    assert err.value.code == "invalid_period"


def test_items_endpoint_accepts_period_filters(monkeypatch):
    seen = {}

    async def fake(tenant, business, **kw):
        seen.update(kw)
        return {"total": 0, "items": []}

    monkeypatch.setattr(svc, "list_items", fake)
    res = _client("member").get("/api/v1/obys-collections/businesses/biz-1/items?date_from=2026-10-01&date_to=2026-10-14")
    assert res.status_code == 200
    assert seen["date_from"] == date(2026, 10, 1) and seen["date_to"] == date(2026, 10, 14)


# ── 화면 배선 ─────────────────────────────────────────────

def _block(start: str, end: str) -> str:
    head = INDEX.index(start)
    return INDEX[head:INDEX.index(end, head)]


def test_glance_sits_in_the_home_between_header_and_kpis():
    dash = _block('<section id="dashboardView">', '<section id="tasksView"')
    assert dash.index('class="home-head"') < dash.index('id="obysGlance"') < dash.index('id="homeKpiGrid"')


def test_delivery_panel_is_inside_sales_view_without_new_nav_tab():
    sales = _block('<section id="salesView"', '<section id="accountsView"')
    assert 'id="obysDeliveryPanel"' in sales
    assert sales.index('id="obysDeliveryPanel"') < sales.index('id="salesSummaryRows"')
    side = _block('<nav class="side-nav"', "</nav>")
    assert "obysDeliveryPanel" not in side and "delivery" not in side.lower()


def test_assets_loaded_with_versions_and_set_view_hooks():
    assert re.search(r"modules/data-overview\.css\?v=\d{8}-r\d+", INDEX)
    assert re.search(r"modules/data-overview\.js\?v=\d{8}-r\d+", INDEX)
    assert re.search(r"modules/clobe-collection\.js\?v=20261014-r\d+", INDEX)
    set_view = _block("function setView(view) {", "applyAccessPolicy();\n    }")
    assert 'currentView === "dashboard") window.obysDataOverview?.openGlance()' in set_view
    assert 'currentView === "sales") window.obysDataOverview?.openDelivery()' in set_view


def test_glance_has_required_cards_and_review_shortcut():
    for copy in ("오늘 한눈에", "배달앱 매출", "카드·현금영수증 매출", "입금 예정", "입금 완료", "자료 수집 현황", "마지막 수집", "검토 대기", "검토하러 가기"):
        assert copy in JS, copy
    assert 'data-od-action="open-review"' in JS and 'goView("clobeReview")' in JS
    assert "합치지 않습니다" in JS


def test_stale_delivery_shows_last_date_reconnect_and_coupang_csv_path():
    for copy in ("수집이 멈춤", "다시 연결", "정산 CSV 올리기", "마지막으로 수집된 자료"):
        assert copy in JS, copy
    assert "csv_upload_available" in JS and 'goView("integrations")' in JS
    assert '[data-ledger-upload="sales"] [data-upload-file]' in JS
    assert 'data-ledger-upload="sales"' in INDEX


def test_clobe_channel_tab_has_connect_required_fallback():
    assert "클로브에서 배달앱 연결 필요" in JS
    assert "connect_required" in JS and "/channel-revenue" in JS
    assert "클로브 기준" in JS


def test_module_uses_only_read_endpoints_and_no_secrets():
    assert "/api/v1/obys-collections" in JS
    assert not re.search(r'method:\s*"(POST|PUT|PATCH|DELETE)"', JS)
    for forbidden in ("ANTHROPIC", "sk-ant", "api_key", "apikey", "Bearer sk"):
        assert forbidden.lower() not in JS.lower()


def test_visible_copy_has_no_internal_identifiers():
    visible = "\n".join(re.findall(r'(?:"[^"\n]*[가-힣][^"\n]*"|`[^`\n]*[가-힣][^`\n]*`)', JS))
    visible = re.sub(r"\$\{[^}]*\}", "", visible)
    for token in ("data_kind", "business_id", "channel_revenue", "source_key", "run_id", "job_id", "runner-"):
        assert token not in visible, token


def test_mobile_layout_has_wrapping_and_large_touch_targets():
    assert "min-height: 44px" in CSS and "min-height: 48px" in CSS
    assert "max-width: 820px" in CSS
    assert "overflow-wrap: anywhere" in CSS and "word-break: keep-all" in CSS
    assert "attr(data-label)" in CSS


def test_review_box_has_pagination_memo_fallback_and_server_period():
    assert "ROWS_PER_PAGE = 50" in CLOBE_JS
    assert 'data-clobe-action="page"' in CLOBE_JS and 'case "page":' in CLOBE_JS
    assert 'item.counterparty || item.memo || "거래처 없음"' in CLOBE_JS
    assert 'params.set("date_from", from)' in CLOBE_JS and 'params.set("date_to", to)' in CLOBE_JS
    assert '["stage", "kind", "from", "to"].includes(key)' in CLOBE_JS
    assert "pageItems().forEach" in CLOBE_JS
    css = (ROOT / "modules" / "clobe-collection.css").read_text(encoding="utf-8")
    assert ".clobe-pager" in css


def test_review_box_keeps_confirm_flow_untouched():
    for marker in ("되돌릴 수 없습니다", "state.ackMismatch", 'state.pending?.scope === "bulk"', 'data-clobe-action="pending-go"'):
        assert marker in CLOBE_JS, marker


# ── V4.1(로그인 후 기본 화면) 배선 ─────────────────────────

V41 = (ROOT / "admin.html").read_text(encoding="utf-8")


def test_v41_page_loads_overview_assets_after_its_inline_app():
    assert re.search(r'<link rel="stylesheet" href="modules/data-overview\.css\?v=\d{8}-r\d+">', V41)
    script = re.search(r'<script src="/static/apps/obys/modules/data-overview\.js\?v=\d{8}-r\d+"></script>', V41)
    assert script, "data-overview.js 가 V4.1 에 없다"
    assert V41.index("function renderPageV41()") < script.start() < V41.index("modules/pwa-install.js")


def test_v41_mounts_only_on_home_and_sales_and_never_loops_on_its_own_output():
    assert 'const business = homeBusiness();' in JS and "obys_v41_business_id" in JS
    assert 'route !== "home" && route !== "sales"' in JS
    assert 'observe(document.getElementById("page"), { childList: true })' in JS
    assert "subtree" not in JS
    assert "!ui.glance.attempted" in JS


def test_v41_navigation_has_legacy_deep_link_for_review_box():
    assert 'LEGACY_URL = "/static/apps/obys/index.html?legacy=1&view=legacy"' in JS
    assert "&goto=${encodeURIComponent(view)}" in JS
    assert 'get("goto")' in JS and '/^[A-Za-z]+$/.test(goto || "")' in JS
    assert 'document.getElementById("quickRegister")?.click()' in JS


def test_v41_scope_defines_missing_design_tokens():
    for token in ("--panel:", "--accent:", ".od-v41 .panel-head", ".od-v41-delivery"):
        assert token in CSS, token
