# CLI Relay / Agent SDK — AUTH_TOKEN ↔ AUTH_TOKEN_2 자동 전환

**날짜**: 2026-03-24  
**요약**: 채팅 경로에서 CLI Relay·Agent SDK가 호스트/번들 자격 1벌만 쓰던 문제를 해소. 컨테이너의 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN_2` 순서(`_ANTHROPIC_KEYS`, `set_key_order`)를 릴레이·SDK subprocess env에 주입하고, 429/401/529 등에서 보조 토큰으로 재시도.

## 변경 파일

| 파일 | 내용 |
|------|------|
| `scripts/claude_relay_server.py` | POST 바디 `oauth_token`, `ignore_cli_resume`. subprocess `env`에 토큰·레거시명(`ANTHROPIC_`+`API_KEY`)·`CLAUDE_CODE_OAUTH_TOKEN` 설정 |
| `app/services/model_selector.py` | `_CLAUDE_OAUTH_RETRY_PATTERNS`, `_cli_oauth_error_retryable`, `_stream_cli_relay` 다중 토큰 루프·릴레이 `DELETE /sessions/{id}`·`ignore_cli_resume`; `_stream_agent_sdk` 토큰별 3회 재시도 후 다음 토큰; `_run_agent_sdk_with_key`에 `oauth_token`, `use_cli_resume`, `ClaudeAgentOptions.env` |

## 동작 정리

- **CLI Relay**: 매 요청에 `oauth_token` 포함. 첫 토큰 실패 시(HTTP 401/402/429/503/529 또는 NDJSON `result.is_error` + 재시도 가능 패턴) **사용자에게 delta/tool이 나가기 전**이면 두 번째 토큰으로 재POST. 전환 전 호스트 매핑 제거(`DELETE`) + `ignore_cli_resume`.
- **Agent SDK**: `ClaudeAgentOptions.env`로 동일 토큰 주입. 첫 토큰만 `use_cli_resume=True`; 이후 토큰은 새 CLI 세션.

## 검증

- `python3.11 -m py_compile` — `model_selector.py`, `claude_relay_server.py` PASS

## 적용·배포

- **소스**: 워크스페이스 반영 완료
- **배포**: `aads-server` 이미지/컨테이너 재배포 후 반영. **호스트** `claude-relay` 서비스는 `claude_relay_server.py` 갱신 후 **재시작** 필요

## 후속 체크

- [ ] 장외/장중 CEO 채팅: Naver 한도 시 Gmail(또는 역순 설정 시)으로 자동 전환되는지 로그(`cli_relay_oauth_switch`, `agent_sdk_oauth_switch`) 확인
- [ ] 릴레이만 구버전일 때: 바디의 `oauth_token` 무시 → 구동은 되나 이중 자격 미적용 가능
