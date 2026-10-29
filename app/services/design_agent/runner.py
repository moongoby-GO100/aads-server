"""Explicit opt-in runner. No startup hook, scheduler, or LLM calls."""
import hashlib
import json

from .budget import BudgetExceeded, RunBudget, check_run
from .config import Config
from .queue import claim_next, mark_done, mark_failed
from .writer import DesignAgentWriter


async def noop(trigger, writer, budget):
    """D1 placeholder: no model calls and no design writes."""


HANDLERS = {'project_created': noop, 'deploy_succeeded': noop, 'manual': noop,
            'screen_change_requested': noop, 'new_screen_requested': noop}


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
            if not status:
                try:
                    # Roll back handler writes on failure while retaining the run audit.
                    writer = DesignAgentWriter(conn, tenant_id, trigger['project_key'], run_id)
                    async with conn.transaction():
                        await HANDLERS[handler](trigger, writer, budget)
                    status = 'succeeded'
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
            if status == 'failed':
                await mark_failed(conn, tenant_id, trigger['id'])
            else:
                await mark_done(conn, tenant_id, trigger['id'], status != 'succeeded')
            return {'id': run_id, 'status': status}
