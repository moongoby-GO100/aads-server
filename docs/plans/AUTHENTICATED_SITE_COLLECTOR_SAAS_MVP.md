# Authenticated Site Collector SaaS MVP

작성: 2026-09-03 KST
프로젝트: AADS
상태: implementation scaffold

## 목적

API를 제공하지 않거나 로그인 세션이 필요한 온라인 사이트 데이터를 사용자 승인 범위 안에서 수집하는 SaaS형 제어 평면을 AADS에 추가한다. 이 기능은 기존 PC Agent를 버리지 않고, 사이트별 수집 프로필, 레시피, 작업 큐, 사용자 개입 재개 흐름을 제품 화면과 API로 묶는다.

## 제품 경계

| 범위 | 포함 | 제외 |
|---|---|---|
| SaaS 화면 | 프로젝트 필터, 사이트 연결 상태, 작업 큐, 개입 필요 패널 | 범용 원격 PC 조작 화면 |
| 서버 API | site profile, recipe dry-run, collection job, resume | 외부 사이트 약관 우회 |
| 런타임 | WebView2, Chrome extension, Chrome CDP, Playwright, 파일 업로드, 공식 API, 수동 export | CAPTCHA/OTP 자동 풀이 |
| 보안 | tenant/role 인증, allowed origin, 감사 가능한 job payload | 비밀번호/쿠키 평문 저장 |

## 다중 프로젝트 지원

| 프로젝트 | 기본 런타임 | 우선 데이터 | 운영 원칙 |
|---|---|---|---|
| AADS | playwright_server | runner, docs, agent health | 내부 운영 우선 |
| KIS | manual_export | orders, fills, statements | 금융 약관/보안 우선 |
| GO100 | official_api | market data, research | 공식 API/수동 export 우선 |
| SF | chrome_extension | uploads, ads, analytics | 다운로드/업로드 추적 |
| NTV2 | webview2 | seller onboarding, settlement, reviews | tenant/account 격리 |
| NAS | file_upload | images, hashes, exports | 파일 해시 검증 |
| CUSTOM | webview2 | 사용자 정의 | 사이트 프로필/레시피 직접 등록 |

## 구현 파일

| 파일 | 역할 |
|---|---|
| `app/api/authenticated_site_collector.py` | `/api/v1/authenticated-site-collector/*` 라우터 |
| `app/services/authenticated_site_collector.py` | SaaS control-plane 서비스, site profile fallback, PC Agent queue 연결 |
| `migrations/147_authenticated_site_collector.sql` | `authenticated_site_profiles` 테이블과 `browser_recipes` SaaS 확장 컬럼 |
| `/root/aads/aads-dashboard/src/app/authenticated-collector/page.tsx` | 사용자용 수집 허브 화면 |
| `/root/aads/aads-dashboard/src/lib/api.ts` | 대시보드 API 클라이언트 |
| `/root/aads/aads-dashboard/src/components/Sidebar.tsx` | 사이드바 진입점 |
| `tests/unit/test_authenticated_site_collector.py` | offline fallback, queue 생성, same work_key resume 검증 |

## 완료 기준

1. 백엔드 라우터 import와 `app/main.py` 등록이 통과해야 한다.
2. DB 미적용 환경에서도 demo profile fallback으로 화면이 빈 500이 아니라 사용 가능한 상태를 보여야 한다.
3. `POST /jobs`는 `pc_agent_collection_queue`의 `queue_type=browser_recipe`로 연결되어야 한다.
4. OTP/CAPTCHA/약관/세션만료는 자동 우회하지 않고 `action_required -> 사용자 조치 -> resume`으로만 처리한다.
5. 대시보드는 프로젝트별 사이트, 작업 큐, 개입 필요 작업, 실패 후 다음 행동을 한 화면에서 보여야 한다.

## 운영 메모

첫 상용 수집 대상은 약관/개인정보/계정권한 검토가 끝난 사이트부터 등록한다. 금융·증권 계열은 공식 API 또는 수동 export를 우선하고, 로그인 자동화는 사용자 보유 권한과 감사 로그가 확인된 범위에서만 실행한다.
