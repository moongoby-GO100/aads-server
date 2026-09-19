import importlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest


@pytest.fixture()
def collector_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(tmp_path / "queue.json"))
    monkeypatch.setenv("AADS_AUTHENTICATED_SITE_PROFILES_PATH", str(tmp_path / "site_profiles.json"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.authenticated_site_collector as collector
    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    collector = importlib.reload(collector)
    return collector, queue_module


async def test_collector_overview_uses_project_defaults_without_database(collector_modules):
    collector, _queue_module = collector_modules

    overview = await collector.collector_overview(tenant_id="00000000-0000-0000-0000-000000000001")

    assert overview["demo"] is True
    assert overview["totals"]["connected_sites"] >= 9
    assert overview["runtime_contracts"]["windows_collector"]["financial_job_type"] == "financial_exclusive"
    assert overview["runtime_contracts"]["windows_collector"]["financial_max_concurrency_per_pc"] == 1
    assert overview["runtime_contracts"]["windows_collector"]["general_site_parallelism_per_pc"] == 1
    assert {item["project_key"] for item in overview["projects"]} >= {
        "AADS",
        "KIS",
        "GO100",
        "SF",
        "NTV2",
        "NAS",
        "STORE_ASSISTANT",
        "MARKETING",
        "BANKING",
    }


async def test_site_profile_upsert_is_available_without_database(collector_modules):
    collector, _queue_module = collector_modules

    profile = await collector.upsert_site_profile(
        tenant_id="00000000-0000-0000-0000-000000000001",
        user_id="ceo",
        payload={
            "project_key": "NTV2",
            "site_key": "newtalk.seller",
            "display_name": "NewTalk seller portal",
            "base_origin": "https://seller.newtalk.kr/login",
            "runtime": "webview2",
            "data_categories": ["settlement", "orders"],
        },
    )
    profiles = await collector.list_site_profiles(
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_key="NTV2",
    )

    assert profile["base_origin"] == "https://seller.newtalk.kr"
    assert profiles["demo"] is False
    assert profiles["sites"][0]["site_key"] == "newtalk.seller"


async def test_create_and_resume_collector_job_preserves_work_key(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "SF",
            "site_key": "sf.creator",
            "display_name": "ShortFlow creator",
            "base_origin": "https://studio.example/login",
            "runtime": "chrome_extension",
            "data_categories": ["uploads"],
        },
    )
    created = await collector.create_collection_job(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "SF",
            "site_key": "sf.creator",
            "recipe_id": "sf.creator.collect",
            "work_key": "sf creator local",
        },
    )
    job = created["job"]
    blocked = await collector.mark_collection_job_action_required(
        job_id=job["id"],
        challenge_kind="otp",
        page_url="https://studio.example/login",
        message="OTP entry required",
        evidence=["인증번호"],
        approval_scope={"origin": "https://studio.example", "otp": "do-not-store"},
    )
    resumed = await collector.resume_collection_job(
        job_id=job["id"],
        resolution="user_input_completed",
        note="OTP entered by user in the live browser",
        physical_input_completed=True,
    )

    assert created["status"] == "created"
    assert job["work_key"] == "sf-creator-local"
    assert blocked is not None
    assert blocked["status"] == "action_required"
    assert blocked["job"]["challenge"]["kind"] == "otp"
    assert blocked["job"]["challenge"]["auto_bypass_allowed"] is False
    assert blocked["job"]["challenge"]["challenge_values_persisted"] is False
    assert blocked["job"]["challenge"]["requires_user_physical_input"] is True
    assert blocked["job"]["challenge"]["user_approved_automation_allowed"] is False
    assert blocked["job"]["challenge"]["approval_scope"]["otp"] == "***MASKED***"
    assert resumed is not None
    assert resumed["same_work_key"] is True
    assert resumed["job"]["work_key"] == "sf-creator-local"
    assert resumed["job"]["status"] == "queued"
    assert resumed["job"]["challenge"]["resolved_by_user"] is True
    assert resumed["job"]["challenge"]["physical_input_completed"] is True
    assert resumed["job"]["challenge"]["auto_bypass_allowed"] is False
    assert (await collector.list_jobs(project_key="SF"))["count"] == 1


def test_collector_recipe_dry_run_maps_webview2_to_pc_agent(collector_modules):
    collector, _queue_module = collector_modules

    plan = collector.build_collector_recipe_dry_run(
        {
            "recipe_id": "ntv2.partner.collect",
            "version": "v1",
            "allowed_origins": ["https://newtalk.kr"],
            "work_key_template": "ntv2 partner",
        },
        target_url="https://newtalk.kr/partners",
        project_key="NTV2",
        site_environment="webview2",
    )

    assert plan["runtime"] == "pc_agent"
    assert plan["saas_extension"]["project_key"] == "NTV2"
    assert plan["saas_extension"]["site_environment"] == "webview2"
    assert plan["saas_extension"]["execution_runtime"] == "pc_agent"


async def test_food_project_keys_and_challenge_policy_object_are_preserved(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    profile = await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "STORE_ASSISTANT",
            "site_key": "baemin.owner",
            "display_name": "Baemin owner portal",
            "base_origin": "https://self.baemin.com/login",
            "allowed_origins": ["https://self.baemin.com"],
            "runtime": "webview2",
            "data_categories": ["orders"],
            "challenge_policy": {"mode": "user_intervention"},
        },
    )

    assert profile["project_key"] == "STORE_ASSISTANT"
    assert profile["challenge_policy"]["mode"] == "user_intervention"
    assert profile["challenge_policy"]["auto_bypass_allowed"] is False
    assert profile["challenge_policy"]["stores_challenge_values"] is False
    assert profile["metadata"]["runtime_contract"] == "windows_collector_v1"


def test_windows_collector_maps_to_pc_agent_execution_runtime(collector_modules):
    collector, _queue_module = collector_modules

    plan = collector.build_collector_recipe_dry_run(
        {
            "recipe_id": "shinhan.easyview.collect",
            "version": "v1",
            "allowed_origins": ["https://bank.shinhan.com"],
            "work_key_template": "banking shinhan mia",
        },
        target_url="https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
        project_key="BANKING",
        site_environment="windows_collector",
    )

    assert plan["runtime"] == "pc_agent"
    assert plan["saas_extension"]["project_key"] == "BANKING"
    assert plan["saas_extension"]["site_environment"] == "windows_collector"
    assert plan["saas_extension"]["execution_runtime"] == "pc_agent"
    assert plan["saas_extension"]["runtime_contract"]["job_type"] == "financial_exclusive"
    assert plan["saas_extension"]["lease_policy"]["exclusive"] is True
    assert plan["saas_extension"]["success_contract"]["minimum_imported_rows"] == 1
    assert plan["saas_extension"]["runtime_contract"]["entry_url_policy"]["same_work_key_required"] is True


async def test_banking_job_uses_financial_exclusive_runtime_contract(collector_modules):
    collector, queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "BANKING",
            "site_key": "shinhan.easyview",
            "display_name": "Shinhan easy inquiry",
            "base_origin": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
            "runtime": "windows_collector",
            "data_categories": ["transactions", "balances"],
        },
    )

    created = await collector.create_collection_job(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "BANKING",
            "site_key": "shinhan.easyview",
            "recipe_id": "shinhan.easyview.collect",
            "work_key": "yeoljeong-bank-shinhan-mia",
        },
    )
    queued = queue_module.queue_snapshot(limit=1)[0]

    assert created["job"]["runtime_contract"]["job_type"] == "financial_exclusive"
    assert created["job"]["lease_policy"]["scope"] == "pc_agent_interactive_browser_lane"
    assert created["job"]["success_contract"]["minimum_imported_rows"] == 1
    assert created["job"]["runtime_contract"]["entry_url_policy"]["entry_url"] == (
        "https://bank.shinhan.com/rib/easy/index.jsp#210000000000"
    )
    assert created["job"]["runtime_contract"]["entry_url_policy"]["forbidden_login_origins"] == [
        "https://bizbank.shinhan.com"
    ]
    assert queued["resource_key"].startswith(f"financial_exclusive|{tenant_id}|")


async def test_shinhan_easyview_profile_rejects_legacy_bizbank_origin(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    profile = await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "BANKING",
            "site_key": "shinhan.easyview",
            "display_name": "Shinhan easy inquiry",
            "base_origin": "https://bizbank.shinhan.com",
            "allowed_origins": ["https://bizbank.shinhan.com", "https://bank.shinhan.com"],
            "runtime": "windows_collector",
            "data_categories": ["transactions", "balances"],
        },
    )

    assert profile["base_origin"] == "https://bank.shinhan.com"
    assert profile["allowed_origins"] == ["https://bank.shinhan.com"]
    assert profile["metadata"]["entry_url"] == "https://bank.shinhan.com/rib/easy/index.jsp#210000000000"
    assert profile["metadata"]["login_url"] == "https://bank.shinhan.com/rib/easy/index.jsp#210000000000"
    assert profile["metadata"]["forbidden_login_origins"] == ["https://bizbank.shinhan.com"]


async def test_challenge_deny_policy_blocks_resume_automation(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "KIS",
            "site_key": "kis.secure",
            "display_name": "KIS secure",
            "base_origin": "https://securities.example/login",
            "runtime": "manual_export",
            "data_categories": ["statements"],
            "challenge_policy": {"mode": "deny"},
        },
    )
    created = await collector.create_collection_job(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "KIS",
            "site_key": "kis.secure",
            "recipe_id": "kis.secure.collect",
        },
    )
    result = await collector.mark_collection_job_action_required(
        job_id=created["job"]["id"],
        challenge_kind="captcha",
        page_url="https://securities.example/login",
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["job"]["error_code"] == "COLLECTOR_CHALLENGE_BLOCKED_BY_POLICY"
    assert result["job"]["challenge"]["auto_bypass_allowed"] is False


async def test_running_challenge_completion_forwards_claim_fence(collector_modules, monkeypatch):
    collector, queue_module = collector_modules
    completed: dict[str, object] = {}

    async def find_job(_job_id):
        return {
            "id": "job-fenced",
            "status": "running",
            "work_key": "same-work",
            "attempt_count": 4,
            "owner_instance": "green-8102",
            "owner_epoch": 12,
            "payload": {"challenge_policy": {"mode": "user_intervention"}},
        }

    async def complete(item_id, **kwargs):
        completed.update({"item_id": item_id, **kwargs})
        return {"id": item_id, "status": kwargs["status"], "result": kwargs["result"]}

    monkeypatch.setattr(collector, "_find_job", find_job)
    monkeypatch.setattr(queue_module, "complete_collection_item_async", complete)

    result = await collector.mark_collection_job_action_required(
        job_id="job-fenced", challenge_kind="captcha"
    )

    assert result is not None
    assert completed["owner_instance"] == "green-8102"
    assert completed["owner_epoch"] == 12
    assert completed["status"] == "action_required"


async def test_user_approved_automation_requires_responsibility_acceptance(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "MARKETING",
            "site_key": "meta.business",
            "display_name": "Meta Business",
            "base_origin": "https://business.facebook.com",
            "runtime": "webview2",
            "data_categories": ["ads"],
            "challenge_policy": {"mode": "user_intervention", "user_approved_automation_allowed": True},
        },
    )
    created = await collector.create_collection_job(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "MARKETING",
            "site_key": "meta.business",
            "recipe_id": "meta.business.collect",
        },
    )
    blocked = await collector.mark_collection_job_action_required(
        job_id=created["job"]["id"],
        challenge_kind="captcha",
        page_url="https://business.facebook.com/login",
    )

    with pytest.raises(ValueError, match="collector_responsibility_acceptance_required"):
        await collector.resume_collection_job(
            job_id=created["job"]["id"],
            resolution="user_approved_automation",
        )

    resumed = await collector.resume_collection_job(
        job_id=created["job"]["id"],
        resolution="user_approved_automation",
        note="User approved responsible same-session automation",
        responsibility_accepted=True,
    )

    assert blocked is not None
    assert blocked["job"]["challenge"]["auto_bypass_allowed"] is False
    assert blocked["job"]["challenge"]["user_approved_automation_allowed"] is True
    assert resumed is not None
    assert resumed["job"]["status"] == "queued"
    assert resumed["job"]["work_key"] == created["job"]["work_key"]
    assert resumed["job"]["challenge"]["approved_automation_requested"] is True
    assert resumed["job"]["challenge"]["responsibility_accepted"] is True
    assert resumed["job"]["challenge"]["resolved_by_user"] is False


async def test_otp_challenge_rejects_user_approved_automation_and_requires_physical_input(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "BANKING",
            "site_key": "shinhan.easyview",
            "display_name": "Shinhan easy inquiry",
            "base_origin": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
            "runtime": "windows_collector",
            "data_categories": ["transactions"],
            "challenge_policy": {"mode": "user_intervention", "user_approved_automation_allowed": True},
        },
    )
    created = await collector.create_collection_job(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "BANKING",
            "site_key": "shinhan.easyview",
            "recipe_id": "shinhan.easyview.collect",
        },
    )
    blocked = await collector.mark_collection_job_action_required(
        job_id=created["job"]["id"],
        challenge_kind="otp",
        page_url="https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
    )

    with pytest.raises(ValueError, match="collector_user_approved_automation_not_allowed_for_challenge"):
        await collector.resume_collection_job(
            job_id=created["job"]["id"],
            resolution="user_approved_automation",
            responsibility_accepted=True,
        )

    with pytest.raises(ValueError, match="collector_physical_input_completion_required"):
        await collector.resume_collection_job(
            job_id=created["job"]["id"],
            resolution="user_input_completed",
        )

    resumed = await collector.resume_collection_job(
        job_id=created["job"]["id"],
        resolution="user_input_completed",
        note="OTP entered directly by user",
        physical_input_completed=True,
    )

    assert blocked is not None
    assert blocked["job"]["challenge"]["requires_user_physical_input"] is True
    assert blocked["job"]["challenge"]["user_approved_automation_allowed"] is False
    assert resumed is not None
    assert resumed["job"]["status"] == "queued"
    assert resumed["job"]["work_key"] == created["job"]["work_key"]


def test_collector_api_overview_uses_tenant_context(collector_modules):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.authenticated_site_collector as api_module

    api_module = importlib.reload(api_module)
    app = FastAPI()
    app.include_router(api_module.router)
    app.dependency_overrides[api_module.require_viewer] = lambda: {
        "tenant": {"id": "00000000-0000-0000-0000-000000000001"},
        "membership": {"user_id": "ceo"},
    }

    response = TestClient(app).get("/authenticated-site-collector/overview")

    assert response.status_code == 200
    assert response.json()["totals"]["connected_sites"] >= 6
    assert response.json()["challenge_contract"]["auto_bypass_allowed"] is False


def test_collector_api_challenge_then_resume(collector_modules):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.authenticated_site_collector as api_module

    api_module = importlib.reload(api_module)
    app = FastAPI()
    app.include_router(api_module.router)
    app.dependency_overrides[api_module.require_viewer] = lambda: {
        "tenant": {"id": "00000000-0000-0000-0000-000000000001"},
        "membership": {"user_id": "ceo"},
    }
    app.dependency_overrides[api_module.require_member] = lambda: {
        "tenant": {"id": "00000000-0000-0000-0000-000000000001"},
        "membership": {"user_id": "ceo"},
    }
    client = TestClient(app)

    saved = client.post(
        "/authenticated-site-collector/site-profiles",
        json={
            "project_key": "MARKETING",
            "site_key": "meta.business",
            "display_name": "Meta Business",
            "base_origin": "https://business.facebook.com",
            "runtime": "webview2",
            "data_categories": ["ads"],
        },
    )
    created = client.post(
        "/authenticated-site-collector/jobs",
        json={"project_key": "MARKETING", "site_key": "meta.business", "recipe_id": "meta.business.collect"},
    )
    job_id = created.json()["job"]["id"]
    challenge = client.post(
        f"/authenticated-site-collector/jobs/{job_id}/challenge-action-required",
        json={
            "challenge_kind": "captcha",
            "page_url": "https://business.facebook.com/login",
            "message": "CAPTCHA required",
            "approval_scope": {"origin": "https://business.facebook.com", "captcha_value": "do-not-store"},
        },
    )
    resumed = client.post(
        f"/authenticated-site-collector/jobs/{job_id}/resume",
        json={"resolution": "approved_same_session", "note": "User completed CAPTCHA in browser"},
    )

    assert saved.status_code == 200
    assert created.status_code == 200
    assert challenge.status_code == 200
    assert challenge.json()["job"]["status"] == "action_required"
    assert challenge.json()["job"]["challenge"]["approval_scope"]["captcha_value"] == "***MASKED***"
    assert resumed.status_code == 200
    assert resumed.json()["job"]["status"] == "queued"


def test_account_first_login_api_is_tenant_scoped_and_idempotent(collector_modules, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.authenticated_site_collector as api_module

    api_module = importlib.reload(api_module)
    calls = []

    async def first_login(**kwargs):
        calls.append(kwargs)
        return {
            "status": "approval_requested",
            "idempotent": len(calls) > 1,
            "account": {"vault_reference": kwargs["vault_reference"], "approval": {"id": "approval-a"}},
        }

    monkeypatch.setattr(api_module, "request_first_login", first_login)
    app = FastAPI()
    app.include_router(api_module.router)
    app.dependency_overrides[api_module.require_member] = lambda: {
        "tenant": {"id": "00000000-0000-0000-0000-000000000001"},
        "membership": {"user_id": "ceo"},
    }
    client = TestClient(app)
    body = {
        "site_profile_id": "00000000-0000-0000-0000-000000000010",
        "account_label": "primary",
        "vault_reference": "00000000-0000-0000-0000-000000000099",
    }

    first = client.post("/authenticated-site-collector/accounts/first-login", json=body)
    second = client.post("/authenticated-site-collector/accounts/first-login", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["account"]["approval"]["id"] == second.json()["account"]["approval"]["id"]
    assert second.json()["idempotent"] is True
    assert {call["tenant_id"] for call in calls} == {"00000000-0000-0000-0000-000000000001"}


def test_account_approval_migration_is_additive_and_keeps_only_vault_reference():
    sql = (
        Path(__file__).parents[2]
        / "migrations"
        / "20260919_authenticated_site_account_approval_scope.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS credential_approval_request_id" in sql
    assert "ADD COLUMN IF NOT EXISTS credential_scope" in sql
    assert "Opaque Agent Vault credential id only" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _CollectorPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


def _account_login_row(*, tenant_id: str, profile_id: str, login_status: str, approval_decision: str):
    return {
        "id": UUID("00000000-0000-0000-0000-000000000020"),
        "tenant_id": UUID(tenant_id),
        "site_profile_id": UUID(profile_id),
        "site_key": "meta.business",
        "base_origin": "https://business.facebook.com",
        "account_label": "primary",
        "login_status": login_status,
        "approval_decision": approval_decision,
        "approval_expires_at": datetime.now(UTC),
        "vault_reference": "00000000-0000-0000-0000-000000000099",
        "credential_scope": {},
    }


class _FirstLoginVaultConn:
    def __init__(self, *, tenant_id: str, profile_id: str, vault_id: UUID, vault_row):
        self.tenant_id = tenant_id
        self.profile_id = profile_id
        self.vault_id = vault_id
        self.vault_row = vault_row
        self.calls = []

    def transaction(self):
        return _AsyncContext()

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "FROM authenticated_site_profiles" in query:
            return {
                "id": self.profile_id,
                "site_key": "meta.business",
                "base_origin": "https://business.facebook.com",
            }
        if "FROM agent_vault_credentials" in query:
            assert args == (self.vault_id, UUID(self.tenant_id))
            assert "tenant_id=$2" in query and "is_active=TRUE" in query
            return self.vault_row
        raise AssertionError(f"unexpected SQL after Vault lookup: {query}")


async def test_first_login_rejects_client_work_key_outside_vault_scope(collector_modules, monkeypatch):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"
    profile_id = "00000000-0000-0000-0000-000000000010"
    vault_id = UUID("00000000-0000-0000-0000-000000000099")

    conn = _FirstLoginVaultConn(
        tenant_id=tenant_id,
        profile_id=profile_id,
        vault_id=vault_id,
        vault_row={
            "id": vault_id,
            "work_key": "approved-vault-work",
            "origin": "https://business.facebook.com",
        },
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://enabled-for-test")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _CollectorPool(conn))

    with pytest.raises(ValueError, match="vault_reference_work_key_mismatch"):
        await collector.request_first_login(
            tenant_id=tenant_id,
            user_id="ceo",
            site_profile_id=profile_id,
            account_label="primary",
            vault_reference=str(vault_id),
            work_key="client-selected-work",
        )

    assert len(conn.calls) == 2


async def test_first_login_inactive_vault_uses_not_found_contract(collector_modules, monkeypatch):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"
    profile_id = "00000000-0000-0000-0000-000000000010"
    vault_id = UUID("00000000-0000-0000-0000-000000000099")

    conn = _FirstLoginVaultConn(
        tenant_id=tenant_id,
        profile_id=profile_id,
        vault_id=vault_id,
        vault_row=None,
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://enabled-for-test")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _CollectorPool(conn))

    with pytest.raises(ValueError, match="vault_reference_not_found"):
        await collector.request_first_login(
            tenant_id=tenant_id,
            user_id="ceo",
            site_profile_id=profile_id,
            account_label="primary",
            vault_reference=str(vault_id),
        )

    assert len(conn.calls) == 2


async def test_recovery_refuses_connected_account_before_new_approval(collector_modules, monkeypatch):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"
    profile_id = "00000000-0000-0000-0000-000000000010"

    class Conn:
        def __init__(self):
            self.fetches = 0

        def transaction(self):
            return _AsyncContext()

        async def fetchrow(self, query, *args):
            self.fetches += 1
            assert "FOR UPDATE OF a" in query
            assert args == (UUID(tenant_id), profile_id, "primary")
            return _account_login_row(
                tenant_id=tenant_id,
                profile_id=profile_id,
                login_status="connected",
                approval_decision="rejected",
            )

    conn = Conn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://enabled-for-test")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _CollectorPool(conn))

    with pytest.raises(ValueError, match="account_recovery_not_required"):
        await collector.recover_account_login(
            tenant_id=tenant_id,
            user_id="ceo",
            site_profile_id=profile_id,
            account_label="primary",
        )

    assert conn.fetches == 1


async def test_recovery_refuses_approved_account_before_vault_or_approval_write(collector_modules, monkeypatch):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"
    profile_id = "00000000-0000-0000-0000-000000000010"

    class Conn:
        def __init__(self):
            self.fetches = 0

        def transaction(self):
            return _AsyncContext()

        async def fetchrow(self, query, *args):
            self.fetches += 1
            assert "FOR UPDATE OF a" in query
            return _account_login_row(
                tenant_id=tenant_id,
                profile_id=profile_id,
                login_status="expired",
                approval_decision="approved",
            )

    conn = Conn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://enabled-for-test")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _CollectorPool(conn))

    with pytest.raises(ValueError, match="account_recovery_not_required"):
        await collector.recover_account_login(
            tenant_id=tenant_id,
            user_id="ceo",
            site_profile_id=profile_id,
            account_label="primary",
        )

    assert conn.fetches == 1


def test_account_approval_migration_enforces_same_tenant_foreign_key():
    sql = (
        Path(__file__).parents[2]
        / "migrations"
        / "20260919_authenticated_site_account_approval_scope.sql"
    ).read_text(encoding="utf-8")

    assert "UNIQUE INDEX IF NOT EXISTS uq_agent_permission_requests_tenant_id_id" in sql
    assert "FOREIGN KEY (tenant_id, credential_approval_request_id)" in sql
    assert "REFERENCES agent_permission_requests(tenant_id, id)" in sql
    assert "ON DELETE SET NULL (credential_approval_request_id)" in sql
    assert "REFERENCES agent_permission_requests(id)" not in sql
    assert "DROP CONSTRAINT %I" in sql
    assert "c.conkey = ARRAY" in sql
