# 로그인 필요 사이트 데이터 수집 플랫폼 계획 — SaaS MVP 보완

## 제품 모델

`Authenticated Site Collector`(로그인 사이트 수집 허브)는 로그인 뒤에 있는 업무 데이터를 사용자가 프로젝트 단위로 연결·수집·모니터링하는 멀티테넌트 SaaS다. 대상 사용자는 운영 담당자, 금융·리서치 분석가, 입점사 관리자, 콘텐츠/광고 운영자와 맞춤형 포털을 관리하는 고객이다.

- 요금제 후보: Starter(프로젝트 1개/수동 실행), Team(다중 프로젝트/예약 실행/감사 로그), Enterprise(전용 Collector/보존 정책/SSO).
- 온보딩: 프로젝트 선택 → 사이트 프로필 생성 → 공식 API/수동 내보내기 우선 안내 → 사용자 로그인 세션 연결 → 레시피 dry-run → 첫 수집.
- 사용자 화면: 연결 상태, 실행/실패 작업, 사용자 개입 필요, 최근 수집을 첫 화면에 제공하며 레시피/fixture/보존 정책은 설정으로 분리한다.
- 권한: 조회는 tenant VIEWER, 사이트·레시피·작업·재개는 MEMBER 이상이다. 모든 변경/실행/재개는 tenant 범위 감사 로그를 남긴다.
- 비밀정보: 비밀번호, 쿠키, 토큰, OTP/CAPTCHA 답은 Collector DB·로그·스크린샷에 저장하지 않는다. 계정은 Vault 참조만 보유한다.
- 챌린지: CAPTCHA/OTP/보안문자를 자동 우회하지 않는다. `action_required → 사용자 조치 → 동일 work_key 재개`만 허용한다.

## 다중 프로젝트·사이트 환경

| 프로젝트 | 대표 용도 | 우선 환경/주의점 |
|---|---|---|
| AADS | 내부 포털, 문서, 에이전트 상태 | official_api, chrome_cdp |
| GO100/KIS | 금융·증권·리서치 | official_api/manual_export 우선, 약관·개인정보 고위험 |
| SF | 숏폼, 광고, 업로드 상태 | chrome_extension/webview2, 파일 상태 추적 |
| NTV2 | 입점사, 커뮤니티, 정산 | tenant/site/account 격리 |
| NAS | 이미지·파일 처리 | file_upload/manual_export, 다운로드 해시 |
| CUSTOM | 고객 정의 사이트/레시피 | origin allowlist와 dry-run 필수 |

사이트 런타임은 `webview2`, `chrome_extension`, `chrome_cdp`, `playwright_server`, `file_upload`, `official_api`, `manual_export`를 명시한다. 사이트 프로필은 tenant + project + site로 격리하며, 레시피는 draft/active/archived 버전과 record type, 정규화 스키마, fixture를 가진다. Windows Collector는 향후 동일 작업 큐의 runtime/work_key 계약에 연결한다.

## MVP 구현

- API: `app/api/authenticated_site_collector.py`
- 서비스: `app/services/authenticated_site_collector.py`, 기존 `browser_recipe_registry.py`, `pc_agent_collection_queue` 테이블
- DB: `migrations/146_authenticated_site_collector.sql`
- UI: `aads-dashboard/src/app/authenticated-collector/page.tsx`, API 클라이언트와 Sidebar
- 테스트: `tests/unit/test_authenticated_site_collector.py`

검증 기준은 tenant/role 가드, 프로젝트·사이트 필터, 구조화 overview, active 레시피만 실행, `browser_recipe` 큐 연결, 개입 작업의 동일 work_key 재개, secret-free payload/audit, 모바일 핵심 상태·재개 버튼 노출이다. 브라우저 실수집 엔진과 WebView2 바이너리는 후속 범위다.
