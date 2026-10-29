"""파일 간 선언 이동: 존재만 면제하고 모호한 경우는 계속 차단한다."""
import pytest

import app.services.code_reviewer as cr


def section(path, body, *, deleted=False):
    old = sum(line.startswith(('-', ' ')) for line in body.splitlines())
    new = sum(line.startswith(('+', ' ')) for line in body.splitlines())
    return (f'diff --git a/{path} b/{path}\n--- a/{path}\n'
            + ('+++ /dev/null\n' if deleted else f'+++ b/{path}\n')
            + f'@@ -1,{old} +1,{new} @@\n{body}')


def fixture(binding='from .sharding import shard_of', target='shard_of'):
    return (section('incremental_parallel.py', '-def shard_of(key):\n-    return key % 8\n'
                    + (f'+{binding}\n' if binding else ''))
            + section('sharding.py', f'+def {target}(key):\n+    return key % 8\n'))


def test_runner_84ca6e29_move():
    notes = {}
    assert cr._precheck_preservation_gate(fixture(), '', None, notes) is None
    assert cr._removed_preservation_symbols(fixture()) == []
    assert notes['preservation_moved_symbols'] == [
        {'symbol': 'def shard_of', 'from': 'incremental_parallel.py', 'to': 'sharding.py'}]


@pytest.mark.parametrize('binding', [None, 'from .sharding import shard_of as other',
                                    '# from .sharding import shard_of',
                                    'text = "from .sharding import shard_of"'])
def test_missing_exposure_stays_gated(binding):
    assert 'def shard_of' in cr._removed_preservation_symbols(fixture(binding))


@pytest.mark.parametrize('binding', ['import sharding as shard_of',
                                    'shard_of = sharding.shard_of',
                                    'from .sharding import (\n    shard_of,\n)'])
def test_explicit_reexposure(binding):
    diff = fixture(binding).replace('\n    shard_of,\n)', '\n+    shard_of,\n+)')
    assert cr._removed_preservation_symbols(diff) == []


@pytest.mark.parametrize('side', ['+', '-'])
def test_ambiguous_global_names_stay_gated(side):
    diff = fixture() + section('duplicate.py', f'{side}def shard_of(key):\n')
    assert 'def shard_of' in cr._removed_preservation_symbols(diff)


def test_rename_stays_gated():
    assert 'def shard_of' in cr._removed_preservation_symbols(fixture(target='stable_shard_of'))


def test_truncated_pair_stays_gated():
    notes = {}
    verdict = cr._precheck_preservation_gate(fixture(), '', None, notes, diff_truncated=True)
    assert verdict.feedback['deleted_symbols'] == ['def shard_of']
    assert 'preservation_moved_symbols' not in notes


def test_duplicate_in_truncated_tail_prevents_move():
    notes = {}
    diff = fixture() + section('duplicate.py', '+def shard_of(key):\n')
    verdict = cr._precheck_preservation_gate(diff, '', None, notes, diff_truncated=True)
    assert verdict.feedback['deleted_symbols'] == ['def shard_of']
    assert 'preservation_moved_symbols' not in notes


def test_route_move_stays_gated():
    diff = (section('old.py', '-@router.get("/old")\n+from .new import route\n')
            + section('new.py', '+@router.get("/old")\n'))
    assert '@router.get' in cr._removed_preservation_symbols(diff)


def test_deleted_source_keeps_existing_resolution():
    diff = section('old.py', '-def shard_of(key):\n', deleted=True)
    diff += section('new.py', '+def shard_of(key):\n')
    assert 'def shard_of' in cr._removed_preservation_symbols(diff)


def test_move_notes_survive_other_hard_gate():
    diff = fixture() + section('other.py', '-def public_api():\n')
    verdict = cr._precheck_preservation_gate(diff, '', None)
    assert verdict.feedback['deleted_symbols'] == ['def public_api']
    assert len(verdict.feedback['preservation_moved_symbols']) == 1


def test_move_reaches_llm_prompt_and_final_feedback():
    import asyncio
    import json
    from unittest.mock import AsyncMock, patch

    response = json.dumps(dict(verdict='APPROVE', correctness=1, security=1,
                               scope_compliance=1, preservation=1, quality=1,
                               issues=[], summary='이동 검수 완료'))
    call = AsyncMock(return_value=response)

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch.object(cr.asyncio, 'to_thread', inline), \
         patch.object(cr, '_get_review_models', AsyncMock(return_value=['codex:gpt-test'])), \
         patch.object(cr, '_call_review_model', call), \
         patch.object(cr, '_save_review_result', AsyncMock()), \
         patch('app.services.review_advisory.schedule_review_advisory'):
        verdict = asyncio.run(cr.review_code_diff(
            project='GO100', job_id='runner-cross-move-test', diff=fixture(),
            instruction='선언을 이동한다', files_changed=['incremental_parallel.py', 'sharding.py']))
    call.assert_awaited_once()
    assert 'preservation_moved_symbols' in str(call.call_args)
    assert '동작 동일성은 리뷰하라' in str(call.call_args)
    assert verdict.feedback['preservation_moved_symbols'][0]['symbol'] == 'def shard_of'
