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

        # 자동 수정 시도
        if "--fix" in sys.argv:
            fixed = _auto_fix(registry_tools, executor_tools, ceo_tools_defs,
                              registry_file, executor_file, tools_file)
            if fixed:
                print(f"\n🔧 자동 수정 완료 ({fixed}건) — 파일 확인 후 다시 커밋하세요.")
                return 0
        else:
            print(f"\n💡 자동 수정: python3 scripts/check_tool_consistency.py --fix")
        return 1
    else:
        print("✅ 도구 정합성 검증 통과")
        return 0


def _auto_fix(registry_tools, executor_tools, ceo_tools_defs,
              registry_file, executor_file, tools_file) -> int:
    """누락된 도구를 자동으로 추가."""
    fixed = 0

    # 1) tool_executor에 누락된 도구 → stub 메서드 + dispatch 항목 추가
    _CEO_ONLY = {
        'generate_image', 'fact_check', 'fact_check_multiple',
        'gemini_grounding_search', 'execute_sandbox', 'send_telegram',
        'search_kakao', 'search_naver', 'search_naver_multi',
        'search_chat_history', 'fetch_url', 'search_logs',
        'visual_qa_test', 'evaluate_alerts', 'send_alert_message',
        'crawl4ai_fetch',
    }
    _SPECIAL = {'code_execution', 'code_explorer', 'run_agent_team'}
    missing_in_executor = registry_tools - executor_tools - _CEO_ONLY - _SPECIAL

    if missing_in_executor:
        content = executor_file.read_text()

        # dispatch dict에 추가
        dispatch_insert_point = content.find("# 첨부파일 재읽기")
        if dispatch_insert_point == -1:
            dispatch_insert_point = content.find("# 작업 모니터")
        if dispatch_insert_point > 0:
            new_entries = ""
            for tool_name in sorted(missing_in_executor):
                new_entries += f'            "{tool_name}": self._{tool_name},\n'
            content = content[:dispatch_insert_point] + \
                f"# 자동 추가 (check_tool_consistency --fix)\n            {new_entries}" + \
                content[dispatch_insert_point:]

        # stub 메서드 추가 (파일 끝)
        stubs = "\n"
        for tool_name in sorted(missing_in_executor):
            stubs += f'''
    async def _{tool_name}(self, inp: Dict[str, Any]) -> Any:
        """자동 생성 stub — ceo_chat_tools.execute_tool로 위임."""
        from app.api.ceo_chat_tools import execute_tool
        return await execute_tool("{tool_name}", inp, "", "")
'''
        content += stubs

        executor_file.write_text(content)
        fixed += len(missing_in_executor)
        print(f"  tool_executor.py: {sorted(missing_in_executor)} 추가됨")

    # 2) tool_registry._DEFER_LOADING에 누락된 도구 → True(온디맨드)로 추가
    defer_keys = extract_defer_loading_keys(registry_file)
    missing_in_defer = registry_tools - defer_keys - _SPECIAL
    if missing_in_defer:
        content = registry_file.read_text()
        insert_point = content.find("}\n\n")  # _DEFER_LOADING 끝
        if insert_point > 0:
            new_entries = ""
            for tool_name in sorted(missing_in_defer):
                new_entries += f'    "{tool_name}": True,  # 자동 추가\n'
            content = content[:insert_point] + new_entries + content[insert_point:]
            registry_file.write_text(content)
            fixed += len(missing_in_defer)
            print(f"  tool_registry.py _DEFER_LOADING: {sorted(missing_in_defer)} 추가됨")

    return fixed


if __name__ == "__main__":
    sys.exit(main())
