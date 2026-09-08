-- 164: LLMOps ledger 스키마 정합화 (163 선행 적용분 교정).
-- Date: 2026-09-08
-- Task: AADS-LANGSMITH-INTERNAL-LLMOPS-P0 (검수 피드백 반영)
--
-- 배경: 163이 여러 변형본으로 작성됐고, 그중 자식 테이블이 llmops_traces(id)
-- UUID를 참조하는 변형본이 운영 DB에 먼저 적용됐다. 정본
-- `migrations/163_ohvis_internal_llmops_foundation.sql`은 trace_id TEXT 계약이며
-- `app/services/llmops_store.py`가 이 계약으로 읽고 쓴다.
--
-- 163은 CREATE TABLE IF NOT EXISTS라서 이미 만들어진 테이블의 모양을 고치지
-- 못한다. 그래서 교정은 이 파일이 맡는다.
-- - ADD COLUMN / ALTER TYPE / DROP CONSTRAINT 만 사용한다. 행을 지우지 않는다.
-- - 정본 163만 적용된 DB에서는 모든 블록이 조건에 걸려 no-op이다.
-- - 변형본이 남긴 여분 컬럼(trace_key 등 정본에도 있는 것 제외)은 전부 기본값이
--   있어 무해하므로 그대로 둔다. 삭제는 되돌릴 수 없다.

-- ── 1. llmops_traces.trace_id (정본의 1급 키) ──────────────────────────────
ALTER TABLE llmops_traces ADD COLUMN IF NOT EXISTS trace_id TEXT;

-- 변형본에서 넘어온 행에 결정론적 값을 채운다 (id 기반이라 재실행해도 동일).
UPDATE llmops_traces
   SET trace_id = id::TEXT
 WHERE trace_id IS NULL;

DO $$
BEGIN
    -- 정본 163은 컬럼 UNIQUE 제약으로 같은 보장을 이미 걸어 둔다.
    IF NOT EXISTS (
        SELECT 1
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
        WHERE c.relname = 'llmops_traces'
          AND i.indisunique
          AND a.attname = 'trace_id'
    ) THEN
        -- DO 가드는 163의 컬럼 UNIQUE 제약(다른 이름)을 감지하기 위한 것이고,
        -- IF NOT EXISTS는 같은 이름으로의 재생성을 막는다. 둘 다 필요하다.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_traces_trace_id
            ON llmops_traces (trace_id);
    END IF;
END
$$;

ALTER TABLE llmops_traces ALTER COLUMN trace_id SET NOT NULL;

-- 163이 선언한 제약. 변형본과 무관하게 동일하므로 재실행해도 no-op이다.
ALTER TABLE llmops_traces ALTER COLUMN graph_run_id SET NOT NULL;

-- ── 2. 자식 테이블의 trace 참조를 TEXT 계약으로 ────────────────────────────
-- 변형본은 llmops_traces(id) UUID를 참조했다. 정본은 trace_id TEXT를 쓰며,
-- legacy(ohvis_harness_traces) trace도 같은 컬럼으로 가리킬 수 있어야 해서 FK를
-- 걸지 않는다. FK를 먼저 떼고 타입을 바꾼다.
DO $$
DECLARE
    target RECORD;
    fk_name TEXT;
BEGIN
    FOR target IN
        SELECT * FROM (VALUES
            ('llmops_spans',      'trace_id'),
            ('llmops_tool_calls', 'trace_id'),
            ('llmops_feedback',   'trace_id'),
            ('llmops_examples',   'source_trace_id'),
            ('llmops_scores',     'source_trace_id')
        ) AS v(table_name, column_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = target.table_name
              AND column_name = target.column_name
              AND data_type = 'uuid'
        ) THEN
            CONTINUE;  -- 이미 정본 형태(TEXT)
        END IF;

        FOR fk_name IN
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.table_schema = tc.table_schema
            WHERE tc.table_schema = 'public'
              AND tc.table_name = target.table_name
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = target.column_name
        LOOP
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', target.table_name, fk_name);
        END LOOP;

        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN %I TYPE TEXT USING %I::TEXT',
            target.table_name, target.column_name, target.column_name
        );
    END LOOP;
END
$$;

-- ── 3. 정본이 ON CONFLICT 추론에 쓰는 인덱스 ───────────────────────────────
-- llmops_store.promote_trace_to_dataset의
-- `ON CONFLICT (dataset_id, source_ref) WHERE source_ref IS NOT NULL`이
-- 이 인덱스를 필요로 한다. 163이 만들지만 변형본 DB에는 없다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llmops_examples_dataset_source
    ON llmops_examples (dataset_id, source_ref) WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llmops_spans_trace
    ON llmops_spans (trace_id, sequence, started_at);

CREATE INDEX IF NOT EXISTS idx_llmops_tool_calls_trace
    ON llmops_tool_calls (trace_id, sequence);

CREATE INDEX IF NOT EXISTS idx_llmops_feedback_trace
    ON llmops_feedback (trace_id, created_at DESC);
