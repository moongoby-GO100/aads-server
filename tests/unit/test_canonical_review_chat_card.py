"""정본 검토 결과의 채팅 승인 카드 전달 회귀 테스트."""
import json
from unittest.mock import AsyncMock

import pytest

from app.services import canonical_document_tools as cdt
from app.services import tool_executor as te
from app.services.chat_service import _tool_result_event_snapshot, normalize_tool_events
from app.services.model_selector import _tool_result_content

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def card():
    return cdt.approval_card(
        {"tenant_id": TENANT, "document_key": "design", "generation": 2,
         "approved_revision_id": None},
        {"id": "00000000-0000-0000-0000-000000000002", "title": "설계 문서",
         "revision": 1, "version": "1.0.0", "status": "review"}, "AADS")


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotent", [False, True])
async def test_review_success_attaches_lookup_card(monkeypatch, card, idempotent):
    review = AsyncMock(return_value={"reviewed": True, "project": "AADS",
                                   "document_key": "design", "idempotent": idempotent})
    lookup = AsyncMock(return_value={"registered": True, "approval_card": card})
    monkeypatch.setattr(cdt, "review_document", review)
    monkeypatch.setattr(cdt, "lookup_documents", lookup)
    monkeypatch.setattr(te, "resolve_bound_tenant_id", AsyncMock(return_value=TENANT))
    monkeypatch.setattr(te, "_resolve_bound_chat_session_id", lambda _: SESSION)
    result = await te.ToolExecutor()._canonical_document_review(
        {"project": "aads", "document_key": "design", "reason": "검토를 요청하는 충분한 사유"})
    assert result["approval_card"] == card
    assert result["approval_card"]["can_approve"] is False
    lookup.assert_awaited_once_with(tenant_id=TENANT,
                                   inp={"project": "AADS", "document_key": "design"})
    assert review.await_args.kwargs["session_id"] == SESSION


@pytest.mark.asyncio
async def test_failed_review_has_no_card(monkeypatch):
    failure = {"error": "review_role_denied"}
    monkeypatch.setattr(cdt, "review_document", AsyncMock(return_value=failure))
    lookup = AsyncMock()
    monkeypatch.setattr(cdt, "lookup_documents", lookup)
    monkeypatch.setattr(te, "resolve_bound_tenant_id", AsyncMock(return_value=TENANT))
    monkeypatch.setattr(te, "_resolve_bound_chat_session_id", lambda _: SESSION)
    result = await te.ToolExecutor()._canonical_document_review({})
    assert result == failure and "approval_card" not in result
    lookup.assert_not_awaited()


@pytest.mark.parametrize("tool_name", ["canonical_document_review", "canonical_document_lookup"])
@pytest.mark.parametrize("padding", [1000, 40000])
def test_card_survives_transport_snapshot_and_replay(card, tool_name, padding):
    content = _tool_result_content(json.dumps({"padding": "x" * padding,
                                              "approval_card": card}), tool_name)
    snapshot = _tool_result_event_snapshot({"tool_name": tool_name, "content": content})
    assert snapshot["canonical_document_cards"] == [card]
    assert normalize_tool_events([snapshot])[0]["canonical_document_cards"] == [card]


@pytest.mark.parametrize("tool_name", ["canonical_document_review", "canonical_document_lookup"])
def test_card_validation_still_rejects_capability(card, tool_name):
    card["can_approve"] = True
    event = {"tool_name": tool_name, "content": json.dumps({"approval_card": card})}
    assert _tool_result_event_snapshot(event)["canonical_document_cards"] == []
    assert normalize_tool_events([{"type": "tool_result", "tool_name": tool_name,
                                   "canonical_document_cards": [card]}])[0]["canonical_document_cards"] == []
