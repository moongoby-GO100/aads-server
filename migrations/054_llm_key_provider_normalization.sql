UPDATE llm_api_keys
SET provider = CASE provider
    WHEN 'alibaba' THEN 'qwen'
    WHEN 'dashscope' THEN 'qwen'
    WHEN 'google' THEN 'gemini'
    WHEN 'claude' THEN 'anthropic'
    WHEN 'moonshot' THEN 'kimi'
    ELSE provider
END,
updated_at = NOW()
WHERE provider IN ('alibaba', 'dashscope', 'google', 'claude', 'moonshot');
