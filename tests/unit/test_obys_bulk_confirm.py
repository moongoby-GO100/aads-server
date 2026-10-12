"""'한 번에 확정'(bulk_review_confirm) 계약: dry_run 무쓰기, 기간·종류 필터, 단계 불변, 멱등, limit/remaining.

운영 DB 대신 SQL 의 의미를 그대로 흉내 내는 메모리 연결을 쓴다. 실제 PostgreSQL 검증은
tests/integration/test_obys_collection_postgres.py 의 bulk 케이스가 맡는다.
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import obys_collections as api
from app.auth import get_current_user
from app.services import obys_collection_service as svc

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
BIZ = "biz-danharu"
BANK, TAX = "bank_transaction", "tax_invoice"


class Rec(dict):
    __getattr__ = dict.__getitem__


class FakeConn:
    def __init__(self):
        self.items: dict[uuid.UUID, dict] = {}
        self.ledger: dict[tuple, dict] = {}
        self.writes = 0
        self.closed = False

    def add(self, kind, key, day, amount, stage="review_box", direction="IN", item_hash=None, business=BIZ):
        item_id = uuid.uuid4()
        self.items[item_id] = {
            "item_id": item_id, "tenant_id": uuid.UUID(TENANT), "business_id": business, "data_kind": kind,
            "source_key": key, "item_hash": item_hash or f"h-{key}", "occurred_on": day, "amount": Decimal(amount),
            "direction": direction, "stage": stage, "reviewed_by": None, "confirmed_by": None}
        return item_id

    def stages(self):
        out: dict[str, int] = {}
        for r in self.items.values():
            out[r["stage"]] = out.get(r["stage"], 0) + 1
        return out

    # ── asyncpg 표면 ──
    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                self.snap = copy.deepcopy((conn.items, conn.ledger))
                return self

            async def __aexit__(self, exc_type, *_):
                if exc_type:
                    conn.items, conn.ledger = self.snap
                return False

        return Tx()

    async def close(self):
        self.closed = True

    def _match(self, sql, args):
        """review_box 조건절($3 종류, 선택적 날짜 >=/<=)을 SQL 문자열대로 해석한다."""
        assert "stage='review_box'" in sql
        kinds = args[2]
        lo = args[int(m.group(1)) - 1] if (m := re.search(r"occurred_on >= \$(\d+)", sql)) else None
        hi = args[int(m.group(1)) - 1] if (m := re.search(r"occurred_on <= \$(\d+)", sql)) else None
        out = []
        for r in self.items.values():
            if (r["tenant_id"], r["business_id"]) != (args[0], args[1]) or r["stage"] != "review_box" or r["data_kind"] not in kinds:
                continue
            if lo is not None and (r["occurred_on"] is None or r["occurred_on"] < lo):
                continue
            if hi is not None and (r["occurred_on"] is None or r["occurred_on"] > hi):
                continue
            out.append(r)
        return out

    async def fetchval(self, sql, *args):
        if "FROM yeoljeong_businesses" in sql:
            return True
        if sql.startswith("SELECT count(*) FROM obys_clobe_item"):
            return len(self._match(sql, args))
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "GROUP BY data_kind, direction" in sql:
            groups: dict[tuple, list] = {}
            for r in self._match(sql, args):
                groups.setdefault((r["data_kind"], r["direction"]), []).append(r["amount"])
            return [Rec(data_kind=k, direction=d, n=len(v), total=sum(v)) for (k, d), v in sorted(groups.items())]
        if "FOR UPDATE SKIP LOCKED" in sql:
            rows = sorted(self._match(sql, args), key=lambda r: (r["data_kind"], r["source_key"], str(r["item_id"])))
            return [Rec(item_id=r["item_id"]) for r in rows[:int(re.search(r"LIMIT (\d+)", sql).group(1))]]
        if sql.startswith("UPDATE obys_clobe_item SET stage='reviewed'"):
            tenant, ids, actor = args
            done = []
            for item_id in ids:
                r = self.items[item_id]
                if r["tenant_id"] == tenant and r["stage"] == "review_box":
                    r.update(stage="reviewed", reviewed_by=actor)
                    self.writes += 1
                    done.append(Rec(r))
            return done
        if sql.startswith("SELECT * FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 AND item_id = ANY"):
            return [Rec(r) for r in self.items.values() if r["item_id"] in args[2] and r["business_id"] == args[1]]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        assert "FROM obys_clobe_ledger_entry" in sql
        e = self.ledger.get((args[0], args[1], args[2], args[3]))
        return Rec(e) if e else None

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return
        self.writes += 1
        if sql.startswith("INSERT INTO obys_clobe_ledger_entry"):
            _, tenant, biz, kind, key, item_id, item_hash, day, amount, direction, actor = args
            self.ledger.setdefault((tenant, biz, kind, key), {
                "entry_id": args[0], "item_id": item_id, "item_hash": item_hash, "revision": 1, "amount": amount})
        elif sql.startswith("UPDATE obys_clobe_item SET stage='superseded'"):
            if self.items[args[0]]["stage"] == "confirmed":
                self.items[args[0]]["stage"] = "superseded"
        elif sql.startswith("UPDATE obys_clobe_ledger_entry"):
            entry = next(e for e in self.ledger.values() if e["entry_id"] == args[0])
            entry.update(item_id=args[1], item_hash=args[2], amount=args[4], revision=entry["revision"] + 1)
        elif sql.startswith("UPDATE obys_clobe_item SET stage='confirmed'"):
            self.items[args[0]].update(stage="confirmed", confirmed_by=args[1])
        else:
            raise AssertionError(sql)


@pytest.fixture
def conn(monkeypatch):
    fake = FakeConn()

    async def connect():
        return fake

    monkeypatch.setattr(svc, "_connect", connect)
    return fake


def run(conn_, **kw):
    kw.setdefault("kinds", [BANK, TAX])
    kw.setdefault("actor", "owner@x")
    return svc.bulk_review_confirm(TENANT, BIZ, **kw)


async def test_dry_run_writes_nothing_and_reports_kind_totals(conn):
    conn.add(BANK, "b1", date(2026, 9, 1), "1000", direction="IN")
    conn.add(BANK, "b2", date(2026, 9, 2), "300", direction="OUT")
    conn.add(TAX, "t1", date(2026, 9, 3), "110", direction="SALES")
    before = copy.deepcopy(conn.items)
    out = await run(conn, dry_run=True)
    assert conn.writes == 0 and conn.items == before and conn.ledger == {}
    assert out["dry_run"] is True and out["total"] == 3 and out["remaining"] == 0
    assert out["kinds"][BANK]["count"] == 2 and out["kinds"][BANK]["amount_total"] == "1300"
    assert out["kinds"][BANK]["by_direction"]["OUT"] == {"count": 1, "amount_total": "300"}
    assert out["kinds"][TAX]["count"] == 1


async def test_period_and_kind_filters_limit_the_scope(conn):
    inside = conn.add(BANK, "b-in", date(2026, 9, 10), "1")
    conn.add(BANK, "b-before", date(2026, 8, 31), "1")
    conn.add(BANK, "b-after", date(2026, 10, 1), "1")
    conn.add(BANK, "b-nodate", None, "1")
    conn.add(TAX, "t-in", date(2026, 9, 10), "1")
    conn.add(BANK, "b-other-biz", date(2026, 9, 10), "1", business="biz-other")
    preview = await run(conn, kinds=[BANK], date_from=date(2026, 9, 1), date_to="2026-09-30", dry_run=True)
    assert preview["total"] == 1 and list(preview["kinds"]) == [BANK]
    out = await run(conn, kinds=[BANK], date_from="2026-09-01", date_to=date(2026, 9, 30))
    assert out["processed"] == 1 and out["confirmed"] == 1 and out["by_kind"] == {BANK: 1} and out["remaining"] == 0
    assert conn.items[inside]["stage"] == "confirmed"
    assert conn.stages() == {"confirmed": 1, "review_box": 5}
    assert len(conn.ledger) == 1


async def test_reviewed_rejected_confirmed_and_superseded_are_untouched(conn):
    keep = {s: conn.add(BANK, f"k-{s}", date(2026, 9, 1), "5", stage=s) for s in ("reviewed", "rejected", "confirmed", "superseded")}
    todo = conn.add(BANK, "todo", date(2026, 9, 1), "5")
    out = await run(conn)
    assert out["processed"] == 1
    assert conn.items[todo]["stage"] == "confirmed" and conn.items[todo]["reviewed_by"] == "owner@x"
    for stage, item_id in keep.items():
        assert conn.items[item_id]["stage"] == stage
        assert conn.items[item_id]["reviewed_by"] is None
    assert len(conn.ledger) == 1


async def test_running_twice_does_not_add_ledger_rows(conn):
    for i in range(3):
        conn.add(BANK, f"b{i}", date(2026, 9, 1 + i), "10")
    first = await run(conn)
    ledger_after_first = dict(conn.ledger)
    second = await run(conn)
    assert first["confirmed"] == 3 and second == {"dry_run": False, "processed": 0, "confirmed": 0, "by_kind": {}, "remaining": 0}
    assert conn.ledger == ledger_after_first and len(conn.ledger) == 3
    assert (await run(conn, dry_run=True))["total"] == 0


async def test_limit_leaves_remaining_and_continues_to_completion(conn):
    for i in range(5):
        conn.add(BANK, f"b{i}", date(2026, 9, 1), "10")
    preview = await run(conn, dry_run=True, limit=2)
    assert preview["total"] == 5 and preview["remaining"] == 3 and preview["batch_limit"] == 2
    first = await run(conn, limit=2)
    assert (first["processed"], first["remaining"]) == (2, 3) and len(conn.ledger) == 2
    second = await run(conn, limit=2)
    assert (second["processed"], second["remaining"]) == (2, 1)
    third = await run(conn, limit=2)
    assert (third["processed"], third["remaining"]) == (1, 0)
    assert len(conn.ledger) == 5 and conn.stages() == {"confirmed": 5}


async def test_revision_of_confirmed_entry_reuses_supersede_rule(conn):
    old = conn.add(TAX, "t1", date(2026, 9, 1), "100", stage="confirmed", item_hash="h-old")
    conn.ledger[(uuid.UUID(TENANT), BIZ, TAX, "t1")] = {
        "entry_id": uuid.uuid4(), "item_id": old, "item_hash": "h-old", "revision": 1, "amount": Decimal("100")}
    new = conn.add(TAX, "t1", date(2026, 9, 1), "150", item_hash="h-new")
    await run(conn, kinds=[TAX])
    entry = conn.ledger[(uuid.UUID(TENANT), BIZ, TAX, "t1")]
    assert entry["revision"] == 2 and entry["item_id"] == new and entry["amount"] == Decimal("150")
    assert conn.items[old]["stage"] == "superseded" and conn.items[new]["stage"] == "confirmed"
    assert len(conn.ledger) == 1


async def test_failure_midway_rolls_everything_back(conn, monkeypatch):
    for i in range(3):
        conn.add(BANK, f"b{i}", date(2026, 9, 1), "10")
    real = conn.execute
    calls = {"n": 0}

    async def flaky(sql, *args):
        if sql.startswith("INSERT INTO obys_clobe_ledger_entry"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
        return await real(sql, *args)

    monkeypatch.setattr(conn, "execute", flaky)
    with pytest.raises(RuntimeError):
        await run(conn)
    assert conn.stages() == {"review_box": 3} and conn.ledger == {}
    assert conn.closed


@pytest.mark.parametrize("kw,code", [
    ({"kinds": []}, "invalid_kinds"), ({"kinds": ["journal_post"]}, "invalid_kinds"),
    ({"date_from": "2026-13-01"}, "invalid_date_from"), ({"date_to": "x"}, "invalid_date_to"),
    ({"date_from": "2026-09-30", "date_to": "2026-09-01"}, "invalid_period"),
])
async def test_bad_input_is_rejected_before_any_connection(kw, code, monkeypatch):
    async def boom():
        raise AssertionError("must not connect")

    monkeypatch.setattr(svc, "_connect", boom)
    with pytest.raises(svc.CollectionError) as exc:
        await run(None, **kw)
    assert exc.value.code == code and exc.value.status == 400


async def test_out_of_scope_tenant_is_refused(conn):
    with pytest.raises(svc.CollectionError) as exc:
        await svc.bulk_review_confirm("99999999-0000-0000-0000-000000000001", BIZ, kinds=[BANK], actor="a")
    assert exc.value.status == 403


async def test_existing_confirm_items_still_works_through_shared_posting(conn):
    reviewed = conn.add(BANK, "r1", date(2026, 9, 1), "7", stage="reviewed")
    waiting = conn.add(BANK, "w1", date(2026, 9, 1), "7")
    out = await svc.confirm_items(TENANT, BIZ, [str(reviewed), str(waiting), str(uuid.uuid4())], actor="owner@x")
    assert out == {"confirmed": 1, "skipped": 2}
    assert conn.items[reviewed]["stage"] == "confirmed" and conn.items[waiting]["stage"] == "review_box"
    again = await svc.confirm_items(TENANT, BIZ, [str(reviewed)], actor="owner@x")
    assert again == {"confirmed": 0, "skipped": 1} and len(conn.ledger) == 1


# ── API ──────────────────────────────────────────────────

def _client(role="owner", status="active"):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    user = {"tenant_id": TENANT, "email": "u@example.com",
            "current_membership": {"tenant_id": TENANT, "role": role, "status": status}}
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


URL = f"/api/v1/obys-collections/businesses/{BIZ}/items/bulk-confirm"


@pytest.mark.parametrize("role,status", [("member", "active"), ("viewer", "active"), ("owner", "invited")])
def test_bulk_confirm_requires_active_owner_or_admin(role, status, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(svc, "bulk_review_confirm", boom)
    assert _client(role, status).post(URL, json={"kinds": [BANK]}).status_code == 403


def test_bulk_confirm_passes_body_to_service_and_maps_errors(monkeypatch):
    seen = {}

    async def fake(tenant, business, **kw):
        seen.update(tenant=tenant, business=business, **kw)
        return {"dry_run": kw["dry_run"], "processed": 0, "confirmed": 0, "by_kind": {}, "remaining": 0}

    monkeypatch.setattr(svc, "bulk_review_confirm", fake)
    resp = _client("admin").post(URL, json={"kinds": [BANK], "date_from": "2026-09-01", "date_to": "2026-09-30", "dry_run": True})
    assert resp.status_code == 200 and resp.json()["dry_run"] is True
    assert seen["kinds"] == [BANK] and seen["date_from"] == date(2026, 9, 1) and seen["date_to"] == date(2026, 9, 30)
    assert seen["dry_run"] is True and seen["actor"] == "u@example.com" and seen["business"] == BIZ

    async def denied(*a, **k):
        raise svc.CollectionError("business_not_found", 404)

    monkeypatch.setattr(svc, "bulk_review_confirm", denied)
    assert _client().post(URL, json={"kinds": [BANK]}).status_code == 404


@pytest.mark.parametrize("body", [{}, {"kinds": []}, {"kinds": ["journal_post"]}, {"kinds": [BANK], "date_from": "nope"}])
def test_bulk_confirm_rejects_bad_body(body):
    assert _client().post(URL, json=body).status_code == 422


# ── 화면 배선(정적) ──────────────────────────────────────

def _static(name):
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "app/static/apps/obys" / name).read_text(encoding="utf-8")


def test_ui_wires_preview_confirm_dialog_and_continuation():
    js, css, html = _static("modules/clobe-collection.js"), _static("modules/clobe-collection.css"), _static("index.html")
    assert "/items/bulk-confirm" in js and "dry_run: true" in js and "dry_run: false" in js
    for action in ("bulk-preview", "bulk-ask", "bulk-go", "bulk-cancel"):
        assert f'"{action}"' in js or f'data-clobe-action="{action}"' in js
    assert 'role="alertdialog" aria-label="한 번에 확정 확인"' in js
    assert "확정 예정" in js and "되돌릴 수 없습니다" in js
    assert "remaining > 0" in js and "BULK_MAX_ROUNDS" in js
    assert 'can("manage")' in js.split("function bulkHtml()", 1)[1].split("function renderBulk", 1)[0]
    # 완료 후 장부(경영요약) 갱신 경로: 요약 캐시 무효화 + 항목·상태 재조회
    run = js.split("async function bulkRun()", 1)[1].split("function pendingHtml", 1)[0]
    assert 'state.summaryKey = ""' in run and "loadItems(true)" in run and "refreshStatusQuiet()" in run
    # 375px: 줄바꿈되어도 눌리는 버튼
    assert "max-width: 480px" in css and ".clobe-bulk-actions button" in css and "flex-wrap: wrap" in css.split(".clobe-bulk-form", 1)[1]
    assert "modules/clobe-collection.js?v=20261012-r1" in html and "modules/clobe-collection.css?v=20261012-r1" in html


def test_ui_copy_uses_business_terms_not_internal_names():
    js = _static("modules/clobe-collection.js")
    body = js.split("// ── 한 번에 확정", 1)[1].split("function pendingHtml", 1)[0]
    for internal in ("review_box", "item_id", "ledger_entry", "stage="):
        assert internal not in body.replace("stageCounts(company).review_box", "")
