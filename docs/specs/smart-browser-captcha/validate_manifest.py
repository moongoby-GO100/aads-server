#!/usr/bin/env python3
"""캡챠 문서와 등록 계획의 정적 무결성 검사. DB/사이트 인수 시험이 아니다."""
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = Path(__file__).with_name('업무계층_등록계획.json')
GOAL = '1a463229-5bbe-4c7e-b020-71b157acc241'


PINNED = {'sources': [{'id': '4d1db0d0-313b-4417-a00b-c7dc1a810e19', 'document_key': 'smart-browser-work-engine-contract', 'content_hash': '30d7f8250975d90cba68bf685dbfa985f4c5e6503cb56c9aeec68c492b264aae', 'status': 'draft', 'generation': 2, 'approved': False, 'revision_number': 2, 'freshness': 'provided_query_snapshot_not_live'}, {'id': '2b7ffa5f-c050-4f11-b5b5-705dd52466b2', 'document_key': 'smart-browser-work-engine-design', 'content_hash': '34629dfa3bd0cfa6f25913b03b67191d2c9e4c1521ff66613c5e002431d62c75', 'status': 'review', 'generation': 4, 'approved': False, 'revision_number': 2, 'freshness': 'provided_query_snapshot_not_live'}, {'id': '3487a522-51d5-40fe-abf3-420cdce5c4f4', 'document_key': 'smart-browser-work-engine-prd', 'content_hash': '2ad915051e20180f97d069a9303d053b468b66842e4c37def866c9a383bc81c6', 'status': 'review', 'generation': 4, 'approved': False, 'revision_number': 2, 'freshness': 'provided_query_snapshot_not_live'}, {'id': '9fb95a2b-91d0-41aa-baba-fa1e1c4e1a3c', 'document_key': 'smart-browser-work-engine-plan', 'content_hash': '7ddf4e84f7af024b0f110bf14f364f178f8692738d3082dafa9248e132ddbc30', 'status': 'review', 'generation': 4, 'approved': False, 'revision_number': 2, 'freshness': 'provided_query_snapshot_not_live'}], 'milestone_snapshot_hash': '3dc81a44f85f8bb01550f14088c927ed5f5bae82fc2182d60f037904f45eaf26', 'original_keys': {'C0': 'captcha:1a463229:C0:v1', 'C1': 'captcha:1a463229:C1:v1', 'C2': 'captcha:1a463229:C2:v1', 'C3': 'captcha:1a463229:C3:v1', 'C4': 'captcha:1a463229:C4:v1', 'captcha:1a463229:C0:v1:epic': 'captcha:1a463229:C0:v1:epic', 'captcha:1a463229:C0:v1:epic:story1': 'captcha:1a463229:C0:v1:epic:story1', 'captcha:1a463229:C0:v1:epic:story1:task1': 'captcha:1a463229:C0:v1:epic:story1:task1', 'captcha:1a463229:C0:v1:epic:story1:task2': 'captcha:1a463229:C0:v1:epic:story1:task2', 'captcha:1a463229:C0:v1:epic:story2': 'captcha:1a463229:C0:v1:epic:story2', 'captcha:1a463229:C0:v1:epic:story2:task1': 'captcha:1a463229:C0:v1:epic:story2:task1', 'captcha:1a463229:C0:v1:epic:story2:task2': 'captcha:1a463229:C0:v1:epic:story2:task2', 'captcha:1a463229:C1:v1:epic': 'captcha:1a463229:C1:v1:epic', 'captcha:1a463229:C1:v1:epic:story1': 'captcha:1a463229:C1:v1:epic:story1', 'captcha:1a463229:C1:v1:epic:story1:task1': 'captcha:1a463229:C1:v1:epic:story1:task1', 'captcha:1a463229:C1:v1:epic:story1:task2': 'captcha:1a463229:C1:v1:epic:story1:task2', 'captcha:1a463229:C1:v1:epic:story2': 'captcha:1a463229:C1:v1:epic:story2', 'captcha:1a463229:C1:v1:epic:story2:task1': 'captcha:1a463229:C1:v1:epic:story2:task1', 'captcha:1a463229:C1:v1:epic:story2:task2': 'captcha:1a463229:C1:v1:epic:story2:task2', 'captcha:1a463229:C2:v1:epic': 'captcha:1a463229:C2:v1:epic', 'captcha:1a463229:C2:v1:epic:story1': 'captcha:1a463229:C2:v1:epic:story1', 'captcha:1a463229:C2:v1:epic:story1:task1': 'captcha:1a463229:C2:v1:epic:story1:task1', 'captcha:1a463229:C2:v1:epic:story1:task2': 'captcha:1a463229:C2:v1:epic:story1:task2', 'captcha:1a463229:C2:v1:epic:story2': 'captcha:1a463229:C2:v1:epic:story2', 'captcha:1a463229:C2:v1:epic:story2:task1': 'captcha:1a463229:C2:v1:epic:story2:task1', 'captcha:1a463229:C2:v1:epic:story2:task2': 'captcha:1a463229:C2:v1:epic:story2:task2', 'captcha:1a463229:C3:v1:epic': 'captcha:1a463229:C3:v1:epic', 'captcha:1a463229:C3:v1:epic:story1': 'captcha:1a463229:C3:v1:epic:story1', 'captcha:1a463229:C3:v1:epic:story1:task1': 'captcha:1a463229:C3:v1:epic:story1:task1', 'captcha:1a463229:C3:v1:epic:story1:task2': 'captcha:1a463229:C3:v1:epic:story1:task2', 'captcha:1a463229:C3:v1:epic:story2': 'captcha:1a463229:C3:v1:epic:story2', 'captcha:1a463229:C3:v1:epic:story2:task1': 'captcha:1a463229:C3:v1:epic:story2:task1', 'captcha:1a463229:C3:v1:epic:story2:task2': 'captcha:1a463229:C3:v1:epic:story2:task2', 'captcha:1a463229:C4:v1:epic': 'captcha:1a463229:C4:v1:epic', 'captcha:1a463229:C4:v1:epic:story1': 'captcha:1a463229:C4:v1:epic:story1', 'captcha:1a463229:C4:v1:epic:story1:task1': 'captcha:1a463229:C4:v1:epic:story1:task1', 'captcha:1a463229:C4:v1:epic:story1:task2': 'captcha:1a463229:C4:v1:epic:story1:task2', 'captcha:1a463229:C4:v1:epic:story2': 'captcha:1a463229:C4:v1:epic:story2', 'captcha:1a463229:C4:v1:epic:story2:task1': 'captcha:1a463229:C4:v1:epic:story2:task1', 'captcha:1a463229:C4:v1:epic:story2:task2': 'captcha:1a463229:C4:v1:epic:story2:task2'}, 'snapshot_sha256': 'f2554430e5f670714b50b9892c64806b3f441700a8d2fb565dec24e87306aa34'}

def check(plan):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(plan['goal_id'] == GOAL, 'goal identity')
    require(plan['goal_status'] == 'blocked', 'goal status')
    require(plan['owner_session_id'] == '7fb5f50a-9fe7-4a29-9333-9c023125aa6e', 'owner')
    require(plan['tenant_id'] == '2d701a8c-9596-4757-8588-faa4f7837112', 'tenant')
    require(plan['source_task_id'] == 'runner-6fbe30cd', 'source')
    milestones = {m['ref']: m for m in plan['milestones']}
    require(set(milestones) == {f'C{i}' for i in range(5)}, 'milestone coverage')
    require(len(plan['milestones']) == 5, 'milestone duplicate')
    items = {w['ref']: w for w in plan['work_items']}
    require(len(items) == len(plan['work_items']), 'duplicate work ref')
    keys = [x['idempotency_key'] for x in [*plan['milestones'], *items.values()]]
    require(len(set(keys)) == len(keys), 'duplicate idempotency key')
    for m in milestones.values():
        require(m['auto_advance'] is False, 'approval gate')
        require(m['remote_id'] is None and m['sequence'] is None, 'unverified server identity')
        for field in ('description', 'completion_criteria', 'acceptance_criteria'):
            require(bool(m[field]), 'empty milestone criteria')
        require(isinstance(m['acceptance_criteria'], list), 'milestone acceptance array')
        counts = {t: sum(w['type'] == t and w['milestone_ref'] == m['ref'] for w in items.values())
                  for t in ('epic', 'story', 'task')}
        require(counts == {'epic': 1, 'story': 2, 'task': 4}, 'stage counts')
    for w in items.values():
        require(w['status'] == 'planned' and w['remote_id'] is None, 'false completion')
        for field in ('description', 'completion_criteria', 'responsible_role', 'responsible_files', 'test_id'):
            require(bool(w[field].strip()), 'empty work field')
        require(isinstance(w['acceptance_criteria'], list) and len(w['acceptance_criteria']) >= 2
                and all(isinstance(v, str) and v.strip() for v in w['acceptance_criteria']), 'acceptance array')
        parent = items.get(w['parent_ref'])
        if w['type'] == 'epic':
            require(w['parent_ref'] is None, 'epic parent')
        else:
            require(parent is not None, 'missing parent')
            require(parent['type'] == {'story': 'epic', 'task': 'story'}[w['type']], 'parent type')
            require(parent['milestone_ref'] == w['milestone_ref'], 'cross milestone parent')
        if w['type'] in ('epic', 'story'):
            require(sum(v['parent_ref'] == w['ref'] for v in items.values()) >= 2, 'empty child hierarchy')
        require(all(d in items or d in milestones for d in w['depends_on']), 'unknown dependency')
    graph = {k: v['depends_on'] for k, v in {**milestones, **items}.items()}
    visiting, visited = set(), set()
    def visit(ref):
        require(ref not in visiting, 'dependency cycle')
        if ref in visited:
            return
        visiting.add(ref)
        for dep in graph[ref]:
            require(dep in graph, 'missing dependency')
            visit(dep)
        visiting.remove(ref)
        visited.add(ref)
    for ref in graph:
        visit(ref)
    require(len(plan['documents']) == 3, 'document count')
    require({d['document_key'] for d in plan['documents']} ==
            {f'smart-browser-captcha-{k}' for k in ('plan', 'prd', 'design')}, 'document keys')
    for d in plan['documents']:
        path = ROOT / d['path']
        require(path.resolve().is_relative_to(ROOT), 'source path outside repo')
        content = path.read_text()
        require(content.startswith('# 스마트브라우저'), 'Korean H1')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == d['content_hash'], 'document hash')
        require(d['source_task_id'] == plan['source_task_id'] and len(d['change_summary']) >= 10, 'provenance')
        require(d['goal_id'] == GOAL, 'document goal')
        require(d['registration_status'] == 'not_registered' and d['view'] is None, 'false registration')
        require(all(r['document_key'] in content for r in plan['source_references']), 'source references')
        require(all(r['id'] in content for r in plan['requirements']), 'requirement coverage')
    tests = {w['test_id'] for w in items.values()}
    require({r['id'] for r in plan['requirements']} == {f'R{i:02}' for i in range(1, 9)}, 'requirements')
    require(all(set(r['tests']) <= tests for r in plan['requirements']), 'test mapping')
    require(plan['existing_milestones']['pending'] == [], 'legacy scope planned')
    check_supplement(plan)
    require(plan['database_replay_growth'] is None, 'false database replay evidence')
    require(not any(v['verified'] for v in plan['runner_links'].values()), 'unverified runner link')


def check_supplement(plan):
    """Reject false completion and unsafe handoff contracts, without network access."""
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    require(plan['goal_title'] == '스마트브라우져 진화 및 최적화', 'goal title preservation')
    require(plan['source_references'] == PINNED['sources'], 'rev2 source identity/hash/status')
    require(plan['source_snapshot']['sha256'] == PINNED['snapshot_sha256']
            and plan['source_snapshot']['live'] is False, 'snapshot provenance')
    for item in plan['milestones'] + plan['work_items']:
        require(item['idempotency_key'] == PINNED['original_keys'][item['ref']], 'original key changed')
    snapshots = plan['existing_milestones']['snapshots']
    require(digest(snapshots) == PINNED['milestone_snapshot_hash'], 'legacy scope/status/owner/evidence changed')
    require(len(snapshots) == 13, 'legacy snapshot coverage')
    legacy = plan['legacy_work_items']
    items = plan['work_items'] + legacy
    indexed = {item['ref']: item for item in items}
    require(len(indexed) == len(items), 'duplicate C/M ref')
    keys = [w['idempotency_key'] for w in items] + [m['idempotency_key'] for m in plan['milestones']]
    require(len(set(keys)) == len(keys), 'duplicate C/M key')
    allowed_milestones = {f'C{i}' for i in range(5)} | {f'M{i}' for i in range(1, 14)}
    for item in items:
        require(item['type'] in {'epic', 'story', 'task'}, 'invalid hierarchy type')
        require(item['milestone_ref'] in allowed_milestones, 'unknown C/M milestone')
        for field in ('inputs', 'action', 'expected_result', 'responsible_files', 'recovery'):
            require(isinstance(item.get(field), str) and len(item[field].strip()) >= 8, 'missing concrete work contract')
        require(item.get('evidence') and item['completion_criteria'] and item['acceptance_criteria'], 'missing work evidence/criteria')
        require(item['status'] == 'planned' and item['remote_id'] is None, 'false C/M done')
        parent = indexed.get(item['parent_ref'])
        if item['type'] == 'epic':
            require(parent is None and item['parent_ref'] is None, 'epic parent')
        else:
            require(parent is not None and parent['type'] == {'story': 'epic', 'task': 'story'}[item['type']], 'C/M parent type')
            require(parent['milestone_ref'] == item['milestone_ref'], 'cross C/M parent')
    for i, snapshot in enumerate(snapshots, 1):
        group = [w for w in legacy if w['milestone_ref'] == f'M{i}']
        require({w['type'] for w in group} == {'epic', 'story', 'task'}, 'legacy hierarchy coverage')
        for item in group:
            require(item['milestone_id'] == snapshot['id'], 'legacy actual milestone id')
            require(item['idempotency_key'] == f"captcha:1a463229:M{i}:v1:{item['type']}", 'legacy stable key')
            require(snapshot['completion_criteria'] in item['description'], 'legacy original criteria')
            require(item['revalidation_only'] == (snapshot['status'] == 'completed'), 'legacy revalidation scope')
    # Combined dependency graph includes both old milestones and new hierarchy.
    graph = {m['ref']: m['depends_on'] for m in plan['milestones']}
    graph.update({f'M{i}': [] for i in range(1, 14)})
    graph.update({w['ref']: w['depends_on'] for w in items})
    def visit(ref, active, done):
        require(ref in graph, 'unknown C/M dependency')
        require(ref not in active, 'C/M dependency cycle')
        if ref in done:
            return
        active.add(ref)
        for dep in graph[ref]:
            visit(dep, active, done)
        active.remove(ref)
        done.add(ref)
    done = set()
    for ref in graph:
        visit(ref, set(), done)
    lane = plan['lane_policy']
    require(lane['default'] == 'server' and lane['pc_required'] is False, 'server first / conditional PC')
    require(set(lane['pc_only_if']) == {'existing_local_login_session_verified', 'certificate_requirement_verified', 'security_program_requirement_verified'}, 'PC evidence required')
    require(lane['auto_lane_change'] is False and lane['new_device_reauthentication'] is True, 'lane/device safety')
    binding = {'tenant', 'project', 'account_ref', 'lane', 'work_key', 'page_id', 'frame_id', 'navigation_revision', 'device_id', 'owner_epoch'}
    require(set(lane['bind']) == binding, 'execution binding')
    require(set(lane['login_success']) == {'account_match', 'business_screen', 'captcha_absent', 'error_absent', 'access_denied_absent'}, 'login postconditions')
    require(lane['http200_is_login'] is False and lane['fallback_is_login'] is False, 'false fallback login')
    for key in ('server_e2e', 'pc_e2e'):
        require(lane[key] == {'status': 'not_run', 'artifact_ref': None}, 'false live E2E evidence')
    recipes = plan['recipe_contract']
    require('approved' in recipes['discover_first'] and 'list' in recipes['discover_first'], 'approved recipe discovery')
    require(recipes['state_machine'] == ['planned', 'e2e_verified', 'registration_pending', 'approved', 'replayed_verified'], 'recipe state separation')
    require(recipes['force_approval'] is False and recipes['authenticated_skip_submit'] is True, 'recipe approval/submit policy')
    require(recipes['password_evidence'] == 'filled/empty', 'secret evidence policy')
    require(set(recipes['binding_fields']) == binding, 'recipe binding')
    require(set(recipes['failure_not_register']) == {'site_error', 'access_denied', 'account_mismatch', 'captcha_present', 'fallback_only'}, 'false success registration')
    pieces = recipes['pieces']
    require([p['key'] for p in pieces] == ['enter-login', 'fill-vault', 'submit-verify', 'query', 'download', 'aggregate'], 'recipe piece coverage/order')
    for index, piece in enumerate(pieces):
        for field in ('title', 'starts_when', 'action', 'succeeds_when', 'verify_assertion', 'failure_return', 'credential_scope', 'runtime_assertion_mapping'):
            require(isinstance(piece.get(field), str) and bool(piece[field].strip()), 'missing recipe field ' + field)
        require(piece['exact_origin'] is None and piece['exact_domain'] is None
                and piece['profile_status'] == 'resolve_from_approved_profile_before_execution', 'fabricated exact site profile')
        for field, expected in [('lane', '$bound.lane'), ('work_key', '$bound.work_key'), ('account_ref', '$bound.account_ref'), ('page_ref', '$bound.page_id')]:
            require(piece[field] == expected, 'piece work/lane/account/page binding')
        require(piece['predecessor']['piece'] == ('approved_profile' if index == 0 else pieces[index-1]['key']), 'recipe predecessor')
        for field in ('recipe_name', 'version', 'hash'):
            require(field in piece['predecessor'] and piece['predecessor'][field] is None, 'unverified recipe identity')
        require(bool(piece['predecessor']['resolution']), 'recipe predecessor resolution')
        for field in ('screenshot_ref', 'artifact_ref', 'run_ref'):
            require(field in piece and piece[field] is None, 'false recipe evidence')
        require(piece['state'] == 'planned' and piece['e2e_status'] == 'not_run'
                and piece['registration_status'] == 'not_registered' and piece['approval_status'] == 'not_approved'
                and piece['replay_status'] == 'not_run', 'false recipe registered/done')
        require(piece['secret_variables'] == (['$vault.username', '$vault.password'] if piece['key'] == 'fill-vault' else []), 'secret variable references')
    contracts = {t['id']: t for t in plan['test_contracts']}
    require(set(contracts) == {w['test_id'] for w in plan['work_items'] if w['type'] == 'task'}, 'leaf test contracts')
    for test in contracts.values():
        for field in ('input', 'action', 'expected', 'responsible_file', 'test_file', 'recovery'):
            require(bool(test.get(field)), 'concrete test criteria')
        require(test['status'] == 'not_run' and test['artifact_ref'] is None, 'false test completion')
        require({'command', 'exit_code', 'tested_sha', 'environment', 'artifact_ref', 'run_ref', 'not_run_reason'} <= set(test['evidence']), 'test evidence schema')
        require(test['test_file_state'] == 'new_planned', 'future tests honesty')
    require(all(r['tests'] and set(r['tests']) <= set(contracts) for r in plan['requirements']), 'requirement to leaf tests')
    require(len(plan['pm_tests']) == 6 and {t['id'] for t in plan['pm_tests']} == {f'PM-C{i}-T{j}' for i in range(1,4) for j in (1,2)}, 'PM test coverage')
    for test in plan['pm_tests']:
        require(test['input'] and test['expected'] and test['status'] == 'not_run', 'PM static/live distinction')
    require(set(plan['upstream_contract']) == {'execution_ledger', 'PM-C1', 'PM-C2', 'PM-C3', 'captcha_boundary'}, 'upstream boundary')
    require('non-expiring' in plan['upstream_contract']['PM-C3'] and 'dataset owner' in plan['upstream_contract']['PM-C1'], 'PM safety')
    require(plan['runner_links']['D']['job_id'] == 'runner-6fbe30cd'
            and plan['runner_links']['E']['job_id'] == 'runner-66611fed'
            and plan['runner_links']['S']['job_id'] == 'runner-83015ff6'
            and plan['runner_links']['I']['job_id'] == 'runner-144faf81', 'actual batch jobs')
    require(plan['rejected_predecessor'] == {'job_id': 'runner-94f7eafb', 'link_as_success': False}, 'rejected D linkage')
    handoff = plan['registration_handoff']
    require(handoff['milestone_atomic_idempotency_supported'] is False and handoff['batch_atomic'] is False
            and handoff['milestone_gate'].startswith('STOP'), 'API atomicity honesty')
    payloads = handoff['payloads']
    require(len(payloads['documents']) == 3 and len(payloads['milestones']) == 5
            and len(payloads['work_items']) == len(items) and len(payloads['links']) == 5, 'payload completeness')
    for payload, document in zip(payloads['documents'], plan['documents']):
        body = payload['body']
        require(payload['submit_ready'] is False and body['expected_generation'] is None, 'unresolved CAS submission')
        require(body['document_key'] == document['document_key'] and body['source_task_id'] == plan['source_task_id']
                and body['goal_id'] == GOAL and len(body['change_summary']) >= 10, 'canonical payload provenance')
        require(hashlib.sha256(body['content'].encode()).hexdigest() == document['content_hash'] == payload['expected_content_hash'], 'payload content hash')
    for payload, milestone in zip(payloads['milestones'], plan['milestones']):
        require(payload['ref'] == milestone['ref'] and payload['submit_ready'] is False, 'milestone submission gate')
        require(payload['body'] == {'title': milestone['title'], 'sequence': None,
                'completion_criteria': milestone['completion_criteria'], 'auto_advance': False}, 'milestone DTO payload')
        require(payload['not_in_dto'] == ['idempotency_key'] and
                payload['not_forwarded_by_handler'] == ['description', 'epics'], 'milestone handler limitations')
    for payload, item in zip(payloads['work_items'], items):
        body = payload['body']
        require(payload['submit_ready'] is False and payload['ref'] == item['ref'], 'unresolved item submission')
        require(set(body) == {'type','milestone_id','parent_id','title','description','acceptance_criteria','priority','idempotency_key'}, 'work DTO fields')
        require(body['type'] == item['type'] and body['idempotency_key'] == item['idempotency_key'], 'work payload identity')
        require(body['milestone_id'] == item.get('milestone_id', '$resolved.' + item['milestone_ref']), 'payload milestone substitution')
        require(body['parent_id'] == (None if item['parent_ref'] is None else '$resolved.' + item['parent_ref']), 'payload parent substitution')
        require(item['completion_criteria'] in body['description'] and body['acceptance_criteria'] == item['acceptance_criteria'], 'criteria payload mapping')
        require(item['responsible_files'] in body['description'] and item['test_id'] in body['description']
                and item['recovery'] in body['description'], 'payload responsibility/test/recovery')
    for payload, expected in zip(payloads['links'], [('C0','D'), ('C1','S'), ('C2','I'), ('C3','I'), ('C4','E')]):
        ref, role = expected
        require(payload['submit_ready'] is False and payload['body'] == {'milestone_id': '$resolved.'+ref, 'task_type':'pipeline_job', 'task_id':plan['runner_links'][role]['job_id']}, 'explicit C job linkage')
    for document in plan['documents']:
        content = (ROOT/document['path']).read_text()
        require('source_task_id=runner-6fbe30cd' in content, 'current document source')
        require('일반 사이트는 서버 Playwright가 기본' in content and '실행 중 레인 자동변경을 금지' in content, 'document lane contract')
        require('HTTP/API/process fallback' in content and 'filled/empty' in content, 'document login contract')
        for source in PINNED['sources']:
            require(source['id'] in content and source['content_hash'] in content, 'document rev2 provenance')
        require(all(test['id'] in content for test in plan['pm_tests']), 'document PM trace')


def supplemental_mutations():
    """Each review blocker has an executable local negative fixture."""
    return [
        ('goal title changed', lambda p: p.update(goal_title='changed')),
        ('milestone false auto advance payload', lambda p: p['registration_handoff']['payloads']['milestones'][0]['body'].update(auto_advance=True)),
        ('milestone guessed sequence', lambda p: p['registration_handoff']['payloads']['milestones'][0]['body'].update(sequence=14)),
        ('wrong current source', lambda p: p.update(source_task_id='runner-94f7eafb')),
        ('original key rewritten', lambda p: p['work_items'][0].update(idempotency_key='different')),
        ('stale source revision', lambda p: p['source_references'][0].update(revision_number=1)),
        ('false approved source', lambda p: p['source_references'][0].update(approved=True)),
        ('source contract None', lambda p: p['source_references'][0].update(content_hash=None)),
        ('missing M hierarchy', lambda p: p['legacy_work_items'].pop()),
        ('legacy owner changed', lambda p: p['existing_milestones']['snapshots'][0].update(owner_session_id='changed')),
        ('legacy evidence changed', lambda p: p['existing_milestones']['snapshots'][0].update(evidence=None)),
        ('legacy scope changed', lambda p: p['existing_milestones']['snapshots'][0].update(completion_criteria='new scope')),
        ('wrong existing M id', lambda p: p['legacy_work_items'][0].update(milestone_id='fabricated')),
        ('legacy task false done', lambda p: p['legacy_work_items'][2].update(status='done')),
        ('legacy parent cross M', lambda p: p['legacy_work_items'][2].update(parent_ref=p['legacy_work_items'][4]['ref'])),
        ('legacy dependency cycle', lambda p: p['legacy_work_items'][2].update(depends_on=[p['legacy_work_items'][2]['ref']])),
        ('legacy missing expected result', lambda p: p['legacy_work_items'][2].update(expected_result='')),
        ('PC default', lambda p: p['lane_policy'].update(default='pc')),
        ('PC mandatory', lambda p: p['lane_policy'].update(pc_required=True)),
        ('PC unconditional', lambda p: p['lane_policy'].update(pc_only_if=[])),
        ('automatic lane switch', lambda p: p['lane_policy'].update(auto_lane_change=True)),
        ('new device no reauth', lambda p: p['lane_policy'].update(new_device_reauthentication=False)),
        ('work key binding removed', lambda p: p['lane_policy']['bind'].remove('work_key')),
        ('account postcondition removed', lambda p: p['lane_policy']['login_success'].remove('account_match')),
        ('HTTP200 login', lambda p: p['lane_policy'].update(http200_is_login=True)),
        ('fallback login', lambda p: p['lane_policy'].update(fallback_is_login=True)),
        ('false server E2E', lambda p: p['lane_policy']['server_e2e'].update(status='passed')),
        ('false PC E2E', lambda p: p['lane_policy']['pc_e2e'].update(status='passed')),
        ('recipe discovery skipped', lambda p: p['recipe_contract'].update(discover_first='create new')),
        ('recipe state conflation', lambda p: p['recipe_contract'].update(state_machine=['done'])),
        ('recipe submit repeats', lambda p: p['recipe_contract'].update(authenticated_skip_submit=False)),
        ('password value read', lambda p: p['recipe_contract'].update(password_evidence='value')),
        ('AccessDenied success register', lambda p: p['recipe_contract']['failure_not_register'].remove('access_denied')),
        ('recipe force approved', lambda p: p['recipe_contract'].update(force_approval=True)),
        ('recipe piece missing', lambda p: p['recipe_contract']['pieces'].pop()),
        ('recipe wrong predecessor', lambda p: p['recipe_contract']['pieces'][1]['predecessor'].update(piece='aggregate')),
        ('recipe predecessor hash omitted', lambda p: p['recipe_contract']['pieces'][1]['predecessor'].pop('hash')),
        ('recipe fabricated origin', lambda p: p['recipe_contract']['pieces'][0].update(exact_origin='https://guessed.invalid')),
        ('recipe other work key', lambda p: p['recipe_contract']['pieces'][0].update(work_key='other')),
        ('recipe secret literal', lambda p: p['recipe_contract']['pieces'][1].update(secret_variables=['plain-secret'])),
        ('false recipe registration', lambda p: p['recipe_contract']['pieces'][0].update(registration_status='registered')),
        ('false recipe live', lambda p: p['recipe_contract']['pieces'][0].update(e2e_status='passed')),
        ('requirement no tests', lambda p: p['requirements'][0].update(tests=[])),
        ('test ID only criteria', lambda p: p['test_contracts'][0].update(expected='')),
        ('false product test', lambda p: p['test_contracts'][0].update(status='passed')),
        ('PM test missing', lambda p: p['pm_tests'].pop()),
        ('PM test false passed', lambda p: p['pm_tests'][0].update(status='passed')),
        ('tombstone cache conflation', lambda p: p['upstream_contract'].update(**{'PM-C3':'24h delete all'})),
        ('rejected old D', lambda p: p['runner_links']['D'].update(job_id='runner-94f7eafb')),
        ('E wrong job', lambda p: p['runner_links']['E'].update(job_id=None)),
        ('false milestone idempotency', lambda p: p['registration_handoff'].update(milestone_atomic_idempotency_supported=True)),
        ('false batch atomicity', lambda p: p['registration_handoff'].update(batch_atomic=True)),
        ('unresolved generation submitted', lambda p: p['registration_handoff']['payloads']['documents'][0].update(submit_ready=True)),
        ('payload different content', lambda p: p['registration_handoff']['payloads']['documents'][0]['body'].update(content='altered')),
        ('blocked goal unrelated M link', lambda p: p['registration_handoff']['payloads']['links'][0]['body'].update(milestone_id=p['existing_milestones']['snapshots'][0]['id'])),
    ]


def main():
    plan = json.loads(MANIFEST.read_text())
    check(plan)
    print('PASS: rev2 provenance, document/payload hashes, R01-R08 tests, C0-C4 5/10/20 + M1-M13 13/13/13, criteria/DAG, server-first, recipe pieces, safe API handoff')
    mutations = [
        ('wrong goal', lambda p: p.update(goal_id='wrong')),
        ('owner changed', lambda p: p.update(owner_session_id='wrong')),
        ('tenant changed', lambda p: p.update(tenant_id='wrong')),
        ('active goal', lambda p: p.update(goal_status='active')),
        ('duplicate key', lambda p: p['work_items'][1].update(idempotency_key=p['work_items'][0]['idempotency_key'])),
        ('missing parent', lambda p: p['work_items'][2].update(parent_ref='absent')),
        ('wrong parent type', lambda p: p['work_items'][2].update(parent_ref=p['work_items'][0]['ref'])),
        ('empty acceptance', lambda p: p['work_items'][2].update(acceptance_criteria=[])),
        ('empty completion', lambda p: p['work_items'][2].update(completion_criteria='')),
        ('false done', lambda p: p['work_items'][2].update(status='done')),
        ('auto advance', lambda p: p['milestones'][0].update(auto_advance=True)),
        ('dependency cycle', lambda p: p['milestones'][0].update(depends_on=['C4'])),
        ('hash mismatch', lambda p: p['documents'][0].update(content_hash='0'*64)),
        ('missing source', lambda p: p['documents'][0].update(source_task_id='')),
        ('short reason', lambda p: p['documents'][0].update(change_summary='짧음')),
        ('false registration', lambda p: p['documents'][0].update(registration_status='registered')),
        ('missing test mapping', lambda p: p['requirements'][0].update(tests=['T-NONE'])),
        ('false DB replay', lambda p: p.update(database_replay_growth=0)),
        ('false runner verified', lambda p: p['runner_links']['D'].update(verified=True)),
    ]
    mutations += supplemental_mutations()
    # Every mandatory recipe field also has its own missing-field negative fixture.
    for field in ("exact_origin", "exact_domain", "lane", "work_key", "account_ref", "page_ref",
                  "starts_when", "succeeds_when", "verify_assertion", "predecessor",
                  "screenshot_ref", "artifact_ref", "run_ref", "failure_return",
                  "credential_scope", "secret_variables"):
        mutations.append(("recipe missing " + field, lambda p, f=field: p["recipe_contract"]["pieces"][0].pop(f)))
    for name, mutate in mutations:
        invalid = copy.deepcopy(plan)
        mutate(invalid)
        try:
            check(invalid)
        except (ValueError, KeyError):
            continue
        raise AssertionError('mutation not rejected: ' + name)
    print(f'PASS: {len(mutations)} negative mutations rejected; live DB/API/site tests NOT RUN')


if __name__ == '__main__':
    main()
