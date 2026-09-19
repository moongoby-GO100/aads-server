-- Qwen3 document embeddings are deliberately isolated from legacy nomic vectors.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS doc_chunk_embeddings_qwen3 (
    id bigserial PRIMARY KEY,
    chunk_id uuid NOT NULL REFERENCES doc_chunks(id) ON DELETE CASCADE,
    model_id text NOT NULL DEFAULT 'qwen3-embedding:0.6b',
    dimension integer NOT NULL DEFAULT 1024 CHECK (dimension = 1024),
    instruction_version text NOT NULL,
    content_sha256 text NOT NULL CHECK (length(content_sha256) = 64),
    embedding vector(1024),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','processing','ready','error')),
    lease_owner text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    worker text,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model_id, instruction_version)
);

CREATE INDEX IF NOT EXISTS idx_qwen3_doc_embedding_hnsw
    ON doc_chunk_embeddings_qwen3 USING hnsw (embedding vector_cosine_ops)
    WHERE state = 'ready' AND embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_qwen3_doc_claim
    ON doc_chunk_embeddings_qwen3 (state, lease_expires_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_qwen3_doc_chunk
    ON doc_chunk_embeddings_qwen3 (chunk_id);

-- Seed only metadata. The hash identifies the exact 4,000-character payload sent
-- to Ollama (instruction included), not the full chunk. Therefore mutations past
-- the cutoff intentionally reuse the deterministic embedding. Legacy vectors are
-- never updated by this migration.
INSERT INTO doc_chunk_embeddings_qwen3
    (chunk_id, instruction_version, content_sha256)
SELECT id, 'qwen3-doc-v2-payload4000', encode(digest(left(
    'Represent this English document for retrieval: ' ||
    coalesce(title,'') || E'\n' || coalesce(heading,'') || E'\n' || coalesce(content,''),
    4000), 'sha256'), 'hex')
FROM doc_chunks
ON CONFLICT (chunk_id, model_id, instruction_version) DO NOTHING;
