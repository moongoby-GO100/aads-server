-- Roll back only the additive Qwen3 contract fence; preserve collection data.
ALTER TABLE IF EXISTS doc_chunk_embeddings_qwen3
    DROP CONSTRAINT IF EXISTS doc_chunk_embeddings_qwen3_contract_ck;
