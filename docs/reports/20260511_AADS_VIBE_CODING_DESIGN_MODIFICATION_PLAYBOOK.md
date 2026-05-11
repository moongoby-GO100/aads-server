# AADS AI Vibe Coding Design Modification Playbook

**작성시각**: 2026-05-11 17:37 KST  
**작성자**: AADS CTO AI  
**상태**: 상세문서 저장 완료  
**대상**: AI 바이브코딩으로 개발된 서비스의 세밀한 디자인 수정 요청과 맥락 유지 체계  
**참고 문서**:
- `docs/reports/20260511_AADS_VIBE_CODING_DESIGN_SERVICE_RESEARCH.md`
- `docs/reports/20260511_AADS_VIBE_DESIGN_USER_JOURNEY_AND_QUALITY.md`
- `docs/plans/AADS-SMART-DESIGN-SYSTEM.md`
- `docs/plans/AADS-OPEN-DESIGN-HUB.md`
- `migrations/082_open_design_hub.sql`
- `app/services/design_audit_service.py`

---

## 1. 핵심 결론

AI 바이브코딩에서 세밀한 디자인 수정이 어려운 이유는 사용자의 표현력이 부족해서가 아니다.
대부분의 실패는 AI에게 다음 정보가 안정적으로 전달되지 않기 때문에 발생한다.

| 누락되는 정보 | 현상 | 해결 방향 |
|---|---|---|
| 현재 화면 맥락 | AI가 기존 레이아웃, 컴포넌트, 반응형 구조를 깨뜨림 | 화면별 `Design Context Pack` 자동 생성 |
| 변경 범위 | 요청하지 않은 페이지나 기능까지 바뀜 | `수정 카드`에 변경 대상과 금지 범위 명시 |
| 디자인 의도 | "더 고급스럽게", "깔끔하게"를 AI가 임의 해석 | 감각어를 토큰, 밀도, 위계, 컴포넌트 규칙으로 변환 |
| 기존 결정 이력 | 이전에 정한 색상, 톤, 금지 패턴이 반복적으로 사라짐 | `Design Memory`와 `DESIGN.md`를 프로젝트 자산으로 저장 |
| 검수 기준 | AI가 완료라고 했지만 실제 화면은 어긋남 | Before/After screenshot, DOM, a11y, visual QA로 닫기 |

따라서 AADS가 제공해야 할 핵심 제품은 단순한 "프롬프트 입력창"이 아니라,
**사용자의 추상적 수정 의도를 구조화하고, AI가 기존 디자인 맥락을 잃지 않게 자동 주입하며,
실제 화면으로 검수하는 디자인 수정 운영 시스템**이다.

---

## 2. 현재 보고서 평가

### 2.1 기존 조사 보고서의 장점

`20260511_AADS_VIBE_CODING_DESIGN_SERVICE_RESEARCH.md`는 외부 생태계 흐름을 잘 정리한다.
특히 `DESIGN.md`, Figma MCP, Storybook MCP, shadcn registry, Open Design, Dyad, bolt.diy를
AADS 구조와 연결한 점이 유효하다.

| 항목 | 평가 | 근거 |
|---|---|---|
| 최신 디자인 개발 흐름 | 좋음 | DESIGN.md, MCP, registry, sandbox preview를 연결 |
| AADS 차별화 방향 | 좋음 | Runner, PromptCompiler, ArtifactPanel과 결합하는 방향 제시 |
| 품질 검수 관점 | 보통 이상 | 5D review, visual QA, Storybook test 제시 |
| 실제 수정요청 UX | 부족 | 사용자가 어떤 형식으로 요청해야 하는지 구체 카드가 없음 |
| 맥락 유지 저장 구조 | 부족 | 디자인 기억, 결정 이력, 적용 범위의 DB/API 설계가 약함 |
| 세밀 수정 루프 | 부족 | "색/간격/폰트/컴포넌트/반응형" 단위 수정 프로토콜이 없음 |

### 2.2 사용자 여정 문서의 장점

`20260511_AADS_VIBE_DESIGN_USER_JOURNEY_AND_QUALITY.md`는 서비스 흐름을 제품 단위로 확장한다.
`Intake -> Design Brief -> Source Context -> DESIGN.md -> Preview -> QA -> Runner Apply` 흐름은
AADS의 기존 구조와 잘 맞는다.

다만 CEO의 현재 질문은 "디자인 서비스를 어떻게 만들 것인가"보다 더 좁다.
핵심은 **이미 AI로 만들어진 서비스를 어떻게 디테일하게 고치게 할 것인가**이다.
따라서 추가로 필요한 것은 다음 세 가지다.

1. 사용자가 쉽게 작성할 수 있는 수정 요청 문법
2. AI가 기존 개발 맥락과 디자인 결정을 유지하는 컨텍스트 패키지
3. 수정 후 실제 화면이 의도와 맞는지 판정하는 검수 루프

---

## 3. 왜 AI는 세밀한 디자인 수정을 자주 실패하는가

### 3.1 자연어만으로는 수정 범위가 너무 넓다

예를 들어 "이 화면을 더 세련되게 고쳐줘"라는 요청은 사람에게도 모호하다.
AI는 보통 다음 중 하나를 임의로 선택한다.

| AI가 임의 선택하는 축 | 가능한 해석 |
|---|---|
| 색상 | 채도를 낮춤, 포인트 색 변경, 배경을 어둡게 함 |
| 레이아웃 | 카드 간격을 키움, 그리드를 바꿈, 섹션 순서를 바꿈 |
| 타이포그래피 | 폰트 크기, 굵기, 줄간격 변경 |
| 컴포넌트 | 버튼, 배지, 입력창 스타일 변경 |
| 분위기 | SaaS, 금융, 커머스, 미디어, 게임 등 임의 톤 적용 |

사용자는 "헤더가 답답하다"는 뜻이었는데, AI는 전체 색상 체계를 바꿀 수 있다.
이것이 바이브코딩 디자인 수정의 대표적인 실패다.

### 3.2 AI는 현재 화면을 직접 보고 있는 것이 아니다

대부분의 코드 에이전트는 파일 일부, 이전 대화, 사용자의 설명만 보고 수정한다.
실제 브라우저 렌더링, viewport, hover 상태, mobile 상태, 텍스트 overflow, 실제 데이터 길이는
별도 도구로 확인하지 않으면 알 수 없다.

따라서 디자인 수정 요청에는 반드시 다음이 포함되어야 한다.

| 컨텍스트 | 필요 이유 |
|---|---|
| 현재 screenshot | 시각적 문제 위치 확인 |
| DOM/컴포넌트 경로 | 어떤 파일을 고칠지 특정 |
| route/page path | 다른 화면 오염 방지 |
| 현재 CSS token | 색상/간격을 기존 체계 안에서 수정 |
| viewport | desktop/mobile/tablet 차이 확인 |
| 실제 데이터 샘플 | 긴 제목, 빈 상태, 오류 상태, 많은 항목에서 깨짐 방지 |

### 3.3 이전 디자인 결정이 저장되지 않는다

AI가 첫 번째 수정에서 "운영 대시보드는 밀도 높고 차분하게"라는 결정을 했더라도,
다음 대화에서 그 결정이 자동으로 들어오지 않으면 다시 랜딩 페이지처럼 크게 꾸밀 수 있다.

이 문제는 프롬프트를 길게 쓰는 것으로 해결되지 않는다.
프로젝트마다 다음 자산이 있어야 한다.

| 자산 | 역할 |
|---|---|
| `DESIGN.md` | 브랜드, 톤, 토큰, 금지 패턴, 컴포넌트 규칙 |
| `SCREEN_CONTEXT.md` | 화면별 목적, 주요 사용자 행동, 변경 금지 영역 |
| `DESIGN_DECISIONS.md` | 왜 이렇게 만들었는지에 대한 결정 이력 |
| `QA_CHECKLIST.md` | 완료 판정 기준 |
| screenshot baseline | 이전 화면과 비교할 기준 이미지 |

---

## 4. CEO가 AI에게 쉽게 디테일하게 수정요청하는 방법

### 4.1 한 문장 요청을 수정 카드로 바꾼다

나쁜 요청:

```text
이 화면 좀 더 고급스럽게 고쳐줘.
```

좋은 요청:

```text
대상: /admin/tasks 화면의 작업 목록 테이블 상단 영역
문제: 상태 카드가 너무 커서 실제 작업 목록이 첫 화면에 적게 보입니다.
목표: 운영자가 5초 안에 대기/실행/실패 작업 수를 보고 바로 필터링할 수 있게 해주세요.
수정 범위: 상단 summary 카드, 필터 바, 테이블 헤더만 수정
변경 금지: API, 테이블 컬럼 의미, 작업 승인/거부 버튼 동작, 사이드바
디자인 방향: 장식 줄이고 밀도 높게, 카드 radius 8px 이하, 상태 색상은 기존 토큰 사용
검수 기준: 1440px 화면에서 테이블 row가 최소 8개 보여야 하고, 모바일에서 필터가 줄바꿈되어 겹치지 않아야 합니다.
```

핵심은 `대상`, `문제`, `목표`, `범위`, `금지`, `검수 기준`을 분리하는 것이다.

### 4.2 수정 카드 표준 양식

AADS는 사용자가 긴 프롬프트를 외울 필요 없이 다음 카드를 채우게 해야 한다.

| 필드 | 사용자가 쓰는 말 | AADS가 AI에게 주입하는 구조 |
|---|---|---|
| 대상 화면 | "작업현황 화면" | route, file path, component path |
| 문제 위치 | "위쪽 카드가 너무 큼" | screenshot region, DOM selector |
| 불편한 이유 | "목록이 안 보여" | UX problem statement |
| 원하는 결과 | "한눈에 보이게" | measurable acceptance criteria |
| 디자인 톤 | "깔끔하게" | density, hierarchy, color usage |
| 변경 가능 범위 | "상단만" | allowed files/components |
| 변경 금지 | "기능은 건드리지 마" | forbidden API/state/behavior |
| 확인 방식 | "모바일도 확인" | viewport QA matrix |

### 4.3 요청 유형별 추천 문법

#### A. 간격/밀도 수정

```text
대상: {화면/컴포넌트}
문제: 여백이 {너무 큼/너무 좁음} 해서 {업무상 문제}가 있습니다.
목표: {한 화면에 더 많이 보기/가독성 높이기/그룹 구분 강화}
수정 기준:
- 카드 내부 padding: {줄이기/늘리기}
- 섹션 간격: {줄이기/늘리기}
- row 높이: {유지/축소/확대}
변경 금지: 색상, API, 데이터 구조
검수: desktop {N}개 항목 표시, mobile overlap 없음
```

#### B. 색상/브랜드 수정

```text
대상: {화면/컴포넌트}
문제: 현재 색상이 {너무 튐/상태 구분이 약함/브랜드와 안 맞음}
목표: {신뢰감/운영 도구 느낌/활기/프리미엄/경고 명확성}
수정 기준:
- primary/accent/surface/danger token 안에서만 조정
- raw hex 하드코딩 금지
- 상태 색상 의미 유지
변경 금지: layout, copy, component structure
검수: contrast 기준 통과, dark/light mode 둘 다 확인
```

#### C. 타이포그래피 수정

```text
대상: {화면/컴포넌트}
문제: 제목/본문/숫자/버튼 텍스트의 위계가 약합니다.
목표: 사용자가 {가장 먼저 봐야 할 정보}를 먼저 보게 해주세요.
수정 기준:
- H1/H2/body/caption/token hierarchy 사용
- 버튼 안 텍스트 overflow 금지
- viewport 기반 font-size 스케일링 금지
변경 금지: 문구 의미, 데이터 표시 순서
검수: 가장 긴 실제 텍스트 샘플에서도 줄겹침 없음
```

#### D. 컴포넌트 교체

```text
대상: {기존 버튼/카드/탭/모달}
문제: 같은 용도의 UI가 화면마다 다르게 생깁니다.
목표: 기존 디자인 시스템 컴포넌트로 통일해주세요.
수정 기준:
- 새 컴포넌트 만들기보다 기존 Button/Card/Badge/Tabs 우선 사용
- variant와 size prop으로 표현
- 기능 동작은 유지
변경 금지: API call, state machine, permission check
검수: 기존 테스트 통과, Storybook/preview에서 동일 동작 확인
```

#### E. 반응형 수정

```text
대상: {화면/컴포넌트}
문제: {모바일/태블릿/작은 노트북}에서 {겹침/가로스크롤/버튼 잘림}이 있습니다.
목표: {주요 행동}이 화면 폭 {N}px에서도 가능해야 합니다.
수정 기준:
- 고정 width 제거 또는 minmax/grid/flex-wrap 사용
- 중요한 버튼은 접히지 않게 유지
- 보조 정보는 접거나 2줄로 허용
변경 금지: desktop 정보 구조
검수: 390px, 768px, 1440px screenshot 비교
```

#### F. "분위기" 수정

"세련되게", "고급스럽게", "앱답게", "덜 장난스럽게" 같은 표현은 반드시 구체 축으로 변환해야 한다.

| 사용자 표현 | AI에게 전달할 구조화 의미 |
|---|---|
| 세련되게 | 색상 수 축소, 여백 체계 통일, typography hierarchy 정리 |
| 고급스럽게 | 채도 낮춤, surface depth 절제, 대비/간격 정돈 |
| 운영툴답게 | 정보 밀도 상승, 장식 축소, 필터/테이블 우선 |
| 앱답게 | touch target, bottom nav, card interaction, mobile flow |
| 덜 촌스럽게 | 이모지/과한 그림자/무작위 색상 제거, token 사용 |
| 더 명확하게 | CTA hierarchy, 상태 색상, label/caption 정리 |

---

## 5. AADS 제품 개선안: Design Modification Studio

### 5.1 제품 정의

`Design Modification Studio`는 이미 만들어진 서비스 화면을 대상으로,
사용자가 자연어로 "이 부분을 이렇게 고쳐줘"라고 말하면 AADS가 자동으로
현재 화면 맥락, 기존 디자인 규칙, 변경 금지 범위, 검수 기준을 묶어 AI에게 전달하는 수정 전용 워크플로다.

목표는 다음과 같다.

| 목표 | 설명 |
|---|---|
| 비개발자 친화 | CEO/PM이 코드 경로를 몰라도 수정 요청 가능 |
| 맥락 유지 | 기존 디자인 결정과 컴포넌트 규칙을 자동 주입 |
| 세밀 수정 | 색, 간격, 폰트, 카드, 테이블, 모바일 등 단위별 요청 가능 |
| 회귀 방지 | 기능/API/데이터 변경 금지 범위를 명시 |
| 실제 검수 | 브라우저 screenshot과 기준 체크리스트로 완료 판정 |

### 5.2 전체 흐름

```text
1. 화면 선택
   - URL, route, screenshot, 현재 세션 화면 중 하나 선택

2. 문제 표시
   - screenshot 위에서 영역 선택 또는 컴포넌트 선택
   - 사용자는 "무엇이 불편한지"만 입력

3. 수정 카드 생성
   - AADS가 문제를 spacing/color/typography/component/responsive/flow로 분류
   - 필요한 추가 질문을 1~3개만 표시

4. Design Context Pack 생성
   - 기존 DESIGN.md
   - 화면 목적
   - 컴포넌트 경로
   - 관련 토큰
   - 금지 범위
   - baseline screenshot
   - 실제 데이터 샘플

5. AI 수정 실행
   - 직접 수정 또는 Runner 투입
   - 코드 변경은 허용 범위 안에서만 수행

6. 자동 검수
   - lint/build/test
   - desktop/mobile screenshot
   - visual diff
   - a11y/overflow/contrast 검사

7. CEO 승인
   - Before/After 비교
   - 변경 파일
   - 미검증 항목
   - rollback 가능성 표시

8. Design Memory 저장
   - 수정 요청
   - 적용 결정
   - screenshot
   - 품질 점수
   - 금지/선호 패턴 업데이트
```

### 5.3 화면 구조

```text
/design/modifications
  - 최근 수정 요청
  - 프로젝트별 화면 목록
  - 실패/재작업 많은 화면

/design/modifications/new
  - URL/화면 선택
  - screenshot 자동 캡처
  - 문제 영역 선택
  - 수정 카드 작성

/design/modifications/:id/context
  - AADS가 수집한 Design Context Pack 확인
  - 변경 가능 파일/금지 파일 표시
  - 누락 맥락 경고

/design/modifications/:id/workbench
  - 좌측: 수정 카드
  - 중앙: Before/After preview
  - 우측: 품질 점수, 검수 로그, 변경 파일

/design/modifications/:id/approve
  - 승인/반려/재수정
  - commit, deploy, rollback 연결
```

---

## 6. Design Context Pack 상세 설계

### 6.1 AI에게 매번 전달할 표준 패키지

```yaml
project:
  key: AADS
  frontend_stack: Next.js 16 + React 19 + Tailwind CSS v4
  repo_path: /root/aads/aads-dashboard

screen:
  route: /admin/tasks
  purpose: Pipeline Runner 작업 상태 확인과 승인/반려
  primary_users:
    - CEO
    - PM
    - Ops
  primary_actions:
    - 작업 상태 확인
    - 로그 확인
    - 승인
    - 반려

current_context:
  screenshot_baseline: design_screenshots/{id}/before.png
  viewport_matrix:
    - 390x844
    - 768x1024
    - 1440x900
  component_paths:
    - src/app/admin/tasks/page.tsx
    - src/components/...
  data_states:
    - empty
    - many_items
    - error
    - long_text

design_contract:
  source: DESIGN.md
  density: compact
  radius_max: 8px
  icon_policy: lucide icons
  color_policy: CSS variables only
  forbidden_patterns:
    - nested cards
    - decorative gradient orbs
    - emoji icons
    - viewport font scaling

modification_request:
  problem: 상단 카드가 커서 테이블이 첫 화면에 적게 보임
  goal: 1440px에서 row 8개 이상 표시
  allowed_scope:
    - summary cards
    - filter bar
    - table header
  forbidden_scope:
    - API contract
    - approval logic
    - sidebar
    - database schema

acceptance_criteria:
  visual:
    - desktop screenshot에서 row 8개 이상 표시
    - mobile screenshot에서 필터 겹침 없음
  technical:
    - npm run lint 또는 해당 파일 eslint 통과
    - build 영향 없음
  behavior:
    - 승인/반려 버튼 동작 유지
```

### 6.2 컨텍스트 수집 우선순위

| 우선순위 | 수집 항목 | AADS 구현 방식 |
|---:|---|---|
| 1 | route와 screenshot | `capture_screenshot`, Playwright |
| 2 | 컴포넌트 파일 경로 | route map, grep/AST scan |
| 3 | 디자인 토큰 | CSS 변수, Tailwind config, DESIGN.md |
| 4 | 기존 결정 | Design Memory, prompt provenance |
| 5 | 실제 데이터 상태 | API mock, DB sample, fixture |
| 6 | 검수 기준 | 수정 카드 acceptance criteria |

### 6.3 맥락 누락 경고

AADS는 AI에게 바로 수정시키기 전에 다음 경고를 보여줘야 한다.

| 누락 항목 | 경고 | 처리 |
|---|---|---|
| screenshot 없음 | 실제 렌더링을 보지 못해 레이아웃 판단이 불안정함 | 캡처 먼저 권장 |
| 컴포넌트 경로 불명확 | 잘못된 파일을 수정할 수 있음 | route scan 실행 |
| 디자인 계약 없음 | 색/폰트/간격을 임의 생성할 수 있음 | `DESIGN.md` 생성 |
| 금지 범위 없음 | 기능 코드까지 변경될 수 있음 | 사용자에게 금지 조건 질문 |
| 검수 기준 없음 | 완료 판정이 주관적임 | 기준 1~3개 자동 제안 |

---

## 7. Design Memory 설계

### 7.1 저장해야 할 기억

| 기억 유형 | 예시 | 재사용 시점 |
|---|---|---|
| 브랜드 원칙 | AADS는 운영 도구이므로 장식보다 정보 밀도 우선 | 모든 화면 수정 |
| 토큰 결정 | danger는 삭제/실패, warning은 대기/주의에만 사용 | 색상 수정 |
| 컴포넌트 결정 | 상태 표현은 Badge 컴포넌트로 통일 | UI 정리 |
| 금지 패턴 | gradient orb, nested card, emoji icon 금지 | 생성/수정 전 |
| 화면 목적 | `/admin/tasks`는 작업 승인과 장애 탐지가 핵심 | 레이아웃 수정 |
| 이전 반려 사유 | 카드가 커서 row가 적게 보인다는 이유로 반려됨 | 재수정 |

### 7.2 DB 모델 보강안

기존 `migrations/082_open_design_hub.sql`은 `design_projects`, `design_token_sets`,
`design_audit_runs`의 Phase 0 초안이다. 세밀한 수정요청 서비스를 위해 다음 테이블이 추가로 필요하다.

#### `design_screens`

| 컬럼 | 설명 |
|---|---|
| id | UUID |
| project_key | AADS, GO100, KIS, SF, NTV2 |
| route | 화면 route 또는 URL |
| name | 화면 이름 |
| purpose | 화면 목적 |
| primary_actions | 주요 사용자 행동 JSONB |
| component_paths | 관련 파일 경로 JSONB |
| metadata | viewport, auth, fixture 정보 |

#### `design_modification_requests`

| 컬럼 | 설명 |
|---|---|
| id | UUID |
| project_key | 프로젝트 |
| screen_id | 대상 화면 |
| user_prompt | 원본 사용자 요청 |
| normalized_card | 구조화된 수정 카드 JSONB |
| request_type | spacing, color, typography, component, responsive, flow |
| allowed_scope | 변경 가능 범위 JSONB |
| forbidden_scope | 변경 금지 범위 JSONB |
| acceptance_criteria | 검수 기준 JSONB |
| status | draft, ready, running, review, approved, rejected |

#### `design_context_packs`

| 컬럼 | 설명 |
|---|---|
| id | UUID |
| request_id | 수정 요청 |
| context | AI 주입용 context JSONB |
| sources | screenshot, files, DESIGN.md, DOM, API sample |
| missing_context | 누락 항목 JSONB |
| prompt_chars | 최종 프롬프트 길이 |
| created_at | 생성 시각 |

#### `design_visual_snapshots`

| 컬럼 | 설명 |
|---|---|
| id | UUID |
| request_id | 수정 요청 |
| phase | before, after, regression |
| viewport | 390x844, 768x1024, 1440x900 |
| image_url | screenshot 저장 경로 |
| dom_summary | 주요 DOM/텍스트 요약 |
| captured_at | 캡처 시각 |

#### `design_decisions`

| 컬럼 | 설명 |
|---|---|
| id | UUID |
| project_key | 프로젝트 |
| screen_id | 선택 |
| subject | 결정 주제 |
| decision | 결정 내용 |
| rationale | 이유 |
| applies_to | global, project, screen, component |
| confidence | 0~1 |
| supersedes_id | 대체된 결정 |

### 7.3 prompt provenance와의 연결

AADS에는 이미 프롬프트 레이어와 provenance 개념이 있다.
디자인 수정에서도 동일한 원칙을 적용해야 한다.

| provenance 항목 | 디자인 수정에서의 의미 |
|---|---|
| applied_assets | 어떤 DESIGN.md, screen rule, design memory가 주입됐는지 |
| system_prompt_chars | 맥락이 실제로 들어갔는지 |
| compile_error | 컨텍스트 조립 실패 여부 |
| workspace/role/intent | design modification intent 적용 여부 |

완료 보고는 "AI가 그렇게 말했다"가 아니라,
**어떤 디자인 자산이 실제 프롬프트에 들어갔고 어떤 screenshot으로 검수했는지**를 기준으로 해야 한다.

---

## 8. AI 수정요청 자동 분류 체계

### 8.1 request_type 분류

| 유형 | 사용자의 말 | AI 작업 |
|---|---|---|
| `spacing_density` | 답답하다, 너무 넓다, 한눈에 안 보인다 | padding, gap, row height, grid 조정 |
| `visual_hierarchy` | 뭐가 중요한지 모르겠다 | heading, CTA, contrast, grouping 조정 |
| `color_brand` | 촌스럽다, 튄다, 브랜드와 안 맞다 | token 기반 palette/semantic color 조정 |
| `typography` | 글자가 작다, 복잡하다, 제목이 약하다 | type scale, weight, line-height 조정 |
| `component_consistency` | 버튼/카드가 제각각이다 | 공통 컴포넌트로 교체 |
| `responsive` | 모바일에서 깨진다 | breakpoint, wrapping, minmax, overflow 수정 |
| `interaction` | 눌렀는지 모르겠다 | hover, active, focus, loading 상태 보강 |
| `content_clarity` | 문구가 애매하다 | label, helper text, empty state 개선 |
| `workflow_layout` | 순서가 불편하다 | 화면 구조, section order, action placement 조정 |

### 8.2 추가 질문 최소화

AI가 질문을 너무 많이 하면 바이브코딩 속도가 떨어진다.
AADS는 요청 유형별로 최대 1~3개만 질문해야 한다.

| 유형 | 필수 질문 |
|---|---|
| spacing_density | "더 많이 보이게"와 "더 읽기 쉽게" 중 무엇이 우선입니까? |
| color_brand | 기존 브랜드 색 유지입니까, 새 방향도 허용합니까? |
| component_consistency | 기능 동작은 그대로 두고 외형만 통일합니까? |
| responsive | 가장 중요한 viewport는 mobile, tablet, desktop 중 무엇입니까? |
| workflow_layout | 사용자가 가장 먼저 해야 하는 행동은 무엇입니까? |

---

## 9. 자동 검수 루프

### 9.1 완료 판정 기준

디자인 수정은 코드 diff만으로 완료 판정하면 안 된다.
다음 5개가 모두 확인되어야 한다.

| 검수 | 기준 | 결과 |
|---|---|---|
| 코드 검수 | 변경 파일이 허용 범위 안인지 | pass/fail |
| 빌드/정적 검수 | lint, typecheck, build 또는 대상 파일 검사 | pass/fail |
| 시각 검수 | Before/After screenshot 비교 | pass/fail |
| 반응형 검수 | mobile/tablet/desktop overlap 없음 | pass/fail |
| 기능 회귀 검수 | 변경 금지 기능/API/state 유지 | pass/fail |

### 9.2 screenshot QA matrix

| viewport | 확인 항목 |
|---|---|
| 390x844 | 버튼 잘림, 가로스크롤, 텍스트 겹침, touch target |
| 768x1024 | 2열/1열 전환, 필터 줄바꿈, 카드 균형 |
| 1440x900 | 정보 밀도, 첫 화면 row 수, CTA 위치 |
| 1920x1080 | 과도한 여백, max-width, grid stretch |

### 9.3 디자인 QA 점수

| 축 | 배점 | 자동화 |
|---|---:|---|
| 요청 일치도 | 25 | 수정 카드 acceptance criteria 대조 |
| 맥락 유지 | 20 | DESIGN.md, token, component reuse 확인 |
| 시각 완성도 | 20 | screenshot review, spacing/hierarchy 검사 |
| 반응형 안정성 | 15 | viewport screenshot |
| 접근성 | 10 | contrast, focus, aria |
| 기술 안정성 | 10 | lint/build/test |

| 점수 | 판정 | 처리 |
|---:|---|---|
| 90 이상 | 승인 후보 | CEO 확인 후 적용 |
| 80~89 | 조건부 승인 | 작은 수정 후 적용 |
| 70~79 | 재수정 | AI에게 반려 사유와 함께 재작업 |
| 70 미만 | 반려 | 컨텍스트 또는 요청 카드 재작성 |

---

## 10. AADS 구현 로드맵

### Phase 0: 문서와 수동 운영 체계

| 작업 | 산출물 | 상태 |
|---|---|---|
| 수정 카드 표준화 | 본 문서 4장 | 완료 |
| Design Context Pack 스키마 | 본 문서 6장 | 완료 |
| Design Memory 모델 | 본 문서 7장 | 완료 |
| 수동 프롬프트 템플릿 | 본 문서 12장 | 완료 |

### Phase 1: 백엔드 기반

| 작업 | 산출물 | 우선순위 |
|---|---|---|
| `design_screens` 테이블 | route/screen 목적 저장 | P1 |
| `design_modification_requests` 테이블 | 수정 카드 저장 | P1 |
| `design_context_packs` 테이블 | AI 주입 맥락 저장 | P1 |
| `design_visual_snapshots` 테이블 | before/after screenshot 저장 | P1 |
| context pack builder | screenshot/file/token/decision 조립 | P1 |
| request classifier | 자연어 -> request_type 분류 | P1 |

### Phase 2: 대시보드 UI

| 화면 | 기능 | 우선순위 |
|---|---|---|
| `/design/modifications/new` | 화면 선택, screenshot, 수정 카드 | P1 |
| `/design/modifications/:id/context` | 수집 맥락 확인, 누락 경고 | P1 |
| `/design/modifications/:id/workbench` | Before/After, QA 점수, 변경 파일 | P1 |
| `/design/modifications/:id/approve` | 승인/반려/재수정 | P2 |

### Phase 3: 자동 검수

| 작업 | 기능 | 우선순위 |
|---|---|---|
| screenshot capture matrix | 390/768/1440 viewport 캡처 | P1 |
| visual overlap detector | 텍스트/버튼 겹침 탐지 | P2 |
| token compliance checker | raw hex, arbitrary color 차단 | P1 |
| component reuse checker | 기존 UI 컴포넌트 사용률 | P2 |
| design score service | QA 점수 계산 | P2 |

### Phase 4: Runner/Agent 연결

현재 CEO 지시에 따라 추가 Pipeline Runner 투입은 중단 상태로 본다.
따라서 본 문서는 실행 제출이 아니라 작업 단위 설계까지만 포함한다.

러너를 다시 사용할 수 있을 때 권장 작업 단위는 다음과 같다.

| 작업 | 크기 | 설명 |
|---|---|---|
| AADS-DESIGN-MOD-001 | M | 디자인 수정 DB 스키마와 read-only API |
| AADS-DESIGN-MOD-002 | M | 수정 카드 UI와 request classifier |
| AADS-DESIGN-MOD-003 | L | context pack builder와 screenshot 수집 |
| AADS-DESIGN-MOD-004 | M | workbench Before/After UI |
| AADS-DESIGN-MOD-005 | M | QA score와 token compliance |

---

## 11. 지시서 초안

아래는 러너 재개 후 사용할 수 있는 작업 지시서 초안이다.
현재 문서 작성 단계에서는 제출하지 않는다.

### AADS-DESIGN-MOD-001

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DESIGN-MOD-001
TITLE: Design Modification Studio DB Schema and Read API
PRIORITY: P1-HIGH
SIZE: M
DESCRIPTION:
AADS Design Modification Studio의 기반 스키마를 추가한다.
대상은 design_screens, design_modification_requests, design_context_packs,
design_visual_snapshots, design_decisions 테이블이다.
기존 migrations/082_open_design_hub.sql의 design_projects/design_token_sets/design_audit_runs와
충돌하지 않게 별도 migration으로 작성한다.
Read-only API로 프로젝트별 화면 목록, 요청 목록, 요청 상세, context pack preview를 제공한다.
DB destructive operation 금지, 기존 design_audit_service 동작 변경 금지.
테스트는 migration SQL 구조 검증과 API schema 단위 테스트를 포함한다.
>>>DIRECTIVE_END
```

### AADS-DESIGN-MOD-002

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DESIGN-MOD-002
TITLE: Design Modification Request Card UI
PRIORITY: P1-HIGH
SIZE: M
DESCRIPTION:
AADS dashboard에 /design/modifications/new 화면을 추가한다.
사용자가 프로젝트, 대상 route, 문제 유형, 문제 설명, 목표, 변경 가능 범위, 변경 금지 범위,
검수 기준을 카드 형태로 입력할 수 있게 한다.
초기 UI는 실제 코드 적용 없이 request draft 저장까지만 수행한다.
기존 대시보드 디자인 원칙을 따르고 nested card, emoji icon, 장식 gradient 사용을 금지한다.
모바일 390px과 desktop 1440px에서 입력 필드와 버튼이 겹치지 않아야 한다.
>>>DIRECTIVE_END
```

### AADS-DESIGN-MOD-003

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DESIGN-MOD-003
TITLE: Design Context Pack Builder
PRIORITY: P1-HIGH
SIZE: L
DESCRIPTION:
수정 요청 draft를 기준으로 AI에게 전달할 Design Context Pack을 생성한다.
포함 항목은 project metadata, screen route, component path candidates, DESIGN.md/design token,
baseline screenshot URL, viewport matrix, allowed_scope, forbidden_scope, acceptance_criteria이다.
컨텍스트 누락 시 missing_context에 저장하고 사용자에게 경고할 수 있게 한다.
실제 코드 수정이나 Runner 제출은 하지 않는다.
시크릿, .env, 인증 토큰은 context pack에 포함하지 않는다.
>>>DIRECTIVE_END
```

### AADS-DESIGN-MOD-004

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DESIGN-MOD-004
TITLE: Design Modification Workbench Before After Review
PRIORITY: P2-MEDIUM
SIZE: M
DESCRIPTION:
/design/modifications/:id/workbench 화면을 추가한다.
좌측에는 수정 카드, 중앙에는 before/after screenshot, 우측에는 QA 상태와 변경 파일 목록을 표시한다.
초기 버전은 mock after 또는 저장된 snapshot을 표시하고, 실제 patch runner 연결은 후속 작업으로 둔다.
정보 밀도 높은 운영 UI로 구성하고 과한 hero/marketing layout을 사용하지 않는다.
>>>DIRECTIVE_END
```

### AADS-DESIGN-MOD-005

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DESIGN-MOD-005
TITLE: Design QA Score and Token Compliance
PRIORITY: P2-MEDIUM
SIZE: M
DESCRIPTION:
design_audit_service.py의 read-only scanner를 확장해 수정 결과의 token compliance, raw color,
emoji icon, repeated button pattern, responsive risk 후보를 점수화한다.
QA 점수는 요청 일치도, 맥락 유지, 시각 완성도, 반응형 안정성, 접근성, 기술 안정성 축으로 저장한다.
초기 구현은 정적 코드 검사와 screenshot metadata 기반으로 제한하고, destructive command와 자동 배포는 금지한다.
>>>DIRECTIVE_END
```

---

## 12. 바로 사용할 수 있는 수동 프롬프트 템플릿

현재 AADS 제품 기능이 완성되기 전까지 CEO가 바로 사용할 수 있는 템플릿이다.

```text
역할:
너는 기존 서비스의 디자인을 세밀하게 수정하는 프론트엔드 엔지니어다.
새 디자인을 처음부터 다시 만들지 말고, 기존 화면의 목적과 기능을 유지하면서 지정된 범위만 고친다.

대상:
- 프로젝트:
- 화면/URL:
- 관련 파일을 먼저 찾아라:

현재 문제:
- 사용자가 불편한 점:
- 시각적으로 어색한 부분:
- 업무상 손해:

목표:
- 수정 후 사용자가 더 잘해야 하는 행동:
- 첫 화면에서 보여야 하는 정보:
- 디자인 톤:

수정 범위:
- 변경 가능:
- 변경 금지:

디자인 규칙:
- 기존 색상 token/CSS variable 우선 사용
- raw hex 하드코딩 금지
- 카드 radius 8px 이하
- nested card 금지
- 이모지 아이콘 대신 lucide icon 사용
- font-size를 viewport width로 스케일링하지 말 것
- 모바일/데스크톱에서 텍스트 겹침 금지

검수 기준:
- desktop 1440px:
- mobile 390px:
- 기능 회귀 금지:
- 테스트/빌드:

작업 방식:
1. 관련 파일과 현재 구조를 먼저 읽어라.
2. 변경 계획을 짧게 세워라.
3. 지정 범위 안에서만 수정하라.
4. 실제 screenshot 또는 테스트로 검수하라.
5. 변경 파일, 검증 결과, 미검증 항목을 보고하라.
```

---

## 13. 운영 원칙

### 13.1 사용자가 해야 할 일

사용자는 전문 디자인 용어를 몰라도 된다.
다만 다음 네 가지는 반드시 말해야 한다.

1. 어디를 고칠지
2. 무엇이 불편한지
3. 어떻게 되면 성공인지
4. 어디는 건드리면 안 되는지

### 13.2 AADS가 대신해야 할 일

AADS는 사용자가 말하지 않아도 다음을 자동으로 붙여야 한다.

| 자동 보강 | 설명 |
|---|---|
| 화면 캡처 | 현재 상태를 AI가 보게 함 |
| 관련 파일 | route와 컴포넌트 경로 탐색 |
| 디자인 규칙 | DESIGN.md, token, 금지 패턴 |
| 이전 결정 | Design Memory와 반려 이력 |
| 실제 데이터 | 빈 상태, 긴 텍스트, 많은 항목 |
| 검수 행렬 | desktop/mobile screenshot 기준 |

### 13.3 AI에게 금지해야 할 일

| 금지 | 이유 |
|---|---|
| 전체 화면 재작성 | 기존 기능 회귀 위험 |
| 새 토큰 임의 생성 | 디자인 시스템 분열 |
| raw hex 남발 | 테마/브랜드 관리 불가 |
| 기능/API 변경 | 디자인 수정 범위 이탈 |
| screenshot 없이 완료 선언 | 실제 UI 깨짐 미탐지 |
| 미검증 성능/품질 수치 보고 | CEO 판단 오류 |

---

## 14. 기존 AADS 자산과의 연결

| 기존 자산 | 현재 의미 | 개선 연결 |
|---|---|---|
| `AADS-SMART-DESIGN-SYSTEM.md` | AADS 대시보드 내부 디자인 시스템 계획 | DESIGN.md/token/component 기반으로 사용 |
| `AADS-OPEN-DESIGN-HUB.md` | 전 프로젝트 디자인 운영 허브 | Design Modification Studio의 상위 관리 화면 |
| `migrations/082_open_design_hub.sql` | Phase 0 디자인 허브 스키마 초안 | 수정요청/스크린/스냅샷 테이블 추가 필요 |
| `design_audit_service.py` | read-only 디자인 코드 스캐너 | QA score와 token compliance로 확장 |
| `visual_qa_test`, `capture_screenshot` | 화면 검수 도구 | Before/After 자동 검수에 연결 |
| Pipeline Runner approval | 코드 반영 승인 루프 | 디자인 수정 승인/반려에 연결 |
| Prompt provenance | 프롬프트 적용 근거 | Design Context Pack 적용 근거로 재사용 |

---

## 15. 우선순위 제안

가장 먼저 만들 것은 거대한 디자인 생성기가 아니다.
CEO가 현재 겪는 문제를 직접 줄이는 순서가 맞다.

| 순위 | 작업 | 이유 |
|---:|---|---|
| 1 | 수정 카드 표준과 수동 템플릿 적용 | 즉시 효과, 비용 낮음 |
| 2 | 화면별 Design Context Pack 저장 | AI 맥락 손실 방지 |
| 3 | Before/After screenshot 검수 | 완료 착각 방지 |
| 4 | Design Memory와 결정 이력 | 반복 수정 품질 상승 |
| 5 | Figma/Storybook/MCP 고도화 | 외부 연동은 기반 이후 |

---

## 16. 최종 권고

AADS의 AI 바이브코딩 디자인 수정 문제는 "더 좋은 모델을 쓰면 해결"되는 문제가 아니다.
좋은 모델을 쓰더라도 맥락, 범위, 금지 조건, 검수 기준이 없으면 같은 문제가 반복된다.

따라서 AADS는 다음 원칙으로 가야 한다.

1. 자연어 요청을 수정 카드로 구조화한다.
2. 화면별 현재 상태를 screenshot과 컴포넌트 경로로 고정한다.
3. `DESIGN.md`와 Design Memory를 AI 프롬프트에 자동 주입한다.
4. 변경 금지 범위를 명시해 기능 회귀를 막는다.
5. 완료 판정은 AI의 말이 아니라 Before/After screenshot과 테스트 결과로 한다.

이렇게 만들면 CEO는 "이 카드 너무 커서 목록이 안 보여. 상단만 더 촘촘하게 고쳐줘"처럼 말해도,
AADS가 자동으로 상세 수정요청으로 변환하고, AI는 기존 디자인 맥락을 유지한 상태에서 안전하게 수정할 수 있다.

---

## 17. 이번 문서의 적용 상태

| 항목 | 상태 | 비고 |
|---|---|---|
| 기존 보고서 검토 | 완료 | 조사 보고서, 사용자 여정 문서, 디자인 시스템 문서 확인 |
| 상세문서 작성 | 완료 | 본 문서 |
| 코드 변경 | 없음 | 문서 작성만 수행 |
| 러너 투입 | 미수행 | CEO의 러너 추가 중단 지시 준수 |
| 비용 | $0 | 외부 유료 호출 없음 |

