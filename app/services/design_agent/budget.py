"""No LLM client: accounting only. Caller holds a tenant transaction lock."""
from dataclasses import dataclass
from decimal import Decimal


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class RunBudget:
    limit: int = 15
    llm_calls: int = 0
    cost_usd: Decimal = Decimal('0')

    def record_call(self, cost_usd=Decimal('0')):
        cost = Decimal(str(cost_usd))
        if not cost.is_finite() or cost < 0:
            raise ValueError('Invalid cost')
        if self.llm_calls >= min(15, self.limit):
            raise BudgetExceeded('Per-run LLM call limit reached')
        self.llm_calls += 1
        self.cost_usd += cost


async def check_run(conn, tenant_id, project_key, handler, input_hash, cap):
    duplicate = await conn.fetchval("""SELECT EXISTS (
        SELECT 1 FROM design_agent_runs WHERE tenant_id=$1 AND project_key=$2
        AND handler=$3 AND input_hash=$4 AND status='succeeded')""",
        tenant_id, project_key, handler, input_hash)
    if duplicate:
        return 'skipped_duplicate'
    cost = await conn.fetchval("""SELECT COALESCE(SUM(cost_usd),0)
        FROM design_agent_runs WHERE tenant_id=$1
        AND started_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Seoul')
            AT TIME ZONE 'Asia/Seoul')
        AND started_at < ((date_trunc('day', now() AT TIME ZONE 'Asia/Seoul')
            + interval '1 day') AT TIME ZONE 'Asia/Seoul')""", tenant_id)
    return 'blocked_budget' if Decimal(str(cost)) >= cap else None
