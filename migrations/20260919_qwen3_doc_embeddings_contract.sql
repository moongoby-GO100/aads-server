-- Additive contract fence for the isolated Qwen3 collection.
-- The original 20260919_qwen3_doc_embeddings.sql is immutable once recorded.
--
-- Production contains historical qwen3-doc-v1 rows alongside v2 rows.  NOT VALID
-- preserves those rows while PostgreSQL enforces this v2 contract for every new
-- or changed row.  After the v1 backlog has been retired separately (without
-- changing this migration), validate with:
--   ALTER TABLE doc_chunk_embeddings_qwen3
--       VALIDATE CONSTRAINT doc_chunk_embeddings_qwen3_contract_ck;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'doc_chunk_embeddings_qwen3_contract_ck'
          AND conrelid = 'doc_chunk_embeddings_qwen3'::regclass
    ) THEN
        ALTER TABLE doc_chunk_embeddings_qwen3
            ADD CONSTRAINT doc_chunk_embeddings_qwen3_contract_ck CHECK (
                model_id = 'qwen3-embedding:0.6b'
                AND dimension = 1024
                AND instruction_version = 'qwen3-doc-v2-payload4000'
            ) NOT VALID;
    END IF;
END $$;
