from app.services.chat_service import (
    _append_generated_image_markdown,
    _generated_image_urls_from_tool_events,
    _normalize_generated_image_url,
)


def test_generated_image_tool_result_is_attached_to_chat_content():
    events = [{"type": "tool_result", "tool_name": "generate_image", "content": '{"url":"/api/v1/image/gallery/media-abc123/image","job_id":"media-abc123","status":"succeeded"}'}]
    urls = _generated_image_urls_from_tool_events(events)
    content = _append_generated_image_markdown("생성이 완료되었습니다.", urls)
    assert urls == ["/api/v1/image/gallery/media-abc123/image"]
    assert content.endswith("![생성 이미지 1](/api/v1/image/gallery/media-abc123/image)")


def test_legacy_static_generated_url_is_normalized_and_not_duplicated():
    public_url = "/api/v1/image/gallery/media-old123/image"
    assert _normalize_generated_image_url("/static/media/generated/image/media-old123.png") == public_url
    assert _append_generated_image_markdown(f"![이미지]({public_url})", [public_url]) == f"![이미지]({public_url})"


def test_failed_generated_image_tool_result_is_ignored():
    events = [{"type": "tool_result", "tool_name": "generate_image", "is_error": True, "content": '{"url":"/api/v1/image/gallery/media-failed/image","status":"failed"}'}]
    assert _generated_image_urls_from_tool_events(events) == []


def test_canonical_card_survives_storage_and_replay_without_content_truncation():
    import json
    from app.services.chat_service import _tool_result_event_snapshot, normalize_tool_events
    from app.services.canonical_document_tools import approval_card
    card = approval_card({"tenant_id": "00000000-0000-0000-0000-000000000001",
        "document_key": "design", "generation": 10, "approved_revision_id": None},
        {"id": "00000000-0000-0000-0000-000000000002", "title": "한글 제목",
         "revision": 10, "version": "1.0.0"}, "AADS")
    event = {"tool_name": "canonical_document_lookup", "content": json.dumps(
        {"padding": "x" * 1000, "approval_card": card})}
    snapshot = _tool_result_event_snapshot(event, content_limit=300)
    assert len(snapshot["content"]) == 300
    assert normalize_tool_events([snapshot])[0]["canonical_document_cards"] == [card]
    event["tool_name"] = "agent_permission_request"
    assert "canonical_document_cards" not in _tool_result_event_snapshot(event)
    event["tool_name"] = "canonical_document_lookup"
    event["content"] = "모델의 임의 승인 문자열"
    assert "canonical_document_cards" not in _tool_result_event_snapshot(event)


def test_canonical_cards_reject_tampering_and_enforce_size_limits():
    from copy import deepcopy
    from app.services.canonical_document_tools import approval_card, validated_approval_cards
    card = approval_card({"tenant_id": "00000000-0000-0000-0000-000000000001",
        "document_key": "design", "generation": 10, "approved_revision_id": None},
        {"id": "00000000-0000-0000-0000-000000000002", "title": "설계",
         "revision": 10, "version": "1.0.0"}, "AADS")
    for field, value in [("approval_endpoint", "https://evil/approve"),
                         ("approval_body", {"revision_id": "other", "expected_generation": 0}),
                         ("can_approve", True), ("title", "x" * 10000),
                         ("tenant_id", "bad"), ("generation", True), ("extra", "action")]:
        changed = deepcopy(card)
        changed[field] = value
        assert validated_approval_cards([changed]) == []
        from app.services.chat_service import normalize_tool_events
        assert normalize_tool_events([{"type": "tool_result", "tool_name": "canonical_document_lookup",
            "canonical_document_cards": [changed]}])[0]["canonical_document_cards"] == []
    cards = validated_approval_cards([card] * 100)
    import json
    assert len(cards) <= 20
    assert sum(len(json.dumps(c, ensure_ascii=False).encode()) for c in cards) <= 24576


def test_tool_transport_budget_depends_only_on_resolved_tool_name():
    from app.services.model_selector import _tool_result_content, _map_cli_event
    content = '{"approval_card": "' + "x" * 35000
    assert len(_tool_result_content(content, "web_search")) == 3000
    assert len(_tool_result_content(content, "canonical_document_lookup")) == 32000
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "lookup", "content": content}]}}
    assert len(_map_cli_event(event)[0]["content"]) == 3000
    mapped = _map_cli_event(event, tool_names={"lookup": "canonical_document_lookup"})
    assert len(mapped[0]["content"]) == 32000


def test_large_canonical_summary_keeps_complete_card_json():
    import json
    from app.services.canonical_document_tools import approval_card
    from app.services.model_selector import _tool_result_content
    from app.services.chat_service import _tool_result_event_snapshot
    card = approval_card({"tenant_id": "00000000-0000-0000-0000-000000000001",
        "document_key": "design", "generation": 10, "approved_revision_id": None},
        {"id": "00000000-0000-0000-0000-000000000002", "title": "설계",
         "revision": 10, "version": "1.0.0"}, "AADS")
    content = json.dumps({"padding": "x" * 40000, "documents": [{"approval_card": card}]})
    transported = _tool_result_content(content, "canonical_document_lookup")
    snapshot = _tool_result_event_snapshot({"tool_name": "canonical_document_lookup", "content": transported})
    assert snapshot["canonical_document_cards"] == [card]
