-- 채팅 한 턴의 구간별 소요 시간.
--
-- 2026-09-14 대표님 질문: "채팅창이 너처럼 즉시 응답이 왜 안되지?"
-- 답하려고 로그를 뒤졌는데 **계측 자체가 없었다.** 어디서 시간을 쓰는지
-- 모르는 채로 추측만 할 수 있었다.
--
-- 실측해 보니 컨텍스트 조립이 2.2초였고 그중 대부분이 질문 임베딩
-- (CPU Ollama, 2.5초)이었다. 나머지는 릴레이 구간인데 그건 못 쟀다.
-- 못 재는 구간이 있으면 고칠 수도 없다.
CREATE TABLE IF NOT EXISTS chat_turn_timing (
    id            bigserial PRIMARY KEY,
    session_id    uuid NOT NULL,
    execution_id  uuid,
    model         text,
    intent        text,
    -- 구간별 밀리초. NULL 은 그 구간에 도달하지 못했다는 뜻이다.
    ctx_ms        integer,   -- 컨텍스트 조립 (Auto-RAG·임베딩·프롬프트 포함)
    rag_ms        integer,   -- 그중 Auto-RAG 만
    relay_ms      integer,   -- 요청을 보내고 첫 토큰이 오기까지
    first_token_ms integer,  -- 턴 시작부터 첫 토큰까지 (사용자가 체감하는 값)
    total_ms      integer,   -- 턴 전체
    prompt_chars  integer,
    history_count integer,
    tool_calls    integer,
    created_at    timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_turn_timing_recent
    ON chat_turn_timing (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_turn_timing_session
    ON chat_turn_timing (session_id, created_at DESC);

COMMENT ON TABLE chat_turn_timing IS
  '채팅 턴 구간별 소요. first_token_ms 가 사용자가 체감하는 값이다.';
