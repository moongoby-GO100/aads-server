# AAG L1/L2 — AADS 아키텍처 결함 리포트

생성 2026-09-18 15:24 KST · 결함 3건 · 판정 불가(UNRESOLVED) 110건

UNRESOLVED 는 **결함 수에 포함하지 않는다**. 판정 못 한 것을 결함으로 세면
숫자가 부풀고, 부풀린 숫자는 아무도 손대지 않아 규칙 전체가 무시된다.

## 스캔 범위

| 대상 | 수 |
|---|---|
| 앱 파이썬 파일 | 396 |
| 라우터 디렉터리 파일 | 91 |
| APIRouter 정의 모듈 | 85 |
| include_router 호출 | 88 |
| 마운트된 라우트 | 936 |
| 네임스페이스 | 85 |
| 프런트 파일 | 237 |
| 해석된 프런트 호출 | 345 |
| SQL 참조 테이블 | 229 |
| 그래프 노드/엣지 | 786 / 1208 |

## 규칙별 건수

| 규칙 | 심각도 | 건수 |
|---|---|---|
| `DUP_MODULE` | P1 | 1 |
| `DOUBLE_MOUNT` | P1 | 0 |
| `ROUTE_SHADOWED` | P0 | 0 |
| `ORPHAN_ROUTER` | P2 | 1 |
| `TABLE_NO_MODEL` | P1 | 0 |
| `PATH_DRIFT` | P1 | 1 |
| `ROUTE_MISSING` | P0 | 0 |
| `STALE_BACKUP` | P2 | 0 |

| **합계** | | **3** |

## DUP_MODULE (1건)

- [P1] 모듈명 `chat.py` 이 2개 디렉터리에 중복 존재: `app/api/chat.py`, `app/routers/chat.py`

## ORPHAN_ROUTER (1건)

- [P2] `app/api/ceo_chat.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다

## PATH_DRIFT (1건)

- [P1] `/root/aads/aads-dashboard/src/lib/api.ts:651` POST `/api/v1/chat/messages` — 경로는 있으나 메서드가 GET 다 (/api/v1/chat/messages)

## UNRESOLVED (결함 아님 — 판정 불가)

### FRONTEND_URL (58건)
- `/root/aads/aads-dashboard/src/app/braming/api.ts:13` fetch() URL 해석 불가 — 변수가 경로 세그먼트 일부에만 붙어 경로를 확정할 수 없음: `${BASE}/braming${path}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1091` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/pipeline/jobs?session_id=${sessionId}&limit=50`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1245` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${qs}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1279` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1335` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${agendaId}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1360` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${agendaId}/restore`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1383` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${agendaId}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:2511` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/artifacts/${activeArtifact.id}/export`
- `/root/aads/aads-dashboard/src/app/chat/ChatInput.tsx:188` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/voice/transcribe`
- `/root/aads/aads-dashboard/src/app/chat/RunnerHostStatus.tsx:55` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/pipeline/runner/status?window_hours=1`
- `/root/aads/aads-dashboard/src/app/chat/api.ts:80` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작: `${BASE_URL}${path}`
- `/root/aads/aads-dashboard/src/app/chat/api.ts:162` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작: `${BASE_URL}/chat/files/upload?session_id=${sessionId}&uploaded_by=user`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:5743` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/sessions/${sessionId}/resume`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:5856` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/executions/${executionId}/events?last_event_id=${encodeURIComponent(replayLastEventId)}`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:7925` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/image/generate`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:8443` fetch() URL 해석 불가 — URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과): fetchUrl
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:8768` fetch() URL 해석 불가 — URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과): retry with backoff
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:9154` fetch() URL 해석 불가 — URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과): resumeUrl
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:9595` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/sessions/${sid}/stop`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:9664` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/sessions/${sid}/stop`
- … 외 38건

### FRONTEND_VAR_SEGMENT (1건)
- `/root/aads/aads-dashboard/src/app/admin/loops/page.tsx:65` POST /api/v1/loops/{}/{} — 변수 세그먼트라 확정 불가 (후보 /api/v1/loops/{}/safety)

### SQL_TABLE (51건)
- `app/api/admin.py:1045` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/admin_users.py:79` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/admin_users.py:180` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/memory_monitor.py:281` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/memory_monitor.py:301` 테이블 이름이 런타임 보간이라 확정 불가
- `app/core/credential_vault.py:705` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/ab_test_problems.py:263` SQL 조각(문장 키워드로 시작하지 않음) — 테이블 확정 불가
- `app/services/financial_audit.py:75` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/financial_audit.py:96` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:272` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:296` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:353` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:366` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:257` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:261` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:316` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:442` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:1208` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:1206` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:201` 테이블 이름이 런타임 보간이라 확정 불가
- … 외 31건

