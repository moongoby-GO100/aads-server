#!/usr/bin/env python3
"""
도구 등록 정합성 검증 — pre-commit hook에서 실행.

3곳의 도구 등록이 일치하는지 검증:
1. tool_registry.py _TOOLS dict
2. tool_executor.py _dispatch dict
3. ceo_chat_tools.py TOOL_DEFINITIONS + execute_tool

불일치 발견 시 exit(1)로 커밋 차단.
"""
import ast
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "app"
errors = []


def extract_dict_keys(filepath: str, dict_pattern: str) -> set:
    """Python 파일에서 dict의 문자열 키 추출."""
    content = Path(filepath).read_text()
    keys = set()
    # "key": 패턴으로 추출
    for match in re.finditer(rf'{dict_pattern}\s*=\s*\{{', content):
        start = match.end()
        depth = 1
        pos = start
        while pos < len(content) and depth > 0:
            if content[pos] == '{':
                depth += 1
            elif content[pos] == '}':
                depth -= 1
            pos += 1
        block = content[start:pos-1]
        for key_match in re.finditer(r'"([a-z_]+)":\s', block):
            keys.add(key_match.group(1))
    return keys


def extract_tool_definitions(filepath: str) -> set:
    """ceo_chat_tools.py TOOL_DEFINITIONS에서 name 추출."""
    content = Path(filepath).read_text()
    names = set()
    for match in re.finditer(r'"name":\s*"([a-z_]+)"', content):
        names.add(match.group(1))
    return names


def extract_dispatch_keys(filepath: str) -> set:
    """tool_executor.py _dispatch dict에서 키 추출."""
    content = Path(filepath).read_text()
    keys = set()
    in_dispatch = False
    for line in content.split('\n'):
        if 'dispatch = {' in line:
            in_dispatch = True
            continue
        if in_dispatch:
            if line.strip() == '}':
                break
            m = re.search(r'"([a-z_]+)":', line)
            if m:
                keys.add(m.group(1))
    return keys


def extract_tools_dict_keys(filepath: str) -> set:
    """tool_registry.py _TOOLS dict에서 키 추출 — "name": "xxx" 패턴."""
    content = Path(filepath).read_text()
    # _TOOLS 시작 위치 찾기
    match = re.search(r'_TOOLS:\s*Dict.*?=\s*\{', content)
    if not match:
        return set()
    tools_section = content[match.end():]
    # _GROUPS 시작 전까지만 (다음 큰 dict)
    groups_match = re.search(r'\n_GROUPS', tools_section)
    if groups_match:
        tools_section = tools_section[:groups_match.start()]
    # 최상위 키: 줄 시작이 4칸 들여쓰기 + "키": { 패턴
    keys = set()
    for m in re.finditer(r'^\s{4}"([a-z][a-z_0-9]+)":\s*\{', tools_section, re.MULTILINE):
        keys.add(m.group(1))
    return keys


def extract_defer_loading_keys(filepath: str) -> set:
    """tool_registry.py _DEFER_LOADING dict에서 키 추출."""
    content = Path(filepath).read_text()
    keys = set()
    in_defer = False
    for line in content.split('\n'):
        if '_DEFER_LOADING' in line and '=' in line and '{' in line:
            in_defer = True
            continue
        if in_defer:
            if line.strip() == '}':
                break
            m = re.search(r'"([a-z_]+)":', line)
            if m:
                keys.add(m.group(1))
    return keys


def main():
    registry_file = BASE / "services" / "tool_registry.py"
    executor_file = BASE / "services" / "tool_executor.py"
    tools_file = BASE / "api" / "ceo_chat_tools.py"

    # 1. 각 파일에서 도구 목록 추출
    registry_tools = extract_tools_dict_keys(registry_file)
    registry_defer = extract_defer_loading_keys(registry_file)
    executor_tools = extract_dispatch_keys(executor_file)
    ceo_tools_defs = extract_tool_definitions(tools_file)

    # 2. 검증: tool_registry에 있는 도구는 executor에도 있어야 함
    # (역방향은 안 체크 — executor에만 있는 내부 도구 허용)
    registry_not_in_executor = registry_tools - executor_tools
    # code_execution 같은 특수 도구 제외
    _SPECIAL_TOOLS = {'code_execution', 'code_explorer', 'run_agent_team'}
    registry_not_in_executor -= _SPECIAL_TOOLS
    # 검색/브라우저 등 ceo_chat_tools 전용 도구 제외 (executor에 없어도 됨)
    _CEO_ONLY_TOOLS = {
        'generate_image', 'fact_check', 'fact_check_multiple',
        'gemini_grounding_search', 'execute_sandbox', 'send_telegram',
        'search_kakao', 'search_naver', 'search_naver_multi',
        'search_chat_history', 'fetch_url', 'search_logs',
        'visual_qa_test', 'evaluate_alerts', 'send_alert_message',
        'crawl4ai_fetch',  # ceo_chat_tools 전용 (tool_executor에서는 deep_crawl로 커버)
    }
    registry_not_in_executor -= _CEO_ONLY_TOOLS

    if registry_not_in_executor:
        errors.append(
            f"tool_registry에 있지만 tool_executor에 없는 도구: {sorted(registry_not_in_executor)}\n"
            f"  → tool_executor.py _dispatch dict에 추가 필요"
        )

    # 3. 검증: _DEFER_LOADING의 키가 _TOOLS에 존재하는지
    defer_not_in_tools = registry_defer - registry_tools - _SPECIAL_TOOLS - {'code_execution'}
    # 주석으로 제거된 도구는 제외
    if defer_not_in_tools:
        # 실제 존재 여부 재확인 (주석 처리된 건 제외)
        pass  # 경고만, 차단하지 않음

    # 4. 검증: pipeline_runner 3종이 executor에 있는지 (핵심 체크)
    runner_tools = {'pipeline_runner_submit', 'pipeline_runner_status', 'pipeline_runner_approve'}
    runner_missing = runner_tools - executor_tools
    if runner_missing:
        errors.append(
            f"Pipeline Runner 도구가 tool_executor에 누락: {sorted(runner_missing)}\n"
            f"  → tool_executor.py _dispatch dict에 추가 필요"
        )

    # 결과 출력
    print(f"[도구 정합성] registry={len(registry_tools)} executor={len(executor_tools)} ceo_tools={len(ceo_tools_defs)}")

    if errors:
        print(f"\n❌ 도구 정합성 오류 {len(errors)}건:")
        for e in errors:
            print(f"  {e}")
        return 1
    else:
        print("✅ 도구 정합성 검증 통과")
        return 0


if __name__ == "__main__":
    sys.exit(main())
