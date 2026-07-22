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
