import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/ohvis_harness.py"
SPEC = importlib.util.spec_from_file_location("ohvis_harness_under_test", MODULE_PATH)
ohvis_harness = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = ohvis_harness
SPEC.loader.exec_module(ohvis_harness)

RISK_POLICIES = ohvis_harness.RISK_POLICIES
find_skills = ohvis_harness.find_skills
recommend_hermes_improvements = ohvis_harness.recommend_hermes_improvements
scan_repository_skills = ohvis_harness.scan_repository_skills
validate_skill_manifest = ohvis_harness.validate_skill_manifest


def test_skill_find_matches_store_assistant_collector():
    result = asyncio.run(
        find_skills(
            "매장비서 배민 로그인 사이트 수집 OTP 재개",
            project="AADS",
            intent="browser_collection",
            limit=5,
        )
    )

    slugs = [item["slug"] for item in result["skills"]]
    assert "authenticated-site-collector" in slugs
    assert result["skills"][0]["policy"]["approval_required"] is True
    assert result["skills"][0]["risk_tier"] in {"auth", "write"}


def test_hermes_recommendation_keeps_external_runtime_guarded():
    result = asyncio.run(
        recommend_hermes_improvements(
            "GO100 장초반 진입 0건 원인 분석",
            project="GO100",
            recent_failure="same report was previously unverifiable",
        )
    )

    assert result["recommended_skills"]
    assert "external autonomous runtime" in result["guardrail"]
    assert result["closed_loop_actions"][-1]["phase"] == "self_improve"


def test_repository_skill_scan_exposes_local_skill_files():
    skills = scan_repository_skills()
    slugs = {item["slug"] for item in skills}

    assert "sales-channel-collector" in slugs
    assert all("read SKILL.md before action" in item["validation"] for item in skills)


def test_risk_policy_blocks_destructive_actions():
    assert RISK_POLICIES["destructive"]["decision"] == "reject"
    assert RISK_POLICIES["deploy"]["approval_required"] is True


def test_destructive_skill_cannot_be_promoted_or_executed_even_with_approval():
    with pytest.raises(ohvis_harness.SkillRegistryError) as exc:
        ohvis_harness._enforce_executable_risk_policy({"risk_tier": "destructive"})
    assert exc.value.code == "skill_policy_rejected"
    assert exc.value.status_code == 403

    # High-risk tiers that are explicitly approvable remain eligible for the
    # Human Gateway scope check performed by execute_skill.
    ohvis_harness._enforce_executable_risk_policy({"risk_tier": "deploy"})


def _manifest(**changes):
    value = {
        "skill_id": "11111111-1111-1111-1111-111111111111",
        "version": "1.0.0",
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        "executor": "site.search-count",
        "allowed_tools": ["site.search"],
        "timeout_seconds": 10,
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
        "idempotency": {"mode": "required"},
        "preconditions": ["authenticated"],
        "postconditions": ["result-count-recorded"],
        "evidence": ["result_hash"],
        "risk_tier": "read",
        "status": "candidate",
        "provenance": {"source": "site-skill", "project": "AADS"},
    }
    value.update(changes)
    return value


def test_executable_manifest_contract_accepts_complete_json_schemas():
    manifest = validate_skill_manifest(_manifest())
    assert manifest["executor"] == "site.search-count"
    assert manifest["idempotency"]["mode"] == "required"


def test_retry_is_rejected_without_required_idempotency():
    with pytest.raises(ohvis_harness.SkillRegistryError) as exc:
        validate_skill_manifest(_manifest(
            retry={"max_attempts": 2, "backoff_seconds": 0},
            idempotency={"mode": "optional"},
        ))
    assert exc.value.code == "retry_requires_idempotency"


def test_schema_validation_and_high_risk_approval_are_fail_closed():
    manifest = validate_skill_manifest(_manifest())
    with pytest.raises(ohvis_harness.SkillRegistryError) as exc:
        ohvis_harness._validate_instance(manifest["input_schema"], {"query": 3}, "bad_input")
    assert exc.value.code == "bad_input"

    provenance = {"issuer": "server_authenticated_request", "tenant_id": "t1"}
    approval = {
        "tenant_id": "t1", "decision": "approved", "max_executions": 1,
        "approval_scope": {
            "skill_id": manifest["skill_id"], "version": manifest["version"],
            "input_hash": "sha256:input", "channel_router_provenance": provenance,
        },
    }
    assert ohvis_harness._approval_matches(
        approval, tenant_id="t1", skill_id=manifest["skill_id"],
        version=manifest["version"], input_hash="sha256:input", channel_provenance=provenance,
    )
    assert not ohvis_harness._approval_matches(
        approval, tenant_id="other", skill_id=manifest["skill_id"],
        version=manifest["version"], input_hash="sha256:input", channel_provenance=provenance,
    )


def test_executor_registry_rejects_replacement_and_has_no_dynamic_loading():
    def first(payload):
        return {"count": 1}

    def second(payload):
        return {"count": 2}

    ohvis_harness.register_skill_executor("test.search-count", first)
    try:
        with pytest.raises(ValueError):
            ohvis_harness.register_skill_executor("test.search-count", second)
        source = MODULE_PATH.read_text(encoding="utf-8")
        assert "eval(" not in source
        assert "exec(" not in source
        assert "import_module(" not in source
    finally:
        ohvis_harness.unregister_skill_executor("test.search-count", executor=first)


def test_code_owned_contract_executor_is_actually_callable():
    executor = ohvis_harness._SKILL_EXECUTORS["ohvis.contract-echo"]
    assert executor({"query": "safe"}) == {"input": {"query": "safe"}}


def test_skill_api_exposes_crud_validate_promote_and_execute_contracts():
    source = (MODULE_PATH.parents[1] / "api" / "ohvis_harness.py").read_text(encoding="utf-8")
    for route in (
        '"/ohvis/harness/skills"',
        '"/ohvis/harness/skills/{skill_id}"',
        '"/ohvis/harness/skills/{skill_id}/versions"',
        '"/ohvis/harness/skills/{skill_id}/versions/{version}/validate"',
        '"/ohvis/harness/skills/{skill_id}/versions/{version}/promote"',
        '"/ohvis/harness/skills/{skill_id}/execute"',
    ):
        assert route in source
    assert "require_tenant_role" in source
    assert "directive_from_authenticated_context" in source


def test_executable_registry_migration_extends_canonical_tables():
    sql = (
        MODULE_PATH.parents[1].parent
        / "migrations"
        / "20260919_executable_skill_registry.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER TABLE ops_skill_library" in sql
    assert "ALTER TABLE ops_skill_versions" in sql
    assert "ALTER TABLE ops_skill_runs" in sql
    assert "tenant_id" in sql
    assert "idempotency_key" in sql
    assert "channel_provenance" in sql
    assert "approval_id" in sql
    assert "cost_usd" in sql and "latency_ms" in sql
    assert "trg_ops_skill_version_immutable" in sql
    assert "CREATE TABLE ops_skill" not in sql
