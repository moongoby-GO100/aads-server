"""모델별 폴백 저장과 런타임 회귀 검증."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import llm_models as api
from app.services import model_selector as ms


class Connection:
    def __init__(self, rows=(), registered=True):
        self.rows = list(rows)
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=self.rows)
        self.fetchval = AsyncMock(return_value=registered)

    @asynccontextmanager
    async def transaction(self):
        yield self


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture(autouse=True)
def cache():
    ms.invalidate_fallback_chain_cache()
    yield
    ms.invalidate_fallback_chain_cache()


def row(provider, model):
    return dict(provider=provider, model_id=model, registry_model_id=model,
                execution_model_id=model, metadata={})


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit,expected', [(True, ['gemini-test']), (False, [])])
async def test_candidates_explicit_provider_and_self(monkeypatch, explicit, expected):
    rows = [row('codex', 'source'), row('gemini', 'gemini-test')]
    conn = Connection(rows)
    monkeypatch.setattr('app.core.db_pool.get_pool', lambda: Pool(conn))
    monkeypatch.setattr(ms, '_per_model_fallback_rows', AsyncMock(return_value=rows if explicit else []))
    monkeypatch.setattr(ms, '_resolve_registered_model_alias', AsyncMock(return_value=('source', None)))
    assert await ms._configured_llm_fallback_candidates('source', {'source', 'gemini-test'}) == expected


@pytest.mark.asyncio
async def test_cache_ttl_and_invalidation(monkeypatch):
    conn = Connection([row('gemini', 'target')])
    monkeypatch.setattr('app.core.db_pool.get_pool', lambda: Pool(conn))
    monkeypatch.setattr(ms, '_model_fallback_route_key', AsyncMock(return_value='llm_fallback:codex/source'))
    import time
    now = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    await ms._per_model_fallback_rows('source')
    await ms._per_model_fallback_rows('source')
    assert conn.fetch.await_count == 1
    now[0] += 16
    await ms._per_model_fallback_rows('source')
    assert conn.fetch.await_count == 2
    ms.invalidate_fallback_chain_cache()
    await ms._per_model_fallback_rows('source')
    assert conn.fetch.await_count == 3


@pytest.mark.parametrize('pinned', [True, False])
def test_codex_chain_priority(pinned):
    assert ms._codex_fallback_plan('source', pinned=pinned, configured=['gemini-test', 'source'],
                                   defaults=['unused'], per_model=True) == ['gemini-test']


@pytest.mark.asyncio
@pytest.mark.parametrize('chain,registered', [
    ([{'provider': 'codex', 'model_id': 'source'}], True),
    ([{'provider': 'gemini', 'model_id': 'target'}] * 2, True),
    ([{'provider': 'gemini', 'model_id': 'missing'}], False),
])
async def test_put_rejects_invalid(monkeypatch, chain, registered):
    conn = Connection(registered=registered)
    monkeypatch.setattr(api, 'get_pool', lambda: Pool(conn))
    monkeypatch.setattr(api, '_ensure_model_routing_preferences_table', AsyncMock())
    with pytest.raises(HTTPException) as error:
        await api.update_fallback_chain('codex', 'source', api.FallbackChainUpdate(chain=chain))
    assert error.value.status_code == 422
    assert not any('DELETE' in call.args[0] for call in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_put_only_replaces_selected_route_and_invalidates(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(api, 'get_pool', lambda: Pool(conn))
    monkeypatch.setattr(api, '_ensure_model_routing_preferences_table', AsyncMock())
    ms._fallback_rows_cache['old'] = (0, [])
    result = await api.update_fallback_chain('codex', 'source', api.FallbackChainUpdate(chain=[
        {'provider': 'gemini', 'model_id': 'target', 'display_order': 1},
    ]))
    assert result['chain'][0]['model_id'] == 'target'
    calls = conn.execute.call_args_list
    assert calls[1].args == ('DELETE FROM model_routing_preferences WHERE route_key = $1', 'llm_fallback:codex/source')
    assert 'FALSE' in calls[2].args[0]
    assert not ms._fallback_rows_cache


@pytest.mark.asyncio
async def test_auto_routing_preserves_fields_and_get_chains(monkeypatch):
    prefs = [dict(route_key=route, provider=provider, model_id=model, display_name=model,
                  is_default=True, is_enabled=True, availability='available', display_order=order)
             for route, provider, model, order in [
                 ('llm', 'anthropic', 'default', 9), ('intent_code', 'codex', 'source', 1),
                 ('llm', 'gemini', 'target', 10), ('llm_fallback:codex/source', 'gemini', 'target', 1),
             ]]
    conn = Connection(prefs)
    conn.fetch.side_effect = [prefs, []]
    monkeypatch.setattr(api, 'get_pool', lambda: Pool(conn))
    monkeypatch.setattr(api, '_ensure_model_routing_preferences_table', AsyncMock())
    monkeypatch.setattr(api.cli_model_autoreg, 'register_chat_llm_candidates', AsyncMock())
    monkeypatch.setattr(api, '_routing_preference_payload', lambda row: row)
    result = await api.get_model_routing_preferences()
    assert set(result) == {'auto_routing', 'preferences', 'total', 'route_keys', 'route_groups',
                           'route_counts', 'default_models', 'blocked_counts', 'fallback_chain',
                           'runner_fallback_chain', 'error_models'}
    assert result['auto_routing']['default_model']['model_id'] == 'default'
    assert result['auto_routing']['intent_policies'] == [{'intent': 'code', 'provider': 'codex', 'model_id': 'source'}]
    monkeypatch.setattr(api, 'get_model_routing_preferences', AsyncMock(return_value=result))
    chains = await api.get_fallback_chains()
    assert chains['chains'][0]['chain'][0]['excluded_reason'] is None
    assert chains['global_chain'][1]['excluded_reason'] == 'global_provider_excluded:gemini'
