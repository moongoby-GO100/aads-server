# OVIS Smart Browser 실행·학습·보안 아키텍처 기획서
**AADS-SMART-BROWSER-PC-APP-PLAN-20260919**  
문서 버전: v2.0
작성: 2026-09-19 KST | 작성자: CTO AI (오비스) | 담당: CEO moongoby 승인

> v2.0 변경: 사이트 학습·재방문 구조와 실행 안전 기반 6종(Channel Router,
> 페이지 데이터 비명령화, Skill API, ARIA 부분 시그니처, 표시 직전 사실
> 재검증, Golden Task 승격)을 정식 반영했습니다. macOS 전용 앱은 CEO 지시에
> 따라 보류하고 Windows를 우선합니다. 1~7장은 최초 기준선이며, 8장 이후가
> 현재 구현 정본입니다.

---

## 요약

현재 스마트 브라우저는 **서버 헤드리스 전용 → 401 차단 → PC Agent 우회** 구조로,  
CEO PC에 PC Agent가 없으면 브라우저 작업이 전혀 실행되지 않는 단일 장애점입니다.

**권장 방향**: 3단계 아키텍처로 전환합니다.
1. **즉시(M1)** — 채팅 아티팩트에 브라우저 스트림 뷰어 + 명령 입력 패널
2. **단기(M2)** — 서버 Playwright 401 인증 경로 개방 (백엔드 핫픽스)
3. **중기(M3)** — 오비스 PC 전용 앱(Windows 우선) 출시 — PC Agent 완전 대체

---

## 1. 현황 분석 (실측 기준)

| 항목 | 현재 상태 | 문제 |
|---|---|---|
| 서버 Playwright | `headless=True` 고정, chrome-headless-shell 3프로세스 구동 | 화면 없음, 창 표시 불가 |
| 서버 캡처 경로 | `POST /browser-bridge/work-sessions/ensure` → **401** | 인증 미통과로 전면 차단 |
| 브라우저 작업 경로 | 전부 PC Agent(`agent_id=2e9379a1`) 로 디스패치 | CEO PC 의존, 부재 시 전면 중단 |
| 채팅 아티팩트 활용 | 미구현 | 채팅창에서 직접 제어 불가 |
| 레시피 기록/재생 | `work_recipes 0건` / `recipe_runs 0건` | M2 작업 진행 중 |

---

## 2. 방안 비교

| 방안 | 설명 | 장점 | 단점 | 구현 난이도 |
|---|---|---|---|---|
| **A. 채팅 아티팩트 뷰어** | 서버 Playwright 스크린샷을 SSE로 실시간 스트리밍 → 아티팩트 패널에 표시. 채팅 입력 → 명령 전송 | 설치 불필요, 즉시 사용 가능, 모든 기기에서 동작 | 클릭/입력은 간접 (명령 중계), 화면 지연 1~3초 | **낮음** (2~3주) |
| **B. 오비스 PC 전용 앱** | Electron 앱으로 로컬 Playwright/Chromium 번들. AADS 백엔드와 WebSocket 연결. 실제 브라우저 창 표시 | 실제 창 표시, PC Agent 불필요, 저지연 | 설치 필요, 앱 배포/업데이트 관리 | **높음** (6~8주) |
| **C. 하이브리드 (권장)** | A(즉시) + B(중기) 순차 구현. 채팅 아티팩트로 먼저 쓰고, PC 앱으로 고도화 | 단계적 가치 제공, 리스크 분산 | 두 경로 유지 부담 | **중간** |

**권장: C안 (하이브리드)** — 즉시 채팅 아티팩트로 가치를 내고, PC 앱으로 완성도를 높입니다.

---

## 3. 방안 A 상세 설계 — 채팅 아티팩트 브라우저 뷰어

### 3-1. 아키텍처

```
[채팅 입력창]
    │ 자연어 명령 (예: "쿠팡 접속해서 검색해줘")
    ▼
[오비스 AI 세션]
    │ LLM 해석 → 브라우저 액션 JSON
    ▼
[POST /ohvis/console/command]  ← 이미 구현됨 (R4 배포 완료)
    │
    ▼
[SmartBrowserGateway]
    │ Playwright 실행 (서버 헤드리스)
    │ 스크린샷 캡처 → 매 액션마다
    ▼
[SSE 스트림 /browser-bridge/stream/{session_id}]
    │ JPEG 프레임 + 상태(URL/title/진행단계)
    ▼
[채팅 우측 아티팩트 패널]
    ├── 브라우저 뷰어 (실시간 화면 스트림)
    ├── 현재 URL / 페이지 제목
    ├── 진행 단계 표시 (기록 중 / 재생 중 / 대기)
    └── 직접 명령 입력창 (아티팩트 내)
```

### 3-2. 구현 단계

| 단계 | 작업 | 예상 기간 |
|---|---|---|
| S1 | 서버 Playwright 401 경로 수정 — `require_console_admin` → `require_session_or_admin` | 1일 |
| S2 | `/browser-bridge/stream/{id}` SSE 엔드포인트 신설 — 스크린샷 JPEG 프레임 push | 3일 |
| S3 | 채팅 아티팩트 React 컴포넌트 — `<img>` SSE 수신 + 명령 입력창 | 3일 |
| S4 | 오비스 콘솔 `/ohvis` 에 아티팩트 연동 버튼 추가 | 1일 |
| S5 | E2E 검증 — 채팅 명령 → 화면 스트림 → 레시피 기록 확인 | 2일 |

**총 예상: 10일**

---

## 4. 방안 B 상세 설계 — 오비스 PC 전용 앱 (Windows 우선, macOS 보류)

### 4-1. 앱 아키텍처

```
[오비스 PC 앱 (Electron)]
    ├── UI 레이어 (React + Tailwind)
    │    ├── 채팅 인터페이스 (오비스 콘솔 내장)
    │    ├── 실제 브라우저 창 (BrowserView 또는 내장 Chromium)
    │    ├── 레시피 관리 패널
    │    └── 인증/설정
    │
    ├── 로컬 실행 엔진
    │    ├── Playwright Node.js 번들 (로컬 Chromium 포함)
    │    ├── 레시피 플레이어 (로컬 실행)
    │    └── 스크린샷 → AADS 서버 전송
    │
    └── AADS 연결 레이어
         ├── WebSocket → aads.newtalk.kr (명령 수신/결과 전송)
         ├── OAuth 인증 (ANTHROPIC_AUTH_TOKEN)
         └── 레시피 DB 동기화 (AADS PostgreSQL)
```

### 4-2. PC 앱 vs PC Agent 비교

| 항목 | 현재 PC Agent | 오비스 PC 앱 |
|---|---|---|
| 설치 방식 | CEO PC 수동 설정 | MSI/DMG 설치파일 배포 |
| 브라우저 창 | CEO 화면에 팝업 | 앱 내 내장 창 |
| AI 처리 | AADS 서버 경유 | AADS 서버 경유 (동일) |
| 레시피 기록 | 미구현 | 클릭/입력 자동 캡처 |
| 다중 사용자 | 불가 | 계정별 앱 설치 |
| 업데이트 | 수동 | 자동 업데이트 |
| 오프라인 재생 | 불가 | 가능 (로컬 레시피) |

### 4-3. 구현 단계

| 단계 | 작업 | 예상 기간 |
|---|---|---|
| P1 | 앱 골격 — Electron + React 보일러플레이트, AADS OAuth 연결 | 1주 |
| P2 | 브라우저 창 내장 — Playwright 번들, BrowserView 연동 | 1주 |
| P3 | 채팅 인터페이스 — `/ohvis` 콘솔 WebView 내장 | 1주 |
| P4 | 레시피 기록기 — 클릭/입력 이벤트 캡처 → AADS DB 저장 | 1.5주 |
| P5 | 레시피 재생기 — AADS에서 수신한 레시피 로컬 실행 | 1주 |
| P6 | 빌드/배포 — Windows(NSIS), 자동 업데이트(electron-updater) | 1주 |

**총 예상: 7주**

---

## 5. 단계별 실행 계획 (권장 C안)

| 순위 | 마일스톤 | 내용 | 기간 | 완료기준 |
|---|---|---|---|---|
| **M0** | 서버 401 해소 | `/browser-bridge` 인증 경로 수정 | 1일 | `curl /browser-bridge/work-sessions/ensure` = 200 |
| **M1** | 채팅 아티팩트 뷰어 | SSE 스트림 + 아티팩트 React 컴포넌트 | 10일 | 채팅창에서 명령 → 브라우저 화면 실시간 표시 |
| **M2** | 레시피 기록·재생 완성 | recorder + player E2E (현재 진행 중) | 진행 중 | `work_recipes ≥1` / `recipe_runs ≥1` |
| **M3** | 오비스 PC 앱 Alpha | Electron 앱 + 브라우저 창 + 채팅 | 4주 | Windows/Mac 설치 후 채팅 → 브라우저 실행 |
| **M4** | 오비스 PC 앱 Beta | 레시피 기록·재생 + 자동 업데이트 | 3주 | 레시피 10건 기록 + 재생 성공률 90% |
| **M5** | PC Agent 완전 대체 | PC 앱으로 모든 브라우저 작업 처리 | 1주 | PC Agent 의존도 0% |

---

## 6. 리스크 및 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| Electron 앱 보안 (인증 토큰 로컬 저장) | 높음 | OS Keychain/Credential Manager 사용, 메모리 전용 저장 |
| Playwright 버전 번들 크기 (~300MB) | 중간 | 별도 다운로더로 분리, 앱 코어는 경량 유지 |
| 채팅 아티팩트 CORS/iframe 제한 | 중간 | SSE 직접 연결, 프록시 없이 처리 |
| M2 레시피 기록기 app.main.py 충돌 | 높음 | runner-1ac68967 non-fast-forward — 재작업 후 재승인 필요 |

---

## 7. 즉시 조치 항목 (이번 주)

1. **M0 서버 401 수정** → `runner-AADS-BROWSER-SERVER-PATH-401-P1` 이미 큐잉됨
2. **M2 레시피 runner-1ac68967** non-fast-forward 재작업 승인
3. **M1 채팅 아티팩트 뷰어** 지시서 작성 후 러너 투입

---

*파일 경로: `/root/aads/aads-server/docs/plans/AADS-SMART-BROWSER-PC-APP-PLAN-20260919.md`*
*버전: v2.0 | 다음 검토: 실행 안전 기반 6종 완료 후*

---

## 8. 사이트 학습·재방문 데이터 책임

사이트를 다시 방문할 때 과거 가격·재고·검색 결과를 답으로 재사용하지 않습니다.
장기 기억은 **구조와 방법**에만 적용하고, 바뀌는 사실은 실행 시점에 다시 읽습니다.

| 계층 | 정본 | 저장 대상 | 금지 대상 |
|---|---|---|---|
| Site Profile | 구조화 DB | origin, 인증 방식, 브라우저 호환성, 위험 등급 | 비밀번호·세션 원문 |
| Page Template | 구조화 DB | 페이지 유형, 안정 영역, ARIA 부분 구조 시그니처 | 전체 DOM 고정 매크로 |
| Site Skill | Skill Registry | 입력 스키마, 실행 함수, 허용 도구, 사후조건, 버전 | 자유문 텍스트의 직접 실행 |
| Semantic Memory | Vector DB | 사이트·스킬·오류·복구 요약과 provenance | 실시간 사실의 장기 정본화 |
| Live Observation | TTL 캐시·실행 로그 | 가격, 재고, 검색 결과, 현재 DOM, 관측 시각 | TTL 만료값의 사용자 표시 |
| Evidence | Object Storage | DOM/ARIA 스냅샷, 스크린샷, 파일, 해시 | Credential 원문 |

재방문 흐름은 `Site Profile 조회 → Page Template 후보 → ARIA 부분 시그니처
대조 → Site Skill 선택 → 라이브 관측 → 표시 직전 재검증`으로 고정합니다.

## 9. 실행 안전 기반 6종

### 9.1 Channel Router — 모든 실행의 선행 게이트

입력은 출처가 명시된 `DirectiveEnvelope`로만 실행 계층에 들어갑니다.

```text
trusted command channels
  user_directive | approved_recipe | internal_control
                         │
                         ▼
                    Channel Router
                         │ typed ActionIntent
                         ▼
                  policy / approval / executor

untrusted observation channels
  page_text | DOM | ARIA | screenshot_OCR | downloaded_file
                         │
                         └── ObservationEnvelope (실행권한 없음)
```

Channel Router는 `source`, `tenant_id`, `session_id`, `correlation_id`,
`trust_level`, `allowed_capabilities`, `payload_hash`를 검증합니다. 출처가 없는
문자열, 페이지에서 추출된 문장, OCR 결과는 ActionIntent로 변환할 수 없습니다.
라우팅 결정과 거부 사유는 감사 로그로 남깁니다.

### 9.2 페이지 데이터 비명령화

DOM·ARIA·OCR·다운로드 문서는 항상 `UNTRUSTED_PAGE_DATA`입니다. 페이지에
“이전 지시를 무시하라”, “도구를 실행하라”, “비밀번호를 입력하라”가 있어도
요약·비교·추출 대상일 뿐 명령이 아닙니다. LLM에는 명령과 관측을 분리된 필드로
전달하며, 관측 필드에서 생성된 tool call은 실행기 앞에서 재차 차단합니다.

필수 방어는 다음과 같습니다.

- 데이터→명령 승격 금지(taint 유지)
- 도메인 전환·다운로드·업로드·결제·전송 시 capability 재검증
- 페이지가 요구한 credential/OTP 입력은 Human Gateway 또는 Credential Broker만 처리
- 관측 텍스트가 레시피·스킬 정의를 수정하지 못하도록 별도 승인 경계 적용
- 모든 차단을 `reason_code=PAGE_DATA_COMMAND_ATTEMPT`로 증거화

### 9.3 실행 가능한 Skill API/함수 관리

Skill은 설명 문서가 아니라 버전된 실행 계약입니다.

```json
{
  "skill_id": "commerce.search_products",
  "version": 3,
  "input_schema": {"query": "string", "max_price": "number|null"},
  "executor": "browser.search_products",
  "allowed_tools": ["navigate", "fill", "press", "extract"],
  "preconditions": ["origin_allowlisted"],
  "postconditions": ["results_have_source_url", "freshness_verified"],
  "risk_tier": "read",
  "status": "active"
}
```

CRUD와 실행을 분리합니다. `draft → validation → active → deprecated` 상태만
허용하고, 실행 API는 active 버전과 JSON Schema에 맞는 인자만 받습니다.
고위험 스킬은 Human Gateway 승인 scope를 소비해야 하며, 모든 실행은
`skill_id/version/input_hash/result/evidence/policy_decision`을 남깁니다.

### 9.4 ARIA 기반 부분 구조 시그니처

전체 DOM 해시는 광고·추천·A/B 테스트 때문에 너무 자주 깨집니다. 검색 폼,
필터 패널, 결과 목록처럼 **업무에 필요한 부분 트리**만 서명합니다.

정규화 항목은 `role`, accessible name, 필수 상태(`checked/expanded/selected`),
상대적 부모-자식 관계, 안정 data attribute입니다. 동적 id, 가격, 재고, 광고,
시간, 순서가 자주 바뀌는 형제는 제외합니다. 일치도는 부분 트리별 점수로 계산해
임계값 아래면 자동 실행하지 않고 재탐색 또는 Human Gateway로 보냅니다.

### 9.5 표시 직전 실시간 사실 재검증

가격·재고·검색 순위·배송일·운영 상태처럼 변동 가능한 값은 최초 추출 시각이
아니라 **사용자에게 표시하기 직전** 다시 확인합니다. 응답에는 `observed_at`,
`revalidated_at`, `source_url`, `evidence_id`, `freshness_status`를 붙입니다.
재검증 실패·불일치·TTL 만료 시 이전 값을 확정형으로 표시하지 않고
`STALE/CONFLICT/UNAVAILABLE`로 반환합니다.

### 9.6 Golden Task·회귀 테스트 기반 승격

자동 학습 결과는 즉시 active가 되지 않습니다. 새 Page Template/Skill/복구
버전은 Golden Task와 기존 회귀 묶음을 통과해야 승격됩니다.

| 게이트 | 필수 판정 |
|---|---|
| 보안 | 페이지 데이터가 tool call로 승격되는 adversarial case 0건 |
| 기능 | 성공·빈 결과·로그인 만료·selector 변경 Golden Task 통과 |
| 구조 | ARIA 부분 시그니처 허용 변경 통과, 핵심 영역 변경 차단 |
| 사실성 | TTL 만료·값 변경 시 재검증/충돌 표시 통과 |
| 회귀 | 기존 active skill의 기준 성공률·비용·시간 임계치 비퇴행 |
| 감사 | recipe/skill/version/evidence/policy decision 추적 가능 |

승격은 `candidate → shadow → active` 순서이며, 실패하면 기존 active 버전을
유지하고 candidate만 격리합니다. 롤백은 직전 active 버전 포인터 전환으로
완료되어야 합니다.

## 10. 우선순위와 의존성

| 순위 | 구현 마일스톤 | 선행 | 완료 기준 |
|---|---|---|---|
| P0 | G1 Channel Router | M3 | 출처 없는/페이지 유래 명령 100% 차단, 감사로그 생성 |
| P0 | G2 페이지 데이터 비명령화 | G1 | prompt-injection Golden case에서 tool call 0건 |
| P0 | G3 Skill Registry API/함수 실행 | G2 | active skill만 스키마 검증 후 실행, 버전·증거 기록 |
| P1 | G4 ARIA 부분 구조 시그니처 | G3 | 동적 영역 변화 허용·핵심 구조 변화 차단 |
| P0 | G5 표시 직전 사실 재검증 | G4 | TTL 만료/값 변경을 STALE·CONFLICT로 표시 |
| P0 | G6 Golden Task 승격 게이트 | G5 | candidate→shadow→active 및 자동 롤백 E2E |

기존 실패 복구·레시피 정본·사이트 학습·검색 라우팅·아티팩트 UI·출시
마일스톤은 위 G1~G6를 선행 조건으로 둡니다. 이렇게 해야 학습 기능을 먼저
만들었다가 나중에 보안 경계를 덧대는 재작업을 피할 수 있습니다.
