-- 160_goal_seed_chat_stability_and_ohvis_followup.sql
-- Goal Control Loop 진행 목표 시드 (멱등).
--
-- 배경: 이전 AADS 목표 "AADS 채팅 시스템 안정화"가 completed 로 전이되면서
-- AADS 프로젝트에 active 목표가 없어졌고, pipeline_runner_service._auto_link_job_to_goal
-- 이 연결할 대상이 사라졌다. 현재 진행 중인 목표와 후속 목표를 시드한다.
--
-- 멱등성: 동일 project+title 목표/마일스톤이 이미 있으면 아무 것도 하지 않는다.
-- 완료 처리 금지: 목표/마일스톤을 완료 상태로 만들지 않는다.
-- 기존 목표(153/156/157 스키마, 기존 6개 목표)는 수정·삭제하지 않는다.

DO $$
DECLARE
    v_goal_id UUID;
    v_followup_id UUID;
BEGIN
    -- 1) 진행 목표: 채팅 시스템 안정화 및 응답 가독성 개선
    SELECT id INTO v_goal_id
    FROM goals
    WHERE project = 'AADS' AND title = '채팅 시스템 안정화 및 응답 가독성 개선'
    LIMIT 1;

    IF v_goal_id IS NULL THEN
        INSERT INTO goals (project, title, priority, status, description, success_criteria, created_by)
        VALUES (
            'AADS',
            '채팅 시스템 안정화 및 응답 가독성 개선',
            'P1',
            'active',
            'CEO 채팅 경로의 응답 유실·중단을 없애고, 응답 구조와 가독성을 일관되게 유지한다.',
            'chat_turn_executions 에 running/retrying/interrupted 잔류 0건, 스트리밍 중단 재현 0건, 응답 구조 템플릿 준수.',
            'CEO'
        )
        RETURNING id INTO v_goal_id;
    END IF;

    INSERT INTO milestones (goal_id, title, sequence_order, completion_criteria, auto_advance, status, started_at)
    SELECT
        v_goal_id,
        seed.title,
        seed.sequence_order,
        seed.completion_criteria,
        TRUE,
        CASE WHEN seed.sequence_order = 1 THEN 'in_progress' ELSE 'pending' END,
        CASE WHEN seed.sequence_order = 1 THEN NOW() ELSE NULL END
    FROM (
        VALUES
            (1, '스트리밍 응답 유실 방지',
                '워치독/부분응답 보존으로 스트림 중단 재현 0건, chat_turn_executions 잔류 running 0건'),
            (2, '응답 구조와 가독성 표준화',
                '응답 구조 템플릿(response_structure_templates) 적용률 확인 및 렌더링 회귀 없음'),
            (3, '무중단 배포 중 세션 연속성 보장',
                'blue/green 전환 중 활성 SSE 스트림 유실 0건, owner_instance/owner_epoch 리스 준수'),
            (4, '회귀 테스트와 모니터링 고정',
                '채팅 관련 단위 테스트 통과 + 배포 후 5분 P0/P1 모니터링 신규 에러 0건')
    ) AS seed(sequence_order, title, completion_criteria)
    WHERE NOT EXISTS (
        SELECT 1 FROM milestones m
        WHERE m.goal_id = v_goal_id AND m.title = seed.title
    );

    -- 2) 후속 목표(draft): 오비스 자율 오케스트레이션 완성
    -- 마일스톤 없이 draft 로 둔다. advance_goal 은 pending 마일스톤이 있는 draft 목표를
    -- active 로 승격시키므로, 마일스톤을 미리 넣지 않아야 draft 상태가 유지된다.
    SELECT id INTO v_followup_id
    FROM goals
    WHERE project = 'AADS' AND title = '오비스 자율 오케스트레이션 완성'
    LIMIT 1;

    IF v_followup_id IS NULL THEN
        INSERT INTO goals (project, title, priority, status, description, success_criteria, created_by)
        VALUES (
            'AADS',
            '오비스 자율 오케스트레이션 완성',
            'P2',
            'draft',
            '목표→마일스톤→작업 자동 연결과 harness 증거 기록을 오비스 자율 실행까지 확장한다.',
            '목표 진행 이벤트가 ohvis_harness_traces 에 기록되고, 러너가 STEP 0 보존 정책을 근거와 함께 준수한다.',
            'CEO'
        );
    END IF;
END $$;
