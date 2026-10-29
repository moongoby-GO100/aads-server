-- 녹화 상태를 슬롯/프로세스 수명과 분리한다. 평문 비밀값은 recorder에서 거부한다.
CREATE TABLE IF NOT EXISTS work_recipe_recordings (
    id text PRIMARY KEY,
    tenant_id uuid NOT NULL,
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_recipe_recordings_tenant_idx
    ON work_recipe_recordings (tenant_id);
