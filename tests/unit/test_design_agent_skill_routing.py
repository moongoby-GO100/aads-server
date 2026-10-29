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
    assert evidence == {'name': name, 'status': 'candidate',
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
        'S3': {'design-change-impact', 'webapp-verification'},
        'S4': {'design-intake-interview', 'design-discovery', 'design-art-direction', 'design-critique'},
        'S5': {'design-interaction-prototype', 'design-critique'},
        'S6': {'webapp-verification', 'design-contrast-audit'},
        'S7': {'design-external-import', 'design-token-extraction'},
        'S8': {'design-critique', 'document-coauthoring'},
        'S9': {'webapp-verification', 'design-change-impact', 'design-contrast-audit'},
    }
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
