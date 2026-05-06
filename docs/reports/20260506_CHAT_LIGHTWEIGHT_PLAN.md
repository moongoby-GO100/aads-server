# AADS Chat Lightweight Plan

작성 시각: 2026-05-06 08:59 KST
범위: 채팅창 체감 지연, 메시지 로딩, 폴링, 프론트 렌더링 경량화 기획. PC Agent 끊김 P0 수정과 분리한다.

## 요약

채팅창 지연의 1차 원인은 단일 병목이 아니라, 프론트 단일 대형 컴포넌트와 반복 메시지 재조회가 누적되는 구조다. 즉시 개선은 "초기 로드 최소화 + 변경 없는 폴링에서 messages 재조회 금지 + 렌더링 가상화" 순서가 맞다.

## 실측 기반 현재 구조

| 항목 | 현재 상태 | 근거 |
|---|---:|---|
| 프론트 채팅 화면 | 6,347 lines | `/root/aads/aads-dashboard/src/app/chat/page.tsx` |
| 백엔드 채팅 서비스 | 6,506 lines | `app/services/chat_service.py` |
| 채팅 라우터 | 1,652 lines | `app/routers/chat.py` |
| 세션 진입 메시지 로드 | `limit=100` | `page.tsx`의 `/chat/messages?session_id=...&limit=100` |
| 완료/복구 재조회 | `limit=50` 반복 | `page.tsx`의 `just_completed`, SSE 끊김, waitingBg 처리 |
| 폴링 최적화 일부 | `streaming-status.last_message_id` 존재 | `app/routers/chat.py` |
| 메시지 API 상한 | `limit <= 1000` | `app/routers/chat.py` |

## 문제점

1. 세션 진입 경로가 무겁다.
   - `streaming-status` 확인 후 메시지 `limit=100` 로드, workspace artifacts 로드, 모델/역할 상태 동기화가 한 화면 effect에 묶여 있다.
   - 초기 화면에는 최근 30~50개만 필요하지만 기본 로드가 100건이고, 메시지 본문/attachments/tools까지 모두 포함한다.

2. 폴링이 아직 "상태 확인"과 "데이터 재조회"를 완전히 분리하지 못했다.
   - `last_message_id`가 있어도 `just_completed`, SSE 끊김, waitingBg, rate_limited 등 여러 분기에서 `/chat/messages?limit=50` 재조회가 반복된다.
   - 상태만 바뀐 경우와 메시지 본문이 바뀐 경우를 분리하는 revision 값이 없다.

3. 프론트 렌더링 단위가 크다.
   - `page.tsx`가 채팅, 세션, SSE, 이미지 생성, artifacts, diff approval, settings overlay까지 한 컴포넌트에 밀집돼 있다.
   - `setMessages`가 잦고, 메시지 리스트가 가상화되어 있지 않아 긴 세션에서 렌더링 비용이 증가한다.

4. 백엔드 메시지 조회가 full row 중심이다.
   - `list_messages`, `list_messages_cursor`가 `SELECT * FROM chat_messages`를 사용한다.
   - 프론트는 폴링에서는 id/role/intent/created_at 정도만 필요한 경우가 많지만, 현재는 content/tools/attachments까지 함께 가져오는 경로가 남아 있다.

5. artifacts가 workspace 기준으로 매번 붙는다.
   - 세션 진입 시 `/chat/artifacts?workspace_id=...`를 호출한다.
   - 채팅 응답 입력/OTP 같은 긴급 상호작용에는 artifacts 전체 로드가 필수 선행 조건이 아니다.

## 개선 설계

### Phase 0: 측정 계측

- API 응답 헤더에 `X-AADS-Query-MS`, `X-AADS-Payload-Bytes`, `X-AADS-Row-Count`를 선택적으로 추가한다.
- 프론트는 세션 진입, 첫 버블 표시, SSE 첫 delta, 메시지 재조회 횟수를 `performance.mark`로 기록한다.
- 목표: 개선 전후를 "체감"이 아니라 p50/p95 로딩 시간과 payload bytes로 판단한다.

### Phase 1: 초기 로드 경량화

- 세션 진입 기본 메시지 로드를 `limit=100`에서 `limit=40`으로 낮춘다.
- `/chat/messages`에 `fields=minimal|full`을 실제 구현한다.
  - `minimal`: id, session_id, role, content preview, intent, model_used, created_at, edited_at
  - `full`: attachments, sources, tools_called, thinking_summary 포함
- 긴 메시지는 첫 로드에서 preview만 받고, 펼침/스크롤 진입 시 full message를 lazy load한다.

### Phase 2: 폴링 재조회 제거

- `streaming-status`에 `message_revision`, `placeholder_revision`, `artifact_revision`을 추가한다.
- 프론트는 revision이 동일하면 `/chat/messages` 재호출을 금지한다.
- waitingBg 중 partial content는 `/streaming-status`의 `partial_content`만 반영하고, 최종 assistant가 생겼을 때만 `limit=20~50` 재조회한다.

### Phase 3: 리스트 가상화와 컴포넌트 분리

- 메시지 리스트를 `react-virtuoso` 또는 `@tanstack/react-virtual`로 가상화한다.
- `page.tsx`를 다음 단위로 분리한다.
  - `useChatSessionLoader`
  - `useChatStreaming`
  - `ChatMessageList`
  - `ChatComposer`
  - `ArtifactPanel`
- `MessageItem`은 `React.memo`로 고정하고, streaming buffer만 별도 컴포넌트 state로 분리한다.

### Phase 4: artifacts lazy load

- 세션 진입 시 artifacts는 count/last_updated만 가져온다.
- 오른쪽 artifact panel을 열 때 full list를 로드한다.
- 메시지 완료 이벤트가 `artifact_revision`을 올릴 때만 panel 데이터를 갱신한다.

## 우선순위 체크리스트

| 우선순위 | 작업 | 기대 효과 | 위험 |
|---|---|---|---|
| P0 | `fields=minimal` 실제 구현, 초기 `limit=40` | 초기 payload 감소 | 낮음 |
| P0 | `streaming-status` revision 기반 messages fetch skip | 폴링 중 네트워크 감소 | 중간 |
| P1 | 메시지 리스트 가상화 | 긴 세션 렌더링 지연 감소 | 중간 |
| P1 | artifacts lazy load | 세션 전환 가벼워짐 | 낮음 |
| P2 | `page.tsx` hook/component 분리 | 유지보수성 개선 | 중간 |
| P2 | 메시지 full body lazy load | 대형 응답 세션 최적화 | 중간 |

## 완료 기준

- 세션 진입 시 `/chat/messages` payload bytes 50% 이상 감소.
- idle polling 상태에서 `/chat/messages` 호출 0회 유지.
- 500개 메시지 세션에서도 스크롤/입력 프레임 드랍이 체감되지 않을 것.
- SSE 중 응답 버블 손실, 세션 전환 빈 화면, recovered 중복 메시지 회귀가 없을 것.

## 권장 실행 방식

채팅 경량화는 프론트/백엔드/SSE 상태 계약을 같이 건드리므로 즉시 P0 hotfix와 분리한다. 별도 작업으로 `AADS Chat Lightweight v1`을 만들고, Phase 0~1을 먼저 배포한 뒤 실측값을 보고 Phase 2~4를 진행한다.
