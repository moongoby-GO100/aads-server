"""ACCT-OBYS-SEAL-REGISTERED-TRADE-NAME-20261016 — 사업장 trade_name(사업자등록증 상호/법인명) 저장·조회·마이그레이션.

운영 이미지 의존(fastapi·asyncpg): bash scripts/run_unit_tests.sh tests/unit/test_obys_business_trade_name.py
도장 상호 우선순위는 test_obys_company_seal.py(화면)·test_obys_company_seal_pdf.py(서버) 가 검사한다.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import pytest

try:
    from fastapi import HTTPException

    from app.api.obys_finance import TenantBusinessUpdatePayload
    from app.services import obys_upload_service as obys
    from app.services import yeoljeong_finance_service as finance

    IMAGE_DEPS = True
except ModuleNotFoundError:
    IMAGE_DEPS = False

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/20261016_obys_business_trade_name.sql"
TENANT_A = UUID("15055cac-71b0-45ec-b714-7093dde189ff")
TENANT_B = UUID("99999999-9999-4999-8999-999999999999")
BIZ = "biz-junghwa"


def _user(tenant: UUID) -> dict:
    return {"email": "owner@example.com", "tenant_id": str(tenant), "current_membership": {"tenant_id": str(tenant), "status": "active", "role": "owner"}}


def _to_sqlite(sql: str) -> str:
    return re.sub(r"\$(\d+)", r":p\1", sql)


def _bind(args):
    return {f"p{i}": (str(a) if isinstance(a, UUID) else a) for i, a in enumerate(args, 1)}


class Store:
    def __init__(self, with_trade_name: bool = True):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.create_function("NOW", 0, lambda: "2026-10-16T00:00:00")
        extra = ", trade_name TEXT" if with_trade_name else ""
        self.db.execute(
            "CREATE TABLE yeoljeong_businesses (id TEXT PRIMARY KEY, tenant_id TEXT, entity_type TEXT NOT NULL DEFAULT 'individual', "
            "name TEXT NOT NULL, registration_no TEXT NOT NULL DEFAULT '', representative TEXT NOT NULL DEFAULT '', "
            "tax_type TEXT NOT NULL DEFAULT '', opened_at TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', "
            f"memo TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT, deleted_at TEXT{extra})"
        )
        self.db.execute("INSERT INTO yeoljeong_businesses (id, tenant_id, entity_type, name) VALUES (?,?,?,?)", (BIZ, str(TENANT_A), "corporation", "열정국밥 중화점"))

    def row(self, business_id=BIZ):
        return dict(self.db.execute("SELECT * FROM yeoljeong_businesses WHERE id=?", (business_id,)).fetchone())


class Conn:
    def __init__(self, store: Store):
        self.store = store

    async def fetchrow(self, sql, *args):
        try:
            row = self.store.db.execute(_to_sqlite(sql), _bind(args)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such column" in str(exc):
                raise type("UndefinedColumnError", (Exception,), {})(str(exc)) from exc
            raise
        return dict(row) if row else None

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def _needs_production_image(request):
    if not IMAGE_DEPS and "migration" not in request.node.name:
        pytest.skip("운영 이미지 의존(fastapi) — scripts/run_unit_tests.sh 로 실행")


@pytest.fixture
def store(monkeypatch):
    fake = Store()

    async def connect():
        return Conn(fake)

    monkeypatch.setattr(obys, "_connect", connect)
    return fake


def _update(payload, tenant=TENANT_A, business_id=BIZ):
    return asyncio.run(obys.update_business(user=_user(tenant), business_id=business_id, payload=payload))


def test_update_saves_and_returns_trade_name_and_leaves_other_columns(store):
    result = _update({"trade_name": "  주식회사 윤희에프엔비  "})
    assert result["trade_name"] == "주식회사 윤희에프엔비"
    row = store.row()
    assert row["trade_name"] == "주식회사 윤희에프엔비" and row["name"] == "열정국밥 중화점" and row["entity_type"] == "corporation"
    assert row["updated_by"] == "owner@example.com"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_trade_name_is_stored_as_null(store, blank):
    _update({"trade_name": "주식회사 윤희에프엔비"})
    result = _update({"trade_name": blank})
    assert result["trade_name"] is None and store.row()["trade_name"] is None


def test_trade_name_is_kept_verbatim_including_underscore(store):
    assert _update({"trade_name": "열정국밥_미아점"})["trade_name"] == "열정국밥_미아점"


def test_trade_name_length_limit(store):
    assert _update({"trade_name": "가" * obys.TRADE_NAME_MAX_LENGTH})["trade_name"] == "가" * obys.TRADE_NAME_MAX_LENGTH
    with pytest.raises(HTTPException) as caught:
        _update({"trade_name": "가" * (obys.TRADE_NAME_MAX_LENGTH + 1)})
    assert caught.value.status_code == 400
    assert store.row()["trade_name"] == "가" * obys.TRADE_NAME_MAX_LENGTH


def test_other_tenant_cannot_change_trade_name(store):
    _update({"trade_name": "주식회사 윤희에프엔비"})
    with pytest.raises(HTTPException) as caught:
        _update({"trade_name": "남의 상호"}, tenant=TENANT_B)
    assert caught.value.status_code == 404
    assert store.row()["trade_name"] == "주식회사 윤희에프엔비"


def test_update_without_trade_name_does_not_touch_it(store):
    _update({"trade_name": "주식회사 윤희에프엔비"})
    _update({"representative": "오윤희"})
    assert store.row()["trade_name"] == "주식회사 윤희에프엔비"


def test_missing_column_gives_a_clear_503_not_a_raw_error(monkeypatch):
    legacy = Store(with_trade_name=False)

    async def connect():
        return Conn(legacy)

    monkeypatch.setattr(obys, "_connect", connect)
    with pytest.raises(HTTPException) as caught:
        _update({"trade_name": "주식회사 윤희에프엔비"})
    assert caught.value.status_code == 503
    assert _update({"representative": "오윤희"})["trade_name"] is None  # 컬럼이 없어도 다른 항목 수정은 된다


def test_update_payload_model_accepts_trade_name_and_still_forbids_unknown_keys():
    assert TenantBusinessUpdatePayload(trade_name="주식회사 윤희에프엔비").model_dump(exclude_unset=True) == {"trade_name": "주식회사 윤희에프엔비"}
    with pytest.raises(ValueError):
        TenantBusinessUpdatePayload(tenant_id="x")


def test_registry_list_selects_trade_name_without_requiring_the_column():
    import inspect

    source = inspect.getsource(obys.list_businesses)
    assert "->>'trade_name' AS trade_name" in source and "to_jsonb(yeoljeong_businesses)" in source
    assert "tenant_id=$1" in source


def test_settings_response_exposes_trade_name_as_camel_case(monkeypatch):
    rows = {
        "businesses": [
            {"id": BIZ, "entity_type": "corporation", "name": "열정국밥 중화점", "registration_no": "", "representative": "", "tax_type": "", "opened_at": "", "address": "", "memo": "", "trade_name": "주식회사 윤희에프엔비"},
            {"id": "biz-mia", "entity_type": "individual", "name": "열정국밥_미아점", "registration_no": "", "representative": "", "tax_type": "", "opened_at": "", "address": "", "memo": "", "trade_name": None},
        ]
    }

    class SettingsConn:
        async def fetchval(self, sql, *args):
            return True

        async def fetch(self, sql, *args):
            if "FROM yeoljeong_businesses" in sql:
                assert "->>'trade_name' AS trade_name" in sql
                return rows["businesses"]
            return []

        async def fetchrow(self, sql, *args):
            return None

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield SettingsConn()

    monkeypatch.setattr(finance, "_get_pool_or_none", lambda: Pool())
    result = asyncio.run(finance.get_settings_persisted(_user(TENANT_A)))
    by_id = {item["id"]: item for item in result["settings"]["businesses"]}
    assert result["meta"]["source"] == "database"
    assert by_id[BIZ]["tradeName"] == "주식회사 윤희에프엔비"
    assert by_id["biz-mia"]["tradeName"] == ""


def test_settings_sync_never_writes_trade_name():
    # 등록증 상호의 편집 경로는 tenant-registry PATCH 하나다. 설정 동기화(전체 목록 PUT)는 옛 화면이 값을 지우는 사고를 만들 수 있어 쓰지 않는다.
    assert "trade_name" not in finance.BUSINESS_SETTINGS_UPSERT_SQL


def test_migration_is_additive_idempotent_and_guarded():
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    assert "ADD COLUMN IF NOT EXISTS trade_name text" in code
    assert "to_regclass('public.yeoljeong_businesses')" in code and "RETURN" in code
    assert code.index("to_regclass") < code.index("ALTER TABLE")
    for forbidden in ("DROP", "RENAME", "NOT NULL", "DEFAULT", "UPDATE", "INSERT", "DELETE"):
        assert forbidden not in code.upper(), forbidden
    assert code.upper().count("ALTER TABLE") == 1
