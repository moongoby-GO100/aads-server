"""_safe_reaction_message: 배포 완료 시 화면 검증 필수 + R-E2E 폴백 안내문."""
from app.services.chat_service import _safe_reaction_message


def test_deploy_report_rules_require_screen_evidence():
    msg = _safe_reaction_message("sess-1", "배포 완료 알림")
    assert "필수" in msg
    assert "capture_screenshot" in msg
    assert "browser_snapshot" in msg
    assert "API 검증으로 대체" in msg
    assert "화면 미검증(배포 전)" in msg
    assert "렌더링 확인 권장" not in msg


def test_fallback_order_is_http_health_container():
    msg = _safe_reaction_message("sess-1", "x")
    assert msg.index("HTTP 상태코드") < msg.index("API health") < msg.index("컨테이너/프로세스")


def test_signature_session_block_and_forbidden_tools_preserved():
    msg = _safe_reaction_message("sess-abc", "본문")
    assert msg.startswith("본문\n\n")
    assert "[현재 세션 ID: sess-abc]" in msg
    for tool in ("delegate_to_agent", "pipeline_c_start", "spawn_subagent", "spawn_parallel_subagents"):
        assert tool in msg
