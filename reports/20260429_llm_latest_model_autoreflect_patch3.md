# LLM 최신모델 자동반영 보강 3차 패치

작성일: 2026-04-29 KST

## 적용 판정

| Provider | 판정 | 근거 |
| --- | --- | --- |
| OpenAI | 완전 자동 | provider key가 있으면 Models API discovery 결과를 DB registry에 병합하고 executable/filter/summary에 반영한다. |
| Gemini | 완전 자동 | Generative Language Models API discovery 결과를 DB registry에 병합하고 `generateContent` 가능 모델만 자동 실행 후보로 분류한다. |
| LiteLLM | 부분 자동 | LiteLLM `/model/info` catalog는 조회하지만 provider별 실행 가능성은 proxy 설정과 admin review에 의존한다. |
| DeepSeek | 템플릿 의존 | 최신 canonical ID는 템플릿으로 즉시 반영하고 실행은 LiteLLM proxy로 고정한다. DeepSeek 직접 catalog discovery는 별도 경로로 추가하지 않았다. |
| Anthropic | 템플릿 의존 | OAuth auth token으로 Claude runtime 실행은 가능하지만 Anthropic Models API discovery에는 `x-api-key`가 필요하므로 OAuth-only 상태에서는 자동 discovery가 불가하다. |
| Codex CLI | 템플릿 의존 | ChatGPT Plus OAuth 기반 relay 모델은 공식 catalog discovery가 아니라 운영 alias 템플릿으로 관리한다. |
| Groq / OpenRouter / Qwen / Kimi / MiniMax | 템플릿 의존 | 현재 registry template과 LiteLLM/OpenAI-compatible 실행 metadata 기준으로 노출한다. provider별 direct catalog 자동 병합은 범위 밖이다. |

## 기존 문제점

- DeepSeek 최신 운영 ID가 `deepseek-chat`, `deepseek-reasoner` 중심이라 최신 canonical ID와 API 응답/selector/가격표가 불일치할 수 있었다.
- 기존 DeepSeek legacy ID를 제거하지 않고 유지해야 했지만, alias가 어떤 canonical 모델을 가리키는지 metadata로 설명하지 못했다.
- Anthropic은 OAuth token으로 Claude 실행은 가능한데 Models API discovery skip reason이 API-key 부재로만 기록되어, 실행 불가와 discovery 불가가 혼동됐다.
- Provider summary에 `runtime_executable`과 `auto_discovery_supported`가 분리되어 있지 않아 운영자가 자동반영 범위를 과대 해석할 수 있었다.
- 과거 registry metadata가 DeepSeek direct backend로 남아 있을 경우, selector가 direct route를 탈 여지가 있었다.

## 이번 패치 내용

- `deepseek-v4-flash`, `deepseek-v4-pro`를 DeepSeek canonical model ID로 등록했다.
- `deepseek-chat`, `deepseek-reasoner`는 compatibility alias로 유지하고 metadata에 `canonical_model`, `compatibility_alias=true`, `deprecation_date=2026-07-24`를 추가했다.
- DeepSeek selector 경로는 legacy alias 요청도 canonical 실행 ID로 변환해 LiteLLM proxy에 전달한다.
- DeepSeek direct provider 상수와 stale direct metadata 경로를 정리해 직접 REST 호출 대신 LiteLLM proxy 실행으로 고정했다.
- Anthropic discovery skip reason을 `oauth_runtime_only_models_api_unavailable`으로 교체했다.
- Provider/model/discovery metadata에 `runtime_executable`, `auto_discovery_supported`, `discovery_requirement`, `model_source`, `active_model_source`, template/discovery active count를 추가했다.
- `/api/v1/llm-models/providers/summary` 응답에 runtime/discovery provider count와 template-runtime-only provider 목록을 추가했다.
- 회귀 테스트에 DeepSeek V4 canonical/alias, Anthropic OAuth-only discovery, DeepSeek alias selector routing 케이스를 추가했다.

## 검증

```bash
E2B_API_KEY=test python3.11 -m pytest tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_llm_registry_sync_flow.py -q
```

결과: `24 passed in 1.92s`

참고: 동일 명령을 환경값 없이 실행하면 `Settings()` collection 단계에서 `E2B_API_KEY` 필수값 누락으로 중단된다.

## 운영 확인 방법

- Provider summary: `/api/v1/llm-models/providers/summary`
  - Anthropic OAuth-only 예상값: `runtime_executable=true`, `auto_discovery_supported=false`, `discovery_requirement`에 `x-api-key required` 포함.
  - DeepSeek 예상값: `deepseek-v4-flash`, `deepseek-v4-pro` active/selectable, legacy alias metadata에 canonical/deprecation 정보 포함.
- Discovery run: `/api/v1/llm-models/discovery-runs?limit=8`
  - Anthropic OAuth-only 예상값: `status=skipped`, `error=oauth_runtime_only_models_api_unavailable`.

## 남은 한계

- Anthropic direct discovery는 Models API용 `x-api-key` 없이는 불가하다. OAuth auth token은 runtime 실행에는 쓰지만 Models API catalog discovery 권한으로 취급하지 않는다.
- DeepSeek 최신 ID는 템플릿 기반 반영이다. provider 공식 catalog discovery가 추가되기 전까지는 alias/canonical 테이블을 운영 코드에서 갱신해야 한다.
- LiteLLM `/model/info` discovery 결과는 proxy 설정 상태에 좌우되므로 provider별 최신성 보장은 OpenAI/Gemini direct discovery보다 낮다.

## 향후 개선안

- Anthropic 운영 전략을 둘 중 하나로 명확화한다: Models API 전용 admin `x-api-key`를 별도 보관해 discovery만 허용하거나, 공식 Claude alias 테이블을 정기 갱신하는 템플릿 운영으로 고정한다.
- DeepSeek/Claude alias table을 날짜 포함 정적 registry로 분리해 deprecation window와 canonical mapping 변경 이력을 API에서 조회 가능하게 만든다.
- LiteLLM discovery 결과를 provider별로 정규화해 `model_source=litellm_discovery`와 실제 upstream provider를 분리 표시한다.
