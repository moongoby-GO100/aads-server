# 스마트 브라우저 실행 아키텍처 기획서
**AADS-SMART-BROWSER-PC-APP-PLAN-20260919**  
작성: 2026-09-19 KST | 작성자: CTO AI (오비스) | 담당: CEO moongoby 승인

---

## 요약

현재 스마트 브라우저는 **서버 헤드리스 전용 → 401 차단 → PC Agent 우회** 구조로,  
CEO PC에 PC Agent가 없으면 브라우저 작업이 전혀 실행되지 않는 단일 장애점입니다.

**권장 방향**: 3단계 아키텍처로 전환합니다.
1. **즉시(M1)** — 채팅 아티팩트에 브라우저 스트림 뷰어 + 명령 입력 패널
2. **단기(M2)** — 서버 Playwright 401 인증 경로 개방 (백엔드 핫픽스)
3. **중기(M3)** — 오비스 PC 전용 앱(Windows/Mac) 출시 — PC Agent 완전 대체

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

## 4. 방안 B 상세 설계 — 오비스 PC 전용 앱 (Windows/Mac)

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
| P6 | 빌드/배포 — Windows(NSIS), Mac(DMG), 자동 업데이트(electron-updater) | 1주 |

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

*파일 경로: `/root/aads/docs/plans/AADS-SMART-BROWSER-PC-APP-PLAN-20260919.md`*  
*버전: v1.0 | 다음 검토: M0 완료 후*
