# 로그인 필요 사이트 데이터 수집 플랫폼 기획 보고서

작성: 2026-09-03 14:12 KST
프로젝트: AADS
작성 목적: API를 확보하지 못한 온라인 사이트 중 로그인/세션/인증이 필요한 사이트의 데이터를 안정적으로 수집하기 위한 PC Agent 대체/보강 아키텍처 검토

## 1. 결론

현재 AADS PC Agent는 이미 Windows EXE, WebSocket 명령, Chrome CDP, PyAutoGUI/Win32 입력, work_key 기반 세션 분리 기능을 갖고 있다. 따라서 구현이 어려운 핵심 원인은 "윈도우 프로그램이 없어서"가 아니라, 범용 원격조작 에이전트 하나가 로그인 사이트별 인증 상태, 탭 선택, DOM 변경, 다운로드, OTP/CAPTCHA, 보안 차단까지 모두 처리하려 하기 때문이다.

권장 방향은 PC Agent를 버리는 것이 아니라, 역할을 세 단계로 분리하는 것이다.

1. AADS 서버: 작업 큐, 권한, 감사로그, 저장소, 스케줄링 담당
2. Windows Collector App: 사이트별 로그인 세션 보존, WebView2/Chrome 제어, 로컬 보안 프로그램/다운로드/파일 접근 담당
3. Site Recipe/Extension Layer: 사이트별 DOM 파싱, 다운로드 버튼, 표준화 규칙 담당

최종 제품 방향은 "모두싸인처럼 별도 서비스"가 아니라 "로그인 사이트 자료수집용 Windows Collector + AADS Cloud Control Plane"이다. 즉 설치형 Windows 프로그램과 클라우드 대시보드가 결합된 하이브리드 수집 플랫폼으로 가야 한다.

## 2. 현재 AADS 실측

| 항목 | 확인 결과 | 근거 |
|---|---|---|
| PC Agent 존재 | 있음. `pc_agent/`에 EXE 빌드, 트레이, 런처, 업데이트, 명령 모듈 존재 | `pc_agent/requirements.txt`, `pc_agent/build.py`, `pc_agent/agent.py` |
| 브라우저 자동화 | Chrome CDP 기반. `browser_launch`가 work_key별 포트/프로필을 할당 | `pc_agent/commands/browser_auto.py` |
| 세션 충돌 보강 이력 | v1.0.64~v1.0.69가 모두 탭 오조작, work_key, stale target, 은행/타 포털 혼선 수정 | `pc_agent/CHANGELOG` |
| 수집 큐 | `delivery`, `bank`, `financial`, `browser_recipe` 큐 타입 존재 | `app/services/pc_agent_collection_queue.py` |
| 보안/챌린지 정책 | OTP/CAPTCHA, 보안문자, 차단 감지와 승인 토큰 흐름 존재 | `app/services/browser_task_gateway.py` |
| 기존 설계 문서 | 멀티서비스 충돌, 판매채널 자동수집 아키텍처 문서 존재 | `docs/plans/AADS-PC-AGENT-MULTI-SERVICE.md`, `docs/plans/20260821_SALES_CHANNEL_PC_AGENT_COLLECTION_ARCHITECTURE.md` |

해석: 이미 필요한 부품은 많지만, "사이트별 수집 제품"이 아니라 "원격 PC 조작 도구" 중심으로 커져 있다. 그래서 새로운 사이트를 추가할 때마다 브라우저 세션, 선택자, 인증, 다운로드, 예외 처리가 한 덩어리로 얽힌다.

## 3. 최신 기술/도구 근거

| 분야 | 최신 근거 | 설계 반영 |
|---|---|---|
| Playwright CDP | Playwright는 `connectOverCDP`/`connect_over_cdp`로 기존 Chromium 브라우저에 붙을 수 있으나 Chromium 기반에 한정된다. [Playwright BrowserType, crawled 2026-09-03](https://playwright.dev/docs/api/class-browsertype) | 현재 AADS CDP 방식은 유지 가능하지만 Chrome/Edge 계열 중심으로 한정해야 한다. |
| Chrome Extension | Chrome content script는 방문 페이지의 DOM을 읽고 수정하며 확장 프로그램과 메시지를 주고받을 수 있다. [Chrome Content Scripts, 2026-09-03 확인](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts) | 마우스/좌표 자동화보다 확장 프로그램 DOM 추출이 안정적이다. |
| Native Messaging | Chrome 확장 프로그램은 Native Messaging Host를 통해 로컬 앱과 stdin/stdout으로 통신할 수 있다. [Chrome Native Messaging, 2026-09-03 확인](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging) | "Chrome Extension + Windows Collector" 조합으로 로그인 세션을 그대로 활용할 수 있다. |
| WebView2 | WebView2는 Microsoft Edge 렌더링 엔진을 네이티브 앱에 임베드하고, 웹/네이티브 상호작용을 제공한다. [Microsoft WebView2 개요, 2025-07-18](https://learn.microsoft.com/en-us/microsoft-edge/webview2/) | 별도 Windows 프로그램 안에 사이트별 브라우저를 격리해 넣는 방식이 가능하다. |
| WebView2 배포 | WebView2 앱 배포 시 클라이언트 PC에 WebView2 Runtime이 필요하다. [Microsoft WebView2 배포, 2025-10-15](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution) | 설치 프로그램에서 Runtime 존재 여부를 점검해야 한다. |
| Power Automate Desktop | Power Automate Desktop은 브라우저 자동화와 웹 페이지 데이터 추출 학습 모듈을 제공한다. [Microsoft Learn, 2026-09-03 확인](https://learn.microsoft.com/en-us/training/modules/pad-web/) | 빠른 PoC에는 유용하지만 AADS 자체 제품화에는 종속성이 크다. |
| 개인정보/스크래핑 | 개인정보위는 공개 개인정보의 AI 활용에서 정당한 이익 요건을 언급했고, 개인정보 처리방침 작성지침과 마이데이터 전송 절차 안내를 운영한다. [개인정보위 보도자료 2024-07-17](https://m.pipc.go.kr/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&mCode=C020010000&nttId=10362), [개인정보 처리방침 작성지침 2025-04-21](https://www.privacy.go.kr/front/bbs/bbsView.do?bbsNo=BBSMSTR_000000000049&bbscttNo=20806) | 로그인 후 접근 데이터는 특히 권한, 목적, 보유기간, 감사로그, 약관 검토가 필수다. |

주의: 이 보고서는 기술/제품 기획이며 법률 자문이 아니다. 실제 상용 수집 대상은 사이트 약관, 계약, 개인정보 처리근거, 저작권, 접근권한을 별도 검토해야 한다.

## 4. 선택 가능한 구현 방식

| 방식 | 설명 | 장점 | 단점 | 판정 |
|---|---|---|---|---|
| A. 기존 PC Agent 고도화 | 현재 Chrome CDP + PyAutoGUI + WebSocket 구조 계속 확장 | 기존 자산 재사용, 빠른 적용 | 사이트 추가마다 충돌 증가, 화면/좌표/탭 혼선 반복 | 단기 보강용 |
| B. Windows Collector App + WebView2 | 사이트별 로그인 브라우저를 앱 내부에 격리하고 JS Bridge로 DOM/다운로드 수집 | 세션 격리, UX 통제, 제품화 쉬움 | WebView2 쿠키/프로필 관리, 보안 프로그램 호환성 검증 필요 | 1순위 |
| C. Chrome Extension + Native Host | 사용자가 실제 Chrome에 로그인하고 확장 프로그램이 허용 도메인의 DOM/다운로드를 수집 | 기존 로그인 세션 활용, DOM 접근 안정, 사이트별 권한 명확 | 확장 설치/권한 승인 필요, Chrome 정책 변화 대응 필요 | 1.5순위 |
| D. Power Automate Desktop/RPA | Microsoft RPA로 화면 자동화와 데이터 추출 구성 | PoC 빠름, 비개발자 수정 가능 | 라이선스/운영 종속, 대량 멀티테넌트 제품화 부적합 | PoC/임시 운영 |
| E. 공식 API/제휴/CSV 업로드 | 공식 API, 엑셀/CSV 다운로드, 수동 업로드 병행 | 법적/운영 리스크 낮음 | 사이트별 API 확보 필요, 완전 자동화 제한 | 항상 병행 |
| F. 원격 브라우저 클라우드 | Browserless/Cloudflare Browser 등 원격 브라우저에 Playwright 연결 | 서버형 확장성 좋음 | 로그인/기기인증/IP 신뢰가 필요한 한국 포털에 약함 | 보조 |

## 5. 권장 아키텍처

```text
AADS Dashboard
  -> Collection Job Queue
  -> Permission/Audit/Policy Engine
  -> Windows Collector App
      -> Site Profile Manager
      -> WebView2 Host 또는 Chrome Extension Native Host
      -> Site Recipe Runtime
      -> Download/OCR/File Capture
      -> Local Secret/Session Store
  -> Normalizer
  -> Canonical Ledger / PostgreSQL / Sheets / ERP
  -> Monitoring / Retry / Action Required UI
```

### 핵심 설계 원칙

1. 수집 실행 단위는 `site_key + account_id + work_key + record_type`으로 고정한다.
2. 로그인 세션은 사이트별 프로필에 보존하고, 일반 사용자 Chrome과 분리한다.
3. 파싱은 좌표/이미지보다 DOM/다운로드/공식 export를 우선한다.
4. OTP, CAPTCHA, 본인인증은 자동 우회하지 않는다. 감지, 알림, 사용자 입력, 같은 세션 재개만 지원한다.
5. HTML 원본과 비밀번호는 장기 저장하지 않는다. 필요한 결과 row, screenshot evidence, 해시, 로그만 보존한다.
6. 사이트별 로직은 `recipe`로 분리한다. 앱 본체를 재배포하지 않고 recipe만 업데이트 가능해야 한다.

## 6. Windows Collector App 제품 설계

### 6.1 기능

| 모듈 | 기능 |
|---|---|
| 로그인 세션 관리 | 사이트별 프로필 생성, 로그인 상태 확인, 세션 만료 감지 |
| 수집 레시피 실행 | URL 이동, selector wait, table extraction, download click, pagination |
| 사용자 개입 | OTP/CAPTCHA/약관 동의 발생 시 화면 표시, 입력 후 재개 |
| 보안 | 로컬 암호화 저장, 민감값 마스킹, 도메인 allowlist, clipboard 보호 |
| 관측성 | 실행 단계 로그, screenshot evidence, 네트워크 실패, selector 실패 분리 |
| 업데이트 | Collector 본체 자동 업데이트, recipe hot reload |
| AADS 연동 | WebSocket 또는 HTTPS polling으로 job 수신/결과 업로드 |

### 6.2 기술 스택 후보

| 스택 | 추천도 | 이유 |
|---|---:|---|
| .NET 8/9 + WebView2 + MSIX/Wix installer | 높음 | Windows 네이티브 배포, WebView2 제어, 장기 유지보수에 유리 |
| Electron + Playwright/CDP | 중간 | 개발 속도 빠르지만 설치 용량/리소스가 큼 |
| Python PyInstaller 현행 유지 | 중간 | 기존 PC Agent와 호환되나 제품형 UI/세션 격리/업데이트에서 한계 |
| Chrome Extension + Native Messaging Host | 높음 | 로그인된 실제 Chrome 세션을 활용해야 하는 사이트에 강함 |

권장 조합은 `.NET WebView2 Collector`를 본체로 만들고, 기존 Chrome 세션이 반드시 필요한 사이트에는 `Chrome Extension + Native Host`를 옵션으로 붙이는 방식이다.

## 7. Site Recipe 구조

```json
{
  "site_key": "baemin",
  "origin": "https://example.com",
  "login_check": {
    "url": "https://example.com/dashboard",
    "success_selectors": ["[data-testid='dashboard']"],
    "auth_required_markers": ["로그인", "비밀번호"]
  },
  "records": {
    "sales": {
      "entry_url": "https://example.com/sales",
      "steps": [
        {"type": "click", "selector": "#date-range"},
        {"type": "set_date", "from": "{{date_from}}", "to": "{{date_to}}"},
        {"type": "extract_table", "selector": "table"}
      ],
      "columns": {
        "date": ["일자", "date"],
        "amount": ["금액", "amount"],
        "order_id": ["주문번호", "order_id"]
      }
    }
  }
}
```

Recipe는 코드가 아니라 데이터로 관리해야 한다. 단, 복잡한 사이트는 Python/TypeScript 플러그인 형태의 advanced adapter를 허용한다.

## 8. 왜 이 방식이 더 나은가

| 현재 문제 | 기존 PC Agent 방식 | 권장 방식 |
|---|---|---|
| 탭 오조작 | CDP target 선택 보강을 계속 추가 | 사이트별 WebView2/프로필 격리 |
| 로그인 세션 만료 | 명령 실패 후 재시도 | 세션 상태를 앱 UI에서 지속 표시 |
| CAPTCHA/OTP | 실패 로그 또는 action_required | 앱이 사용자 입력을 받고 같은 세션에서 재개 |
| 사이트 DOM 변경 | 코드 수정/배포 필요 | recipe 업데이트/fixture 테스트 |
| 다운로드 파일 | 브라우저 다운로드 감지 로직 필요 | 앱이 다운로드 폴더와 파일 해시 직접 관리 |
| 제품화 | 내부 도구 느낌 | 설치형 Collector + SaaS 대시보드로 판매 가능 |

## 9. MVP 범위

### Phase 0: 대상 선정

1. 로그인 사이트 3개 선정
2. 각 사이트의 수집 데이터 정의: 매출, 주문, 리뷰, 정산, 상품, 광고 등
3. 약관/계정권한/개인정보 포함 여부 체크
4. CSV/export 가능 여부 우선 확인

### Phase 1: Collector MVP

1. Windows Collector App 설치/로그인
2. AADS와 WebSocket 연결
3. WebView2 사이트 프로필 3개 생성
4. 로그인 상태 감지
5. DOM table extraction + download capture
6. action_required 화면과 재개 버튼

### Phase 2: Recipe Runtime

1. JSON recipe 스키마
2. selector/marker fixture 테스트
3. normalize mapping
4. failed/partial/no_records 분리
5. recipe 버전 관리와 롤백

### Phase 3: 운영화

1. 스케줄 수집
2. 결과 원장 upsert
3. 대시보드 상태판
4. 실패 스크린샷
5. 사용자 승인 토큰
6. 보안/감사 로그

## 10. 완료 기준

| 우선순위 | 완료 기준 | 검증 |
|---|---|---|
| P0 | Windows Collector가 AADS에 등록되고 heartbeat 유지 | 10분 연결 유지, 재시작 후 자동 재연결 |
| P0 | 사이트별 세션이 일반 Chrome과 분리 | WebView2 profile path 또는 extension origin 권한 확인 |
| P0 | 3개 사이트에서 로그인 상태 감지 | success/auth_required/challenge_required 상태 분리 |
| P0 | 1개 record_type 실제 수집 | DB row count, source hash, 중복 재수집 row count 불변 |
| P1 | OTP/CAPTCHA 발생 시 사용자 개입 후 재개 | 같은 work_key/session_id로 이어서 성공 |
| P1 | recipe 변경만으로 selector 수정 | 앱 본체 재배포 없이 fixture 테스트 통과 |
| P1 | 민감정보 마스킹 | 로그/스크린샷/DB에 비밀번호·토큰 미노출 |

## 11. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 사이트 약관 위반 | 계정 차단/법무 리스크 | 대상별 약관 검토, 공식 API/제휴/CSV 우선 |
| 개인정보 처리 | 개인정보보호법 리스크 | 목적 제한, 최소수집, 보유기간, 접근로그, 삭제권 |
| CAPTCHA/OTP 자동 우회 | 보안 우회 리스크 | 자동 풀이 금지, 사용자 입력과 재개만 지원 |
| DOM 변경 | 수집 실패 | fixture, selector fallback, recipe 버전 관리 |
| 윈도우 환경 차이 | 설치/동작 실패 | WebView2 runtime 체크, 진단 리포트, 자동 업데이트 |
| 보안 프로그램 충돌 | 은행/공공/쇼핑몰 수집 실패 | PC Agent native fallback, 수동 업로드 fallback |

## 12. 권장 의사결정

즉시 방향은 다음과 같다.

1. 현재 PC Agent는 폐기하지 않는다. AADS 제어/명령/원격진단 계층으로 유지한다.
2. 로그인 사이트 파싱 전용으로 Windows Collector App을 별도 제품처럼 만든다.
3. 첫 구현은 `.NET + WebView2`로 가고, 기존 Chrome 세션이 중요한 사이트는 Chrome Extension + Native Messaging Host 옵션을 붙인다.
4. 모든 사이트는 recipe로 등록한다. 코드에 사이트별 selector를 계속 박는 방식은 중단한다.
5. CAPTCHA/OTP/보안문자는 우회하지 않고 `action_required -> 사용자 입력 -> 같은 세션 재개`로 표준화한다.

이 방향이면 PC Agent 구현 난이도는 낮아지고, 향후에는 "로그인 사이트 자동수집 SaaS"로 별도 상품화도 가능하다.

## 13. SaaS 모델 보완 및 직접 구현 반영

작성 보강: 2026-09-03 KST

CEO 추가 지시에 따라 방향을 내부 PC Agent 개선이 아니라 "사용자가 직접 쓰는 SaaS형 로그인 사이트 수집 허브"로 확장했다. 핵심은 사용자가 프로젝트와 사이트를 연결하고, 로그인 상태와 수집 작업 상태를 확인하며, OTP/CAPTCHA/약관 동의 같은 자동 우회 금지 상황에서 직접 조치한 뒤 같은 work_key로 재개할 수 있게 만드는 것이다.

### 13.1 SaaS 사용자 흐름

| 단계 | 사용자 행동 | 시스템 처리 |
|---|---|---|
| 온보딩 | 프로젝트 선택, 사이트 프로필 생성 | `project_key`, `site_key`, `allowed_origins`, `runtime`, `data_categories` 저장 |
| 연결 | Windows Collector/WebView2/Chrome extension/수동 export 중 선택 | 런타임별 허용 origin과 세션 격리 정책 적용 |
| 수집 시작 | 사이트와 레시피 선택 후 실행 | `pc_agent_collection_queue`에 `queue_type=browser_recipe` 작업 생성 |
| 개입 필요 | OTP/CAPTCHA/세션만료/약관동의 직접 처리 | 자동 우회하지 않고 `action_required`로 표시 |
| 재개 | 사용자가 조치 완료 버튼 실행 | 동일 `work_key` 기준으로 다시 `queued` 처리 |
| 검증 | 결과 row, artifact hash, 실패 원인 확인 | 감사 로그와 재시도 정책으로 운영 |

### 13.2 구현된 MVP 골격

| 영역 | 구현 파일 | 내용 |
|---|---|---|
| 백엔드 API | `app/api/authenticated_site_collector.py` | overview, site profile, recipe dry-run, jobs, resume 라우터 |
| 서비스 | `app/services/authenticated_site_collector.py` | 다중 프로젝트 기본 프로필, DB/file fallback, PC Agent 큐 연결 |
| DB | `migrations/147_authenticated_site_collector.sql` | `authenticated_site_profiles`, `browser_recipes` SaaS 확장 컬럼 |
| 대시보드 | `/root/aads/aads-dashboard/src/app/authenticated-collector/page.tsx` | 프로젝트 필터, 사이트 연결, 작업 큐, 개입 필요 패널 |
| API 클라이언트 | `/root/aads/aads-dashboard/src/lib/api.ts` | collector API 함수와 응답 타입 |
| 네비게이션 | `/root/aads/aads-dashboard/src/components/Sidebar.tsx` | "로그인 수집 허브" 메뉴 |
| 테스트 | `tests/unit/test_authenticated_site_collector.py` | fallback overview, profile upsert, job 생성, same work_key resume |

### 13.3 별도 서비스화 방향

범용 전자계약 서비스처럼 "모든 사이트 자동 수집"으로 바로 포지셔닝하면 약관, 보안, 개인정보 리스크가 크다. 1차 상품명은 내부 코드명 `Authenticated Site Collector`로 두고, 실제 상품은 "로그인 사이트 수집 허브"처럼 업무 결과 중심으로 잡는다.

| 상품 계층 | 대상 | 과금 후보 |
|---|---|---|
| Starter | 1~3개 사이트 수동/반자동 수집 | 월 구독 + 작업 수 제한 |
| Team | 여러 프로젝트와 팀 계정 | 사이트 수, 계정 수, 보존기간 기준 |
| Ops | 정산/광고/리뷰/파일 수집 운영팀 | 전용 Collector, 감사 로그, SLA |
| Enterprise | 금융/대형 입점사 | 사설 배포, SSO, 보안 검토, 계약형 과금 |

### 13.4 남은 구현 단계

1. DB migration 적용 후 실제 tenant별 site profile 영속화 검증
2. Windows Collector App/WebView2 클라이언트가 `/jobs`를 polling하거나 WebSocket으로 claim하는 실행기 구현
3. 사이트별 recipe fixture 테스트와 selector 변경 감지 자동화
4. `/authenticated-collector` 화면 브라우저 캡처 검증과 모바일 레이아웃 검증
5. 수집 대상 사이트별 약관/개인정보/계정권한 검토 문서화
