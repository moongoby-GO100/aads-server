# 뉴톡 V2 프로젝트 인수인계서
**버전**: 5.8.0

**최종수정**: 2026-03-09 KST (HANDOVER v5.8.0 — NTV2-043 Reverb WebSocket 전체 정리)
**목적**: 신규 개발자·AI 에이전트가 프로젝트를 즉시 이해하고 작업할 수 있도록 하는 종합 인계 문서

> **작업 규칙**: docs/CEO-DIRECTIVES.md 참조

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
reverb:    Laravel Reverb → :6001 (WS, NTV2-043-BRIDGE)
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
V2 도메인(HTTPS): https://v2.newtalk.kr
V2 API: http://114.207.244.86:8080
V2 Frontend: http://114.207.244.86:3000
V2 PM 모니터링: https://v2.newtalk.kr/api/health/full (또는 ?key={HEALTH_API_KEY})
V2 기본 헬스: https://v2.newtalk.kr/api/health
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
| NTV2-027 | 2026-03-06 | v5.6.0 | 9cc3f52 | Git 미push 커밋 전량 동기화 완료 (origin/main 최신화) |
| NTV2-028 | 2026-03-06 | v5.6.0 | 9cc3f52 | 프론트엔드 --no-cache 재빌드 성공, admin 6페이지+컴포넌트 7개 확인 |
| NTV2-029 | 2026-03-06 | v5.6.0 | 96c83ab | v2.newtalk.kr 도메인 Nginx 프록시 연결 (HTTPS + API + WebSocket) |
| NTV2-033 | 2026-03-06 | v5.6.0 | 1d75a49 | PM 원격 모니터링 API /api/health/full 구축 (10개 섹션: infra·db·routes·users·git·frontend·docker·docs·reverb·domain) |
| NTV2-030 | 2026-03-06 | v5.6.0 | — | 테스트 계정 6개 비밀번호 일괄 변경 완료 |
| NTV2-031 | 2026-03-06 | v5.6.0 | 22b4fa3 | Reverb WebSocket 설정 추가 (.env.docker BROADCAST_CONNECTION=reverb, echo.ts 활성화) |
| NTV2-041 | 2026-03-07 | v5.7.0 | 839c947 | PM 모니터링 API /api/health/full 구축 |
| NTV2-042 | 2026-03-07 | v5.7.0 | bd92098 | health/full 컨테이너 격리 이슈 개선 (git/frontend/reverb 멀티패스) |
| NTV2-043 | 2026-03-09 | v5.8.0 | 7006386 | Reverb WebSocket 전체 정리: reverb 컨테이너 포트 8081→6001, docker nginx /app/→reverb:8080 수정, 수동 인스턴스 정리, .env.docker 인증키 통일 |

---

## 3. 진행 중 작업

현재 진행 중 작업 없음.

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
| PM 원격 모니터링 API 구축 | NTV2-033 | /api/health/full로 Git·Docker·빌드·계정·Reverb 등 전체 상태 실시간 확인 가능. HEALTH_API_KEY는 .env.docker 참조 |

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
- **NTV2-033 완료**: PM 원격 모니터링 API — https://v2.newtalk.kr/api/health/full
  반환 항목: infra(PHP·Laravel·DB·Redis·Disk), database(테이블 수·레코드), routes(API 라우트·컨트롤러),
  users(역할별·테스트계정), git(브랜치·커밋·미push), frontend(빌드·page수·메신저파일),
  docker(컨테이너 상태), docs(HANDOVER·CONTEXT·CEO-DIR 버전), reverb(포트·설정), domain(Nginx 설정)
- **NTV2-041 완료** (SHA: 839c947): PM 모니터링 API /api/health/full 구축
- **NTV2-042 완료** (SHA: bd92098): health/full 컨테이너 격리 이슈 개선 — git/frontend/reverb 멀티패스 탐색 (base_path+/srv/newtalk-v2 / 127.0.0.1+reverb+host.docker.internal)
- **NTV2-043 완료** (SHA: 7006386, BRIDGE: 2026-03-11): Reverb WebSocket 전체 정리 — reverb 컨테이너 포트 8081→6001 (docker-compose.yml), docker nginx /app/ → reverb:8080 수정, 수동 reverb 인스턴스 정리, .env.docker 인증키 통일. 검증: port_6001 listening, WebSocket 101 확인

### 웹 Claude가 해야 할 일 (다음 작업 큐)
1. V1-HOTFIX-002 실서버 배포 — CEO 승인 수신 후 진행 (P0)
2. V1-FIX-001 Phase 2 — CEO 승인 수신 후 이미지 URL 치환 실행 (P0)
3. NT-001 Phase 1-B Reverb 활성화 — frontend에서 `npm install laravel-echo pusher-js` → echo.ts ECHO_ENABLED=true (P1, Reverb :6001 기동 완료)
4. NT-002, NT-003 — 별도 지시서 수신 후 진행 (P2)
5. R5 기획 착수 — CEO 확정 후 (P2)

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
| 5.6.0 | 2026-03-06 | NTV2-027~031: Git push 동기화, 프론트엔드 빌드, v2.newtalk.kr 도메인 연결, 테스트 계정 변경, Reverb 설정 + HANDOVER 갱신 |
| 5.7.0 | 2026-03-07 | NTV2-041/042: PM 모니터링 API 구축 + 컨테이너 격리 이슈 개선 (git/frontend/reverb 멀티패스) |
| 5.8.0 | 2026-03-09 | NTV2-043: Reverb WebSocket 전체 정리 — reverb 컨테이너 포트 8081→6001, docker nginx /app/→reverb:8080, 수동 인스턴스 정리, .env.docker 인증키 통일 |
| 5.6.0 | 2026-03-06 | NTV2-029 v2.newtalk.kr 도메인 연결, NTV2-033 PM 원격 모니터링 API /api/health/full 구축, URL 항목에 도메인+모니터링 경로 추가 |
| 5.5.0 | 2026-03-06 | NT-001-Phase-1B: 메신저 프론트엔드 채팅 UI — types/messenger.ts, messenger-api.ts(8함수), echo.ts(Reverb준비), 7컴포넌트(MessengerLayout·ConversationList·ConversationItem·MessageView·MessageBubble·MessageInput·TypingIndicator), 페이지 3개, 레이아웃 메뉴 3곳(admin/wholesale/retail) |

---

## 2026-05-05 상품등록 UI 기획 v1.1 기록

- CEO 지시: V2 상품등록 목적을 재정의하고, 도매 직접등록/뉴톡 대행등록/촬영서비스 후 등록/뉴톡 자체 소비자 판매를 모두 반영한 별도 HTML 기획서 작성.
- 확인 근거:
  - V2 관리자 상품 상세: `/srv/newtalk-v2/frontend/src/components/admin-product/AdminProductDetail.tsx`
  - V2 상품 타입/API: `/srv/newtalk-v2/frontend/src/types/admin-product.ts`, `/srv/newtalk-v2/frontend/src/lib/admin-product-api.ts`
  - V2 도매 상품 콘솔: `/srv/newtalk-v2/frontend/src/components/wholesale/wholesale-console.tsx`
  - V1 샘플등록: `/home/newpigup3/views/productSample/product_sample_mng_add.php`
  - V2 products migration: `/srv/newtalk-v2/src/database/migrations/2026_02_21_100005_create_products_table.php`
- 산출물:
  - 문서 원본: `/srv/newtalk-v2/docs/planning/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.1-20260505.html`
  - Laravel public: `/srv/newtalk-v2/src/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.1-20260505.html`
  - Next public: `/srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.1-20260505.html`
- 핵심 결정 초안:
  - 상품등록 첫 단계는 `등록 목적 선택`으로 둔다.
  - 상품 마스터와 판매 채널/촬영 작업/상품정보고시는 분리한다.
  - 현재 V2의 `active/inactive/pending` 판매 상태와 별도로 `draft/review/shooting/ready` 계열 업무 상태가 필요하다.
  - V1 샘플등록의 분류, 구분, 브랜드, 도매처, 도매상품명, 도매가, 중국원가, 원산지, 혼용율, 색상, 사이즈, 원단느낌, 스타일, 연령타겟, 착장컬러, 촬영요청, 상품이미지, 샘플 참고사진, 촬영 컨셉을 V2 필드 그룹에 반영한다.
- CEO 확인 필요:
  - 초기 대행등록 상품의 공식 소유자를 `임시 소유 후 도매 승인`으로 둘지.
  - 촬영서비스 상품을 `상품 초안 먼저 생성` 방식으로 진행할지.
  - 상품정보고시 필수 시점을 `소비자 공개 전`으로 둘지.

## 2026-05-05 도매 상품 이미지/다운로드 1차 조치

- 적용 범위:
  - `/srv/newtalk-v2/src/app/Http/Controllers/Api/WholesaleDashboardController.php`
  - `/srv/newtalk-v2/src/routes/api.php`
  - `/srv/newtalk-v2/frontend/src/lib/api.ts`
  - `/srv/newtalk-v2/frontend/src/lib/wholesale-dashboard-api.ts`
  - `/srv/newtalk-v2/frontend/src/components/wholesale/wholesale-console.tsx`
- 조치 내용:
  - V1 이미지명만으로 URL을 만들던 상세 이미지 fallback을 실제 파일 존재 확인 기반으로 변경.
  - DB 이미지명이 누락된 경우 `/v1_img/{GoodsCode}` 디렉터리 스캔 결과로 대체 이미지 목록을 보강.
  - `GET /api/wholesale/products/{product}/download-images` 원본 이미지 ZIP 엔드포인트 추가.
  - ZIP에는 받을 수 있는 파일을 우선 포함하고, 누락 파일이 있으면 `DOWNLOAD_NOTICE.txt`와 `X-Image-*` 헤더로 expected/available/missing 수를 전달.
  - 도매 상품 상세의 `원본 이미지` 버튼을 실제 ZIP 다운로드 동작에 연결하고, 누락 수를 화면에 안내.
- 검증:
  - 컨테이너 PHP 문법 검사 통과: `php -l app/Http/Controllers/Api/WholesaleDashboardController.php`
  - 라우트 캐시 정리 후 라우트 등록 확인: `api/wholesale/products/{product}/download-images`
  - 비인증 API JSON 요청은 `401` 확인.
  - 전체 TypeScript 검사는 기존 `src/lib/pipeline-api.ts` 타입 오류로 실패. 이번 변경 파일의 직접 오류는 별도 확인 필요.

## 2026-05-05 V1 JSZip 이미지 다운로드 보완 조치

- 배경:
  - `bag513k64` 리쥬네브 도매처 다운로드에서 76개 중 27개가 `Failed to fetch`로 반복 누락.
  - 서버 파일은 존재하나 `https://img.newtalk.kr/data/files/goods/goodscode/img/bag513k64/bag513k64-s_35.jpg` 공개 CDN 요청은 `403`으로 재현.
- 적용 범위:
  - `/home/danharoo/www/application/views/products/goods_code.php`
  - `/home/danharoo/www/supplier/views/products/goods_list.php`
  - `/home/danharoo/www/minimall/views/bottom.php`
  - `/home/danharoo/www/application/controllers/products.php`
  - `/home/danharoo/www/supplier/controllers/products.php`
  - `/home/danharoo/www/minimall/controllers/goods.php`
- 조치 내용:
  - JSZip 1차 다운로드는 CDN/CF URL 유지.
  - 파일별 2회 재시도 후 실패하면 같은 로그인 세션의 서버 보완 경로를 추가 2회 호출.
  - 서버 보완 경로는 전체 ZIP이 아니라 실패한 단일 이미지 파일만 `readfile()`로 내려줌.
  - manifest JSON에 `fallback_url` 추가: 관리자/도매 `/products/goods_zip_file`, 미니몰 `/goods/goods_zip_file`.
  - 최종 실패한 파일만 기존 `다운로드_안내.txt`와 `goods_zip_download_log`에 남김.
- 검증:
  - PHP 문법 검사 통과: 위 컨트롤러 3개, 뷰 3개.
  - 운영 반영 백업: `*.bak_20260505_095514_fallback`.
  - `bag513k64` 원본 파일 수: 76개, `bag513k64-s_35.jpg` 서버 파일 존재 확인.
  - CDN 직접 요청은 403 확인, 보완 경로는 로그인 세션 필요로 비인증 curl에서 `/auth/login` 302 확인.

## 2026-05-05 상품등록 UI 기획 v1.2 기록

- CEO 지시: 상품등록 흐름도 추가, 역할별 상품등록 흐름 의존성 정리, V1 샘플등록/상품수정 페이지 전체 필드와 DB 재검수 후 V2 필요 항목 비교 반영.
- 확인 근거:
  - V1 관리자 샘플등록/상품등록: `/home/danharoo/www/application/views/admin/products/in_form.php`
  - V1 관리자 상품수정: `/home/danharoo/www/application/views/admin/products/edit_form.php`
  - V1 저장 로직: `/home/danharoo/www/application/controllers/admin/products.php`
  - V1 상품 상태 흐름: `/home/danharoo/www/application/views/products/goods_list.php`
  - V1 DB 스키마: `goods`, `goods_master`, `goods_detail`, `goods_code` INFORMATION_SCHEMA 조회
  - V2 관리자 상품상세: `/srv/newtalk-v2/frontend/src/components/admin-product/AdminProductDetail.tsx`
  - V2 상품 DB 구조: `products`, `product_images`, `product_options`, `product_details` migrations
- 산출물:
  - Next public: `/srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.2-20260505.html`
  - Laravel public: `/srv/newtalk-v2/src/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.2-20260505.html`
  - 브라우저 URL: `https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.2-20260505.html`
- 반영 내용:
  - 상품등록 목적을 도매 직접등록, 뉴톡 대행등록, 촬영서비스 연동, 뉴톡 자체 소비자 직접판매로 재정의.
  - 역할별 흐름도를 3개 시나리오로 추가하고, 관리자/도매/촬영팀/웹작업자/소매/소비자 의존성 매트릭스 작성.
  - V1 샘플등록/상품수정 필드 약 60개와 DB 저장 테이블을 V2 필요 항목으로 재분류.
  - V2 현재 상품상세가 V1 대체에 부족한 항목: 촬영/샘플/BizProgress, 마켓별 상품명, 상품코드 이미지 URL, 상세스펙, 세금/배송, 매입처 검증.
- 배포 조치:
  - Apache `00-v2.newtalk.kr.conf` 80/443 VirtualHost에 v1.2 Alias 및 `ProxyPass !` 추가.
  - `apachectl configtest` 결과 `Syntax OK`.
  - `systemctl reload apache2` 완료.
  - 외부 URL `HTTP/2 200` 확인.
