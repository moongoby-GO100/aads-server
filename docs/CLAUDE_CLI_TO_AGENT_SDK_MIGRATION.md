# AADS Claude 호출 경로 전환 기술서 v2.0

> 최종 업데이트: 2026-03-17
> 버전: 2.0 (Agent SDK + Agent 팀 + Evolution Engine 복구)

## 1. 배경

- AADS 채팅 AI에서 Anthropic SDK/LiteLLM 경유 Claude 호출이 OAuth 토큰 인증 실패
- LiteLLM 프록시 "No connected db" 400 에러로 전체 백그라운드 시스템 중단
- Claude Code CLI는 같은 토큰으로 정상 작동 → Agent SDK로 전환하여 근본 해결

## 2. 변경 이력

### Phase 1: CLI Relay (중간 단계, 현재 비활성)
- `claude_relay_server.py` (호스트 port 8199) → `claude -p` subprocess
- MCP 브릿지 (`mcp_servers/aads_tools_bridge.py`) → 51개 도구 등록
- `model_selector.py`에서 `_stream_claude_cli()` 추가
- `--resume` 세션 유지 구현
- systemd 서비스 등록 후 비활성화 (Agent SDK로 대체)

### Phase 2: Agent SDK (최종 채택)
- Agent SDK가 내부적으로 번들 CLI를 subprocess로 실행 (동일 인증 경로)
- `model_selector.py`에서 `_stream_agent_sdk()` → 모든 Claude 인텐트 처리
- MCP 브릿지를 컨테이너 내부에서 직접 실행 (`python -m mcp_servers.aads_tools_bridge`)
- CLI relay 서버 불필요

### Phase 3: 토큰 자동 교대 + Agent 팀
- OAuth 토큰 2개 (Naver 우선 → Gmail 폴백) 3라운드 교대 (총 6회)
- Agent 팀 활성화: researcher/developer/qa 서브에이전트 3개
- 채팅 입력창 키 토글 버튼 (🟢Naver / 🔵Gmail)

### Phase 4: 대화 컨텍스트 + Evolution Engine 복구
- `_format_messages_as_text(has_resume)`: resume 없으면 최근 40건 대화 포함
- `anthropic_client.py`: LiteLLM → OAuth 직접 (백그라운드 8개 시스템 복구)
- `call_llm_with_fallback()`: Claude(Naver→Gmail) → Gemini 3.1 Flash Lite 폴백
- 품질 평가에 `context_awareness` (맥락 파악) 25% 가중치 추가
- 스트리밍 중간저장 1초 간격 + 변경감지 최적화

## 3. 최종 아키텍처

```
브라우저 → AADS 백엔드 (Docker :8100)
    → chat_service.py
        ├── 시스템 프롬프트 빌드 (Layer 1~5 메모리)
        ├── 대화 히스토리 로드 (DB에서 500건)
        └── model_selector.call_stream()
              ├── Claude → _stream_agent_sdk()
              │     ├── 토큰 교대: Naver → Gmail → Naver → Gmail (3라운드)
              │     ├── Agent SDK → 번들 CLI → Anthropic API (OAuth)
              │     ├── MCP 브릿지 → ToolExecutor (51개 도구)
              │     ├── Agent 팀: researcher/developer/qa (Sonnet)
              │     └── 세션 이어가기 (--resume, _cli_session_map)
              └── Gemini → _stream_litellm() (폴백)
    → 후처리 (chat_service.py Phase C):
        ├── DB 저장 + streaming_placeholder (1초 간격)
        ├── F2: Fact Extraction (핵심사실 추출)
        ├── F8: CEO Pattern Tracking
        ├── F11: Self Evaluation (context_awareness 포함)
        ├── Output Validator (거짓보고 차단)
        ├── Semantic Cache
        └── Evolution Engine (B1 반성, B2 교정학습...)
```

## 4. 인증 구조

```
docker-compose.yml:
  ANTHROPIC_API_KEY=${ANTHROPIC_AUTH_TOKEN_2}      # Naver OAuth (1순위)
  ANTHROPIC_API_KEY_FALLBACK=${ANTHROPIC_AUTH_TOKEN} # Gmail OAuth (2순위)
  ANTHROPIC_BASE_URL=https://api.anthropic.com      # 직접 호출 (LiteLLM 우회)
```

### 채팅 AI (Agent SDK 경유)
```
_stream_agent_sdk() → ClaudeAgentOptions(env={"ANTHROPIC_API_KEY": key})
  → 번들 CLI가 OAuth 토큰으로 직접 인증
  → 3라운드 교대: Naver(1) → Gmail(2) → Naver(3) → Gmail(4) → Naver(5) → Gmail(6)
  → 전부 실패 시 Gemini 3 Flash Preview 폴백
```

### 백그라운드 시스템 (Anthropic SDK 직접)
```
anthropic_client.get_client() → AsyncAnthropic(api_key=Naver, base_url=api.anthropic.com)
call_llm_with_fallback() → Claude Naver → Claude Gmail → Gemini 3.1 Flash Lite
```

### 키 변경 시 필수 체크리스트
1. 수정 전: `docker exec aads-server env | grep ANTHROPIC`
2. 수정 후: `docker compose up -d --force-recreate aads-server`
3. recreate 후: `docker exec aads-server env | grep ANTHROPIC` 확인
4. Agent SDK 테스트: `say ok` 통과
5. 채팅 API 테스트: curl 메시지 전송 확인

## 5. 토큰 자동 교대

```python
# model_selector.py
_ANTHROPIC_KEYS = [Naver, Gmail]  # 런타임 변경 가능 (키 토글 버튼)

# 3라운드 × 2키 = 총 6회, 지수 백오프
Round 1: Naver → Gmail
  (1초 대기)
Round 2: Naver → Gmail
  (2초 대기)
Round 3: Naver → Gmail
  → 전부 실패 시 Gemini 폴백
```

### 키 순서 변경 API
```
GET  /api/v1/settings/auth-keys  → 현재 키 순서 조회
POST /api/v1/settings/auth-keys  → {"primary": "naver"|"gmail"}
```
- 서버 메모리에서 즉시 변경 (전 세션 반영)
- 서버 재시작 시 docker-compose 기본값 복귀 (Naver 우선)

## 6. Agent 팀

```python
agents = {
    "researcher": AgentDefinition(
        description="코드/DB/로그 조사",
        prompt="시스템 조사 전문가...",
        model="sonnet",
    ),
    "developer": AgentDefinition(
        description="코드 수정/배포",
        prompt="풀스택 개발자...",
    ),
    "qa": AgentDefinition(
        description="테스트/검증",
        prompt="QA 엔지니어...",
        model="sonnet",
    ),
}
```
- Claude가 작업 복잡도 판단 → 자동으로 서브에이전트 호출
- 각 서브에이전트 독립 컨텍스트 (토큰 절약)
- MCP 도구 전체 상속 (SSH 원격 작업 포함)

## 7. 대화 컨텍스트 유지

```python
_format_messages_as_text(messages, has_resume):
  has_resume=True:  최신 user 메시지만 (CLI가 기억)
  has_resume=False: 최근 40건 대화 직렬화 (서버 재시작 후 폴백)

_cli_session_map: {aads_session_id: cli_session_id}  # 메모리, 재시작 시 초기화
```

## 8. 품질 평가 (self_evaluator.py)

| 항목 | 가중치 | 설명 |
|------|--------|------|
| **context_awareness** | **25%** | 이전 대화 맥락 이해 (이전 6건 참조) |
| accuracy | 25% | 사실적 정확성 |
| completeness | 15% | 응답 완성도 |
| tool_grounding | 15% | 도구로 검증했는가 |
| relevance | 10% | 질문과의 관련성 |
| actionability | 10% | 다음 단계 제시 |

- 최소 응답 길이: 1자 (모든 응답 평가)
- quality < 0.4 → B1 반성(reflexion) 트리거

## 9. 스트리밍 중간저장

- 1초 간격으로 `streaming_placeholder` DB 저장
- 변경 없으면 스킵 (content 길이 + tool count 기반 감지)
- 서버 재시작 시 `resume_interrupted_streams()`가 자동 복구

## 10. 변경된 파일 목록

| 파일 | 변경 내용 |
|------|----------|
| `app/services/model_selector.py` | _stream_agent_sdk, 토큰 교대, Agent 팀, 세션 resume, 대화 컨텍스트 |
| `app/core/anthropic_client.py` | LiteLLM→OAuth 직접, call_llm_with_fallback, Gemini 폴백 |
| `app/services/self_evaluator.py` | context_awareness 추가, 최소 1자, prev_messages |
| `app/services/chat_service.py` | 중간저장 1초, prev_messages 전달 |
| `app/services/tool_registry.py` | TOOL_CATEGORY_GUIDE에 Agent 팀 섹션 |
| `app/routers/chat.py` | /settings/auth-keys API |
| `mcp_servers/aads_tools_bridge.py` | MCP stdio 브릿지, ToolExecutor 경유 51개 도구 |
| `scripts/claude_relay_server.py` | CLI relay (Phase 1 백업, 비활성) |
| `scripts/mcp_config_template.json` | MCP config 템플릿 |
| `scripts/update_claude_all_servers.sh` | 전 서버 Claude Code CLI + Codex CLI + SDK 자동 업데이트 크론 |
| `docker-compose.yml` | ANTHROPIC_API_KEY, BASE_URL, FALLBACK, CLI_ENABLED |
| `frontend: page.tsx` | 키 토글 버튼 (🟢Naver/🔵Gmail) |
| `frontend: chatApi.ts` | authKeyApi 추가 |

## 11. 서버 환경

| 서버 | CLI | Agent SDK | Python | 역할 |
|------|-----|-----------|--------|------|
| 68 호스트 | 2.1.77 | 0.1.48 | 3.11 | AADS Docker 호스트 |
| 68 Docker | 2.1.71 (번들) | 0.1.48 | 3.12 | 채팅 AI 실행 |
| 211 | 2.1.77 | 0.1.48 | 3.11 | KIS/GO100 |
| 114 | 2.1.77 | 0.1.48 | 3.11 | SF/NTV2 |

자동 업데이트: 매일 04:00 KST (`/root/aads/aads-server/scripts/update_claude_all_servers.sh`)

## 12. 폴백 체인

```
채팅 AI: Agent SDK(Naver) → Agent SDK(Gmail) × 3라운드 → Gemini Flash (LiteLLM)
백그라운드: Claude Haiku(Naver) → Claude Haiku(Gmail) → Gemini 3.1 Flash Lite (직접 API)
```
