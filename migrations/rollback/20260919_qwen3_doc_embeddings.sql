-- Explicit/manual rollback. This removes only the Qwen3 collection, never doc_chunks.
DROP TABLE IF EXISTS doc_chunk_embeddings_qwen3;

