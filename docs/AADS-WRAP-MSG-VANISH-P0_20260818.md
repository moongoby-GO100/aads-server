# AADS-WRAP: MSG-VANISH-P0 — CEO 질문 버블 사라짐 긴급 수정
날짜: 2026-08-18 | 우선순위: P0

## 문제
CEO 채팅창에서 입력한 질문 버블이 Pipeline Runner / 시스템 메시지에 밀려 사라지는 현상.
- `limit=40` 중 러너 알림(숨김 대상)이 대부분을 차지해 실제 대화 40건 확보 불가
- 렌더 cap이 raw `messages.length` 기준이라 숨김 메시지가 cap에 포함됨
- `protectedLocalQuestions`가 `localQuestionEchoIds`에만 의존해 일부 user 버블 누락

---

## 수정 내용 (3개 항목)

### P0-1: chat_service.py — 숨김 메시지 제외 후 LIMIT 적용

**파일**: `app/services/chat_service.py` (commit `c9423e2f`)

`_AUTO_MESSAGE_EXCLUDE_FILTER` (line 6034–6043) 패턴 통합:
```
이전: 이모지별 11개 LIKE '🔧 [Pipeline Runner]%' 패턴
이후:
  AND NOT (role = 'assistant' AND content LIKE '%[Pipeline Runner]%')
  AND NOT (role = 'assistant' AND content LIKE '%[Runner]%')
  AND NOT (role = 'user'      AND content LIKE '[시스템]%')
```

**SQL 구조 — 필터가 LIMIT 전에 적용됨**:

`list_messages_cursor` (lines 6561–6580):
```sql
SELECT * FROM (
  SELECT ... FROM chat_messages
  WHERE session_id = $1 AND tenant_id = $4
    AND intent IS DISTINCT FROM '_deleted_duplicate'
    AND [_extra_filter]
    AND NOT (role='assistant' AND content LIKE '%[Pipeline Runner]%')  ← 여기서 제외
    AND NOT (role='assistant' AND content LIKE '%[Runner]%')
    AND NOT (role='user'      AND content LIKE '[시스템]%')
  ORDER BY created_at DESC
  LIMIT 120   ← 제외 후 120건
) sub ORDER BY created_at ASC
```

`list_messages` (line 6508) 도 동일 필터 적용 (flat query, WHERE → LIMIT 순서 동일).

### P0-2: page.tsx — 렌더 상한 기준 전환

**파일**: `src/app/chat/page.tsx` line 7050 (commit `f91f838`)

```ts
// 이전
const MAX_RENDER = messages.length > 500 ? 40 : 150;

// 이후
const MAX_RENDER = display.length > 400 ? 200 : 150;
// display = 숨김 메시지 제외한 표시 대상 목록
```

- `messages` (raw) → `display` (숨김 제외 후) 기준으로 산정
- 임계값: 500 → 400 (display 기준이므로 낮춤)
- 상한: 40 → 200 (표시 대상만 있으므로 높여도 DOM 과부하 없음)

### P0-3: page.tsx — 로컬 질문 보호 전환

**파일**: `src/app/chat/page.tsx` lines 7053–7061 (commit `f91f838`)

```ts
// 이전
const protectedLocalQuestions = display
  .filter((item) => localQuestionEchoIdsRef.current.has(item.msg.id) && !cappedIds.has(item.msg.id))
  .slice(-5);

// 이후
const protectedLocalQuestions = display
  .filter((item) => (
    !cappedIds.has(item.msg.id) &&
    (
      localQuestionEchoIdsRef.current.has(item.msg.id) ||
      (item.msg.role === "user" && item.msg.intent !== "system_trigger")
    )
  ))
  .slice(-20);  // CEO 입력 질문 버블은 렌더 cap에서 절대 탈락시키지 않는다
```

- 보호 조건: `localQuestionEchoIds` 한정 → 모든 `role=user` (시스템 트리거 제외)
- 보호 개수: 5 → 20

### API 요청 limit

`limit=40` → `limit=120` (page.tsx 내 6개 API 호출 전부 변경)

---

## 검증 결과

| 항목 | 결과 |
|------|------|
| `py_compile` chat_service.py | ✓ 구문 이상 없음 |
| `pytest tests/unit/test_tools_and_pipeline.py` | ✓ 56/56 PASS |
| 대시보드 이미지 빌드 | ✓ 13:59 +0200 (P0 커밋 13:53 이후 재빌드) |
| `aads-dashboard` 컨테이너 | ✓ Up (healthy) |
| 서버 health-check | ✓ 응답 정상 |

---

## 관련 커밋

- **aads-server**: `c9423e2f` — `fix(chat): AADS-MSG-VANISH-P0 — 숨김 메시지 제외 후 LIMIT 적용`
  - 변경 파일: `app/services/chat_service.py` (+4, -11)
- **aads-dashboard**: `f91f838` — `fix(chat): AADS-MSG-VANISH-P0 - CEO 질문 버블 사라짐 수정`
  - 변경 파일: `src/app/chat/page.tsx` (+15, -9)

*(platform_accounts.json 등 계좌 관련 변경은 이 수정과 무관한 별도 uncommitted 파일)*

---

## 교훈

- **L-MSG-01**: 숨김 대상 메시지는 SQL WHERE 절에서 LIMIT 전에 제거해야 한다. 이모지별 개별 패턴보다 role+content 조합 패턴이 유지보수에 유리.
- **L-MSG-02**: 렌더 cap 기준은 항상 "실제 화면에 보이는 메시지 수"로 산정해야 한다. raw count 기준 시 숨김 메시지가 cap을 소진함.
- **L-MSG-03**: P0 수정 후 WRAP 파일 즉시 작성. 검수자가 SQL/프론트 변경을 각각 확인할 수 있도록 파일·라인 번호 명시 필수.
