# PRD: AADS 폴백 설정 통합 관리 시스템

**Task ID**: AADS-LLM-FALLBACK-OPS
**Version**: v1.0
**작성일**: 2026-09-11 KST
**우선순위**: P1
**상태**: 승인 → Phase 1 진행 중

---

## 1. 배경 및 목적

### 문제
- 폴백 체인이 4개 Python 파일에 하드코딩되어 있어 변경 시 코드 수정 + 배포 필요
- LiteLLM 경유 유료 모델(qwen-turbo, gpt-5.2 등)이 자동 폴백되어 예기치 않은 과금 발생
- CEO가 폴백 순서를 변경하려면 개발자에게 요청해야 하는 구조

### 목표
1. LiteLLM 경유 유료 모델 폴백 즉시 비활성화 (무료 Groq만 유지)
2. 대체 폴백을 Claude CLI / Codex CLI 모델로 전환
3. 모든 폴백을 DB 기반으로 전환, Ops 설정페이지에서 관리

---

## 2. 현재 상태 — 하드코딩 폴백 전수 조사

### 2.1 하드코딩 위치 목록

| # | 파일 | 변수/함수 | 하드코딩 내용 | LiteLLM 유료 |
|---|------|----------|-------------|-------------|
| 1 | `app/core/anthropic_client.py` L37-43 | `_BG_FALLBACK_MODELS` | `groq-llama-70b, groq-gpt-oss-120b` (env 기본값) | Groq=무료 ✅ |
| 2 | `app/core/anthropic_client.py` L208-407 | `call_llm_with_fallback()` | BYOK→OAuth1→OAuth2→qwen(비활성)→LiteLLM chain | qwen 유료(비활성), LiteLLM chain |
| 3 | `app/services/model_router.py` L31-57 | `INTENT_MODEL_MAP` | 인텐트별 모델 dict (qwen-turbo 6곳) | qwen-turbo=DashScope 유료 |
| 4 | `app/services/model_router.py` L153-220 | `AGENT_MODELS` | 에이전트 역할별 primary→fallback→error | OpenAI gpt-5.2/5.6=유료 |
| 5 | `app/services/model_selector.py` L122 | `_anthropic` client | LiteLLM 프록시 경유 클라이언트 | LiteLLM 프록시 |

### 2.2 즉시 비활성화 대상 (LiteLLM 유료)

| 대상 | 파일:라인 | 발동 조건 | 조치 |
|------|----------|----------|------|
| qwen3-235b (DashScope) | `anthropic_client.py:390` | Claude 전슬롯 실패 | 이미 비활성 (확인) |
| qwen-turbo (INTENT_MODEL_MAP) | `model_router.py:32-57` | casual/search/url_analyze 등 6인텐트 | claude-haiku로 대체 |
| gpt-5.2-chat-latest (에이전트 폴백) | `model_router.py:168-176` | PM/Developer primary 실패 | codex gpt-5.6-sol로 대체 |
| gpt-5.6-sol (에이전트 폴백) | `model_router.py:186-188` | Judge primary 실패 | claude-haiku로 대체 |
| gpt-5.6-luna (에이전트 폴백) | `model_router.py:198-206` | Researcher/Strategist 실패 | claude-haiku로 대체 |

### 2.3 유지 대상 (무료)

| 모델 | 경로 | 비용 |
|------|------|------|
| groq-llama-70b | LiteLLM→Groq | $0 |
| groq-gpt-oss-120b | LiteLLM→Groq | $0 |
| groq-qwen3-32b | LiteLLM→Groq | $0 |
| groq-kimi-k2 | LiteLLM→Groq | $0 |

---

## 3. 목표 아키텍처

### 3.1 핵심 원칙

1. **DB 단일 소스**: 모든 폴백 체인은 `model_routing_preferences` 테이블에서 읽기
2. **코드 Zero-Config**: Python 코드에 모델명/폴백 순서 하드코딩 없음
3. **Ops UI 관리**: `/admin/model-routing` 페이지에서 route_key별 폴백 관리
4. **핫 리로드**: 캐시 TTL 30초, 수동 무효화 API 제공
5. **Claude CLI / Codex CLI 우선**: 유료 LiteLLM 폴백을 CLI 모델로 대체

### 3.2 폴백 체인 재설계

#### A. 채팅 LLM 폴백 (route_key=`llm`)

| 순위 | 대상 | 라벨 | 발동 조건 |
|------|------|------|----------|
| 1 | Claude CLI Relay | 선택된 모델 (Opus/Sonnet/Fable) | 정상 |
| 2 | Claude CLI Relay | 같은 등급 대체 슬롯 (OAuth 교대) | 1순위 429/한도 |
| 3 | Codex CLI Relay | gpt-5.6-sol / gpt-6-astra | Claude 전체 불가 |
| 4 | Groq (무료) | groq-llama-70b → groq-gpt-oss-120b | CLI 전체 불가 |

#### B. 배경 LLM 폴백 (route_key=`background_llm`)

| 순위 | 모델 | 경로 | 비용 |
|------|------|------|------|
| 1 | claude-haiku-4-5 | OAuth 직접 | 시스템 토큰 |
| 2 | claude-sonnet-5 | Claude CLI Relay | CLI 토큰 |
| 3 | groq-llama-70b | LiteLLM→Groq | $0 무료 |
| 4 | groq-gpt-oss-120b | LiteLLM→Groq | $0 무료 |

#### C. 에이전트 역할별 폴백 (route_key=`agent_{role}`)

| 역할 | Primary | Fallback | Error |
|------|---------|----------|-------|
| supervisor | claude-opus-4-6 | claude-sonnet-4-6 | claude-haiku-4-5 |
| architect | claude-opus-4-6 | claude-sonnet-4-6 | claude-haiku-4-5 |
| pm | claude-sonnet-4-6 | codex:gpt-5.6-sol | claude-haiku-4-5 |
| developer | claude-sonnet-4-6 | codex:gpt-5.6-sol | claude-haiku-4-5 |
| qa | claude-sonnet-4-6 | claude-haiku-4-5 | claude-haiku-4-5 |
| judge | claude-sonnet-4-6 | codex:gpt-5.6-sol | claude-haiku-4-5 |
| devops | codex:gpt-5-mini | claude-haiku-4-5 | claude-sonnet-4-6 |
| researcher | claude-haiku-4-5 | codex:gpt-5.6-luna | claude-sonnet-4-6 |
| planner | claude-sonnet-4-6 | claude-haiku-4-5 | claude-haiku-4-5 |

#### D. 인텐트 라우팅 — qwen-turbo 대체

| 인텐트 | Before | After |
|--------|--------|-------|
| casual | claude-haiku | claude-haiku (유지) |
| search | qwen-turbo | claude-haiku |
| url_analyze | qwen-turbo | claude-haiku |
| memory_recall | qwen-turbo | claude-haiku |
| dashboard | qwen-turbo | claude-haiku |
| research | qwen-turbo | claude-haiku |
| health_check | qwen-turbo | claude-haiku |

---

## 4. 구현 설계

### 4.1 DB 스키마

기존 `model_routing_preferences` 테이블 그대로 사용 — 새 route_key 추가만:

```
신규 route_key:
- agent_supervisor, agent_architect, agent_pm, agent_developer
- agent_qa, agent_judge, agent_devops, agent_researcher, agent_planner
- intent_casual, intent_search, intent_deep_research, intent_url_analyze
- intent_video_analyze, intent_image_analyze, intent_planning
- intent_decision, intent_code_exec, intent_directive_gen
- intent_memory_recall, intent_dashboard, intent_diagnosis
- intent_research, intent_execute, intent_browser
- intent_strategy, intent_qa, intent_design, intent_health_check
```

### 4.2 신규 모듈: `app/services/fallback_chain_loader.py`

```python
"""DB 기반 폴백 체인 로더 — 30초 캐시, 핫 리로드."""

@dataclass
class FallbackEntry:
    provider: str
    model_id: str
    display_order: int
    is_enabled: bool
    is_default: bool
    notes: str

async def get_fallback_chain(route_key: str) -> list[FallbackEntry]:
    """model_routing_preferences에서 route_key의 활성 모델을 display_order 순 반환.
    30초 메모리 캐시. invalidate_fallback_cache()로 즉시 무효화."""

def invalidate_fallback_cache() -> None:
    """PUT API 호출 시 캐시 즉시 제거."""
```

### 4.3 Backend API

| Method | Path | 기능 |
|--------|------|------|
| GET | `/api/v1/ops/fallback-chains` | 전체 폴백 체인 조회 |
| PUT | `/api/v1/ops/fallback-chains/{route_key}` | 체인 순서/활성화 변경 |
| POST | `/api/v1/ops/fallback-chains/{route_key}/test` | 연결 테스트 |
| POST | `/api/v1/ops/fallback-chains/invalidate` | 캐시 즉시 무효화 |

### 4.4 코드 리팩터링 대상

| 파일 | Before | After |
|------|--------|-------|
| `anthropic_client.py` | `_BG_FALLBACK_MODELS` 하드코딩 | `get_fallback_chain("background_llm")` |
| `model_router.py` | `INTENT_MODEL_MAP` dict | `get_fallback_chain(f"intent_{intent}")` |
| `model_router.py` | `AGENT_MODELS` dict | `get_fallback_chain(f"agent_{role}")` |

### 4.5 Frontend (대시보드)

기존 `/admin/model-routing` 페이지 확장:
1. **"폴백 설정" 탭** — route_key 그룹별 드래그 앤 드롭 순서 변경
2. **활성/비활성 토글** — 각 폴백 모델 on/off
3. **연결 테스트 버튼** — ping 후 결과 표시
4. **비용 뱃지** — 무료($0) / 유료($X.XX) 표시
5. **그룹 분류**: 채팅 LLM / 배경 LLM / 에이전트 역할별 / 인텐트별

---

## 5. 마이그레이션 계획

### Phase 1: 즉시 조치 (P0, 당일) ← 현재 진행
1. `model_router.py`: INTENT_MODEL_MAP의 qwen-turbo → claude-haiku 대체
2. `model_router.py`: AGENT_MODELS 폴백에서 OpenAI 유료 → Codex CLI 대체
3. DB `model_routing_preferences`: background_llm route의 qwen-turbo is_enabled=false 확인
4. 배포 및 검증

### Phase 2: DB 이관 (P1, 1~2일)
1. `fallback_chain_loader.py` 모듈 구현
2. 하드코딩 4곳 → DB 조회로 교체
3. DB 시드 데이터 INSERT
4. API 엔드포인트 4개 구현
5. 단위 테스트

### Phase 3: Ops UI (P2, 2~3일)
1. `/admin/model-routing` 페이지에 폴백 관리 탭 추가
2. 드래그 순서 변경, 활성 토글, 연결 테스트 UI
3. 변경 이력 로그
4. E2E 검증

---

## 6. 검증 기준

| # | 대상 | 확인 내용 |
|---|------|----------|
| 1 | 코드 하드코딩 | grep "qwen-turbo\|INTENT_MODEL_MAP\|AGENT_MODELS\|_BG_FALLBACK" 0건 |
| 2 | DB 단일 소스 | 모든 폴백이 model_routing_preferences SELECT로 조회 |
| 3 | Ops UI | /admin/model-routing에서 순서 변경 → 30초 내 반영 |
| 4 | 유료 LiteLLM 차단 | Claude 전슬롯 실패 → Codex CLI → Groq 무료 순으로만 폴백 |
| 5 | 배경 LLM | compaction/memory 배경 작업이 haiku → groq 무료 정상 처리 |
| 6 | 에이전트 역할 | pipeline runner가 DB 설정 기반 모델 사용 |

---

## 7. 리스크

| 리스크 | 영향 | 대안 |
|--------|------|------|
| Groq 무료 한도 초과 | 배경 작업 실패 | Groq 복수 모델 순회 + 로컬 Ollama 검토 |
| DB 장애 시 체인 로딩 실패 | LLM 전면 중단 | env 기본값 하드폴백 유지 (`_DEFAULT_CHAIN`) |
| 캐시 TTL 30초 지연 | 긴급 변경 지연 | 수동 캐시 무효화 API |

---

## 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-09-11 | v1.0 | 초안 작성, CEO 승인 후 Phase 1 착수 |
