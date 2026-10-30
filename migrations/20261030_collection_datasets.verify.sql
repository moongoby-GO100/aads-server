-- 수집 데이터셋 릴리스 검증 (읽기 전용)
SELECT table_name, column_name, data_type, column_default
FROM information_schema.columns
WHERE table_schema='public' AND table_name IN
 ('collection_datasets','collection_runs','collection_records')
ORDER BY table_name, ordinal_position;
SELECT conrelid::regclass, conname, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conrelid IN
 ('collection_datasets'::regclass,'collection_runs'::regclass,'collection_records'::regclass);
