-- 094_pc_ollama_backend_correction.sql
-- Align local pc-* chat models with the in-process PC Agent Ollama backend.

BEGIN;

UPDATE llm_models
SET
    metadata = COALESCE(metadata, '{}'::jsonb)
        || jsonb_build_object(
            'execution_backend', 'pc_ollama',
            'routing_note', 'AADS chat routes directly to PC Agent, and PC Agent executes the model on CEO PC Ollama.'
        ),
    updated_at = NOW()
WHERE provider = 'litellm'
  AND model_id LIKE 'pc-%'
  AND (
      metadata->>'execution_backend' IS DISTINCT FROM 'pc_ollama'
      OR metadata->>'routing_note' LIKE 'LiteLLM forwards%'
  );

COMMIT;
