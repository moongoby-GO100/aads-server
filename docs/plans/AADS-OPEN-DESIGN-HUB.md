# AADS Open Design Hub 기획서

**TASK_ID**: AADS-204
**작성일**: 2026-05-11 13:45 KST
**작성자**: CTO AI
**상태**: 기획 저장 완료 -> Phase 0 러너 투입 예정
**우선순위**: P1
**참고 문서**: `docs/plans/AADS-SMART-DESIGN-SYSTEM.md`

---

## 1. 목표

AADS Open Design Hub는 AADS, GO100, KIS, SF, NTV2 등 각 프로젝트의 디자인을 중앙에서 운영하고, 신규 프로젝트의 디자인 작업을 빠르게 시작·검수·유지할 수 있게 하는 개방형 디자인 운영 시스템이다.

핵심 목표는 다음 4가지다.

| 목표 | 설명 |
|------|------|
| 공통 디자인 언어 | 색상, 타이포그래피, 간격, radius, shadow, 상태 색상, 아이콘 사용 원칙을 중앙 토큰으로 관리 |
| 프로젝트별 어댑터 | Next.js/Tailwind, 정적 HTML/CSS, 기존 자체 CSS 등 서로 다른 프론트 구조에 맞춰 적용 |
| 운영 관측 | 프로젝트별 토큰 적용률, 하드코딩 색상, 접근성 위험, 컴포넌트 중복을 AADS에서 점검 |
| 신규 프로젝트 가속 | 새 프로젝트 생성 시 브랜드/목적/사용자군 입력만으로 토큰, 기본 레이아웃, 컴포넌트 세트를 생성 |

---

## 2. 배경

기존 `AADS-SMART-DESIGN-SYSTEM.md`는 AADS 대시보드 내부의 디자인 시스템 부재를 해결하기 위한 계획이다. 그러나 현재 AADS는 단일 제품이 아니라 여러 프로젝트를 운영하는 관제 시스템이므로, 디자인 시스템도 AADS 대시보드 전용이 아니라 전 프로젝트를 관리하는 Open Design Hub로 확장해야 한다.

현재 확인된 문제는 다음과 같다.

| 영역 | 문제 | 영향 |
|------|------|------|
| 디자인 토큰 | 프로젝트별 색상·간격·상태 표현이 분산 | UI 일관성 저하, 수정 비용 증가 |
| 컴포넌트 | Button, Card, Badge, Modal 등 기본 컴포넌트 재작성 | 개발 속도 저하, 회귀 위험 증가 |
| 아이콘 | 이모지/문자/커스텀 SVG 혼재 | 접근성, 시각 품질, 유지보수 저하 |
| 신규 프로젝트 | 디자인 시작점이 매번 수동 | 초기 화면 품질 편차 발생 |
| 검수 | 디자인 품질을 코드 리뷰에서 수동 확인 | 누락, 주관적 판단, 반복 지적 발생 |

---

## 3. 제품 정의

Open Design Hub는 단순 UI 라이브러리가 아니라 디자인 운영 시스템이다.

### 3-1. Design Registry

프로젝트별 디자인 설정과 적용 상태를 저장하는 중앙 레지스트리다.

| 항목 | 예시 |
|------|------|
| 프로젝트 | AADS, GO100, KIS, SF, NTV2 |
| 브랜드 토큰 | primary, accent, success, warning, danger, surface, text |
| 플랫폼 | Next.js/Tailwind, HTML/CSS, React SPA |
| 적용 어댑터 | `tailwind-v4`, `css-vars`, `legacy-css` |
| 품질 상태 | token coverage, hardcoded colors, contrast risk, component reuse |

### 3-2. Token Studio

디자인 토큰을 만들고 검증하는 화면이다.

필수 기능:

- 색상 팔레트 생성 및 대비 검사
- 다크/라이트 테마 쌍 관리
- spacing, radius, shadow, typography scale 관리
- CSS variables, Tailwind theme, JSON token export
- 프로젝트별 override 이력 관리

### 3-3. Component Catalog

공통 컴포넌트와 프로젝트별 변형을 관리한다.

초기 컴포넌트:

| 컴포넌트 | 용도 |
|----------|------|
| Button | 명령, 아이콘 버튼, 위험 액션 |
| Input | 텍스트 입력, 검색, 필터 |
| Badge | 상태, 우선순위, 모델, 프로젝트 태그 |
| Card | 반복 아이템, 상태 패널 |
| Dialog | 승인, 거부, 위험 조치 확인 |
| Tabs | 관리 화면 내부 뷰 전환 |
| Tooltip | 아이콘 버튼 설명 |
| StatusIndicator | 서버, 러너, 배포, DB 상태 |

### 3-4. Design Auditor

프로젝트 코드를 스캔해 디자인 품질 위험을 수치화한다.

초기 점검 항목:

| 점검 | 탐지 방식 | 결과 |
|------|-----------|------|
| 하드코딩 색상 | `#hex`, `rgb()`, Tailwind arbitrary color 검색 | 색상 토큰화 후보 |
| 이모지 아이콘 | JSX/TSX/HTML 텍스트 내 이모지 탐지 | lucide 아이콘 교체 후보 |
| 중복 버튼 | button class 패턴 군집화 | Button 컴포넌트 전환 후보 |
| 접근성 | aria-label 누락, contrast risk | P0/P1/P2 이슈 |
| 레이아웃 위험 | fixed width, overflow, nested card | 반응형 개선 후보 |

### 3-5. Project Starter

신규 프로젝트 디자인 작업을 시작하는 생성기다.

입력:

- 프로젝트명
- 서비스 성격: admin, SaaS, commerce, social, media, finance
- 브랜드 키워드
- 주 사용 환경: desktop, mobile, mixed
- 선호 밀도: compact, balanced, editorial
- 접근성 수준: baseline, WCAG AA

출력:

- design token JSON
- `design-tokens.css`
- Tailwind preset 또는 CSS variables
- 기본 layout/components scaffold
- 디자인 검수 체크리스트
- 신규 프로젝트 디자인 README

---

## 4. 시스템 아키텍처

```
┌──────────────────────────────────────────────────────────┐
│                    AADS Open Design Hub                  │
├──────────────────────────────────────────────────────────┤
│  Admin UI                                                 │
│  - /admin/design                                          │
│  - /admin/design/projects                                 │
│  - /admin/design/tokens                                   │
│  - /admin/design/audit                                    │
├──────────────────────────────────────────────────────────┤
│  Backend API                                              │
│  - design projects registry                               │
│  - token CRUD/versioning                                  │
│  - audit scan result                                      │
│  - starter generation request                             │
├──────────────────────────────────────────────────────────┤
│  Design Engine                                            │
│  - token compiler                                         │
│  - project adapter                                        │
│  - code scanner                                           │
│  - report generator                                       │
├──────────────────────────────────────────────────────────┤
│  Outputs                                                  │
│  - CSS variables                                          │
│  - Tailwind preset                                        │
│  - component templates                                    │
│  - audit report                                           │
│  - runner directives                                      │
└──────────────────────────────────────────────────────────┘
```

---

## 5. 데이터 모델 초안

### 5-1. `design_projects`

| 컬럼 | 설명 |
|------|------|
| id | UUID |
| project_key | AADS, GO100, KIS, SF, NTV2 |
| display_name | 표시명 |
| frontend_stack | next-tailwind, react, html-css, unknown |
| adapter_key | tailwind-v4, css-vars, legacy-css |
| repo_path | 서버 기준 경로 |
| status | active, paused, archived |
| metadata | JSONB |

### 5-2. `design_token_sets`

| 컬럼 | 설명 |
|------|------|
| id | UUID |
| project_key | 프로젝트 키 |
| version | semver 또는 timestamp |
| mode | global, project, experimental |
| tokens | JSONB |
| created_by | 생성 주체 |
| created_at | 생성 시각 |

### 5-3. `design_audit_runs`

| 컬럼 | 설명 |
|------|------|
| id | UUID |
| project_key | 프로젝트 키 |
| status | running, done, error |
| score | 0~100 |
| findings | JSONB |
| scanned_files | integer |
| created_at | 실행 시각 |

---

## 6. 운영 화면 기획

### 6-1. `/admin/design`

전 프로젝트 디자인 운영 대시보드.

표시 항목:

- 프로젝트별 디자인 점수
- 토큰 적용률
- 하드코딩 색상 수
- 접근성 위험 수
- 최근 audit 시각
- 다음 권장 작업

### 6-2. `/admin/design/projects/[projectKey]`

프로젝트별 상세 화면.

표시 항목:

- 현재 토큰 세트
- 적용 어댑터
- 최근 변경 이력
- audit findings
- 컴포넌트 전환 후보
- runner 작업 생성 버튼

### 6-3. `/admin/design/starter`

신규 프로젝트 디자인 생성 화면.

작업 흐름:

1. 프로젝트 성격 입력
2. 토큰 초안 생성
3. 미리보기
4. scaffold 산출
5. runner directive 생성

---

## 7. 실행 로드맵

### Phase 0: 문서·스키마·스캐너 설계 (P0, 1~2일)

| 작업 | 산출물 |
|------|--------|
| 본 기획서 저장 | `docs/plans/AADS-OPEN-DESIGN-HUB.md` |
| DB 스키마 마이그레이션 초안 | `migrations/0xx_design_hub.sql` |
| 코드 스캐너 PoC | 하드코딩 색상/이모지/중복 버튼 탐지 |
| Admin API 계약 초안 | `/api/v1/admin/design/*` |
| runner 작업 분해 | Phase 1~4 작업 지시서 |

### Phase 1: AADS 자체 적용 기반 (P1, 3~5일)

| 작업 | 산출물 |
|------|--------|
| AADS 디자인 토큰 통합 | `aads-dashboard/src/styles/design-tokens.css` |
| 기본 UI 컴포넌트 | Button, Badge, Card, Dialog, Tooltip |
| Header/Sidebar 1차 전환 | 하드코딩 색상 제거 |
| `/admin/design` 초기 화면 | 프로젝트 목록 + audit placeholder |

### Phase 2: Design Auditor 구현 (P1, 3~5일)

| 작업 | 산출물 |
|------|--------|
| 프로젝트 파일 스캔 | AADS/GO100/KIS/SF/NTV2 경로별 어댑터 |
| findings 저장 | `design_audit_runs.findings` |
| 점수 산식 | token coverage, hardcoding, accessibility |
| 리포트 생성 | Markdown/HTML report |

### Phase 3: Project Starter 구현 (P2, 4~6일)

| 작업 | 산출물 |
|------|--------|
| 신규 프로젝트 입력 폼 | `/admin/design/starter` |
| 토큰 생성기 | JSON/CSS/Tailwind preset |
| 템플릿 생성 | layout, components, README |
| runner directive 연동 | 생성 결과를 작업 지시서로 변환 |

### Phase 4: 전 프로젝트 어댑터 확장 (P2, 5~10일)

| 작업 | 산출물 |
|------|--------|
| GO100/KIS 어댑터 | 서버211 프로젝트 경로 스캔 |
| SF/NTV2 어댑터 | 서버114 프로젝트 경로 스캔 |
| 프로젝트별 적용 계획 | risk-based migration plan |
| 운영 리포트 | 주간 디자인 품질 리포트 |

---

## 8. 품질 기준

| 기준 | 완료 조건 |
|------|-----------|
| 토큰 일관성 | 새 UI는 raw hex 색상을 직접 사용하지 않는다 |
| 컴포넌트 재사용 | 반복 버튼/배지/카드는 공통 컴포넌트를 우선 사용한다 |
| 접근성 | 아이콘 버튼은 aria-label 또는 Tooltip을 갖는다 |
| 반응형 | 375px, 768px, 1280px에서 텍스트와 버튼이 겹치지 않는다 |
| 검수 자동화 | audit 결과가 DB에 저장되고 재조회 가능하다 |
| 프로젝트 독립성 | 각 프로젝트의 기존 디자인을 강제 덮어쓰지 않고 adapter로 점진 전환한다 |

---

## 9. 리스크와 대응

| 리스크 | 영향 | 대응 |
|--------|------|------|
| 프로젝트별 프론트 구조 차이 | 단일 패키지 강제 적용 불가 | 공통 토큰 + 프로젝트별 adapter 구조 |
| 기존 UI 회귀 | 운영 화면 깨짐 | AADS 내부부터 작은 화면 단위로 적용 |
| 과도한 자동 생성 | 브랜드 품질 저하 | generated draft -> preview -> 승인 -> runner 적용 흐름 |
| 스캐너 오탐 | 잘못된 수정 지시 | findings는 자동 수정이 아니라 후보로 저장 |
| 러너 중복 작업 | queue 혼잡 | Phase 단위 순차 제출, scope 제한 |

---

## 10. 첫 러너 작업 제안

첫 작업은 전체 구현이 아니라 Phase 0 기반을 만드는 것으로 제한한다.

**작업명**: AADS-204 Open Design Hub Phase 0
**범위**:

- 이 문서를 기준으로 DB 스키마 초안 작성
- `/api/v1/admin/design` API 계약 초안 또는 최소 route scaffold 작성
- 디자인 스캐너 PoC 함수 작성
- 작업 분해 문서 `docs/plans/AADS-OPEN-DESIGN-HUB-IMPLEMENTATION.md` 작성
- 실제 AADS UI 대규모 변경은 하지 않음

**금지**:

- 기존 AADS 대시보드 스타일 전면 교체 금지
- 배포 스크립트 수정 금지
- 기존 프로젝트 UI 자동 수정 금지
- `.env`, secret, 인증 정보 접근 금지

---

## 11. 결론

Open Design Hub는 AADS의 디자인을 예쁘게 만드는 작업이 아니라, 여러 프로젝트의 디자인 품질을 중앙에서 관측하고, 신규 프로젝트의 디자인 출발점을 표준화하며, 러너가 안전하게 UI 작업을 수행하도록 만드는 운영 시스템이다.

따라서 구현 순서는 다음이 적절하다.

1. 문서와 스키마, audit PoC를 먼저 만든다.
2. AADS 대시보드에 제한적으로 적용해 검증한다.
3. 프로젝트별 adapter를 붙여 전 프로젝트 운영 체계로 확장한다.
