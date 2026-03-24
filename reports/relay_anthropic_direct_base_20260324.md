# 릴레이 OAuth 시 Anthropic 직접 URL 강제

**날짜**: 2026-03-24  
**목적**: 호스트 `ANTHROPIC_BASE_URL`이 LiteLLM을 가리킬 때 CLI subprocess가 `no_db_connection` 400을 받는 재발 방지.

## 조치

- `scripts/claude_relay_server.py`: 요청 바디에 `oauth_token`이 있으면 `proc_env["ANTHROPIC_BASE_URL"]`을 `ANTHROPIC_API_DIRECT_URL`(기본 `https://api.anthropic.com`)로 설정.

## 운영

- `systemctl restart claude-relay` 로 릴레이 프로세스 재시작 후 반영.

## 선택 환경변수

- `ANTHROPIC_API_DIRECT_URL` — 비우지 않는 한 기본은 `https://api.anthropic.com`.
