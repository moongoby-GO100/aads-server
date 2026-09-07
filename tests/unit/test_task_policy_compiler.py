"""task_policy_compiler 단위 테스트.

핵심 목적:
1. 컴파일된 정책이 러너/코드리뷰어의 기존 게이트와 같은 문구를 쓰는지 (드리프트 차단)
2. risk_tier 추론과 승인 필요 여부가 ohvis_harness.RISK_POLICIES와 일치하는지
3. 삭제가 기본 금지로 유지되는지
"""
from pathlib import Path

from app.services.ohvis_harness import RISK_POLICIES
from app.services.task_policy_compiler import (
    NO_BROAD_REPLACEMENT_RULES,
    PRESERVATION_CLASSES,
    PRESERVATION_SURVEY_STEPS,
    TARGET_VALIDATION_STEPS,
    compile_task_policy,
    infer_risk_tier,
    normalize_risk_tier,
    render_policy_markdown,
)

ROOT = Path(__file__).resolve().parents[2]


def test_survey_and_validation_wording_matches_runner_checklist() -> None:
    runner_source = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(
        encoding="utf-8"
    )

    for step in PRESERVATION_SURVEY_STEPS:
        assert step in runner_source, f"러너 STEP 0 문구와 불일치: {step}"
    for step in TARGET_VALIDATION_STEPS:
        assert step in runner_source, f"러너 검증 체크리스트 문구와 불일치: {step}"


def test_no_broad_replacement_rules_match_code_reviewer_gate() -> None:
    reviewer_source = (ROOT / "app" / "services" / "code_reviewer.py").read_text(encoding="utf-8")

    assert "기존 함수/클래스/API 라우터 삭제가 있으면 preservation 0.2 이하, FLAG로 판정" in reviewer_source
    assert "삭제 라인이 추가 라인의 50%를 초과하면 preservation 0.3 이하, REQUEST_CHANGES 이상으로 차단" in reviewer_source
    assert any("50%" in rule for rule in NO_BROAD_REPLACEMENT_RULES)
    assert any("FLAG" in rule for rule in NO_BROAD_REPLACEMENT_RULES)


def test_compile_task_policy_returns_full_preservation_contract() -> None:
    policy = compile_task_policy(
        project="aads",
        intent="ops",
        goal_title="채팅 시스템 안정화 및 응답 가독성 개선",
    )

    assert policy["project"] == "AADS"
    assert policy["existing_implementation_survey"] == list(PRESERVATION_SURVEY_STEPS)
    assert policy["preservation_classification"] == list(PRESERVATION_CLASSES)
    assert policy["no_broad_replacement"]
    assert policy["target_validation"] == list(TARGET_VALIDATION_STEPS)
    assert policy["evidence_requirements"]
    assert policy["rollback_notes"]
    assert policy["deletion_allowed"] is False
    assert policy["checklist"], "체크리스트는 비어 있으면 안 된다"
    assert all({"id", "section", "requirement"} <= set(item) for item in policy["checklist"])
    assert "ohvis_harness_traces" in " ".join(policy["evidence_requirements"])


def test_risk_tier_explicit_beats_inference_and_matches_risk_policies() -> None:
    explicit = compile_task_policy(project="AADS", intent="ops", risk_tier="deploy")
    assert explicit["risk_tier"] == "deploy"
    assert explicit["risk_tier_source"] == "explicit"
    assert explicit["risk_policy"] == RISK_POLICIES["deploy"]
    assert explicit["approval_required"] is True

    unknown = compile_task_policy(project="AADS", intent="ops", risk_tier="not-a-tier")
    assert unknown["risk_tier_source"] != "explicit"
    assert normalize_risk_tier("not-a-tier") is None
    assert normalize_risk_tier("Deploy") == "deploy"


def test_risk_tier_inference_escalates_from_intent_and_title() -> None:
    assert infer_risk_tier("task_query", None)[0] == "read"
    assert infer_risk_tier("ops", None) == ("write", "intent")
    assert infer_risk_tier("ops", "AADS 블루그린 배포 후 모니터링")[0] == "deploy"
    assert infer_risk_tier("browser_collection", "배민 로그인 수집")[0] == "auth"
    assert infer_risk_tier("ops", "GO100 주문 게이트 점검")[0] == "financial"

    destructive = compile_task_policy(project="AADS", goal_title="레거시 테이블 truncate")
    assert destructive["risk_tier"] == "destructive"
    assert destructive["decision"] == "reject"
    assert "CEO 승인 원문" in destructive["evidence_requirements"]


def test_risk_specific_evidence_and_rollback_are_appended() -> None:
    deploy = compile_task_policy(project="AADS", intent="deploy")
    rollback_text = " ".join(deploy["rollback_notes"])

    assert any("digest" in item for item in deploy["evidence_requirements"])
    assert any("5분 P0/P1" in item for item in deploy["evidence_requirements"])
    assert "nginx" in rollback_text


def test_render_policy_markdown_contains_every_section() -> None:
    markdown = render_policy_markdown(compile_task_policy(project="AADS", intent="ops"))

    assert "## 기존 구현 보존 정책" in markdown
    assert "### STEP 0 기존 구현 조사 (코드 수정 전 필수)" in markdown
    assert "### 통째 대체 금지 하드 게이트" in markdown
    assert "### 롤백 노트" in markdown
    for step in PRESERVATION_SURVEY_STEPS:
        assert f"- [ ] {step}" in markdown
    assert "삭제는 기본 금지다" in markdown
