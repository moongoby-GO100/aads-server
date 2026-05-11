from app.services.chat_service import _HISTORY_EXCLUDED_INTENTS, _history_intent_filter_sql
from app.services.model_selector import _format_messages_as_text


def test_llm_history_filter_excludes_system_and_runner_noise():
    predicate = _history_intent_filter_sql()

    for intent in (
        "streaming_placeholder",
        "rate_limited",
        "system_trigger",
        "auto_reaction",
        "runner_response",
    ):
        assert intent in _HISTORY_EXCLUDED_INTENTS
        assert intent in predicate


def test_cli_resume_injects_recent_turns_not_only_latest_user():
    messages = [
        {"role": "user", "content": "첫 지시: NTV2 세션 문제 확인"},
        {"role": "assistant", "content": "직전 응답: 원인은 workspace_switch 오분류입니다."},
        {"role": "user", "content": "이전 지시 참고해서 조치해"},
    ]

    rendered = _format_messages_as_text(messages, has_resume=True)

    assert "AADS 대화 연속성 컨텍스트" in rendered
    assert "직전 응답: 원인은 workspace_switch 오분류입니다." in rendered
    assert "이전 지시 참고해서 조치해" in rendered


def test_cli_non_resume_keeps_more_than_legacy_40_turns():
    messages = [
        {"role": "user" if idx % 2 == 0 else "assistant", "content": f"turn-{idx}"}
        for idx in range(60)
    ]

    rendered = _format_messages_as_text(messages, has_resume=False)

    assert "turn-0" in rendered
    assert "turn-59" in rendered
