import asyncio
import pytest
import importlib.util
import sys
from pathlib import Path

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
