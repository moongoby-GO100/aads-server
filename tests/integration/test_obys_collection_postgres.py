"""오비서 클로브 수집 — 격리 PostgreSQL 에서 마이그레이션·수집·검토·확정 계약을 검증한다.

OBYS_COLLECTION_TEST_DB_URL 이 가리키는 DB 는 테스트가 스키마를 만들고 지운다(운영 DB 금지).
클로브는 가짜 세션으로 대체한다 — 실제 호출 증거는 별도의 읽기 전용 실측으로 남긴다.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_core as core
from app.services import obys_collection_service as svc

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
LYLON = "d1695f15-6b68-4929-bc8d-646827363ff9"
OTHER_TENANT = "99999999-0000-0000-0000-000000000001"
C_DANHARU, C_EONNI, C_YEOLJEONG, C_LYLON, C_NEW = "cid-danharu", "cid-eonni", "cid-yeoljeong", "cid-lylon", "cid-new"
PERIOD = {"period_start": "2026-09-01", "period_end": "2026-09-30"}
MIG = Path("migrations/20261003_obys_clobe_collection.sql")
DOWN = Path("migrations/rollback/20261003_obys_clobe_collection.down.sql")
TABLES = ("obys_clobe_company_link", "obys_clobe_collection_lease", "obys_clobe_collection_run", "obys_clobe_item",
          "obys_clobe_ledger_entry", "obys_clobe_collection_state")


def _url() -> str:
    value = os.getenv("OBYS_COLLECTION_TEST_DB_URL", "")
    if not value:
        pytest.skip("OBYS_COLLECTION_TEST_DB_URL is required (isolated PostgreSQL only)")
    return value


# ── 가짜 클로브 ──────────────────────────────────────────

def make_items(kind: str, n: int, prefix: str = "x") -> list[dict]:
    out = []
    for i in range(n):
        day = f"2026-09-{(i % 27) + 1:02d}"
        if kind == core.KIND_BANK:
            out.append({"transactionId": f"{prefix}{i}", "accountId": 1, "transactionAt": f"{day}T10:00:00",
                        "transactionName": f"거래{i}", "inAmount": 1000 + i, "outAmount": 0, "bankName": "신한"})
        elif kind == core.KIND_TAX:
            out.append({"id": f"{prefix}{i}", "type": "SALES", "issueDate": day, "supplyValue": 100 + i, "taxAmount": 10,
                        "totalAmount": 110 + i, "contractorCompanyName": f"고객{i}", "settlementStatus": "OPEN"})
        elif kind == core.KIND_CASH:
            out.append({"id": f"{prefix}{i}", "type": "PURCHASE", "usedDateTime": f"{day}T09:00:00", "totalAmount": 55,
                        "supplyAmount": 50, "vatAmount": 5, "counterpartName": f"상점{i}"})
        else:
            out.append({"id": f"{prefix}{i}", "usedAt": f"{day}T12:00:00", "usedAmount": 2200 + i, "vatAmount": 200,
                        "merchantName": f"가맹{i}", "cardCompanyName": "국민"})
    return out


class FakeClobe:
    def __init__(self, companies, data, assets=None):
        self.companies, self.data, self.assets = companies, data, assets or {}
        self.calls: list[tuple[str, dict]] = []
        self.hook = None          # async (n, tool, args) -> None, 호출 직전에 실행
        self.drop_last_on_page: dict[str, int] = {}   # kind -> page: 그 페이지의 마지막 항목을 조용히 뺀다
        self.denied: set[str] = set()

    def __call__(self):
        return _Session(self)


class _Session:
    def __init__(self, fake):
        self.fake = fake

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, tool, args=None):
        args = args or {}
        f = self.fake
        if tool not in clobe.COLLECTION_TOOLS:
            raise clobe.ClobeToolDenied(f"tool_denied:{tool}")
        f.calls.append((tool, args))
        if f.hook:
            await f.hook(len(f.calls), tool, args)
        if tool == "get_my_context":
            return {"companies": f.companies}
        cid = args["companyId"]
        if cid in f.denied:
            raise clobe.ClobeError("forbidden")
        if tool == "get_scraping_status":
            return {"assets": f.assets.get(cid, [])}
        kind = next(k for k, t in clobe.COLLECTION_DATA_TOOLS.items() if t == tool)
        items = f.data.get(cid, {}).get(kind, [])
        if kind == core.KIND_CASH:
            items = [i for i in items if i["type"] == args["type"]]
        size = args["size"]
        if kind == core.KIND_BANK:
            off = int(args.get("cursor") or 0)
            chunk = items[off:off + size]
            has_next = off + size < len(items)
            return {"content": chunk, "totalElements": len(items), "hasNext": has_next,
                    "nextCursor": str(off + size) if has_next else None}
        page = args["page"]
        chunk = list(items[page * size:(page + 1) * size])
        if f.drop_last_on_page.get(kind) == page and chunk:
            chunk.pop()
        meta = {"totalElements": len(items), "hasNext": (page + 1) * size < len(items)}
        if kind == core.KIND_TAX:
            meta.update(supplyValueSum=sum(i["supplyValue"] for i in items), taxAmountSum=sum(i["taxAmount"] for i in items),
                        totalAmountSum=sum(i["totalAmount"] for i in items))
        elif kind == core.KIND_CASH:
            sg = [-1 if "취소" in i.get("transactionType", "") else 1 for i in items]   # 실측: 합계는 취소분을 뺀 순액
            meta.update(totalAmountSum=sum(s * i["totalAmount"] for s, i in zip(sg, items)),
                        supplyAmountSum=sum(s * i["supplyAmount"] for s, i in zip(sg, items)),
                        vatAmountSum=sum(s * i["vatAmount"] for s, i in zip(sg, items)))
        else:
            meta.update(totalUsedAmountSum=sum(i["usedAmount"] for i in items), totalVatAmountSum=sum(i["vatAmount"] for i in items))
        return {"content": chunk, **meta}


def company(cid, name, reg):
    return {"companyId": cid, "companyName": name, "businessRegNo": reg, "role": "OWNER"}


CONTEXT = [
    company(C_DANHARU, "단하루", "123-45-67890"),
    company(C_EONNI, "언니냉면", "234-56-78901"),
    company(C_YEOLJEONG, "열정국밥", "999-99-99999"),
    company(C_LYLON, "주식회사 라일론", "456-78-90123"),
]


# ── 픽스처 ───────────────────────────────────────────────

@pytest.fixture
async def db(monkeypatch):
    url = _url()
    monkeypatch.setattr(svc, "_connect", lambda: asyncpg.connect(url))
    monkeypatch.setattr(core, "PAGE_SIZE", 3)
    admin = await asyncpg.connect(url)
    await admin.execute("DROP TABLE IF EXISTS " + ",".join(reversed(TABLES)) + ", yeoljeong_businesses CASCADE")
    await admin.execute(
        "CREATE TABLE yeoljeong_businesses (id text PRIMARY KEY, tenant_id uuid NOT NULL, name text NOT NULL, "
        "registration_no text NOT NULL DEFAULT '', deleted_at timestamptz)")
    for biz in (
        ("biz-danharu", TENANT, "단하루", "123-45-67890"),
        ("biz-eonni", TENANT, "언니냉면", "기초등록 필요"),
        ("biz-junghwa", TENANT, "열정국밥 중화점", "345-67-89012"),
        ("biz-lylon-e2e", LYLON, "주식회사 라일론", "456-78-90123"),
        ("biz-other", OTHER_TENANT, "타테넌트", "111-11-11111"),
    ):
        await admin.execute("INSERT INTO yeoljeong_businesses (id,tenant_id,name,registration_no) VALUES ($1,$2,$3,$4)", *biz)
    await admin.execute(MIG.read_text(encoding="utf-8"))
    yield admin
    await admin.execute("DROP TABLE IF EXISTS " + ",".join(reversed(TABLES)) + ", yeoljeong_businesses CASCADE")
    await admin.close()


def fake_for(*cids, counts=None):
    counts = counts or {core.KIND_BANK: 7, core.KIND_TAX: 5, core.KIND_CASH: 4, core.KIND_CARD: 3}
    data = {cid: {k: make_items(k, n, prefix=f"{cid}-") for k, n in counts.items()} for cid in cids}
    return FakeClobe(CONTEXT, data)


async def linked(db, fake):
    summary = await svc.discover_companies(session_factory=fake)
    await svc.approve_link(C_EONNI, "biz-eonni", actor="admin@x", name_evidence=True)
    return summary


async def n(db, table, where=""):
    return await db.fetchval(f"SELECT count(*) FROM {table} {where}")


# ── 마이그레이션 ─────────────────────────────────────────

async def test_migration_is_idempotent_and_rollback_is_clean(db):
    sql = MIG.read_text(encoding="utf-8")
    await db.execute(sql)
    await db.execute(sql)
    for t in TABLES:
        assert await db.fetchval("SELECT to_regclass($1)", t) is not None
    await db.execute(DOWN.read_text(encoding="utf-8"))
    await db.execute(DOWN.read_text(encoding="utf-8"))
    for t in TABLES:
        assert await db.fetchval("SELECT to_regclass($1)", t) is None
    assert await n(db, "yeoljeong_businesses") == 5   # 기존 테이블은 건드리지 않는다
    await db.execute(sql)
    assert await db.fetchval("SELECT to_regclass('obys_clobe_item')") is not None


# ── 회사 발견 · 연결 ─────────────────────────────────────

async def test_discover_maps_exact_companies_and_never_auto_equates_yeoljeong(db):
    fake = fake_for()
    s = await svc.discover_companies(session_factory=fake)
    assert (s["seen"], s["new"], s["linked"], s["review"], s["blocked"]) == (4, 4, 1, 2, 1)
    rows = {r["clobe_company_id"]: r for r in await db.fetch("SELECT * FROM obys_clobe_company_link")}
    assert (rows[C_DANHARU]["link_status"], rows[C_DANHARU]["business_id"]) == ("linked", "biz-danharu")
    assert rows[C_EONNI]["link_status"] == "review" and rows[C_EONNI]["candidate_business_ids"] == ["biz-eonni"]
    assert rows[C_YEOLJEONG]["link_status"] == "review" and rows[C_YEOLJEONG]["business_id"] is None
    assert rows[C_LYLON]["link_status"] == "blocked" and rows[C_LYLON]["tenant_id"] is None
    again = await svc.discover_companies(session_factory=fake)
    assert again["new"] == 0 and await n(db, "obys_clobe_company_link") == 4


async def test_company_added_later_flows_through_review_without_code_change(db):
    fake = fake_for()
    await svc.discover_companies(session_factory=fake)
    fake.companies = CONTEXT + [company(C_NEW, "새가게", "777-77-77777")]
    s = await svc.discover_companies(session_factory=fake)
    assert s["new"] == 1
    row = await db.fetchrow("SELECT * FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_NEW)
    assert row["link_status"] == "review"
    with pytest.raises(svc.CollectionError) as e:
        await svc.run_collection(C_NEW, session_factory=fake, **PERIOD)
    assert e.value.code == "company_not_linked"
    assert await db.fetchval("SELECT count(*) FROM obys_clobe_company_link WHERE permission_error='absent_from_context'") == 0
    fake.companies = CONTEXT
    s = await svc.discover_companies(session_factory=fake)
    assert s["absent"] == 0 and s["missing"] == 1   # 한 번 빠진 것만으로는 absent 가 아니다
    s = await svc.discover_companies(session_factory=fake)
    assert s["absent"] == 1


async def test_empty_context_never_marks_every_company_absent(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    fake.companies = []
    s = await svc.discover_companies(session_factory=fake)
    assert s["absent"] == 0 and s["absent_skipped"] == "empty_context"
    assert await db.fetchval("SELECT count(*) FROM obys_clobe_company_link WHERE permission_error='absent_from_context'") == 0
    assert await db.fetchval("SELECT count(*) FROM obys_clobe_company_link WHERE permission_ok IS FALSE") == 0


@pytest.mark.parametrize("path", ["linked", "review"])
async def test_company_reappearing_in_context_clears_absent_mark(db, path):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    if path == "review":
        # review 상태(kept 가 아닌 UPSERT 경로)도 같은 규칙을 따라야 한다
        await db.execute("UPDATE obys_clobe_company_link SET link_status='review', link_basis='name_only', "
                         "tenant_id=NULL, business_id=NULL WHERE clobe_company_id=$1", C_DANHARU)
    fake.companies = [company(C_NEW, "새가게", "777-77-77777")]
    await svc.discover_companies(session_factory=fake)
    s = await svc.discover_companies(session_factory=fake)
    assert s["absent"] >= 1
    row = await db.fetchrow("SELECT permission_ok, permission_error FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU)
    assert (row["permission_ok"], row["permission_error"]) == (False, "absent_from_context")
    fake.companies = CONTEXT
    await svc.discover_companies(session_factory=fake)
    row = await db.fetchrow("SELECT permission_ok, permission_error FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU)
    assert (row["permission_ok"], row["permission_error"]) == (None, None)


async def test_partial_context_response_does_not_flag_healthy_linked_company(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    fake.companies = [company(C_NEW, "새가게", "777-77-77777")]   # 일시적으로 일부만 돌려준 응답
    s = await svc.discover_companies(session_factory=fake)
    assert s["absent"] == 0
    row = await db.fetchrow("SELECT permission_ok, permission_error, missing_streak, link_status "
                            "FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU)
    assert (row["permission_ok"], row["permission_error"], row["missing_streak"], row["link_status"]) == (None, None, 1, "linked")
    fake.companies = CONTEXT                                    # 다음 응답에서 돌아오면 연속 횟수가 리셋된다
    await svc.discover_companies(session_factory=fake)
    fake.companies = [company(C_NEW, "새가게", "777-77-77777")]
    s = await svc.discover_companies(session_factory=fake)
    assert s["absent"] == 0
    assert await db.fetchval("SELECT missing_streak FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU) == 1


async def test_absent_never_overwrites_a_real_permission_error(db):
    fake = fake_for(C_DANHARU)
    fake.denied = {C_DANHARU}
    await svc.discover_companies(session_factory=fake)
    await svc.check_company_permission(C_DANHARU, session_factory=fake)
    fake.companies = [company(C_NEW, "새가게", "777-77-77777")]
    for _ in range(svc.ABSENT_AFTER_MISSES + 1):
        await svc.discover_companies(session_factory=fake)
    row = await db.fetchrow("SELECT permission_ok, permission_error FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU)
    assert row["permission_ok"] is False and row["permission_error"].startswith("permission_denied")


@pytest.mark.parametrize("basis", ["excluded_scope", "admin_blocked", "something_else"])
async def test_blocked_row_stays_blocked_whatever_the_basis(db, basis):
    fake = fake_for()
    await svc.discover_companies(session_factory=fake)
    # 라일론이 아닌 회사(열정국밥: 재판정하면 review)를 어떤 사유로든 blocked 로 만든 뒤 다시 발견한다
    await db.execute("UPDATE obys_clobe_company_link SET link_status='blocked', link_basis=$2 WHERE clobe_company_id=$1",
                     C_YEOLJEONG, basis)
    s = await svc.discover_companies(session_factory=fake)
    row = await db.fetchrow("SELECT link_status, link_basis, tenant_id, business_id FROM obys_clobe_company_link "
                            "WHERE clobe_company_id=$1", C_YEOLJEONG)
    assert (row["link_status"], row["link_basis"]) == ("blocked", basis)
    assert row["tenant_id"] is None and row["business_id"] is None
    assert s["blocked"] == 2


async def test_admin_block_and_real_permission_error_are_not_cleared_by_rediscovery(db):
    fake = fake_for(C_DANHARU)
    fake.denied = {C_DANHARU}
    await svc.discover_companies(session_factory=fake)
    await svc.check_company_permission(C_DANHARU, session_factory=fake)
    await svc.discover_companies(session_factory=fake)
    row = await db.fetchrow("SELECT permission_ok, permission_error FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU)
    assert row["permission_ok"] is False and row["permission_error"].startswith("permission_denied")


async def test_internal_admin_cannot_start_run_for_unlinked_or_blocked_company(db):
    fake = fake_for(C_DANHARU, C_EONNI)
    await svc.discover_companies(session_factory=fake)
    for cid, code in ((C_EONNI, "company_not_linked"), ("cid-unknown", "company_not_discovered")):
        with pytest.raises(svc.CollectionError) as e:
            await svc.require_runnable_company(cid)
        assert e.value.code == code
    await svc.require_runnable_company(C_DANHARU)
    await svc.block_company(C_DANHARU, actor="admin@x")
    with pytest.raises(svc.CollectionError):
        await svc.require_runnable_company(C_DANHARU)
    fake.calls.clear()
    with pytest.raises(svc.CollectionError):
        await svc.run_collection(C_DANHARU, session_factory=fake, **PERIOD)
    assert not fake.calls


async def test_permission_check_records_denied_without_changing_link(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    ok = await svc.check_company_permission(C_DANHARU, session_factory=fake)
    assert ok["permission_ok"] is True
    fake.denied.add(C_DANHARU)
    bad = await svc.check_company_permission(C_DANHARU, session_factory=fake)
    assert bad["permission_ok"] is False and bad["error"].startswith("permission_denied")
    assert await db.fetchval("SELECT link_status FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU) == "linked"
    with pytest.raises(svc.CollectionError) as e:
        await svc.check_company_permission(C_LYLON, session_factory=fake)
    assert e.value.code == "company_blocked"


async def test_approval_rules(db):
    fake = fake_for()
    await svc.discover_companies(session_factory=fake)
    for company_id, biz, evidence, code in (
        (C_EONNI, "biz-eonni", False, "name_evidence_required"),
        (C_EONNI, "biz-danharu", True, "reg_no_conflict"),
        (C_EONNI, "biz-lylon-e2e", True, "business_blocked"),
        (C_EONNI, "biz-other", True, "tenant_not_in_collection_scope"),
        (C_EONNI, "biz-missing", True, "business_not_found"),
        (C_LYLON, "biz-eonni", True, "company_blocked"),
        ("cid-unknown", "biz-eonni", True, "company_not_discovered"),
    ):
        with pytest.raises(svc.CollectionError) as e:
            await svc.approve_link(company_id, biz, actor="admin@x", name_evidence=evidence)
        assert e.value.code == code, (company_id, biz)
    view = await svc.approve_link(C_EONNI, "biz-eonni", actor="admin@x", name_evidence=True)
    assert view["link_status"] == "linked" and view["approved_by"] == "admin@x"
    assert "reg_no" not in view
    with pytest.raises(svc.CollectionError) as e:   # 같은 사업자에 두 회사를 못 붙인다
        await svc.approve_link(C_YEOLJEONG, "biz-eonni", actor="admin@x", name_evidence=True)
    assert e.value.code in ("business_already_linked", "reg_no_conflict", "name_mismatch")


async def test_admin_block_survives_rediscovery_and_refuses_runs(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    await svc.block_company(C_DANHARU, actor="admin@x")
    await svc.discover_companies(session_factory=fake)
    assert await db.fetchval("SELECT link_status FROM obys_clobe_company_link WHERE clobe_company_id=$1", C_DANHARU) == "blocked"
    with pytest.raises(svc.CollectionError):
        await svc.run_collection(C_DANHARU, session_factory=fake, **PERIOD)
    assert fake.calls and all(t in ("get_my_context",) for t, _ in fake.calls)


async def test_blocked_and_lylon_companies_are_never_queried(db):
    fake = fake_for(C_DANHARU, C_LYLON)
    await svc.discover_companies(session_factory=fake)
    fake.calls.clear()
    with pytest.raises(svc.CollectionError) as e:
        await svc.run_collection(C_LYLON, session_factory=fake, **PERIOD)
    assert e.value.code == "company_not_linked"
    assert not fake.calls
    assert await n(db, "obys_clobe_item") == 0


# ── 수집 ─────────────────────────────────────────────────

async def test_live_run_lands_in_review_box_only_and_never_touches_ledger(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    out = await svc.run_collection(C_DANHARU, session_factory=fake, **PERIOD)
    assert out["status"] == "succeeded", out
    by = {r["data_kind"]: r for r in out["runs"]}
    assert by[core.KIND_BANK]["counts"]["inserted"] == 7 and by[core.KIND_TAX]["counts"]["inserted"] == 5
    assert by[core.KIND_CASH]["counts"]["inserted"] == 4 and by[core.KIND_CARD]["counts"]["inserted"] == 3
    assert await n(db, "obys_clobe_item") == 19
    assert await n(db, "obys_clobe_item", "WHERE stage <> 'review_box'") == 0
    assert await n(db, "obys_clobe_ledger_entry") == 0
    assert await n(db, "obys_clobe_item", "WHERE tenant_id <> '%s' OR business_id <> 'biz-danharu'" % TENANT) == 0
    assert all(t in clobe.COLLECTION_TOOLS for t, _ in fake.calls)
    cash_types = {a["type"] for t, a in fake.calls if t == "get_cash_receipts"}
    assert cash_types == {"PURCHASE", "SALES"}
    assert all(a["size"] == 3 for t, a in fake.calls if t != "get_my_context" and "size" in a)
    state = await db.fetch("SELECT data_kind, status, last_success_at FROM obys_clobe_collection_state")
    assert {r["status"] for r in state} == {"succeeded"} and len(state) == 4


async def test_same_batch_rerun_and_shadow_create_zero_duplicates(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, session_factory=fake, **PERIOD)
    snapshot = await db.fetch("SELECT item_id, item_hash FROM obys_clobe_item ORDER BY item_id")
    rerun = await svc.run_collection(C_DANHARU, session_factory=fake, **PERIOD)
    assert rerun["status"] == "succeeded"
    assert sum(r["counts"]["inserted"] for r in rerun["runs"]) == 0
    assert sum(r["counts"]["unchanged"] for r in rerun["runs"]) == 19
    assert await db.fetch("SELECT item_id, item_hash FROM obys_clobe_item ORDER BY item_id") == snapshot
    shadow = await svc.run_collection(C_DANHARU, mode="shadow", session_factory=fake, **PERIOD)
    assert shadow["status"] == "succeeded"
    assert sum(r["counts"]["would_insert"] for r in shadow["runs"]) == 0
    assert await db.fetch("SELECT item_id, item_hash FROM obys_clobe_item ORDER BY item_id") == snapshot
    assert await n(db, "obys_clobe_collection_run", "WHERE mode='shadow' AND status='succeeded'") == 4


async def test_shadow_on_empty_db_reports_would_insert_but_writes_nothing(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    shadow = await svc.run_collection(C_DANHARU, mode="shadow", session_factory=fake, **PERIOD)
    assert shadow["status"] == "succeeded", shadow
    assert sum(r["counts"]["would_insert"] for r in shadow["runs"]) == 19
    assert await n(db, "obys_clobe_item") == 0 and await n(db, "obys_clobe_collection_state") == 0


async def test_page_omission_is_partial_and_not_success(db):
    fake = fake_for(C_DANHARU)
    fake.drop_last_on_page[core.KIND_TAX] = 1
    await svc.discover_companies(session_factory=fake)
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    run = out["runs"][0]
    assert out["status"] == "partial" and run["status"] == "partial"
    assert run["error_code"] == "count_mismatch" and run["reconcile"][0]["collected_total"] == 4
    st = await db.fetchrow("SELECT status, last_success_at, consecutive_failures FROM obys_clobe_collection_state")
    assert st["status"] == "partial" and st["last_success_at"] is None and st["consecutive_failures"] == 1
    fake.drop_last_on_page.clear()
    fixed = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    assert fixed["status"] == "succeeded" and await n(db, "obys_clobe_item") == 5


async def test_source_scrape_error_is_partial_even_when_counts_match(db):
    fake = fake_for(C_DANHARU)
    fake.assets[C_DANHARU] = [
        {"category": "CARD_APPROVAL", "status": "ERROR", "scrapedAt": "2026-10-02T06:00:00", "failureCategory": "AUTH"}]
    await svc.discover_companies(session_factory=fake)
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_CARD], session_factory=fake, **PERIOD)
    assert out["runs"][0]["status"] == "partial" and out["runs"][0]["error_code"] == "source_scrape_error"
    assert out["runs"][0]["source_as_of"].startswith("2026-10-02T06:00:00")


async def test_transient_failure_resumes_from_checkpoint_without_duplicates(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)

    async def flaky(count, tool, args):
        if tool == "get_tax_invoices" and args.get("page") == 1 and not getattr(flaky, "fired", False):
            flaky.fired = True
            raise clobe.ClobeTransientError("http_503")

    fake.hook = flaky
    first = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    assert first["status"] == "partial" and first["runs"][0]["error_code"].startswith("transient")
    assert await n(db, "obys_clobe_item") == 3          # 0 페이지까지만 적재
    st = await db.fetchrow("SELECT status, next_retry_at FROM obys_clobe_collection_state")
    assert st["status"] == "partial" and st["next_retry_at"] is not None
    fake.calls.clear()
    second = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    run = second["runs"][0]
    assert second["status"] == "succeeded" and run["resumed"] is True and run["attempt"] == 2
    pages = [a["page"] for t, a in fake.calls if t == "get_tax_invoices"]
    assert pages == [1], pages                            # 완료된 페이지는 다시 읽지 않는다
    assert await n(db, "obys_clobe_item") == 5
    assert await db.fetchval("SELECT count(DISTINCT chain_id) FROM obys_clobe_collection_run") == 1


async def test_token_expiry_marks_needs_reauth_and_keeps_data(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)

    async def expire(count, tool, args):
        if tool == "get_tax_invoices" and args.get("page") == 1:
            raise clobe.ClobeReauthRequired("reauth_required")

    fake.hook = expire
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    assert out["status"] == "needs_reauth" and out["error_code"] == "reauth_required"
    st = await db.fetchrow("SELECT status, next_retry_at, last_error_code FROM obys_clobe_collection_state")
    assert (st["status"], st["last_error_code"]) == ("needs_reauth", "reauth_required") and st["next_retry_at"] is None
    assert await n(db, "obys_clobe_item") == 3
    fake.hook = None
    done = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    assert done["status"] == "succeeded" and await n(db, "obys_clobe_item") == 5


async def test_lease_loss_mid_run_stops_all_writes(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)

    async def steal(count, tool, args):
        if tool == "get_tax_invoices" and args.get("page") == 1:
            await db.execute(
                "UPDATE obys_clobe_collection_lease SET owner_instance='other-api', owner_epoch=owner_epoch+1, "
                "lease_expires_at=now()+interval '1 hour' WHERE clobe_company_id=$1", C_DANHARU)

    fake.hook = steal
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX, core.KIND_CARD], session_factory=fake, **PERIOD)
    assert out["status"] == "lease_lost"
    assert await n(db, "obys_clobe_item") == 3            # 인수 후에는 한 행도 쓰지 않는다
    runs = await db.fetch("SELECT data_kind, status FROM obys_clobe_collection_run")
    assert [(r["data_kind"], r["status"]) for r in runs] == [(core.KIND_TAX, "lease_lost")]
    assert await n(db, "obys_clobe_collection_state") == 0
    assert (await db.fetchrow("SELECT owner_instance FROM obys_clobe_collection_lease"))["owner_instance"] == "other-api"
    with pytest.raises(svc.CollectionError) as e:         # 새 소유자의 임대가 살아있는 동안은 시작도 못 한다
        await svc.run_collection(C_DANHARU, session_factory=fake, **PERIOD)
    assert e.value.code == "collection_in_progress"


async def test_expired_lease_is_taken_over_with_higher_epoch(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    await db.execute(
        "INSERT INTO obys_clobe_collection_lease (clobe_company_id, owner_instance, owner_epoch, lease_expires_at) "
        "VALUES ($1,'dead-api',5, now() - interval '1 minute')", C_DANHARU)
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_CARD], session_factory=fake, **PERIOD)
    assert out["status"] == "succeeded"
    row = await db.fetchrow("SELECT owner_epoch FROM obys_clobe_collection_lease")
    assert row["owner_epoch"] == 6
    assert await db.fetchval("SELECT owner_epoch FROM obys_clobe_collection_run") == 6


async def test_revised_item_goes_to_review_box_and_old_version_is_superseded(db):
    fake = fake_for(C_DANHARU, counts={core.KIND_TAX: 2, core.KIND_BANK: 0, core.KIND_CASH: 0, core.KIND_CARD: 0})
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    fake.data[C_DANHARU][core.KIND_TAX][0]["totalAmount"] = 9999
    fake.data[C_DANHARU][core.KIND_TAX][0]["settlementStatus"] = "MATCHED"   # 휘발 필드 — 해시에 영향 없어야 한다
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    assert out["runs"][0]["counts"]["revised"] == 1 and out["runs"][0]["counts"]["inserted"] == 0
    stages = {r["stage"]: r["n"] for r in await db.fetch("SELECT stage, count(*) n FROM obys_clobe_item GROUP BY stage")}
    assert stages == {"review_box": 2, "superseded": 1}


# ── 검토 → 확정 ──────────────────────────────────────────

async def _ids(db, kind=None):
    rows = await db.fetch("SELECT item_id FROM obys_clobe_item WHERE ($1::text IS NULL OR data_kind=$1) ORDER BY source_key", kind)
    return [str(r["item_id"]) for r in rows]


async def test_review_then_confirm_is_staged_and_idempotent(db):
    fake = fake_for(C_DANHARU, counts={core.KIND_TAX: 3, core.KIND_BANK: 0, core.KIND_CASH: 0, core.KIND_CARD: 0})
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    ids = await _ids(db)
    early = await svc.confirm_items(TENANT, "biz-danharu", ids, actor="owner@x")      # 검토 전 확정은 거부
    assert early == {"confirmed": 0, "skipped": 3} and await n(db, "obys_clobe_ledger_entry") == 0
    assert (await svc.review_items(TENANT, "biz-danharu", ids[:2], "approve", actor="acct@x"))["updated"] == 2
    assert (await svc.review_items(TENANT, "biz-danharu", ids[:2], "approve", actor="acct@x"))["updated"] == 0
    assert (await svc.review_items(TENANT, "biz-danharu", ids[2:], "reject", actor="acct@x"))["stage"] == "rejected"
    assert (await svc.confirm_items(TENANT, "biz-danharu", ids, actor="owner@x")) == {"confirmed": 2, "skipped": 1}
    assert await n(db, "obys_clobe_ledger_entry") == 2
    assert (await svc.confirm_items(TENANT, "biz-danharu", ids, actor="owner@x"))["confirmed"] == 0
    assert await n(db, "obys_clobe_ledger_entry") == 2
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)   # 재수집은 원장을 늘리지 않는다
    assert out["runs"][0]["counts"]["inserted"] == 0 and await n(db, "obys_clobe_ledger_entry") == 2


async def test_revision_of_confirmed_item_requires_new_review_and_bumps_revision(db):
    fake = fake_for(C_DANHARU, counts={core.KIND_TAX: 1, core.KIND_BANK: 0, core.KIND_CASH: 0, core.KIND_CARD: 0})
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    first = await _ids(db)
    await svc.review_items(TENANT, "biz-danharu", first, "approve", actor="a")
    await svc.confirm_items(TENANT, "biz-danharu", first, actor="a")
    fake.data[C_DANHARU][core.KIND_TAX][0]["totalAmount"] = 5000
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    entry = await db.fetchrow("SELECT * FROM obys_clobe_ledger_entry")
    assert entry["revision"] == 1 and str(entry["item_id"]) == first[0]      # 수집만으로는 원장이 바뀌지 않는다
    new = [i for i in await _ids(db) if i != first[0]]
    assert len(new) == 1
    await svc.review_items(TENANT, "biz-danharu", new, "approve", actor="a")
    await svc.confirm_items(TENANT, "biz-danharu", new, actor="a")
    assert await n(db, "obys_clobe_ledger_entry") == 1
    entry = await db.fetchrow("SELECT * FROM obys_clobe_ledger_entry")
    assert entry["revision"] == 2 and str(entry["item_id"]) == new[0]
    assert await db.fetchval("SELECT stage FROM obys_clobe_item WHERE item_id=$1", uuid.UUID(first[0])) == "superseded"


async def test_ledger_identity_is_unique_at_database_level(db):
    fake = fake_for(C_DANHARU, counts={core.KIND_TAX: 1, core.KIND_BANK: 0, core.KIND_CASH: 0, core.KIND_CARD: 0})
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_TAX], session_factory=fake, **PERIOD)
    item = await db.fetchrow("SELECT * FROM obys_clobe_item")
    sql = ("INSERT INTO obys_clobe_ledger_entry (entry_id, tenant_id, business_id, data_kind, source_key, item_id, item_hash, "
           "confirmed_by) VALUES ($1,$2,$3,$4,$5,$6,$7,'t')")
    args = (item["tenant_id"], item["business_id"], item["data_kind"], item["source_key"], item["item_id"], item["item_hash"])
    await db.execute(sql, uuid.uuid4(), *args)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(sql, uuid.uuid4(), *args)
    with pytest.raises(asyncpg.UniqueViolationError):    # 같은 항목 버전 이중 적재
        await db.execute(
            "INSERT INTO obys_clobe_item (item_id, tenant_id, business_id, clobe_company_id, data_kind, source_key, item_hash, "
            "payload, first_run_id, last_run_id) SELECT $1, tenant_id, business_id, clobe_company_id, data_kind, source_key, "
            "item_hash, payload, first_run_id, last_run_id FROM obys_clobe_item", uuid.uuid4())


async def test_tenant_scope_on_items_and_status(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_CARD], session_factory=fake, **PERIOD)
    ids = await _ids(db)
    for tenant in (OTHER_TENANT, LYLON):
        with pytest.raises(svc.CollectionError):
            await svc.list_items(tenant, "biz-danharu")
    with pytest.raises(svc.CollectionError) as e:
        await svc.list_items(TENANT, "biz-other")
    assert e.value.code == "business_not_found"
    with pytest.raises(svc.CollectionError):
        await svc.list_items(TENANT, "biz-lylon-e2e")
    other = await svc.review_items(TENANT, "biz-eonni", ids, "approve", actor="a")
    assert other["updated"] == 0                     # 다른 사업자의 항목 id 로는 아무것도 못 바꾼다
    listing = await svc.list_items(TENANT, "biz-danharu", stage="review_box", kind=core.KIND_CARD)
    assert listing["total"] == 3 and set(listing["items"][0]) >= {"item_id", "amount", "stage", "source_as_of"}
    with pytest.raises(svc.CollectionError) as e:
        await svc.require_company_tenant(C_DANHARU, OTHER_TENANT)
    assert e.value.status == 404
    await svc.require_company_tenant(C_DANHARU, TENANT)


async def test_status_contract_for_ui(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    await svc.run_collection(C_DANHARU, kinds=[core.KIND_BANK], session_factory=fake, **PERIOD)
    tenant_view = await svc.company_status(TENANT)
    assert [c["clobe_company_id"] for c in tenant_view["companies"]] == [C_DANHARU]
    c = tenant_view["companies"][0]
    assert set(c["kinds"]) == set(core.DATA_KINDS) and c["kinds"][core.KIND_BANK]["status"] == "succeeded"
    assert c["kinds"][core.KIND_TAX]["status"] == "never_run"
    assert c["kinds"][core.KIND_BANK]["last_success_at"] and c["kinds"][core.KIND_BANK]["covered_to"] == "2026-09-30"
    assert c["item_stage_counts"] == {"review_box": 7} and "reg_no" not in c
    assert tenant_view["unsupported_sources"][0]["status"] == "file_input_required"
    admin_view = await svc.company_status(None, internal_admin=True)
    assert {x["clobe_company_id"] for x in admin_view["companies"]} == {C_DANHARU, C_EONNI, C_YEOLJEONG, C_LYLON}
    runs = await svc.list_runs(TENANT, C_DANHARU)
    assert runs[0]["status"] == "succeeded" and runs[0]["counts"]["inserted"] == 7
    with pytest.raises(svc.CollectionError):
        await svc.list_runs(OTHER_TENANT, C_DANHARU)


async def test_period_and_input_validation(db):
    fake = fake_for(C_DANHARU)
    await svc.discover_companies(session_factory=fake)
    for kwargs, code in (
        ({"mode": "bogus"}, "invalid_mode"), ({"kinds": ["journal"]}, "invalid_kind"),
        ({"period_start": "2026-10-01", "period_end": "2026-09-01"}, "invalid_period"),
        ({"period_start": "2020-01-01", "period_end": "2026-09-01"}, "period_too_long"),
    ):
        with pytest.raises(svc.CollectionError) as e:
            await svc.run_collection(C_DANHARU, session_factory=fake, **kwargs)
        assert e.value.code == code


@pytest.mark.parametrize("mode", ["live", "shadow"])
async def test_cash_cancellations_net_out_in_sum_reconciliation(db, mode):
    fake = fake_for(C_DANHARU, counts={core.KIND_CASH: 3, core.KIND_BANK: 0, core.KIND_TAX: 0, core.KIND_CARD: 0})
    items = fake.data[C_DANHARU][core.KIND_CASH]
    for it in items:
        it["type"], it["transactionType"] = "SALES", "승인거래"
    items[1]["transactionType"] = "취소거래"
    await svc.discover_companies(session_factory=fake)
    out = await svc.run_collection(C_DANHARU, kinds=[core.KIND_CASH], mode=mode, session_factory=fake, **PERIOD)
    run = out["runs"][0]
    assert run["status"] == "succeeded", run
    assert all(c["status"] == "ok" for r in run["reconcile"] for c in r["sum_checks"])
