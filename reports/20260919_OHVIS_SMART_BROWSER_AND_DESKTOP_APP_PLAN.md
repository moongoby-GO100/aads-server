# 오비스 스마트브라우저 채팅창 내 실행 + 데스크톱 전용앱 기획서

- 작성: 2026-09-19 08:4x KST / CTO AI
- 지시: CEO — "채팅창에서 바로 진행 / 오른쪽 아티팩트에 Playwright 스마트브라우저 / 꼭 PC에이전트가 필요하면 Windows·Mac 전용 오비스 앱을 만들어 동일 작동"
- **범위 변경 (2026-09-19 CEO): "맥은 일단 보류하자." → 데스크톱앱은 Windows 단독. macOS 항목은 7.1 보류 표로 내렸다.**
  아래 본문에 남아 있는 macOS 서술은 *보류 시점의 판단 근거*로 보존한 것이며 착수 대상이 아니다.
- TASK_ID: AADS-OHVIS-SMARTBROWSER-DESKTOP-PLAN

---

## 0. 결론 (먼저)

셋 다 **된다. 새로 만들 것보다 이미 있는 것을 연결하는 쪽이 크다.**

| 질문 | 답 | 근거(실측) |
|---|---|---|
| 채팅창에서 바로 브라우저 작업이 되나 | **된다.** 서버 Playwright 라이브뷰가 이미 한 번 성공했다 | `browser_tasks` 3건 중 `ohvis-live-view-verify` = completed, `verification=self_hosted_playwright_live_frame`, frame 1366x768 [DB 조회] |
| 오른쪽 아티팩트에 붙일 수 있나 | **된다. 단, 지금은 탭이 없다** | `ArtifactTabs.tsx` 탭 7종(보고서/코드/미리보기/차트/대시보드/AI Drive/작업) — 브라우저 탭 부재 [코드 실측] |
| PC 에이전트가 꼭 필요한가 | **대부분 불필요. 은행·인증서·로컬 PC 한정** | `browser_task_gateway.py` 이미 `primary_runtime=self_hosted_playwright`, `fallback=pc_agent` 로 라우팅 설계됨 (L583~623) |
| Windows/Mac 전용 앱이 가능한가 | **Windows는 이미 있다. Mac이 0이다** | PyInstaller `kakaobot-setup-1.0.73.exe` 존재, `darwin/macos` 문자열 0건 [파일 실측] |

**핵심 진단 — 기능이 없는 게 아니라 경로가 끊겨 있다.**
`browser_tasks` 마지막 기록이 **2026-08-28** 이다. 3주간 서버 브라우저 경로가 한 번도 쓰이지
않았고, 그동안 모든 브라우저 작업이 PC Agent 창으로 갔다. 원인은 서버 경로 401 (현재
`runner-d10a349a` 가 수정 중).

---

## 1. 지금 구조 (실측)

```
채팅 세션
  └─ browser_task_gateway.py (1,417줄)
       ├─ classify_playwright_access()  ← 접근 가능성 판정
       ├─ _probe_self_hosted_playwright_access()  ← 서버 Playwright 시도  ★여기서 401로 실패 중
       └─ pc_agent_manager.execute_routed_command()  ← 폴백. 지금은 사실상 이쪽만 산다
```

| 구성요소 | 위치 | 상태 |
|---|---|---|
| 서버 Playwright | 호스트 + `aads-server` 컨테이너 양쪽 import OK | ✅ 설치됨 [실측] |
| 라우팅 게이트웨이 | `app/services/browser_task_gateway.py` | ✅ 구현됨, 폴백 설계 완료 |
| 권한 정책 | `app/services/browser_permission_policy.py` (140줄) | ✅ 존재 |
| 레시피 레지스트리 | `app/services/browser_recipe_registry.py` | ✅ 존재 |
| API | `app/api/browser_tasks.py`, `browser_bridge.py`, `browser_recipes.py` | ✅ 존재 |
| 전용 화면 | `src/app/browser-tasks/page.tsx` | ⚠️ 채팅과 분리된 별도 route |
| **아티팩트 브라우저 탭** | — | ❌ **없음. 이번 기획의 1순위 공백** |
| PC Agent (Windows) | `pc_agent/` v1.0.73, `install.bat`, PyInstaller | ✅ 온라인 (`oby-ceo`, Win10 26200, heartbeat 7.8초) |
| PC Agent (macOS) | — | ❌ 없음 |

---

## 2. 목표 아키텍처 — 3계층 폴백

사용자는 계층을 고르지 않는다. **게이트웨이가 고르고, 화면에는 "지금 어디서 돌고 있는지"만 뜬다.**

| 계층 | 실행 위치 | 담당 작업 | 사용자 체감 |
|---|---|---|---|
| **L1 서버 Playwright** (기본) | contabo116 컨테이너 | 공개 페이지, 로그인 불요 확인, 렌더 검증, 캡처, 크롤링, 사내 서비스 | 채팅 오른쪽 아티팩트에 화면이 뜬다. 설치 0 |
| **L2 오비스 데스크톱앱** (신규, **Windows만** — macOS 보류) | CEO PC (Windows) | 이미 로그인된 브라우저 세션 재사용, 로컬 파일, 프린터, 트레이 | 앱만 켜 두면 채팅에서 동일하게 지시 |
| **L3 레거시 PC Agent** | Windows only | 은행·공인인증서·보안프로그램(안랩/베라포트) | 현행 유지. L2가 흡수할 때까지 |

**L1이 못 하는 것만 L2로 내려간다.** 판정은 사람이 아니라 `classify_playwright_access()` 가 한다.

---

## 3. A. 채팅 아티팩트 스마트브라우저 (L1)

### 3.1 무엇을 만드나

`ArtifactTabs.tsx` 에 **8번째 탭 `{ id: "browser", icon: "🌐", label: "브라우저" }`** 를 추가하고,
`ArtifactBrowserView.tsx` 를 새로 만든다.

| 구역 | 내용 |
|---|---|
| 상단 | 현재 URL + 실행 런타임 배지(`서버` / `내 PC` / `PC Agent`) + 중지 버튼 |
| 본문 | 라이브 프레임 (스냅샷 폴링 or SSE 프레임 푸시). 이미 1366x768 프레임 수신 실적 있음 |
| 하단 | **단계 내러티브** — "1. 로그인 페이지 진입 → 2. 아이디 입력 → 3. 대기 중" |
| 우하단 | 승인 필요 시 인라인 승인 버튼 (`requires_approval`, `approval_request_id` 컬럼 이미 존재) |

### 3.2 왜 지금 안 보였나

1. **서버 경로 401** — 게이트웨이가 L1을 시도했다가 인증 실패 → 조용히 L2/L3으로 폴백.
   사용자 눈에는 "왜 PC 창이 뜨지?" 로만 보였다. → `runner-d10a349a` 진행 중.
2. **폴백이 조용하다** — 어느 계층으로 내려갔는지 화면에 안 뜬다. **배지로 노출해야 한다.**
3. **단계 응답 부재** — → `runner-ded582f7` (AADS-BROWSER-STEP-NARRATION-P1) 로 제출됨.

### 3.3 재사용 대상 (새로 만들지 않는다)

- 백엔드: `browser_task_gateway.py`, `browser_tasks.py` API, `browser_tasks` 테이블 그대로.
- 프론트: `src/app/browser-tasks/page.tsx` 의 라이브뷰 로직을 컴포넌트로 추출해 아티팩트 탭이 재사용.
  → 별도 화면과 채팅 아티팩트가 **같은 컴포넌트**를 쓰게 한다. 두 벌로 유지하면 한쪽이 반드시 낡는다.

---

## 4. B. 오비스 데스크톱앱 (L2) — **Windows 단독** (macOS 보류)

> **2026-09-19 CEO 결정: "맥은 일단 보류하자."**
> 이 장의 범위는 Windows 하나다. macOS 관련 항목(M5, Apple 공증, 화면기록 권한 플로우)은
> 착수하지 않고 8장 "보류 항목"으로 내려둔다. 설계는 크로스플랫폼을 깨지 않는 선까지만 유지한다 —
> 나중에 Mac을 풀 때 재작성하지 않기 위함이고, 지금 Mac 때문에 Windows 일정을 늘리지는 않는다.

### 4.1 설계 원칙

**"앱은 껍데기, 두뇌는 서버."** 앱은 로컬 실행 권한만 빌려주는 얇은 브릿지다.
지시·판단·로그는 전부 서버(contabo116)에 남는다. 앱 버전이 달라도 동작이 갈리지 않게 하기 위함이다.

### 4.2 기술 선택

| 항목 | 선택 | 사유 | 비권장안 |
|---|---|---|---|
| 셸 | **Electron** | 기존 Next.js 대시보드 UI를 거의 그대로 얹는다. Chrome CDP 제어가 1급 | Tauri(번들 작지만 Python sidecar·CDP 통합 재작업 큼) |
| 로컬 코어 | **기존 `pc_agent/agent.py` 를 sidecar 로 동봉** | 21,000줄급 자산 재사용. 프로토콜(WebSocket) 그대로 | 전면 재작성(공수 3배, 회귀 위험) |
| 브라우저 | 사용자 기존 Chrome을 **CDP 부착** | 이미 로그인된 세션 재사용 = L2의 존재 이유. `chrome_cdp` capability 이미 보유 | 앱 내장 Chromium(로그인 다시 해야 함 → 무의미) |
| 통신 | 현행 WebSocket(`device_list`/`device_execute`) 유지 | 서버측 무변경 | 신규 프로토콜 |
| 업데이트 | electron-updater + 서버 VERSION 파일 | 현행 `kakaobot-setup.exe.version` 체계 승계 | 수동 재설치 |

### 4.3 화면 (최소)

L1-원칙 "첫 진입은 핵심 업무로" 적용 — **설정이 아니라 상태가 첫 화면이다.**

1. **상태 1화면**: 연결됨/끊김, 연결된 서버, 지금 실행 중인 작업, 마지막 오류, 재시도 버튼.
2. **채팅**: 대시보드 채팅을 그대로 임베드. 앱에서 지시해도 서버 세션은 동일.
3. **권한(보조 메뉴)**: 어떤 사이트에 로컬 제어를 허용할지. 기본은 전부 차단, 화이트리스트 추가식.
4. 트레이 상주 + 자동 시작 (현행 `install_service.py` 승계).

### 4.4 Windows 범위 확정 (macOS는 보류 — 착수하지 않음)

| 항목 | Windows (이번 범위) | macOS (보류) | 보류 시 조치 |
|---|---|---|---|
| Chrome 제어 | CDP 9222 | 동일 코드로 동작 예상 [미검증] | 플랫폼 분기 하드코딩 금지 — 나중에 그대로 켜지게 둔다 |
| 자동 시작 | 서비스/시작프로그램 | LaunchAgent | Electron `setLoginItemSettings` 추상화만 유지 |
| 화면 캡처 권한 | 불요 | 화면기록 권한 플로우 필요 | **구현하지 않는다** (M5 보류) |
| 서명 | 코드서명 인증서 | Apple 공증 필수 | **인증서 구매 안 함** — 연 $99 지출 보류 |
| 은행·인증서 | 가능 | 대부분 불가(보안프로그램 미지원) | Mac을 풀어도 은행은 Windows 전용 유지 |

> **보류 결정의 근거(2026-09-19 CEO).** Mac을 넣으면 Apple 공증 인증서 구매·권한 플로우·별도
> 빌드 파이프라인이 M4 뒤에 붙어 Windows 베타가 늦어진다. Mac에서 얻는 것은 "일반 웹 자동화 +
> 로컬 파일 + 이미 로그인된 세션"뿐이고, 그 대부분은 L1(서버 Playwright)이 이미 덮는다.
> **단, 코드에 `win32` 전제를 박지 않는다.** 보류는 포기가 아니라 순서 문제다.

---

## 5. 마일스톤

| M | 내용 | 산출물 | 선행 | 규모 |
|---|---|---|---|---|
| **M1** | 서버 Playwright 401 해소 + 런타임 배지 노출 | L1 경로 복구, `browser_tasks` row 증가 | `runner-d10a349a` | S |
| **M2** | 아티팩트 `브라우저` 탭 + 라이브뷰 컴포넌트 추출 | `ArtifactBrowserView.tsx`, 탭 8종 | M1 | M |
| **M3** | 단계 내러티브 + 인라인 승인 | 단계 이벤트 ≥3건, 승인 버튼 | `runner-ded582f7` | S |
| **M4** | 오비스 데스크톱앱 Windows 베타 | Electron 셸 + agent sidecar + 상태화면 | M2 | L |
| ~~**M5**~~ | ~~macOS 빌드 + 공증~~ | — | — | **⏸ 보류 (2026-09-19 CEO)** |

**M1~M3 까지가 CEO 질문의 본론("채팅창에서 바로")이다. M4(Windows)가 그 다음이고,
M5(macOS)는 보류다 — 마일스톤 등록에서 제외하고 `goals` 진행률 분모에도 넣지 않는다.**

---

## 6. 리스크

| # | 리스크 | 영향 | 완화 |
|---|---|---|---|
| 1 | L1 401이 안 풀리면 M2~M3 전부 헛것 | 치명 | M1 완료를 M2 착수 게이트로 고정 |
| 2 | 조용한 폴백 재발 | 원인 추적 불가 | 런타임 배지 + `browser_tasks.result.metadata.runtime` 필수 기록 |
| ~~3~~ | ~~Apple 공증 인증서 미보유~~ | — | **해소(회피)** — macOS 보류로 인증서 지출·리스크 모두 제거 |
| 4 | Electron 앱이 기존 PC Agent와 이중 접속 | 명령 중복 실행 | 동일 `agent_id` 배타 lease. 신규 앱 접속 시 레거시 세션 강제 종료 |
| 5 | 라이브뷰 프레임 대역폭 | 채팅 지연 | 폴링 1~2fps + 변경분만. 전체 영상 스트리밍 금지 |

---

## 7. 검증 기준 (완료 판정)

- **M1**: `browser_tasks` 에 2026-09-19 이후 row 생성 + `result.metadata.runtime = self_hosted_playwright`.
- **M2**: 채팅에서 "aads.newtalk.kr 열어봐" → 오른쪽 아티팩트에 프레임 표시 + 캡처 파일 생성.
- **M3**: 한 작업에 단계 이벤트 ≥3건, 각 단계가 채팅 버블로 회신.
- **M4**: Windows 앱 설치 후 `device_list` 에 신규 `capabilities` 로 online, 채팅 지시 1건 왕복 성공.
- ~~**M5**: macOS 앱 실행 → 화면기록 권한 승인 → 동일 지시 1건 왕복 성공.~~ → **보류. 완료 판정 대상 아님.**

### 7.1 보류 항목 (2026-09-19 CEO 결정)

| 항목 | 상태 | 되살리는 조건 |
|---|---|---|
| macOS 데스크톱앱(M5) | ⏸ 보류 | CEO가 Mac 사용을 지시하거나, Apple Developer($99/년) 승인이 날 때 |
| Apple 공증 인증서 구매 | ⏸ 보류 | 위와 동일 |
| macOS 화면기록 권한 플로우 | ⏸ 보류 | 위와 동일 |

보류 항목은 **코드에 플랫폼 하드코딩을 만들지 않는 선까지만** 지킨다. 그 이상은 지금 하지 않는다.

각 M은 **화면 캡처 또는 DB row** 없이 완료로 보고하지 않는다 (R-E2E).

---

## 8. 교훈

- **기능이 없는 것과 경로가 끊긴 것을 구분하라.** 서버 Playwright 라이브뷰는 2026-08-28에 이미
  성공했다. 3주간 안 쓰인 이유는 401이었고, 폴백이 조용해서 아무도 몰랐다.
- **조용한 폴백은 버그다.** 어느 계층에서 돌았는지 화면에 남지 않으면 사용자는 "왜 PC 창이 뜨지"
  라고만 느끼고, 에이전트는 원인을 3주간 못 찾는다. **폴백에는 반드시 표식을 남긴다.**
- **Mac에서 은행이 된다고 적지 않는다.** 검증 못 한 것을 기획서에 적으면 그게 다음 사고의 근거가 된다.
