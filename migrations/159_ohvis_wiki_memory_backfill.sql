-- 159: OHVIS LLM Wiki first-pass memory_facts backfill.
-- Date: 2026-09-07
--
-- This is intentionally bounded and idempotent. It promotes high-value,
-- active memory facts into wiki pages so the new OHVIS wiki/search surface
-- can use existing knowledge immediately without copying the full memory
-- table during deployment.

INSERT INTO ohvis_wiki_pages
    (project, slug, title, summary, body, source_ids, confidence, metadata, created_at, updated_at)
SELECT
    ranked.project,
    'memory-fact-' || ranked.id::text AS slug,
    LEFT(COALESCE(NULLIF(ranked.subject, ''), ranked.category || ' fact'), 240) AS title,
    LEFT(COALESCE(NULLIF(ranked.context_snippet, ''), ranked.detail, ''), 800) AS summary,
    CONCAT_WS(
        E'\n\n',
        '# ' || COALESCE(NULLIF(ranked.subject, ''), ranked.category || ' fact'),
        'Project: ' || COALESCE(ranked.project, 'UNKNOWN'),
        'Category: ' || COALESCE(ranked.category, 'unknown'),
        COALESCE(NULLIF(ranked.detail, ''), ranked.context_snippet, '')
    ) AS body,
    ARRAY[]::UUID[] AS source_ids,
    COALESCE(ranked.confidence, 0.5) AS confidence,
    jsonb_build_object(
        'backfill_source', 'memory_facts',
        'memory_fact_id', ranked.id::text,
        'category', ranked.category,
        'referenced_count', ranked.referenced_count,
        'migration', '159_ohvis_wiki_memory_backfill'
    ) AS metadata,
    COALESCE(ranked.created_at, NOW()),
    NOW()
FROM (
    SELECT
        mf.*,
        ROW_NUMBER() OVER (
            PARTITION BY mf.project
            ORDER BY
                COALESCE(mf.confidence, 0) DESC,
                COALESCE(mf.referenced_count, 0) DESC,
                COALESCE(mf.updated_at, mf.created_at) DESC
        ) AS project_rank
    FROM memory_facts mf
    WHERE mf.superseded_by IS NULL
      AND mf.project IN ('AADS', 'CEO', 'GO100', 'KIS', 'NAS', 'NTV2', 'SF')
      AND mf.category IN (
          'api_contract',
          'architecture_decision',
          'ceo_instruction',
          'config_change',
          'data_model_change',
          'decision',
          'error_pattern',
          'error_resolution',
          'feature_change',
          'project_insight',
          'project_snapshot',
          'timeline_event'
      )
      AND COALESCE(mf.confidence, 0) >= 0.7
) ranked
WHERE ranked.project_rank <= 300
ON CONFLICT (project, slug) DO UPDATE
SET title = EXCLUDED.title,
    summary = EXCLUDED.summary,
    body = EXCLUDED.body,
    confidence = EXCLUDED.confidence,
    metadata = ohvis_wiki_pages.metadata || EXCLUDED.metadata,
    updated_at = NOW();
