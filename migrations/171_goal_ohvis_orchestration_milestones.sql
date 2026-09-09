-- 171_goal_ohvis_orchestration_milestones.sql
-- 후속 목표 "오비스 자율 오케스트레이션 완성" 정상화 (멱등).
--
-- 프로덕션 관측 (2026-09-09):
--   goals: 오비스 자율 오케스트레이션 완성 | status=active | progress=0 | parent_goal_id=NULL
--   milestones: 0건
-- migration 160 은 이 목표를 draft 로 시드했지만, 선행 목표 "AADS 채팅 시스템
-- 안정화" 가 완료되면서 _update_goal_progress 의 draft 자동 활성화 로직이
-- 마일스톤 0개인 채로 active 로 올렸다. 그 결과 advance_active_goals 가 매
-- 사이클마다 이 목표를 집어 "no_open_milestone" trace 만 찍는 루프가 생겼다.
--
-- 여기서 하는 일
--   1) 순서가 있는 마일스톤 4개를 시드한다 (전부 pending — 완료 처리 금지).
--   2) 부모 게이트: 현재 진행 목표를 parent_goal_id 로 건다.
--   3) 정당한 진행/이력이 하나도 없을 때에만 draft 로 되돌린다.
--
-- 보존 규칙: 기존 마일스톤/링크/진행률을 덮어쓰지 않는다. 이미 작업이 붙었거나
-- 진행률이 있거나 완료된 마일스톤이 있으면 status 를 건드리지 않는다.

DO $$
DECLARE
    v_goal_id   UUID;
    v_parent_id UUID;
    v_seeded    INT := 0;
    v_history   INT := 0;
BEGIN
    SELECT id INTO v_goal_id
    FROM goals
    WHERE project = 'AADS' AND title = '오비스 자율 오케스트레이션 완성'
    LIMIT 1;

    IF v_goal_id IS NULL THEN
        RAISE NOTICE '171: follow-up goal not present — nothing to do (migration 160 not applied?)';
        RETURN;
    END IF;

    -- 1) 순서가 있는 마일스톤 (없을 때만 삽입).
    --    auto_advance=TRUE 라서 앞 단계가 완료되면 GoalStateMachine 이 다음 단계를
    --    자동 개시한다. 시드는 전부 pending 이다 — 부모가 열려 있는 동안에는
    --    goal_advance_gate 가 in_progress 승격을 막는다.
    INSERT INTO milestones (goal_id, title, sequence_order, completion_criteria, auto_advance, status)
    SELECT v_goal_id, seed.title, seed.sequence_order, seed.completion_criteria, TRUE, 'pending'
    FROM (
        VALUES
            (1, '릴리스 증거 기반 목표 자동전진',
                'deploy_release_provenance 에 인증 배포 계보가 쌓이고, goal_task_links 가 릴리스 증거로만 완료 승격된다'),
            (2, '작업↔목표 명시적 바인딩 정착',
                'bind_source=legacy_auto_project 신규 생성 0건, 신규 링크는 explicit_api/explicit_directive 만'),
            (3, '오비스 하네스 추적 상시 기록',
                'goal_release_evidence / goal_advance trace 가 correlation_id 와 함께 ohvis_harness_traces 에 남는다'),
            (4, '러너 STEP 0 보존정책 자동 감사',
                '러너 결과에 기존 구현 분류(유지/수정/신규/삭제)가 근거와 함께 남고 회귀 테스트가 통과한다')
    ) AS seed(sequence_order, title, completion_criteria)
    WHERE NOT EXISTS (
        SELECT 1 FROM milestones m WHERE m.goal_id = v_goal_id AND m.title = seed.title
    );
    GET DIAGNOSTICS v_seeded = ROW_COUNT;

    -- 2) 부모 게이트 — 아직 부모가 없을 때만 건다(운영자가 지정한 부모는 존중).
    IF (SELECT parent_goal_id FROM goals WHERE id = v_goal_id) IS NULL THEN
        SELECT id INTO v_parent_id
        FROM goals
        WHERE project = 'AADS'
          AND title = '채팅 시스템 안정화 및 응답 가독성 개선'
          AND id <> v_goal_id
        LIMIT 1;

        IF v_parent_id IS NOT NULL THEN
            UPDATE goals SET parent_goal_id = v_parent_id, updated_at = NOW()
            WHERE id = v_goal_id;
        END IF;
    END IF;

    -- 3) 정당한 이력이 전혀 없을 때에만 draft 로 되돌린다.
    --    링크가 하나라도 붙었거나, 완료/진행 중 마일스톤이 있거나, 진행률이
    --    0 이 아니면 그대로 둔다 — 실제 진행을 지우지 않기 위해서다.
    SELECT COUNT(*) INTO v_history
    FROM (
        SELECT 1 FROM goal_task_links WHERE goal_id = v_goal_id
        UNION ALL
        SELECT 1 FROM milestones
         WHERE goal_id = v_goal_id AND status IN ('completed', 'in_progress', 'blocked')
        UNION ALL
        SELECT 1 FROM goals
         WHERE id = v_goal_id AND (COALESCE(progress, 0) > 0 OR completed_at IS NOT NULL)
    ) AS history;

    IF v_history = 0 THEN
        UPDATE goals
        SET status = 'draft', updated_at = NOW()
        WHERE id = v_goal_id AND status = 'active';
    END IF;

    RAISE NOTICE '171: goal=% milestones_seeded=% history_rows=%', v_goal_id, v_seeded, v_history;
END $$;
