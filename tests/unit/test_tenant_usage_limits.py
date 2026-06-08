from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import inspect
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.routers import chat as chat_router
from app.services import chat_service
from app.services import model_selector
from app.services import oauth_usage_tracker
from app.services import tenant_usage_limits as limits
from app.services import tool_executor


def _usage(tokens: int = 0, cost: str = "0", calls: int = 0) -> limits.TenantMonthlyUsage:
    return limits.TenantMonthlyUsage(
        tenant_id="00000000-0000-0000-0000-000000000001",
        month_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        calls=calls,
        input_tokens=tokens,
        output_tokens=0,
        total_tokens=tokens,
        cost_usd=Decimal(cost),
    )


def test_tenant_usage_soft_and_hard_limit_evaluation():
    policy = limits.TenantPlanPolicy(
        plan_key="free",
        monthly_token_limit=100,
        monthly_cost_limit_usd=Decimal("10"),
        monthly_call_limit=10,
        soft_limit_ratio=Decimal("0.8"),
        hard_limit_ratio=Decimal("1.0"),
    )

    soft = limits.evaluate_usage_limit(
        tenant_id="tenant-a",
        operation="chat",
        usage=_usage(tokens=70, calls=1),
        policy=policy,
        projected_tokens=15,
    )
    assert soft.allowed
    assert soft.status == "soft_limit"
    assert soft.limit_name == "tokens"

    hard = limits.evaluate_usage_limit(
        tenant_id="tenant-a",
        operation="chat",
        usage=_usage(tokens=95, calls=1),
        policy=policy,
        projected_tokens=10,
    )
    assert not hard.allowed
    assert hard.status == "hard_limit"
    assert hard.limit_name == "tokens"


def test_internal_tenant_and_admin_override_bypass_hard_limit():
    usage = _usage(tokens=999, cost="999", calls=999)
    policy = limits.TenantPlanPolicy(
        plan_key="internal",
        monthly_token_limit=1,
        monthly_cost_limit_usd=Decimal("1"),
        monthly_call_limit=1,
        soft_limit_ratio=Decimal("0.8"),
        hard_limit_ratio=Decimal("1.0"),
        internal_exempt=True,
    )
    internal = limits.evaluate_usage_limit(
        tenant_id="internal",
        operation="model:claude",
        usage=usage,
        policy=policy,
    )
    assert internal.allowed
    assert internal.status == "internal_exempt"

    customer_policy = limits.TenantPlanPolicy(
        plan_key="free",
        monthly_token_limit=1,
        monthly_cost_limit_usd=Decimal("1"),
        monthly_call_limit=1,
        soft_limit_ratio=Decimal("0.8"),
        hard_limit_ratio=Decimal("1.0"),
    )
    override = limits.evaluate_usage_limit(
        tenant_id="customer",
        operation="tool:query_database",
        usage=usage,
        policy=customer_policy,
        admin_override=True,
    )
    assert override.allowed
    assert override.status == "admin_override"


def test_usage_summary_ratio_helper_handles_zero_and_decimal_limits():
    assert limits._usage_ratio(50, 100) == 0.5
    assert limits._usage_ratio(1, 0) == 0.0
    assert limits._usage_ratio(Decimal("2.50"), Decimal("10.00")) == 0.25


def test_tenant_usage_migration_extends_logs_and_policy_tables():
    sql = Path("migrations/102_saas_tenant_usage_limits.sql").read_text(encoding="utf-8")

    for table in ("oauth_usage_log", "bg_llm_usage_log", "cost_tracking"):
        assert f"ALTER TABLE {table}" in sql
        assert "ADD COLUMN IF NOT EXISTS tenant_id UUID" in sql
        assert f"fk_{table}_tenant" in sql

    assert "CREATE TABLE IF NOT EXISTS tenant_plan_limits" in sql
    assert "CREATE TABLE IF NOT EXISTS tenant_usage_overrides" in sql
    assert "idx_oauth_usage_tenant_month" in sql
    assert "idx_bg_llm_usage_tenant_month" in sql
    assert "idx_cost_tracking_tenant_month" in sql


def test_chat_model_tool_hooks_are_tenant_aware():
    chat_route_source = inspect.getsource(chat_router.send_message)
    chat_stream_source = inspect.getsource(chat_service.send_message_stream)
    model_source = inspect.getsource(model_selector.call_stream)
    tool_source = inspect.getsource(tool_executor.ToolExecutor.execute)
    oauth_source = inspect.getsource(oauth_usage_tracker.log_usage)

    assert "check_tenant_usage_limit" in chat_route_source
    assert "current_tenant_id.set(tenant_id)" in chat_route_source
    assert "tenant_id: Optional[str] = None" in chat_stream_source
    assert "tenant_id=resolved_tenant_id" in chat_stream_source
    assert "operation=f\"model:{model}\"" in model_source
    assert "operation=f\"tool:{tool_name}\"" in tool_source
    assert "tenant_id: Optional[str] = None" in oauth_source
    assert "\"tenant_id\"" in oauth_source
