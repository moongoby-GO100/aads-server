-- 수집 데이터셋 저장소: 신규 표만 생성. 릴리스 승인 경로에서 적용.
BEGIN;
CREATE TABLE IF NOT EXISTS collection_datasets (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
 project_key text NOT NULL, dataset_key text NOT NULL, recipe_name text NOT NULL,
 field_schema jsonb NOT NULL, key_fields text[] NOT NULL,
 schedule_cron text, enabled boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,dataset_key)
);
CREATE TABLE IF NOT EXISTS collection_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), dataset_id uuid NOT NULL REFERENCES collection_datasets(id),
 recipe_run_id uuid, status text NOT NULL CHECK(status IN
 ('success','no_records','partial','extraction_failed','action_required','failed')),
 rows_seen integer NOT NULL DEFAULT 0, rows_new integer NOT NULL DEFAULT 0,
 required_fill_rate numeric, evidence_path text, error_code text,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS collection_records (
 dataset_id uuid NOT NULL REFERENCES collection_datasets(id), record_key text NOT NULL,
 fields jsonb NOT NULL, first_seen_run_id uuid REFERENCES collection_runs(id),
 last_seen_run_id uuid REFERENCES collection_runs(id),
 first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(dataset_id,record_key)
);
COMMIT;
