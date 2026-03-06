-- AADS-128: 프로젝트 산출물 통합 DB 테이블
-- 모든 에이전트 산출물(전략보고서, PRD, 아키텍처, 코드, 테스트 결과, 배포 등)을 통합 저장

CREATE TABLE IF NOT EXISTS project_artifacts (
    id           SERIAL PRIMARY KEY,
    project_id   TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    content      JSONB NOT NULL,
    source_agent TEXT,
    source_task  TEXT,
    version      INTEGER DEFAULT 1,
    created_at   TIMESTAMP DEFAULT NOW()
);

-- artifact_type 값:
--   strategy_report  — Strategist 시장조사 보고서
--   prd              — Planner PRD (Product Requirements Document)
--   architecture     — Planner 아키텍처 설계
--   phase_plan       — Planner 페이즈 계획
--   taskspec         — TaskSpec 목록
--   code             — Developer 생성 코드
--   test_result      — QA 테스트 결과
--   deployment       — DevOps 배포 결과

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON project_artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type    ON project_artifacts(artifact_type);
