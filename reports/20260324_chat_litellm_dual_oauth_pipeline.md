# 채팅 Claude — Naver 리밋 시 Gmail 미전환 원인 및 조치

**일자**: 2026-03-24 (KST)

## 원인 (요약)

1. **`call_stream` 메인 경로**가 LiteLLM 이중 키를 쓰지 않고 **CLI Relay → Agent SDK**만 사용함. 둘 다 **계정 1개(호스트 OAuth / 컨테이너 번들 CLI 자격)**만 사용 → Naver 한도 소진 시 Gmail으로 자동 전환 없음.
2. LiteLLM에 Claude 배포가 2개 있어도 **기본 `simple-shuffle`**이면 Naver 쪽에 반복 배정될 수 있음. **order 기반 우선순위**가 없으면 “자동 폴백”이 기대와 다르게 동작.

## 조치

| 항목 | 내용 |
|------|------|
| `model_selector.call_stream` | CLI Relay 실패 후 **LiteLLM `/v1/messages`** 단계 삽입 → 그 다음 Agent SDK → Gemini |
| `litellm-config.yaml` | Claude 배포에 **`order: 1`(KEY_1 Gmail) / `order: 2`(KEY_2 Naver)** + `router_settings.enable_pre_call_checks: true`, `num_retries: 3` |
| `model_selector._stream_agent_sdk` docstring | “Naver→Gmail 자동 교대” 오해 제거 (실제는 동일 자격 재시도만) |
| `anthropic_client` docstring | Gmail→Naver 순서 문서 정합 |

## 배포

- `aads-server` 이미지/컨테이너 재시작
- **`aads-litellm` 컨테이너 재시작** (config 반영)

## 검증

- `python3.11 -m py_compile` (model_selector, anthropic_client): 통과
