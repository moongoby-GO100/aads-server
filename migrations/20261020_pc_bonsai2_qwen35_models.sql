-- PC 로컬모델 교체: pc-qwen38-27b 별칭은 bonsai-2:27b 로 재지정(브리지 _MODEL_MAP),
-- pc-bonsai2-27b / pc-qwen35-9b 를 신규 등록한다. DROP/DELETE 없음, 재실행 안전.
-- 신규 행은 pc-qwen38-27b-local 행을 복제해 provider/category/capabilities/metadata
-- 패턴을 그대로 따르고, 식별/표시/실행 모델 ID 만 덮어쓴다.
-- runner_review_advisory_config 는 건드리지 않는다(별칭 유지로 자동 전환).
BEGIN;

INSERT INTO llm_models
    (provider, model_id, display_name, family, category,
     supports_tools, supports_thinking, supports_vision, supports_coding,
     input_cost, output_cost, is_active, activation_source, linked_key_name,
     metadata, execution_model_id, discovery_source, verification_status,
     capabilities, pricing, is_selectable, is_executable)
SELECT src.provider, v.model_id, v.display_name, src.family, src.category,
       src.supports_tools, src.supports_thinking, src.supports_vision, src.supports_coding,
       src.input_cost, src.output_cost, src.is_active, src.activation_source, src.linked_key_name,
       jsonb_set(COALESCE(src.metadata, '{}'::jsonb), '{execution_model_id}', to_jsonb(v.exec_id::text)),
       v.exec_id, src.discovery_source, src.verification_status,
       src.capabilities, src.pricing, src.is_selectable, src.is_executable
FROM llm_models src
CROSS JOIN (VALUES
    ('pc-bonsai2-27b-local', 'PC Bonsai 2 27B (Local)', 'pc-bonsai2-27b'),
    ('pc-qwen35-9b-local',   'PC Qwen3.5 9B (Local, 빠름)', 'pc-qwen35-9b')
) AS v(model_id, display_name, exec_id)
WHERE src.model_id = 'pc-qwen38-27b-local'
ON CONFLICT (provider, model_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    execution_model_id = EXCLUDED.execution_model_id,
    metadata = EXCLUDED.metadata,
    updated_at = NOW();

UPDATE llm_models
SET display_name = 'PC Qwen3.8-27B → Bonsai 2 27B (Local)',
    updated_at = NOW()
WHERE model_id = 'pc-qwen38-27b-local';

COMMIT;
