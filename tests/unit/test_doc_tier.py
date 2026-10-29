import pytest

from app.services.doc_tier import compute_doc_tier, provider_of, revision_metadata, settings


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    for name in ('LONG_CHARS', 'SPLIT_CHARS', 'GOAL_LINK_ENABLED', 'D3_DAILY_LIMIT'):
        monkeypatch.delenv('AADS_DOC_TIER_' + name, raising=False)


@pytest.mark.parametrize('kind', ['prd', 'contract', 'architecture', 'plan', 'design', 'spec', 'tasks', 'report', 'reference'])
@pytest.mark.parametrize('count', [14999, 15000, 15001, 50000, 50001])
@pytest.mark.parametrize('goal', [False, True])
def test_boundaries(kind, count, goal):
    expected = 'SPLIT' if count > 50000 else (
        'D3' if count > 15000 or kind in {'prd', 'contract', 'architecture'} else (
            'D2' if kind in {'plan', 'design', 'spec', 'tasks'} else 'D1'))
    assert compute_doc_tier(kind, count, goal) == expected


@pytest.mark.parametrize('model,provider', [
    ('claude-sonnet-5-5', 'anthropic'), ('claude-opus-5-5', 'anthropic'),
    ('claude-fable-5-1', 'anthropic'), ('codex:gpt-6.1-sol', 'openai'),
    ('codex:gpt-6-astra', 'openai'), ('codex:gpt-6-luna', 'openai'),
    ('codex:gpt-5.6-luna', 'openai'), ('claude:opus', 'anthropic'),
    ('litellm:gemini', 'other:litellm'), ('gemini', 'other:gemini'),
    ('', 'unknown'), (None, 'unknown'), ('unknown', 'unknown'),
])
def test_providers(model, provider):
    assert provider_of(model) == provider


def test_environment(monkeypatch):
    monkeypatch.setenv('AADS_DOC_TIER_LONG_CHARS', '10')
    monkeypatch.setenv('AADS_DOC_TIER_SPLIT_CHARS', '20')
    monkeypatch.setenv('AADS_DOC_TIER_GOAL_LINK_ENABLED', 'true')
    monkeypatch.setenv('AADS_DOC_TIER_D3_DAILY_LIMIT', '3')
    assert compute_doc_tier('report', 11) == 'D3'
    assert compute_doc_tier('report', 21) == 'SPLIT'
    assert compute_doc_tier('report', 1, True) == 'D3'
    assert settings()['d3_daily_limit'] == 3


def test_legacy_metadata_is_not_persisted():
    row = {'content': '한글', 'doc_tier': None}
    result = revision_metadata(row, 'report')
    assert result['doc_tier'] is None
    assert result['doc_tier_computed'] == 'D1'
    assert row == {'content': '한글', 'doc_tier': None}


def test_provider_non_string_is_unknown():
    for model in (0, 1, True, [], {}, object()):
        assert provider_of(model) == 'unknown'
