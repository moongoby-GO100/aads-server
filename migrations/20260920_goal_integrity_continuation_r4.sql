-- AADS-GOAL-INTEGRITY-CONTINUATION-R4
-- Preserve history, retire the duplicate M10 card, and replace the synthetic
-- one-to-one wrappers with an executable M16 Epic -> Story -> Task hierarchy.
BEGIN;

CREATE TABLE IF NOT EXISTS goal_hierarchy_repair_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    goal_id UUID NOT NULL,
    entry_key TEXT NOT NULL,
    phase TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, goal_id, entry_key, phase)
);

DO $$
DECLARE
    v_tenant CONSTANT UUID := '2d701a8c-9596-4757-8588-faa4f7837112';
    v_goal CONSTANT UUID := 'cf1ec2f6-0072-4f85-aa5e-b08760cd6613';
    v_m10 CONSTANT UUID := '0f97ce3d-45c4-411c-ab08-35ba5aea21d2';
    v_m16 CONSTANT UUID := 'c83dd908-9351-4a84-b9cf-96f4d221a29b';
    v_project CONSTANT TEXT := 'AADS';
    v_assignment UUID;
    v_owner UUID;
    v_synthetic_count INTEGER;
    v_epics INTEGER;
    v_stories INTEGER;
    v_tasks INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM goals WHERE id = v_goal) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM goals
         WHERE id = v_goal AND tenant_id = v_tenant AND project = v_project
    ) THEN
        RAISE EXCEPTION 'goal integrity R4 tenant/project mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM milestones
         WHERE id = v_m16 AND goal_id = v_goal AND tenant_id = v_tenant
           AND project = v_project AND sequence_order = 22
           AND status = 'in_progress'
           AND title = 'M16/W-16. 데이터 진단·블루그린 릴리스'
    ) THEN
        RAISE EXCEPTION 'goal integrity R4 canonical M16 mismatch';
    END IF;

    SELECT id, session_id INTO v_assignment, v_owner
      FROM project_role_assignments
     WHERE tenant_id = v_tenant AND project = v_project AND active
     ORDER BY CASE WHEN role_key = 'GoalSystemAdmin' THEN 0 ELSE 1 END,
              assigned_at, id
     LIMIT 1;
    IF v_assignment IS NULL OR v_owner IS NULL THEN
        RAISE EXCEPTION 'goal integrity R4 requires an active AADS assignment';
    END IF;

    SELECT count(*) INTO v_synthetic_count
      FROM work_items
     WHERE tenant_id = v_tenant AND project = v_project AND goal_id = v_goal
       AND idempotency_key LIKE 'goal-v12-backfill:%';
    IF v_synthetic_count <> 81 THEN
        RAISE EXCEPTION 'goal integrity R4 expected 81 synthetic rows, found %',
            v_synthetic_count;
    END IF;

    INSERT INTO goal_hierarchy_repair_audit
        (tenant_id, project, goal_id, entry_key, phase, payload)
    SELECT v_tenant, v_project, v_goal,
           'aads-goal-integrity-continuation-r4-20260920', 'before',
           jsonb_build_object(
               'goal', (SELECT to_jsonb(g) FROM goals g WHERE g.id = v_goal),
               'm10', (SELECT to_jsonb(m) FROM milestones m WHERE m.id = v_m10),
               'synthetic_items', (
                   SELECT jsonb_agg(to_jsonb(w) ORDER BY w.id)
                     FROM work_items w
                    WHERE w.tenant_id = v_tenant AND w.project = v_project
                      AND w.goal_id = v_goal
                      AND w.idempotency_key LIKE 'goal-v12-backfill:%'
               )
           )
    ON CONFLICT (tenant_id, goal_id, entry_key, phase) DO NOTHING;

    UPDATE milestones
       SET status = 'superseded',
           review_note = concat_ws(
               E'\n', NULLIF(review_note, ''),
               'R4 superseded by canonical M16/W-16 c83dd908-9351-4a84-b9cf-96f4d221a29b.'
           ),
           criteria_history = COALESCE(criteria_history, '[]'::jsonb)
               || jsonb_build_array(jsonb_build_object(
                   'event', 'superseded',
                   'reason', 'duplicate_release_milestone',
                   'canonical_milestone_id', v_m16::text,
                   'at', clock_timestamp()
               )),
           updated_at = clock_timestamp()
     WHERE id = v_m10 AND goal_id = v_goal AND status = 'blocked';

    IF NOT EXISTS (
        SELECT 1 FROM milestones WHERE id = v_m10 AND goal_id = v_goal
          AND status = 'superseded'
    ) THEN
        RAISE EXCEPTION 'goal integrity R4 M10 was not safely superseded';
    END IF;

    UPDATE work_items
       SET status = 'cancelled', progress = 0, completed_at = NULL,
           description = CASE
               WHEN COALESCE(description, '') LIKE '%[legacy synthetic wrapper]%' THEN description
               ELSE concat_ws(E'\n', description,
                    '[legacy synthetic wrapper] Retained for audit; hidden from the default tree.')
           END,
           version = version + 1,
           updated_at = clock_timestamp()
     WHERE tenant_id = v_tenant AND project = v_project AND goal_id = v_goal
       AND idempotency_key LIKE 'goal-v12-backfill:%'
       AND (status <> 'cancelled'
            OR COALESCE(description, '') NOT LIKE '%[legacy synthetic wrapper]%');

    INSERT INTO work_items
        (tenant_id, project, goal_id, milestone_id, type, title, description,
         acceptance_criteria, status, priority, assignment_id, idempotency_key,
         created_by)
    SELECT v_tenant, v_project, v_goal, v_m16, 'epic', x.title, x.description,
           x.criteria, 'in_progress', 'P1', v_assignment, x.item_key, v_owner
      FROM (VALUES
        ('goal-integrity-r4:m16:epic:data', '운영 데이터 정합성 확정',
         '중복 마일스톤과 synthetic wrapper를 감사 가능한 이력으로 격리한다.',
         '["기본 카드에 terminal history 0건","synthetic 81행 보존·비활성"]'::jsonb),
        ('goal-integrity-r4:m16:epic:release', '단일 SHA 블루그린 릴리스',
         '검증된 단일 이미지로 candidate부터 standby까지 순차 배포한다.',
         '["candidate direct health 통과","routed health 통과","standby same digest"]'::jsonb),
        ('goal-integrity-r4:m16:epic:certify', '목표 완료 인증',
         '5분 P0/P1 관찰과 UI/API 근거를 묶어 목표 완료를 판정한다.',
         '["5분 P0/P1 신규 오류 0건","GoalPanel/API 계층 검증","handover 기록"]'::jsonb)
      ) AS x(item_key, title, description, criteria)
    ON CONFLICT (tenant_id, project, parent_id, idempotency_key) DO NOTHING;

    INSERT INTO work_items
        (tenant_id, project, goal_id, milestone_id, parent_id, type, title,
         description, acceptance_criteria, status, priority, assignment_id,
         idempotency_key, created_by)
    SELECT e.tenant_id, e.project, e.goal_id, e.milestone_id, e.id, 'story',
           x.title, x.description, x.criteria, 'ready', 'P1', v_assignment,
           x.story_key, v_owner
      FROM work_items e
      JOIN (VALUES
        ('goal-integrity-r4:m16:epic:data', 'scope', '목표·테넌트 범위 검증', 'tenant/project/goal/milestone 정본을 고정한다.', '["교차 범위 0건"]'::jsonb),
        ('goal-integrity-r4:m16:epic:data', 'hierarchy', '실제 업무계층 재구성', 'M16을 실행 가능한 3→6→12 계층으로 구성한다.', '["Epic 3, Story 6, Task 12"]'::jsonb),
        ('goal-integrity-r4:m16:epic:release', 'preflight', '릴리스 사전검증', '회귀·diff·migration dry-run을 통과한다.', '["필수 테스트 skip 0","migration 2회 멱등"]'::jsonb),
        ('goal-integrity-r4:m16:epic:release', 'bluegreen', '블루그린 전환', '후보 헬스 후 짧은 nginx lock으로 전환한다.', '["rollback 가능","동일 SHA 이미지 1회 build"]'::jsonb),
        ('goal-integrity-r4:m16:epic:certify', 'monitor', '배포 후 관찰', '라우팅 헬스와 P0/P1 오류를 5분 관찰한다.', '["관찰 구간 신규 P0/P1 0건"]'::jsonb),
        ('goal-integrity-r4:m16:epic:certify', 'close', '정본 완료 처리', 'DB 증거와 handover를 기록한 뒤에만 완료한다.', '["M16 completed","goal completed","handover resolved"]'::jsonb)
      ) AS x(epic_key, story_key, title, description, criteria)
        ON e.idempotency_key = x.epic_key
     WHERE e.tenant_id = v_tenant AND e.project = v_project AND e.goal_id = v_goal
    ON CONFLICT (tenant_id, project, parent_id, idempotency_key) DO NOTHING;

    INSERT INTO work_items
        (tenant_id, project, goal_id, milestone_id, parent_id, type, title,
         description, acceptance_criteria, status, priority, assignment_id,
         idempotency_key, created_by)
    SELECT s.tenant_id, s.project, s.goal_id, s.milestone_id, s.id, 'task',
           x.title, x.description, x.criteria, 'ready', 'P1', v_assignment,
           x.task_key, v_owner
      FROM work_items s
      JOIN (VALUES
        ('scope', 'tenant', '테넌트·프로젝트 경계 확인', 'DB 정본 범위를 확인한다.', '["tenant/project mismatch 0건"]'::jsonb),
        ('scope', 'canonical', 'M10/M16 정본 단일화', 'M10은 이력으로, M16은 실행 정본으로 유지한다.', '["live release milestone 1건"]'::jsonb),
        ('hierarchy', 'retire', 'synthetic wrapper 비활성화', '81개 wrapper를 삭제하지 않고 이력화한다.', '["synthetic active 0건"]'::jsonb),
        ('hierarchy', 'decompose', 'M16 3→6→12 분해 검증', '부모·소유자·인수조건을 검증한다.', '["orphan work item 0건"]'::jsonb),
        ('preflight', 'tests', '관련 회귀시험 실행', 'goal/router/binding 회귀시험을 실행한다.', '["pytest 실패 0건"]'::jsonb),
        ('preflight', 'database', 'PostgreSQL 2회 적용 검증', '트랜잭션에서 migration을 두 번 실행한다.', '["2회 적용 후 중복 0건"]'::jsonb),
        ('bluegreen', 'candidate', 'candidate direct health', 'candidate 슬롯의 직접 health를 검증한다.', '["candidate health 200"]'::jsonb),
        ('bluegreen', 'route', 'cutover·routed health·standby sync', '라우팅 전환 후 standby를 같은 digest로 동기화한다.', '["routed health 200","slot digest 동일"]'::jsonb),
        ('monitor', 'window', '5분 오류 관찰', 'release 시각 이후 P0/P1 신규 오류를 확인한다.', '["P0/P1 신규 오류 0건"]'::jsonb),
        ('monitor', 'ui', 'GoalPanel 화면 검증', '기본 카드·업무트리·복구 상태를 확인한다.', '["cancelled/superseded 기본 노출 0건"]'::jsonb),
        ('close', 'evidence', '완료 증거 저장', 'commit/deploy/test/UI 근거를 M16에 저장한다.', '["evidence 필드 비어있지 않음"]'::jsonb),
        ('close', 'handover', '핸드오버·목표 완료', 'DB 핸드오버 후 목표를 완료한다.', '["handover DB 1건","goal progress 1.0"]'::jsonb)
      ) AS x(story_key, task_key, title, description, criteria)
        ON s.idempotency_key = x.story_key
     WHERE s.tenant_id = v_tenant AND s.project = v_project AND s.goal_id = v_goal
       AND s.type = 'story'
    ON CONFLICT (tenant_id, project, parent_id, idempotency_key) DO NOTHING;

    WITH RECURSIVE canonical_tree AS (
        SELECT id, type
          FROM work_items
         WHERE tenant_id = v_tenant AND project = v_project AND goal_id = v_goal
           AND milestone_id = v_m16
           AND idempotency_key LIKE 'goal-integrity-r4:m16:epic:%'
        UNION ALL
        SELECT child.id, child.type
          FROM work_items child
          JOIN canonical_tree parent ON child.parent_id = parent.id
         WHERE child.tenant_id = v_tenant AND child.project = v_project
           AND child.goal_id = v_goal AND child.milestone_id = v_m16
    )
    SELECT count(*) FILTER (WHERE type = 'epic'),
           count(*) FILTER (WHERE type = 'story'),
           count(*) FILTER (WHERE type = 'task')
      INTO v_epics, v_stories, v_tasks
      FROM canonical_tree;
    IF (v_epics, v_stories, v_tasks) IS DISTINCT FROM (3, 6, 12) THEN
        RAISE EXCEPTION 'goal integrity R4 hierarchy mismatch: %/%/%',
            v_epics, v_stories, v_tasks;
    END IF;

    UPDATE goals
       SET status = 'active', completed_at = NULL,
           progress = (
               SELECT round(
                   count(*) FILTER (WHERE status = 'completed')::numeric
                   / NULLIF(count(*) FILTER (
                       WHERE status NOT IN ('cancelled', 'superseded', 'archived')
                   ), 0), 2
               )
                 FROM milestones WHERE goal_id = v_goal
           ),
           updated_at = clock_timestamp()
     WHERE id = v_goal AND tenant_id = v_tenant AND project = v_project;

    INSERT INTO goal_hierarchy_repair_audit
        (tenant_id, project, goal_id, entry_key, phase, payload)
    VALUES (
        v_tenant, v_project, v_goal,
        'aads-goal-integrity-continuation-r4-20260920', 'repair',
        jsonb_build_object(
            'canonical_milestone_id', v_m16,
            'superseded_milestone_id', v_m10,
            'synthetic_preserved', v_synthetic_count,
            'shape', jsonb_build_object('epics', v_epics, 'stories', v_stories, 'tasks', v_tasks)
        )
    ) ON CONFLICT (tenant_id, goal_id, entry_key, phase) DO NOTHING;
END $$;

COMMIT;
