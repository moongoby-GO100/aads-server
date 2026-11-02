"""운영 스키마 차이와 비원자 API 복구를 외부 DB 없이 검증한다."""
import copy
import json

import pytest

from scripts.smart_browser_goal_registration import (
    DEPENDENCIES,
    GOAL_ID,
    SCOPES,
    APIAdapter,
    Blocked,
    apply_plan,
    build_plan,
    execute_work_items,
    main,
)


@pytest.fixture
def snapshot():
    runners = [r for scope in SCOPES for r in scope[3]]
    return {
        'complete': True,
        'goal': {'id': GOAL_ID, 'tenant_id': 'actual-tenant', 'project': 'AADS',
                 'status': 'blocked', 'owner_session_id': 'actual-owner', 'owner_role_key': None},
        'actor': {'tenant_id': 'actual-tenant', 'project': 'AADS', 'session_id': 'actual-owner'},
        'schema': {'milestones': ['id', 'goal_id', 'sequence_order'],
                   'work_items': ['id', 'goal_id', 'tenant_id', 'idempotency_key'],
                   'pipeline_jobs': ['job_id', 'tenant_id', 'project', 'depends_on'],
                   'goal_task_links': ['id', 'goal_id', 'task_type', 'task_id', 'milestone_id'],
                   'chat_workspace_change_ledger': ['session_id', 'project', 'repo', 'file_path', 'status']},
        'milestones': [{'id': f'm{i}', 'goal_id': GOAL_ID, 'title': f'기존 M{i}', 'sequence_order': i,
                        'status': 'completed' if i % 2 else 'blocked', 'owner_session_id': 'keep-owner',
                        'owner_role_key': 'keep-role'} for i in range(1, 14)],
        'work_items': [], 'goal_task_links': [],
        'pipeline_jobs': [{'job_id': r, 'tenant_id': 'actual-tenant', 'project': 'AADS',
                           'status': 'queued', 'depends_on': DEPENDENCIES.get(r)} for r in runners],
    }


def test_operational_schema_and_preservation(snapshot):
    before = copy.deepcopy(snapshot)
    plan = build_plan(snapshot)
    assert snapshot == before
    assert plan['preserved']['milestones'] == before['milestones']
    assert plan['preserved']['goal'] == before['goal']
    assert [m['payload']['sequence'] for m in plan['milestones']] == list(range(14, 22))
    assert all(m['payload']['auto_advance'] is False for m in plan['milestones'])
    assert len(plan['work_items']) == 24
    assert plan['capabilities']['ledger_scope'] == 'session_join_required'
    assert plan['capabilities']['work_item_runner_binding'] == 'unsupported'
    assert build_plan(snapshot) == plan
    assert all(w['payload']['acceptance_criteria'] for w in plan['work_items'])


@pytest.mark.parametrize('change', ['tenant', 'goal', 'actor', 'runner', 'dependency', 'complete'])
def test_scope_and_dependency_block(snapshot, change):
    if change == 'tenant':
        snapshot['milestones'][0]['tenant_id'] = 'other'
    elif change == 'goal':
        snapshot['milestones'][0]['goal_id'] = 'other'
    elif change == 'actor':
        snapshot['actor']['project'] = 'other'
    elif change == 'runner':
        del snapshot['pipeline_jobs'][0]['tenant_id']
    elif change == 'dependency':
        snapshot['pipeline_jobs'][-1]['depends_on'] = 'other'
    else:
        snapshot['complete'] = False
    with pytest.raises(Blocked):
        build_plan(snapshot)


def test_history_and_push_only_are_not_acceptance(snapshot):
    snapshot['pipeline_jobs'][0]['status'] = 'rejected_done'
    snapshot['pipeline_jobs'][1]['status'] = 'running'
    snapshot['pipeline_jobs'][-1].update(status='done', deploy_policy='push_only')
    plan = build_plan(snapshot)
    assert [(r['runner'], r['role']) for r in plan['runner_links'][:2]] == [
        ('runner-c7ed1499', 'history'), ('runner-3580253c', 'execution')]
    assert all(r['acceptance_complete'] is False for r in plan['runner_links'])
    assert plan['preserved']['pipeline_jobs'][-1]['deploy_policy'] == 'push_only'


def existing_milestones(snapshot):
    plan = build_plan(snapshot)
    for i, m in enumerate(plan['milestones']):
        snapshot['milestones'].append(dict(m['payload'], id=f'new-{i}', goal_id=GOAL_ID,
                                           sequence_order=m['payload']['sequence']))
    return build_plan(snapshot)


def test_reuse_and_conflict(snapshot):
    plan = existing_milestones(snapshot)
    assert all(m['reuse'] for m in plan['milestones'])
    assert 'milestone_api_has_no_atomic_idempotency' not in plan['blocked']
    snapshot['milestones'][-1]['completion_criteria'] = ''
    with pytest.raises(Blocked, match='milestone_conflict'):
        build_plan(snapshot)


def test_original_links_preserved(snapshot):
    existing_milestones(snapshot)
    snapshot['goal_task_links'] = [{'goal_id': GOAL_ID, 'task_type': 'pipeline_job',
                                   'task_id': 'runner-c7ed1499', 'milestone_id': 'new-0',
                                   'status': 'failed'}]
    plan = build_plan(snapshot)
    assert plan['runner_links'][0]['reuse']
    assert plan['preserved']['goal_task_links'] == snapshot['goal_task_links']
    snapshot['goal_task_links'][0]['milestone_id'] = 'm1'
    with pytest.raises(Blocked, match='existing_runner_link_conflict'):
        build_plan(snapshot)


class FakeAPI:
    def __init__(self, fail_at=None, denial=False):
        self.rows = {}
        self.calls = 0
        self.fail_at = fail_at
        self.denial = denial

    def request(self, method, path, payload):
        self.calls += 1
        if self.denial:
            raise Blocked('permission_denied')
        key = payload['idempotency_key']
        if key not in self.rows:
            self.rows[key] = dict(payload, id=f'item-{len(self.rows)}', goal_id=GOAL_ID)
        # Simulate committed write followed by lost response, not a pre-write failure.
        if self.calls == self.fail_at:
            raise Blocked('api_response_uncertain')
        return {'work_item': self.rows[key]}

    def deployed_contract(self):
        return {'missing_fields': {}, 'missing_routes': [], 'semantic_idempotency_verified': False}


def test_lost_response_resume_and_idempotency(snapshot, tmp_path):
    plan = existing_milestones(snapshot)
    api = FakeAPI(fail_at=5)
    checkpoint = tmp_path / 'checkpoint.json'
    with pytest.raises(Blocked, match='api_response_uncertain'):
        execute_work_items(plan, api, checkpoint)
    assert len(api.rows) == 5
    assert json.loads(checkpoint.read_text())['pending'] == plan['work_items'][4]['key']
    execute_work_items(plan, api, checkpoint)
    assert len(api.rows) == 24
    execute_work_items(plan, api, checkpoint)
    assert len(api.rows) == 24
    assert json.loads(checkpoint.read_text())['pending'] is None
    assert 'token' not in checkpoint.read_text()


def test_permission_denial_stops_writes(snapshot, tmp_path):
    api = FakeAPI(denial=True)
    with pytest.raises(Blocked, match='permission_denied'):
        execute_work_items(existing_milestones(snapshot), api, tmp_path / 'checkpoint')
    assert api.calls == 1 and not api.rows


def test_checkpoint_wrong_plan(snapshot, tmp_path):
    path = tmp_path / 'checkpoint'
    path.write_text(json.dumps({'plan_hash': 'other', 'ids': {}}))
    api = FakeAPI()
    with pytest.raises(Blocked, match='checkpoint_plan_mismatch'):
        execute_work_items(existing_milestones(snapshot), api, path)
    assert api.calls == 0


@pytest.mark.parametrize('bad', ['criteria', 'parent'])
def test_invalid_existing_work(snapshot, bad):
    snapshot['work_items'] = [{'id': 'w1', 'goal_id': GOAL_ID, 'milestone_id': 'm1',
                               'type': 'story', 'parent_id': 'missing', 'acceptance_criteria': ['확인']}]
    if bad == 'criteria':
        snapshot['work_items'][0]['acceptance_criteria'] = [' ']
    with pytest.raises(Blocked):
        build_plan(snapshot)


def test_apply_blocked_even_if_openapi_matches(snapshot, tmp_path):
    api = FakeAPI()
    result = apply_plan(build_plan(snapshot), api, tmp_path / 'checkpoint')
    assert result['status'] == 'blocked' and not result['registered']
    assert api.calls == 0
    assert 'runner_work_item_binding_unsupported' in result['blocked']


def test_cli_dry_run_and_unconfigured_apply(snapshot, tmp_path, capsys, monkeypatch):
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(snapshot))
    assert main(['--snapshot', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'dry_run'
    monkeypatch.delenv('AADS_API_BASE_URL', raising=False)
    monkeypatch.delenv('AADS_ACCESS_TOKEN', raising=False)
    assert main(['--snapshot', str(path), '--apply']) == 2
    assert json.loads(capsys.readouterr().out)['registered'] is False


def test_deployed_schema_difference():
    adapter = APIAdapter('https://example.invalid', 'never-output', 'actual-session')
    adapter.request = lambda *args: {'paths': {}, 'components': {'schemas': {}}}
    result = adapter.deployed_contract()
    assert len(result['missing_routes']) == 3
    assert 'idempotency_key' in result['missing_fields']['WorkItemCreateRequest']


@pytest.mark.parametrize('origin', ['http://example.invalid', 'https://u:p@example.invalid', 'https://example.invalid/?token=secret'])
def test_no_insecure_origin(origin):
    with pytest.raises(Blocked, match='https_origin_required'):
        APIAdapter(origin, 'secret', 'session')


def test_no_unrelated_snapshot_secrets_exported(snapshot):
    snapshot['goal']['token'] = 'never-echo-secret'
    snapshot['pipeline_jobs'][0]['instruction'] = 'never-echo-secret'
    assert 'never-echo-secret' not in json.dumps(build_plan(snapshot))


def test_work_items_reused_without_status_reset(snapshot, tmp_path):
    plan = existing_milestones(snapshot)
    api = FakeAPI()
    execute_work_items(plan, api, tmp_path / 'checkpoint')
    snapshot['work_items'] = list(api.rows.values())
    for row in snapshot['work_items']:
        row.update(status='blocked', assignment_id=None)
    before = copy.deepcopy(snapshot)
    reused = build_plan(snapshot)
    assert all(w['reuse'] for w in reused['work_items'])
    assert snapshot == before


def test_no_redirect_credentials_forwarded(monkeypatch):
    import urllib.request
    from urllib.error import HTTPError

    def opener(handler):
        assert handler.redirect_request(None, None, 302, '', {}, 'https://other.invalid') is None
        class Denied:
            def open(self, *args, **kwargs):
                raise HTTPError('https://example.invalid', 403, 'never-output-secret', {}, None)
        return Denied()

    monkeypatch.setattr(urllib.request, 'build_opener', opener)
    api = APIAdapter('https://example.invalid', 'never-output-secret', 'actual-session')
    with pytest.raises(Blocked, match='^permission_denied$'):
        api.request('GET', '/openapi.json')
