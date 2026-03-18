-- 개발 워크플로우 프로세스 메모리 주입 (모든 세션에 반영)
-- 2026-03-18 CEO 지시
-- ai_meta_memory: UNIQUE(project, category, key), value=jsonb
-- ai_observations: UNIQUE(category, key, COALESCE(project,'')), value=text

-- 1. ai_meta_memory (learn_pattern) → <memory_learned> 블록
INSERT INTO ai_meta_memory (project, category, key, value, confidence)
VALUES (
  '',
  'ceo_preference',
  'dev_workflow_process',
  '{"rule": "개발 수정 작업 필수 프로세스 (Pipeline Runner + Agent SDK + delegate_to_agent 모두 동일)", "process": "1.작업수행 → 2.빌드확인 → 3.git commit(로컬만) → 4.완료보고(diff포함) → 5.채팅AI검수(기존코드보존, 변경범위, 빌드성공) → 6.이상없으면 승인 → 7.git push → 8.서비스배포 | 이상있으면 git reset HEAD~1 후 수정지시 → 재작업", "원칙": "절대 배포를 먼저 하지 않는다. 승인 전 서버 적용 금지. commit → 검수 → 승인 → push → 배포 순서 엄수", "적용범위": "Pipeline Runner, Agent SDK 직접수정, delegate_to_agent 모두 동일 적용"}'::jsonb,
  0.95
)
ON CONFLICT (project, category, key) DO UPDATE
SET value = EXCLUDED.value, confidence = 0.95, updated_at = NOW();

-- 2. ai_observations (ceo_preference) → <memory_preferences> 블록
INSERT INTO ai_observations (project, category, key, value, confidence)
VALUES (
  NULL,
  'ceo_preference',
  'dev_workflow_must_follow',
  '코드 수정 작업 시 반드시 commit→검수→승인→push→배포 순서를 따라야 한다. 배포를 먼저 하고 나중에 커밋하는 것은 절대 금지. Pipeline Runner, Agent SDK 직접수정, delegate_to_agent 모두 동일하게 적용. 거부 시 git reset HEAD~1로 원복 후 재작업.',
  0.95
)
ON CONFLICT (category, key, COALESCE(project, ''::character varying)) DO UPDATE
SET value = EXCLUDED.value, confidence = 0.95, updated_at = NOW();

-- 3. ai_observations (tool_strategy) → <memory_tool_strategy> 블록
INSERT INTO ai_observations (project, category, key, value, confidence)
VALUES (
  NULL,
  'tool_strategy',
  'pipeline_runner_workflow',
  'Pipeline Runner 작업 프로세스: 러너가 코드수정 → 빌드확인 → git commit(로컬) → 채팅방에 diff+완료보고 → 채팅AI가 검수(기존코드보존, 변경범위, 빌드성공) → 이상없으면 승인(approve) → git push + 서비스배포. 이상있으면 거부(reject) → git reset HEAD~1 + 수정피드백 → 러너 재작업. Agent SDK 직접수정도 동일 프로세스 적용.',
  0.95
)
ON CONFLICT (category, key, COALESCE(project, ''::character varying)) DO UPDATE
SET value = EXCLUDED.value, confidence = 0.95, updated_at = NOW();
