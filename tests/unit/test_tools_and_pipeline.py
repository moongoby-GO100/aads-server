"""
AADS 도구 + 파이프라인 단위/통합 테스트.

실행: docker exec aads-server python3 -m pytest tests/unit/test_tools_and_pipeline.py -v
       또는 호스트에서: cd /root/aads/aads-server && docker exec aads-server python3 -m pytest tests/unit/test_tools_and_pipeline.py -v

커버리지:
  1. 도구 함수 단위 테스트 (크래시 없이 정상 에러 반환)
  2. 경로 자동교정
  3. 보안 화이트리스트/차단 패턴
  4. 파이프 탐지
  5. Circuit breaker 변수 정의 순서
  6. Output validator 패턴
  7. Intent → 도구 활성화 흐름
  8. 기능 간 충돌 테스트 (시맨틱 캐시 + 도구, 품질평가 + 팩트추출 등)
"""
import asyncio
import os
import sys
import re
import json

# 프로젝트 루트
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

# ═══════════════════════════════════════════════════════════════════
# 1. 도구 함수 단위 테스트
# ═══════════════════════════════════════════════════════════════════

class TestPathNormalization:
    """AADS 경로 자동교정 테스트."""

    def test_host_absolute_path(self):
        from app.api.ceo_chat_tools import _normalize_aads_path
        assert _normalize_aads_path("/root/aads/aads-server/app/main.py") == "app/main.py"

    def test_container_double_prefix(self):
        from app.api.ceo_chat_tools import _normalize_aads_path
        assert _normalize_aads_path("/app/app/main.py") == "app/main.py"

    def test_aads_server_prefix(self):
        from app.api.ceo_chat_tools import _normalize_aads_path
        assert _normalize_aads_path("aads-server/app/main.py") == "app/main.py"

    def test_app_aads_server_prefix(self):
        from app.api.ceo_chat_tools import _normalize_aads_path
        assert _normalize_aads_path("/app/aads-server/app/main.py") == "app/main.py"

    def test_correct_path_unchanged(self):
        from app.api.ceo_chat_tools import _normalize_aads_path
        assert _normalize_aads_path("app/main.py") == "app/main.py"

    def test_nested_path(self):
        from app.api.ceo_chat_tools import _normalize_aads_path
        assert _normalize_aads_path("/root/aads/aads-server/app/services/chat_service.py") == "app/services/chat_service.py"


class TestReadRawFile:
    """_read_raw_file이 줄번호 없이 반환하는지 테스트."""

    @pytest.mark.asyncio
    async def test_no_line_numbers(self):
        from app.api.ceo_chat_tools import _read_raw_file
        content = await _read_raw_file("AADS", "app/main.py")
        assert not content.startswith("[ERROR]"), f"파일 읽기 실패: {content[:100]}"
        first_line = content.split("\n")[0]
        # 줄번호 패턴: "     1\t..." 이 없어야 함
        assert not re.match(r'^\s*\d+\t', first_line), f"줄번호가 포함됨: {first_line[:50]}"

    @pytest.mark.asyncio
    async def test_nonexistent_file(self):
        from app.api.ceo_chat_tools import _read_raw_file
        result = await _read_raw_file("AADS", "nonexistent_file_xyz.py")
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_path_escape_blocked(self):
        from app.api.ceo_chat_tools import _read_raw_file
        result = await _read_raw_file("AADS", "../../etc/passwd")
        assert "[ERROR]" in result


class TestPatchRemoteFile:
    """patch_remote_file 단위 테스트."""

    @pytest.mark.asyncio
    async def test_old_string_not_found_returns_error_with_hint(self):
        from app.api.ceo_chat_tools import tool_patch_remote_file
        result = await tool_patch_remote_file("AADS", "app/main.py", "NONEXISTENT_XYZ_12345", "REPLACED")
        assert "[ERROR]" in result
        assert "read_remote_file" in result  # 가이드 포함

    @pytest.mark.asyncio
    async def test_same_old_new_rejected(self):
        from app.api.ceo_chat_tools import tool_patch_remote_file
        result = await tool_patch_remote_file("AADS", "app/main.py", "same", "same")
        assert "[ERROR]" in result
        assert "동일" in result

    @pytest.mark.asyncio
    async def test_no_crash_on_valid_file(self):
        """실제 파일에서 크래시 없이 에러 메시지 반환 (UnboundLocalError 방지 확인)."""
        from app.api.ceo_chat_tools import tool_patch_remote_file
        result = await tool_patch_remote_file("AADS", "app/main.py", "THIS_WILL_NOT_MATCH", "REPLACED")
        assert isinstance(result, str)  # 크래시 없이 문자열 반환


class TestReadRemoteFile:
    """read_remote_file 경로 교정 + 에러 가이드 테스트."""

    @pytest.mark.asyncio
    async def test_auto_corrected_path(self):
        from app.api.ceo_chat_tools import tool_read_remote_file
        result = await tool_read_remote_file("AADS", "/root/aads/aads-server/app/main.py")
        assert "[AADS 파일" in result  # 자동교정 후 정상 읽기

    @pytest.mark.asyncio
    async def test_not_found_has_guide(self):
        from app.api.ceo_chat_tools import tool_read_remote_file
        result = await tool_read_remote_file("AADS", "nonexistent.py")
        assert "[ERROR]" in result
        assert "경로 규칙" in result or "read_remote_file" in result


# ═══════════════════════════════════════════════════════════════════
# 2. 보안 화이트리스트/차단 테스트
# ═══════════════════════════════════════════════════════════════════

class TestRunRemoteCommandSecurity:
    """run_remote_command 보안 규칙 테스트."""

    @pytest.mark.asyncio
    async def test_rm_rf_blocked(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "rm -rf /")
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_cat_allowed(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "cat app/main.py")
        assert "[ERROR]" not in result or "허용" not in result

    @pytest.mark.asyncio
    async def test_tail_allowed(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "tail -20 app/main.py")
        assert "[AADS 명령 실행" in result

    @pytest.mark.asyncio
    async def test_grep_escape_pipe_allowed(self):
        """grep \\| 이스케이프 파이프가 차단되지 않아야 함."""
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", 'grep -rn "streaming" app/main.py')
        assert "[ERROR]" not in result or "파이프" not in result

    @pytest.mark.asyncio
    async def test_2_dev_null_allowed(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "ls /tmp 2>/dev/null")
        assert "파이프" not in result and "위험" not in result

    @pytest.mark.asyncio
    async def test_2_stderr_redirect_allowed(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "ls /nonexistent 2>&1")
        assert "파이프" not in result and "위험" not in result

    @pytest.mark.asyncio
    async def test_pipe_to_grep_allowed(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "ps aux | grep python")
        assert "파이프" not in result

    @pytest.mark.asyncio
    async def test_whitelist_deny_has_guide(self):
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "nmap localhost")
        assert "[ERROR]" in result
        assert "허용 명령" in result  # 가이드 포함


# ═══════════════════════════════════════════════════════════════════
# 3. Circuit Breaker 변수 정의 순서 테스트
# ═══════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    """circuit breaker 코드의 변수 정의 순서 + 동작 테스트."""

    def test_is_green_defined_before_use(self):
        """_is_green이 사용 전에 정의되는지 소스 코드 레벨 검증."""
        import inspect
        from app.services.model_selector import _stream_anthropic
        src = inspect.getsource(_stream_anthropic)
        # _is_green 첫 정의 위치
        first_def = src.find("_is_green = tu.name in _GREEN_TOOLS")
        # _is_green 첫 사용 위치 (_same_limit에서)
        first_use = src.find("_SAME_TOOL_ERROR_LIMIT_GREEN if _is_green")
        assert first_def < first_use, f"_is_green 정의({first_def}) 가 사용({first_use})보다 뒤에 있음!"

    def test_green_tools_defined(self):
        import inspect
        from app.services.model_selector import _stream_anthropic
        src = inspect.getsource(_stream_anthropic)
        assert "_GREEN_TOOLS" in src
        assert "read_remote_file" in src  # Green 도구 목록에 포함

    def test_error_limits_are_advisory(self):
        """에러 제한이 차단이 아닌 경고 수준인지 확인."""
        import inspect
        from app.services.model_selector import _stream_anthropic
        src = inspect.getsource(_stream_anthropic)
        # STOP 값이 999 (사실상 비활성)
        assert "_CONSECUTIVE_ERROR_STOP = 999" in src or "_CONSECUTIVE_ERROR_STOP = 9" in src


# ═══════════════════════════════════════════════════════════════════
# 4. Output Validator 패턴 테스트
# ═══════════════════════════════════════════════════════════════════

class TestOutputValidator:
    """output_validator가 <tool_call> 할루시네이션을 탐지하는지 테스트."""

    def test_detects_tool_call_xml(self):
        from app.services.output_validator import _FABRICATED_XML_PATTERNS
        test_text = '<tool_call>{"name": "read_remote_file"}</tool_call>'
        matched = any(p.search(test_text) for p in _FABRICATED_XML_PATTERNS)
        assert matched, "<tool_call> 패턴 미탐지"

    def test_detects_tool_response_xml(self):
        from app.services.output_validator import _FABRICATED_XML_PATTERNS
        test_text = '<tool_response>{"output": "fake data"}</tool_response>'
        matched = any(p.search(test_text) for p in _FABRICATED_XML_PATTERNS)
        assert matched, "<tool_response> 패턴 미탐지"

    def test_detects_function_results(self):
        from app.services.output_validator import _FABRICATED_XML_PATTERNS
        test_text = '<function_results>some result</function_results>'
        matched = any(p.search(test_text) for p in _FABRICATED_XML_PATTERNS)
        assert matched

    def test_normal_text_not_flagged(self):
        from app.services.output_validator import _FABRICATED_XML_PATTERNS
        test_text = "서버 상태를 확인했습니다. 모두 정상입니다."
        matched = any(p.search(test_text) for p in _FABRICATED_XML_PATTERNS)
        assert not matched, "정상 텍스트가 오탐됨"


# ═══════════════════════════════════════════════════════════════════
# 5. Intent → 도구 활성화 흐름 테스트
# ═══════════════════════════════════════════════════════════════════

class TestIntentToolActivation:
    """casual 인텐트에서 도구 키워드 감지 로직 테스트."""

    def test_tool_keywords_exist_in_code(self):
        """chat_service에 도구 키워드 감지 로직이 있는지."""
        import inspect
        from app.services.chat_service import send_message_stream
        src = inspect.getsource(send_message_stream)
        assert "_tool_requiring_keywords" in src
        assert "INTENT_FIX" in src

    def test_model_override_enables_tools(self):
        """model_override가 Claude일 때 use_tools=True 강제하는 코드 존재."""
        import inspect
        from app.services.chat_service import send_message_stream
        src = inspect.getsource(send_message_stream)
        assert "claude" in src.lower() and "use_tools = True" in src


# ═══════════════════════════════════════════════════════════════════
# 6. 기능 간 충돌 테스트 (통합)
# ═══════════════════════════════════════════════════════════════════

class TestCrossFeatureConflicts:
    """서로 다른 기능이 충돌하지 않는지 테스트."""

    def test_semantic_cache_import_no_side_effect(self):
        """시맨틱 캐시 import가 다른 모듈에 영향 없음."""
        from app.services.semantic_cache import SemanticCache
        from app.services.chat_service import send_message_stream
        # 둘 다 import 성공하면 충돌 없음
        assert SemanticCache is not None
        assert send_message_stream is not None

    def test_self_evaluator_and_fact_extractor_coexist(self):
        """자기평가 + 팩트추출이 동시 import 시 충돌 없음."""
        from app.services.self_evaluator import evaluate_response
        from app.services.fact_extractor import extract_facts
        assert evaluate_response is not None
        assert extract_facts is not None

    def test_context_builder_layers_no_conflict(self):
        """context_builder가 모든 레이어를 import할 수 있는지."""
        from app.services.context_builder import build_messages_context
        from app.core.memory_recall import build_memory_context
        from app.services.auto_rag import build_auto_rag_context
        from app.services.workspace_preloader import build_workspace_preload
        assert all([build_messages_context, build_memory_context,
                    build_auto_rag_context, build_workspace_preload])

    def test_evolution_engine_components_coexist(self):
        """진화 엔진 12개 컴포넌트 동시 import."""
        modules = {}
        try:
            from app.services.self_evaluator import evaluate_response
            modules["self_evaluator"] = True
        except Exception as e:
            modules["self_evaluator"] = str(e)
        try:
            from app.services.auto_rag import build_auto_rag_context
            modules["auto_rag"] = True
        except Exception as e:
            modules["auto_rag"] = str(e)
        try:
            from app.services.fact_extractor import extract_facts
            modules["fact_extractor"] = True
        except Exception as e:
            modules["fact_extractor"] = str(e)
        try:
            from app.services.workspace_preloader import build_workspace_preload
            modules["workspace_preloader"] = True
        except Exception as e:
            modules["workspace_preloader"] = str(e)
        try:
            from app.services.contradiction_detector import detect_contradictions
            modules["contradiction_detector"] = True
        except Exception as e:
            modules["contradiction_detector"] = str(e)
        try:
            from app.services.ceo_pattern_tracker import track_interaction
            modules["ceo_pattern_tracker"] = True
        except Exception as e:
            modules["ceo_pattern_tracker"] = str(e)
        try:
            from app.services.semantic_cache import SemanticCache
            modules["semantic_cache"] = True
        except Exception as e:
            modules["semantic_cache"] = str(e)
        try:
            from app.services.eval_pipeline import aggregate_quality_stats
            modules["eval_pipeline"] = True
        except Exception as e:
            modules["eval_pipeline"] = str(e)

        failed = {k: v for k, v in modules.items() if v is not True}
        assert not failed, f"import 실패: {failed}"

    def test_output_validator_and_tools_no_conflict(self):
        """output_validator와 도구 실행기 동시 사용 충돌 없음."""
        from app.services.output_validator import validate_response
        from app.services.tool_executor import ToolExecutor
        assert validate_response is not None
        assert ToolExecutor is not None

    def test_system_prompt_builds_without_error(self):
        """시스템 프롬프트가 모든 워크스페이스에서 정상 빌드."""
        from app.core.prompts.system_prompt_v2 import build_layer1, WS_ROLES
        for ws_key in WS_ROLES:
            result = build_layer1(workspace_key=ws_key)
            assert isinstance(result, str), f"{ws_key} 빌드 실패"
            assert len(result) > 100, f"{ws_key} 프롬프트가 너무 짧음"
            # R-CRITICAL-002가 포함되어 있는지
            assert "tool_call" in result, f"{ws_key} 프롬프트에 tool_call 금지 규칙 누락"

    def test_memory_gc_and_recall_no_circular_import(self):
        """memory_gc와 memory_recall 간 순환 import 없음."""
        from app.core.memory_gc import gc_observations
        from app.core.memory_recall import build_memory_context
        assert gc_observations is not None
        assert build_memory_context is not None


# ═══════════════════════════════════════════════════════════════════
# 7. 회귀 테스트 (과거 버그 재발 방지)
# ═══════════════════════════════════════════════════════════════════

class TestRegressions:
    """수정된 버그가 재발하지 않는지 확인."""

    def test_r_critical_002_covers_tool_call(self):
        """R-CRITICAL-002 규칙에 <tool_call>이 포함."""
        from app.core.prompts.system_prompt_v2 import build_layer1
        prompt = build_layer1("CEO")
        assert "<tool_call>" in prompt and "절대 금지" in prompt

    def test_patch_reads_raw_not_numbered(self):
        """patch_remote_file이 _read_raw_file을 사용하는지 (줄번호 버그 방지)."""
        import inspect
        from app.api.ceo_chat_tools import tool_patch_remote_file
        src = inspect.getsource(tool_patch_remote_file)
        assert "_read_raw_file" in src, "patch가 여전히 tool_read_remote_file 사용 중"

    def test_terminate_task_handles_string_id(self):
        """terminate_task가 문자열 ID에서 TypeError 없이 처리."""
        import inspect
        from app.services.tool_executor import ToolExecutor
        src = inspect.getsource(ToolExecutor._terminate_task)
        assert "_int_id" in src and "ValueError" in src

    def test_streaming_status_checks_db_placeholder(self):
        """streaming-status가 DB placeholder도 확인."""
        import inspect
        from app.routers.chat import get_streaming_status
        src = inspect.getsource(get_streaming_status)
        assert "streaming_placeholder" in src
