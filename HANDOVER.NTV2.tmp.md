# 뉴톡 V2 프로젝트 인수인계서

**버전**: 5.4.0
**최종수정**: 2026-03-06 KST (R5-FRONT-PRODUCTS-001: 관리자 상품 CRUD 관리 페이지 구현)
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
| DOCS-FIX-009 | 2026-02-27 | v3.15.0 | — | R4 최종 문서 정합성 복구 (로컬 커밋 완료, push 미완료) |
| DOCS-SETUP-001 | 2026-02-28 | v4.0.0 | — | CEO-DIRECTIVES.md 생성 + HANDOVER.md 표준 8섹션 전환, .cursorrules 인계서 규칙 추가 |
| SEEDER-001 | 2026-02-28 | v4.9.0 | da42612 | 시더 14개 (admin~outsource 6계정, 상품·채널·콘텐츠·파이프라인 등) |
| API-SMOKE-002 | 2026-03-05 | v5.0.0 | b13ae13 | 시드 데이터 기반 API 기능 테스트 (pipeline/dashboard 200 확인) |
| R5-FRONT-PIPELINE-001 | 2026-03-05 | v5.1.0 | — | 콘텐츠 파이프라인 관리자 페이지: pipeline-api.ts(10함수), 페이지 3개, 컴포넌트 6개, 빌드 에러 0 |
| R5-FRONT-PRODUCTS-001 | 2026-03-06 | v5.4.0 | — | 관리자 상품 CRUD 관리 페이지: admin-product-api.ts(5함수), 페이지 2개(/admin/products, /admin/products/[id]), 컴포넌트 4개, 빌드 에러 0 |

---

## 3. 진행 중 작업

| Task ID | 상태 | 내용 |
|---------|------|------|
| V1-FIX-001 | 지시서 전달 | V1 이미지 URL DO→newtalk.kr 치환 |
| DOCS-FIX-009 | push 미완료 | R4 문서 정합성 (로컬 커밋 완료, 원격 push 미완료) |
| CODE-REVIEW-001 | 보고서 확인 대기 | R1~R4 코드 검수 |

---

## 4. 보류/미시작

| 항목 | 선행조건 | 우선순위 |
|------|----------|----------|
| DOCS-FIX-009 push | force-with-lease 실행 | P0 즉시 |
| V1-FIX-001 실행 | Cursor 실행 | P0 즉시 |
| CODE-REVIEW-001 확인 | 보고서 push | P1 단기 |
| R5 기획 | 코드 검수 완료 | P2 중기 |

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

---

## 6. 웹 Claude 인수인계 사항

### 최신 상태 (2026-02-28)
- R4 라운드: 코드 46건 완료 (R0~R4 API+FRONT+DOCS-FIX)
- DOCS-SETUP-001 완료: CEO-DIRECTIVES.md v1.0 생성, HANDOVER.md v4.0.0 (8섹션 표준), .cursorrules 인계서 규칙 추가
- DOCS-FIX-009: 로컬 커밋 완료, 원격 push 미완료 (non-fast-forward)
- V1-FIX-001: 이미지 URL 치환 지시서 전달, Cursor 실행 대기
- CODE-REVIEW-001: 서버 로컬에 보고서 존재, push/내용 미확인

### 웹 Claude가 해야 할 일
1. V1-FIX-001 실행 보고 수신 → Phase 1(조사) 결과 검증 → Phase 2~4 진행 판단
2. CODE-REVIEW-001 보고서 push 확인 → 내용 교차검증 → 이슈 분류 → R-FIX 지시서 작성
3. DOCS-FIX-009 push 완료 확인 (force-with-lease)
4. R5 기획 착수 (CEO 확정 후)

### 대표님 확인 필요 사항
1. V1-FIX-001: newtalk.kr/img/YYYYMM/ 경로 구조가 DO Spaces와 동일한지
2. CODE-REVIEW-001: 발견 이슈 중 긴급 수정 필요 건 우선순위 결정
3. R5 기획 범위·일정 확정

### 주의사항
- Cursor가 git push를 건너뛰는 패턴이 반복됨 → 모든 지시서에 push 단계 명시 필수
- V1 소스는 CodeIgniter 2.x — config/database.php, config/config.php에 도메인 설정 있을 가능성 높음
- 이전 대화에서 non-fast-forward 충돌 발생 → 작업 시작 전 git pull --rebase 선행 필수

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
│   │   ├── R1-TASK-002-report.md
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
| 5.4.0 | 2026-03-06 | R5-FRONT-PRODUCTS-001: 관리자 상품 CRUD 관리 페이지 구현 완료 |
| 5.4.1 | 2026-05-05 | 상품등록 UI 수정기획 v1.2 작성: V1 샘플등록/상품수정 필드, DB 컬럼, 역할별 흐름, V2 누락 항목 비교 반영 |

---

## 9. 2026-05-05 작업 기록

- 산출물: `/srv/newtalk-v2/src/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.2-20260505.html`
- 공개 URL: `https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.2-20260505.html`
- 검수 근거:
  - V1 화면: `/home/danharoo/www/application/views/products/in_form.php`, `/home/danharoo/www/application/views/products/edit_form.php`
  - V1 저장 로직: `/home/danharoo/www/application/controllers/products.php`
  - V1 DB: `goods`, `goods_detail`, `goods_image`, `goods_master`, `goods_option_code`
  - V2 현 구조: `ProductController`, `StoreProductRequest`, `UpdateProductRequest`, 상품 관련 migration
- 결론: V2 상품등록은 현재 기본 CRUD 수준이라 V1 관리자 상품등록/수정 업무를 대체하기 부족하다. 도매/매입처, 촬영/샘플, 채널별 가격/상품명, 상세 스펙, 이미지 20+10 구조를 DB와 UI에 추가해야 한다.
