"""러너 실패 3분류 택소노미 (AADS M1).

error / cancelled 로 끝난 pipeline_jobs 를 세 갈래로 나눈다.

- ``instruction_defect`` : 지시서 결함 (리뷰 REQUEST_CHANGES, 지시 충돌, 빌드 실패)
- ``infra``              : 리뷰·CLI·배포 인프라 장애 (쿼터, 재시작 고아, 워치독, push/fetch 실패)
- ``gate_block``         : 정상 게이트 차단 (의존/중복/파일충돌/수동중단/preflight)

분류 없이 개선하면 인프라 장애를 지시서 품질 문제로 오진한다.

신호 우선순위는 ``error_detail`` → ``review_feedback`` 이다.
2026-09-23 실측에서 최근 7일 error 579건 중 193건(33%)은 error_detail 이 NULL 이었고,
그 193건 전부가 review_feedback 에 ``[Runner Guard] 동일 파일 충돌 감지``(91건) 또는
``강제종료``(102건) 를 갖고 있었다. 이 폴백이 없으면 미분류가 33% 가 되어
M1 완료기준(미분류 5% 이하)을 맞출 수 없다.

RULES 의 **순서가 곧 우선순위**다. 같은 순서의 CASE 가
``migrations/182_pipeline_failure_classification_v1.sql`` 에 들어 있고,
``tests/unit/test_pipeline_failure_taxonomy.py`` 가 둘의 동기화를 검사한다.
한쪽만 고치면 테스트가 막는다.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

CLASS_INSTRUCTION = "instruction_defect"
CLASS_INFRA = "infra"
CLASS_GATE = "gate_block"
CLASS_UNCLASSIFIED = "unclassified"

FAILURE_CLASSES = (CLASS_INSTRUCTION, CLASS_INFRA, CLASS_GATE, CLASS_UNCLASSIFIED)

VIEW_NAME = "pipeline_failure_classification_v1"
FAILED_STATUSES = ("error", "cancelled")


class Rule(NamedTuple):
    """분류 규칙 한 줄.

    field         : "detail"(error_detail) | "feedback"(review_feedback, detail 이 빈 경우만)
    match         : "prefix" | "contains"
    needle        : 소문자 비교 대상 문자열 (SQL 의 strpos 인자와 동일해야 한다)
    subtype       : 세부 원인 키
    failure_class : 3분류 중 하나
    also_contains : 추가 조건 (같은 needle 이 두 갈래로 갈릴 때만 사용)
    """

    field: str
    match: str
    needle: str
    subtype: str
    failure_class: str
    also_contains: Optional[str] = None


RULES: tuple[Rule, ...] = (
    # --- 리뷰·CLI 인프라 --------------------------------------------------
    Rule("detail", "prefix", "review_infra_failed", "review_infra", CLASS_INFRA),
    Rule("detail", "contains", "review_model_config_invalid", "review_config_invalid", CLASS_INFRA),
    Rule("detail", "prefix", "review_origin_adjudication", "adjudication_unresolved", CLASS_INFRA),
    Rule("detail", "prefix", "llm_quota_exhausted", "llm_quota", CLASS_INFRA),
    Rule("detail", "prefix", "rate_limit", "rate_limit", CLASS_INFRA),
    Rule("detail", "prefix", "server_restart_orphan", "server_restart_orphan", CLASS_INFRA),
    Rule("detail", "prefix", "watchdog_stall", "watchdog_stall", CLASS_INFRA),
    Rule("detail", "prefix", "timeout_max_runtime", "runtime_timeout", CLASS_INFRA),
    Rule("detail", "prefix", "approval_commit_failed", "git_commit_failed", CLASS_INFRA),
    Rule("detail", "prefix", "git_fetch_failed", "git_fetch_failed", CLASS_INFRA),
    # --- 배포·push: preflight/stale base 는 게이트, 그 밖의 실패는 인프라 ---
    Rule("detail", "prefix", "deploy_preflight", "deploy_preflight_gate", CLASS_GATE),
    Rule("detail", "prefix", "push_stale_base", "push_stale_base_gate", CLASS_GATE),
    Rule("detail", "prefix", "push_fail", "push_failed", CLASS_INFRA),
    Rule("detail", "prefix", "deploy_", "deploy_infra", CLASS_INFRA),
    Rule("detail", "prefix", "health_check_fail_rollback", "health_rollback", CLASS_INFRA),
    Rule("detail", "contains", "post_cutover evidence write failure", "deploy_infra", CLASS_INFRA),
    # --- 지시서 결함 -------------------------------------------------------
    Rule("detail", "prefix", "review_failed", "review_request_changes", CLASS_INSTRUCTION),
    Rule("detail", "contains", ":build_failed", "build_failed", CLASS_INSTRUCTION),
    Rule("detail", "prefix", "invalid_aads_target", "invalid_target", CLASS_INSTRUCTION),
    # --- 의존·중복·수동 게이트 --------------------------------------------
    Rule("detail", "prefix", "blocked_dependency", "dependency_block", CLASS_GATE),
    Rule("detail", "prefix", "orphaned_dependency", "dependency_block", CLASS_GATE),
    Rule("detail", "prefix", "parent ", "dependency_block", CLASS_GATE),
    Rule("detail", "prefix", "dedup_blocked", "dedup_gate", CLASS_GATE),
    Rule("detail", "prefix", "duplicate", "dedup_gate", CLASS_GATE),
    Rule("detail", "prefix", "manual_stop", "manual_stop", CLASS_GATE),
    Rule("detail", "prefix", "superseded", "superseded", CLASS_GATE),
    Rule("detail", "contains", "cancelled before execution", "superseded", CLASS_GATE),
    Rule("detail", "contains", "manual review rejected", "manual_review_rejected", CLASS_GATE),
    Rule("detail", "prefix", "manual_review_failed", "manual_review_rejected", CLASS_GATE),
    # --- no_changes 는 두 갈래: "완료" 보고면 게이트, 아니면 지시 충돌 ------
    Rule("detail", "prefix", "no_changes", "no_changes_already_done", CLASS_GATE, also_contains="완료"),
    Rule("detail", "prefix", "no_changes", "no_changes_instruction_conflict", CLASS_INSTRUCTION),
    # --- error_detail 이 빈 경우의 2순위 신호 ------------------------------
    Rule("feedback", "contains", "[runner guard]", "runner_guard_conflict", CLASS_GATE),
    Rule("feedback", "contains", "강제종료", "manual_force_kill", CLASS_GATE),
    Rule("feedback", "contains", "[dependency repair]", "dependency_block", CLASS_GATE),
    Rule("feedback", "contains", "[codex]", "codex_auth_fallback", CLASS_INFRA),
)


def classify(
    error_detail: Optional[str],
    review_feedback: Optional[str] = None,
) -> tuple[str, str, str]:
    """(failure_class, failure_subtype, signal_source) 를 돌려준다.

    SQL 뷰의 CASE 와 같은 결과를 내야 한다. 규칙을 고치면 마이그레이션도 같이 고친다.
    """
    detail = (error_detail or "").strip().lower()
    feedback = (review_feedback or "").strip().lower()

    for rule in RULES:
        if rule.field == "detail":
            haystack = detail
        else:
            # error_detail 이 있으면 그것이 1순위 신호다 — feedback 규칙은 보지 않는다.
            if detail:
                continue
            haystack = feedback
        if not haystack:
            continue
        if rule.match == "prefix":
            if not haystack.startswith(rule.needle):
                continue
        elif rule.needle not in haystack:
            continue
        if rule.also_contains and rule.also_contains not in haystack:
            continue
        source = "error_detail" if rule.field == "detail" else "review_feedback"
        return rule.failure_class, rule.subtype, source

    if detail:
        source = "error_detail"
    elif feedback:
        source = "review_feedback"
    else:
        source = "none"
    return CLASS_UNCLASSIFIED, "unknown", source


CLASS_LABELS_KO = {
    CLASS_INSTRUCTION: "지시서 결함",
    CLASS_INFRA: "리뷰·CLI 인프라",
    CLASS_GATE: "정상 게이트 차단",
    CLASS_UNCLASSIFIED: "미분류",
}
