"""쓰기 경로의 실측/확인 게이트 및 감사 정보 비노출(mock 네트워크)."""
import logging
from unittest.mock import AsyncMock

import pytest

from app.services import clobe_mcp_client as clobe


@pytest.fixture
def env(monkeypatch):
    observed = {'update_memo', 'issue_tax_invoice', 'preview_tax_invoice', 'delete_employee'}
    pool = AsyncMock()
    pool.fetch.side_effect = lambda *_: [{'name': n} for n in observed]
    session = AsyncMock()
    session.request.return_value = {'structuredContent': {'ok': True}}

    async def with_session(fn):
        return await fn(session)

    monkeypatch.setattr(clobe, 'ensure_schema', AsyncMock())
    monkeypatch.setattr(clobe, '_pool', AsyncMock(return_value=pool))
    monkeypatch.setattr(clobe, '_with_session', with_session)
    return observed, session


@pytest.mark.asyncio
async def test_normal_write_and_read_collection_isolation(env):
    _, session = env
    before = dict(clobe.COLLECTION_TOOLS)
    await clobe.call_write_tool('update_memo', {'memo': 'hello'}, caller='admin:1')
    session.request.assert_awaited_once_with('tools/call', {
        'name': 'update_memo', 'arguments': {'input': {'memo': 'hello'}},
    })
    assert clobe.classify_tool('update_memo')[0] is False
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_tool('update_memo', {})
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.assert_collection_tool('update_memo')
    assert clobe.COLLECTION_TOOLS == before


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [{}, {'preview': True}, {'confirm': True}, {'confirm': 1, 'preview': True}])
async def test_risky_requires_explicit_confirmation_and_preview(env, options):
    _, session = env
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_write_tool('issue_tax_invoice', {}, caller='admin', **options)
    session.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_precedes_write_with_identical_snapshot(env):
    _, session = env
    args = {'input': {'amount': 100}}

    async def respond(method, params):
        if params['name'].startswith('preview_'):
            args['input']['amount'] = 999
            params['arguments']['input']['amount'] = 777
        return {'structuredContent': {'ok': True}}

    session.request.side_effect = respond
    await clobe.call_write_tool('issue_tax_invoice', args, caller='admin', confirm=True, preview=True)
    calls = session.request.await_args_list
    assert [c.args[1]['name'] for c in calls] == ['preview_tax_invoice', 'issue_tax_invoice']
    assert calls[1].args[1]['arguments'] == {'input': {'amount': 100}}


@pytest.mark.asyncio
async def test_preview_error_prevents_write(env, caplog):
    _, session = env
    session.request.return_value = {'isError': True, 'content': [{'text': 'secret'}]}
    with caplog.at_level(logging.INFO), pytest.raises(clobe.ClobeToolError):
        await clobe.call_write_tool('issue_tax_invoice', {}, caller='admin', confirm=True, preview=True)
    assert session.request.await_count == 1
    assert 'status=error' in caplog.text
    assert 'secret' not in caplog.text


@pytest.mark.asyncio
async def test_missing_preview_blocks_even_with_confirmation(env):
    _, session = env
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_write_tool('delete_employee', {}, caller='admin')
    with pytest.raises(clobe.ClobeToolDenied, match='preview_not_available'):
        await clobe.call_write_tool('delete_employee', {}, caller='admin', confirm=True, preview=True)
    session.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['unobserved_write', 'get_my_context'])
async def test_unobserved_and_read_tools_rejected(env, name):
    _, session = env
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_write_tool(name, {}, caller='admin')
    session.request.assert_not_awaited()


@pytest.mark.parametrize('name', [
    'issue_tax_invoice', 'issue_tax_invoices', 'issue_modified_tax_invoice',
    'submit_efiling', 'confirm_payroll', 'close_period', 'delete_memo',
    'run_workflow', 'run_workflow_now', 'import_wehago_payroll',
    'request_vat_hometax_collection',
])
def test_required_risk_categories(name):
    assert clobe._write_requires_confirmation(name)


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_audit_masks_all_values_including_free_text_and_caller(env, caplog, fails):
    _, session = env
    secrets = ['short-token', '110-123-456789', '900101-1234567', 'private-caller']
    if fails:
        session.request.side_effect = RuntimeError(secrets[0])
    with caplog.at_level(logging.INFO):
        try:
            await clobe.call_write_tool('update_memo', {
                secrets[0]: {'memo': secrets[:3], 'nested': {'account': secrets[1]}},
            }, caller=secrets[3])
        except RuntimeError:
            assert fails
    assert all(s not in caplog.text for s in secrets)
    assert 'tool=update_memo' in caplog.text
    assert 'caller_sha256=' in caplog.text
    assert f"status={'error' if fails else 'success'}" in caplog.text


@pytest.mark.asyncio
async def test_denied_attempt_audited(env, caplog):
    with caplog.at_level(logging.INFO), pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_write_tool('delete_employee', {}, caller='admin')
    assert 'status=denied' in caplog.text


@pytest.mark.asyncio
async def test_full_name_preview_and_no_cross_request_reuse(env):
    observed, session = env
    observed.remove('preview_tax_invoice')
    observed.add('preview_issue_tax_invoice')
    await clobe.call_write_tool('issue_tax_invoice', {}, caller='admin', confirm=True, preview=True)
    assert session.request.await_args_list[0].args[1]['name'] == 'preview_issue_tax_invoice'
    session.request.reset_mock()
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_write_tool('issue_tax_invoice', {}, caller='admin', confirm=True)
    session.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_error_reported_as_error(env, caplog):
    _, session = env
    session.request.return_value = {'isError': True}
    with caplog.at_level(logging.INFO), pytest.raises(clobe.ClobeToolError):
        await clobe.call_write_tool('update_memo', {}, caller='admin')
    assert 'status=error' in caplog.text


@pytest.mark.asyncio
async def test_missing_caller_denied(env):
    _, session = env
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_write_tool('update_memo', {}, caller='')
    session.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [{'amount': 100}, {'input': {'amount': 100}}, None])
async def test_both_calls_wrap_input_exactly_once(env, arguments):
    _, session = env
    await clobe.call_write_tool('issue_tax_invoice', arguments, caller='admin', confirm=True, preview=True)
    expected = {'input': {} if arguments is None else {'amount': 100}}
    assert session.request.await_count == 2
    for call in session.request.await_args_list:
        assert call.args[1]['arguments'] == expected


@pytest.mark.asyncio
async def test_input_with_sibling_is_payload_not_wrapper(env):
    _, session = env
    arguments = {'input': {'memo': 'a'}, 'companyId': 'b'}
    await clobe.call_write_tool('update_memo', arguments, caller='admin')
    assert session.request.await_args.args[1]['arguments'] == {'input': arguments}


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [[], 'secret-value', 42])
async def test_invalid_arguments_rejected_before_network(env, arguments):
    _, session = env
    with pytest.raises(clobe.ClobeToolDenied, match='invalid_arguments'):
        await clobe.call_write_tool('update_memo', arguments, caller='admin')
    session.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_exception_prevents_write(env):
    _, session = env
    session.request.side_effect = clobe.ClobeTransientError('mcp_network_error')
    with pytest.raises(clobe.ClobeTransientError):
        await clobe.call_write_tool('issue_tax_invoice', {}, caller='admin', confirm=True, preview=True)
    assert session.request.await_count == 1
