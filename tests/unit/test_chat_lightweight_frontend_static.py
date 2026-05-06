from pathlib import Path


REAL_DASHBOARD_CHAT_PAGE = Path("../aads-dashboard/src/app/chat/page.tsx")
CHAT_PAGE = REAL_DASHBOARD_CHAT_PAGE if REAL_DASHBOARD_CHAT_PAGE.exists() else Path("aads-dashboard/src/app/chat/page.tsx")


def test_minimal_polling_uses_content_preserving_merge_guard():
    source = CHAT_PAGE.read_text()

    assert "function mergeServerMessageWithExisting" in source
    assert "keepExistingContent" in source
    assert "content: keepExistingContent ? existingContent : serverContent" in source
    assert "mergeServerMessageWithExisting(m, fullMsg)" in source


def test_minimal_tool_summary_hydrates_full_message_once():
    source = CHAT_PAGE.read_text()

    assert "toolHydrationRequestedRef" in source
    assert "chatApi<ChatMessage>(`/chat/messages/${msg.id}`)" in source
    assert "tool_hydration_status: \"loading\"" in source
    assert "도구 상세 기록을 불러오는 중입니다." in source


def test_tool_result_only_events_count_as_tool_usage():
    source = CHAT_PAGE.read_text()

    assert "getToolNamesFromMessage(msg).length" in source
    assert "|| toolEventsForRender.length" in source
