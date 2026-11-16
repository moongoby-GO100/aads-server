"""백필 승인 게이트: 구간 판정, 클로브 수동 API, 배달 full_backfill, 관리자 승인 API.

실 DB 는 쓰지 않는다. 승인 행 로더(load_rows)와 DB 접속(_connect)은 대체한다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import obys_collections as api
from app.auth import get_current_user
from app.services import obys_backfill_approval as approval
from app.services import obys_collection_core as core
from app.services import obys_collection_service as svc
from app.services import yeoljeong_finance_service as yjf

ROOT = Path(__file__).parents[2]
D = date


def row(scope="c1", kind="channel_revenue", start=D(2026, 1, 1), end=D(2026, 12, 31), source="clobe", branch="", revoked=None):
    return {"source": source, "scope_id": scope, "branch": branch, "data_kind": kind,
            "period_start": start, "period_end": end, "revoked_at": revoked}


def check(rows, **kw):
    base = {"source": "clobe", "scope_id": "c1", "kind": "channel_revenue", "start": D(2026, 3, 1), "end": D(2026, 3, 31)}
    approval.check_rows(rows, **{**base, **kw})


# ── 구간 판정 ───────────────────────────────────────────────

def test_no_rows_is_409_with_the_required_message():
    with pytest.raises(approval.BackfillNotApproved) as ei:
        check([])
    assert ei.value.status == 409
    assert ei.value.message.startswith("승인되지 않은 회사·기간입니다(승인 범위: 없음")


def test_inside_the_approved_range_passes_including_both_edges():
    check([row()], start=D(2026, 1, 1), end=D(2026, 12, 31))


@pytest.mark.parametrize("start,end", [(D(2025, 12, 31), D(2026, 3, 1)), (D(2026, 3, 1), D(2027, 1, 1))])
def test_one_day_beyond_either_edge_is_409(start, end):
    with pytest.raises(approval.BackfillNotApproved, match="2026-01-01~2026-12-31"):
        check([row()], start=start, end=end)


def test_channel_revenue_is_checked_against_whole_calendar_months():
    """월 단위 스키마는 yearMonth=2026-01 로 그 달 전체를 읽는다 — 날짜 일부만 승인해서는 통과하지 못한다."""
    partial = [row(start=D(2026, 1, 10), end=D(2026, 1, 15))]
    with pytest.raises(approval.BackfillNotApproved, match="실제 조회 범위: 2026-01-01~2026-01-31"):
        check(partial, start=D(2026, 1, 10), end=D(2026, 1, 15))
    check([row(start=D(2026, 1, 1), end=D(2026, 1, 31))], start=D(2026, 1, 10), end=D(2026, 1, 15))
    check([row(start=D(2025, 12, 1), end=D(2026, 2, 28))], start=D(2025, 12, 31), end=D(2026, 2, 1))
    with pytest.raises(approval.BackfillNotApproved):
        check([row(start=D(2025, 12, 1), end=D(2026, 2, 27))], start=D(2025, 12, 31), end=D(2026, 2, 1))


def test_other_kinds_and_delivery_are_still_checked_by_exact_dates():
    check([row(kind="bank_transaction", start=D(2026, 1, 10), end=D(2026, 1, 15))],
          kind="bank_transaction", start=D(2026, 1, 10), end=D(2026, 1, 15))
    check([row(source="delivery", kind="baemin", start=D(2026, 1, 10), end=D(2026, 1, 15))],
          source="delivery", kind="baemin", start=D(2026, 1, 10), end=D(2026, 1, 15))


@pytest.mark.parametrize("start,end", [(D(2026, 1, 10), D(2026, 1, 15)), (D(2025, 12, 31), D(2026, 2, 1)), (D(2026, 2, 28), D(2026, 3, 1))])
def test_effective_range_covers_every_month_the_monthly_channel_calls_actually_read(start, end):
    schema = {"properties": {"companyId": {"type": "string"}, "yearMonth": {"type": "string"}}}
    calls = core.build_channel_calls(schema, "c1", start, end)
    low, high = approval.effective_range("clobe", core.KIND_CHANNEL, start, end)
    assert calls and all(low <= c["month"] and (c["month"] + timedelta(days=27)) <= high for c in calls)
    assert (low, high) == (calls[0]["month"], (calls[-1]["month"].replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))


def test_adjacent_and_overlapping_approvals_combine_but_a_gap_does_not():
    two = [row(start=D(2026, 1, 1), end=D(2026, 3, 15)), row(start=D(2026, 3, 16), end=D(2026, 6, 30))]
    check(two, start=D(2026, 3, 1), end=D(2026, 4, 30))
    gapped = [row(start=D(2026, 1, 1), end=D(2026, 3, 14)), row(start=D(2026, 3, 16), end=D(2026, 6, 30))]
    with pytest.raises(approval.BackfillNotApproved):
        check(gapped, start=D(2026, 3, 1), end=D(2026, 4, 30))


def test_other_company_other_kind_other_source_and_revoked_never_count():
    for other in (row(scope="c2"), row(kind="bank_transaction"), row(source="delivery"), row(revoked=D(2026, 2, 1))):
        with pytest.raises(approval.BackfillNotApproved):
            check([other])


def test_delivery_branch_scoping_blank_branch_covers_every_branch_but_named_branch_only_itself():
    kw = {"source": "delivery", "scope_id": "biz-mia", "kind": "baemin"}
    named = row(scope="biz-mia", kind="baemin", source="delivery", branch="미아점")
    check([named], branch="미아점", **kw)
    with pytest.raises(approval.BackfillNotApproved):
        check([named], branch="강남점", **kw)
    check([row(scope="biz-mia", kind="baemin", source="delivery")], branch="강남점", **kw)


# ── 클로브 수동 API ──────────────────────────────────────────

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"


def _admin():
    return {"tenant_id": TENANT, "email": "ceo@example.com", "is_internal_admin": True,
            "current_membership": {"tenant_id": TENANT, "role": "owner", "status": "active"}}


def _owner():
    return {**_admin(), "is_internal_admin": False}


def _client(user):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def runs(monkeypatch):
    state = type("S", (), {})()
    state.rows, state.calls = [], []

    async def runnable(company_id):
        return None

    async def run_collection(company_id, **kw):
        state.calls.append((company_id, kw))
        return {"company_id": company_id, "status": "succeeded", "runs": []}

    async def load_rows(source, scope_id, kind):
        return list(state.rows)

    monkeypatch.setattr(svc, "require_runnable_company", runnable)
    monkeypatch.setattr(svc, "run_collection", run_collection)
    monkeypatch.setattr(approval, "load_rows", load_rows)
    return state


URL = "/api/v1/obys-collections/companies/c1/runs"


def test_manual_run_with_default_or_recent_window_is_not_a_backfill(runs):
    recent = (date.today() - timedelta(days=core.DEFAULT_WINDOW_DAYS - 1)).isoformat()
    assert _client(_admin()).post(URL, json={}).status_code == 200
    assert _client(_admin()).post(URL, json={"period_start": recent}).status_code == 200
    assert len(runs.calls) == 2


def test_manual_run_older_than_default_window_without_approval_is_409_and_runs_nothing(runs):
    resp = _client(_admin()).post(URL, json={"period_start": "2026-01-01", "period_end": "2026-02-01"})
    assert resp.status_code == 409 and resp.json()["detail"].startswith("승인되지 않은 회사·기간입니다(승인 범위: 없음")
    assert runs.calls == []


def test_manual_backfill_requires_every_requested_kind_to_be_approved(runs):
    body = {"period_start": "2026-01-10", "period_end": "2026-02-01", "kinds": ["channel_revenue", "bank_transaction"]}
    runs.rows = [row()]
    assert _client(_admin()).post(URL, json=body).status_code == 409 and runs.calls == []
    runs.rows = [row(), row(kind="bank_transaction")]
    assert _client(_admin()).post(URL, json=body).status_code == 200 and len(runs.calls) == 1


def test_manual_channel_backfill_with_partial_month_approval_is_409_and_runs_nothing(runs):
    runs.rows = [row(start=D(2026, 1, 10), end=D(2026, 1, 15))]
    resp = _client(_admin()).post(URL, json={"period_start": "2026-01-10", "period_end": "2026-01-15", "kinds": ["channel_revenue"]})
    assert resp.status_code == 409 and "실제 조회 범위: 2026-01-01~2026-01-31" in resp.json()["detail"]
    assert runs.calls == []
    runs.rows = [row(start=D(2026, 1, 1), end=D(2026, 1, 31))]
    assert _client(_admin()).post(URL, json={"period_start": "2026-01-10", "period_end": "2026-01-15", "kinds": ["channel_revenue"]}).status_code == 200


def test_invalid_period_is_left_to_run_collection_so_its_400_is_not_masked_by_a_409(runs):
    resp = _client(_admin()).post(URL, json={"period_start": "2020-01-01", "period_end": "2026-09-01"})
    assert resp.status_code == 200 and len(runs.calls) == 1  # 실제 run_collection 이 period_too_long(400)을 올린다


# ── 관리자 승인 API ──────────────────────────────────────────

BODY = {"source": "clobe", "scope_id": "c1", "data_kind": "channel_revenue",
        "period_start": "2026-01-01", "period_end": "2026-03-31", "reason": "분기 마감 대사"}


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/obys-collections/admin/backfill-approvals"),
    ("post", "/api/v1/obys-collections/admin/backfill-approvals"),
    ("post", "/api/v1/obys-collections/admin/backfill-approvals/3f2b7c0e-1111-4222-8333-444455556666/revoke"),
])
def test_approval_endpoints_are_internal_admin_only(method, path):
    resp = getattr(_client(_owner()), method)(path, **({"json": BODY} if method == "post" else {}))
    assert resp.status_code == 403


def test_create_approval_records_the_actor_and_validates_input(monkeypatch):
    seen = {}

    async def create(**kw):
        seen.update(kw)
        return {"approval_id": "x", "approved_by": kw["actor"]}

    monkeypatch.setattr(approval, "create_approval", create)
    resp = _client(_admin()).post("/api/v1/obys-collections/admin/backfill-approvals", json=BODY)
    assert resp.status_code == 201 and seen["actor"] == "ceo@example.com" and seen["start"] == D(2026, 1, 1)
    short = {**BODY, "reason": "짧음"}
    assert _client(_admin()).post("/api/v1/obys-collections/admin/backfill-approvals", json=short).status_code == 422

    async def bad(**kw):
        raise ValueError("unknown_scope")

    monkeypatch.setattr(approval, "create_approval", bad)
    resp = _client(_admin()).post("/api/v1/obys-collections/admin/backfill-approvals", json=BODY)
    assert resp.status_code == 400 and resp.json()["detail"] == "unknown_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,err", [
    ({"source": "x"}, "invalid_source"),
    ({"kind": "bogus"}, "invalid_kind"),
    ({"start": D(2026, 5, 1), "end": D(2026, 4, 1)}, "invalid_period"),
    ({"reason": "  짧 "}, "reason_required"),
    ({"branch": "미아점"}, "branch_not_allowed_for_clobe"),
])
async def test_create_approval_rejects_bad_input_before_touching_the_db(monkeypatch, kw, err):
    async def no_db():
        raise AssertionError("must not connect")

    monkeypatch.setattr(approval, "_connect", no_db)
    args = {"source": "clobe", "scope_id": "c1", "kind": "channel_revenue", "start": D(2026, 1, 1), "end": D(2026, 3, 1),
            "reason": "충분한 사유", "actor": "ceo"}
    with pytest.raises(ValueError, match=err):
        await approval.create_approval(**{**args, **kw})


# ── 배달 full_backfill ──────────────────────────────────────

def _delivery_payload(**kw):
    return {"services": ["baemin"], "business_id": "biz-mia", "branch": "열정국밥_미아점",
            "mode": "full_backfill", "date_from": "2024-01-01", "date_to": "2024-03-31", **kw}


@pytest.fixture
def delivery(monkeypatch):
    state = type("S", (), {})()
    state.rows, state.reads = [], 0

    async def load_rows(source, scope_id, kind):
        state.reads += 1
        return list(state.rows)

    monkeypatch.setattr(approval, "load_rows", load_rows)
    return state


def test_delivery_full_backfill_without_approval_is_409(delivery):
    with pytest.raises(HTTPException) as ei:
        yjf._require_delivery_backfill_approval(_delivery_payload())
    assert ei.value.status_code == 409 and ei.value.detail.startswith("승인되지 않은 회사·기간입니다(승인 범위: 없음")


def test_delivery_full_backfill_inside_range_passes(delivery):
    delivery.rows = [row(scope="biz-mia", kind="baemin", source="delivery", start=D(2024, 1, 1), end=D(2024, 12, 31),
                         branch="열정국밥_미아점")]
    yjf._require_delivery_backfill_approval(_delivery_payload())


def test_delivery_partial_overrun_other_business_and_other_service_are_409(delivery):
    ok = row(scope="biz-mia", kind="baemin", source="delivery", start=D(2024, 1, 1), end=D(2024, 3, 30))
    delivery.rows = [ok]
    with pytest.raises(HTTPException):
        yjf._require_delivery_backfill_approval(_delivery_payload())
    delivery.rows = [{**ok, "period_end": D(2024, 12, 31), "scope_id": "biz-other"}]
    with pytest.raises(HTTPException):
        yjf._require_delivery_backfill_approval(_delivery_payload())
    delivery.rows = [{**ok, "period_end": D(2024, 12, 31), "data_kind": "coupangeats"}]
    with pytest.raises(HTTPException):
        yjf._require_delivery_backfill_approval(_delivery_payload())


def test_delivery_regular_sync_does_not_consult_approvals(delivery):
    today = yjf.datetime.now(yjf.KST).date()
    yjf._require_delivery_backfill_approval(
        _delivery_payload(mode="", date_from=(today - timedelta(days=3)).isoformat(), date_to=today.isoformat()))
    yjf._require_delivery_backfill_approval(
        _delivery_payload(mode="", date_from=(today - timedelta(days=62)).isoformat(), date_to=today.isoformat()))
    yjf._require_delivery_backfill_approval({"services": ["baemin"]})
    assert delivery.reads == 0


@pytest.mark.parametrize("extra", [{"mode": ""}, {"mode": "incremental"}, {"collection_mode": "manual"}])
def test_delivery_old_period_without_full_backfill_mode_is_still_gated(delivery, extra):
    payload = _delivery_payload(date_from="2024-01-01", date_to="2024-01-31")
    payload.pop("mode")
    with pytest.raises(HTTPException) as ei:
        yjf._require_delivery_backfill_approval({**payload, **extra})
    assert ei.value.status_code == 409 and delivery.reads == 1


def test_delivery_old_period_without_mode_passes_only_inside_approved_range(delivery):
    payload = _delivery_payload(date_from="2024-01-01", date_to="2024-01-31")
    payload.pop("mode")
    delivery.rows = [row(scope="biz-mia", kind="baemin", source="delivery", start=D(2024, 1, 1), end=D(2024, 1, 31),
                         branch="열정국밥_미아점")]
    yjf._require_delivery_backfill_approval(payload)
    with pytest.raises(HTTPException):
        yjf._require_delivery_backfill_approval({**payload, "date_to": "2024-02-01"})


def test_sync_entry_points_refuse_old_period_without_mode(delivery, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("side effect before the approval gate")

    for name in ("_settle_stale_delivery_collection_statuses", "_try_acquire_delivery_sync_lock", "_read",
                 "_sync_delivery_unlocked"):
        monkeypatch.setattr(yjf, name, boom)
    monkeypatch.setattr(yjf, "_is_admin", lambda user: True)
    payload = _delivery_payload(date_from="2024-01-01", date_to="2024-01-31")
    payload.pop("mode")
    for entry in ("sync_delivery", "queue_delivery_sync"):
        with pytest.raises(HTTPException) as ei:
            getattr(yjf, entry)(payload, {"email": "a@b.c"})
        assert ei.value.status_code == 409


def _queue_row(rid, date_from, date_to, status="queued", branch="열정국밥_미아점"):
    return {"id": rid, "service": "baemin", "business_id": "biz-mia", "branch": branch, "status": status,
            "date_from": date_from, "date_to": date_to, "payload": {"mode": "full_backfill"}}


def test_queued_backfill_row_outside_approved_range_is_not_selected(delivery):
    """요청 기간(2026-01)은 승인됐어도 큐에 남은 2024-03-31 행은 실행 기간 기준으로 막혀야 한다."""
    delivery.rows = [row(scope="biz-mia", kind="baemin", source="delivery", start=D(2026, 1, 1), end=D(2026, 1, 31),
                         branch="열정국밥_미아점")]
    payload = _delivery_payload(date_from="2026-01-01", date_to="2026-01-31")
    scopes = [("biz-mia", "열정국밥_미아점")]
    statuses = [_queue_row("old", "2024-03-31", "2024-03-31")]
    with pytest.raises(HTTPException) as ei:
        yjf._delivery_select_baemin_backfill_statuses(statuses, payload, scopes)
    assert ei.value.status_code == 409 and "2024-03-31" in ei.value.detail


def test_queued_backfill_selection_skips_unapproved_rows_and_keeps_approved_ones(delivery):
    delivery.rows = [row(scope="biz-mia", kind="baemin", source="delivery", start=D(2026, 1, 1), end=D(2026, 1, 31),
                         branch="열정국밥_미아점")]
    payload = _delivery_payload(date_from="2026-01-01", date_to="2026-01-31", max_backfill_runs=4)
    scopes = [("biz-mia", "열정국밥_미아점")]
    statuses = [_queue_row("old", "2024-03-31", "2024-03-31"), _queue_row("new", "2026-01-30", "2026-01-31"),
                _queue_row("rev", "2026-01-10", "2026-02-01")]
    picked = yjf._delivery_select_baemin_backfill_statuses(statuses, payload, scopes)
    assert [r["id"] for r in picked] == ["new"]


def test_queued_backfill_row_of_other_business_approval_is_not_selected(delivery):
    delivery.rows = [row(scope="biz-other", kind="baemin", source="delivery", start=D(2024, 1, 1), end=D(2024, 12, 31))]
    scopes = [("biz-mia", "열정국밥_미아점")]
    with pytest.raises(HTTPException):
        yjf._delivery_select_baemin_backfill_statuses(
            [_queue_row("old", "2024-03-31", "2024-03-31")], _delivery_payload(), scopes)


def test_empty_queue_selects_nothing_without_reading_approvals(delivery):
    assert yjf._delivery_select_baemin_backfill_statuses([], _delivery_payload(), [("biz-mia", "x")]) == []
    assert delivery.reads == 0


def test_delivery_all_scope_needs_every_branch_approved(delivery, monkeypatch):
    monkeypatch.setattr(yjf, "_read", lambda name: [])
    payload = _delivery_payload(business_id="all", branch="전체", all_businesses=True)
    scopes = yjf._delivery_sync_scopes(payload, ["baemin"], [])
    assert len(scopes) > 1
    first_business, first_branch = scopes[0]
    delivery.rows = [row(scope=first_business, kind="baemin", source="delivery", start=D(2024, 1, 1), end=D(2024, 12, 31),
                         branch=first_branch)]
    with pytest.raises(HTTPException) as ei:
        yjf._require_delivery_backfill_approval(payload)
    assert ei.value.status_code == 409


@pytest.mark.parametrize("entry", ["sync_delivery", "queue_delivery_sync"])
def test_delivery_entry_points_refuse_before_any_side_effect(delivery, monkeypatch, entry):
    def boom(*a, **k):
        raise AssertionError("side effect before the approval gate")

    for name in ("_settle_stale_delivery_collection_statuses", "_try_acquire_delivery_sync_lock", "_read",
                 "_sync_delivery_unlocked"):
        monkeypatch.setattr(yjf, name, boom)
    monkeypatch.setattr(yjf, "_is_admin", lambda user: True)
    with pytest.raises(HTTPException) as ei:
        getattr(yjf, entry)(_delivery_payload(), {"email": "a@b.c"})
    assert ei.value.status_code == 409


def test_every_delivery_backfill_entry_point_is_wired_to_the_gate():
    src = (ROOT / "app/services/yeoljeong_finance_service.py").read_text(encoding="utf-8")
    for fn in ("def queue_delivery_sync(", "def sync_delivery("):
        body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert "_require_delivery_backfill_approval(payload)" in body, fn
    script = (ROOT / "scripts/yeoljeong_auto_collect.py").read_text(encoding="utf-8")
    assert "_require_delivery_backfill_approval(payload)" in script.split("def _global_delivery_queue_items(", 1)[1][:400]
    main_src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "yjf_svc._require_delivery_backfill_approval" in main_src


def test_run_sync_works_inside_a_running_event_loop():
    import asyncio

    async def inner():
        return 7

    async def outer():
        return approval.run_sync(inner())

    assert asyncio.run(outer()) == 7
