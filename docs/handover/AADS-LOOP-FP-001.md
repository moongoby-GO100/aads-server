# AADS-LOOP-FP-001 — 루프 명령 오탐으로 "⚠️ 활성 루프가 없습니다." 응답 사고

- **기록일**: 2026-07-30 08:45 KST
- **커밋**: `87fd6bd4`
- **영향 범위**: CEO Chat (`send_message_stream`) 전 세션

## 1. 증상

CEO가 "이어서 진행해"를 입력했는데 본 응답 대신
`⚠️ 활성 루프가 없습니다.` 한 줄만 표시되고 턴이 종료됨.

## 2. 근본 원인 (2중)

| # | 원인 | 코드 위치 |
|---|------|-----------|
| A | `reply_to` 인용문(최대 2000자)이 `content`에 병합된 뒤, 그 오염된 문자열로 루프 인텐트를 판정 | `chat_service.py:8110` 병합 → `:8527` 판정 |
| B | `detect_loop_intent`가 앵커(`루프\|감시`)와 액션(`중지\|상태`)이 문서 **어디든** 각각 존재하면 루프 명령으로 판정 | `loop_chat_handler.py:37-47` |

인용된 이전 AI 응답에 "루프", "중단/중지"가 포함되어 `loop_stop`으로 오분류 →
`handle_loop_stop()`이 해당 세션 활성 루프를 찾지 못해 경고 문구 반환 후 `return`.

## 3. 조치

| 항목 | 내용 |
|------|------|
| 판정 입력 정화 | `detect_loop_intent(persisted_user_content)` — 인용문/재개 스캐폴드 제거본 사용 |
| 핸들러 입력 정화 | `handle_loop_start/stop/resume`에 `persisted_user_content` 전달 (루프 ID·간격·취소 여부 오파싱 차단) |
| 가드 ① 길이 | 200자 초과 입력은 루프 명령 아님 |
| 가드 ② 서술/질의 제외 | `보고해·설명·뭐지·구현·기획·문서·분석·검토·차이` 등 포함 시 제외 |
| 가드 ③ 인접 강제 | 앵커와 액션이 인접해야 매칭 (`루프 중지` O / `루프…보고…중단` X) |

## 4. 검증

- `scripts/_verify_loop_intent.py` — 오탐 6건 + 정탐 6건 = **12/12 PASS**
- `aads-server`, `aads-server-green` 양 슬롯 동일 결과
- `py_compile` 3파일 통과, Hot-Reload 45/67 모듈, health-check HTTP 200

## 5. 남은 리스크

- 루프 실사용 이력 0건 (`ohvis_loops` id=5 `paused` 1건, `session_id=e2e-prod`)
- 대시보드 루프 UI 미검증
