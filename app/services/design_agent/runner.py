"""Explicit opt-in runner. No startup hook, scheduler, or LLM calls."""
import hashlib
import json

from .audit import handle_deploy_audit
from .budget import BudgetExceeded, RunBudget, check_run
from .config import Config
from .generate import handle_generate
from .importer import handle_import
from .intake import assert_brief_confirmed
from .queue import claim_next, mark_done, mark_failed
from .skills import load_skill, required_skills, resolve_situation
from .writer import DesignAgentWriter


async def noop(trigger, writer, budget):
    """D1 placeholder: no model calls and no design writes."""


async def handle_manual(trigger, writer, budget):
    payload = trigger['payload']
    if isinstance(payload, str):
        payload = json.loads(payload)
    if (payload or {}).get('kind') == 'genspark_import':
        return await handle_import(trigger, writer, budget)
    return await noop(trigger, writer, budget)


BRIEF_UNCONFIRMED = 'design_brief_unconfirmed'


class BriefBlocked(Exception):
    """New-screen generation refused before any write; not a successful run."""

    def __init__(self, reason=BRIEF_UNCONFIRMED):
        super().__init__(reason)
        self.reason = reason


def _norm(value):
    value = value.strip() if isinstance(value, str) else value
    return value or None


async def handle_new_screen(trigger, writer, budget):
    """Brief gate (D1.6) in front of generation: confirmed AND scoped to this project/screen."""
    payload = trigger['payload']
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload = payload or {}
    session_id = payload.get('intake_session_id')
    if not session_id:
        raise BriefBlocked()
    conn = writer._conn
    # Share-lock the session row first: record_answer/save_question take FOR UPDATE and
    # would reset brief_confirmed_at, so they must wait until this transaction ends.
    session = await conn.fetchrow("""SELECT project_key, screen_ref FROM design_intake_sessions
        WHERE id=$1 AND tenant_id=$2 FOR SHARE""", session_id, writer.tenant_id)
    if session is None:
        raise BriefBlocked()
    try:
        await assert_brief_confirmed(conn, writer.tenant_id, session_id)
    except ValueError as exc:
        raise BriefBlocked() from exc
    screen = _norm(payload.get('screen_ref') or payload.get('screen_id'))
    if (_norm(session['project_key']) != writer.project_key
            or _norm(session['screen_ref']) != screen):
        raise BriefBlocked()
    return await handle_generate(trigger, writer, budget)


HANDLERS = {'project_created': handle_generate, 'deploy_succeeded': handle_deploy_audit,
            'manual': handle_manual, 'screen_change_requested': noop,
            'new_screen_requested': handle_new_screen}


async def process_one(pool, tenant_id, config=None):
    config = config or Config.from_env()
    if not config.enabled:
        return {'status': 'blocked_disabled'}
    if not tenant_id:
        raise ValueError('Tenant is required')
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialize daily budget and duplicate checks within each tenant.
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                               str(tenant_id))
            trigger = await claim_next(conn, tenant_id)
            if not trigger:
                return None
            handler = trigger['event_type']
            payload = trigger['payload']
            if isinstance(payload, str):
                payload = json.loads(payload)
            input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            status = await check_run(conn, tenant_id, trigger['project_key'], handler,
                                     input_hash, config.daily_usd_cap)
            run_id = await conn.fetchval("""INSERT INTO design_agent_runs
                (trigger_id,tenant_id,project_key,handler,input_hash,status)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""", trigger['id'], tenant_id,
                trigger['project_key'], handler, input_hash, status or 'running')
            budget = RunBudget(config.max_llm_calls)
            error = None
            blocked_reason = None
            if not status:
                writer = DesignAgentWriter(conn, tenant_id, trigger['project_key'], run_id)
                try:
                    situation = resolve_situation(handler, payload)
                    names = await required_skills(conn, situation)
                    loaded, failures = [], []
                    for name in names:
                        try:
                            loaded.append(load_skill(name))
                        except ValueError as exc:
                            failures.append(str(exc))
                    if failures:
                        raise ValueError('; '.join(failures))
                    await conn.execute("""UPDATE design_agent_runs SET skills_loaded=$3::jsonb
                        WHERE id=$1 AND tenant_id=$2""", run_id, tenant_id, json.dumps(loaded))
                    # Roll back handler writes on failure while retaining the run audit.
                    async with conn.transaction():
                        await HANDLERS[handler](trigger, writer, budget)
                    status = 'succeeded'
                except BriefBlocked as exc:
                    # The runs CHECK has no 'blocked' status; 'failed' is never a duplicate
                    # source, so the same payload can be resubmitted once the brief is confirmed.
                    status, error, blocked_reason = 'failed', exc.reason, exc.reason
                except Exception as exc:
                    status = 'blocked_budget' if isinstance(exc, BudgetExceeded) else 'failed'
                    error = str(exc)
                    await conn.execute("""UPDATE design_agent_runs SET writes=$3::jsonb
                        WHERE id=$1 AND tenant_id=$2""", run_id, tenant_id,
                        json.dumps([dict(w, rolled_back=True) for w in writer.writes]))
            await conn.execute("""UPDATE design_agent_runs SET status=$3,
                llm_calls=$4,cost_usd=$5,error=COALESCE($6,error),finished_at=now()
                WHERE id=$1 AND tenant_id=$2""", run_id, tenant_id, status,
                budget.llm_calls, budget.cost_usd, error)
            if blocked_reason:
                await mark_done(conn, tenant_id, trigger['id'], True)
                return {'id': run_id, 'status': 'blocked', 'reason': blocked_reason}
            if status == 'failed':
                await mark_failed(conn, tenant_id, trigger['id'])
            else:
                await mark_done(conn, tenant_id, trigger['id'], status != 'succeeded')
            return {'id': run_id, 'status': status}
