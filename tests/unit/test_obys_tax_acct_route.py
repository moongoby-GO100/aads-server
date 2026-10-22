"""오비서 H3: 세금신고 접수 구분(P-10), 원천세 소득 구분(P-11), 회계 권한·테넌트 격리·동결(P-12),
acct_purchase 전표 조회 DB 라우팅(P-15)."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api import acct_purchase
from app.api import ceo_chat_tools_db as db
from app.api import obys_accounting as router
from app.services import yeoljeong_accounting_service as svc
from app.services import yeoljeong_withholding as wh

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OBYS_DATABASE_URL", "postgresql://u:pw@db.invalid:5432/obys")
    monkeypatch.delenv("OBYS_JOURNAL_WRITE_FROZEN", raising=False)
    monkeypatch.delenv("ACCT_LEDGER_DB_NAME", raising=False)
    monkeypatch.delenv("ACCT_APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("ACCT_DATABASE_URL", raising=False)
    monkeypatch.delenv("ACCT_DB_APP_USER", raising=False)
    monkeypatch.delenv("ACCT_DB_APP_PASSWORD", raising=False)


@pytest.fixture
def unfrozen(monkeypatch):
    monkeypatch.setenv("OBYS_JOURNAL_WRITE_FROZEN", "false")


class FakeConn:
    def __init__(self, status="draft", details=None):
        self.row = {"id": "tax-1", "business_id": "biz-a", "status": status, "details": details or {}}
        self.updates: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *params):
        if sql.lstrip().startswith("SELECT status"):
            return {"status": self.row["status"]}
        if sql.lstrip().startswith("UPDATE"):
            self.updates.append((sql, params))
            return {**self.row, "status": self._new_status(sql, params)}
        raise AssertionError(sql)

    def _new_status(self, sql, params):
        for i, p in enumerate(params, start=1):
            if f"status = ${i}" in sql:
                return p
        return self.row["status"]

    async def close(self):
        return None


def _patch_asyncpg(monkeypatch, conn):
    async def _connect(*args, **kwargs):
        return conn

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_connect))


# ── P-10 접수 구분 ──────────────────────────────────────────────────────────
class TestFilingView:
    def test_submitted_without_receipt_is_not_complete(self):
        view = svc.filing_view({"status": "submitted", "details": {}})
        assert view["filing_complete"] is False
        assert "신고 완료 아님" in view["filing_label"]
        assert view["receipt_number"] == ""

    def test_receipt_confirmed_without_number_is_not_complete(self):
        view = svc.filing_view({"status": "receipt_confirmed", "details": {}})
        assert view["filing_complete"] is False
        assert "접수 완료" != view["filing_label"]

    def test_receipt_confirmed_with_number_is_complete(self):
        details = json.dumps({"receipt": {"receipt_number": "R-2026-0001", "received_at": "2026-10-20T10:00:00+09:00"}})
        view = svc.filing_view({"status": "receipt_confirmed", "details": details})
        assert view["filing_complete"] is True
        assert view["filing_label"] == "접수 완료"
        assert view["receipt_number"] == "R-2026-0001"


class TestUpdateTaxReport:
    async def test_receipt_confirmed_requires_number(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn())
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report(
                "tax-1", {"status": "receipt_confirmed", "receipt_received_at": "2026-10-20T10:00:00+09:00"}
            )
        assert exc.value.status_code == 422

    @pytest.mark.parametrize("number", ["", "  ", "-", "없음", "N/A", "a b", "x" * 65])
    async def test_receipt_confirmed_rejects_placeholder_number(self, monkeypatch, unfrozen, number):
        _patch_asyncpg(monkeypatch, FakeConn())
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report(
                "tax-1",
                {"status": "receipt_confirmed", "receipt_number": number, "receipt_received_at": "2026-10-20T10:00:00+09:00"},
            )
        assert exc.value.status_code == 422

    async def test_receipt_confirmed_requires_received_at(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn())
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report("tax-1", {"status": "receipt_confirmed", "receipt_number": "R-1234"})
        assert exc.value.status_code == 422

    async def test_future_received_at_rejected(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn())
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report(
                "tax-1", {"status": "receipt_confirmed", "receipt_number": "R-1234", "receipt_received_at": future}
            )
        assert exc.value.status_code == 422

    async def test_receipt_fields_without_confirmed_status_rejected(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn())
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report("tax-1", {"status": "submitted", "receipt_number": "R-1234"})
        assert exc.value.status_code == 422

    async def test_unknown_status_rejected(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn())
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report("tax-1", {"status": "done"})
        assert exc.value.status_code == 422

    async def test_submitted_alone_is_saved_but_not_complete(self, monkeypatch, unfrozen):
        conn = FakeConn()
        _patch_asyncpg(monkeypatch, conn)
        report = await svc.update_tax_report("tax-1", {"status": "submitted"})
        assert report["status"] == "submitted"
        assert report["filing_complete"] is False
        assert not any("jsonb_build_object" in sql for sql, _ in conn.updates)

    async def test_receipt_confirmed_with_number_and_time_is_saved(self, monkeypatch, unfrozen):
        conn = FakeConn()
        _patch_asyncpg(monkeypatch, conn)
        report = await svc.update_tax_report(
            "tax-1",
            {"status": "receipt_confirmed", "receipt_number": "R-2026-0001", "receipt_received_at": "2026-10-20T10:00:00+09:00"},
        )
        assert report["status"] == "receipt_confirmed"
        sql, params = conn.updates[0]
        assert "jsonb_build_object('receipt'" in sql
        saved = next(json.loads(p) for p in params if isinstance(p, str) and p.startswith("{"))
        assert saved["receipt_number"] == "R-2026-0001"
        assert saved["received_at"].startswith("2026-10-20T10:00:00")

    async def test_confirmed_report_amounts_and_status_locked(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn(status="receipt_confirmed"))
        for payload in ({"tax_amount": 1}, {"status": "draft"}):
            with pytest.raises(HTTPException) as exc:
                await svc.update_tax_report("tax-1", payload)
            assert exc.value.status_code == 409

    async def test_missing_report_returns_none(self, monkeypatch, unfrozen):
        class Empty(FakeConn):
            async def fetchrow(self, sql, *params):
                return None

        _patch_asyncpg(monkeypatch, Empty())
        assert await svc.update_tax_report("nope", {"memo": "x"}) is None


# ── P-11 원천세 소득 구분 ────────────────────────────────────────────────────
class TestWithholding:
    def test_business_income_verified_with_sources(self):
        rule = wh.get_rule(wh.BUSINESS_INCOME)
        assert rule.verified
        assert any("nts.go.kr" in url for url in rule.sources)
        assert any("law.go.kr" in url for url in rule.sources)

    def test_business_income_total_is_3_3_percent(self):
        result = wh.compute_withholding(wh.BUSINESS_INCOME, 1_000_000)
        assert (result["income_tax"], result["local_income_tax"], result["total"]) == (30_000, 3_000, 33_000)
        assert result["rounding_verified"] is False

    @pytest.mark.parametrize("kind", [wh.WAGE_INCOME, wh.DAILY_WAGE_INCOME])
    def test_unverified_kinds_are_blocked(self, kind):
        with pytest.raises(wh.WithholdingUnverified) as exc:
            wh.compute_withholding(kind, 1_000_000)
        assert "검증 필요" in exc.value.reason

    def test_unknown_kind(self):
        with pytest.raises(wh.WithholdingKindUnknown):
            wh.compute_withholding("other", 1)

    def test_legacy_rate_constant_matches_rule(self):
        assert svc.WITHHOLDING_TAX_RATE == pytest.approx(0.033)

    def test_withholding_figures_missing_kind_does_not_compute(self):
        amount, info = svc._withholding_figures(None, 1_000_000)
        assert amount is None and info["status"] == "verification_required"

    def test_withholding_figures_unverified_kind_does_not_compute(self):
        amount, info = svc._withholding_figures(wh.WAGE_INCOME, 1_000_000)
        assert amount is None and info["status"] == "verification_required"

    def test_withholding_figures_business_income(self):
        amount, info = svc._withholding_figures(wh.BUSINESS_INCOME, 1_000_000)
        assert amount == 33_000 and info["status"] == "computed"

    def test_withholding_figures_unknown_kind_is_422(self):
        with pytest.raises(HTTPException) as exc:
            svc._withholding_figures("other", 1)
        assert exc.value.status_code == 422


# ── P-12 권한·테넌트 격리·동결 ───────────────────────────────────────────────
def _user(role="owner", tenant=TENANT, **extra):
    return {
        "tenant_id": tenant,
        "email": "a@example.com",
        "current_membership": {"tenant_id": tenant, "status": "active", "role": role},
        **extra,
    }


class TestRoles:
    @pytest.mark.parametrize("role", ["member", "viewer"])
    async def test_non_manager_member_rejected(self, role):
        with pytest.raises(HTTPException) as exc:
            await router._require_manager(_user(role))
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("role", ["owner", "admin"])
    async def test_manager_allowed(self, role):
        user = _user(role)
        assert await router._require_manager(user) is user

    async def test_inactive_or_foreign_membership_rejected(self):
        bad = _user("owner")
        bad["current_membership"]["status"] = "pending"
        foreign = _user("owner")
        foreign["current_membership"]["tenant_id"] = OTHER_TENANT
        for user in (bad, foreign, {"tenant_id": TENANT}):
            with pytest.raises(HTTPException) as exc:
                await router._require_manager(user)
            assert exc.value.status_code == 403


class TestTenantScope:
    def _patch(self, monkeypatch, owned_businesses):
        calls = []

        class Conn:
            async def fetchval(self, sql, business_id, tenant_id):
                calls.append((business_id, str(tenant_id)))
                return (business_id, str(tenant_id)) in owned_businesses

            async def close(self):
                return None

        async def _connect():
            return Conn()

        monkeypatch.setattr(router.upload_svc, "_connect", _connect)
        return calls

    async def test_other_tenant_gets_404(self, monkeypatch):
        self._patch(monkeypatch, {("biz-a", TENANT)})
        with pytest.raises(HTTPException) as exc:
            await router._scope_business(_user("owner", OTHER_TENANT), "biz-a")
        assert exc.value.status_code == 404

    async def test_owning_tenant_passes(self, monkeypatch):
        self._patch(monkeypatch, {("biz-a", TENANT)})
        assert await router._scope_business(_user("owner"), "biz-a") == "biz-a"

    async def test_missing_business_is_404(self, monkeypatch):
        self._patch(monkeypatch, set())
        with pytest.raises(HTTPException) as exc:
            await router._scope_business(_user("owner"), None)
        assert exc.value.status_code == 404

    async def test_internal_admin_does_not_cross_tenant(self, monkeypatch):
        self._patch(monkeypatch, {("biz-a", TENANT)})
        with pytest.raises(HTTPException) as exc:
            await router._scope_business(_user("owner", OTHER_TENANT, is_internal_admin=True), "biz-a")
        assert exc.value.status_code == 404

    async def test_update_route_checks_report_owner_before_writing(self, monkeypatch, unfrozen):
        self._patch(monkeypatch, {("biz-a", TENANT)})
        wrote = []

        async def _biz(report_id):
            return "biz-a"

        async def _update(report_id, payload):
            wrote.append(report_id)
            return {"id": report_id}

        monkeypatch.setattr(router.svc, "tax_report_business_id", _biz)
        monkeypatch.setattr(router.svc, "update_tax_report", _update)
        payload = router.TaxReportUpdatePayload(memo="x")
        with pytest.raises(HTTPException) as exc:
            await router.update_tax_report("tax-1", payload, _user("owner", OTHER_TENANT))
        assert exc.value.status_code == 404 and wrote == []
        result = await router.update_tax_report("tax-1", payload, _user("owner"))
        assert result == {"tax_report": {"id": "tax-1"}} and wrote == ["tax-1"]

    async def test_delete_rule_checks_rule_owner(self, monkeypatch, unfrozen):
        self._patch(monkeypatch, {("biz-a", TENANT)})

        async def _biz(rule_id):
            return "biz-a"

        async def _delete(rule_id):
            raise AssertionError("다른 테넌트 규칙을 지우면 안 된다")

        monkeypatch.setattr(router.svc, "category_rule_business_id", _biz)
        monkeypatch.setattr(router.svc, "delete_category_rule", _delete)
        with pytest.raises(HTTPException) as exc:
            await router.delete_category_rule("cr-1", _user("owner", OTHER_TENANT))
        assert exc.value.status_code == 404


class TestWriteFreeze:
    async def test_frozen_by_default_rejects_tax_report_and_rule_writes(self):
        calls = [
            svc.create_tax_report({"business_id": "biz-a", "report_type": "vat"}),
            svc.update_tax_report("tax-1", {"memo": "x"}),
            svc.create_category_rule({"keyword": "k", "category": "c"}),
            svc.delete_category_rule("cr-1"),
        ]
        for call in calls:
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 409
            assert exc.value.detail["code"] == "accounting_write_moved_to_acct"

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", ""])
    async def test_frozen_values(self, monkeypatch, value):
        monkeypatch.setenv("OBYS_JOURNAL_WRITE_FROZEN", value)
        with pytest.raises(HTTPException) as exc:
            await svc.update_tax_report("tax-1", {"memo": "x"})
        assert exc.value.status_code == 409

    async def test_unfrozen_allows_write_path(self, monkeypatch, unfrozen):
        _patch_asyncpg(monkeypatch, FakeConn())
        report = await svc.update_tax_report("tax-1", {"memo": "x"})
        assert report["id"] == "tax-1"

    async def test_reads_not_frozen(self, monkeypatch):
        class Conn:
            async def fetch(self, sql, *params):
                return []

            async def close(self):
                return None

        _patch_asyncpg(monkeypatch, Conn())
        assert await svc.list_tax_reports("biz-a") == []


# ── P-15 전표 조회 DB 라우팅 ─────────────────────────────────────────────────
class TestLedgerDatabase:
    def test_default_is_acct(self):
        assert db.acct_ledger_database_name() == "acct"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ACCT_LEDGER_DB_NAME", "acct_v2")
        assert db.acct_ledger_database_name() == "acct_v2"

    @pytest.mark.parametrize("bad", ["a b", "acct;drop", "a-b", "x" * 64])
    def test_invalid_name_rejected(self, monkeypatch, bad):
        monkeypatch.setenv("ACCT_LEDGER_DB_NAME", bad)
        with pytest.raises(ValueError):
            db.acct_ledger_database_name()

    def test_dsn_database_replaced_keeping_credentials(self):
        out = db._dsn_with_database("postgresql://u:p%40ss@h.example:5433/obys?sslmode=require", "acct")
        assert out == "postgresql://u:p%40ss@h.example:5433/acct?sslmode=require"

    async def test_dsn_mode_connects_to_requested_database(self, monkeypatch):
        monkeypatch.setenv("ACCT_APP_DATABASE_URL", "postgresql://u:pw@h.example:5433/obys")
        seen = {}

        async def _boom(dsn=None, **kwargs):
            seen["dsn"] = dsn
            raise OSError("stop")

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_boom))
        await db.query_acct_database("SELECT 1", database="acct")
        assert seen["dsn"].endswith("/acct")
        await db.query_acct_database("SELECT 1")
        assert seen["dsn"].endswith("/obys")

    async def test_tunnel_mode_connects_to_requested_database(self, monkeypatch):
        monkeypatch.setenv("ACCT_DB_APP_USER", "u")
        monkeypatch.setenv("ACCT_DB_APP_PASSWORD", "p")
        monkeypatch.setenv("ACCT_DB_NAME", "obys")
        monkeypatch.setattr(db, "_ensure_ssh_tunnel", lambda project: 15432)
        seen = []

        async def _boom(*args, **kwargs):
            seen.append(kwargs.get("database"))
            raise OSError("stop")

        monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_boom))
        await db.query_acct_database("SELECT 1", database="acct")
        await db.query_acct_database("SELECT 1")
        assert seen == ["acct", "obys"]

    async def test_invalid_database_argument_rejected(self):
        result = await db.query_acct_database("SELECT 1", database="a;b")
        assert "database" in result["error"]

    async def test_acct_purchase_reads_use_ledger_database(self, monkeypatch):
        calls = []

        async def _query(sql, acct_tenant_id=None, database=None):
            calls.append((acct_tenant_id, database))
            return {"rows": []}

        monkeypatch.setattr(acct_purchase, "query_acct_database", _query)
        await acct_purchase._fetch_acct_journals("select 1", 8)
        await acct_purchase._fetch("select 1")
        assert calls == [("8", "acct"), (None, "acct")]

    async def test_acct_purchase_follows_env_override(self, monkeypatch):
        monkeypatch.setenv("ACCT_LEDGER_DB_NAME", "acct_v2")
        calls = []

        async def _query(sql, acct_tenant_id=None, database=None):
            calls.append(database)
            return {"rows": []}

        monkeypatch.setattr(acct_purchase, "query_acct_database", _query)
        await acct_purchase._fetch("select 1")
        assert calls == ["acct_v2"]

    async def test_acct_purchase_bad_setting_is_503(self, monkeypatch):
        monkeypatch.setenv("ACCT_LEDGER_DB_NAME", "bad name")
        with pytest.raises(HTTPException) as exc:
            await acct_purchase._fetch("select 1")
        assert exc.value.status_code == 503

    def test_docstring_no_longer_claims_jinah_server(self):
        head = acct_purchase.__doc__.split("데이터 경로")[0]
        assert "cafe24_114" in head and "acct-pg" in head
