# Server Playwright Bot Defense and Managed Browser Plan

문서 ID: AADS-SERVER-PLAYWRIGHT-BOT-DEFENSE-MANAGED-BROWSER-PLAN-20260828
작성 기준: 2026-08-28 13:54:40 KST
대상: AADS / OHVIS / API가 없는 로그인 어드민 자동화 / 서버 Playwright / Managed Browser
상태: 기획문서 저장 완료, 구현 지시 전 기준 문서
연결 문서:
- `docs/plans/20260828_APILESS_AUTHENTICATED_ADMIN_AUTOMATION_PLAN.md`
- `docs/plans/20260828_APPROVED_CAPTCHA_AUTOMATION_POLICY.md`
- `docs/plans/20260819_OHVIS_ASIDE_BROWSER_AGENT_ARCHITECTURE.md`

## 1. 요약

서버 Playwright는 API가 없는 웹사이트의 로그인, 화면 탐색, 자료 수집, 파일 다운로드, 파일 업로드, 제출 결과 확인을 자동화하는 핵심 런타임으로 사용할 수 있다. 다만 모든 사이트가 서버 headless 브라우저를 허용하는 것은 아니므로, OHVIS는 "봇차단 우회 도구"가 아니라 **허가된 계정과 업무 범위 안에서 실행되는 관리형 브라우저 작업 플랫폼**으로 설계해야 한다.

핵심 결론은 다음과 같다.

1. 구체적인 봇방어 회피 기법, 무단 CAPTCHA 우회, fingerprint spoofing, 방어 정책 무력화, 차단 회피용 프록시 회전 구현은 OHVIS 제품 정책으로 금지한다.
2. 대신 작업 생성 전에 서버 Playwright 접근 진단을 수행하고, 결과를 `reachable`, `auth_required`, `challenge_required`, `bot_or_waf_blocked`, `network_or_tls_error`, `runtime_unavailable` 등으로 분류한다.
3. 로그인, OTP, CAPTCHA, 인증서, 업로드, 결제, 게시, 삭제는 승인 토큰과 감사 로그 범위 안에서 자동화한다.
4. PC Agent 없이 가능한 사이트는 `self_hosted_playwright`로 처리하고, 로컬 인증서/기기 신뢰/CEO PC 로그인 세션이 필요한 사이트만 `pc_agent`로 처리한다.
5. 외부 유료 서비스 Browserbase, Browserless, Cloudflare Browser Run, Firecrawl, Apify, Hyperbrowser, Stagehand의 장점은 세션 지속, live view, trace/replay, worker pool, session pool, typed extraction, queue/backpressure에 있다. OHVIS는 이 장점을 자체 구현하되, 회피성 기능은 정책적으로 흡수하지 않는다.

## 2. CEO 지시 반영 원칙

| CEO 지시 | 반영 정책 | 구현 원칙 |
|---|---|---|
| API 없는 웹사이트도 파싱/스크래핑/업로드 자동화 | `BrowserRecipe` 기반으로 로그인, 수집, 업로드, 검증을 표준화 | 사이트별 레시피, 파서, 업로더, verifier 분리 |
| 로그인 필요한 어드민 처리 | Vault + storage state + 승인형 challenge 처리 | 비밀번호/OTP/CAPTCHA 값 원문 저장 금지 |
| Browserbase 유료 대체 | 자체 서버 Playwright Worker Pool 우선 | 외부 샌드박스는 비교 PoC/비민감 사이트 한정 |
| PC Agent 없이 진행 | `self_hosted_playwright` 런타임 우선 | PC Agent는 로컬 인증/기기 세션 필요 시만 fallback |
| CAPTCHA 우회 금지 | 승인 없는 자동 판독/입력 금지 | 승인된 page/origin/task 범위의 모델 판독/입력은 허용 |
| 사용자가 화면을 계속 볼 수 없는 문제 | human takeover만이 아니라 approval-scoped automation으로 해결 | 승인 로그 후 자동 판독, 자동 입력, 자동 재시도 |
| 여러 작업 동시성 | recipe별 conflict key와 resource policy로 제어 | 같은 계정/도메인 위험 작업은 직렬화 |
| 브라우저 실행 과정 화면 표시 | Live View, 최신 frame, event timeline, trace/replay 제공 | 민감값 masking, 최신 frame retention 제한 |

## 3. 용어 정의

| 용어 | 의미 |
|---|---|
| 서버 Playwright | AADS 서버 컨테이너 또는 전용 worker에서 실행되는 Chromium/Playwright 런타임 |
| PC Agent | CEO 또는 운영자 PC에 설치된 Agent가 로컬 브라우저, 파일, 인증서를 제어하는 런타임 |
| BrowserRecipe | 사이트별 로그인, 이동, 수집, 업로드, 승인, 검증 절차를 정의하는 버전 관리 단위 |
| Approval Token | 누가, 언제, 어떤 사이트, 어떤 업무, 어떤 액션을 몇 회까지 허용했는지 담는 자동화 권한 |
| Bot Defense | Cloudflare, Akamai, DataDome, reCAPTCHA 등 자동화 탐지/인증/차단 계층 |
| 접근 진단 | URL에 서버 Playwright로 접근했을 때 로그인 필요, 인증 challenge, WAF 차단, 네트워크 오류 등을 분류하는 사전 검사 |
| Managed Browser | 작업 큐, 세션 지속, Live View, 감사 로그, 승인 게이트, artifact 저장을 포함한 브라우저 실행 플랫폼 |

## 4. 보안 및 법적 경계

OHVIS가 제공해야 하는 것은 **사용자 권한 내 반복 업무 자동화**다. 차단을 숨기거나 방어를 무력화하는 기술을 제품 기능으로 제공하면 계정 차단, 약관 위반, 법적 리스크가 커진다.

| 구분 | OHVIS 허용 | OHVIS 금지 |
|---|---|---|
| 로그인 | 사용자가 등록한 계정/Vault/storage state로 로그인 | 제3자 계정, 탈취 쿠키, 무단 세션 사용 |
| CAPTCHA | 승인된 페이지와 업무 범위 안에서 모델 판독/입력 | 승인 없는 solver, 방어장치 회피, 우회 절차 제공 |
| OTP | 사용자가 입력한 OTP transient 주입, 푸시 승인 완료 감지, 공식 TOTP/API | 문자/앱 무단 열람, LLM 임의 생성, 대리 인증 |
| 프록시 | 사내 고정 egress, 사이트 허용 IP, 기업망 접근, 지역 제한 검증 | 차단 회피 목적의 무단 IP 회전/은닉 |
| fingerprint | 정상 브라우저 채널, 고정 viewport, 업무 프로필 일관성 | 탐지 회피 목적의 위장/무력화 |
| 수집 속도 | 사이트 약관과 업무 필요 범위의 rate limit | 대량 공격성 요청, 병렬 로그인 폭증 |
| WAF 차단 | 진단 후 공식 API/화이트리스트/PC Agent/수동 승인으로 전환 | 차단 룰을 속이는 세부 우회 기법 구현 |

## 5. Playwright 접근 실패 유형

| 유형 | 증상 | 원인 후보 | OHVIS 처리 |
|---|---|---|---|
| `reachable` | 페이지 정상 로딩 | 서버 Playwright 허용 | 레시피 실행 가능 |
| `auth_required` | 로그인 페이지/세션 만료 | 쿠키 만료, 새 기기, SSO | Vault login 또는 storage state 재생성 |
| `challenge_required` | CAPTCHA/OTP/인증서/본인확인 | 계정 보호, 고위험 액션, 새 브라우저 | 승인 토큰 또는 transient 입력 흐름 |
| `bot_or_waf_blocked` | 403/429/차단 페이지/JS challenge 반복 | WAF, bot detection, 데이터센터 IP 거부 | 공식 연동, 허용 IP 등록, PC Agent, 외부 샌드박스 검토 |
| `unsupported_browser` | 기능 미지원/브라우저 경고 | headless, WebGL, codec, certificate store 차이 | headed/Chrome channel/PC Agent 분기 |
| `network_or_tls_error` | TLS, DNS, 내부망 접근 실패 | 서버 네트워크, mTLS, 사내망 제한 | 네트워크 allowlist 또는 PC Agent 분기 |
| `runtime_unavailable` | browser launch 실패 | Docker deps, sandbox, memory | worker health repair, queue 재시도 |
| `timeout_or_slow_page` | load timeout, SPA 미완료 | 느린 사이트, selector wait 오류 | 단계별 wait, screenshot, trace 저장 |
| `selector_drift` | 버튼/표를 못 찾음 | UI 변경, iframe, shadow DOM | locator 후보, accessibility tree, LLM 화면 분석 |
| `state_conflict` | 다른 작업 때문에 로그아웃/중복 제출 | 같은 계정 동시 실행 | conflict key 직렬화 |

## 6. 서버 Playwright의 합법적 안정화 방법

아래 항목은 차단 회피가 아니라 자동화 품질과 운영 안정성을 높이는 방법이다.

| 영역 | 적용 방법 | 목적 | 주의 |
|---|---|---|---|
| 인증 상태 | origin/work_key별 storage state 저장 | 반복 로그인 감소 | state 파일은 secret 취급 |
| 브라우저 격리 | task별 context, site별 persistent profile | 세션 충돌 방지 | 같은 계정 위험 작업은 1개만 실행 |
| 정상 브라우저 | Chromium channel/Chrome channel 분리 | 렌더링 호환성 개선 | 위장 목적 설정 금지 |
| 관측성 | screenshot, DOM snapshot, trace, console, network summary | 실패 원인 재현 | 민감 header/body masking |
| 단계 제어 | deterministic selector 우선, LLM selector 보조 | UI 변경 대응 | LLM 단독 제출 금지 |
| rate limit | recipe별 최소 간격, max retries, backoff | 계정 차단 위험 감소 | 공격성 병렬 금지 |
| artifact | HTML/CSV/XLSX/PDF/screenshot hash 저장 | 증빙과 parser 결과 연결 | 개인정보 보존기간 정책 필요 |
| 승인 게이트 | action preview + approval token consume | 위험 액션 통제 | 승인 scope 초과 시 즉시 중단 |
| runtime fallback | self-hosted -> PC Agent -> external sandbox review | 실패 시 대안 제공 | 자동 fallback도 정책 로그 필요 |

## 7. 금지해야 할 구현 목록

다음은 구현하지 않는다.

1. 사이트의 봇 탐지 신호를 숨기기 위한 fingerprint spoofing 세부 구현.
2. CAPTCHA solver 외부 서비스 무단 연동.
3. WAF/Cloudflare/Akamai/DataDome 등의 차단 룰을 우회하는 절차 문서화.
4. 승인 없는 주거용 프록시 회전, IP 은닉, 국가 우회.
5. 사이트 약관상 금지된 대량 scraping, rate limit 회피.
6. OTP 앱/문자/이메일을 사용자의 명시 승인 없이 읽는 자동화.
7. 결제/이체/게시/삭제/업로드를 승인 preview 없이 실행.
8. storage state, 쿠키, bearer token, 비밀번호, CAPTCHA 정답을 repo 또는 로그에 저장.

## 8. 승인형 CAPTCHA/OTP 정책

CAPTCHA/OTP는 "항상 사람이 화면을 보고 직접 입력"해야만 하는 대상이 아니다. CEO 지시 기준으로, 사용자가 특정 페이지와 업무 범위를 승인하면 OHVIS가 그 승인 범위 안에서 자동 판독, 자동 입력, 자동 제출, 결과 재판정을 수행할 수 있다.

| challenge | 승인 전 | 승인 후 | 저장 정책 |
|---|---|---|---|
| 숫자 CAPTCHA | 감지, 화면 hash, 승인 요청 | 비전 모델 판독, 입력, 제출, 성공/실패 판정 | 정답 저장 금지 |
| 이미지 CAPTCHA | 감지, 승인 요청 | 사이트 권한과 정책 검토 후 제한적 허용 | 원문/정답 저장 금지 |
| OTP | 입력칸 감지, push 알림 | 사용자가 제공한 OTP 1회 주입 또는 공식 TOTP | 값 저장 금지 |
| 인증서 비밀번호 | 인증서 선택 화면 감지 | Vault write-only 입력 | 원문 저장 금지 |
| 본인확인 | 감지, 승인 요청 | 사용자가 외부 앱/휴대폰에서 완료한 결과 감지 | 대리 조작 금지 |

승인 이벤트에는 다음 메타데이터를 남긴다.

| 필드 | 설명 |
|---|---|
| `approved_by` | 승인자 계정 또는 CEO 세션 |
| `approved_at` | 승인 시각 |
| `origin` | 승인된 도메인 |
| `page_url_hash` | URL 원문 대신 hash 또는 masked URL |
| `task_id` | browser task ID |
| `recipe_id` / `recipe_hash` | 승인 당시 레시피 |
| `challenge_kind` | captcha, otp, certificate 등 |
| `automation_scope` | 모델 판독, 자동입력, 제출, 재시도 허용 범위 |
| `max_executions` | 반복 허용 횟수 |
| `expires_at` | 만료 시각 |
| `result` | consumed, rejected, expired, failed |

## 9. Browserbase 기술 기반 분석

공식 문서 기준 Browserbase는 cloud browser, session, context, live view, identity/authentication, Stagehand, search/fetch/runtime을 묶은 managed browser platform이다.

| 구성 | Browserbase 방식 | OHVIS 자체 구현 대응 |
|---|---|---|
| Sessions API | 브라우저 세션 생성 후 Playwright/Puppeteer/Selenium 연결 URL 제공 | `browser_tasks` + `self_hosted_playwright` worker session |
| Contexts | 쿠키, 인증, localStorage, IndexedDB 등 사용자 데이터를 세션 간 유지 | `work_key + origin`별 encrypted storage state |
| Website Authentication | context, proxy, fingerprinting, live view로 로그인 상태 유지 | 승인형 로그인 setup task + storage state 재사용 |
| Live View | 세션 화면을 실시간으로 보고 human-in-the-loop 가능 | `/browser-tasks` Live View frame + event timeline |
| Stagehand | 자연어 selector, self-healing action, typed extract | deterministic locator 우선 + LLM 보조 selector + schema validation |
| Observability | replay, logs, network, trace | Playwright trace, console/network summary, artifact hash |
| Runtime | agent를 schedule/on-demand로 실행 | AADS Pipeline/BrowserRecipe queue |

OHVIS가 가져올 핵심은 Browserbase의 유료 cloud browser 자체가 아니라 다음 구조다.

1. 세션과 인증상태를 분리하고 context로 재사용한다.
2. 브라우저 실행 화면을 사용자가 볼 수 있게 한다.
3. human-in-the-loop를 단순 수동 개입이 아니라 승인 토큰 기반 자동 재개로 만든다.
4. 작업별 trace/replay/network log를 남긴다.
5. 자연어/LLM은 selector 보조와 typed extraction에 제한적으로 쓴다.

## 10. Browserless 기술 기반 분석

Browserless는 브라우저를 서비스로 제공하면서 Playwright/Puppeteer 연결, durable session/profile, proxy, stealth, captcha 등 opt-in 기능을 제공하는 계열이다.

| 구성 | 벤더 기능 | OHVIS 반영 |
|---|---|---|
| BaaS | 원격 Chrome에 CDP/Playwright 연결 | 자체 browser worker pool |
| Durable profiles | 인증 상태와 세션 유지 | encrypted storage state |
| Proxy | datacenter/residential proxy 지원 | 기본 미반영, 공식 허용 IP/기업망만 검토 |
| Stealth routes | 탐지 회피 목적 기능 | 제품 정책상 미반영 |
| CAPTCHA solving | solver 기능 | 승인형 CAPTCHA 모델 판독만 내부 정책으로 제한 |
| Replay/trace | 실패 분석 | Playwright trace와 OHVIS event log |

Browserless류에서 가져올 것은 "브라우저 풀 운영, 세션 프로필, replay, 큐/동시성"이고, 회피성 stealth/proxy/captcha solver는 OHVIS의 기본 기능으로 가져오지 않는다.

## 11. Cloudflare Browser Run 기술 기반 분석

Cloudflare Browser Run은 Cloudflare 글로벌 네트워크에서 headless Chrome을 실행하는 managed browser 서비스다. 2026-08-11 공식 문서 기준 Browser Rendering에서 Browser Run으로 명칭이 바뀌었고, browser automation, scraping, testing, content generation을 지원한다.

2026-04-15 Cloudflare changelog 기준 Live View, Human in the Loop, Session Recordings가 추가되어 사용자가 세션을 실시간으로 보고 문제 발생 시 개입하고 종료 후 재생할 수 있다.

| 구성 | Cloudflare 방식 | OHVIS 자체 구현 대응 |
|---|---|---|
| Global browser pool | Cloudflare 네트워크에서 headless Chrome 실행 | AADS 서버/전용 VM worker pool |
| REST quick actions | screenshot, PDF, markdown, json 등 | access-check, screenshot, artifact parser |
| Live View | page, DOM, console, network 실시간 확인 | `/browser-tasks` Live View + event timeline |
| Human in the Loop | live view로 인간 개입 후 agent 재개 | approval token + transient input + resume |
| Session recording | 종료 후 replay | trace.zip, latest frames, event replay |

Cloudflare에서 가져올 핵심은 global infra가 아니라 "observability first"다. OHVIS도 브라우저 자동화 실패 시 로그 한 줄이 아니라 화면, DOM, console, network, trace를 함께 남겨야 한다.

## 12. Firecrawl 기술 기반 분석

Firecrawl은 search, scrape, interact, parse, crawl, agent, browser sandbox를 제공하는 웹 데이터 API 계열이다. 공식 문서 기준 scrape는 markdown/html/structured JSON을 반환하고, interact는 클릭, 폼 입력, 동적 콘텐츠 추출을 수행한다.

| 구성 | Firecrawl 방식 | OHVIS 반영 |
|---|---|---|
| Scrape | URL을 markdown/html/JSON으로 변환 | 공개/준공개 페이지 파싱 fallback |
| Interact | scrape session에서 click/fill/extract | 저위험 정보수집 보조 |
| Parse | PDF/DOCX/XLSX/HTML 구조화 | 업로드/다운로드 artifact parser |
| Browser Sandbox | managed browser session | external_sandbox adapter 후보 |
| MCP | AI tool 통합 | AADS MCP tool 노출 후보 |

Firecrawl은 로그인 어드민의 결제/게시/삭제 같은 위험 액션 런타임보다는 공개 정보 수집, 문서 파싱, LLM-ready extraction에 강하다. OHVIS에서는 `external_sandbox_review` 또는 `public_data_fetch` 런타임으로 분리하는 것이 맞다.

## 13. Apify/Crawlee 기술 기반 분석

Apify/Crawlee 계열은 crawler queue, session pool, proxy configuration, autoscaled pool, request retries, dataset storage가 강점이다.

| 구성 | 벤더 기능 | OHVIS 반영 |
|---|---|---|
| RequestQueue | URL/작업 큐 관리 | `browser_recipe_runs` queue |
| SessionPool | 쿠키/세션 유지 | `work_key + origin` profile |
| AutoscaledPool | CPU/메모리 기반 동시성 조절 | runtime resource allocator |
| maxRequestRetries | 실패 재시도 | recipe별 retry/backoff |
| maxRequestsPerCrawl | 무한 크롤 방지 | max_steps/max_pages |
| Dataset | 결과 저장 | artifact + parse result table |

OHVIS가 반드시 가져와야 할 것은 session pool과 autoscaled pool 개념이다. 같은 계정/도메인은 직렬화하되, 공개 페이지나 지점별 조회처럼 충돌이 낮은 작업은 resource budget 안에서 병렬화한다.

## 14. Hyperbrowser/Stagehand 계열 분석

Hyperbrowser는 cloud browser session과 Playwright/Puppeteer 연결, AI agent runtime, session management를 제공한다. Stagehand는 Playwright 위에 자연어 action, typed extraction, self-healing selector를 얹는 SDK다.

| 구성 | 의미 | OHVIS 구현 방향 |
|---|---|---|
| Cloud sessions | 브라우저 인프라를 외부에서 제공 | 자체 worker pool 우선, 외부 adapter 후보 |
| AI action | 자연어로 클릭/입력/탐색 | 위험 액션에는 사용 금지, selector 후보 생성에만 사용 |
| Typed extraction | schema 기반 결과 추출 | parser result schema validation |
| Action cache/replay | 성공한 흐름 재사용 | recipe version + step replay |
| Metrics | LLM 사용량/시간 측정 | task event에 model/tokens/duration 기록 |

OHVIS는 Stagehand류의 자연어 조작을 그대로 위험 업무에 쓰면 안 된다. 대신 "화면 분석 -> 후보 selector -> dry-run -> verifier -> 승인"의 보조 계층으로 제한한다.

## 15. OHVIS 자체 구현 아키텍처

```text
CEO Chat / OHVIS Browser Tasks UI
        |
        v
Browser Automation Gateway
 - task request normalization
 - recipe lookup
 - access check
 - policy/risk classification
        |
        +---------------------+----------------------+
        |                     |                      |
        v                     v                      v
BrowserRecipe Registry   Approval Service       Agent Vault
 - steps/version hash     - approval token       - credential lookup
 - resource policy        - consume/expire       - write-only autofill
 - risk actions           - audit log            - secret masking
        |
        v
Runtime Router
 - self_hosted_playwright
 - pc_agent
 - external_sandbox_review
        |
        v
Browser Worker Pool
 - context/profile lease
 - screenshot/live frame
 - trace/console/network summary
 - artifact capture
        |
        v
Parser / Uploader / Verifier
 - DOM parser
 - file parser
 - network response normalizer
 - upload result checker
 - DB writer
```

## 16. 런타임 선택 기준

| 조건 | 1순위 | 2순위 | 판정 |
|---|---|---|---|
| 공개/준공개 페이지 수집 | `self_hosted_playwright` | Firecrawl/Apify 후보 | PC Agent 불필요 |
| 일반 ID/PW 어드민 | `self_hosted_playwright` | PC Agent | storage state 가능하면 서버 우선 |
| 기기 신뢰/인증서/로컬 파일 필요 | PC Agent | 사내 원격 VM | 서버 단독 한계 |
| CAPTCHA/OTP 반복 발생 | 승인 토큰 + 자동 판독/입력 또는 transient 입력 | PC Agent live view | 승인 없는 판독 금지 |
| WAF/bot block | 공식 API/화이트리스트/권한 있는 브라우저 세션 | 외부 sandbox 검토 | 우회 구현 금지 |
| 대량 공개 crawling | self-hosted worker pool | Apify/Crawlee 구조 참고 | rate limit 필수 |
| 파일 업로드/게시/삭제 | 승인 토큰 + 직렬 실행 | PC Agent | preview/hash verifier 필수 |

## 17. 동시성 및 리소스 배분 설계

| 정책 | 기본값 | 이유 |
|---|---:|---|
| global browser workers | 3 | AADS 서버 메모리 보호 |
| per-origin workers | 1 | 같은 사이트 세션 충돌 방지 |
| per-work-key workers | 1 | 같은 계정 중복 로그인 방지 |
| low-risk read-only workers | 3 | 공개/조회성 수집 처리량 확보 |
| risky action workers | 1 | 승인 scope와 제출 대상 불일치 방지 |
| max page steps | 80 | 무한 탐색 방지 |
| max run time | 15분 | stuck browser 방지 |
| max retries | 2 | 계정 보호 |
| min retry delay | 60초 | CAPTCHA/차단 반복 방지 |
| artifact retention | 30일 기본 | 증빙/개인정보 균형 |

`BrowserRecipe`에는 다음 필드를 둔다.

```json
{
  "concurrency_policy": {
    "conflict_keys": ["origin", "work_key", "risk_group"],
    "max_parallel_runs": 1,
    "queue_strategy": "fifo",
    "reject_on_conflict": false
  },
  "resource_policy": {
    "runtime": "self_hosted_playwright",
    "max_memory_mb": 1024,
    "max_run_seconds": 900,
    "max_pages": 20,
    "max_steps": 80,
    "max_artifact_mb": 100
  },
  "retry_policy": {
    "max_attempts": 2,
    "backoff_seconds": [60, 180],
    "stop_on": ["challenge_required", "bot_or_waf_blocked", "approval_required"]
  }
}
```

## 18. 접근 진단 API 설계

| API | 역할 |
|---|---|
| `POST /api/v1/browser-tasks/access-check` | URL 접근 가능성, HTTP 상태, challenge, WAF, runtime 오류 분류 |
| `GET /api/v1/browser-tasks/{task_id}/live-frame` | 최신 화면 frame 조회 |
| `GET /api/v1/browser-tasks/{task_id}/events` | 최근 실행 이벤트 조회 |
| `POST /api/v1/browser-recipes/dry-run` | 레시피의 selector, approval, resource, concurrency 정책 검증 |
| `POST /api/v1/browser-recipes/{id}/run-plan` | 현재 큐/리소스 기준 실행 가능 여부 판정 |

응답 예시는 다음과 같다.

```json
{
  "ok": true,
  "classification": "challenge_required",
  "target_url": "https://example-admin.invalid/login",
  "origin": "https://example-admin.invalid",
  "http_status": 200,
  "signals": ["captcha_text", "otp_input"],
  "recommended_runtime": "self_hosted_playwright",
  "fallback_runtimes": ["pc_agent"],
  "requires_approval": true,
  "approval_kinds": ["captcha_model_read", "transient_input"],
  "safe_next_action": "request_approval",
  "blocked_reason": null
}
```

## 19. Live View 및 Replay 설계

| 기능 | P0 | P1 | P2 |
|---|---|---|---|
| 최신 화면 | latest screenshot frame | 1초 간격 선택적 frame | WebRTC/stream 검토 |
| 이벤트 | step, url, status, approval, error | console/network summary | trace viewer 연동 |
| 사용자 개입 | 승인 버튼, transient input | read/write live takeover | 공동 작업자 공유 |
| replay | event timeline | screenshot timeline | Playwright trace replay |
| masking | input value masking | sensitive DOM redaction | screenshot 영역 redaction |

운영 원칙:

1. 비밀번호, OTP, CAPTCHA 정답, 인증서 비밀번호는 DB/log에 남기지 않는다.
2. screenshot에는 값이 보일 수 있으므로 retention과 접근 권한을 제한한다.
3. Live View가 끊겨도 task event는 계속 저장되어야 한다.
4. 작업 실패 시 마지막 frame, URL, classification, selector, verifier 결과를 한 화면에 표시한다.

## 20. Parser/Upload/Verifier 설계

| 단계 | 처리 | 완료 기준 |
|---|---|---|
| Capture | HTML, screenshot, downloaded file, network response 저장 | artifact hash 생성 |
| Parse | CSV/XLSX/PDF/DOM/JSON을 표준 schema로 변환 | row count, period, amount validation |
| Upload Preview | 파일명, hash, 대상 URL, form selector 표시 | 승인 토큰 발급 가능 |
| Upload Execute | 승인된 file hash와 selector만 사용 | 결과 화면/접수번호 캡처 |
| Verify | success text, receipt, DB row, artifact 연결 | task `completed` 또는 `review_required` |

## 21. 오비스 구현 우선순위

| 우선 | 구현 과제 | 내용 | 완료 기준 |
|---|---|---|---|
| P0 | Bot Defense Access Check 보강 | challenge/WAF/network/runtime/selector drift 분류 | 모든 browser task 생성 전 진단 이벤트 저장 |
| P0 | Approval-scoped Challenge Automation | CAPTCHA/OTP/인증서 승인 scope 소비 | 승인 없는 실행 0건, 승인 후 자동 재개 |
| P0 | Live View Evidence | latest frame + event timeline + masked metadata | 사용자가 실행 과정을 눈으로 확인 |
| P0 | Runtime Router | self-hosted 우선, PC Agent fallback, external review 분리 | run-plan에 PC Agent 필요 여부 표시 |
| P1 | Worker Pool | per-origin/per-work-key lease, memory/time budget | 동시 3개 read-only task 안정 실행 |
| P1 | Trace/Replay | Playwright trace, network/console summary | 실패 task 재현 가능 |
| P1 | Recipe Manager | UI에서 recipe version, resource, risk action 관리 | dry-run 결과 표시 |
| P1 | Artifact Parser | CSV/XLSX/PDF/DOM parser registry | 원본과 row 연결 |
| P2 | External Adapter PoC | Browserbase/Cloudflare/Firecrawl/Apify 비교 | 비용/성공률/보안성 보고 |
| P2 | Self-healing Selector | LLM 후보 selector + deterministic verifier | UI 변경 시 자동 복구율 측정 |

## 22. 구현 지시서 초안

```text
TASK_ID: AADS-SERVER-PLAYWRIGHT-BOT-DEFENSE-P0-20260828
TITLE: OHVIS 서버 Playwright 접근 진단/승인형 자동화/Live Evidence P0
PRIORITY: P0
SIZE: L
MODE: code_modify_verify

DESCRIPTION:
1. 모든 BrowserRecipe run 생성 전 access-check를 실행하고 classification을 browser_task_events에 저장한다.
2. classification은 reachable/auth_required/challenge_required/bot_or_waf_blocked/unsupported_browser/network_or_tls_error/runtime_unavailable/timeout_or_slow_page/selector_drift/state_conflict를 지원한다.
3. challenge_required는 approval token 없이는 모델 판독/입력/제출을 실행하지 않는다.
4. approval token이 있으면 origin/page/task/recipe_hash/max_executions/expires_at을 검증하고 자동 판독/입력/제출/재판정을 수행한다.
5. bot_or_waf_blocked는 우회 시도 없이 공식 API/화이트리스트/PC Agent/external_sandbox_review 권장으로 종료한다.
6. self_hosted_playwright worker pool에 per-origin/per-work-key lease와 resource budget을 적용한다.
7. Live View에 latest frame, event timeline, access diagnosis, recommended runtime, approval status를 표시한다.
8. trace/network/console summary는 민감값 마스킹 후 저장한다.

VERIFY:
- python3 -m py_compile app/api/browser_tasks.py app/services/browser_task_gateway.py app/services/browser_recipe_registry.py app/services/browser_permission_policy.py
- pytest tests/unit/test_browser_task_policy.py -q
- dashboard: npx eslint src/app/browser-tasks/page.tsx
- dashboard: npx tsc --noEmit --pretty false
- API: POST /api/v1/browser-tasks/access-check with reachable/auth_required/challenge_required/bot_or_waf_blocked fixtures
```

## 23. 운영 체크리스트

| 체크 | 기준 | 실패 시 |
|---|---|---|
| 사이트 권한 | 사용자 계정/업무 권한 확인 | 레시피 등록 보류 |
| robots/약관 | 업무 자동화 허용 범위 확인 | 공식 API/제휴 우선 |
| 로그인 | Vault/storage state 사용 가능 | 승인 setup task |
| CAPTCHA/OTP | 승인 scope 정의 | 자동화 보류 |
| 위험 액션 | preview/hash/selector/result verifier 존재 | 실행 불가 |
| 동시성 | conflict key 설정 | run-plan 실패 |
| 리소스 | memory/time/artifact budget 설정 | queued 또는 fail-fast |
| 관측성 | screenshot/event/trace 저장 | 운영 반영 불가 |
| 민감값 | masking/redaction 테스트 | 배포 차단 |

## 24. 참고자료

| 출처 | 확인 내용 | OHVIS 반영 |
|---|---|---|
| Playwright Authentication, https://playwright.dev/docs/auth | storage state로 인증 상태 재사용, 파일은 민감 정보로 취급 필요. 병렬 worker는 계정 분리 필요 | encrypted storage state, work_key/origin 격리 |
| Playwright Docker, https://playwright.dev/docs/docker | 공식 Docker 이미지는 테스트/개발 목적이며 untrusted crawling은 별도 user/seccomp 권장 | self-hosted worker 격리, root browser 실행 금지 방향 |
| Playwright Network, https://playwright.dev/docs/network | browser 또는 context 단위 proxy 설정 가능 | 회피 목적이 아닌 기업망/허용 IP 접근에만 검토 |
| Playwright Trace Viewer, https://playwright.dev/docs/trace-viewer | action, DOM, log, console, network, metadata를 trace로 확인 가능 | 실패 task replay/diagnosis |
| Browserbase Contexts, https://docs.browserbase.com/platform/browser/core-features/contexts | cookies/auth/localStorage/IndexedDB를 세션 간 유지 | OHVIS context/profile 설계 |
| Browserbase Session Live View, https://docs.browserbase.com/platform/browser/observability/session-live-view | live view, human-in-the-loop, iframe embedding, disconnect handling | `/browser-tasks` Live View |
| Browserbase Website Authentication, https://docs.browserbase.com/platform/identity/authentication | context, live view, session persistence로 인증 흐름 안정화 | 로그인 setup task + approval resume |
| Browserbase Introduction, https://docs.browserbase.com/welcome/introduction | cloud browsers, search, fetch, runtime, Stagehand | 자체 managed browser platform 기준 |
| Browserless Documentation, https://docs.browserless.io/ | Browserless MCP/BaaS/REST/BrowserQL/Playwright 연결/전용 배포/보안 설정 | 외부 BaaS 비교 기준 |
| Browserless Stealth Routes, https://docs.browserless.io/baas/bot-detection/stealth | stealth, anti-detection, CAPTCHA, proxy 계열 기능 존재 | OHVIS 기본 기능으로 미반영, 금지선 문서화 |
| Cloudflare Browser Run, https://developers.cloudflare.com/browser-run/ | 글로벌 네트워크 headless Chrome, REST/Playwright/Puppeteer 계열 자동화 | 외부 adapter 후보, 자체 worker pool 비교 기준 |
| Cloudflare Browser Run Changelog 2026-04-15, https://developers.cloudflare.com/changelog/post/2026-04-15-br-observability/ | Live View, Human in the Loop, Session Recordings | OHVIS observability P1 |
| Firecrawl Introduction, https://docs.firecrawl.dev/introduction | search/scrape/interact/parse/crawl/browser sandbox | 공개 데이터/문서 파싱 adapter 후보 |
| Apify PlaywrightCrawler Options, https://docs.apify.com/sdk/js/docs/2.3/typedefs/playwright-crawler-options | session pool, proxyConfiguration, retries, concurrency, autoscaled pool | queue/resource policy 설계 |
| Apify Playwright Scraper, https://apify.com/apify/playwright-scraper | Playwright 기반 scraping, proxy config, browser config, debug/browser logs | 공개 crawling 운영 패턴 참고 |
| Hyperbrowser Docs, https://www.hyperbrowser.ai/docs/home | cloud browser sessions, Playwright/Puppeteer 연결, sandbox, AI agents | external_sandbox adapter 후보 |
| Stagehand Docs, https://docs.stagehand.dev/ | agent용 browser SDK, self-healing actions, typed extraction, metrics | LLM selector 보조와 schema extraction 참고 |
| OHVIS Aside Benchmark, `docs/plans/20260819_OHVIS_ASIDE_BROWSER_AGENT_ARCHITECTURE.md` | 로컬 우선 브라우저, Vault, Guard/Full access, routine, audit | 제품 UX/권한 모델 |
| API-less Admin Plan, `docs/plans/20260828_APILESS_AUTHENTICATED_ADMIN_AUTOMATION_PLAN.md` | 로그인 어드민 수집/업로드/승인/동시성 상위 설계 | 본 문서의 상위 기획 |
| Approved CAPTCHA Policy, `docs/plans/20260828_APPROVED_CAPTCHA_AUTOMATION_POLICY.md` | 승인 scope 안의 CAPTCHA 자동 판독/입력 정책 | challenge automation 기준 |

## 25. 완료 판정

이 문서는 서버 Playwright 기반 브라우저 자동화의 봇방어 대응 방향을 "무단 우회"가 아니라 "승인형, 관측형, 정책형 자동화"로 확정한다.

P0 구현 완료 조건은 다음이다.

1. access-check가 모든 신규 browser task에 붙는다.
2. bot/WAF/challenge/auth/runtime/network/selector 오류가 분류되어 UI에 표시된다.
3. 승인 없는 CAPTCHA/OTP/위험 액션 자동 실행은 0건이다.
4. 승인된 CAPTCHA/OTP/업로드/게시/삭제/결제는 scope 안에서 자동 재개된다.
5. PC Agent 없이 가능한 레시피는 self-hosted Playwright에서 실행된다.
6. Live View와 event timeline으로 사용자가 실행 과정을 확인할 수 있다.
7. trace/replay/artifact로 실패 원인을 재현할 수 있다.
8. 동시성은 recipe conflict key와 resource budget으로 제어된다.
9. 외부 샌드박스는 유료 의존이 아니라 비교/보조 adapter로만 남긴다.
