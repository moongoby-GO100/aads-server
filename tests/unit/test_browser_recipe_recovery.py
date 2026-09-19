import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/browser_recipe_recovery.py"
SPEC = importlib.util.spec_from_file_location("browser_recipe_recovery_under_test", MODULE_PATH)
recovery = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


def test_failure_classification_and_bounded_retries_are_fail_closed():
    assert recovery.classify_failure(error_code="locator_timeout") == "selector_changed"
    assert recovery.classify_failure(error_code="SESSION_EXPIRED") == "login_expired"
    assert recovery.classify_failure(error_code="gateway timeout") == "network_transient"
    assert recovery.classify_failure(aria_decision="rediscover") == "aria_signature_mismatch"
    assert recovery.recovery_plan(failure_class="network_transient", prior_attempts=1)["action"] == "retry"
    assert recovery.recovery_plan(failure_class="network_transient", prior_attempts=2)["action"] == "human_gateway"
    assert recovery.recovery_plan(failure_class="login_expired", prior_attempts=0)["action"] == "human_gateway"


def test_recovery_evidence_discards_credentials_cookies_otp_and_page_commands():
    cleaned = recovery.sanitize_recovery_evidence({
        "aria_signature_hash": "a" * 64,
        "cookie": "secret", "otp": "123456", "page_command": "click()",
        "node_count": 3,
    })
    assert cleaned == {"aria_signature_hash": "a" * 64, "node_count": 3}
    assert recovery._safe_selector("button[data-testid='save']")
    assert recovery._safe_selector("javascript:alert(1)") is None


def test_recovery_migration_keeps_recipe_canon_and_scope_index():
    sql = (Path(__file__).resolve().parents[2] / "migrations/20260919_m4_recipe_recovery.sql").read_text()
    assert "browser_recipe_recovery_events" in sql
    assert "recipe_id, recipe_version, site_key, page_key, skill_key, skill_version" in sql
    assert "ALTER TABLE browser_recipes" not in sql
    assert "DROP " not in sql.upper() and "TRUNCATE" not in sql.upper()


def test_recording_is_tenant_scoped_idempotent_and_never_auto_promotes(monkeypatch):
    calls = []

    class Connection:
        async def fetchrow(self, query, *args):
            calls.append((query, args))
            if "FROM browser_recipe_recovery_events" in query:
                return None
            if "browser_learned_artifacts" in query:
                return {"id": "00000000-0000-0000-0000-000000000010"}
            if "browser_learned_artifact_versions" in query:
                assert "'candidate'" in query
                return {"id": "00000000-0000-0000-0000-000000000011"}
            return {"failure_class": "selector_changed", "recovery_action": "rediscover", "retry_attempt": 0,
                    "retry_limit": 2, "candidate_artifact_version_id": None, "human_guidance": "safe"}

        async def fetchval(self, query, *args):
            calls.append((query, args))
            return 0

        def transaction(self):
            return Transaction()

    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return False

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: SimpleNamespace(acquire=lambda: Acquire()))
    asyncio.run(recovery.record_recipe_recovery(
        tenant_id="00000000-0000-0000-0000-000000000001", recipe_id="catalog", recipe_version="v1",
        site_key="shop", page_key="orders", idempotency_key="recovery-001", error_code="locator_not_found",
        rediscovered_selector="button[data-testid='save']",
    ))
    assert all("tenant_id=$1::uuid" in query or "INSERT INTO browser_recipe_recovery_events" in query or "browser_learned_artifacts" in query or "browser_learned_artifact_versions" in query for query, _ in calls)
    assert not any("'active'" in query for query, _ in calls)
