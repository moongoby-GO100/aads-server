-- 목표 문서 버전 원장.
--
-- 기존 goal_documents 는 경로만 연결해 같은 기획서의 v1.0.0/v1.1.0을
-- 구분하거나 최신본 하나를 고를 수 없었다. 원본 파일은 그대로 보존하고,
-- 논리 문서(document_key)별 버전·최신 포인터·변경 요약을 이 테이블에 둔다.

ALTER TABLE goal_documents
    ADD COLUMN IF NOT EXISTS document_key text,
    ADD COLUMN IF NOT EXISTS version text,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS is_latest boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS change_summary text,
    ADD COLUMN IF NOT EXISTS supersedes_id bigint REFERENCES goal_documents(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT NOW();

-- /v1.2.3/ 디렉터리를 쓰는 기존 정본 세트는 kind+파일명(PLAN.md 등)을
-- 같은 논리 문서 키로 묶는다. reference 문서가 여럿이어도 서로 합쳐지지
-- 않는다. 그 밖의 기존 링크는 경로 해시를 붙여 잘못 합치지 않는다.
UPDATE goal_documents
SET document_key = CASE
        WHEN doc_path ~ '/v[0-9]+\.[0-9]+\.[0-9]+/' THEN
            kind || ':' || lower(substring(
                doc_path FROM '/v[0-9]+\.[0-9]+\.[0-9]+/([^/]+)$'
            ))
        ELSE kind || ':' || substr(md5(doc_path), 1, 12)
    END
WHERE document_key IS NULL OR btrim(document_key) = '';

UPDATE goal_documents
SET version = COALESCE(
        substring(doc_path FROM '/v([0-9]+\.[0-9]+\.[0-9]+)/'),
        substring(COALESCE(title, '') FROM '[vV]([0-9]+\.[0-9]+\.[0-9]+)'),
        '1.0.0'
    )
WHERE version IS NULL OR btrim(version) = '';

ALTER TABLE goal_documents
    ALTER COLUMN document_key SET NOT NULL,
    ALTER COLUMN version SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'goal_documents_status_check'
    ) THEN
        ALTER TABLE goal_documents
            ADD CONSTRAINT goal_documents_status_check
            CHECK (status IN ('active', 'superseded', 'archived'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'goal_documents_version_check'
    ) THEN
        ALTER TABLE goal_documents
            ADD CONSTRAINT goal_documents_version_check
            CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'goal_documents_no_self_supersede'
    ) THEN
        ALTER TABLE goal_documents
            ADD CONSTRAINT goal_documents_no_self_supersede
            CHECK (supersedes_id IS NULL OR supersedes_id <> id);
    END IF;
END $$;

-- 컬럼을 처음 추가하면 같은 계보의 기존 행이 모두 is_latest=true다. 그런
-- 중복 계보만 정리한다. 마이그레이션을 재실행해도 이미 보관한 버전이나
-- 수동 승격 결과를 다시 활성화하지 않는다.
WITH duplicate_latest_groups AS (
    SELECT goal_id, document_key
    FROM goal_documents
    GROUP BY goal_id, document_key
    HAVING count(*) FILTER (WHERE is_latest) > 1
), parsed AS (
    SELECT d.id, d.goal_id, d.document_key,
           (regexp_match(d.version, '^([0-9]+)\.([0-9]+)\.([0-9]+)$')) AS parts
    FROM goal_documents d
    JOIN duplicate_latest_groups g
      ON g.goal_id = d.goal_id AND g.document_key = d.document_key
), ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY goal_id, document_key
               ORDER BY (parts[1])::integer DESC,
                        (parts[2])::integer DESC,
                        (parts[3])::integer DESC,
                        id DESC
           ) AS newest_rank,
           lag(id) OVER (
               PARTITION BY goal_id, document_key
               ORDER BY (parts[1])::integer,
                        (parts[2])::integer,
                        (parts[3])::integer,
                        id
           ) AS previous_id
    FROM parsed
)
UPDATE goal_documents d
SET is_latest = (r.newest_rank = 1),
    status = CASE WHEN r.newest_rank = 1 THEN 'active' ELSE 'superseded' END,
    supersedes_id = r.previous_id,
    updated_at = NOW()
FROM ranked r
WHERE r.id = d.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_documents_latest
    ON goal_documents (goal_id, document_key)
    WHERE is_latest;

CREATE INDEX IF NOT EXISTS idx_goal_documents_versions
    ON goal_documents (goal_id, document_key, created_at DESC);

COMMENT ON COLUMN goal_documents.document_key IS
  '목표 안에서 버전 계보를 묶는 안정 키. 같은 키의 is_latest=true 행은 하나뿐이다.';
COMMENT ON COLUMN goal_documents.version IS
  '정규화된 semantic version(X.Y.Z).';
COMMENT ON COLUMN goal_documents.is_latest IS
  '해당 goal_id/document_key 계보에서 현재 목표 화면에 기본 노출할 최신본.';
COMMENT ON COLUMN goal_documents.supersedes_id IS
  '바로 이전 버전 goal_documents.id. 원본 파일은 삭제하지 않는다.';
