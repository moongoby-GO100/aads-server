"""스마트브라우저 공통엔진 등록 도구 (smart-browser-goal-registration).

기본은 snapshot 계획만 생성한다. 현재 API의 비멱등 milestone/link 계약 때문에
전체 apply는 차단된다. 운영 DDL/직접 SQL/자동 재시도/완료 상태 변경은 하지 않는다.
운영 명령: python3 scripts/smart_browser_goal_registration.py --snapshot snapshot.json
명시적 적용 검사: 위 명령에 --apply --checkpoint checkpoint.json 추가 (현재 쓰기 차단).
필수 환경변수: AADS_API_BASE_URL(HTTPS origin), AADS_ACCESS_TOKEN(기존 로그인 Bearer),
AADS_ACTOR_SESSION_ID(인증 사용자의 기존 세션). DSN/새 권한을 만들지 않는다.
재개: 같은 snapshot/체크포인트로 동일 명령. API 안전 계약 보완 전 전체 적용 불가.
Snapshot 필수: goal, actor(tenant_id/project/session_id), milestones, work_items,
pipeline_jobs, goal_task_links, schema(table -> column list). 배열은 범위 전체 조회여야 한다.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

GOAL_ID = '1a463229-5bbe-4c7e-b020-71b157acc241'
SCOPES = (
    ('계약', '기획PRD설계·계약', '최신 rev2 기획·PRD·설계의 요구사항과 API 계약을 대조하고 승인 근거를 연결한다.', ('runner-c7ed1499', 'runner-3580253c')),
    ('비교', '비교시험', '동일 입력으로 비교시험을 실행하고 성공률·지연·비용 및 재현 가능한 결과를 검토한다.', ('runner-0dc26357', 'runner-087dac9a', 'runner-801f00fd')),
    ('복구', '실행원장·체크포인트·복구', '실행 중단 후 체크포인트에서 재개하여 중복 실행 없이 원장과 복구 결과를 대조한다.', ('runner-2c77a119',)),
    ('수집', '수집품질·원자저장', '누락·중복·형식 오류를 검출하고 저장 실패 주입 시 부분 데이터가 노출되지 않음을 확인한다.', ('runner-8e90a992',)),
    ('공통', 'REST/MCP/Python SDK 공통사용', 'REST·MCP·Python SDK가 동일 실행 ID와 오류·복구 계약을 사용함을 계약시험으로 확인한다.', ('runner-34040ef1',)),
    ('크롤러', '화면변경복구·크롤러', '화면 구조 변경 사례에서 탐색을 복구하고 실패 시 원인·재시도 경계를 기록한다.', ('runner-6bcb8321',)),
    ('화면', '사용자 실행/결과/복구 화면', '사용자가 실행·결과·실패 원인을 조회하고 승인된 복구를 수행하는 흐름을 검수한다.', ('runner-0a64318e',)),
    ('인수', '통합·운영인수', '통합시험·운영 복구 절차·배포 증거를 인수자가 확인한다. push_only done만으로 인수하지 않는다.', ('runner-1ee1919b',)),
)
DEPENDENCIES = dict(zip(
    ('runner-2c77a119', 'runner-8e90a992', 'runner-34040ef1', 'runner-6bcb8321', 'runner-0a64318e', 'runner-1ee1919b'),
    ('runner-3580253c', 'runner-2c77a119', 'runner-8e90a992', 'runner-34040ef1', 'runner-6bcb8321', 'runner-0a64318e'),
))


class Blocked(Exception):
    """출력 가능한 고정 오류 코드만 사용한다."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_snapshot(snapshot):
    goal, actor = snapshot['goal'], snapshot['actor']
    if goal['id'] != GOAL_ID or not goal.get('tenant_id') or not goal.get('project'):
        raise Blocked('goal_scope_mismatch')
    if any(actor.get(k) != goal[k] for k in ('tenant_id', 'project')) or not actor.get('session_id'):
        raise Blocked('actor_scope_mismatch')
    if snapshot.get('complete') is not True:
        raise Blocked('complete_snapshot_required')
    for table in ('milestones', 'work_items', 'goal_task_links', 'pipeline_jobs'):
        if not isinstance(snapshot[table], list) or not snapshot['schema'].get(table):
            raise Blocked('snapshot_schema_missing')
        for row in snapshot[table]:
            # Legacy children may derive tenant/project via their verified goal FK.
            if table != 'pipeline_jobs' and row.get('goal_id') != GOAL_ID:
                raise Blocked('row_goal_mismatch')
            for key in ('tenant_id', 'project'):
                if key in row and row[key] != goal[key]:
                    raise Blocked('row_scope_mismatch')
            if table == 'pipeline_jobs' and any(row.get(k) != goal[k] for k in ('tenant_id', 'project')):
                raise Blocked('runner_scope_missing')
    items = {r['id']: r for r in snapshot['work_items']}
    milestones = {r['id'] for r in snapshot['milestones']}
    for row in items.values():
        if row.get('milestone_id') not in milestones or not row.get('acceptance_criteria') or not all(str(c).strip() for c in row['acceptance_criteria']):
            raise Blocked('empty_criteria_or_orphan')
        expected = {'epic': None, 'story': 'epic', 'task': 'story'}.get(row['type'], 'invalid')
        parent = items.get(row.get('parent_id'))
        if (expected is None and row.get('parent_id')) or (expected is not None and (not parent or parent['type'] != expected or parent['milestone_id'] != row['milestone_id'])):
            raise Blocked('orphan_parent')


PRESERVE_FIELDS = {
    'goal': ('id', 'tenant_id', 'project', 'status', 'owner_session_id', 'owner_role_key'),
    'milestones': ('id', 'goal_id', 'title', 'sequence_order', 'status', 'owner_session_id', 'owner_role_key'),
    'work_items': ('id', 'goal_id', 'milestone_id', 'parent_id', 'type', 'status', 'assignment_id', 'idempotency_key'),
    'goal_task_links': ('id', 'goal_id', 'milestone_id', 'task_type', 'task_id', 'status'),
    'pipeline_jobs': ('job_id', 'tenant_id', 'project', 'status', 'depends_on', 'deploy_policy'),
}


def preserved_metadata(snapshot):
    def project(row, fields):
        return {key: copy.deepcopy(row[key]) for key in fields if key in row}
    return {table: project(snapshot[table], fields) if table == 'goal' else
            [project(row, fields) for row in snapshot[table]]
            for table, fields in PRESERVE_FIELDS.items()}


def build_plan(snapshot):
    validate_snapshot(snapshot)
    plan = {'goal_id': GOAL_ID, 'registered': False, 'milestones': [], 'work_items': [],
            'runner_links': [], 'unsupported': [], 'blocked': [],
            'preserved': preserved_metadata(snapshot),
            'capabilities': {'ledger_scope': 'session_join_required' if 'tenant_id' not in snapshot['schema'].get('chat_workspace_change_ledger', []) else 'tenant_column',
                             'work_item_runner_binding': 'unsupported' if 'work_item_ids' not in snapshot['schema']['goal_task_links'] else 'unverified_deployed_api'},
            'document_keys': ['smart-browser-work-engine-plan', 'smart-browser-work-engine-prd', 'smart-browser-work-engine-design']}
    runners = {r['job_id']: r for r in snapshot['pipeline_jobs']}
    sequence = max((m['sequence_order'] for m in snapshot['milestones']), default=0)
    for index, (_, title, criteria, chain) in enumerate(SCOPES, 1):
        key = f'smart-browser-common-engine:{GOAL_ID}:{index}'
        matches = [m for m in snapshot['milestones'] if m.get('title') == title or m.get('idempotency_key') == key]
        if len(matches) > 1:
            raise Blocked('duplicate_milestone')
        if matches:
            ms = matches[0]
            if ms['title'] != title or ms.get('completion_criteria') != criteria or ms.get('auto_advance') is not False:
                raise Blocked('milestone_conflict')
            ms_id = ms['id']
        else:
            sequence += 1
            ms_id = '$' + key
            plan['blocked'].append('milestone_api_has_no_atomic_idempotency')
        plan['milestones'].append({'key': key, 'id': ms_id, 'reuse': bool(matches), 'payload': {
            'title': title, 'sequence': matches[0]['sequence_order'] if matches else sequence,
            'completion_criteria': criteria, 'auto_advance': False}})
        parent = None
        for kind in ('epic', 'story', 'task'):
            item_key = f'{key}:{kind}'
            suffix = {'epic': '범위 구현', 'story': '인수 흐름', 'task': '검증 수행'}[kind]
            payload = {'type': kind, 'milestone_id': ms_id, 'parent_id': parent,
                       'title': f'{title} {suffix}',
                       'description': criteria, 'acceptance_criteria': [criteria], 'priority': 'P2',
                       'idempotency_key': item_key}
            existing = [w for w in snapshot['work_items'] if w.get('idempotency_key') == item_key]
            if len(existing) > 1:
                raise Blocked('duplicate_work_item')
            if existing:
                if any(existing[0].get(k) != v for k, v in payload.items()):
                    raise Blocked('work_item_conflict')
                item_id = existing[0]['id']
            else:
                item_id = '$' + item_key
            plan['work_items'].append({'key': item_key, 'id': item_id, 'reuse': bool(existing), 'payload': payload})
            parent = item_id
        for pos, runner in enumerate(chain):
            current = runners.get(runner)
            if not current:
                plan['blocked'].append('runner_missing:' + runner)
                continue
            dependency = DEPENDENCIES.get(runner)
            if dependency and current.get('depends_on') not in (dependency, [dependency]):
                raise Blocked('dependency_conflict')
            links = [l for l in snapshot['goal_task_links'] if l.get('task_type') == 'pipeline_job' and l.get('task_id') == runner]
            if any(l.get('milestone_id') != ms_id for l in links):
                raise Blocked('existing_runner_link_conflict')
            plan['runner_links'].append({'runner': runner, 'role': 'execution' if pos == len(chain)-1 else 'history',
                'status': current['status'], 'depends_on': current.get('depends_on'),
                'acceptance_complete': False, 'reuse': bool(links),
                'payload': {'task_type': 'pipeline_job', 'task_id': runner, 'milestone_id': ms_id}})
            if not links:
                plan['blocked'].append('link_api_mutates_status_and_checks_completion')
    plan['unsupported'] = ['runner_to_work_item_binding_not_in_LinkTaskRequest',
                           'milestone_idempotency_key_not_in_MilestoneCreateRequest',
                           'atomic_multi_api_transaction_not_available', 'deployed_runner_sync_not_verified']
    # Missing binding cannot be fixed by a client-supplied unknown JSON property.
    plan['blocked'].append('runner_work_item_binding_unsupported')
    plan['blocked'] = sorted(set(plan['blocked']))
    plan['plan_hash'] = digest(plan)
    return plan


class APIAdapter:
    """기존 Bearer + X-Chat-Session-ID만 사용. 응답/예외 원문을 노출하지 않는다."""
    def __init__(self, base_url, token, session_id):
        parsed = urlparse(base_url)
        if not parsed.hostname or parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise Blocked('https_origin_required')
        if not token or not session_id:
            raise Blocked('authentication_unconfigured')
        self.base_url, self.token, self.session_id = base_url.rstrip('/'), token, session_id

    def request(self, method, path, payload=None):
        request = Request(self.base_url + path, method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'X-Chat-Session-ID': self.session_id, 'Content-Type': 'application/json'})
        # urllib redirects could forward Authorization; reject all redirects.
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            raise Blocked('permission_denied' if exc.code in (401, 403) else f'http_{exc.code}') from None
        except (URLError, ValueError, OSError):
            raise Blocked('api_response_uncertain') from None

    def deployed_contract(self):
        spec = self.request('GET', '/openapi.json')
        schemas = spec.get('components', {}).get('schemas', {})
        expected = {'MilestoneCreateRequest': {'title', 'sequence', 'auto_advance', 'completion_criteria'},
                    'WorkItemCreateRequest': {'type', 'milestone_id', 'parent_id', 'idempotency_key', 'acceptance_criteria'},
                    'LinkTaskRequest': {'task_type', 'task_id', 'milestone_id'}}
        missing = {name: sorted(fields - set(schemas.get(name, {}).get('properties', {}))) for name, fields in expected.items()}
        routes = [f'/api/v1/goals/{{goal_id}}/{part}' for part in ('milestones', 'work-items', 'link-task')]
        return {'missing_fields': {k: v for k, v in missing.items() if v},
                'missing_routes': [p for p in routes if 'post' not in spec.get('paths', {}).get(p, {})],
                'semantic_idempotency_verified': False}


def save_checkpoint(path, state):
    """같은 디렉터리 atomic replace + fsync; 토큰·응답 전문은 저장하지 않는다."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.registration-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute_work_items(plan, adapter, checkpoint):
    """멱등 업무 생성 단계. 전체 등록 게이트를 대체하지 않는 재개 가능한 실행 단위.

    응답 유실도 같은 키로 재요청한다. checkpoint만 믿고 skip하지 않는다.
    현재 전체 apply는 아래 apply_plan에서 막히므로 이 함수만 운영 호출하지 않는다.
    """
    checkpoint = Path(checkpoint)
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {'plan_hash': plan['plan_hash'], 'ids': {}}
    if state['plan_hash'] != plan['plan_hash']:
        raise Blocked('checkpoint_plan_mismatch')
    ids = dict(state['ids'])
    for item in plan['work_items']:
        payload = copy.deepcopy(item['payload'])
        for field in ('milestone_id', 'parent_id'):
            value = payload[field]
            if value and value.startswith('$'):
                if value[1:] not in ids:
                    raise Blocked('unresolved_parent_or_milestone')
                payload[field] = ids[value[1:]]
        if not payload['acceptance_criteria'] or not all(str(c).strip() for c in payload['acceptance_criteria']):
            raise Blocked('empty_criteria')
        state['pending'] = item['key']
        save_checkpoint(checkpoint, state)
        result = adapter.request('POST', f'/api/v1/goals/{GOAL_ID}/work-items', payload)
        row = result.get('work_item', {})
        if not row.get('id') or any(row.get(k) != v for k, v in payload.items()) or row.get('goal_id') != GOAL_ID:
            raise Blocked('api_contract_mismatch')
        if item['key'] in ids and ids[item['key']] != row['id']:
            raise Blocked('replay_id_mismatch')
        ids[item['key']] = row['id']
        state.update(ids=ids, pending=None)
        save_checkpoint(checkpoint, state)
    return state


def apply_plan(plan, adapter, checkpoint):
    # Read deployed contract before ANY mutation. Shape agreement is not semantic proof.
    contract = adapter.deployed_contract()
    return {'registered': False, 'status': 'blocked', 'blocked': plan['blocked'],
            'unsupported': plan['unsupported'], 'deployed_contract': contract,
            'checkpoint': str(checkpoint), 'next_gate': '권한 있는 운영 경로에서 상태·의존성 재조회 및 API 안전 계약 보완 후 새 snapshot으로 재계획'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--checkpoint', default='smart-browser-registration.checkpoint.json')
    args = parser.parse_args(argv)
    try:
        plan = build_plan(json.loads(Path(args.snapshot).read_text()))
        result = {'status': 'dry_run', 'registered': False, 'plan': plan}
        if args.apply:
            adapter = APIAdapter(os.getenv('AADS_API_BASE_URL', ''), os.getenv('AADS_ACCESS_TOKEN', ''), os.getenv('AADS_ACTOR_SESSION_ID', ''))
            if adapter.session_id != json.loads(Path(args.snapshot).read_text())['actor']['session_id']:
                raise Blocked('actor_session_mismatch')
            result = apply_plan(plan, adapter, args.checkpoint)
    except Blocked as exc:
        result = {'status': 'blocked', 'registered': False, 'error': str(exc)}
    except (KeyError, TypeError, ValueError, OSError):
        result = {'status': 'blocked', 'registered': False, 'error': 'invalid_snapshot_or_checkpoint'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
