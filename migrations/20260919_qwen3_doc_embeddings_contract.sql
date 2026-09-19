-- Additive contract fence for the isolated Qwen3 collection.
-- The original 20260919_qwen3_doc_embeddings.sql is immutable once recorded.
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
            );
    END IF;
END $$;
