"""Document cross-company reviews using the existing CLI relay contract."""
from dataclasses import dataclass
import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import HTTPException
from app.core.db_pool import get_pool
from app.services import doc_review_rubric as rubric
from app.services.doc_tier import compute_doc_tier, provider_of, settings

logger = logging.getLogger(__name__)
_TASKS = set()


@dataclass(frozen=True)
class ReviewerPick:
    model: str
    role: str = 'cross'


def pick_reviewers(tier, author_model, tier_list, *, available, extra_lists=(), exclude=()):
    author = provider_of(author_model)
    candidates = [m for m in dict.fromkeys(tier_list + [m for ls in extra_lists for m in ls])
                  if m in available and m not in exclude and m != author_model
                  and provider_of(m) in ('anthropic', 'openai')]
    if author not in ('anthropic', 'openai'):
        picks = [ReviewerPick(next((m for m in candidates if provider_of(m) == p), ''))
                 for p in ('anthropic', 'openai')]
        return ([p for p in picks if p.model], 'ok' if all(p.model for p in picks) else 'blocked_no_cross')
    cross = next((m for m in candidates if provider_of(m) != author), None)
    if not cross:
        return [], 'blocked_no_cross'
    picks = [ReviewerPick(cross)]
    if tier == 'D3':
        same = next((m for m in tier_list if m in candidates and provider_of(m) == author), None)
        if not same:
            return picks, 'blocked_no_second'
        picks.append(ReviewerPick(same, 'same_company_other_model'))
    return picks, 'ok'


def pick_follow_up(author_model, lists, *, available, needed, rerun=(), exclude=()):
    """후속 검토 리뷰어. 다시 돌릴 모델을 먼저 넣고, 그래도 빈 회사는 모든 크기 목록에서 그 회사 모델을 찾는다.

    pick_reviewers 의 D3 규칙(같은 회사 후보를 첫 목록에서만 찾음)을 쓰지 않는다 — 필요한 회사 모델이 다른 크기
    목록에만 있어도 채울 수 있어야 한다. 작성 모델은 고르지 않는다.
    """
    ordered = [m for m in dict.fromkeys(m for ls in lists for m in ls)
               if m in available and m not in exclude and m != author_model
               and provider_of(m) in ('anthropic', 'openai')]
    picks, covered = [], set()
    for model in ordered:
        if model in rerun:
            picks.append(ReviewerPick(model, 'follow_up_rerun'))
            covered.add(provider_of(model))
    for provider in sorted(set(needed) - covered):
        model = next((m for m in ordered if provider_of(m) == provider and m not in rerun), None)
        if model:
            picks.append(ReviewerPick(model, 'follow_up'))
    return picks


def tier_of(revision, head):
    return revision.get('doc_tier') or compute_doc_tier(head.get('kind', ''), len(revision.get('content') or ''), bool(head.get('goal_linked')))


def _serialize_at(created_at):
    if created_at is None:
        return None
    if hasattr(created_at, 'isoformat'):
        return created_at.isoformat()
    return created_at


def _latest_by_model(rows):
    latest = {}
    for r in rows:
        if r['verdict'] != 'attempt_failed' or r['reviewer_model'] not in latest:
            latest[r['reviewer_model']] = r
    return latest


def _scrub_nonfinite(value):
    """읽기 경로: NaN·±Infinity 를 재귀적으로 None 으로 바꾼다. (정제값, 바꾼 개수)."""
    if isinstance(value, float):
        return (value, 0) if math.isfinite(value) else (None, 1)
    if isinstance(value, dict):
        out, count = {}, 0
        for k, v in value.items():
            out[k], n = _scrub_nonfinite(v)
            count += n
        return out, count
    if isinstance(value, list):
        out, count = [], 0
        for v in value:
            item, n = _scrub_nonfinite(v)
            out.append(item)
            count += n
        return out, count
    return value, 0


def _reason_has_nonfinite(row):
    """저장된 검토 근거에 NaN·±Infinity 가 있는가. 있으면 그 pass 는 승인 근거로 쓰지 않는다."""
    _, data = rubric.parse_review_reason(row.get('reason'))
    return isinstance(data, dict) and _scrub_nonfinite(data)[1] > 0


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def build_review_detail(rows):
    """검토 모델별 최신 결과의 쉬운 요약. 목록·상세·검토 대기 API 가 모두 이 함수를 쓴다."""
    reviews = []
    nonfinite_total = 0
    for r in _latest_by_model(rows).values():
        text, data = rubric.parse_review_reason(r.get('reason'))
        data, nonfinite = _scrub_nonfinite(data if isinstance(data, dict) else {})
        raw_issues = data.get('issues')
        raw_issues = raw_issues if isinstance(raw_issues, list) else []
        issues = [i for i in raw_issues if isinstance(i, dict)]
        raw_checks = data.get('checks')
        checks = [c for c in raw_checks if isinstance(c, dict)] if isinstance(raw_checks, list) else []
        score = data.get('score')
        score = score if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        scores = data.get('scores')
        brief = data.get('ceo_brief')
        review = {'model': r['reviewer_model'], 'verdict': r['verdict'], 'at': _serialize_at(r.get('created_at')),
                  'summary': text, 'structured': bool(data),
                  'ceo_brief': brief if isinstance(brief, str) else None, 'score': score,
                  'scores': scores if isinstance(scores, dict) else None, 'issues': issues,
                  'invalid_issue_count': len(raw_issues) - len(issues), 'checks': checks,
                  'dropped_blockers': _count(data.get('dropped_blockers'))}
        if nonfinite:
            review['warnings'] = ['non_finite_number']
            nonfinite_total += nonfinite
        reviews.append(review)
    briefs = [x['ceo_brief'] for x in reviews if x['ceo_brief']]
    scores = [x['score'] for x in reviews if x['score'] is not None]
    issues = [i for x in reviews for i in x['issues']]
    detail = {'reviews': reviews, 'ceo_brief': briefs[-1] if briefs else None,
              'min_score': min(scores) if scores else None,
              'blocker_count': sum(1 for i in issues if i.get('severity') == 'blocker')
              + sum(x['dropped_blockers'] for x in reviews),
              'invalid_issue_count': sum(x['invalid_issue_count'] for x in reviews)}
    if nonfinite_total:
        detail['warnings'] = ['non_finite_number']
    return detail


def summarize(tier, author_model, rows):
    def serialize_at(created_at):
        if created_at is None:
            return None
        if hasattr(created_at, 'isoformat'):
            return created_at.isoformat()
        return created_at
    models = [{'model': r['reviewer_model'], 'provider': r['reviewer_provider'],
               'verdict': r['verdict'], 'cross_company': r['cross_company'], 'at': serialize_at(r['created_at'])} for r in rows]
    # Only substantive outcomes replace a previous pass/reject.
    latest = {}
    for r in rows:
        if r['verdict'] != 'attempt_failed' or r['reviewer_model'] not in latest:
            latest[r['reviewer_model']] = r
    # 점수가 비유한(NaN·±Infinity)인 pass 는 통과로 세지 않는다 — 재검토 대기(pending)로 남겨 승인을 막는다.
    passes = [r for r in latest.values() if r['verdict'] == 'pass' and not _reason_has_nonfinite(r)]
    providers = {r['reviewer_provider'] for r in passes}
    author = provider_of(author_model)
    passed = (bool(passes) and any(r['cross_company'] for r in passes))
    if tier == 'D3' or author not in ('anthropic', 'openai'):
        passed = len(passes) >= 2 and {'anthropic', 'openai'} <= providers
    rejected = any(r['verdict'] == 'reject' for r in latest.values())
    state = 'rejected' if rejected else 'passed' if passed else (
        'blocked' if any(r['verdict'] == 'blocked' for r in latest.values()) else 'pending')
    return {'doc_tier': tier, 'review_models': models, 'review_state': state, 'review_detail': build_review_detail(rows)}


async def summary(conn, head, revision):
    rows = await conn.fetch('SELECT * FROM project_document_reviews WHERE revision_id=$1 ORDER BY id', revision['id'])
    result = summarize(tier_of(revision, head), revision.get('author_model'), rows)
    if revision.get('doc_tier') is None and not rows:
        result['review_state'] = 'not_required'
    return result


async def notify_blocked(tenant, project, reason):
    from app.services.push_notifications import send_web_push_to_user
    try:
        async with get_pool().acquire() as conn:
            owners = await conn.fetch("SELECT user_id::text AS user_id FROM tenant_memberships WHERE tenant_id=$1::uuid AND role='owner' AND status='active' AND deleted_at IS NULL", tenant)
        for owner in owners:
            await send_web_push_to_user(tenant_id=tenant, user_id=owner['user_id'], payload={
                'title': '문서 교차 검토 대기', 'body': f'{project}: {reason}', 'url': '/docs'})
    except Exception:
        logger.exception('doc_cross_review_push_failed')
    try:
        from app.services.ceo_notify import send_ceo_text
        await send_ceo_text(f'문서 교차 검토 대기 {project}: {reason}')
    except Exception:
        logger.exception('doc_cross_review_telegram_failed')


# project_document_reviews.role CHECK 가 허용하는 값(migrations/20261029_document_tier.sql).
STORED_ROLES = frozenset({'cross', 'same_company_other_model', 'code'})


def stored_role(pick_role, cross_company):
    """후속 검토 선택 역할(follow_up·follow_up_rerun)은 DB 제약에 없다 — 회사 관계로 저장 값을 정한다.

    7d766b8e(2026-11-05) 이후 후속 검토 INSERT 가 전부 CheckViolation 으로 실패해, 두 번째 회사 검토가 한 건도
    기록되지 않고 자동승인이 needs_second_company 로 멈췄다(2026-11-06 실측).
    """
    if pick_role in STORED_ROLES:
        return pick_role
    return 'cross' if cross_company else 'same_company_other_model'


async def _record(conn, tenant, project, head, revision, pick, verdict, reason):
    cross_company = (provider_of(pick.model) != provider_of(revision.get('author_model'))
                     and provider_of(pick.model) in ('anthropic', 'openai'))
    await conn.execute('INSERT INTO project_document_reviews '
                       '(tenant_id,project_key,head_id,revision_id,reviewer_model,reviewer_provider,role,cross_company,verdict,reason,source_task_id) '
                       'VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
                       tenant, project, head['id'], revision['id'], pick.model, provider_of(pick.model),
                       stored_role(pick.role, cross_company), cross_company,
                       verdict, reason, revision.get('source_task_id'))


async def _approved_titles(conn, tenant, project, head):
    """같은 프로젝트 승인 문서의 제목·key 만(본문 제외). 조회 실패는 검토를 막지 않는다."""
    try:
        from app.api.canonical_documents import approved_brief
        rows = await approved_brief(conn, tenant, project, limit=8)
    except Exception:
        logger.warning('doc_cross_review_approved_titles_failed', exc_info=True)
        return []
    own = head.get('document_key')
    return [{'title': str(r.get('title') or '')[:120], 'document_key': str(r.get('document_key') or '')[:128]}
            for r in rows if r.get('document_key') != own][:8]


async def execute_review(tenant, project, head, revision, *, follow_up=False):
    from app.services import code_reviewer as cli
    tier = tier_of(revision, head)
    if tier == 'SPLIT':
        raise HTTPException(409, {'code': 'document_split_required', 'message': '분할 권고: 문서를 나누어 등록하세요'})
    token = uuid.uuid4()
    async with get_pool().acquire() as conn:
        locked = await conn.fetchval(
            "INSERT INTO document_cross_review_leases(revision_id,owner,expires_at) VALUES($1,$2,now()+interval '5 minutes') "
            "ON CONFLICT(revision_id) DO UPDATE SET owner=EXCLUDED.owner,expires_at=EXCLUDED.expires_at "
            "WHERE document_cross_review_leases.expires_at < now() RETURNING owner", revision['id'], token)
        if not locked:
            return await summary(conn, head, revision)
    try:
        async with get_pool().acquire() as conn:
            current = await summary(conn, head, revision)
            if current['review_state'] == 'passed' and not follow_up:
                return current
            needed, rerun = set(), set()
            if follow_up:
                from app.services import doc_auto_approval
                prior = await conn.fetch('SELECT * FROM project_document_reviews WHERE revision_id=$1 ORDER BY id', revision['id'])
                needed, rerun = doc_auto_approval.follow_up_targets(prior)
                if not needed and not rerun:
                    return current
            approved = await _approved_titles(conn, tenant, project, head)
            prior_findings = []
            try:
                from app.services import doc_auto_fix
                prior_findings = await doc_auto_fix.previous_findings(conn, tenant, project, head, revision)
            except Exception:
                logger.warning('doc_cross_review_prior_findings_failed', exc_info=True)
            sizes = ['AI_REVIEW_LIGHT', 'AI_REVIEW_STD', 'AI_REVIEW_HEAVY']
            lists = []
            for size in sizes[{'D1': 0, 'D2': 1, 'D3': 2}[tier]:]:
                raw = await conn.fetchval('SELECT models FROM runner_model_config WHERE size=$1', size)
                models = json.loads(raw) if isinstance(raw, str) else list(raw or [])
                if not models:
                    raw = await conn.fetchval("SELECT models FROM runner_model_config WHERE size='AI_REVIEW'")
                    models = json.loads(raw) if isinstance(raw, str) else list(raw or [])
                lists.append(models)
        kind = head.get('kind') or ''
        try:
            checks = rubric.run_checks(kind, revision.get('title'), revision.get('content'), tier)
        except Exception:
            logger.warning('doc_cross_review_checks_failed', exc_info=True)
            checks = [{'id': 'checks_unavailable', 'ok': False, 'detail': '결정적 검사를 실행하지 못함'}]
        system, prompt = rubric.build_review_request(kind, tier, revision.get('title'), revision.get('content'), checks, approved,
                                                     prior_findings=prior_findings)
        available = {m for ls in lists for m in ls if cli._is_cli_review_model(m)}
        excluded, completed = set(), set()
        while True:
            if follow_up:
                # 후속 검토는 아직 채워지지 않은 회사·다시 돌릴 모델만 부른다. 못 구하면 기록·알림 없이 멈춘다.
                picks = pick_follow_up(revision.get('author_model'), lists, available=available,
                                       needed=needed, rerun=rerun, exclude=excluded)
                status = 'ok'
                if not picks:
                    logger.warning('doc_cross_review_follow_up_unavailable revision_id=%s status=%s', revision['id'], status)
                    break
            else:
                picks, status = pick_reviewers(tier, revision.get('author_model'), lists[0], available=available,
                                              extra_lists=lists[1:], exclude=excluded)
            async with get_pool().acquire() as conn:
                if status == 'ok' and (tier == 'D3' or follow_up):
                    async with conn.transaction():
                        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'doc-d3:' + tenant)
                        count = await conn.fetchval("SELECT count(*) FROM document_cross_review_budget WHERE tenant_id=$1::uuid AND review_day=CURRENT_DATE", tenant)
                        reserved = await conn.fetchval('SELECT 1 FROM document_cross_review_budget WHERE revision_id=$1 AND review_day=CURRENT_DATE', revision['id'])
                        if not reserved and count >= settings()['d3_daily_limit']:
                            status = 'blocked_daily_limit: 하루 D3 검토 상한 초과'
                        elif not reserved:
                            await conn.execute('INSERT INTO document_cross_review_budget(tenant_id,revision_id,review_day) VALUES($1::uuid,$2,CURRENT_DATE)', tenant, revision['id'])
                if status != 'ok':
                    if not follow_up:
                        await _record(conn, tenant, project, head, revision, ReviewerPick('unconfigured'), 'blocked', status)
                    await conn.execute('DELETE FROM document_cross_review_budget WHERE revision_id=$1 AND review_day=CURRENT_DATE', revision['id'])
            if status != 'ok':
                if follow_up:
                    logger.warning('doc_cross_review_follow_up_blocked revision_id=%s status=%s', revision['id'], status)
                else:
                    await notify_blocked(tenant, project, status)
                break
            failed = False
            for pick in picks:
                if pick.model in completed:
                    continue
                async with get_pool().acquire() as conn:
                    owned = await conn.fetchval("UPDATE document_cross_review_leases SET expires_at=now()+interval '5 minutes' WHERE revision_id=$1 AND owner=$2 AND expires_at>now() RETURNING owner", revision['id'], token)
                    if not owned:
                        return await summary(conn, head, revision)
                try:
                    response = await asyncio.wait_for(cli._call_review_model(
                        model=pick.model, prompt=prompt, system=system, max_tokens=4096), timeout=180)
                    text, _ = cli._extract_review_text(response)
                    verdict, reason = rubric.finalize_review(cli._parse_review_json(text), checks)
                    completed.add(pick.model)
                except Exception as exc:
                    verdict, reason = 'attempt_failed', cli._sanitize_review_text(str(exc), limit=500)
                    excluded.add(pick.model)
                    failed = True
                async with get_pool().acquire() as conn, conn.transaction():
                    owned = await conn.fetchval('SELECT owner FROM document_cross_review_leases WHERE revision_id=$1 AND owner=$2 AND expires_at>now() FOR UPDATE', revision['id'], token)
                    if not owned:
                        return await summary(conn, head, revision)
                    await _record(conn, tenant, project, head, revision, pick, verdict, reason)
                if failed:
                    break
            if not failed:
                break
        async with get_pool().acquire() as conn:
            outcome = await summary(conn, head, revision)
    finally:
        async with get_pool().acquire() as conn:
            await conn.execute('DELETE FROM document_cross_review_leases WHERE revision_id=$1 AND owner=$2', revision['id'], token)
    await _auto_approve_after_review(tenant, project, head, revision, outcome, allow_follow_up=not follow_up)
    return outcome


async def _auto_approve_after_review(tenant, project, head, revision, outcome, *, allow_follow_up):
    """리뷰 기록 직후 자동승인 판정. 임대(lease)를 놓은 뒤에 불러야 후속 검토가 임대에 막히지 않는다. 실패는 로그만.

    리뷰 기록이 끝난 모든 상태(반려·차단 포함)를 판정한다 — 승인은 evaluate 가 review_state=='passed' 일 때만 내므로
    기존과 같고, 반려·차단 지적으로 보류된 문서는 hold 로 자동 보완(doc_auto_fix) 계획을 탄다.
    """
    passed = outcome.get('review_state') == 'passed'
    try:
        from app.services import doc_auto_approval
        async with get_pool().acquire() as conn:
            decision = await doc_auto_approval.maybe_auto_approve(conn, tenant, project, head['id'], revision['id'])
            if passed and allow_follow_up and decision.action == 'hold' and 'needs_second_company' in decision.reasons:
                await doc_auto_approval.request_follow_up_review(conn, tenant, project, head, revision)
            from app.services import doc_auto_fix
            if doc_auto_fix._plannable(decision):
                try:
                    # hold 는 보완, 마이그레이션 skip 은 상신, 자동승인 off(skip mode_off) 는 계획 로그만 남긴다.
                    await doc_auto_fix.maybe_schedule_auto_fix(
                        conn, tenant, project, head['id'], revision['id'],
                        None if decision.reasons == ['mode_off'] else decision)
                except Exception:
                    logger.exception('doc_auto_fix_failed revision_id=%s', revision.get('id'))
    except Exception:
        logger.exception('doc_auto_approval_failed revision_id=%s', revision.get('id'))


_DETACHED_TIMEOUT_SECONDS = 1500


def _is_short_lived_bridge():
    return os.getenv('AADS_MCP_BRIDGE', '').strip().lower() in ('1', 'true', 'yes')


def _app_root():
    return '/app' if os.path.isdir('/app') else str(Path(__file__).resolve().parents[2])


def _open_worker_log(root):
    """Append-mode log file for the detached worker's stdout/stderr; None if no writable location exists."""
    for base in (os.getenv('DOC_CROSS_REVIEW_WORKER_LOG_DIR') or os.path.join(root, 'logs'), tempfile.gettempdir()):
        try:
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, 'doc_cross_review_worker.log')
            return path, open(path, 'ab')
        except OSError:
            continue
    return None, None


def _watch_worker(proc, revision_id, log_path):
    code = proc.wait()
    if code == 0:
        logger.info('doc_cross_review_worker_exit_ok revision_id=%s', revision_id)
    elif code == 124:
        logger.error('doc_cross_review_worker_timeout revision_id=%s limit=%ss log=%s', revision_id, _DETACHED_TIMEOUT_SECONDS, log_path)
    else:
        logger.error('doc_cross_review_worker_exit_nonzero revision_id=%s code=%s log=%s', revision_id, code, log_path)


def _spawn_detached(tenant, project, head, revision, follow_up=False):
    """Start the review worker detached from this process; returns False when it could not be started."""
    root = _app_root()
    env = dict(os.environ)
    env.pop('AADS_MCP_BRIDGE', None)
    env['PYTHONPATH'] = root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    cmd = ['timeout', str(_DETACHED_TIMEOUT_SECONDS), sys.executable, '-m', 'app.services.doc_cross_review_worker',
           '--tenant', str(tenant), '--project', str(project),
           '--document-key', str(head['document_key']), '--revision-id', str(revision['id'])]
    if follow_up:
        cmd.append('--follow-up')
    log_path, log_file = _open_worker_log(root)
    if log_file is None:
        logger.warning('doc_cross_review_worker_log_unavailable revision_id=%s', revision['id'])
    out = log_file or subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, cwd=root, env=env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT if log_file else subprocess.DEVNULL)
    except Exception:
        logger.exception('doc_cross_review_detach_failed revision_id=%s', revision['id'])
        return False
    finally:
        if log_file:
            log_file.close()
    logger.warning('doc_cross_review_worker_spawned revision_id=%s pid=%s log=%s', revision['id'], proc.pid, log_path)
    threading.Thread(target=_watch_worker, args=(proc, revision['id'], log_path), daemon=True).start()
    return True


def schedule(tenant, project, head, revision, follow_up=False):
    if _is_short_lived_bridge() and (
            _spawn_detached(tenant, project, head, revision, follow_up=True) if follow_up
            else _spawn_detached(tenant, project, head, revision)):
        return

    async def run():
        try:
            if follow_up:
                await execute_review(tenant, project, dict(head), dict(revision), follow_up=True)
            else:
                await execute_review(tenant, project, dict(head), dict(revision))
        except asyncio.CancelledError:
            logger.warning('doc_cross_review_auto_cancelled', extra={'revision_id': str(revision['id'])})
            raise
        except Exception:
            logger.exception('doc_cross_review_auto_failed')
    task = asyncio.create_task(run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def enforce(conn, tenant, project, head, revision_id, *, approve=False):
    if os.getenv('DOC_CROSS_REVIEW_ENFORCE', '1').lower() in ('0', 'false', 'no'):
        return {}
    revision = await conn.fetchrow('SELECT * FROM project_document_revisions WHERE head_id=$1 AND id=$2', head['id'], revision_id)
    if not revision:
        raise HTTPException(404, 'revision_not_found')
    if revision.get('doc_tier') is None:
        return await summary(conn, head, revision)
    result = await summary(conn, head, revision)
    tier = result['doc_tier']
    if tier == 'SPLIT':
        raise HTTPException(409, {'code': 'document_split_required', 'message': '분할 권고: 문서를 나누어 등록하세요'})
    if tier == 'D1':
        if approve:
            raise HTTPException(409, {'code': 'approval_not_required', 'message': 'D1 문서는 승인 단계가 없습니다'})
        if revision.get('doc_tier') is not None and result['review_state'] in ('pending', 'blocked'):
            schedule(tenant, project, head, revision)
        return result
    if result['review_state'] != 'passed':
        if result['review_state'] in ('pending', 'blocked'):
            schedule(tenant, project, head, revision)
        raise HTTPException(409, {'code': 'cross_review_required', 'message': '다른 회사 모델의 교차 검토가 필요합니다. 잠시 후 다시 요청하세요', **result})
    return result
