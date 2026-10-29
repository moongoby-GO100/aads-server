"""대화 압축의 실행 가능한 함수만 로드하여 인증/SDK 의존성을 격리한다."""
import ast
from pathlib import Path
import re
from typing import Any, Dict, List

import pytest


def load_formatter():
    tree = ast.parse(Path('app/services/model_selector.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == '_format_messages_as_text')
    namespace = dict(List=List, Dict=Dict, Any=Any, _re_mod=re,
                     _CLI_RESUME_CONTINUITY_WINDOW=20, _CLI_NO_RESUME_HISTORY_WINDOW=80)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<history>', 'exec'), namespace)
    return namespace[node.name]


FORMAT = load_formatter()
TABLE = '| 순위 | 작업 | 상태 |\n| --- | --- | --- |\n| P1 | 후속 실행 | 🟢자동 |'
CORE = TABLE + '\njob_id: runner-1234abcd\n커밋 `abcdef0`\n→ 계속 실행\n| 결과 | 승인 |'


def render(content, role='assistant', resume=False):
    return FORMAT([{'role': role, 'content': content},
                   {'role': 'user', 'content': '계속'}], has_resume=resume)


@pytest.mark.parametrize('size', [3000, 4500, 6000])
@pytest.mark.parametrize('resume', [False, True])
def test_middle_actions_survive(size, resume):
    result = render('가' * (size // 2) + '\n' + CORE + '\n' + '나' * (size // 2), resume=resume)
    for line in CORE.splitlines():
        assert line in result


@pytest.mark.parametrize('role,head,tail', [('assistant', 800, 200), ('user', 1300, 300)])
def test_plain_long_content_unchanged(role, head, tail):
    content = 'a' * 2200 + 'b' * 2200
    label = '응답' if role == 'assistant' else '지시'
    expected = content[:head] + f'\n...[{label} 축소]...' + content[-tail:]
    assert render(content, role) == f"[{'AI' if role == 'assistant' else 'CEO'}]\n{expected}\n\n[CEO]\n계속"


def test_cap_and_newest_table_and_job_priority():
    middle = '\n'.join(f'→ 오래된 결정 {i} ' + 'x' * 70 for i in range(60))
    content = 'a' * 900 + '\n' + middle + '\n' + TABLE + '\nrunner-deadbeef\n' + 'z' * 400
    result = render(content).split('\n\n[CEO]')[0][5:]
    assert len(result) <= 1000 + 1200
    assert TABLE in result
    assert 'runner-deadbeef' in result
    assert '→ 오래된 결정 59 ' in result


def test_single_user_verbatim():
    content = 'a' * 3000 + CORE + 'b' * 3000
    assert FORMAT([{'role': 'user', 'content': content}]) == content


def test_short_and_system_unchanged():
    content = 'x' * 1000
    assert render(content).startswith('[AI]\n' + content)
    assert FORMAT([{'role': 'system', 'content': CORE}]) == ''


def test_header_crossing_head_boundary():
    content = 'x' * 780 + '\n' + TABLE + '\n' + 'z' * 2500
    result = render(content)
    assert '| P1 | 후속 실행 | 🟢자동 |' in result


def test_false_positive_identifiers_and_oversize_lines():
    content = 'a' * 900 + '\nrunner-1234abcde\n`abcdef`\n' + '→ ' + 'x' * 1500 + '\n' + 'b' * 400
    assert '보존 항목' not in render(content)


def test_latest_table_precedes_older_tables_under_cap():
    tables = '\n설명\n'.join(
        TABLE + f'\n| P2 | 작업 {i} ' + 'x' * 200 + ' | 🟡 |'
        for i in range(10)
    )
    result = render('a' * 900 + '\n' + tables + '\nrunner-deadbeef\n' + 'z' * 400)
    assert '| P2 | 작업 9 ' in result
    assert '| P2 | 작업 0 ' not in result
    assert 'runner-deadbeef' in result
    assert len(result.split('\n\n[CEO]')[0][5:]) <= 2200


def test_oversize_table_keeps_latest_rows_within_cap():
    table = TABLE + '\n' + '\n'.join(f'| P1 | 항목 {i} ' + 'x' * 80 + ' | 🟢 |' for i in range(50))
    result = render('a' * 900 + '\n' + table + '\n' + 'z' * 400)
    assert '| 순위 |' in result
    assert '| P1 | 항목 49 ' in result
    assert len(result.split('\n\n[CEO]')[0][5:]) <= 2200
