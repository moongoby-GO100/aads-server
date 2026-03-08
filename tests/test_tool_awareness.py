"""
AADS-186A: 도구 인식 테스트 — 소스 코드 검사 방식
Python 3.6 venv 호환: 실제 임포트 없이 파일 소스 내용으로 검증.
실제 런타임 테스트는 Docker(Python 3.11) 환경에서 통합 테스트로 수행.
"""
import ast
import os
import re
import pytest

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path):
    with open(os.path.join(_BASE, rel_path)) as f:
        return f.read()


# ─── 1. system_prompt_v2.py — 프롬프트 구조 확인 ─────────────────────────────

def test_system_prompt_xml_sections():
    """XML 섹션 role/capabilities/tools_available/rules/response_guidelines 존재 확인."""
    src = _read("app/core/prompts/system_prompt_v2.py")
    required = ["<role>", "<capabilities>", "<tools_available>", "<rules>", "<response_guidelines>"]
    missing = [s for s in required if s not in src]
    assert not missing, "system_prompt_v2.py 누락 XML 섹션: %s" % missing


def test_system_prompt_tool_categories():
    """시스템 프롬프트에 도구 카테고리 6개 이상 포함 확인."""
    src = _read("app/core/prompts/system_prompt_v2.py")
    categories = ["서버 접근", "웹 검색", "파일 접근", "데이터", "운영", "실행"]
    missing = [c for c in categories if c not in src]
    assert not missing, "system_prompt_v2.py 누락 카테고리: %s" % missing


def test_system_prompt_new_workflow_tools_mentioned():
    """시스템 프롬프트에 신규 도구(inspect_service/get_all_service_status/generate_directive) 언급 확인."""
    src = _read("app/core/prompts/system_prompt_v2.py")
    for tool in ["inspect_service", "get_all_service_status", "generate_directive"]:
        assert tool in src, "system_prompt_v2.py에 %s 미언급" % tool


def test_system_prompt_build_layer1_function():
    """build_layer1 함수 정의 확인."""
    src = _read("app/core/prompts/system_prompt_v2.py")
    assert "def build_layer1(" in src, "build_layer1 함수 없음"
    assert "WS_LAYER1" in src, "WS_LAYER1 딕셔너리 없음"


def test_system_prompt_role_section():
    """<role> 섹션에 AADS CTO AI 역할 정의 포함 확인."""
    src = _read("app/core/prompts/system_prompt_v2.py")
    assert "AADS CTO AI" in src, "<role>에 AADS CTO AI 정의 없음"
    assert "6개 서비스" in src, "<role>에 6개 서비스 언급 없음"


# ─── 2. context_builder.py — system_prompt_v2 연동 확인 ──────────────────────

def test_context_builder_imports_system_prompt_v2():
    """context_builder.py가 system_prompt_v2.build_layer1 사용 확인."""
    src = _read("app/services/context_builder.py")
    assert "system_prompt_v2" in src, "context_builder가 system_prompt_v2 미임포트"
    assert "build_layer1" in src, "context_builder가 build_layer1 미사용"


def test_context_builder_removed_hardcoded_static():
    """context_builder.py에 _LAYER1_STATIC 하드코딩 문자열이 제거됐음을 확인."""
    src = _read("app/services/context_builder.py")
    assert "_LAYER1_STATIC" not in src, "context_builder에 _LAYER1_STATIC 하드코딩 잔존"


# ─── 3. tool_registry.py — 신규 도구 및 examples 확인 ────────────────────────

def test_tool_registry_new_tools_defined():
    """tool_registry.py에 신규 도구 3개 정의 확인."""
    src = _read("app/services/tool_registry.py")
    for name in ["inspect_service", "get_all_service_status", "generate_directive"]:
        assert ('"%s"' % name) in src, "tool_registry.py에 %s 미정의" % name


def test_tool_registry_workflow_group():
    """tool_registry.py workflow 그룹 정의 확인."""
    src = _read("app/services/tool_registry.py")
    assert '"workflow"' in src, "workflow 그룹 없음"
    # workflow 그룹에 3개 도구 모두 포함 확인
    workflow_match = re.search(r'"workflow":\s*\[([^\]]+)\]', src)
    assert workflow_match, "workflow 그룹 파싱 실패"
    group_content = workflow_match.group(1)
    for tool in ["inspect_service", "get_all_service_status", "generate_directive"]:
        assert tool in group_content, "workflow 그룹에 %s 없음" % tool


def test_tool_registry_input_examples_present():
    """핵심 도구에 input_examples 키 포함 확인."""
    src = _read("app/services/tool_registry.py")
    tools_with_examples = [
        "list_remote_dir", "read_remote_file", "query_database",
        "directive_create", "health_check", "web_search_brave",
        "inspect_service", "get_all_service_status", "generate_directive",
    ]
    # input_examples가 전체 파일에 충분히 등장하는지 확인
    count = src.count('"input_examples"')
    assert count >= len(tools_with_examples), (
        "input_examples 정의 수 %d < 예상 %d" % (count, len(tools_with_examples))
    )


def test_tool_registry_response_format_in_tools():
    """list_remote_dir/read_remote_file/query_database에 response_format 파라미터 확인."""
    src = _read("app/services/tool_registry.py")
    # response_format이 최소 3번 이상 나타나야 함
    count = src.count('"response_format"')
    assert count >= 3, "response_format 파라미터 정의 수 %d < 3" % count
    # enum concise/detailed 정의 확인
    assert '"concise"' in src, "concise enum 없음"
    assert '"detailed"' in src, "detailed enum 없음"


def test_tool_registry_api_format_excludes_examples():
    """ToolRegistry.get_tools()가 input_examples 제외하는 코드 존재 확인."""
    src = _read("app/services/tool_registry.py")
    assert "input_examples" in src and 'k != "input_examples"' in src, (
        "get_tools()에서 input_examples 제외 로직 없음"
    )


def test_tool_registry_existing_groups_preserved():
    """기존 system/action/search 그룹 도구 보존 확인."""
    src = _read("app/services/tool_registry.py")
    expected_tools = [
        "health_check", "dashboard_query", "task_history",  # system
        "directive_create", "query_database", "list_remote_dir",  # action
        "web_search_brave",  # search
    ]
    for tool in expected_tools:
        assert ('"%s"' % tool) in src, "기존 도구 %s 누락" % tool


# ─── 4. tool_executor.py — 신규 도구 디스패치 확인 ────────────────────────────

def test_tool_executor_dispatch_registered():
    """tool_executor.py에 신규 도구 디스패치 등록 확인."""
    src = _read("app/services/tool_executor.py")
    for name in ["inspect_service", "get_all_service_status", "generate_directive"]:
        assert name in src, "tool_executor.py에 %s 미등록" % name


def test_tool_executor_new_methods_implemented():
    """tool_executor.py에 신규 도구 메서드 구현 확인."""
    src = _read("app/services/tool_executor.py")
    for method in ["_inspect_service", "_get_all_service_status", "_generate_directive"]:
        assert ("async def %s(" % method) in src, "%s 메서드 미구현" % method


def test_tool_executor_timeout_updated():
    """도구 타임아웃이 20초로 업데이트됐는지 확인."""
    src = _read("app/services/tool_executor.py")
    assert "_TOOL_TIMEOUT = 20.0" in src, "타임아웃 20초 업데이트 미확인"


# ─── 5. intent_router.py — 신규 인텐트 확인 ──────────────────────────────────

def test_intent_router_new_intents_in_map():
    """intent_router.py INTENT_MAP에 service_inspection/all_service_status 확인."""
    src = _read("app/services/intent_router.py")
    assert '"service_inspection"' in src, "service_inspection 인텐트 미정의"
    assert '"all_service_status"' in src, "all_service_status 인텐트 미정의"


def test_intent_router_workflow_group_assigned():
    """신규 인텐트가 workflow 그룹으로 매핑됐는지 확인."""
    src = _read("app/services/intent_router.py")
    # service_inspection이 workflow 그룹을 가지는지 패턴 확인
    assert 'group": "workflow"' in src or '"workflow"' in src, (
        "workflow 그룹 매핑 없음"
    )


def test_intent_router_classify_prompt_updated():
    """분류 프롬프트에 신규 인텐트 키워드 포함 확인."""
    src = _read("app/services/intent_router.py")
    assert "service_inspection" in src, "_CLASSIFY_PROMPT에 service_inspection 없음"
    assert "all_service_status" in src, "_CLASSIFY_PROMPT에 all_service_status 없음"
    assert "서비스 점검" in src, "서비스 점검 키워드 없음"
    assert "전체 서비스 상태" in src, "전체 서비스 상태 키워드 없음"


def test_intent_router_keyword_fallback_updated():
    """_keyword_fallback에 신규 인텐트 키워드 추가 확인."""
    src = _read("app/services/intent_router.py")
    assert "service_inspection" in src and "서비스 점검" in src, (
        "_keyword_fallback에 service_inspection 키워드 미추가"
    )
    assert "all_service_status" in src and "전체 서비스 상태" in src, (
        "_keyword_fallback에 all_service_status 키워드 미추가"
    )
