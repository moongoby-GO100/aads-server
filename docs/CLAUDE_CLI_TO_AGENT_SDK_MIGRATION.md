# AADS Claude 호출 경로 전환: LiteLLM/SDK → Agent SDK

## 배경
- AADS 채팅 AI에서 Anthropic SDK/LiteLLM 경유 Claude 호출이 OAuth 토큰 인증 실패
- Claude Code CLI는 같은 토큰으로 정상 작동
- CLI subprocess → Agent SDK 순으로 전환하여 근본 해결

## 변경 이력

### Phase 1: CLI Relay (중간 단계)
- claude_relay_server.py (호스트 port 8199) → claude -p subprocess
- MCP 브릿지 (mcp_servers/aads_tools_bridge.py) → 51개 도구 등록
- model_selector.py에서 _stream_claude_cli() 추가
- --resume 세션 유지 구현

### Phase 2: Agent SDK (최종)
- Agent SDK가 내부적으로 번들 CLI를 subprocess로 실행 (동일 인증 경로)
- model_selector.py에서 _stream_agent_sdk()로 교체
- MCP 브릿지를 컨테이너 내부에서 직접 실행 (docker exec 불필요)
- CLI relay 서버 불필요

## 최종 아키텍처

```
브라우저 → AADS 백엔드 (Docker :8100)
    → chat_service.py → model_selector.py
    → _stream_agent_sdk()
    → Agent SDK (claude_agent_sdk)
    → 번들 CLI (/usr/local/lib/python3.12/.../claude)
    → Anthropic API (OAuth 직접 인증)
           ↓
    MCP 브릿지 (python -m mcp_servers.aads_tools_bridge)
           ↓
    execute_tool() (51개 도구)
```

## 인증 구조

```
docker-compose.yml:
  ANTHROPIC_API_KEY=${ANTHROPIC_AUTH_TOKEN}    # OAuth 토큰
  ANTHROPIC_BASE_URL=https://api.anthropic.com  # 직접 호출
```

- LiteLLM 프록시 우회 (LiteLLM의 "No connected db" 400 에러 회피)
- OAuth 토큰(sk-ant-oat01-...)을 ANTHROPIC_API_KEY로 전달
- 번들 CLI가 자동으로 OAuth 인증 처리

## 변경된 파일

| 파일 | 변경 내용 |
|------|----------|
| `app/services/model_selector.py` | _stream_agent_sdk() 추가, Claude 경로를 Agent SDK로 전환 |
| `mcp_servers/aads_tools_bridge.py` | 신규: MCP stdio 브릿지, 51개 도구 동적 등록 |
| `scripts/claude_relay_server.py` | 신규: CLI relay (Phase 1, 현재 백업용) |
| `scripts/mcp_config_template.json` | 신규: MCP config 템플릿 |
| `docker-compose.yml` | ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL 추가 |

## 복구된 기능

| 기능 | 이전 (LiteLLM) | CLI Relay | Agent SDK |
|------|----------------|-----------|-----------|
| 인증 | 실패 | 정상 | 정상 |
| Extended Thinking | 지원 | 미지원 | 지원 |
| 도구 51개 | 지원 | 지원 | 지원 |
| 메모리 시스템 | 지원 | 지원 | 지원 |
| CEO 인터럽트 | 지원 | 미지원 | SDK client.interrupt() |
| 세션 유지 | SDK 관리 | --resume | SDK 관리 |
| Prompt Caching | 지원 | CLI 관리 | CLI 관리 |

## 폴백 경로

Claude 장애 시 자동 Gemini 전환:
```python
if _had_error:
    yield {"type": "delta", "content": "[Claude 장애 → Gemini 전환]\n\n"}
    async for event in _stream_litellm("gemini-3-flash-preview", ...):
        yield event
```

## 환경 설정

### docker-compose.yml
```yaml
- ANTHROPIC_API_KEY=${ANTHROPIC_AUTH_TOKEN}
- ANTHROPIC_BASE_URL=https://api.anthropic.com
- CLAUDE_CLI_ENABLED=${CLAUDE_CLI_ENABLED:-true}
```

### systemd (Phase 1 백업, 현재 비활성화 가능)
```
/etc/systemd/system/claude-relay.service
```

## 날짜
2026-03-17
