"""D1.5 routing gates, file evidence, and pending migration contracts."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from app.services.design_agent import runner, skills
from app.services.design_agent.config import Config
from tests.unit.test_design_agent_skeleton import Connection

ROOT = Path(__file__).resolve().parents[2]
NEW_SKILLS = (
    'design-intake-interview', 'design-discovery', 'design-art-direction',
    'design-interaction-prototype', 'design-external-import',
    'design-project-onboarding', 'design-token-extraction',
    'design-contrast-audit', 'design-critique', 'design-change-impact',
)


async def run(monkeypatch, names, payload=None, event='manual', blocked=None):
    conn = Connection()
    conn.fetchrow.return_value = dict(id='trigger', project_key='p',
                                     event_type=event, payload=payload or {})
    conn.fetch.return_value = [{'skill_name': n} for n in names]
    monkeypatch.setattr(runner, 'check_run', AsyncMock(return_value=blocked))
    handler = AsyncMock()
    monkeypatch.setitem(runner.HANDLERS, event, handler)
    result = await runner.process_one(SimpleNamespace(acquire=lambda: conn),
                                      't', Config(enabled=True))
    return result, conn, handler


@pytest.mark.asyncio
async def test_missing_skill_blocks_handler(monkeypatch):
    result, conn, handler = await run(monkeypatch,
        ['design-critique', 'missing-design-skill'], {'situation': 'S8'})
    assert result['status'] == 'failed'
    handler.assert_not_awaited()
    update = next(c for c in conn.execute.call_args_list if 'llm_calls=$4' in c.args[0])
    assert 'missing-design-skill' in update.args[6]


@pytest.mark.asyncio
async def test_loaded_evidence(monkeypatch):
    result, conn, handler = await run(monkeypatch, ['design-critique'], {'situation': 'S8'})
    assert result['status'] == 'succeeded'
    handler.assert_awaited_once()
    update = next(c for c in conn.execute.call_args_list if 'SET skills_loaded=' in c.args[0])
    assert update.args[1:3] == ('row-id', 't')
    assert json.loads(update.args[3]) == [skills.load_skill('design-critique')]
    assert json.loads(update.args[3])[0]['version'] == '0.3.0'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'situation': 'S10'}, {'situation': None}])
async def test_manual_requires_valid_situation(monkeypatch, payload):
    result, conn, handler = await run(monkeypatch, ['design-critique'], payload)
    assert result['status'] == 'failed'
    handler.assert_not_awaited()
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['blocked_budget', 'skipped_duplicate'])
async def test_existing_gates_precede_loading(monkeypatch, blocked):
    result, conn, handler = await run(monkeypatch, ['missing'], blocked=blocked)
    assert result['status'] == blocked
    conn.fetch.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_routes_fail_closed(monkeypatch):
    result, _, handler = await run(monkeypatch, [], {'situation': 'S8'})
    assert result['status'] == 'failed'
    handler.assert_not_awaited()


@pytest.mark.parametrize('event,situation', [
    ('project_created', 'S1'), ('new_screen_requested', 'S2'),
    ('screen_change_requested', 'S3'), ('deploy_succeeded', 'S9'),
])
def test_event_routes(event, situation):
    assert skills.resolve_situation(event, {'situation': 'S7'}) == situation


@pytest.mark.parametrize('name', NEW_SKILLS)
def test_candidate_files_and_fixture(name):
    evidence = skills.load_skill(name)
    data = (skills.SKILLS_ROOT / name / 'SKILL.md').read_bytes()
    assert evidence == {'name': name, 'status': 'candidate', 'version': '0.3.0',
                        'sha256': hashlib.sha256(data).hexdigest()}
    assert '## 검증 fixture' in data.decode()


@pytest.mark.parametrize('content', [
    'no frontmatter', '---\nname: other\nstatus: candidate\n---',
    '---\nname: test\nstatus: retired\n---', '---\n[invalid\n---',
])
def test_malformed_skill_rejected(monkeypatch, tmp_path, content):
    monkeypatch.setattr(skills, 'SKILLS_ROOT', tmp_path)
    (tmp_path / 'test').mkdir()
    (tmp_path / 'test' / 'SKILL.md').write_text(content)
    with pytest.raises(ValueError, match='Skill test'):
        skills.load_skill('test')


def test_unreadable_skill_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, 'SKILLS_ROOT', tmp_path)
    (tmp_path / 'test' / 'SKILL.md').mkdir(parents=True)
    with pytest.raises(ValueError, match='Skill test'):
        skills.load_skill('test')


def test_intake_rule():
    text = (skills.SKILLS_ROOT / 'design-intake-interview' / 'SKILL.md').read_text()
    assert '문항마다 프로젝트 기준 선택 예시 3개 이상' in text


def test_pending_migration_routes():
    import re

    name = '20261101_design_agent_skill_routing.sql'
    assert name > '20261031_design_agent_skeleton.sql'
    sql = (ROOT / 'migrations' / name).read_text()
    expected = {
        'S1': {'design-intake-interview', 'design-discovery', 'design-art-direction', 'design-project-onboarding'},
        'S2': {'design-intake-interview', 'frontend-design-guide', 'design-interaction-prototype'},
        'S3': {'design-intake-interview', 'design-change-impact', 'webapp-verification'},
        'S4': {'design-intake-interview', 'design-discovery', 'design-art-direction', 'design-critique'},
        'S5': {'design-interaction-prototype', 'design-critique'},
        'S6': {'webapp-verification', 'design-contrast-audit'},
        'S7': {'design-external-import', 'design-token-extraction'},
        'S8': {'design-critique', 'document-coauthoring'},
        'S9': {'webapp-verification', 'design-change-impact', 'design-contrast-audit'},
    }
    supplement = (ROOT / 'migrations/20261103_design_agent_skill_routing_intake.sql').read_text()
    assert 'ON CONFLICT DO NOTHING' in supplement
    sql += supplement
    pairs = re.findall(r"\('(S[1-9])', '([^']+)'\)", sql)
    assert len(pairs) == sum(map(len, expected.values()))
    assert set(pairs) == {(s, n) for s, names in expected.items() for n in names}
    assert "DEFAULT '[]'::jsonb" in sql
    assert 'ON CONFLICT DO NOTHING' in sql
    assert 'DROP ' not in sql
    for names in expected.values():
        for name in names:
            skills.load_skill(name)


def test_skill_risk_is_read():
    from app.services.skill_file_import import infer_risk_tier

    for name in NEW_SKILLS:
        text = (skills.SKILLS_ROOT / name / 'SKILL.md').read_text()
        metadata = yaml.safe_load(text.split('---', 2)[1])
        assert infer_risk_tier(metadata, text)[0] == 'read'


@pytest.mark.parametrize('name', NEW_SKILLS)
def test_four_fixture_categories(name):
    text = (skills.SKILLS_ROOT / name / 'SKILL.md').read_text()
    for category in ('정상', '누락', '충돌', '실패'):
        block = text.split(f'### {category} fixture\n', 1)[1].split('### ', 1)[0]
        assert '입력:' in block and '기대:' in block


def test_intake_question_order_and_sources():
    text = (skills.SKILLS_ROOT / 'design-intake-interview' / 'SKILL.md').read_text()
    assert '- Q6 진입·이동:' in text
    assert '- Q7 실패·예외:' in text
    assert '- Q8 시각 방향:' in text
    assert 'CEO 선택 / CEO 직접 입력 / 권장값 수락 / 추정값 확인' in text
    assert 'ceo_choice / ceo_input / recommended_accepted / estimate_confirmed' in text
    for rule in ('고르면 달라지는 것', '답 수정 시 이전 확인 무효', 'blocked(brief_unconfirmed)',
                 'brainstorming 생략 조항보다 우선', 'S3 범위 질문 모드'):
        assert rule in text


def test_interaction_required_states():
    text = (skills.SKILLS_ROOT / 'design-interaction-prototype' / 'SKILL.md').read_text()
    assert '정상·로딩·빈 화면·오류·권한 없음·처리 중' in text
    for rule in ('세션 만료', '입력 보존', '중복 제출 방지', '확인/취소'):
        assert rule in text


def test_change_impact_uses_intake_and_blocks_handoff():
    text = (skills.SKILLS_ROOT / 'design-change-impact' / 'SKILL.md').read_text()
    for rule in ('질문은 소유하지 않는다', 'design-intake-interview 답변 기록(범위 질문 모드)',
                 '승인 전 인계 금지', '브리프 확인 전 시안 생성 금지',
                 'baseline_revision', 'baseline_hash', 'before_hash', 'after_hash',
                 'allowed_scope/forbidden_scope/acceptance_criteria'):
        assert rule in text
    block = text.split('scope_answers: null}', 1)[1].split('입력:', 1)[0]
    assert '시안 0개' in block and 'next_action=ask' in block


@pytest.mark.parametrize('version', [None, '', '0.2', 'v0.2.0', '0.2.0-extra', 2])
def test_missing_or_invalid_version_rejected(monkeypatch, tmp_path, version):
    monkeypatch.setattr(skills, 'SKILLS_ROOT', tmp_path)
    target = tmp_path / 'design-test' / 'SKILL.md'
    target.parent.mkdir()
    metadata = {'name': 'design-test', 'status': 'candidate'}
    if version is not None:
        metadata['version'] = version
    target.write_text('---\n' + yaml.safe_dump(metadata) + '---\n# 테스트\n')
    with pytest.raises(ValueError, match='version'):
        skills.load_skill('design-test')


@pytest.mark.asyncio
async def test_s3_required_skills_include_intake():
    conn = Connection()
    conn.fetch.return_value = [{'skill_name': name} for name in
        ('design-change-impact', 'design-intake-interview', 'webapp-verification')]
    assert 'design-intake-interview' in await skills.required_skills(conn, 'S3')
    assert conn.fetch.call_args.args[1] == 'S3'
    assert 'required=true' in conn.fetch.call_args.args[0]


@pytest.mark.asyncio
async def test_invalid_version_blocks_handler(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, 'SKILLS_ROOT', tmp_path)
    folder = tmp_path / 'test'
    folder.mkdir()
    (folder / 'SKILL.md').write_text('---\nname: test\nstatus: candidate\nversion: invalid\n---\n')
    result, conn, handler = await run(monkeypatch, ['test'], {'situation': 'S3'})
    assert result['status'] == 'failed'
    handler.assert_not_awaited()
    assert not any('SET skills_loaded=' in c.args[0] for c in conn.execute.call_args_list)


def test_version_evidence_tracks_file_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, 'SKILLS_ROOT', tmp_path)
    folder = tmp_path / 'test'
    folder.mkdir()
    path = folder / 'SKILL.md'
    path.write_text('---\nname: test\nstatus: candidate\nversion: 1.2.3\n---\n')
    first = skills.load_skill('test')
    path.write_text(path.read_text().replace('1.2.3', '1.2.4'))
    second = skills.load_skill('test')
    assert first['version'] == '1.2.3' and second['version'] == '1.2.4'
    assert first['sha256'] != second['sha256']


@pytest.mark.parametrize('name', [
    'frontend-design-guide', 'webapp-verification', 'document-coauthoring',
])
def test_common_skill_without_version(name):
    evidence = skills.load_skill(name)
    assert evidence == {
        'name': name, 'status': 'candidate', 'version': None,
        'sha256': hashlib.sha256((skills.SKILLS_ROOT / name / 'SKILL.md').read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize('version', [None, '', '0.2', 'v0.2.0', '0.2.0-extra', 2, True])
def test_common_explicit_invalid_version_rejected(monkeypatch, tmp_path, version):
    monkeypatch.setattr(skills, 'SKILLS_ROOT', tmp_path)
    target = tmp_path / 'test' / 'SKILL.md'
    target.parent.mkdir()
    metadata = {'name': 'test', 'status': 'candidate', 'version': version}
    target.write_text('---\n' + yaml.safe_dump(metadata) + '---\n')
    with pytest.raises(ValueError, match='version'):
        skills.load_skill('test')
