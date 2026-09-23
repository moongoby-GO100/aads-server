"""러너 실패 3분류 택소노미 테스트 (M1).

두 가지를 지킨다.

1. 2026-09-23 실측 error_detail / review_feedback 원문이 기대한 분류로 떨어지는가.
2. 파이썬 RULES 와 migrations/182 의 SQL CASE 가 같은 순서로 유지되는가.
   규칙을 한쪽만 고치면 뷰와 API 가 서로 다른 답을 내므로 여기서 막는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.pipeline_failure_taxonomy import (
    CLASS_GATE,
    CLASS_INFRA,
    CLASS_INSTRUCTION,
    CLASS_UNCLASSIFIED,
    FAILURE_CLASSES,
    RULES,
    classify,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "migrations" / "182_pipeline_failure_classification_v1.sql"

# (error_detail, review_feedback, 기대 class, 기대 subtype) — 전부 실측 원문 기반
OBSERVED_CASES = [
    # 지시서 결함
    ("review_failed: verdict=REQUEST_CHANGES score=0.4", None,
     CLASS_INSTRUCTION, "review_request_changes"),
    ("review_failed: verdict=REQUEST_CHANGES score=0.5 source=auto_sweeper", None,
     CLASS_INSTRUCTION, "review_request_changes"),
    ("review_failed: verdict=FLAG score=0.2 category=PRESERVATION_HARD_GATE", None,
     CLASS_INSTRUCTION, "review_request_changes"),
    ("no_changes: RESULT", None,
     CLASS_INSTRUCTION, "no_changes_instruction_conflict"),
    ("go100-frontend:build_failed", None,
     CLASS_INSTRUCTION, "build_failed"),
    ("invalid_aads_target", None,
     CLASS_INSTRUCTION, "invalid_target"),
    # 리뷰·CLI 인프라
    ("review_infra_failed: verdict=FLAG score=0.0 http=500 model=codex:gpt-5.6-terra attempts=3/3", None,
     CLASS_INFRA, "review_infra"),
    # REVIEW_MODEL_CONFIG_INVALID 는 review_failed 로 시작해도 설정 결함이므로 인프라다
    ("review_failed: verdict=FLAG score=0.0 category=REVIEW_MODEL_CONFIG_INVALID needs_retry=true", None,
     CLASS_INFRA, "review_config_invalid"),
    ("server_restart_orphan", None, CLASS_INFRA, "server_restart_orphan"),
    ("watchdog_stall_30min", None, CLASS_INFRA, "watchdog_stall"),
    ("timeout_max_runtime", None, CLASS_INFRA, "runtime_timeout"),
    ("llm_quota_exhausted: You've hit your weekly limit", None, CLASS_INFRA, "llm_quota"),
    ("rate_limit: openai.RateLimitError: Error code: 429", None, CLASS_INFRA, "rate_limit"),
    ("approval_commit_failed", None, CLASS_INFRA, "git_commit_failed"),
    ("git_fetch_failed", None, CLASS_INFRA, "git_fetch_failed"),
    ("deploy_timeout", None, CLASS_INFRA, "deploy_infra"),
    ("health_check_fail_rollback", None, CLASS_INFRA, "health_rollback"),
    ("push_fail(state=fast_forward/recheck=stale_base): event=push_failed", None,
     CLASS_INFRA, "push_failed"),
    ("review_origin_adjudication_expired", None, CLASS_INFRA, "adjudication_unresolved"),
    # 정상 게이트 차단
    ("blocked_dependency: parent runner-1a2b3c is error", None, CLASS_GATE, "dependency_block"),
    ("orphaned_dependency: parent runner-1a2b3c failed", None, CLASS_GATE, "dependency_block"),
    ("parent R10 rejected by independent review score 0.69", None,
     CLASS_GATE, "dependency_block"),
    ("dedup_blocked: existing job runner-1a2b3c is queued/queued", None, CLASS_GATE, "dedup_gate"),
    ("duplicate_next_milestone: existing runner-e7d655fe already running", None,
     CLASS_GATE, "dedup_gate"),
    ("deploy_preflight_git_state", None, CLASS_GATE, "deploy_preflight_gate"),
    ("push_stale_base: 승인 SHA 의 base 가 origin/main 보다 낡아", None,
     CLASS_GATE, "push_stale_base_gate"),
    ("manual_stop: M4 dashboard runner workspace mismatch", None, CLASS_GATE, "manual_stop"),
    ("manual_review_failed: AT-049 assessment linkage absent", None,
     CLASS_GATE, "manual_review_rejected"),
    ("no_changes: ## 작업 완료 — RESULT", None, CLASS_GATE, "no_changes_already_done"),
    # error_detail 이 비었을 때의 2순위 신호(실측 193건의 출처)
    (None, "[Runner Guard] 동일 파일 충돌 감지: server:AGENTS.md",
     CLASS_GATE, "runner_guard_conflict"),
    (None, " | 강제종료: AI 판단에 의한 강제 종료", CLASS_GATE, "manual_force_kill"),
    (None, "\n[Dependency repair] G2 original review_hold superseded",
     CLASS_GATE, "dependency_block"),
    (None, "\n[Codex] codex:gpt-5.6-terra 즉시폴백: rate-limit/quota 초과",
     CLASS_INFRA, "codex_auth_fallback"),
    # 미분류
    ("unknown", None, CLASS_UNCLASSIFIED, "unknown"),
    (None, None, CLASS_UNCLASSIFIED, "unknown"),
]


@pytest.mark.parametrize("detail,feedback,expected_class,expected_subtype", OBSERVED_CASES)
def test_classify_observed_samples(detail, feedback, expected_class, expected_subtype):
    failure_class, subtype, _source = classify(detail, feedback)
    assert (failure_class, subtype) == (expected_class, expected_subtype)


def test_error_detail_beats_review_feedback():
    """detail 이 있으면 feedback 규칙은 보지 않는다 — 신호 우선순위."""
    failure_class, subtype, source = classify(
        "review_failed: verdict=REQUEST_CHANGES score=0.4",
        "[Runner Guard] 동일 파일 충돌 감지: server:AGENTS.md",
    )
    assert failure_class == CLASS_INSTRUCTION
    assert subtype == "review_request_changes"
    assert source == "error_detail"


def test_signal_source_reported():
    assert classify(None, "[Runner Guard] x")[2] == "review_feedback"
    assert classify("server_restart_orphan", None)[2] == "error_detail"
    assert classify(None, None)[2] == "none"


def test_all_rules_use_valid_class():
    for rule in RULES:
        assert rule.failure_class in FAILURE_CLASSES
        assert rule.field in ("detail", "feedback")
        assert rule.match in ("prefix", "contains")
        assert rule.needle == rule.needle.lower()


def test_migration_case_matches_rules_order():
    """SQL CASE 의 (class|subtype) 태그 순서가 RULES 순서와 같아야 한다."""
    assert MIGRATION.exists(), f"마이그레이션 파일 없음: {MIGRATION}"
    sql = MIGRATION.read_text(encoding="utf-8")

    tags = re.findall(r"(?:THEN|ELSE) '([a-z_]+)\|([a-z_]+)'", sql)
    assert tags, "SQL 에서 분류 태그를 찾지 못했다"
    assert tags[-1] == ("unclassified", "unknown"), "SQL 의 마지막 분기는 ELSE 미분류여야 한다"

    expected = [(rule.failure_class, rule.subtype) for rule in RULES]
    assert tags[:-1] == expected, (
        "RULES 와 migrations/182 의 CASE 순서가 어긋났다 — 한쪽만 고치지 말 것"
    )


def test_classification_route_registered():
    """조회 API 가 라우터에 실제로 붙어 있는가.

    hot-reload 는 모듈만 다시 읽고 include_router 는 다시 돌리지 않는다.
    새 라우트는 프로세스 재기동(deploy.sh bluegreen)이 있어야 노출된다 —
    그 사실을 놓치지 않도록 등록 자체를 테스트로 고정한다.
    """
    from app.api.pipeline_runner import router

    paths = {getattr(route, "path", None) for route in router.routes}
    assert "/pipeline/failures/classification" in paths


def test_migration_contains_every_needle():
    sql = MIGRATION.read_text(encoding="utf-8")
    for rule in RULES:
        column = "s.d" if rule.field == "detail" else "s.f"
        assert f"strpos({column}, '{rule.needle}')" in sql, f"SQL 에 누락된 규칙: {rule.needle}"
        if rule.also_contains:
            assert f"strpos({column}, '{rule.also_contains}')" in sql
