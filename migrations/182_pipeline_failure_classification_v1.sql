-- 182_pipeline_failure_classification_v1.sql
-- M1 실패 원인 계측 — 러너 실패를 지시서결함/인프라/정상게이트 3분류로 나누는 뷰.
--
-- 규칙 원본은 app/services/pipeline_failure_taxonomy.py 의 RULES 다.
-- 아래 CASE 는 그 RULES 와 **같은 순서**여야 하며(순서가 곧 우선순위),
-- tests/unit/test_pipeline_failure_taxonomy.py 가 둘의 동기화를 검사한다.
-- 한쪽만 고치면 테스트가 막는다.
--
-- 신호 우선순위: error_detail → (비어 있으면) review_feedback.
-- 2026-09-23 실측: 최근 7일 error 579건 중 193건이 error_detail NULL 이었고
-- 그 전부가 review_feedback 에 [Runner Guard](91) 또는 강제종료(102) 를 갖고 있었다.

DROP VIEW IF EXISTS pipeline_failure_classification_v1;

CREATE VIEW pipeline_failure_classification_v1 AS
SELECT
    j.job_id,
    j.tenant_id,
    j.project,
    j.status,
    j.created_at,
    j.updated_at,
    COALESCE(NULLIF(j.actual_model, ''), NULLIF(j.model, ''), 'unknown') AS model_key,
    j.goal_id,
    j.milestone_id,
    split_part(t.tag, '|', 1) AS failure_class,
    split_part(t.tag, '|', 2) AS failure_subtype,
    CASE
        WHEN btrim(COALESCE(j.error_detail, '')) <> '' THEN 'error_detail'
        WHEN btrim(COALESCE(j.review_feedback, '')) <> '' THEN 'review_feedback'
        ELSE 'none'
    END AS signal_source,
    LEFT(
        COALESCE(NULLIF(btrim(COALESCE(j.error_detail, '')), ''),
                 NULLIF(btrim(COALESCE(j.review_feedback, '')), ''),
                 ''),
        200
    ) AS signal_sample
FROM pipeline_jobs j
CROSS JOIN LATERAL (
    SELECT lower(btrim(COALESCE(j.error_detail, '')))   AS d,
           lower(btrim(COALESCE(j.review_feedback, ''))) AS f
) s
CROSS JOIN LATERAL (
    SELECT CASE
        -- 리뷰·CLI 인프라
        WHEN strpos(s.d, 'review_infra_failed') = 1 THEN 'infra|review_infra'
        WHEN strpos(s.d, 'review_model_config_invalid') > 0 THEN 'infra|review_config_invalid'
        WHEN strpos(s.d, 'review_origin_adjudication') = 1 THEN 'infra|adjudication_unresolved'
        WHEN strpos(s.d, 'llm_quota_exhausted') = 1 THEN 'infra|llm_quota'
        WHEN strpos(s.d, 'rate_limit') = 1 THEN 'infra|rate_limit'
        WHEN strpos(s.d, 'server_restart_orphan') = 1 THEN 'infra|server_restart_orphan'
        WHEN strpos(s.d, 'watchdog_stall') = 1 THEN 'infra|watchdog_stall'
        WHEN strpos(s.d, 'timeout_max_runtime') = 1 THEN 'infra|runtime_timeout'
        WHEN strpos(s.d, 'approval_commit_failed') = 1 THEN 'infra|git_commit_failed'
        WHEN strpos(s.d, 'git_fetch_failed') = 1 THEN 'infra|git_fetch_failed'
        -- 배포·push: preflight / stale base 는 게이트, 그 밖의 실패는 인프라
        WHEN strpos(s.d, 'deploy_preflight') = 1 THEN 'gate_block|deploy_preflight_gate'
        WHEN strpos(s.d, 'push_stale_base') = 1 THEN 'gate_block|push_stale_base_gate'
        WHEN strpos(s.d, 'push_fail') = 1 THEN 'infra|push_failed'
        WHEN strpos(s.d, 'deploy_') = 1 THEN 'infra|deploy_infra'
        WHEN strpos(s.d, 'health_check_fail_rollback') = 1 THEN 'infra|health_rollback'
        WHEN strpos(s.d, 'post_cutover evidence write failure') > 0 THEN 'infra|deploy_infra'
        -- 지시서 결함
        WHEN strpos(s.d, 'review_failed') = 1 THEN 'instruction_defect|review_request_changes'
        WHEN strpos(s.d, ':build_failed') > 0 THEN 'instruction_defect|build_failed'
        WHEN strpos(s.d, 'invalid_aads_target') = 1 THEN 'instruction_defect|invalid_target'
        -- 의존·중복·수동 게이트
        WHEN strpos(s.d, 'blocked_dependency') = 1 THEN 'gate_block|dependency_block'
        WHEN strpos(s.d, 'orphaned_dependency') = 1 THEN 'gate_block|dependency_block'
        WHEN strpos(s.d, 'parent ') = 1 THEN 'gate_block|dependency_block'
        WHEN strpos(s.d, 'dedup_blocked') = 1 THEN 'gate_block|dedup_gate'
        WHEN strpos(s.d, 'duplicate') = 1 THEN 'gate_block|dedup_gate'
        WHEN strpos(s.d, 'manual_stop') = 1 THEN 'gate_block|manual_stop'
        WHEN strpos(s.d, 'superseded') = 1 THEN 'gate_block|superseded'
        WHEN strpos(s.d, 'cancelled before execution') > 0 THEN 'gate_block|superseded'
        WHEN strpos(s.d, 'manual review rejected') > 0 THEN 'gate_block|manual_review_rejected'
        WHEN strpos(s.d, 'manual_review_failed') = 1 THEN 'gate_block|manual_review_rejected'
        -- no_changes: "완료" 보고면 게이트, 아니면 지시 충돌
        WHEN strpos(s.d, 'no_changes') = 1 AND strpos(s.d, '완료') > 0
            THEN 'gate_block|no_changes_already_done'
        WHEN strpos(s.d, 'no_changes') = 1
            THEN 'instruction_defect|no_changes_instruction_conflict'
        -- error_detail 이 빈 경우의 2순위 신호
        WHEN s.d = '' AND strpos(s.f, '[runner guard]') > 0 THEN 'gate_block|runner_guard_conflict'
        WHEN s.d = '' AND strpos(s.f, '강제종료') > 0 THEN 'gate_block|manual_force_kill'
        WHEN s.d = '' AND strpos(s.f, '[dependency repair]') > 0 THEN 'gate_block|dependency_block'
        WHEN s.d = '' AND strpos(s.f, '[codex]') > 0 THEN 'infra|codex_auth_fallback'
        ELSE 'unclassified|unknown'
    END AS tag
) t
WHERE j.status IN ('error', 'cancelled');

COMMENT ON VIEW pipeline_failure_classification_v1 IS
    'M1 실패 3분류(instruction_defect/infra/gate_block). 규칙 원본: app/services/pipeline_failure_taxonomy.py';

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_failed_created
    ON pipeline_jobs (created_at DESC)
    WHERE status IN ('error', 'cancelled');
