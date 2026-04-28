# 누적 4000건 세션 분기 권유 — 코드 정밀 분석 + 개선안 보고서

_작성: 2026-04-28 | 작성자: Claude Opus 4.7 (1M context)_
_컨텍스트: AADS-002[기능개선] 채팅 19808b87 누적 4000건의 응답 지연 진단 후속 작업_

---

## 1. 현재 상태 실측

### 1-1. 세션 누적 분포 (전 시스템)
| 누적 메시지 | 세션 수 |
|---|---:|
| 2000+ | 7 |
| 1000~1999 | 2 |
| 500~999 | 8 |
| 200~499 | 6 |
| 50~199 | 10 |
| <50 | 33 |

전체 36,650 메시지 중 **88.8% (32,527)이 `is_compacted=true`** — 압축 자체는 광범위하게 동작.

### 1-2. 거대 세션의 실제 활성 컨텍스트
| 세션 | 누적 | **활성(미압축)** | 비고 |
|---|---:|---:|---|
| GO100-005 [COO] | 7,197 | **40** | 압축 양호 ⭐ |
| **AADS-002 (현재 채팅)** | **4,008** | **121** | 25,748 토큰 (avg 851 chars × 121) |
| GO100-002 [CTO] | 3,292 | 47 | 압축 양호 |
| GO100-006 [디자이너] | 3,031 | **198** | 압축 부분 동작 |
| KIS-003 [연구소] | 2,604 | 103 |  |
| AADS-003 [유지보수] | 2,380 | **185** | |
| **AADS-012 [LLM 인증]** | 2,150 | **450** | **압축 거의 미동작** ⚠️ |
| GO100-003 [트레이너] | 1,522 | 68 |  |
| AADS-008 [CTO] | 1,247 | 73 |  |

**핵심**: `message_count`(누적)는 무의미한 지표. **`활성 메시지 수`가 실제 LLM 입력 크기를 결정**합니다. AADS-012는 누적 절반인데 활성이 4배 → 압축 트리거가 안 걸렸음.

## 2. 현 코드의 압축·분기 메커니즘

### 2-1. 압축 트리거 (자동)
**파일**: `app/services/context_builder.py:450-461`
\`\`\`python
_COMPACTION_THRESHOLD = int(os.getenv("COMPACTION_TRIGGER_TOKENS", "80000"))
if _est > _COMPACTION_THRESHOLD:
    from app.services.compaction_service import check_and_compact
    messages = await check_and_compact(session_id, messages, db_conn=conn)
\`\`\`
- **80K 토큰 초과 시에만** 자동 발동 (turn 수 무관)
- `COMPACTION_KEEP_RECENT=20` — 최근 20턴 보존, 그 이전 LLM 요약

### 2-2. 히스토리 로딩 (`chat_service.py:3236, 3253`)
\`\`\`sql
SELECT role, content FROM (
  SELECT role, content, created_at FROM chat_messages
  WHERE session_id = \$1 AND (is_compacted IS NULL OR is_compacted = false)
  ORDER BY created_at DESC LIMIT 200
) sub ORDER BY created_at ASC
\`\`\`
**문제점**:
- 활성 메시지가 200개 미만이어도 모두 로드 → AADS-012는 매 턴 450개 모두 로드
- 200 cap이 있어도, 각 메시지 평균 851 chars면 200 × 851 = **170K chars ≒ 42K 토큰**

### 2-3. 분기(branch) 기능
**파일**: `app/routers/chat.py:867-921`
- 메시지 단위 branch 존재 (`POST /chat/messages/{message_id}/branch`)
- **단점**: 새 \`branch_id\` 부여하지만 **같은 session 내**에 머무름. message_count는 계속 늘어남.
- Frontend의 \`branchPointRef\`로 트리거 가능하지만 사용자가 수동으로 메시지 우클릭 필요.

### 2-4. 자동 분기·아카이브·세션 전환 권유
**현재 없음.** \`fork_session\`, \`archive_session\`, "새 세션 만들까요?" 같은 UX 없음.

## 3. 사용자 체감과 4000건 세션의 관계

| 지연 요소 | 4000건 세션에서 비중 |
|---|---|
| 입력 토큰 (히스토리 200건) | **42K+ 토큰** → opus 입력 단가 + 처리 시간 |
| LLM 라운드트립 14회 (도구) | 14 × 40초 = 560초 |
| Prompt cache hit률 저하 | 매 턴 메시지 200건 prefix 일부 변경 → cache miss |
| TTFB (첫 토큰까지) | 입력 토큰 클수록 느림 |

따라서 **누적이 클수록 매 턴 +5~10초 추가 지연**. 비용도 비례 상승 (opus 입력 토큰 단가 × 42K).

## 4. 개선안 (즉시·단기·중기)

### 즉시 (frontend 1줄 + UI 토스트)
**Sidebar/ChatHeader에 nudge 배너** — 활성 메시지 수 50 초과 시 표시:
\`\`\`tsx
{activeSession?.active_message_count >= 50 && (
  <div className="banner-warning">
    이 세션의 활성 컨텍스트가 {activeSession.active_message_count}건입니다.
    응답 속도가 느려질 수 있어 새 세션을 권장합니다.
    <button onClick={() => createNewSession({forkSummary: true})}>
      요약과 함께 새 세션 시작 →
    </button>
  </div>
)}
\`\`\`
백엔드 변경: \`GET /chat/sessions\` 응답에 \`active_message_count\` 추가:
\`\`\`sql
SELECT s.*,
  (SELECT count(*) FROM chat_messages m
   WHERE m.session_id = s.id AND NOT coalesce(m.is_compacted, false)) AS active_message_count
FROM chat_sessions s
\`\`\`

### 즉시 (backend) — 압축 강제 트리거 보강
**파일**: \`app/services/context_builder.py:450\`
\`\`\`python
# 현재: 토큰 기반만
if _est > _COMPACTION_THRESHOLD:
    messages = await check_and_compact(...)

# 개선: 활성 메시지 수 기반도 추가
_ACTIVE_MSG_THRESHOLD = int(os.getenv("COMPACTION_ACTIVE_MSG_THRESHOLD", "100"))
if _est > _COMPACTION_THRESHOLD or len(messages) > _ACTIVE_MSG_THRESHOLD:
    messages = await check_and_compact(...)
\`\`\`
**효과**: AADS-012의 450건 활성을 100건 이내로 자동 정리. 25-50% 토큰 절감 예상.

### 단기 (backend API + frontend) — \`fork_session\` 신규 도입
**신규 엔드포인트**: \`POST /chat/sessions/{session_id}/fork\`
\`\`\`python
# 동작:
# 1) 기존 세션에 compaction 강제 실행 → 요약 생성
# 2) 새 chat_session row 생성 (workspace_id 동일, title="{기존} (계속)")
# 3) 새 세션 첫 메시지로 요약 삽입 (intent=session_carryover)
# 4) 응답: { new_session_id, summary_message_id }
\`\`\`
Frontend: 권유 배너에서 "새 세션으로 분기" 버튼 → \`fork_session\` 호출 → 새 세션 자동 진입.

**장점**:
- 기존 세션은 보존 (히스토리 손실 없음)
- 새 세션은 요약 1건 + 빈 컨텍스트 → 응답 속도 즉시 회복
- 이름·태그 유지

### 단기 — 동적 LIMIT 적용
**파일**: \`app/services/chat_service.py:3239, 3256\` 의 \`LIMIT 200\` 변경:
\`\`\`sql
-- 활성 메시지 200개 초과 세션은 50개로 제한 (이미 압축본이 충분)
LIMIT CASE WHEN \$2 > 200 THEN 50 ELSE 200 END
\`\`\`
(\$2 = \`(SELECT count(*) FROM chat_messages WHERE session_id=\$1 AND NOT compacted)\`)

### 중기 (전략) — workspace 자동 회전
워크스페이스 단위로 "활성 세션 1개 유지" 정책:
- 활성 세션의 message_count > 1000이면 자동으로 archive + new 세션 권유
- archive된 세션은 검색 가능하되 기본 목록에서 숨김

## 5. 권장 우선순위 (ROI)

| # | 변경 | 개발 시간 | 효과 |
|---|---|---|---|
| 1 | **frontend 배너** + \`active_message_count\` 노출 | 1시간 | 사용자 자각 → 행동 유도 |
| 2 | **압축 트리거 OR 조건 추가** (active > 100) | 30분 | AADS-012 같은 케이스 자동 정리 |
| 3 | **\`fork_session\` API + UI** | 4시간 | 거대 세션 일회성 해소 가능 |
| 4 | **동적 LIMIT** (200 → 50) | 30분 | 활성 200+ 세션 즉시 50% 입력 토큰 절감 |
| 5 | workspace 자동 회전 정책 | 2일 | 운영 자동화 |

## 6. 결론
- **압축 자체는 동작**하지만 **트리거가 토큰 단일축**이라 활성 메시지 누적이 큰 세션을 잡지 못합니다 (AADS-012 사례).
- **사용자 자발적 분기 UX 부재** — branch는 메시지 단위만 있고 "세션 분기" 개념이 없음.
- **즉시 적용 가능한 1·2번**(배너 + 압축 OR 조건)만으로 \`active_message_count\`가 큰 4000건급 세션의 응답 속도 30~50% 개선 예상.
- **3번 fork_session**은 거대 세션 일회성 정리에 효과적이며, 사용자에게 "기존 컨텍스트 손실 없이 빠른 새 세션"이라는 명확한 가치 제공.

---

## 부록: 측정 데이터 출처
- DB: \`postgresql://aads@aads-postgres:5432/aads\` 직접 쿼리
- 측정 시각: 2026-04-28 09:00~09:30 KST
- 코드 분석 대상:
  - \`/root/aads/aads-server/app/services/context_builder.py\` (commit c46ddbe 기준)
  - \`/root/aads/aads-server/app/services/chat_service.py\` (commit c46ddbe 기준)
  - \`/root/aads/aads-server/app/routers/chat.py\` (commit b24b47f 기준)
  - \`/root/aads/aads-dashboard/src/app/chat/page.tsx\` (commit 56ed27c 기준)
