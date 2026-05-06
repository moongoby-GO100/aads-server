from pathlib import Path


CHAT_PAGE = Path("aads-dashboard/src/app/chat/page.tsx")


def test_minimal_polling_uses_content_preserving_merge_guard():
    source = CHAT_PAGE.read_text()

    assert "function mergeMessagePreservingFullContent" in source
    assert "content: keepCurrentContent ? currentContent : incomingContent" in source
    assert "mergeMessagePreservingFullContent(m, match)" in source
    assert "mergeMessagePreservingFullContent(tempMatch, m)" in source


def test_minimal_tool_summary_hydrates_full_message_once():
    source = CHAT_PAGE.read_text()

    assert "toolHydrationRequestedRef" in source
    assert "chatApi<ChatMessage>(`/chat/messages/${msg.id}`)" in source
    assert "tool_hydration_status: \"loading\"" in source
    assert "도구 상세 기록을 불러오는 중입니다." in source
