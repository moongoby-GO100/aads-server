-- OHVIS 문서 정본관리 게이트: milestone -> epic -> story -> task 실행계층.
-- Additive/idempotent. 기존 업무 상태를 덮어쓰지 않는다.
BEGIN;

WITH spec(milestone_id, code, epic_title, story_title, task_title, criterion) AS (
  VALUES
    ('a2bbc46d-02d8-4823-b84b-23d51e1cb47b'::uuid,'dg1-1','DG1.1 정본 대장 등록 실행','정본 등록 범위 확정·반영','docs/specs 99건 등록 검증','is_latest specs 정본 99건'),
    ('9ec5f0be-6601-4c4e-83ae-e22ed5d38e93'::uuid,'dg1-2','DG1.2 이중 정본 해소 실행','중복 물리경로 판별·정리','이중 정본 0건 검증','동일 물리 경로 active/latest 중복 0건'),
    ('ab72a317-2947-4887-a9dc-d51d1809c8fe'::uuid,'dg1-3','DG1.3 등록 API 회귀방지','등록 identity 정규화 검증','중복등록 회귀 테스트','회귀 테스트 통과 및 origin/main 반영'),
    ('29f38bed-1c1e-4072-9b0c-fbf80281a4e0'::uuid,'dg2-2','DG2.2 훅 동기화 강제','저장소본·설치본 일치 보장','pre-commit 설치본 동기화 검증','두 pre-commit 파일 diff 결과 SAME'),
    ('1ff87dec-0776-4396-8ffb-31682bfe52bc'::uuid,'dg2-3','DG2.3 수렴 하드게이트','owner_resolved 제출 전제 강제','미수렴 구현 제출 거부 검증','owner_resolved=false 제출이 실제 거부'),
    ('96b301f4-5e83-42ac-995e-2d6ef5beb3c0'::uuid,'dg3-1','DG3.1 색인 확장자 정책','Markdown 단일 색인 정책 적용','비-md 색인 차단 검증','수집 확장자 .md 단일 및 기존 청크 방침 확정'),
    ('45be2217-4d6d-4cb2-a607-d1a256d75a6e'::uuid,'dg3-2','DG3.2 정본 색인 반영','신규 정본 색인 파이프라인 검증','정본별 청크 존재 검증','각 최신 정본 doc_path의 doc_chunks가 1개 이상'),
    ('1b954bf2-05d0-45be-97ac-05ddba9cf8f4'::uuid,'dg4-1','DG4.1 리뷰 장애 규명','리뷰 모델 설정 오류 확정','error_book signature 검증','확정 원인 등록 및 실제 로그 signature 매칭'),
    ('f198fb60-56b0-4678-a4e1-8626d43521b6'::uuid,'dg4-2','DG4.2 허위 원인 회귀방지','허위 실패원인 사례 정본화','prevention·fix 분리 검증','error_book 항목에 prevention과 fix 근거 존재')
)
INSERT INTO work_items
    (tenant_id,project,goal_id,milestone_id,type,title,description,acceptance_criteria,
     status,priority,idempotency_key,completed_at)
SELECT g.tenant_id,g.project,g.id,m.id,'epic',s.epic_title,
       'OHVIS 문서 정본관리 게이트 마일스톤 실행 묶음',jsonb_build_array(s.criterion),
       CASE m.status WHEN 'completed' THEN 'completed' WHEN 'blocked' THEN 'blocked'
            WHEN 'in_progress' THEN 'in_progress' ELSE 'ready' END,
       g.priority,'ohvis-'||s.code||'-epic',
       CASE WHEN m.status='completed' THEN clock_timestamp() END
  FROM spec s JOIN milestones m ON m.id=s.milestone_id JOIN goals g ON g.id=m.goal_id
ON CONFLICT DO NOTHING;

WITH spec(milestone_id, code, title, criterion) AS (
  VALUES
    ('a2bbc46d-02d8-4823-b84b-23d51e1cb47b'::uuid,'dg1-1','정본 등록 범위 확정·반영','is_latest specs 정본 99건'),
    ('9ec5f0be-6601-4c4e-83ae-e22ed5d38e93'::uuid,'dg1-2','중복 물리경로 판별·정리','동일 물리 경로 active/latest 중복 0건'),
    ('ab72a317-2947-4887-a9dc-d51d1809c8fe'::uuid,'dg1-3','등록 identity 정규화 검증','회귀 테스트 통과 및 origin/main 반영'),
    ('29f38bed-1c1e-4072-9b0c-fbf80281a4e0'::uuid,'dg2-2','저장소본·설치본 일치 보장','두 pre-commit 파일 diff 결과 SAME'),
    ('1ff87dec-0776-4396-8ffb-31682bfe52bc'::uuid,'dg2-3','owner_resolved 제출 전제 강제','owner_resolved=false 제출이 실제 거부'),
    ('96b301f4-5e83-42ac-995e-2d6ef5beb3c0'::uuid,'dg3-1','Markdown 단일 색인 정책 적용','수집 확장자 .md 단일 및 기존 청크 방침 확정'),
    ('45be2217-4d6d-4cb2-a607-d1a256d75a6e'::uuid,'dg3-2','신규 정본 색인 파이프라인 검증','각 최신 정본 doc_path의 doc_chunks가 1개 이상'),
    ('1b954bf2-05d0-45be-97ac-05ddba9cf8f4'::uuid,'dg4-1','리뷰 모델 설정 오류 확정','확정 원인 등록 및 실제 로그 signature 매칭'),
    ('f198fb60-56b0-4678-a4e1-8626d43521b6'::uuid,'dg4-2','허위 실패원인 사례 정본화','error_book 항목에 prevention과 fix 근거 존재')
)
INSERT INTO work_items
    (tenant_id,project,goal_id,milestone_id,parent_id,type,title,description,
     acceptance_criteria,status,priority,idempotency_key,completed_at)
SELECT e.tenant_id,e.project,e.goal_id,e.milestone_id,e.id,'story',s.title,
       '마일스톤 완료기준을 단일 검증 흐름으로 구현',jsonb_build_array(s.criterion),
       e.status,e.priority,'ohvis-'||s.code||'-story',e.completed_at
  FROM spec s JOIN work_items e ON e.milestone_id=s.milestone_id
   AND e.type='epic' AND e.idempotency_key='ohvis-'||s.code||'-epic'
ON CONFLICT DO NOTHING;

WITH spec(milestone_id, code, title, criterion) AS (
  VALUES
    ('a2bbc46d-02d8-4823-b84b-23d51e1cb47b'::uuid,'dg1-1','docs/specs 99건 등록 검증','is_latest specs 정본 99건'),
    ('9ec5f0be-6601-4c4e-83ae-e22ed5d38e93'::uuid,'dg1-2','이중 정본 0건 검증','동일 물리 경로 active/latest 중복 0건'),
    ('ab72a317-2947-4887-a9dc-d51d1809c8fe'::uuid,'dg1-3','중복등록 회귀 테스트','회귀 테스트 통과 및 origin/main 반영'),
    ('29f38bed-1c1e-4072-9b0c-fbf80281a4e0'::uuid,'dg2-2','pre-commit 설치본 동기화 검증','두 pre-commit 파일 diff 결과 SAME'),
    ('1ff87dec-0776-4396-8ffb-31682bfe52bc'::uuid,'dg2-3','미수렴 구현 제출 거부 검증','owner_resolved=false 제출이 실제 거부'),
    ('96b301f4-5e83-42ac-995e-2d6ef5beb3c0'::uuid,'dg3-1','비-md 색인 차단 검증','수집 확장자 .md 단일 및 기존 청크 방침 확정'),
    ('45be2217-4d6d-4cb2-a607-d1a256d75a6e'::uuid,'dg3-2','정본별 청크 존재 검증','각 최신 정본 doc_path의 doc_chunks가 1개 이상'),
    ('1b954bf2-05d0-45be-97ac-05ddba9cf8f4'::uuid,'dg4-1','error_book signature 검증','확정 원인 등록 및 실제 로그 signature 매칭'),
    ('f198fb60-56b0-4678-a4e1-8626d43521b6'::uuid,'dg4-2','prevention·fix 분리 검증','error_book 항목에 prevention과 fix 근거 존재')
)
INSERT INTO work_items
    (tenant_id,project,goal_id,milestone_id,parent_id,type,title,description,
     acceptance_criteria,status,priority,idempotency_key,completed_at)
SELECT st.tenant_id,st.project,st.goal_id,st.milestone_id,st.id,'task',s.title,
       '실행 후 명령·쿼리 결과를 evidence로 연결',jsonb_build_array(s.criterion),
       st.status,st.priority,'ohvis-'||s.code||'-task',st.completed_at
  FROM spec s JOIN work_items st ON st.milestone_id=s.milestone_id
   AND st.type='story' AND st.idempotency_key='ohvis-'||s.code||'-story'
ON CONFLICT DO NOTHING;

WITH scope AS (
  SELECT m.id AS milestone_id, m.goal_id AS goal_id,
         m.tenant_id AS tenant_id, m.project AS project, g.priority AS priority
    FROM milestones m JOIN goals g ON g.id=m.goal_id
   WHERE m.id='62d09718-db3e-4a09-b4a8-03fa8a99b05d'::uuid
)
INSERT INTO work_items
    (tenant_id,project,goal_id,milestone_id,type,title,description,
     acceptance_criteria,status,priority,idempotency_key)
SELECT tenant_id,project,goal_id,milestone_id,'epic','DG2.1 표류 해소·차단 승격 실행',
       '문서 표류를 실제값으로 정정한 뒤 pre-commit을 fail-closed로 승격',
       '["전체 스캔 EXIT=0","표류 staged 문서 차단","ALLOW_DOC_DRIFT=1만 예외"]'::jsonb,
       'in_progress',priority,'ohvis-dg2-1-epic' FROM scope
ON CONFLICT DO NOTHING;

WITH spec(code,title,criterion) AS (
  VALUES ('drift','DG2.1-S1 표류 기준값 정정','전체 doc_drift 스캔 표류 0건'),
         ('hook','DG2.1-S2 pre-commit 하드 게이트','표류 staged 문서 차단과 명시 예외'),
         ('verify','DG2.1-S3 인수검증·증거','자동 테스트·설치본·evidence 검증')
), epic AS (
  SELECT * FROM work_items WHERE idempotency_key='ohvis-dg2-1-epic' -- gitleaks:allow
   AND milestone_id='62d09718-db3e-4a09-b4a8-03fa8a99b05d'::uuid
)
INSERT INTO work_items
    (tenant_id,project,goal_id,milestone_id,parent_id,type,title,description,
     acceptance_criteria,status,priority,idempotency_key)
SELECT e.tenant_id,e.project,e.goal_id,e.milestone_id,e.id,'story',s.title,
       'DG2.1 독립 검증 흐름',jsonb_build_array(s.criterion),'in_progress',e.priority,
       'ohvis-dg2-1-story-'||s.code FROM epic e CROSS JOIN spec s
ON CONFLICT DO NOTHING;

WITH spec(story_code,task_code,title,criterion) AS (
  VALUES
    ('drift','backend','DG2.1-T1 백엔드 문서 수치 정정','백엔드 문서 대상 표류 0건'),
    ('drift','frontend','DG2.1-T2 대시보드 문서 수치 정정','프론트 문서 대상 표류 0건'),
    ('drift','scan','DG2.1-T3 전체 스캔 0건 확인','python3 scripts/doc_drift_check.py EXIT=0'),
    ('hook','fail','DG2.1-T4 --warn 제거·비정상 종료 차단','표류 문서를 stage하면 hook EXIT!=0'),
    ('hook','override','DG2.1-T5 ALLOW_DOC_DRIFT=1 예외','명시 예외에서만 hook EXIT=0'),
    ('hook','sync','DG2.1-T6 설치 훅 동기화','저장소본과 설치본 diff -q 성공'),
    ('verify','block-test','DG2.1-T7 차단 fixture 테스트','자동 테스트가 차단 경로 재현'),
    ('verify','override-test','DG2.1-T8 예외 fixture 테스트','자동 테스트가 예외 경로 재현'),
    ('verify','evidence','DG2.1-T9 검증 증거·마일스톤 보고','명령·커밋·DB evidence 연결')
)
INSERT INTO work_items
    (tenant_id,project,goal_id,milestone_id,parent_id,type,title,description,
     acceptance_criteria,status,priority,idempotency_key)
SELECT st.tenant_id,st.project,st.goal_id,st.milestone_id,st.id,'task',s.title,
       '검증 가능한 최소 실행 단위',jsonb_build_array(s.criterion),'ready',st.priority,
       'ohvis-dg2-1-task-'||s.task_code
  FROM spec s JOIN work_items st
    ON st.idempotency_key='ohvis-dg2-1-story-'||s.story_code
   AND st.milestone_id='62d09718-db3e-4a09-b4a8-03fa8a99b05d'::uuid
ON CONFLICT DO NOTHING;

COMMIT;
