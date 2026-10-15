"""모델 전환 배너: 단정("실행 불가") 대신 실제 시도·실패 사유·응답 모델을 적는다."""
from __future__ import annotations

import pytest

from app.services import chat_service
from app.services import model_switch_banner as msb


@pytest.mark.parametrize(
    "error, reason",
    [
        ("429 rate limit", "한도 초과"),
        ("401 Unauthorized", "인증 오류"),
        ("request timed out", "시간 초과"),
        ("HTTP 400: bad request", "HTTP 400"),
        ("upstream failed", "요청 오류"),
    ],
)
def test_banner_names_classified_reason(error, reason):
    banner = msb.build_switch_banner("claude-opus-5-5", "gpt-6-astra", msb.classify_failure_reason(error))
    assert banner == f"\n\n[claude-opus-5-5 응답 실패({reason}) → gpt-6-astra로 응답]\n\n"
    assert "실행 불가" not in banner


def test_unknown_reason_says_response_failed_only():
    for reason in (None, ""):
        banner = msb.build_switch_banner("a", "b", reason)
        assert banner.strip() == "[a 응답 실패 → b로 응답]"


def test_multi_attempt_path_marks_failed_hops():
    banner = msb.build_switch_banner(
        "opus-5-5", "gpt-5.6-sol", "한도 초과", failed_path=["opus-5-5", "gpt-6-astra"],
    )
    assert banner.strip() == (
        "[opus-5-5 응답 실패(한도 초과) → gpt-5.6-sol로 응답 · 시도: opus-5-5 → gpt-6-astra(실패) → gpt-5.6-sol]"
    )


def test_single_failed_attempt_has_no_path():
    banner = msb.build_switch_banner("a", "b", "시간 초과", failed_path=["a"])
    assert "시도:" not in banner


def test_new_and_legacy_formats_are_recognised():
    new = msb.build_switch_banner("a", "Codex CLI b", "Codex 인증 실패(재로그인 필요)")
    legacy_items = [
        "\n\n[gpt-5.6-sol 실행 불가 → Codex CLI gpt-5.5 전환]\n\n",
        "\n\n[m 오류 → Codex CLI x 전환]\n\n",
        "\n\n[m (Antigravity) 오류 → Codex CLI x 전환]\n\n",
    ]
    parsed = msb.parse_switch_banners(new)
    assert parsed[0]["format"] == msb.FORMAT_NEW
    assert (parsed[0]["requested"], parsed[0]["actual"]) == ("a", "Codex CLI b")
    assert parsed[0]["reason"] == "Codex 인증 실패(재로그인 필요)"
    for text in legacy_items:
        got = msb.parse_switch_banners(text)
        assert len(got) == 1 and got[0]["format"] == msb.FORMAT_LEGACY
        assert msb.has_switch_banner(text)
    assert msb.parse_switch_banners(legacy_items[0])[0]["actual"] == "Codex CLI gpt-5.5"


def test_path_is_parsed_and_strip_removes_both_formats():
    banner = msb.build_switch_banner("a", "c", "과부하", failed_path=["a", "b"])
    parsed = msb.parse_switch_banners(banner)[0]
    assert parsed["path"] == "a → b(실패) → c"
    body = "\n\n[x 실행 불가 → y 전환]\n\n" + banner + "본문입니다"
    assert msb.strip_switch_banners(body) == "본문입니다"
    assert msb.strip_switch_banners("본문") == "본문"
    assert not msb.has_switch_banner("[모델 응답 실패 (인증 오류) — 직접 선택하신 모델이라 바꾸지 않았습니다.]")


def test_chat_service_uses_shared_helper():
    assert chat_service._model_switch_banner("a", "b", "인증 오류") == msb.build_switch_banner("a", "b", "인증 오류")
    assert chat_service._safe_fallback_reason("429") == msb.classify_failure_reason("429")


def test_retry_note_builds_reason_and_path_banner():
    chain: list = []
    banner = chat_service._note_retry_model_switch(
        chain, prev_model="gpt-6-astra", new_model="gpt-5.6-sol", base_model="opus-5-5",
        reason="시간 초과", failed_path=["opus-5-5", "gpt-6-astra"], banner_reason="한도 초과",
    )
    assert banner.strip() == (
        "[opus-5-5 응답 실패(한도 초과) → gpt-5.6-sol로 응답 · 시도: opus-5-5 → gpt-6-astra(실패) → gpt-5.6-sol]"
    )
    assert chain[0]["reason"] == "시간 초과"


def test_retry_note_without_reason_does_not_assert_unrunnable():
    banner = chat_service._note_retry_model_switch(
        [], prev_model="a", new_model="b", base_model="a", reason="",
    )
    assert banner.strip() == "[a 응답 실패 → b로 응답]"


def test_interrupt_break_leaves_no_banner():
    # 5cf57465: 추가 지시로 끊긴 시도는 재시도 루프를 빠져나가므로 다음 시도의 배너가 만들어지지 않는다.
    import inspect

    src = inspect.getsource(chat_service.send_message_stream)
    assert "if _interrupt_stream_break:\n                break" in src
