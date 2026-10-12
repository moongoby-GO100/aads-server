"""클로브 확정 거래 읽기 전용 조회(list_ledger_entries / GET .../ledger-entries) 계약.

SQL 의 의미(테넌트·사업장 한정, 종류·기간 필터, (종류, source_key) 당 최대 revision, 그룹 합계)를 흉내 내는
메모리 연결을 쓴다. 실제 PostgreSQL 검증은 tests/integration/test_obys_collection_postgres.py 가 맡는다.
"""
from __future__ import annotations

import re
import shutil
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
import asyncio
import httpx

from app.api import obys_collections as api
from app.auth import get_current_user
from app.services import obys_collection_service as svc

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
SECOND_TENANT = "99999999-0000-0000-0000-000000000001"
UNLISTED_TENANT = "88888888-0000-0000-0000-000000000002"
BIZ = "biz-obywai"
TAX, BANK, CARD = "tax_invoice", "bank_transaction", "card_approval"


class Rec(dict):
    __getattr__ = dict.__getitem__


class FakeConn:
    def __init__(self):
        self.entries: list[dict] = []
        self.items: dict[uuid.UUID, dict] = {}
        self.businesses = {(uuid.UUID(TENANT), BIZ)}
        self.sql: list[str] = []
        self.closed = False
        self.in_snapshot = False
        self.transactions = []

    def transaction(self, **options):
        from contextlib import asynccontextmanager
        self.transactions.append(options)

        @asynccontextmanager
        async def snapshot():
            self.in_snapshot = True
            try:
                yield
            finally:
                self.in_snapshot = False
        return snapshot()

    def add(self, kind, key, day, amount, *, direction="SALES", revision=1, tenant=TENANT, business=BIZ,
            counterparty="거래처"):
        item_id = uuid.uuid4()
        self.items[item_id] = {"counterparty": counterparty}
        self.entries.append({
            "entry_id": uuid.uuid4(), "tenant_id": uuid.UUID(tenant), "business_id": business, "data_kind": kind,
            "source_key": key, "item_id": item_id, "occurred_on": day, "amount": Decimal(amount), "direction": direction,
            "revision": revision, "confirmed_by": "owner@x",
            "confirmed_at": datetime(2026, 10, 12, 15, 0, revision, tzinfo=timezone.utc)})

    def _latest(self, sql, args):
        tenant, business, kinds, *dates = args
        best: dict[tuple, dict] = {}
        for e in self.entries:
            if (e["tenant_id"], e["business_id"]) != (tenant, business) or e["data_kind"] not in kinds:
                continue
            key = (e["data_kind"], e["source_key"])
            if key not in best or (e["revision"], e["confirmed_at"]) > (best[key]["revision"], best[key]["confirmed_at"]):
                best[key] = e
        lo = dates.pop(0) if "occurred_on >= $" in sql else None
        hi = dates.pop(0) if "occurred_on <= $" in sql else None
        return [e for e in best.values()
                if (lo is None or (e["occurred_on"] and e["occurred_on"] >= lo))
                and (hi is None or (e["occurred_on"] and e["occurred_on"] <= hi))]

    async def fetchval(self, sql, business, tenant):
        assert "FROM yeoljeong_businesses" in sql
        return (tenant, business) in self.businesses

    async def fetch(self, sql, *args):
        assert self.in_snapshot
        self.sql.append(sql)
        rows = self._latest(sql, args)
        if "GROUP BY data_kind, direction" in sql:
            groups: dict[tuple, list] = {}
            for e in rows:
                groups.setdefault((e["data_kind"], e["direction"]), []).append(e["amount"])
            return [Rec(data_kind=k, direction=d, n=len(v), total=sum(v)) for (k, d), v in sorted(groups.items())]
        rows.sort(key=lambda e: (-e["occurred_on"].toordinal(), e["data_kind"], e["source_key"]))
        lim, off = (int(x) for x in re.search(r"LIMIT (\d+) OFFSET (\d+)", sql).groups())
        return [Rec(**e, counterparty=self.items[e["item_id"]]["counterparty"]) for e in rows[off:off + lim]]

    async def close(self):
        self.closed = True


@pytest.fixture
def conn(monkeypatch):
    fake = FakeConn()

    async def connect():
        return fake

    monkeypatch.setattr(svc, "_connect", connect)
    monkeypatch.setenv("OBYS_CLOBE_COLLECTION_TENANT_IDS", SECOND_TENANT)
    return fake


def seed_obywai(conn):
    conn.add(TAX, "t1", date(2026, 10, 10), "52800.00", direction="SALES", counterparty="고객A")
    conn.add(TAX, "t2", date(2026, 10, 11), "-712800.00", direction="PURCHASE", counterparty="공급사B")


async def test_two_confirmed_rows_sum_to_minus_660000(conn):
    seed_obywai(conn)
    out = await svc.list_ledger_entries(TENANT, BIZ)
    assert out["total"] == 2 and out["summary"]["count"] == 2
    assert Decimal(out["summary"]["amount_total"]) == Decimal("-660000")
    assert {(g["data_kind"], g["direction"]): (g["count"], Decimal(g["amount_total"])) for g in out["summary"]["groups"]} == {
        (TAX, "PURCHASE"): (1, Decimal("-712800")), (TAX, "SALES"): (1, Decimal("52800"))}
    first = out["entries"][0]
    assert first["occurred_on"] == "2026-10-11" and first["counterparty"] == "공급사B"
    assert set(first) == {"occurred_on", "data_kind", "direction", "amount", "counterparty", "source_key", "revision",
                          "confirmed_by", "confirmed_at"}
    assert conn.closed


async def test_only_latest_revision_per_key_counts(conn):
    conn.add(TAX, "t1", date(2026, 9, 1), "100.00", revision=1)
    conn.add(TAX, "t1", date(2026, 9, 2), "500.00", revision=3)
    conn.add(TAX, "t1", date(2026, 9, 3), "300.00", revision=2)
    out = await svc.list_ledger_entries(TENANT, BIZ)
    assert out["total"] == 1 and out["entries"][0]["amount"] == "500.00" and out["entries"][0]["revision"] == 3
    assert out["summary"]["amount_total"] == "500.00"


async def test_period_filter_applies_after_picking_latest_revision(conn):
    conn.add(TAX, "t1", date(2026, 9, 1), "100.00", revision=1)
    conn.add(TAX, "t1", date(2026, 10, 5), "500.00", revision=2)
    out = await svc.list_ledger_entries(TENANT, BIZ, date_from="2026-09-01", date_to="2026-09-30")
    assert out["total"] == 0 and out["entries"] == []
    out = await svc.list_ledger_entries(TENANT, BIZ, date_from=date(2026, 10, 1))
    assert out["total"] == 1 and out["summary"]["amount_total"] == "500.00"


async def test_kinds_filter_and_defaults(conn):
    conn.add(TAX, "t1", date(2026, 9, 1), "100.00")
    conn.add(BANK, "b1", date(2026, 9, 2), "40.00", direction="IN")
    conn.add(CARD, "c1", date(2026, 9, 3), "7.00", direction="OUT")
    assert (await svc.list_ledger_entries(TENANT, BIZ))["total"] == 3
    only = await svc.list_ledger_entries(TENANT, BIZ, kinds=[BANK])
    assert [e["data_kind"] for e in only["entries"]] == [BANK] and only["summary"]["amount_total"] == "40.00"


async def test_other_tenant_rows_never_leak_and_foreign_business_is_404(conn):
    seed_obywai(conn)
    conn.add(TAX, "x1", date(2026, 10, 12), "999.00", tenant=SECOND_TENANT)
    conn.add(TAX, "x2", date(2026, 10, 12), "888.00", business="biz-other")
    out = await svc.list_ledger_entries(TENANT, BIZ)
    assert out["total"] == 2 and out["summary"]["amount_total"] == "-660000.00"
    for tenant, business in ((TENANT, "biz-other"), (SECOND_TENANT, BIZ)):
        with pytest.raises(svc.CollectionError) as exc:
            await svc.list_ledger_entries(tenant, business)
        assert (exc.value.status, exc.value.code) == (404, "business_not_found")


async def test_pagination_keeps_totals_over_the_whole_period(conn):
    for i in range(5):
        conn.add(TAX, f"t{i}", date(2026, 9, i + 1), "10.00")
    page = await svc.list_ledger_entries(TENANT, BIZ, limit=2, offset=2)
    assert len(page["entries"]) == 2 and page["total"] == 5 and page["summary"]["amount_total"] == "50.00"
    assert [e["occurred_on"] for e in page["entries"]] == ["2026-09-03", "2026-09-02"]


async def test_empty_business_returns_zero_summary(conn):
    out = await svc.list_ledger_entries(TENANT, BIZ)
    assert out["entries"] == [] and out["total"] == 0
    assert out["summary"] == {"count": 0, "amount_total": "0", "groups": []}


@pytest.mark.parametrize("kw,code", [
    ({"kinds": ["wetax"]}, "invalid_kinds"),
    ({"date_from": "2026-13-01"}, "invalid_date_from"),
    ({"date_from": "2026-10-02", "date_to": "2026-10-01"}, "invalid_period"),
])
async def test_bad_input_is_rejected_before_any_connection(kw, code, monkeypatch):
    async def boom():
        raise AssertionError("must not connect")

    monkeypatch.setattr(svc, "_connect", boom)
    with pytest.raises(svc.CollectionError) as exc:
        await svc.list_ledger_entries(TENANT, BIZ, **kw)
    assert (exc.value.status, exc.value.code) == (400, code)


async def test_tenant_outside_collection_scope_is_403(monkeypatch):
    async def boom():
        raise AssertionError("must not connect")

    monkeypatch.setattr(svc, "_connect", boom)
    with pytest.raises(svc.CollectionError) as exc:
        await svc.list_ledger_entries(UNLISTED_TENANT, BIZ)
    assert exc.value.status == 403


async def test_service_issues_read_only_statements(conn):
    seed_obywai(conn)
    await svc.list_ledger_entries(TENANT, BIZ)
    assert conn.sql and all(s.startswith("WITH latest") for s in conn.sql)
    assert not any(w in s.upper() for s in conn.sql for w in ("INSERT ", "UPDATE ", "DELETE "))


# ── API ──────────────────────────────────────────────────

def _user(role="viewer", status="active", tenant=TENANT):
    return {"tenant_id": tenant, "email": "u@example.com", "is_internal_admin": False,
            "current_membership": {"tenant_id": tenant, "role": role, "status": status}}


def _client(user):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    async def current_user():
        return user

    app.dependency_overrides[get_current_user] = current_user

    class Client:
        def get(self, path, **kwargs):
            async def request():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://test",
                ) as client:
                    return await client.get(path, **kwargs)
            return asyncio.run(request())

    return Client()


URL = f"/api/v1/obys-collections/businesses/{BIZ}/ledger-entries"


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_every_active_member_role_can_read(role, monkeypatch):
    seen = {}

    async def fake(tenant, business, **kw):
        seen.update(tenant=tenant, business=business, **kw)
        return {"entries": [], "total": 0}

    monkeypatch.setattr(svc, "list_ledger_entries", fake)
    resp = _client(_user(role)).get(URL, params=[("kinds", TAX), ("kinds", BANK), ("date_from", "2026-10-01"),
                                                 ("date_to", "2026-10-31"), ("limit", "50"), ("offset", "10")])
    assert resp.status_code == 200
    assert seen == {"tenant": TENANT, "business": BIZ, "date_from": date(2026, 10, 1), "date_to": date(2026, 10, 31),
                    "kinds": [TAX, BANK], "limit": 50, "offset": 10}


@pytest.mark.parametrize("role,status", [("guest", "active"), ("owner", "suspended"), ("", "active")])
def test_inactive_or_unknown_membership_is_403(role, status, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not reach service")

    monkeypatch.setattr(svc, "list_ledger_entries", boom)
    assert _client(_user(role, status)).get(URL).status_code == 403


def test_membership_of_another_tenant_is_403(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not reach service")

    monkeypatch.setattr(svc, "list_ledger_entries", boom)
    user = _user("owner")
    user["current_membership"]["tenant_id"] = SECOND_TENANT
    assert _client(user).get(URL).status_code == 403


def test_service_errors_map_to_http(monkeypatch):
    async def missing(*a, **k):
        raise svc.CollectionError("business_not_found", 404)

    monkeypatch.setattr(svc, "list_ledger_entries", missing)
    resp = _client(_user()).get(URL)
    assert resp.status_code == 404 and resp.json()["detail"] == "business_not_found"


@pytest.mark.parametrize("query", ["kinds=wetax", "limit=0", "limit=501", "offset=-1", "date_from=nope"])
def test_invalid_query_is_422(query):
    assert _client(_user()).get(f"{URL}?{query}").status_code == 422


def test_end_to_end_through_api_with_fake_db(conn):
    seed_obywai(conn)
    body = _client(_user()).get(URL).json()
    assert body["summary"]["amount_total"] == "-660000.00" and body["total"] == 2
    assert _client(_user(tenant=SECOND_TENANT)).get(URL).status_code == 404
    assert _client(_user(tenant=UNLISTED_TENANT)).get(URL).status_code == 403


# ── 화면(정적) ───────────────────────────────────────────

JS = Path("app/static/apps/obys/modules/ledger-details.js").read_text(encoding="utf-8")
CSS = Path("app/static/apps/obys/modules/ledger-details.css").read_text(encoding="utf-8")


def test_ui_section_is_read_only_and_uses_korean_labels():
    assert "/api/v1/obys-collections" in JS and "/ledger-entries?" in JS
    assert "클로브 확정 거래" in JS
    assert "확정된 거래가 없습니다 — 검토할 거래에서 한 번에 확정을 누르면 여기에 나타납니다" in JS
    assert 'data-view="clobeReview"' in JS
    for label in ("은행거래", "세금계산서", "현금영수증", "카드승인", "입금", "출금", "매출", "매입"):
        assert label in JS
    request_fn = JS.split("async function clobeRequest", 1)[1].split("function clobeErrorText", 1)[0]
    assert "method:" not in request_fn and "body:" not in request_fn
    visible = JS.split("function clobeSectionHtml", 1)[1].split("function renderClobe", 1)[0]
    assert "${escapeHtml(row.data_kind)}" not in visible and "${escapeHtml(g.data_kind)}" not in visible
    assert "${escapeHtml(row.direction)}" not in visible and "${escapeHtml(g.direction)}" not in visible


def test_ui_section_has_mobile_touch_targets_and_wrapping():
    block = CSS.split(".clobe-ledger-panel", 1)[1]
    assert "min-height: 44px" in block and "overflow-wrap: anywhere" in block and "max-width: 640px" in block


async def test_ledger_queries_share_readonly_repeatable_read_snapshot(conn):
    seed_obywai(conn)
    await svc.list_ledger_entries(TENANT, BIZ)
    assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert not conn.in_snapshot


@pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치 런타임")
def test_ui_business_race_rebinding_and_pagination_execute_in_node():
    """Execute the actual module with a small DOM adapter; no static handler assertions."""
    import subprocess

    script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  constructor(attrs = '') {
    this.attrs = attrs; this.handlers = {}; this.dataset = {};
    this.style = {setProperty(){}};
    for (const m of attrs.matchAll(/data-([\w-]+)(?:="([^"]*)")?/g)) {
      this.dataset[m[1].replace(/-([a-z])/g, (_, x) => x.toUpperCase())] = m[2] || '';
    }
    this.value = ''; this.disabled = /\bdisabled\b/.test(attrs);
  }
  set innerHTML(html) {
    this.html = html; this.nodes = [];
    for (const m of html.matchAll(/<(button|select|input)\b([^>]*)>/g)) this.nodes.push(new Element(m[2]));
    const section = html.match(/<section[^>]*data-clobe-section[^>]*>([\s\S]*?)<\/section>/);
    this.section = section ? new Element('data-clobe-section') : null;
    if (this.section) this.section.innerHTML = section[1];
  }
  querySelectorAll(selector) {
    if (selector === '[data-clobe-section]') return this.section ? [this.section] : [];
    const attr = selector.match(/^\[(data-[\w-]+)\]$/)?.[1];
    return attr ? (this.nodes || []).filter(n => n.attrs.includes(attr)) : [];
  }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  emit(type, value) { this.value = value ?? this.value; for (const fn of this.handlers[type] || []) fn({target:this}); }
}
const root = new Element(); root.innerHTML = '';
const pending = [];
const window = {};
const ctx = {window, document:{cookie:'', getElementById:()=>root}, localStorage:{getItem:()=>''},
  URLSearchParams, AbortController, FormData, setTimeout, clearTimeout,
  fetch:(url)=>new Promise(resolve=>pending.push({url, resolve}))};
let source = fs.readFileSync('app/static/apps/obys/modules/ledger-details.js','utf8');
source = source.replace('window.obysLedgerDetails = { open, setBusinesses, closeDrawer };',
  'window.test = {state, render, loadClobe, chooseFile}; window.obysLedgerDetails = { open, setBusinesses, closeDrawer };');
vm.runInNewContext(source, ctx);
const {state, render, loadClobe, chooseFile} = window.test;
const flush = async()=>{for(let i=0;i<12;i++) await Promise.resolve();};
const reply = (p, payload)=>p.resolve({ok:true,json:async()=>payload});
const rows = (n, prefix)=>Array.from({length:n},(_,i)=>({source_key:prefix+i, counterparty:prefix, amount:'1', data_kind:'tax_invoice'}));
(async()=>{
  state.view='salesLedger'; state.businessId='A'; state.businesses=[{id:'A',name:'A'},{id:'B',name:'B'}];
  state.clobe.entries=rows(1,'A'); state.clobe.summary={count:1,amount_total:'1'}; render();
  const oldRequest=loadClobe(); const old=pending[0];
  root.querySelector('[data-ledger-business]').emit('change','B');
  assert.equal(state.clobe.entries.length,0); assert.equal(state.clobe.summary,null);
  await flush();
  const current=pending.find(p=>p.url.includes('/businesses/B/'));
  assert.ok(current, 'B request starts while legacy requests are still pending');
  reply(old,{entries:rows(1,'A'),summary:{count:1,amount_total:'999'},total:1}); await oldRequest;
  assert.equal(state.clobe.entries.length,0);
  reply(current,{entries:rows(100,'B'),summary:{count:101,amount_total:'101'},total:101}); await flush();
  assert.equal(state.clobe.entries[0].counterparty,'B');
  root.querySelector('[data-ledger-source]').emit('change','upload');
  assert.equal(root.section.querySelector('[data-clobe-kind]').handlers.click.length,1);
  chooseFile({name:'book.csv',size:10});
  assert.equal(root.section.querySelector('[data-clobe-kind]').handlers.click.length,1);
  const next=root.section.querySelectorAll('[data-clobe-page]')[1]; next.emit('click');
  assert.ok(pending.at(-1).url.includes('offset=100'));
  reply(pending.at(-1),{entries:rows(1,'last'),summary:{count:101,amount_total:'101'},total:101}); await flush();
  assert.equal(state.clobe.entries[0].counterparty,'last');
  assert.equal(root.section.querySelectorAll('[data-clobe-page]')[1].disabled,true);
  root.section.querySelectorAll('[data-clobe-page]')[0].emit('click');
  assert.ok(pending.at(-1).url.includes('offset=0'));
  reply(pending.at(-1),{entries:rows(100,'B'),summary:{count:101,amount_total:'101'},total:101}); await flush();
  root.section.querySelectorAll('[data-clobe-kind]')[2].emit('click');
  assert.ok(pending.at(-1).url.includes('kinds=tax_invoice'));
  assert.ok(pending.at(-1).url.includes('offset=0'));
  reply(pending.at(-1),{}); await flush();
  state.clobe.error={status:500}; render();
  assert.equal(root.section.querySelector('[data-clobe-retry]').handlers.click.length,1);
  root.section.querySelector('[data-clobe-retry]').emit('click');
  reply(pending.at(-1),{}); await flush();
  console.log('business race, re-render handlers, retry, 101-row pagination PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
