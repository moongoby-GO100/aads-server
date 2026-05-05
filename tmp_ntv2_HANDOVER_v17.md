# 뉴톡 V2 프로젝트 인수인계서

**버전**: 5.6.5
**최종수정**: 2026-05-05 KST (HANDOVER v5.6.5 — 상품등록 UI 기획 v1.5 배포 및 정적 경로 검증 반영)
**목적**: 신규 개발자·AI 에이전트가 프로젝트를 즉시 이해하고 작업할 수 있도록 하는 종합 인계 문서

> **작업 규칙**: docs/CEO-DIRECTIVES.md 참조

---

## 2026-05-05 작업 기록

- 상품등록 UI 기획서 최신본 `NT-PRODUCT-REGISTRATION-UI-PLAN-v1.3-20260505.html` 상단에 버전 히스토리 링크를 추가했다.
- 연결된 이전 버전: `v1.0`, `v1.1`, `v1.1-20260505`, `v1.2-20260505`.
- 배포 위치: `/srv/newtalk-v2/frontend/public/`, `/srv/newtalk-v2/src/public/`.
- 검증: `https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.0.html`부터 `v1.3-20260505.html`까지 모두 HTTP 200 확인.
- 상품등록 UI 기획서 v1.5 `NT-PRODUCT-REGISTRATION-UI-PLAN-v1.5-20260505.html`를 `/srv/newtalk-v2/frontend/public/`에 추가했다.
- v1.5 핵심 변경: 사용자 목적 선택 제거, 자동 작업 컨텍스트 카드, Product Workspace, 이미지 작업실, 상세HTML 빌더, 채널 제공 패키지, API/DB 의존성, 상태/모바일/우선순위까지 구현자 전달용 상세 설계로 확장.
- Apache `00-v2.newtalk.kr.conf` 80/443 VirtualHost에 v1.5 Alias/ProxyPass 예외를 추가하고 `apache2ctl configtest` Syntax OK 후 reload했다.
- 검증: `https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.5-20260505.html` HTTP 200 확인. v1.4도 HTTP 200 유지 확인.

---

## 1. 프로젝트 개요

뉴톡 V2는 V1(CodeIgniter 2.x/PHP 5.4)을 Laravel 12 + Next.js 16으로 재구축하는 프로젝트.
SNS형 B2B SaaS 마켓플레이스로 진화 중.

**핵심 이해관계자**: CEO (moongoby@gmail.com) – 사입 시스템 유일 의사결정자.

### 접속 정보

#### 서버 (rfree-009)
```
SSH: ssh -p 7916 -i ~/.ssh/id_ed25519_newtalk root@114.207.244.86
OS: Ubuntu 20.04
CPU: AMD EPYC 7262 8-Core
RAM: 16 GB
Disk: 875 GB
IP: 114.207.244.86 (V2), 114.207.244.87 (V1 어드민)
Docker: 28.1.1, Compose v2.35.1
```

#### V2 Docker 스택 (/srv/newtalk-v2/)
```
app:      PHP 8.3-FPM (Laravel 12)
nginx:    1.25-alpine → :8080
db:       MySQL 8.0 → :3307
redis:    Redis 7 → :6380
frontend: Next.js 16 → :3000 (R2 추가)
```

#### DB 접속
```
V1 (읽기 전용): mysql -u pigupuser -p -h 127.0.0.1 -P 3306 autoda
  비밀번호: /home/danharoo/www/application/config/database.php 참조
V2 (읽기/쓰기): mysql -u newtalk_v2_user -p -h 127.0.0.1 -P 3307 newtalk_v2
  비밀번호: /srv/newtalk-v2/.env.docker 참조
```

#### NAS
```
Synology DS1821+, IP 192.168.30.23
image-auto 컨테이너: :8100
```

#### Git
```
레포: git@github.com:moongoby/newtalk-v2-api-.git (끝 하이픈 주의)
웹: https://github.com/moongoby/newtalk-v2-api-
```

#### URL
```
V2 API: http://114.207.244.86:8080
V2 Frontend: http://114.207.244.86:3000
V1: http://114.207.244.86
```

#### 테스트 계정 (비밀번호: .env 또는 시더 참조, 인계서에 평문 기록 금지)
```
admin@newtalk.kr (관리자)
md@newtalk.kr (MD)
purchaser@newtalk.kr (사입자)
wholesale@newtalk.kr (도매)
retail@newtalk.kr (소매)
outsource@newtalk.kr (외주)
```

### 기존 시스템 보호 (System A~D)

| ID | 설명 | 규칙 |
|---|---|---|
| A | V1 웹 (114.207.244.86:80) | 수정 금지 |
| B | V1 어드민 (114.207.244.87) | 수정 금지 |
| C | NAS image-auto (192.168.30.23:8100) | 별도 진행 |
| D | ShortFlow AI 쇼츠 | 별도 진행 |

---

## 2. 완료된 작업

상세 내용은 각 docs/reports/{TASK-ID}-report.md 참조.

| Task ID | 날짜 | 버전 | 커밋 SHA | 핵심 결과 |
|---------|------|------|----------|-----------|
| R0 | 2026-02-21 | v0.1.0 | — | Laravel 12 + Docker, V1 스키마 226테이블, 38테이블 마이그레이션, RBAC 6역할 |
| R1-TASK-001 | 2026-02-22 | v1.0.0 | 37ad7e4 | Sanctum 인증 API |
| R1-TASK-002 | 2026-02-22 | v1.0.0 | 876f4b3 | 상품 CRUD API, 모델·이미지·옵션·카테고리 |
| R1-TASK-003 | 2026-02-22 | v1.0.0 | 555ee03 | 발주·입고·바코드 API, 7단계 상태 전이 |
| R1-TASK-004 | 2026-02-22 | v1.0.0 | 67f0a64 | 사입 대시보드 API 6 엔드포인트 |
| R1-TASK-005 | 2026-02-22 | v1.0.0 | be662c7 | 기본 대시보드 + V1 마이그레이션 3커맨드 (users/products/wholesale) |
| R2-FRONT-001 | 2026-02-23 | v1.1.0 | ce541c5 | Next.js 16 셋업, 인증·역할별 라우팅 |
| R2-FRONT-001-DEPLOY | 2026-02-23 | v1.2.0 | 870c007 | 프론트엔드 Docker 배포, :3000 |
| R2-API-001 | 2026-02-23 | v1.3.0 | 520353b | SNS 소셜 엔진 API (피드·팔로우·찜) |
| R2-FIX-001 | 2026-02-24 | v1.4.1 | — | 검수 피드백 반영 (역할체크, 바인딩, wishlist toggle) |
| R2-FRONT-002 | 2026-02-23 | v1.4.0 | 520353b | 홈 피드 + 탐색 UI |
| R2-FRONT-003 | 2026-02-24 | v1.5.0 | 520353b | 상품 상세·찜·공유 UI |
| R2-API-002 | 2026-02-24 | v1.6.0 | 520353b | 브랜드 페이지 API |
| R2-FRONT-004 | 2026-02-24 | v1.6.0 | 520353b | 브랜드 페이지 UI |
| R2-FRONT-005 | 2026-02-24 | v1.7.0 | 520353b | 관리자 구매 대시보드 상세 |
| R2-FRONT-006 | 2026-02-24 | v1.8.0 | 520353b | 도매 콘텐츠 업로드 UI |
| R2-API-003 | 2026-02-25 | v1.9.0 | 520353b | AI 콘텐츠 처리 API |
| R2-API-004 | 2026-02-25 | v2.0.0 | 520353b | 카페24 API 연동 |
| R3-API-001 | 2026-02-25 | v2.1.0 | 87cb07b | 사입 주문 API (장바구니·주문) |
| R3-FRONT-001 | 2026-02-25 | v2.2.0 | b798049 | 사입 주문·장바구니 UI |
| R3-API-002 | 2026-02-25 | v2.3.0 | b798049 | 결제 연동 API (토스페이먼츠) |
| R3-FRONT-002 | 2026-02-25 | v2.4.0 | b798049 | 결제 UI |
| R3-API-003 | 2026-02-25 | v2.5.0 | b798049 | 배송 API |
| R3-FRONT-003 | 2026-02-25 | v2.6.0 | b798049 | 배송 UI |
| R3-API-004 | 2026-02-26 | v2.7.0 | b798049 | DM API |
| R3-FRONT-004 | 2026-02-26 | v2.8.0 | b798049 | DM UI |
| R3-API-005 | 2026-02-26 | v2.9.0 | — | Shorts API |
| R3-FRONT-005 | 2026-02-26 | v2.10.0 | — | Shorts UI |
| R3-API-006 | 2026-02-26 | v2.11.0 | — | 정산 API |
| R3-FRONT-006 | 2026-02-26 | v2.12.0 | — | 정산 UI |
| R4-API-001 | 2026-02-26 | v3.1.0 | — | 거래처 제도 API |
| R4-API-002 | 2026-02-26 | v3.2.0 | — | 스토리 API |
| R4-FRONT-001 | 2026-02-26 | v3.6.0 | — | 거래처 제도 UI |
| R4-API-003 | 2026-02-26 | v3.3.0 | — | AI 맞춤 피드 + 추천 엔진 |
| R4-API-004 | 2026-02-26 | v3.4.0 | — | 셀러 채널 관리 API |
| R4-API-005 | 2026-02-26 | v3.5.0 | — | 콘텐츠 파이프라인 API |
| R4-FRONT-002 | 2026-02-26 | v3.7.0 | — | 스토리 UI |
| R4-FRONT-003 | 2026-02-26 | v3.8.0 | — | AI 추천 피드 UI + 소매 마이페이지 |
| R4-API-006 | 2026-02-26 | v3.9.0 | — | SNS 자동 게시 API |
| R4-API-007 | 2026-02-26 | v3.10.0 | — | 위탁배송 고도화 + 드롭십 API |
| R4-FRONT-006 | 2026-02-26 | v3.14.0 | — | 콘텐츠 파이프라인 UI |
| R4-FRONT-004 | 2026-02-26 | v3.12.0 | — | 셀러 채널 관리 UI |
| R4-FRONT-005 | 2026-02-26 | v3.13.0 | — | SNS 자동 게시 UI |
| R4-FRONT-007 | 2026-02-26 | v3.15.0 | — | 위탁배송·드롭십 UI |
| DOCS-FIX-007 | 2026-02-26 | — | — | SHA 교체 + ARCHITECTURE 재작성 |
| DOCS-FIX-008 | 2026-02-26 | v3.11.0 | — | 4대 핵심 문서 정합성 복구 |
| DOCS-FIX-009 | 2026-02-27 | v3.15.0 | — | R4 최종 문서 정합성 복구 |
| DOCS-SETUP-001 | 2026-02-28 | v4.0.0 | — | CEO-DIRECTIVES.md 생성 + HANDOVER.md 표준 8섹션 전환 |
| R5-PLAN-DRAFT-001 | 2026-03-01 | — | 98050a7 | R5 Phase B 기획 초안 + V1 이미지 경로 조사 |
| R5-B2-MIGRATE-001 | 2026-03-01 | v4.4.0 | 55c73b4 | 결제+배송+정산+쇼츠 12테이블 마이그레이션 |
| ROUTE-CONNECT-B1-001 | 2026-03-02 | v4.3.0 | f39ef28 | B-1 라우트 연결 35EP, 총 142라우트 |
| ROUTE-CONNECT-B2-001 | 2026-03-02 | v4.5.0 | 26ee445 | 결제+배송+정산+쇼츠 라우트 연결 36EP |
| R5-B3-001 | 2026-03-03 | v4.6.0 | 8013204 | 거래처+스토리+AI추천+셀러채널 10테이블 + 25EP 라우트 |
| INTEGRATION-CHECK-001 | 2026-03-04 | — | a3eeb96 | 203라우트 전수 통합 검수 + 빈 모델 2개 fillable 수정 |
| API-TEST-001 | 2026-03-04 | — | 8c4b0e1 | 스모크+Feature Test 완료 |
| CODE-REVIEW-001 | 2026-03-04 | — | a3eeb96 | R1~R4 코드 검수 완료 (203라우트, 97테이블) |
| SEEDER-001 | 2026-03-05 | v4.9.0 | da42612 | 시더 8개: UserSeeder·CategorySeeder·ProductSeeder·OrderSeeder·PurchaseOrderSeeder·ShortSeeder·SettlementSeeder·PartnershipSeeder, users=17, products=46 |
| V1-HOTFIX-001 | 2026-03-04 | — | 9463cfa | V1 이미지 캐시 버스팅 + GoodsEtc73 즉시 반영 |
| V1-HOTFIX-002 | 2026-03-05 | — | 0f1de87 | V1 이미지 동일 파일명 덮어쓰기 수정 (3파일, 3버그 해소) |
| NTV2-VERIFY-001 | 2026-03-05 | v4.8.0 | 0f1de87 | 500 에러 7/7 해소 HTTP 재확인, DropshipService·FulfillmentService·ContentPipelineService 구현 |
| DOCS-SYNC-003 | 2026-03-05 | v5.0.0 | — | HANDOVER v5.0 + CEO-DIRECTIVES v1.1 정합성 복구 |
| R5-FRONT-SETTLE-001 | 2026-03-05 | v5.1.0 | 5a1390b | 정산 프론트엔드 전체 구현: settlement-api.ts 6함수, wholesale/admin 정산 페이지 4개, 컴포넌트 5개, 레이아웃 메뉴 2곳, 빌드 에러 0, API 200 확인 |
| R5-FRONT-PIPELINE-001 | 2026-03-05 | v5.2.0 | 8c63353 | 콘텐츠 파이프라인 관리자 UI: 파이프라인 목록·상세·생성 3페이지+6컴포넌트 |
| FRONTEND-AUDIT-001 | 2026-03-05 | — | 0ddc519 | 프론트엔드 전수 감사: 412 ts/tsx, 78 page.tsx, 12영역 100% 매핑 |
| API-SMOKE-002 | 2026-03-05 | — | f793574 | 스모크 재테스트: 6계정 로그인 성공, 500에러 0, products=46/orders=2/shorts=10/settlements=5 |
| R5-API-HEALTH-001 | 2026-03-05 | v5.2.0 | d58a3fd | GET /api/health 200, DB/Redis/Disk 모니터링 엔드포인트 추가 |
| R5-FRONT-PRODUCTS-001 | 2026-03-06 | v5.4.0 | 3c649f6 | 관리자 상품 CRUD 관리 페이지: AdminProductTable·Detail·DeleteDialog·Filter 4컴포넌트, admin-product-api.ts 6함수 |
| NT-001-Phase-1A | 2026-03-06 | — | 2fd517e | 메신저 백엔드 MVP: DB 스키마 확장, Events 3개, MessengerController 8EP, MessengerService, Reverb 설정 |
| NT-001-Phase-1B | 2026-03-06 | v5.5.0 | — | 메신저 프론트엔드 채팅 UI: types/messenger.ts, messenger-api.ts(8함수), echo.ts(Reverb준비), components/messenger 7컴포넌트, 페이지 3개(admin/wholesale/retail), 레이아웃 메뉴 3곳 |

---

## 3. 진행 중 작업

| Task ID | 상태 | 내용 |
|---------|------|------|
| NT-001-Phase-1B | 완료 | 메신저 프론트엔드 채팅 UI — 7컴포넌트, 8 API함수, 3페이지, 레이아웃 메뉴 3곳 |

---

## 4. 보류/미시작

| 항목 | 선행조건 | 우선순위 |
|------|----------|----------|
| V1-HOTFIX-002 실서버 배포 | CEO 승인 대기 | P0 즉시 |
| V1-FIX-001 Phase 2 | CEO 승인 대기 | P0 즉시 |
| FRONTEND-AUDIT-001 | — | P1 단기 |
| R5 기획 | CEO 확정 | P2 중기 |

---

## 5. 핵심 발견

| 발견 | 날짜 | 영향 |
|------|------|------|
| auth_code 90 사용자 65,580명 미분류 | R1 | 소매/도매 분류 필요 |
| V1 products 컬럼명 차이 | R1 | be662c7에서 해결 |
| R1 브랜치 develop 미병합 | R2 이전 | 정리 필요 |
| Docker mount path 확인 필요 | — | src/ vs 루트 |
| Cursor git push 누락 패턴 반복 | R4 | .cursorrules 자동 push 규칙 추가 필요 |
| DO Spaces URL이 V1에 하드코딩 | V1-FIX-001 | 소스+DB 치환 필요 (CEO 승인 완료) |
| DropshipService·FulfillmentService·ContentPipelineService 미구현 | NTV2-VERIFY-001 | 500 에러 7건 → 구현 완료, HTTP 200 확인 |
| claudebot SSH키/Docker 권한 미복구 | 운영 | V2 repo 13+ 커밋 미push — 수동 push 필요 |
| /api/health disk_free_gb 188.9GB | R5-API-HEALTH-001 | 875GB 디스크 중 188.9GB 여유 — 모니터링 유지 |

---

## 6. 웹 Claude 인수인계 사항

### 최신 상태 (2026-03-06)
- R5 Phase A~B 완료: 203라우트, 97테이블, INTEGRATION-CHECK-001·API-TEST-001·CODE-REVIEW-001 통과
- SEEDER-001 완료: 시더 8개, users=17, products=46, shorts=10, purchase_orders=36
- V1-HOTFIX-001 완료: V1 이미지 캐시 버스팅 + GoodsEtc73 즉시 반영 (2026-03-04)
- V1-HOTFIX-002 완료: V1 이미지 동일 파일명 덮어쓰기 수정 3파일 — **실서버 배포 CEO 승인 대기**
- NTV2-VERIFY-001 완료: 500 에러 7/7 HTTP 200 재확인, DropshipService 등 구현 완료
- DOCS-SYNC-003 완료: HANDOVER v5.0 + CEO-DIRECTIVES v1.1 정합성 복구
- **R5-FRONT-SETTLE-001 완료**: 정산 프론트엔드 전체 구현 (SHA: 5a1390b) — settlement-api.ts 6함수, wholesale/admin 페이지 4개, 컴포넌트 5개, 빌드 에러 0, API 200
- **R5-FRONT-PIPELINE-001 완료**: 콘텐츠 파이프라인 관리자 UI 3페이지+6컴포넌트 (SHA: 8c63353)
- **FRONTEND-AUDIT-001 완료**: 412 ts/tsx, 78 page.tsx, 12영역 100% 매핑 (SHA: 0ddc519)
- **API-SMOKE-002 완료**: 6계정 로그인 성공, 500에러 0, products=46/orders=2/shorts=10/settlements=5 (SHA: f793574)
- **R5-API-HEALTH-001 완료**: GET /api/health 200, DB/Redis/Disk 모니터링 (SHA: d58a3fd)
- **R5-FRONT-DROPSHIP-001 완료**: 드롭십 프론트 전면 개선 (SHA: 3a0c6aa) — types/dropship.ts, dropship-api.ts(6함수), wholesale 2페이지 개선, 컴포넌트 4개(DropshipProductCard·OrderTable·StatusBadge·StatsWidget)
- **R5-FRONT-USERS-001 완료**: 관리자 사용자 관리 페이지 구현 (T-022) — types/admin-user.ts, admin-user-api.ts(4함수), admin/users 2페이지, 컴포넌트 3개(AdminUserTable·AdminUserFilter·AdminUserRoleBadge), admin-layout 메뉴 추가
- **NT-001 Phase 1-A 완료** (SHA: 2fd517e): 메신저 백엔드 MVP — DB 스키마 확장, Events 3개(MessageSent·MessageReadEvent·UserTyping), MessengerController 8EP, MessengerService, Reverb 설정
- **NT-001 Phase 1-B 완료**: 메신저 프론트엔드 채팅 UI — types/messenger.ts, messenger-api.ts(8함수), echo.ts(Reverb준비·polling fallback), 7컴포넌트(MessengerLayout·ConversationList·ConversationItem·MessageView·MessageBubble·MessageInput·TypingIndicator), 페이지 3개(admin/wholesale/retail /messenger), 레이아웃 메뉴 3곳

### 웹 Claude가 해야 할 일 (다음 작업 큐)
1. V1-HOTFIX-002 실서버 배포 — CEO 승인 수신 후 진행 (P0)
2. V1-FIX-001 Phase 2 — CEO 승인 수신 후 이미지 URL 치환 실행 (P0)
3. NT-001 Phase 1-B 빌드 검증 — Docker 컨테이너 재기동 후 `npm run build` 실행 (P1)
4. NT-001 Phase 1-B Reverb 활성화 — `npm install laravel-echo pusher-js` → echo.ts ECHO_ENABLED=true (P1, Reverb 기동 후)
5. NT-002, NT-003 — 별도 지시서 수신 후 진행 (P2)
6. R5 기획 착수 — CEO 확정 후 (P2)

### 대표님 확인 필요 사항
1. V1-HOTFIX-002 실서버 배포 승인
2. V1-FIX-001 Phase 2 실행 승인 (이미지 URL DO Spaces → newtalk.kr 치환)
3. R5 기획 범위·일정 확정

### 주의사항
- V1-HOTFIX-002: 실서버 배포 시 기존 이미지 파일 덮어쓰기 가능 — CEO 승인 후 진행
- V1-FIX-001 Phase 2: V1 소스·DB 수정 포함 — CEO 건별 승인 필수
- DropshipService·FulfillmentService·ContentPipelineService 구현 완료, 실제 외부 연동은 미설정

---

## 7. 문서 위치 + 업데이트 규칙

```
/srv/newtalk-v2/
├── docs/
│   ├── CEO-DIRECTIVES.md              ← CEO 지시 (필수 읽기)
│   ├── planning/
│   │   └── NT-V2-PLAN-002-FINAL.md     ← 기획서 (8레이어, 66화면)
│   ├── architecture/
│   │   └── NT-V2-ARCHITECTURE.md       ← 시스템 아키텍처
│   ├── handover/
│   │   └── HANDOVER.md                 ← 이 문서 (인수인계서)
│   ├── reports/
│   │   ├── R1-TASK-001-report.md
│   │   ├── … (기타 보고서)
│   ├── v1-analysis/
│   │   └── v1-purchasing-analysis.md
│   ├── scripts/
│   ├── CHANGELOG.md
│   └── README.md
├── .cursorrules
├── frontend/                           ← Next.js 16 (R2)
├── src/ 또는 루트                      ← Laravel 12
├── docker-compose.yml
└── .env.docker                         ← DB/Redis 비밀번호 (커밋 금지)
```

### 업데이트 규칙
- 작업 완료 시: 섹션 2, 3, 5, 6 갱신
- push 대상: V2 repo(/srv/newtalk-v2) + project-docs repo
- 확인: curl raw URL → HTTP 200

---

## 8. 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| 1.0.0 | 2026-02-23 | R1 완료 + R2 착수 상태 기준 초판 |
| 2.x | 2026-02-24~26 | R2/R3 완료, R4-API-001·002, R4-FRONT-001 |
| 3.0.0 | 2026-02-26 | DOCS-FIX-008: 완료 항목 정합성 복구; R4-FRONT-006 콘텐츠 파이프라인 UI 완료 반영 |
| 3.0.1 | 2026-02-27 | DOCS-FIX-009: R4-FRONT-004·005·007 완료 반영, R4 라운드 종결 |
| 4.0.0 | 2026-02-28 | DOCS-SETUP-001: 표준 8섹션 구조 전환, 섹션 6 웹 Claude 인수인계 추가, CEO-DIRECTIVES.md 분리 |
| 4.9.0 | 2026-03-05 | SEEDER-001: 시더 8개, users=17, products=46 |
| 5.0.0 | 2026-03-05 | DOCS-SYNC-003: SEEDER-001·V1-HOTFIX-001·002·NTV2-VERIFY-001 완료 반영, R5 Phase A~B 완료 추가, 보류/미시작 갱신, 섹션 6 갱신 |
| 5.1.0 | 2026-03-05 | R5-FRONT-SETTLE-001: 정산 프론트엔드 전체 구현 (settlement-api.ts 6함수, 페이지 4개, 컴포넌트 5개, 레이아웃 2곳, 빌드 에러 0, API 200) |
| 5.2.0 | 2026-03-05 | T-011~T-019 완료 반영: API-SMOKE-002(6계정 로그인·500에러 0), FRONTEND-AUDIT-001(412 ts/tsx·78 page.tsx·12영역), DOCS-SYNC-003, R5-FRONT-SETTLE-001(정산 4페이지), R5-FRONT-PIPELINE-001(파이프라인 3페이지), R5-API-HEALTH-001(헬스체크), 알려진 이슈 2건 추가 |
| 5.3.0 | 2026-03-05 | T-020 R5-FRONT-DROPSHIP-001: 드롭십 타입·API 분리, wholesale 2페이지 개선, 컴포넌트 4개 신규 |
| 5.4.0 | 2026-03-06 | T-022 R5-FRONT-USERS-001: 관리자 사용자 관리 페이지 (목록+상세), types/admin-user.ts, admin-user-api.ts(4함수), 컴포넌트 3개, admin-layout 메뉴 추가 |
| 5.5.0 | 2026-03-06 | NT-001-Phase-1B: 메신저 프론트엔드 채팅 UI — types/messenger.ts, messenger-api.ts(8함수), echo.ts(Reverb준비), 7컴포넌트(MessengerLayout·ConversationList·ConversationItem·MessageView·MessageBubble·MessageInput·TypingIndicator), 페이지 3개, 레이아웃 메뉴 3곳(admin/wholesale/retail) |
| 5.6.0 | 2026-04-29 | NT-ORDER-02 직접 보강: `migrate:v1-orders` 주문 청크 100건으로 축소, 주문별 `order_product` 로딩으로 메모리 피크 완화, V1 `order_barcode` → V2 `barcodes` 이관 추가. 운영 컨테이너 `php -l` 통과, `--dry-run --limit=1` 기준 source_items 971 / source_barcodes 2,472 / failed_orders 0 확인. `runner-cb96f6a2`는 LiteLLM 원격 러너 미기동으로 queued 정체되어 superseded 처리. |
| 5.6.1 | 2026-04-29 | NT-WHOLESALE-IMAGE-DETAIL-HOTFIX: 도매회원 상품 API에 V1 `goods_detail` 이미지/상세설명 fallback 추가, `/api/legacy/product-images/{code}/{filename}` 로컬 이미지 서빙 라우트 추가. 샘플 상품 77046 기준 thumbnail `/api/legacy/product-images/bl5890k62/bl5890k62-s_7.jpg`, images 48건, description 1,885 bytes, 이미지 라우트 HTTP 200 확인. |
| 5.6.2 | 2026-04-29 | NT-WHOLESALE-IMAGE-URL-HOTFIX: 도매회원 상품 API의 V1 이미지 fallback URL을 HTTPS 절대 URL(secure_url)로 변경. test111 계정 기준 상품 목록 thumbnail이 https://v2.newtalk.kr/api/legacy/product-images/... 로 반환되고 이미지 라우트 HTTP 200 image/jpeg 확인. |
| 5.6.3 | 2026-04-29 | NT-WHOLESALE-SUPPLIER-INFO-HOTFIX: 도매회원 상품 API에 `wholesale` 도매처 객체와 `supplier_name/shop_name/contact_phone/shop_location` 필드 추가, 상품 목록/상세 UI에 도매처 상호·연락처·위치 표시. V1 이미지 fallback은 브라우저 동일 출처 상대경로(`/api/legacy/product-images/...`)로 복구. test111 계정 기준 total 8,594건, 첫 상품 supplier_name `중국사입`, contact_phone `01037460511`, 이미지 라우트 HTTP 200 확인. frontend 이미지 재빌드 및 컨테이너 재기동 완료. |
| 5.6.4 | 2026-05-05 | PRODUCT-REG-UI-PLAN-v1.4: 상품등록 목적 수동 선택 제거, 로그인 역할·진입 경로·연결 업무·판매 채널 기반 자동 목적분류 기획 반영. 구현 전 상세 목업 HTML `NT-PRODUCT-REGISTRATION-UI-PLAN-v1.4-20260505.html` 생성 및 `frontend/public`, `src/public` 배포. Apache 80/443 Alias/ProxyPass 예외 추가 후 HTTP/2 200 확인. |
| 5.6.5 | 2026-05-05 | PRODUCT-REG-UI-PLAN-v1.5: 상품등록 UI 기획을 즉시 구현용 상세 설계서로 확장. 자동 컨텍스트, Product Workspace, 이미지 작업실, 상세HTML 빌더, 채널 제공 패키지, API/DB 의존성, 상태/모바일/구현 우선순위 포함. `frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.5-20260505.html` 배포, Apache 80/443 Alias/ProxyPass 예외 추가, HTTP 200 확인. |
| 5.6.6 | 2026-05-05 | PRODUCT-REG-UI-PLAN-v1.7: V1/V2 갭 해소 전략을 이미지형 UI 보고서로 버전업. 전환 아키텍처 보드, 상품목록 UI, 상품 작업실 UI, 이미지 등록/정렬/상세HTML 빌더, 상품코드 자동채번 패널, 역할별 의존성, 구현 전환 순서 포함. `frontend/public`와 `src/public`에 배포, Apache 80/443 Alias/ProxyPass 예외 추가, HTTP 200 확인. |

## 2026-05-05 12:45 KST - 상품등록 UI 기획 v1.6 반영
- 신규 문서: `/srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.6-20260505.html`
- 복제 문서: `/srv/newtalk-v2/src/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.6-20260505.html`
- 반영 내용: V1 상품코드 수동 입력 현황과 DB 코드 패턴 기반 V2 자동채번 조건, 샘플/상품등록/상품수정 조건, V2 상품 상세 탭 설계, 관리자 상품목록 연동 기획.
- Apache 정적 예외: 80/443 VirtualHost에 v1.6 Alias/ProxyPass 예외 추가 후 reload.
- 검증: `https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.6-20260505.html` HTTP/2 200, 핵심 섹션 포함 확인.

## 2026-05-05 13:08 KST - 상품등록 UI 기획 v1.7 이미지형 보고서 반영
- 신규 문서: `/srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.7-20260505.html`
- 복제 문서: `/srv/newtalk-v2/src/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.7-20260505.html`
- 반영 내용: 전환 아키텍처 보드, 뉴톡 관리자 상품목록 UI 이미지, 상품 작업실 상세 UI 이미지, 이미지 등록/정렬/상세HTML 빌더 UI 이미지, 상품코드 자동채번 패널, 역할별 상품등록 의존성, V1 기능 인벤토리와 V2 구현 우선순위.
- Apache 정적 예외: 80/443 VirtualHost에 v1.7 Alias/ProxyPass 예외 추가 후 reload.
- 검증: `https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.7-20260505.html` HTTP 200, `UI 이미지 A/B/C/D` 및 `전환 아키텍처 보드` 섹션 포함 확인.
