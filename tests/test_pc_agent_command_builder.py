"""AADS-195 Phase 3: PC Agent 명령 빌더 단위 테스트."""
from __future__ import annotations

import pytest

from app.services.pc_agent_command_builder import (
    build_command,
    build_command_for_intent,
    format_result,
)


class TestBuildCommand:
    """자연어 → 명령 JSON 변환 테스트."""

    def test_screenshot(self) -> None:
        result = build_command("PC 스크린샷 찍어")
        assert result == {"type": "screenshot"}

    def test_screenshot_capture(self) -> None:
        result = build_command("화면 캡처해줘")
        assert result is not None
        assert result["type"] == "screenshot"

    def test_kakao_send_basic(self) -> None:
        result = build_command("카카오톡으로 김대리에게 '회의 5분전' 보내줘")
        assert result is not None
        assert result["type"] == "kakao_send"
        assert result["recipient"] == "김대리"
        assert result["message"] == "회의 5분전"

    def test_kakao_send_short(self) -> None:
        result = build_command("카톡 보내줘")
        assert result is not None
        assert result["type"] == "kakao_send"

    def test_kakao_read(self) -> None:
        result = build_command("카톡 메시지 확인해줘")
        assert result is not None
        assert result["type"] == "kakao_read"

    def test_shell_notepad(self) -> None:
        result = build_command("PC에서 메모장 열어")
        assert result is not None
        assert result["type"] == "shell"
        assert "notepad" in result["command"].lower()

    def test_shell_chrome(self) -> None:
        result = build_command("크롬 실행해")
        assert result is not None
        assert result["type"] == "shell"
        assert "chrome" in result["command"].lower()

    def test_shell_calculator(self) -> None:
        result = build_command("계산기 열어줘")
        assert result is not None
        assert result["type"] == "shell"
        assert "calc" in result["command"].lower()

    def test_file_list(self) -> None:
        result = build_command("PC 파일 목록 보여줘")
        assert result is not None
        assert result["type"] == "file_list"

    def test_file_read_with_path(self) -> None:
        result = build_command("C:\\Users\\test\\readme.txt 파일 읽어줘")
        assert result is not None
        assert result["type"] == "file_read"
        assert result["path"] == "C:\\Users\\test\\readme.txt"

    def test_process_list(self) -> None:
        result = build_command("프로세스 목록 보여줘")
        assert result is not None
        assert result["type"] == "process_list"

    def test_system_info(self) -> None:
        result = build_command("PC 시스템 정보 알려줘")
        assert result is not None
        assert result["type"] == "system_info"

    def test_no_match(self) -> None:
        result = build_command("오늘 날씨 어때")
        assert result is None


class TestBuildCommandForIntent:
    """인텐트 기반 명령 빌더 테스트."""

    def test_pc_screenshot(self) -> None:
        result = build_command_for_intent("pc_screenshot", "화면 찍어줘")
        assert result == {"type": "screenshot"}

    def test_pc_kakao_send(self) -> None:
        result = build_command_for_intent("pc_kakao", "카톡으로 보내줘")
        assert result is not None
        assert result["type"] == "kakao_send"

    def test_pc_kakao_read(self) -> None:
        result = build_command_for_intent("pc_kakao", "카톡 메시지 읽어줘")
        assert result is not None
        assert result["type"] == "kakao_read"

    def test_pc_file(self) -> None:
        result = build_command_for_intent("pc_file", "파일 목록 보여줘")
        assert result is not None
        assert result["type"] == "file_list"

    def test_pc_control(self) -> None:
        result = build_command_for_intent("pc_control", "메모장 열어")
        assert result is not None
        assert result["type"] == "shell"


class TestFormatResult:
    """결과 포맷 변환 테스트."""

    def test_screenshot_result(self) -> None:
        result = format_result("screenshot", {"data": {"image": "abc123"}, "status": "success"})
        assert "data:image/png;base64" in result

    def test_shell_result(self) -> None:
        result = format_result("shell", {"data": {"output": "hello world", "exit_code": 0}, "status": "success"})
        assert "hello world" in result

    def test_error_result(self) -> None:
        result = format_result("shell", {"status": "error", "data": {"error": "timeout"}})
        assert "오류" in result

    def test_none_result(self) -> None:
        result = format_result("shell", None)
        assert "응답 없음" in result

    def test_kakao_send_result(self) -> None:
        result = format_result("kakao_send", {"data": {"recipient": "김대리", "sent": True}, "status": "success"})
        assert "김대리" in result


class TestImports:
    """모듈 임포트 테스트."""

    def test_import_command_builder(self) -> None:
        from app.services.pc_agent_command_builder import build_command
        assert callable(build_command)

    def test_import_pc_agent_manager(self) -> None:
        from app.services.pc_agent_manager import pc_agent_manager
        assert pc_agent_manager is not None

    def test_import_pc_agent_models(self) -> None:
        from app.models.pc_agent import CommandRequest, CommandResult, AgentInfo
        assert CommandRequest is not None
        assert CommandResult is not None
        assert AgentInfo is not None

    def test_command_request_kakao_type(self) -> None:
        """CommandRequest에 kakao_send 타입이 추가되었는지 확인."""
        from app.models.pc_agent import CommandRequest
        req = CommandRequest(agent_id="test", command_type="kakao_send")
        assert req.command_type == "kakao_send"

    def test_command_request_system_info_type(self) -> None:
        from app.models.pc_agent import CommandRequest
        req = CommandRequest(agent_id="test", command_type="system_info")
        assert req.command_type == "system_info"
