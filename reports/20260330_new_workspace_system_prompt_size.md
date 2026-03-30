# 새 테스트 워크스페이스·세션 — 시스템 프롬프트 크기 측정

**일시**: 2026-03-30 (KST)  
**방법**: `aads-server` 컨테이너 내 Python — `init_pool()` 후 `build_messages_context()` / `system_prompt_v2.build_layer1()` 호출, `estimate_tokens()` (바이트÷3 근사).

## 결론

**워크스페이스 DB `system_prompt`가 비어 있어도 기본 시스템 프롬프트는 크다.**

- **Layer 1만 (풀, 인텐트 없음)**: 약 **7,020자**, 추정 **~3,432 토큰**.
- **실제 채팅 조합 (Layer1 + Layer2 + 메모리·프리로드·보정·Auto-RAG 등)**:  
  - `raw_messages=[]` (히스토리 없음) 기준 약 **10,061자**, 추정 **~5,145 토큰**.  
  - 첫 사용자 메시지 `"안녕"` / 긴 작업 요청 1턴 기준도 **동일 대역 (~1.0만 자 / ~5.1k~5.4k 토큰)**.

즉, **“테스트 프로젝트 + 새 세션”만으로 프롬프트가 작아지지 않는다.** 워크스페이스 커스텀 문구가 없어도 **전역 Layer1·동적 레이어**가 이미 수천~만 자 단위다.

## 보조 측정 (Layer1 압축)

- `intent="greeting"`으로 **경량 Layer1**만 직접 빌드 시: 약 **913자**, 추정 **~476 토큰** (`build_layer1_lite` 수준).
- 다만 **실제 `build_messages_context` 총합**은 Layer2·RAG·프리로드 등으로 여전히 **~1만 자**에 달함 — 인사만으로 전체가 크게 줄지는 않을 수 있음.

## 구현 참고 (`context_builder`)

- `system_prompt = layer1 + layer2 + memory + preload + auto_rag + artifact` 조합 (`app/services/context_builder.py`).
- `chat_service`는 `build_messages_context(..., intent=...)`를 넘기지 않아, 압축은 **마지막 user 문장 휴리스틱·내부 로직**에 의존.

## 캐시 이슈 (참고)

`context_builder.build_layer1` 래퍼는 **ws_key + base_system_prompt만** 캐시 키로 쓰며 **`intent`는 키에 포함되지 않음**. 동일 프로세스에서 풀/라이트를 바꿔 호출하면 **의도와 다른 Layer1이 재사용될 수 있음** — 별도 이슈로 두고, 본 측정은 `system_prompt_v2.build_layer1` 직접 호출로 풀/라이트 길이를 구분함.

## 검증 명령 (재현)

컨테이너에서 `init_pool()` 후 `build_messages_context("[TEST] …", <uuid>, [], "", conn)` 및 1턴 user 메시지 변형 호출로 동일 측정 가능.
