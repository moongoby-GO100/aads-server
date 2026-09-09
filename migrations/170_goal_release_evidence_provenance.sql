-- 170_goal_release_evidence_provenance.sql
-- Goal Control ↔ 릴리스 증거 다리: **DB 에 내구성 있게 저장된** 커밋 계보.
--
-- 배경 (2026-09-09 거부 사유):
--  * .dockerignore 가 .git 을 제외하므로 실행 중인 aads-server 컨테이너에는
--    /app/.git 이 존재하지 않는다. 따라서 런타임에서 `git rev-parse` /
--    `git merge-base --is-ancestor` 로 릴리스 포함 여부를 검증하는 설계는
--    프로덕션에서 **절대 성립할 수 없다**.
--  * deploy_runs.release_sha 는 대부분 12자 축약 SHA(267/282행)라 그 자체로는
--    모호하다. 축약 접두사를 런타임에 확장할 수단이 컨테이너 안에 없다.
--
-- 해결: Git 히스토리가 **살아 있는 시점**(호스트의 깨끗한 릴리스 워크트리에서
-- deploy.sh 가 도는 동안)에 계보를 계산해 40자 full SHA 로 못박아 저장한다.
-- 런타임은 이 테이블만 읽고 Git 을 전혀 쓰지 않는다.
--
-- 모든 문장은 IF NOT EXISTS / 조건부라 반복 실행해도 안전하다(멱등).
-- 기존 테이블(deploy_runs, deploy_release_manifests, goal_task_links)의 행을
-- 삭제하거나 값을 되돌리지 않는다 — 컬럼/테이블 추가만 한다.

-- ─── 1) 릴리스 계보 원장 ────────────────────────────────────────────────────
-- 한 행 = "task_sha 커밋이 release_sha 릴리스에 포함된다"는 감사 가능한 사실.
-- relationship:
--   exact    — task_sha == release_sha (그 커밋 자체가 릴리스 HEAD)
--   ancestor — task_sha 가 release_sha 의 조상 (git merge-base --is-ancestor)
-- 접두사/모호한 값은 저장하지 않는다: 두 컬럼 모두 40자 소문자 hex 로 강제한다.
CREATE TABLE IF NOT EXISTS deploy_release_provenance (
    id              BIGSERIAL PRIMARY KEY,
    deploy_run_id   BIGINT NOT NULL REFERENCES deploy_runs(id) ON DELETE CASCADE,
    project         TEXT NOT NULL,
    component       TEXT NOT NULL DEFAULT 'api',
    source_ref      TEXT NOT NULL,
    task_sha        TEXT NOT NULL,
    release_sha     TEXT NOT NULL,
    relationship    TEXT NOT NULL,
    resolved_by     TEXT NOT NULL DEFAULT 'deploy.sh',
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 부분 적용 후 재실행도 안전하게 복구한다. 기존 행은 full task_sha 자체를
-- source_ref 로 사용한다.
ALTER TABLE deploy_release_provenance ADD COLUMN IF NOT EXISTS source_ref TEXT;
UPDATE deploy_release_provenance SET source_ref = task_sha WHERE source_ref IS NULL;
ALTER TABLE deploy_release_provenance ALTER COLUMN source_ref SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deploy_release_provenance_relationship_chk') THEN
        ALTER TABLE deploy_release_provenance
            ADD CONSTRAINT deploy_release_provenance_relationship_chk
            CHECK (relationship IN ('exact', 'ancestor'));
    END IF;
    -- 40자 full SHA 강제 = "모호한 접두사 저장 금지"를 스키마 수준에서 못박는다.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deploy_release_provenance_task_sha_chk') THEN
        ALTER TABLE deploy_release_provenance
            ADD CONSTRAINT deploy_release_provenance_task_sha_chk
            CHECK (task_sha ~ '^[0-9a-f]{40}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deploy_release_provenance_source_ref_chk') THEN
        ALTER TABLE deploy_release_provenance
            ADD CONSTRAINT deploy_release_provenance_source_ref_chk
            CHECK (source_ref ~ '^[0-9a-f]{7,40}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deploy_release_provenance_release_sha_chk') THEN
        ALTER TABLE deploy_release_provenance
            ADD CONSTRAINT deploy_release_provenance_release_sha_chk
            CHECK (release_sha ~ '^[0-9a-f]{40}$');
    END IF;
    -- exact 는 정의상 두 SHA 가 같아야 한다 — 위조된 exact 를 스키마가 막는다.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deploy_release_provenance_exact_chk') THEN
        ALTER TABLE deploy_release_provenance
            ADD CONSTRAINT deploy_release_provenance_exact_chk
            CHECK (relationship <> 'exact' OR task_sha = release_sha);
    END IF;
END $$;

-- 한 배포 실행 안에서 같은 task_sha 는 단 하나의 관계만 가진다.
-- ON CONFLICT DO NOTHING 재기록(멱등 재실행)의 충돌 대상이기도 하다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_release_provenance_run_task
    ON deploy_release_provenance (deploy_run_id, task_sha);
CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_release_provenance_run_source
    ON deploy_release_provenance (deploy_run_id, project, source_ref);

-- 런타임 조회 경로: "이 커밋이 인증된 릴리스에 들어갔는가?"
CREATE INDEX IF NOT EXISTS idx_deploy_release_provenance_task
    ON deploy_release_provenance (project, task_sha, deploy_run_id DESC);
CREATE INDEX IF NOT EXISTS idx_deploy_release_provenance_source
    ON deploy_release_provenance (project, source_ref, deploy_run_id DESC);
CREATE INDEX IF NOT EXISTS idx_deploy_release_provenance_release
    ON deploy_release_provenance (project, release_sha);

COMMENT ON TABLE deploy_release_provenance IS
    'deploy.sh 인증 경로에서 기록한 커밋→릴리스 계보. 런타임은 Git 없이 이 표만 읽는다.';
COMMENT ON COLUMN deploy_release_provenance.relationship IS
    'exact(커밋==릴리스 HEAD) | ancestor(git merge-base --is-ancestor 로 확인)';
COMMENT ON COLUMN deploy_release_provenance.task_sha IS
    '작업 커밋 40자 full SHA. 기록 시점에 Git 으로 해석하며 접두사는 저장하지 않는다.';
COMMENT ON COLUMN deploy_release_provenance.source_ref IS
    '원본 작업 참조. pipeline commit full SHA 또는 기존 release 링크의 7~40자 hex; 호스트 Git에서 task_sha로 유일 해석됨.';
COMMENT ON COLUMN deploy_release_provenance.release_sha IS
    '릴리스 HEAD 40자 full SHA. deploy_runs.release_sha 는 12자 축약이라 여기서 정규화한다.';

-- ─── 2) goal_task_links: 릴리스 증거 출처 기록 ──────────────────────────────
-- 링크가 "릴리스 증거로" 완료 처리됐다는 사실과 그 근거를 행에 남긴다.
-- 이 컬럼이 채워져 있으면 재실행 시 다시 쓰지 않는다(멱등의 근거).
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS release_deploy_run_id BIGINT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS release_sha TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS release_relationship TEXT;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS release_verified_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'goal_task_links_release_sha_chk') THEN
        ALTER TABLE goal_task_links
            ADD CONSTRAINT goal_task_links_release_sha_chk
            CHECK (release_sha IS NULL OR release_sha ~ '^[0-9a-f]{40}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'goal_task_links_release_relationship_chk') THEN
        ALTER TABLE goal_task_links
            ADD CONSTRAINT goal_task_links_release_relationship_chk
            CHECK (release_relationship IS NULL OR release_relationship IN ('exact', 'ancestor'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_goal_task_links_release
    ON goal_task_links (release_deploy_run_id)
    WHERE release_deploy_run_id IS NOT NULL;

COMMENT ON COLUMN goal_task_links.release_deploy_run_id IS
    '이 링크를 완료로 인정한 인증 배포(deploy_runs.id). NULL 이면 릴리스 증거로 완료된 적 없음.';
COMMENT ON COLUMN goal_task_links.release_relationship IS
    'exact | ancestor — deploy_release_provenance 에서 가져온 계보 종류';
