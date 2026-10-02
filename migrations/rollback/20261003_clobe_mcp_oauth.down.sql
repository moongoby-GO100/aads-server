-- Down migration for migrations/20261003_clobe_mcp_oauth.sql (연결 토큰·도구 기록 삭제)
DROP TABLE IF EXISTS clobe_mcp_tools;
DROP TABLE IF EXISTS clobe_mcp_connection;
DROP TABLE IF EXISTS clobe_mcp_oauth_state;
DROP TABLE IF EXISTS clobe_mcp_oauth_client;
