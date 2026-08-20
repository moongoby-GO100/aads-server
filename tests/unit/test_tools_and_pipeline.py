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
from uuid import uuid4

# 프로젝트 루트
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

# ═══════════════════════════════════════════════════════════════════
# 1. 도구 함수 단위 테스트
# ═══════════════════════════════════════════════════════════════════


class TestToolTimeouts:
    """도구 실행 타임아웃 분류 테스트."""

    BROWSER_TOOL_NAMES = (
        "browser_connect",
        "browser_navigate",
        "browser_snapshot",
        "browser_screenshot",
        "browser_click",
        "browser_fill",
        "browser_press_key",
        "browser_select_option",
        "browser_check",
        "browser_upload_file",
        "browser_download",
        "browser_tab_list",
    )

    def test_browser_tools_have_cdp_timeout_budget(self):
        from app.services import tool_executor

        assert tool_executor._TOOL_TIMEOUT == 20.0
        assert tool_executor._BROWSER_TOOL_TIMEOUT >= 210.0
        assert tool_executor._BROWSER_TOOL_TIMEOUT > tool_executor._LONG_TOOL_TIMEOUT
        for tool_name in self.BROWSER_TOOL_NAMES:
            assert tool_name in tool_executor._BROWSER_TOOLS

    def test_browser_intent_exposes_all_browser_tools(self):
        from app.services import tool_executor
        from app.services.tool_registry import INTENT_REQUIRED_TOOLS

        for tool_name in self.BROWSER_TOOL_NAMES:
            assert tool_name in tool_executor._INTENT_TOOL_MAP["browser"]
            assert tool_name in tool_executor._INTENT_TOOL_MAP["browser_action"]
            assert tool_name in INTENT_REQUIRED_TOOLS["browser"]


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
        assert "[AADS 명령 실행" in result

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self):
        """위험 명령(rm -rf /)이 차단되는지 확인."""
        from app.api.ceo_chat_tools import tool_run_remote_command
        result = await tool_run_remote_command("AADS", "rm -rf /")
        # 차단 또는 에러 반환
        assert "[ERROR]" in result or "차단" in result or "금지" in result


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

    def test_report_quality_rejects_thin_analysis(self):
        from app.services.output_validator import validate_response

        response = (
            "반영은 되어 있습니다. 다만 품질은 더 개선할 수 있습니다. "
            "추가로 프롬프트를 보강하면 됩니다."
        )
        result = validate_response(
            response_text=response,
            tools_called=True,
            intent="cto_strategy",
        )

        assert result.is_valid is False
        assert result.violation_type == "REPORT_STRUCTURE_WEAK"
        assert "문제점" in result.retry_prompt
        assert "완료기준" in result.retry_prompt

    def test_report_quality_accepts_structured_analysis(self):
        from app.services.output_validator import validate_response

        response = """
**요약** — 핵심 문제는 보고서 본문이 형식만 갖추고 판단 근거가 약한 점입니다.

## 문제점/리스크
| 항목 | 상태 | 근거 |
|---|---|---|
| 보고 품질 | 부족 | [DB 조회] |
| 사용자 만족 | 위험 | [최근 응답 샘플] |
| 완료 판단 | 불명확 | [코드 확인] |

## 원인/근거
원인은 L4 규칙이 문제점과 개선안을 강제하지 않는 구조입니다. 검증은 DB와 코드 기준으로 확인했습니다.

## 개선 권장안
권장안은 프롬프트 보강, validator 재작성 트리거, 샘플 회귀 테스트 순서입니다.

## 검증 방법/완료기준
완료기준은 구조화 응답이 통과하고 빈약 응답이 REPORT_STRUCTURE_WEAK로 차단되는 것입니다.

→ 다음 단계: 운영 DB 마이그레이션 적용 후 pytest로 회귀 검증합니다.
"""
        result = validate_response(
            response_text=response,
            tools_called=True,
            intent="cto_strategy",
        )

        assert result.is_valid is True

    def test_status_report_quality_rejects_thin_next_step_report(self):
        from app.services.output_validator import validate_response

        response = "DB에는 저장되어 있습니다. 화면 표시 패치가 우선입니다. → 다음 단계: 즉시 패치합니다."
        result = validate_response(
            response_text=response,
            tools_called=True,
            intent="status_check",
        )

        assert result.is_valid is False
        assert result.violation_type == "PROGRESS_ONLY_RESPONSE"

    def test_status_report_quality_accepts_compact_structured_report(self):
        from app.services.output_validator import validate_response

        response = """
**현황** — 최신 응답은 DB에는 저장됐지만 화면 표시 경로가 불안정합니다.

| 문제점 | 원인/근거 | 권장 조치 | 검증 |
|---|---|---|---|
| 응답 미노출 | 렌더 필터와 SSE 병합 경합 [코드 확인] | 표시 필터 완화 | URL 재확인 |
| 다음 단계 부족 | 보고 템플릿 미강제 [코드 확인] | validator 강화 | 회귀 테스트 |
| 완료판정 불명확 | 배포/커밋 상태 누락 [git status] | 완료상태 보정 | ledger 확인 |

→ 다음 단계: validator 회귀 테스트와 배포 후 동일 URL을 재검증합니다.
"""
        result = validate_response(
            response_text=response,
            tools_called=True,
            intent="status_check",
        )

        assert result.is_valid is True


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

    def test_r_critical_002_covers_fabrication_rules(self):
        """R-CRITICAL-002 규칙: 시스템 프롬프트에 행동 원칙 포함."""
        from app.core.prompts.system_prompt_v2 import build_layer1
        prompt = build_layer1("CEO")
        assert "금지" in prompt, "시스템 프롬프트에 금지 규칙 누락"

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

    def test_terminate_task_cleans_terminal_runner_pid(self):
        """error/cancelled 상태여도 살아있는 로컬 runner_pid를 정리."""
        import inspect
        from app.services.tool_executor import ToolExecutor

        src = inspect.getsource(ToolExecutor._terminate_task)
        assert "runner_pid" in src
        assert "_local_pid_alive" in src
        assert "_terminate_local_process_tree" in src
        assert "\"cancelled\"" in src

    def test_streaming_status_checks_db_placeholder(self):
        """streaming-status가 DB placeholder도 확인."""
        import inspect
        from app.routers.chat import get_streaming_status
        src = inspect.getsource(get_streaming_status)
        assert "streaming_placeholder" in src

    def test_last_response_settles_stale_running_execution(self):
        """last-response가 죽은 running 실행 때문에 최종 응답 복구를 막지 않음."""
        import inspect
        from app.routers.chat import get_last_response
        src = inspect.getsource(get_last_response)
        assert "_settle_stale_execution_for_recovery" in src
        assert "stale running execution settled by recovery endpoint" in inspect.getsource(
            __import__("app.routers.chat", fromlist=["_settle_stale_execution_for_recovery"])._settle_stale_execution_for_recovery
        )

    def test_final_save_promotes_execution_placeholder_before_old_assistant(self):
        """최종 저장은 이전 assistant_message_id보다 execution placeholder를 우선 승격."""
        import inspect
        from app.services.chat_service import _save_and_update_session

        src = inspect.getsource(_save_and_update_session)
        placeholder_select = "WHERE execution_id = $1\n                              AND intent = 'streaming_placeholder'"
        assistant_select = "SELECT assistant_message_id FROM chat_turn_executions WHERE id = $1"
        assert placeholder_select in src
        assert src.index(placeholder_select) < src.index(assistant_select)
        assert "ELSE $2::uuid" in src

    @pytest.mark.asyncio
    async def test_settle_stale_execution_recovers_recent_progress_without_live_runtime(self, monkeypatch):
        """메모리상 live runtime이 없으면 recent partial도 recovery가 정리."""
        from app.routers import chat as chat_router

        class FakeConn:
            def __init__(self):
                self.execute_calls = []
                self.fetchval_calls = []

            async def fetchval(self, query, *args):
                self.fetchval_calls.append(query)
                if "UPDATE chat_messages" in query:
                    return uuid4()
                return None

            async def execute(self, query, *args):
                self.execute_calls.append(query)

        monkeypatch.setattr(chat_router.svc, "normalize_tool_events", lambda tools: tools or [])
        monkeypatch.setattr(chat_router.svc, "_strip_streaming_progress_markers", lambda text: text)
        monkeypatch.setattr(chat_router.svc, "_has_meaningful_partial_content", lambda text: bool((text or "").strip()))
        monkeypatch.setattr(chat_router.svc, "_FIRST_RESPONSE_TIMEOUT_SEC", 120, raising=False)

        settled = await chat_router._settle_stale_execution_for_recovery(
            FakeConn(),
            uuid4(),
            {
                "status": "running",
                "partial_content": "partial answer",
                "tools_called": [],
                "last_event_id": "1778546800-0",
                "updated_age_seconds": 25,
                "updated_recently": True,
                "execution_id": str(uuid4()),
            },
            has_live_runtime=False,
        )

        assert settled is not None
        assert settled["just_completed"] is True

    @pytest.mark.asyncio
    async def test_recovery_auto_resume_restores_retrying_execution(self, monkeypatch):
        """recovery 정리 후 retry budget이 있으면 자동 이어쓰기를 예약."""
        from app.routers import chat as chat_router

        execution_id = uuid4()
        session_id = uuid4()
        assistant_id = uuid4()
        calls = {"execute": [], "fetchval": [], "resumed": []}

        class FakeConn:
            async def fetchrow(self, query, *args):
                return {
                    "retry_count": 0,
                    "requested_model": "gpt-5.5",
                    "last_user_msg": "원래 질문",
                    "workspace_name": "CEO",
                }

            async def fetchval(self, query, *args):
                calls["fetchval"].append((query, args))
                if "UPDATE chat_turn_executions" in query:
                    return execution_id
                return None

            async def execute(self, query, *args):
                calls["execute"].append((query, args))

        async def fake_resume(*args, **kwargs):
            calls["resumed"].append((args, kwargs))

        monkeypatch.setattr(chat_router.svc, "_strip_streaming_progress_markers", lambda text: text)
        monkeypatch.setattr(chat_router.svc, "_resume_single_stream", fake_resume)

        scheduled = await chat_router._schedule_recovery_auto_resume(
            FakeConn(),
            session_id,
            execution_id,
            assistant_id,
            "partial answer\n\n_(응답이 중단되어 여기까지 보존되었습니다.)_",
        )

        await chat_router.asyncio.sleep(0)

        assert scheduled is True
        assert calls["resumed"]
        assert any("status = 'retrying'" in query for query, _ in calls["fetchval"])
        assert any("current_execution_id" in query for query, _ in calls["execute"])

    @pytest.mark.asyncio
    async def test_interrupted_auto_resume_schedules_completion_gate_retry(self, monkeypatch):
        """validator/contract 중단은 자동 완료보고 이어쓰기로 전환."""
        from app.services import chat_service

        execution_id = uuid4()
        session_id = uuid4()
        assistant_id = uuid4()
        calls = {"execute": [], "fetchval": [], "resumed": []}

        class FakeTask:
            def add_done_callback(self, callback):
                self.callback = callback

        class FakeConn:
            async def fetchrow(self, query, *args):
                return {
                    "retry_count": 0,
                    "requested_model": "gpt-5.5",
                    "current_execution_id": execution_id,
                    "last_user_msg": "원래 질문",
                    "workspace_name": "CEO",
                }

            async def fetchval(self, query, *args):
                calls["fetchval"].append((query, args))
                if "UPDATE chat_turn_executions" in query:
                    return execution_id
                return None

            async def execute(self, query, *args):
                calls["execute"].append((query, args))

        async def fake_resume(*args, **kwargs):
            calls["resumed"].append((args, kwargs))

        def fake_create_task(coro):
            # Close coroutine to avoid un-awaited warnings; this test only verifies scheduling args.
            coro.close()
            calls["resumed"].append(((), {}))
            return FakeTask()

        monkeypatch.setattr(chat_service, "_strip_streaming_progress_markers", lambda text: text)
        monkeypatch.setattr(chat_service, "_resume_single_stream", fake_resume)
        monkeypatch.setattr(chat_service._heartbeat_asyncio, "create_task", fake_create_task)

        scheduled = await chat_service._schedule_interrupted_auto_resume(
            FakeConn(),
            str(session_id),
            str(execution_id),
            assistant_id,
            "진행 로그만 있는 부분 응답",
            "output_validator_autonomous_failed:PROGRESS_ONLY_RESPONSE",
        )

        assert scheduled is True
        assert calls["resumed"]
        assert any("status = 'retrying'" in query for query, _ in calls["fetchval"])
        assert any("current_execution_id" in query for query, _ in calls["execute"])

    @pytest.mark.asyncio
    async def test_api_shutdown_auto_resume_does_not_consume_retry_budget(self, monkeypatch):
        """배포/프로세스 종료 중단은 응답 품질 실패가 아니므로 자동 재개하되 retry_count를 올리지 않는다."""
        from app.services import chat_service

        execution_id = uuid4()
        session_id = uuid4()
        assistant_id = uuid4()
        calls = {"execute": [], "fetchval": [], "resumed": []}

        class FakeTask:
            def add_done_callback(self, callback):
                self.callback = callback

        class FakeConn:
            async def fetchrow(self, query, *args):
                return {
                    "retry_count": 4,
                    "requested_model": "gpt-5.5",
                    "current_execution_id": execution_id,
                    "last_user_msg": "원래 질문",
                    "workspace_name": "CEO",
                }

            async def fetchval(self, query, *args):
                calls["fetchval"].append((query, args))
                if "UPDATE chat_turn_executions" in query:
                    return execution_id
                return None

            async def execute(self, query, *args):
                calls["execute"].append((query, args))

        async def fake_resume(*args, **kwargs):
            calls["resumed"].append((args, kwargs))

        def fake_create_task(coro):
            coro.close()
            calls["resumed"].append(((), {}))
            return FakeTask()

        monkeypatch.setattr(chat_service, "_strip_streaming_progress_markers", lambda text: text)
        monkeypatch.setattr(chat_service, "_resume_single_stream", fake_resume)
        monkeypatch.setattr(chat_service._heartbeat_asyncio, "create_task", fake_create_task)

        scheduled = await chat_service._schedule_interrupted_auto_resume(
            FakeConn(),
            str(session_id),
            str(execution_id),
            assistant_id,
            "배포 중 보존된 부분 응답",
            "api_shutdown_before_process_stop",
        )

        assert scheduled is True
        update_calls = [
            args for query, args in calls["fetchval"]
            if "UPDATE chat_turn_executions" in query
        ]
        assert update_calls
        assert update_calls[0][4] is True
        assert update_calls[0][5] == 8
        assert calls["resumed"]
        assert any("current_execution_id" in query for query, _ in calls["execute"])

    @pytest.mark.asyncio
    async def test_settle_stale_execution_keeps_recent_live_runtime(self, monkeypatch):
        """실제 live runtime이 있으면 recent running execution은 유지."""
        from app.routers import chat as chat_router

        class FakeConn:
            async def fetchval(self, query, *args):
                raise AssertionError("recent live execution should not mutate DB")

            async def execute(self, query, *args):
                raise AssertionError("recent live execution should not mutate DB")

        monkeypatch.setattr(chat_router.svc, "normalize_tool_events", lambda tools: tools or [])
        monkeypatch.setattr(chat_router.svc, "_strip_streaming_progress_markers", lambda text: text)
        monkeypatch.setattr(chat_router.svc, "_has_meaningful_partial_content", lambda text: bool((text or "").strip()))
        monkeypatch.setattr(chat_router.svc, "_FIRST_RESPONSE_TIMEOUT_SEC", 120, raising=False)

        settled = await chat_router._settle_stale_execution_for_recovery(
            FakeConn(),
            uuid4(),
            {
                "status": "running",
                "partial_content": "partial answer",
                "tools_called": [],
                "last_event_id": "1778546800-0",
                "updated_age_seconds": 25,
                "updated_recently": True,
                "execution_id": str(uuid4()),
            },
            has_live_runtime=True,
        )

        assert settled is None

    def test_tool_executor_all_tools_callable(self):
        """ToolExecutor의 모든 도구 매핑이 실제 callable 메서드를 참조하는지 검증.

        check_tool_consistency --fix 등 자동 생성 코드가 클래스 밖에
        메서드를 놓으면 self._method 참조 실패 → 이 테스트가 잡음.
        """
        from app.services.tool_executor import ToolExecutor
        import inspect
        import re

        # _dispatch 메서드 소스에서 self._xxx 참조 추출
        src = inspect.getsource(ToolExecutor._dispatch)
        tool_refs = set(re.findall(r'self\.(_\w+)', src))
        # _dispatch 자체 제거
        tool_refs.discard('_dispatch')

        missing = []
        for ref in tool_refs:
            if not hasattr(ToolExecutor, ref):
                missing.append(ref)

        assert not missing, (
            f"ToolExecutor에 {len(missing)}개 메서드 누락 (클래스 밖 정의 의심): "
            f"{missing}"
        )
        assert len(tool_refs) > 10, (
            f"도구 매핑이 너무 적음 ({len(tool_refs)}개) — 파싱 오류 확인 필요"
        )
