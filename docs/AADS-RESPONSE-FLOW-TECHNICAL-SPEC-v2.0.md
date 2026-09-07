# AADS 응답 플로우 기술문서 v2.0 (AADS-CRF)

- 버전: **v2.0** / 2026-09-08 KST
- 대체: v1.0(응답 개요 7칩, 2026-09-06)
- 대상 시스템: `aads-server`(FastAPI 0.115), `aads-dashboard`(Next.js 16), `aads-postgres`(PG15)
- 관련 PRD: `docs/plans/20260908_AADS_CEO_RESPONSE_FLOW_PRD_v2.0.md`

---

## 1. 아키텍처

```
CEO 입력
  │
  ▼
[intent 분류] ──────────────────────────────────────────────┐
  │                                                          │
  ▼                                                          │
[PromptCompiler]  prompt_assets(L1~L5) 조립                   │
  │   └─ L1 global-ceo-8section-response-flow (priority 23)   │
  │      → "지시파악→목표→계획→실행순서→결과→검증→리스크→다음" 지시
  ▼                                                          │
[LLM 스트리밍 생성]                                            │
  │                                                          │
  ▼                                                          │
[output_validator.validate_response()]                        │
  │   └─ check_report_quality_structure()                     │
  │        ├─ structural_gaps  (기존)                          │
  │        ├─ readability_gaps (기존)                          │
  │        └─ flow_gaps        ★v2.0 신규                      │
  │             evaluate_ceo_flow_coverage() < 5/8 → 재작성    │
  ▼                                                          │
[chat_messages 저장] ─────────────────────────────────────────┘
  │
  ▼
[Dashboard ChatBubble]
      └─ ResponseOverviewPanel ★8칩 (지시파악/목표/계획/실행순서/결과/검증/리스크/다음)
             └─ 칩 클릭 → 본문 섹션 스크롤 점프
```

---

## 2. 백엔드 구현

### 2.1 파일
`app/services/output_validator.py`

### 2.2 신규 상수

| 심볼 | 값/설명 |
|---|---|
| `_CEO_FLOW_GROUPS` | 8개 섹션 키(`brief/goal/plan/steps/result/verify/risk/next`) × 키워드 튜플 |
| `_CEO_FLOW_MIN_CHARS` | `900` — 이 미만 응답은 플로우 검사 제외 |
| `_CEO_FLOW_MIN_COVERAGE` | `5` — 8개 중 5개 미만 감지 시 재작성 |

### 2.3 신규 공개 함수

```python
def evaluate_ceo_flow_coverage(response_text: str) -> list[str]:
    """8섹션 중 본문에서 감지되지 않은 섹션 키 목록을 반환한다.
    반환값이 빈 리스트면 8/8 충족."""
```

프론트 칩 판정과 동일 기준을 백엔드에서도 재사용할 수 있도록 **공개 함수**로 노출했다.

### 2.4 판정 로직 (`check_report_quality_structure`)

```
flow_missing = evaluate_ceo_flow_coverage(text)
flow_gaps = [] if len(text) < 900 else (
    [f"flow_{k}" for k in flow_missing] if (8 - len(flow_missing)) < 5 else []
)

우선순위:
  structural_gaps >= 2  → REPORT_STRUCTURE_WEAK (기존 사유)
  readability_gaps >= 2 → REPORT_STRUCTURE_WEAK (기존 사유)
  flow_gaps 존재        → REPORT_STRUCTURE_WEAK (★신규: 8섹션 커버리지 미달)
  그 외                 → None (통과)
```

**설계 의도 — 왜 8/8을 강제하지 않는가:**
현재 채팅 응답 중단(interrupted)이 P0 이슈다. 검증 실패는 재시도를 유발하고,
재시도는 중단 확률을 높인다. 따라서 v2.0은 프롬프트(L1)를 1차 방어선으로 두고,
검증기는 "명백히 형식을 벗어난 응답"만 걸러내는 하한선(5/8) 역할만 한다.

### 2.5 재시도 프롬프트

`_build_report_quality_retry_prompt()`의 본문 필수 섹션 지시를 v2.0 8섹션으로 교체했다.
간단 조회·인사에는 강제하지 않는다는 예외 문구를 함께 주입한다.

---

## 3. 프롬프트 레이어 (DB)

| 컬럼 | 값 |
|---|---|
| `slug` | `global-ceo-8section-response-flow` |
| `title` | L1 Global / CEO 8섹션 응답 플로우 (AADS-CRF v2.0) |
| `layer_id` | 1 |
| `priority` | 23 |
| `intent_scope` | `NULL` (전역) |
| `role_scope` | `{*}` |
| `enabled` | `true` |

적용 검증은 `compiled_prompt_provenance.applied_assets`에 해당 slug가 포함되는지로 판정한다.
워크스페이스 문구나 모델 자기소개로 판정하지 않는다.

---

## 4. 프론트엔드 구현

### 4.1 파일
`src/app/chat/page.tsx`

### 4.2 타입 확장

```ts
type ResponseOverview = {
  ...
  hasBrief: boolean;   // ★신규 — 지시 파악
  hasGoal: boolean;
  hasPlan: boolean;
  hasSteps: boolean;   // ★신규 — 실행순서
  hasProgress: boolean;
  hasResult: boolean;
  hasVerification: boolean;
  hasRisk: boolean;
  ...
};
```

### 4.3 감지 정규식

| 필드 | 패턴 |
|---|---|
| `hasBrief` | `지시\s*(확인\|파악\|정리\|요약\|범위)`, `요청\s*(확인\|파악\|정리\|요약\|범위)`, `질문\s*(확인\|파악)` |
| `hasSteps` | `실행\s*순서`, `작업\s*순서`, `진행\s*순서`, `실행\s*단계`, `수행\s*단계`, `수행\s*내역`, `조치\s*내역`, `\d\s*단계` |

### 4.4 칩 구성

```ts
const indicators = [
  { label: "지시파악", ok: overview.hasBrief },
  { label: "목표",     ok: overview.hasGoal },
  { label: "계획",     ok: overview.hasPlan },
  { label: "실행순서", ok: overview.hasSteps || overview.hasProgress },
  { label: "결과",     ok: overview.hasResult },
  { label: "검증",     ok: overview.hasVerification },
  { label: "리스크",   ok: overview.hasRisk },
  { label: "다음",     ok: Boolean(overview.nextAction) },
];
```

`실행순서`는 기존 `hasProgress`를 하위호환으로 흡수한다(구 응답에서도 칩이 빈칸으로 남지 않게).

### 4.5 칩 클릭 점프 사전

`jumpToResponseOverviewTarget()`의 키워드 맵에 `지시파악`, `실행순서` 항목을 추가했다.
공백/기호를 제거한 정규화 문자열로 본문 헤딩을 매칭해 스크롤한다.

---

## 5. 배포 절차

| 대상 | 절차 |
|---|---|
| `aads-server` (Python) | `docker exec aads-server bash /app/scripts/reload-api.sh` (0ms 무중단) 또는 `deploy.sh bluegreen` |
| `aads-dashboard` (Next.js) | `docker compose -f /root/aads/aads-dashboard/docker-compose.yml build && up -d` (blue/green) |
| `prompt_assets` | DB UPSERT 즉시 반영, 배포 불필요 |

> 주의: 이 환경의 MCP 원격 쓰기 도구는 **활성 API 컨테이너 내부 파일시스템**에 기록된다.
> 호스트 저장소 반영에는 `docker cp <container>:/app/... /root/aads/aads-server/...` 전파가 필요하다.
> 전파를 빠뜨리면 커밋·이미지 빌드에 변경이 포함되지 않는다. (v2.0 작업 중 실제 발생)

---

## 6. 검증 방법

```bash
# 1) 백엔드 로직
docker exec aads-server-green python3 -c "
from app.services.output_validator import evaluate_ceo_flow_coverage
print(evaluate_ceo_flow_coverage(open('/tmp/sample.md').read()))"

# 2) 프롬프트 적용
SELECT provenance->'applied_assets'
FROM compiled_prompt_provenance ORDER BY created_at DESC LIMIT 1;

# 3) 프론트 빌드
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard

# 4) 회귀 감시 (배포 후 24h)
SELECT interrupt_category, count(*) FROM chat_executions
WHERE created_at > NOW() - INTERVAL '24 hours' GROUP BY 1;
```

---

## 7. 변경 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| v1.0 | 2026-09-06 | 응답 개요 7칩(목표/계획/진행/결과/검증/리스크/다음), 클릭 점프 |
| **v2.0** | **2026-09-08** | **CEO 8섹션 플로우 — L1 프롬프트 에셋 신규, 백엔드 커버리지 검증, 프론트 8칩 확장** |
