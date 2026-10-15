"""클로브 판매채널 매출(get_monthly_revenue → data_kind=channel_revenue): 정규화, 멱등 source_key, 미연결, 확정 제외."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import obys_collections as api
from app.auth import get_current_user
from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_core as core
from app.services import obys_collection_service as svc

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
CID = "co-1"

# 응답 형태는 운영 실측 전이라 별칭 표 기준의 대표 형태를 쓴다(중첩 + 채널 + 기관 + 일자).
NESTED = {
    "companyId": CID,
    "channels": [
        {"channel": "DELIVERY", "institutions": [
            {"institutionName": "배달의민족", "days": [
                {"date": "2026-10-01", "grossAmount": 100000, "netAmount": 88000, "feeAmount": 12000, "count": 5},
                {"date": "2026-10-02", "grossAmount": 50000, "netAmount": 44000, "feeAmount": 6000, "count": 2}]},
            {"institutionName": "쿠팡이츠", "days": [
                {"date": "2026-10-01", "grossAmount": 30000, "netAmount": 25000, "feeAmount": 5000, "count": 1}]}]},
        {"channel": "CARD", "institutions": [
            {"institutionName": "신한카드", "days": [
                {"date": "2026-10-01", "grossAmount": 200000, "netAmount": 196000, "feeAmount": 4000, "count": 9}]}]},
    ],
    "summary": {"grossAmount": 380000, "netAmount": 353000, "feeAmount": 27000, "count": 17},
}


def parse(payload, **kw):
    return core.parse_channel_payload(payload, company_id=CID, date_basis=kw.pop("date_basis", "SETTLEMENT"), **kw)


# ── normalize ────────────────────────────────────────────

def test_normalize_nested_response_maps_channel_institution_amounts_and_skips_summary():
    out = parse(NESTED)
    recs = {r["source_key"]: r for r in out["records"]}
    assert len(recs) == 4  # summary 합계 행은 레코드가 아니다
    baemin = recs["ch|co-1|DELIVERY|배민|2026-10-01|SETTLEMENT"]  # 입력 "배달의민족" → 표준명 "배민"
    assert (baemin["channel"], baemin["institution"], baemin["grain"]) == ("DELIVERY", "배민", "DAY")
    assert (baemin["gross"], baemin["net"], baemin["fee"], baemin["count"]) == (
        Decimal("100000.00"), Decimal("88000.00"), Decimal("12000.00"), 5)
    assert recs["ch|co-1|CARD|신한카드|2026-10-01|SETTLEMENT"]["gross"] == Decimal("200000.00")


def test_normalize_item_columns():
    rec = parse(NESTED)["records"][0]
    row = core.normalize_item(core.KIND_CHANNEL, rec)
    assert row["source_key"] == rec["source_key"]
    assert row["institution"] == "배민" and row["direction"] == "DELIVERY"
    assert row["occurred_on"] == date(2026, 10, 1) and row["amount"] == Decimal("100000.00")


def test_channel_code_aliases_and_dict_key_channel_and_month_grain():
    assert [core.channel_code(v) for v in ("delivery_app", "마켓플레이스", "Open_Market", "pg", "", "XYZ")] == [
        "DELIVERY", "MKP", "MKP", "PG", "UNKNOWN", "XYZ"]
    payload = {"DELIVERY": [{"name": "요기요", "yearMonth": "2026-09", "gross": "1,500", "net": 1300, "fee": 200, "count": 3}]}
    (rec,) = parse(payload)["records"]
    assert rec["channel"] == "DELIVERY" and rec["grain"] == "MONTH" and rec["date"] == "2026-09-01"
    assert rec["source_key"] == "ch|co-1|DELIVERY|요기요|2026-09|SETTLEMENT" and rec["gross"] == Decimal("1500.00")


def test_month_call_without_date_field_uses_requested_month():
    (rec,) = parse({"rows": [{"channel": "PG", "name": "토스", "grossAmount": 10, "netAmount": 9, "feeAmount": 1, "count": 1}]},
                   default_month=date(2026, 8, 1))["records"]
    assert rec["date"] == "2026-08-01" and rec["grain"] == "MONTH"


def test_response_basis_overrides_requested_basis():
    (rec,) = parse({"rows": [{"channel": "CARD", "date": "2026-10-01", "dateBasis": "sale", "grossAmount": 1}]})["records"]
    assert rec["date_basis"] == "SALE"


# ── 멱등 source_key ──────────────────────────────────────

def test_source_key_is_stable_across_reads_and_hash_ignores_source_noise():
    a, b = parse(NESTED)["records"], parse(NESTED)["records"]
    assert [r["source_key"] for r in a] == [r["source_key"] for r in b]
    rows_a, rows_b = svc._prepare_rows(core.KIND_CHANNEL, a, None), svc._prepare_rows(core.KIND_CHANNEL, b, None)
    assert [r["hash"] for r in rows_a] == [r["hash"] for r in rows_b]
    noisy = [{**r, "source": {**r["source"], "updatedAt": "2026-10-14T00:00:00"}} for r in a]
    assert [r["hash"] for r in svc._prepare_rows(core.KIND_CHANNEL, noisy, None)] == [r["hash"] for r in rows_a]


def test_changed_amount_changes_hash_but_not_source_key():
    first = svc._prepare_rows(core.KIND_CHANNEL, parse(NESTED)["records"], None)[0]
    revised = parse(NESTED)["records"][0]
    revised["gross"] = Decimal("1.00")
    again = svc._prepare_rows(core.KIND_CHANNEL, [revised], None)[0]
    assert again["source_key"] == first["source_key"] and again["hash"] != first["hash"]


def test_duplicate_keys_are_summed_into_one_row():
    rows = {"rows": [{"channel": "CARD", "name": "신한카드", "date": "2026-10-01", "grossAmount": 100, "count": 1},
                     {"channel": "CARD", "name": "신한카드", "date": "2026-10-01", "grossAmount": 50, "count": 2}]}
    (rec,) = parse(rows)["records"]
    assert rec["gross"] == Decimal("150.00") and rec["count"] == 3


# ── 빈 응답 vs 형식 변경 ─────────────────────────────────

@pytest.mark.parametrize("payload", [{}, {"companyId": CID, "channels": []}, {"content": [], "totalElements": 0},
                                      {"months": [{"month": "2026-10", "channels": []}]}])
def test_empty_response_yields_no_records_and_no_error(payload):
    out = parse(payload)
    assert out["records"] == [] and out["connected"] is None


def test_unknown_numeric_shape_is_an_error_not_not_connected():
    with pytest.raises(ValueError, match="channel_unexpected_shape"):
        parse({"rows": [{"channel": "CARD", "date": "2026-10-01", "salesTotalWon": 100}]})


def test_metrics_without_date_is_an_error():
    with pytest.raises(ValueError, match="channel_unexpected_shape"):
        parse({"rows": [{"channel": "CARD", "grossAmount": 100}]})


def test_explicit_connected_false_is_reported():
    assert parse({"connected": False, "channels": []})["connected"] is False


# ── 입력 스키마 → 호출 인자 ──────────────────────────────

def _schema(props, wrap=False):
    inner = {"type": "object", "properties": props}
    return {"type": "object", "properties": {"input": inner}} if wrap else inner


def test_calls_use_range_args_from_schema_and_pick_settlement_basis_and_day_granularity():
    schema = _schema({"companyId": {}, "startDate": {}, "endDate": {}, "dateBasis": {"enum": ["SALE", "SETTLEMENT"]},
                      "granularity": {"enum": ["MONTH", "DAY"]}}, wrap=True)
    (call,) = core.build_channel_calls(schema, CID, date(2026, 7, 14), date(2026, 10, 12))
    assert call["args"] == {"companyId": CID, "startDate": "2026-07-14", "endDate": "2026-10-12",
                            "dateBasis": "SETTLEMENT", "granularity": "DAY"}
    assert call["basis"] == "SETTLEMENT" and call["month"] is None


def test_calls_split_by_month_when_schema_only_takes_a_month():
    schema = _schema({"companyId": {}, "yearMonth": {}})
    calls = core.build_channel_calls(schema, CID, date(2026, 8, 20), date(2026, 10, 2))
    assert [c["args"]["yearMonth"] for c in calls] == ["2026-08", "2026-09", "2026-10"]
    assert [c["month"] for c in calls] == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    ym = core.build_channel_calls(_schema({"year": {}, "month": {}}), CID, date(2026, 12, 5), date(2027, 1, 3))
    assert [(c["args"]["year"], c["args"]["month"]) for c in ym] == [(2026, 12), (2027, 1)]


def test_calls_never_send_args_missing_from_schema_and_fallback_without_schema():
    (call,) = core.build_channel_calls(_schema({"companyId": {}, "startDate": {}, "endDate": {}}), CID,
                                       date(2026, 10, 1), date(2026, 10, 2))
    assert set(call["args"]) == {"companyId", "startDate", "endDate"}
    (fallback,) = core.build_channel_calls({}, CID, date(2026, 10, 1), date(2026, 10, 2))
    assert fallback["args"] == {"companyId": CID, "startDate": "2026-10-01", "endDate": "2026-10-02"}


def test_basis_env_override(monkeypatch):
    monkeypatch.setenv("OBYS_CLOBE_CHANNEL_DATE_BASIS", "sale")
    schema = _schema({"companyId": {}, "startDate": {}, "endDate": {}, "dateBasis": {"enum": ["SALE", "SETTLEMENT"]}})
    (call,) = core.build_channel_calls(schema, CID, date(2026, 10, 1), date(2026, 10, 2))
    assert call["args"]["dateBasis"] == "SALE"


def test_allowlist_has_the_revenue_tool_and_it_is_read_only():
    assert clobe.COLLECTION_DATA_TOOLS[core.KIND_CHANNEL] == core.CHANNEL_TOOL
    assert core.CHANNEL_TOOL in clobe.COLLECTION_TOOLS
    assert clobe.classify_tool(core.CHANNEL_TOOL)[0]


# ── 수집: 미연결 / 적재 ──────────────────────────────────

class FakeConn:
    def __init__(self, had_before=False):
        self.had_before = had_before
        self.executed: list[tuple] = []

    async def fetchval(self, *_a):
        return self.had_before

    async def fetchrow(self, *_a):
        return None

    async def execute(self, *a):
        self.executed.append(a)


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, args=None):
        self.calls.append((name, args))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _ctx(kind=core.KIND_CHANNEL):
    return svc.RunCtx(CID, uuid.UUID(TENANT), "biz-1", "inst", 1, 300, "live", kind=kind,
                      run_id=uuid.uuid4(), chain_id=uuid.uuid4())


@pytest.fixture
def written(monkeypatch):
    out: list[dict] = []

    async def schema():
        return {"properties": {"companyId": {}, "startDate": {}, "endDate": {}}}

    async def write_page(_conn, _ctx, rows, _state, _counts, _seen):
        out.extend(rows)

    monkeypatch.setattr(svc, "_channel_input_schema", schema)
    monkeypatch.setattr(svc, "_write_page", write_page)
    return out


async def _collect(conn, session):
    counts = {"pages": 0, "received": 0}
    return await svc._collect_channel(conn, session, _ctx(), date(2026, 10, 1), date(2026, 10, 3), {}, {}, counts), counts


async def test_empty_response_without_history_is_not_connected_not_error(written):
    out, _ = await _collect(FakeConn(had_before=False), FakeSession({"channels": []}))
    assert out["not_connected"] is True and written == []


async def test_empty_response_with_history_is_a_normal_empty_period(written):
    out, _ = await _collect(FakeConn(had_before=True), FakeSession({"channels": []}))
    assert out["not_connected"] is False and written == []


async def test_explicit_disconnect_flag_wins_even_with_history(written):
    out, _ = await _collect(FakeConn(had_before=True), FakeSession({"connected": False, "channels": []}))
    assert out["not_connected"] is True


async def test_data_is_written_once_per_source_key(written):
    session = FakeSession(NESTED)
    out, counts = await _collect(FakeConn(), session)
    assert out["not_connected"] is False
    assert session.calls[0][0] == "get_monthly_revenue"
    assert len(written) == 4 and len({r["source_key"] for r in written}) == 4
    assert counts["received"] == 4


async def test_unexpected_shape_is_partial_error_code(written):
    with pytest.raises(svc.CollectionError) as exc:
        await _collect(FakeConn(), FakeSession({"rows": [{"channel": "CARD", "date": "2026-10-01", "weird": 1}]}))
    assert exc.value.code == "channel_unexpected_shape"


async def test_not_connected_state_row_is_recorded_without_failure_counter():
    class Conn(FakeConn):
        async def fetchrow(self, *_a):
            return {"consecutive_failures": 3}

    conn = Conn()
    await svc._update_state(conn, _ctx(), "succeeded", None, None, date(2026, 10, 1), date(2026, 10, 3),
                            state_status="not_connected")
    sql, *args = conn.executed[0]
    assert "$7" in sql and "0,$4,$5,$6" in sql  # consecutive_failures 는 0 으로 초기화된다
    assert "not_connected" in args and core.CHANNEL_NOT_CONNECTED in args


# ── 확정/전표 제외 ───────────────────────────────────────

def test_channel_revenue_is_not_a_confirmable_kind():
    assert core.KIND_CHANNEL not in core.DATA_KINDS and core.KIND_CHANNEL in core.COLLECTION_KINDS


async def test_post_reviewed_rows_skips_channel_rows_entirely():
    class Conn:
        async def execute(self, *_a):
            raise AssertionError("ledger/stage must not be written for channel rows")

        async def fetchrow(self, *_a):
            raise AssertionError("ledger must not be read for channel rows")

    row = {"data_kind": core.KIND_CHANNEL, "source_key": "k", "item_id": uuid.uuid4(), "item_hash": "h",
           "occurred_on": None, "amount": None, "direction": "DELIVERY"}
    assert await svc._post_reviewed_rows(Conn(), uuid.UUID(TENANT), "biz-1", [row], "u") == 0


async def test_bulk_confirm_rejects_channel_kind():
    with pytest.raises(svc.CollectionError) as exc:
        await svc.bulk_review_confirm(TENANT, "biz-1", kinds=[core.KIND_CHANNEL], actor="u", dry_run=True)
    assert exc.value.code == "invalid_kinds"


async def test_review_and_list_queries_are_restricted_to_ledger_kinds(monkeypatch):
    seen: list[tuple] = []

    class Conn:
        async def fetchval(self, *a):
            seen.append(a)
            return True

        async def fetch(self, *a):
            seen.append(a)
            return []

        async def close(self):
            pass

    async def connect():
        return Conn()

    monkeypatch.setattr(svc, "_connect", connect)
    await svc.review_items(TENANT, "biz-1", [str(uuid.uuid4())], "approve", actor="u")
    await svc.list_items(TENANT, "biz-1")
    assert seen, "queries were not issued"
    for sql, *args in seen:
        if "obys_clobe_item" in sql:
            assert "data_kind = ANY(" in sql
            assert list(core.DATA_KINDS) in args and core.KIND_CHANNEL not in core.DATA_KINDS


# ── 조회 API ─────────────────────────────────────────────

def _client(role="viewer"):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    user = {"tenant_id": TENANT, "email": "u@example.com", "is_internal_admin": False,
            "current_membership": {"tenant_id": TENANT, "role": role, "status": "active"}}
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_channel_revenue_endpoint_passes_filters(monkeypatch):
    got = {}

    async def fake(tenant, business, **kw):
        got.update(tenant=tenant, business=business, **kw)
        return {"rows": [], "confirmable": False}

    monkeypatch.setattr(svc, "list_channel_revenue", fake)
    resp = _client().get("/api/v1/obys-collections/businesses/biz-1/channel-revenue",
                         params={"date_from": "2026-10-01", "date_to": "2026-10-31", "company_id": CID,
                                 "channel": "DELIVERY", "date_basis": "SETTLEMENT"})
    assert resp.status_code == 200 and resp.json()["confirmable"] is False
    assert got["business"] == "biz-1" and got["company_id"] == CID and got["channel"] == "DELIVERY"
    assert got["date_from"] == date(2026, 10, 1) and got["date_to"] == date(2026, 10, 31)


def test_channel_revenue_endpoint_rejects_unknown_channel_and_non_members(monkeypatch):
    assert _client().get("/api/v1/obys-collections/businesses/b/channel-revenue?channel=BOGUS").status_code == 422
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: {
        "tenant_id": TENANT, "current_membership": {"tenant_id": "other", "role": "owner", "status": "active"}}
    assert TestClient(app).get("/api/v1/obys-collections/businesses/b/channel-revenue").status_code == 403


def test_run_request_accepts_channel_revenue_kind(monkeypatch):
    async def run(company_id, **kw):
        return {"status": "succeeded", "kinds": kw["kinds"]}

    async def ok(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "run_collection", run)
    monkeypatch.setattr(svc, "require_company_tenant", ok)
    resp = _client("owner").post("/api/v1/obys-collections/companies/c1/runs", json={"kinds": ["channel_revenue"]})
    assert resp.status_code == 200 and resp.json()["kinds"] == ["channel_revenue"]


async def test_list_channel_revenue_validates_period_before_touching_db(monkeypatch):
    async def boom():
        raise AssertionError("db must not be touched")

    monkeypatch.setattr(svc, "_connect", boom)
    with pytest.raises(svc.CollectionError) as exc:
        await svc.list_channel_revenue(TENANT, "biz-1", date_from="2026-10-31", date_to="2026-10-01")
    assert exc.value.code == "invalid_period"


# ── 클로브 실측 형태(sourcePlatform · *Krw · total* 누계) ─────

def _daily_row(platform, day, sales, fee, settle, count):
    return {"channel": "MKP", "timeUnit": "DAILY", "date": day, "sourcePlatform": platform,
            "salesAmountKrw": sales, "feeAmountKrw": fee, "settlementAmountKrw": settle, "salesCount": count,
            "totalSalesAmountKrw": 9_999_999, "totalFeeAmountKrw": 8_888_888, "totalSettlementAmountKrw": 7_777_777,
            "totalSalesCount": 777, "totalCount": 555}


def _clobe_page(*rows):
    return {"content": list(rows), "page": 0, "size": 20, "hasNext": False, "totalCount": 555,
            "totalSalesAmountKrw": 9_999_999, "totalFeeAmountKrw": 8_888_888, "totalSettlementAmountKrw": 7_777_777,
            "totalSalesCount": 777}


CLOBE_DAY = _clobe_page(
    _daily_row("배민", "2026-10-16", 2_000_000, 700_000, 1_300_000, 90),
    _daily_row("쿠팡이츠", "2026-10-16", 300_000, 90_000, 210_000, 12),
    _daily_row("요기요", "2026-10-16", 50_000, 10_000, 40_000, 3))


def test_clobe_daily_rows_split_by_delivery_app_and_use_day_values_only():
    out = parse(CLOBE_DAY)
    recs = {r["institution"]: r for r in out["records"]}
    assert set(recs) == {"배민", "쿠팡이츠", "요기요"} and len(out["records"]) == 3
    baemin = recs["배민"]
    assert baemin["source_key"] == "ch|co-1|MKP|배민|2026-10-16|SETTLEMENT" and baemin["channel"] == "MKP"
    assert (baemin["gross"], baemin["fee"], baemin["net"], baemin["count"]) == (
        Decimal("2000000.00"), Decimal("700000.00"), Decimal("1300000.00"), 90)
    assert recs["요기요"]["gross"] == Decimal("50000.00") and recs["요기요"]["count"] == 3
    used = {v for r in out["records"] for v in (r["gross"], r["fee"], r["net"], r["count"])}
    assert not used & {9_999_999, 8_888_888, 7_777_777, 777, 555, Decimal("9999999.00")}


def test_period_totals_are_never_a_daily_value_even_when_the_day_value_is_missing():
    row = _daily_row("배민", "2026-10-16", None, None, None, None)
    row = {k: v for k, v in row.items() if v is not None}
    (rec,) = parse(_clobe_page(row))["records"]
    assert (rec["gross"], rec["fee"], rec["net"], rec["count"]) == (None, None, None, None)


def test_same_platform_same_day_is_one_record_and_institution_alias_is_normalized():
    rows = _clobe_page(_daily_row("배달의민족", "2026-10-16", 100, 10, 90, 1), _daily_row("배민", "2026-10-16", 50, 5, 45, 2))
    (rec,) = parse(rows)["records"]
    assert rec["institution"] == "배민" and rec["gross"] == Decimal("150.00") and rec["count"] == 3
    assert [core.institution_name(v) for v in ("쿠팡 이츠", "Yogiyo", "땡겨요", None)] == ["쿠팡이츠", "요기요", "땡겨요", ""]


def test_normalize_item_puts_daily_gross_into_amount_column():
    row = core.normalize_item(core.KIND_CHANNEL, parse(CLOBE_DAY)["records"][0])
    assert row["amount"] == Decimal("2000000.00") and row["institution"] == "배민" and row["direction"] == "MKP"


def test_legacy_channel_key_drops_only_the_institution_segment():
    assert core.legacy_channel_key("ch|co-1|MKP|배민|2026-10-16|SETTLEMENT") == "ch|co-1|MKP||2026-10-16|SETTLEMENT"
    assert core.legacy_channel_key("ch|co-1|MKP||2026-10-16|SETTLEMENT") is None
    assert core.legacy_channel_key("fp:abc:1") is None


def test_empty_page_with_period_totals_keeps_unexpected_shape_verdict():
    # 판정 불변 확인: total* 를 별칭에서 뺐어도 '지표가 있는 객체' 판정은 그대로다(별도 작업 범위).
    with pytest.raises(ValueError, match="channel_unexpected_shape"):
        parse({"content": [], "totalCount": 0, "page": 0, "size": 20})


# ── 옛 빈-기관명 행 supersede ─────────────────────────────

class LegacyConn:
    def __init__(self, legacy_rows):
        self.legacy_rows = legacy_rows
        self.updated: list = []
        self.inserted: list = []

    async def fetch(self, sql, *args):
        if "ORDER BY first_seen_at DESC" in sql and args[3].startswith("ch|") and "||" in args[3]:
            return self.legacy_rows
        return []

    async def execute(self, sql, *args):
        (self.updated if sql.startswith("UPDATE obys_clobe_item SET stage='superseded'") else self.inserted).append((sql, args))


def _upsert_ctx():
    return svc.RunCtx(CID, uuid.UUID(TENANT), "biz-1", "inst", 1, 300, "live", kind=core.KIND_CHANNEL,
                      run_id=uuid.uuid4(), chain_id=uuid.uuid4())


async def _upsert_all(conn):
    counts = {"inserted": 0, "unchanged": 0, "revised": 0}
    for row in svc._prepare_rows(core.KIND_CHANNEL, parse(CLOBE_DAY)["records"], None):
        await svc._upsert_item(conn, _upsert_ctx(), row, counts)
    return counts


async def test_new_institution_rows_supersede_the_blank_institution_row_without_deleting():
    old_id, older_id = uuid.uuid4(), uuid.uuid4()
    conn = LegacyConn([{"item_id": old_id, "stage": "review_box"}, {"item_id": older_id, "stage": "reviewed"}])
    counts = await _upsert_all(conn)
    assert counts["inserted"] == 3
    assert [a[0] for _, a in conn.updated[:2]] == [old_id, older_id] and not any("DELETE" in s for s, _ in conn.updated + conn.inserted)
    assert len(conn.inserted) == 3
    assert all(a[14] == old_id for _, a in conn.inserted)  # supersedes_item_id


async def test_confirmed_blank_institution_row_is_kept_and_counted():
    conn = LegacyConn([{"item_id": uuid.uuid4(), "stage": "confirmed"}])
    counts = await _upsert_all(conn)
    assert conn.updated == [] and counts["legacy_confirmed_kept"] == 3 and counts["inserted"] == 3


async def test_no_legacy_row_means_plain_insert():
    conn = LegacyConn([])
    counts = await _upsert_all(conn)
    assert conn.updated == [] and counts["inserted"] == 3 and all(a[14] is None for _, a in conn.inserted)
