"""등록 반려 사유 — 입력 → decide → DB(decision_reason) → 조회 왕복과 빈 사유 거절."""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.services.work_recipe import registration
from app.services.work_recipe.registration import RegistrationError

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
SECRET = "sk-ant-oat01-" + "a" * 40


class _Store:
    def __init__(self) -> None:
        self.row: dict[str, Any] = {
            "id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT), "recipe_id": None,
            "domain": "example.com", "name": "r", "status": "pending", "requested_by": "bot",
            "decided_by": "", "decision_reason": "", "spec": json.dumps({"steps": []}), "dry_run": "{}",
        }
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append(query)
        if "UPDATE work_recipe_registration_requests" in query:
            assert "decision_reason=$4" in query.replace(" ", "")
            self.row.update(status="rejected", decided_by=args[2], decision_reason=args[3])
        return dict(self.row)

    async def fetch(self, query: str, *args: Any):
        self.queries.append(query)
        return [dict(self.row)] if self.row["status"] == args[1] else []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    def acquire(self):
        store = self

        class _Acq:
            async def __aenter__(self_inner):
                return store

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(registration, "get_pool", lambda: s)
    return s


@pytest.mark.parametrize("blank", ["", "   ", "\n\t", None])
async def test_reject_without_reason_is_refused_and_row_untouched(store, blank):
    with pytest.raises(RegistrationError, match="rejection_reason_required"):
        await registration.decide_registration(
            store.row["id"], tenant_id=TENANT, decision="reject", decided_by="alice", reason=blank)
    assert store.row["status"] == "pending"
    assert not any("UPDATE" in q for q in store.queries)


def test_blank_reason_maps_to_422():
    assert registration.registration_error_status(RegistrationError("rejection_reason_required")) == 422
    assert registration.registration_error_status(RegistrationError("registration_not_found")) == 404


async def test_reject_reason_roundtrips_through_list_get_and_summary(store):
    await registration.decide_registration(
        store.row["id"], tenant_id=TENANT, decision="reject", decided_by="alice",
        reason="  로그인 단계가 빠졌습니다  ")
    assert store.row["decision_reason"] == "로그인 단계가 빠졌습니다"

    listed = await registration.list_registrations(tenant_id=TENANT, status="rejected")
    assert listed[0]["decision_reason"] == "로그인 단계가 빠졌습니다"
    got = await registration.get_registration(store.row["id"], tenant_id=TENANT)
    assert got["decision_reason"] == "로그인 단계가 빠졌습니다"
    assert registration.registration_summary(listed[0])["rejection_reason"] == "로그인 단계가 빠졌습니다"


async def test_reason_is_masked_and_capped(store):
    await registration.decide_registration(
        store.row["id"], tenant_id=TENANT, decision="reject", decided_by="alice",
        reason=f"토큰 {SECRET} " + "x" * 3000)
    assert SECRET not in store.row["decision_reason"]
    assert len(store.row["decision_reason"]) <= registration.REJECTION_REASON_MAX_LEN


def test_summary_hides_reason_for_non_rejected_rows():
    approved = {"status": "approved", "decision_reason": '{"policy":"auto"}', "spec": {}, "dry_run": {}}
    assert registration.registration_summary(approved)["rejection_reason"] == ""


async def test_approve_does_not_require_reason(store):
    store.row["status"] = "approved"
    with pytest.raises(RegistrationError, match="registration_already_decided"):
        await registration.decide_registration(
            store.row["id"], tenant_id=TENANT, decision="approve", decided_by="alice")
