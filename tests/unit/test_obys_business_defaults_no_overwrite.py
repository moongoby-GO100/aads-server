"""AADS-OBYS-BIZ-DEFAULTS-NO-OVERWRITE-20260930

설정 동기화(save_settings_persisted)가 DB 의 사업자 등록 4항목을 자리표시자·빈 값으로
덮지 않는다. 실제 upsert SQL 을 sqlite 에서 그대로 실행해 판정한다($n → :pn 치환만 함).
관리자 직접 수정(obys_upload_service.update_business)은 계속 덮어쓴다.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from app.services import obys_upload_service as obys
from app.services import yeoljeong_finance_service as finance

TENANT = UUID("15055cac-71b0-45ec-b714-7093dde189ff")
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": str(TENANT)}
BIZ = "biz-eonni-naengmyeon"
REAL = {
    "registration_no": "773-12-03049",
    "representative": "오병용",
    "opened_at": "2026-07-21",
    "address": "서울 성북구 동소문로 90 1층 102호",
}
PLACEHOLDERS = {
    "registrationNo": "기초등록 필요",
    "representative": "미등록",
    "openedAt": "",
    "address": "",
}
COLUMN_BY_KEY = {
    "registrationNo": "registration_no",
    "representative": "representative",
    "openedAt": "opened_at",
    "address": "address",
}


def _to_sqlite(sql: str) -> str:
    return re.sub(r"\$(\d+)", r":p\1", sql)


def _bind(args):
    return {f"p{i}": (str(a) if isinstance(a, UUID) else a) for i, a in enumerate(args, 1)}


class Db:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.create_function("NOW", 0, lambda: "2026-09-30T00:00:00")
        self.db.execute(
            "CREATE TABLE yeoljeong_businesses (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "entity_type TEXT NOT NULL DEFAULT 'individual', name TEXT NOT NULL, "
            "registration_no TEXT NOT NULL DEFAULT '', representative TEXT NOT NULL DEFAULT '', "
            "tax_type TEXT NOT NULL DEFAULT '', opened_at TEXT NOT NULL DEFAULT '', "
            "address TEXT NOT NULL DEFAULT '', memo TEXT NOT NULL DEFAULT '', "
            "sort_order INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL DEFAULT '', "
            "updated_at TEXT, deleted_at TEXT)"
        )
        self.other_sql: list[str] = []

    def seed(self, business_id=BIZ, **values):
        row = {"registration_no": "", "representative": "", "opened_at": "", "address": "", **values}
        self.db.execute(
            "INSERT INTO yeoljeong_businesses (id, tenant_id, name, tax_type, memo, sort_order, "
            "registration_no, representative, opened_at, address) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (business_id, str(TENANT), "언니냉면", "일반과세", "원래메모", 9,
             row["registration_no"], row["representative"], row["opened_at"], row["address"]),
        )

    def row(self, business_id=BIZ):
        return dict(self.db.execute("SELECT * FROM yeoljeong_businesses WHERE id=?", (business_id,)).fetchone())


class FakeConn:
    def __init__(self, store: Db):
        self.store = store

    async def fetchval(self, sql, *args):
        return True

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        if "INSERT INTO yeoljeong_businesses" in sql:
            self.store.db.execute(_to_sqlite(sql), _bind(args))
        else:
            self.store.other_sql.append(sql)

    async def fetchrow(self, sql, *args):
        cursor = self.store.db.execute(_to_sqlite(sql), _bind(args))
        row = cursor.fetchone()
        return dict(row) if row else None

    async def close(self):
        return None


class FakePool:
    def __init__(self, store: Db):
        self.store = store

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.store)


@pytest.fixture
def store(tmp_path, monkeypatch):
    fake = Db()
    monkeypatch.setattr(finance, "DATA_DIR", tmp_path)
    monkeypatch.setattr(finance, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(finance, "_get_pool_or_none", lambda: FakePool(fake))

    async def connect():
        return FakeConn(fake)

    monkeypatch.setattr(obys, "_connect", connect)
    return fake


def _save(overrides=None, business_id=BIZ):
    business = {"id": business_id, "name": "언니냉면", "taxType": "일반과세", "memo": "원래메모", **PLACEHOLDERS}
    business.update(overrides or {})
    result = asyncio.run(finance.save_settings_persisted({"settings": {"businesses": [business]}}, ADMIN))
    assert result["meta"]["source"] == "database"  # 예외가 삼켜져 파일 폴백된 게 아니다
    return result


def test_placeholder_constant_is_single_source():
    assert set(finance.BUSINESS_PLACEHOLDER_VALUES) == {"기초등록 필요", "미등록"}
    assert finance._business_registration_value("  기초등록 필요 ") == ""
    assert finance._business_registration_value("미등록") == ""
    assert finance._business_registration_value(None) == ""
    assert finance._business_registration_value(" 773-12-03049 ") == "773-12-03049"
    for placeholder in finance.BUSINESS_PLACEHOLDER_VALUES:
        assert placeholder in finance.BUSINESS_SETTINGS_UPSERT_SQL


def test_real_values_survive_placeholder_and_blank_settings_save(store):
    store.seed(**REAL)
    _save()
    row = store.row()
    for column, value in REAL.items():
        assert row[column] == value


def test_blank_db_values_are_filled_from_settings(store):
    store.seed()
    _save({"registrationNo": "123-45-67891", "representative": "김테스트", "openedAt": "2026-01-02", "address": "서울 어딘가"})
    row = store.row()
    assert (row["registration_no"], row["representative"], row["opened_at"], row["address"]) == (
        "123-45-67891", "김테스트", "2026-01-02", "서울 어딘가")


def test_existing_placeholder_in_db_is_treated_as_blank(store):
    store.seed(registration_no="기초등록 필요", representative="미등록")
    _save({"registrationNo": "123-45-67891", "representative": "김테스트"})
    row = store.row()
    assert row["registration_no"] == "123-45-67891"
    assert row["representative"] == "김테스트"


def test_whitespace_only_db_value_is_treated_as_blank(store):
    store.seed(registration_no="   ", address="  ")
    _save({"registrationNo": "123-45-67891", "address": "서울 어딘가"})
    row = store.row()
    assert row["registration_no"] == "123-45-67891"
    assert row["address"] == "서울 어딘가"


@pytest.mark.parametrize("key", list(COLUMN_BY_KEY))
@pytest.mark.parametrize("incoming", ["기초등록 필요", "미등록", "", "  "])
def test_placeholder_or_blank_incoming_never_overwrites_each_column(store, key, incoming):
    store.seed(**REAL)
    _save({key: incoming})
    assert store.row()[COLUMN_BY_KEY[key]] == REAL[COLUMN_BY_KEY[key]]


def test_placeholder_incoming_into_blank_db_stays_blank(store):
    store.seed()
    _save({"registrationNo": "기초등록 필요", "representative": "미등록"})
    row = store.row()
    assert row["registration_no"] == ""
    assert row["representative"] == ""


def test_new_business_is_inserted_with_placeholders_normalized_to_blank(store):
    _save({"registrationNo": "기초등록 필요", "representative": "미등록"}, business_id="biz-new")
    row = store.row("biz-new")
    assert row["registration_no"] == ""
    assert row["representative"] == ""


def test_non_registration_columns_keep_previous_overwrite_behavior(store):
    store.seed(**REAL)
    _save({"name": "언니냉면 성신여대역점", "taxType": "간이과세", "memo": "새메모", "entityType": "corporation"})
    row = store.row()
    assert row["name"] == "언니냉면 성신여대역점"
    assert row["tax_type"] == "간이과세"
    assert row["memo"] == "새메모"
    assert row["entity_type"] == "corporation"
    assert row["updated_by"] == "owner@example.com"
    assert row["deleted_at"] is None


def test_sort_order_follows_settings_position(store):
    store.seed(**REAL)
    assert store.row()["sort_order"] == 9
    _save()
    # 사업자 4개(canonical)가 병합돼 들어오므로 위치는 canonical 순서다
    assert store.row()["sort_order"] == 3


def test_admin_update_business_still_overwrites(store):
    store.seed(**REAL)
    changed = {
        "registration_no": "123-45-67891",
        "representative": "김테스트",
        "opened_at": "2027-01-01",
        "address": "새 주소",
    }
    result = asyncio.run(obys.update_business(user=ADMIN, business_id=BIZ, payload=changed))
    for column, value in changed.items():
        assert result[column] == value
        assert store.row()[column] == value


def test_settings_sync_after_admin_edit_keeps_admin_value(store):
    store.seed(**REAL)
    asyncio.run(obys.update_business(user=ADMIN, business_id=BIZ, payload={"representative": "김테스트"}))
    _save({"representative": "다른사람"})
    assert store.row()["representative"] == "김테스트"
