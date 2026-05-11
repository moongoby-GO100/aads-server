# AADS Vibe Design Service: User Journey Architecture and Design Quality Plan

작성시각: 2026-05-11 10:30 KST

## 1. 결론

AADS 바이브코딩 디자인 서비스는 단순한 "프롬프트로 화면 생성" 기능이 아니라,
사용자 입력을 `DESIGN.md`, 디자인 소스, 컴포넌트 레지스트리, 시각 QA, 승인된 코드 반영으로 연결하는
운영형 디자인 제작 파이프라인으로 구현해야 한다.

핵심 차별점은 세 가지다.

1. 최신 디자인 참고는 `DESIGN.md`와 디자인 소스 커넥터로 구조화한다.
2. 검증된 구현은 Figma MCP, Code Connect, Storybook MCP, shadcn registry를 통해 재사용한다.
3. 디자인 퀄리티는 멀티모달 평가, Storybook 테스트, 접근성, 브랜드 일관성, 실제 코드 diff 검수로 닫는다.

## 2. 사용자 여정 흐름 아키텍처

### 2.1 대상 사용자별 주요 여정

| 사용자 | 시작 입력 | 핵심 니즈 | AADS 제공 결과 |
|---|---|---|---|
| 비개발 창업자 | 자연어, 참고 URL, 스크린샷 | 빠른 MVP 화면 | preview URL, React 코드, 디자인 설명서 |
| 디자이너 | Figma 링크, 브랜드 가이드 | 디자인 의도 유지 | DESIGN.md, Figma frame update, component mapping |
| 프론트엔드 개발자 | 기존 repo, Storybook, shadcn registry | 기존 컴포넌트 재사용 | production-ready PR, stories, visual QA |
| PM/운영자 | 기능 요구사항, 경쟁 서비스 링크 | 플로우 설계와 화면 비교 | user flow, wireframe, UI variant report |
| 엔터프라이즈 고객 | 사내 디자인 시스템, 보안 제약 | 권한/감사/재현성 | audit log, source provenance, approval workflow |

### 2.2 전체 여정

```text
사용자 요청
  -> Intake
  -> Design Brief Normalization
  -> Source Context Collection
  -> DESIGN.md / Skill Selection
  -> UX Flow Planning
  -> Visual Direction Generation
  -> Component-grounded UI Generation
  -> Sandbox Preview
  -> Design Quality Evaluation
  -> Human Review / Revision
  -> Code Apply via Pipeline Runner
  -> Design Memory / Provenance 저장
```

### 2.3 단계별 설계

| 단계 | 사용자 화면 | 내부 처리 | 산출물 | 검증 |
|---|---|---|---|---|
| 1. Intake | 프로젝트 생성, 목표/톤/금지 스타일 입력 | intent=`design_intake` | design brief | 필수 필드 완성도 |
| 2. Source 연결 | URL, 이미지, Figma, repo, Storybook 연결 | source crawler, screenshot, Figma MCP, repo scan | source bundle | 출처/권한 확인 |
| 3. 디자인 계약 | 스타일 선택/수정 | DESIGN.md 생성 또는 선택 | `DESIGN.md` | 색상/타이포/접근성 룰 |
| 4. UX 흐름 | 화면 목록과 사용자 journey 확인 | IA, task flow, route map 생성 | journey map, sitemap | 누락 화면 체크 |
| 5. 화면 생성 | variant 2~3개 미리보기 | agent + registry + component context | React/Tailwind preview | 빌드, 렌더링 |
| 6. 품질 평가 | 점수/이슈 표시 | visual QA, a11y, token check, screenshot diff | quality report | 기준 미달 시 재생성 |
| 7. 수정 루프 | 사용자가 코멘트 | critique -> patch -> preview | revised preview | regression check |
| 8. 적용 | 승인 버튼 | Pipeline Runner 코드 반영 | commit/PR | lint/build/test |
| 9. 운영 기억 | 버전/결정 기록 | prompt provenance, design memory | reusable design asset | 재사용 가능성 |

### 2.4 AADS 내부 모듈 제안

| 모듈 | 책임 | 우선순위 |
|---|---|---|
| `design_projects` | 고객 프로젝트와 디자인 상태 관리 | P0 |
| `design_sources` | URL/Figma/repo/screenshot 출처 저장 | P0 |
| `design_briefs` | 목표, 사용자, 톤, 제약 정규화 | P0 |
| `design_contracts` | `DESIGN.md`, tokens, brand rules 저장 | P0 |
| `design_generations` | 생성 화면, prompt, model, preview 저장 | P0 |
| `design_quality_reports` | visual/a11y/brand/code 품질 점수 | P0 |
| `design_registry_items` | AADS/shadcn/custom blocks 관리 | P1 |
| `design_approvals` | 사용자 승인, Runner 반영, rollback | P1 |

### 2.5 서비스 화면 구조

```text
/design
  - 프로젝트 목록, 최근 preview, 품질 점수

/design/new
  - 자연어 brief
  - 참고 URL/스크린샷/Figma/repo 업로드
  - 업종/톤/플랫폼 선택

/design/:id/brief
  - 정규화된 요구사항
  - 사용자 여정, 화면 목록, 금지 스타일

/design/:id/sources
  - URL/Figma/Storybook/repo 연결 상태
  - 권한, 출처, 사용 가능 컴포넌트

/design/:id/studio
  - 좌측: 요구사항/디자인 계약
  - 중앙: sandbox preview
  - 우측: 품질 점수, 이슈, agent log

/design/:id/review
  - variant 비교
  - screenshot diff
  - a11y/brand/component score

/design/:id/apply
  - 코드 diff
  - Runner 제출/승인/rollback
```

## 3. 최신 디자인 참고와 검증된 시스템 조사

### 3.1 DESIGN.md: AI가 읽는 디자인 계약

Google Labs는 Stitch의 `DESIGN.md` draft specification을 공개했다.
공식 설명 기준으로, 목적은 AI 에이전트가 색상 의도를 추측하지 않고 WCAG 접근성 규칙까지 검증하게 하는 것이다.

AADS 적용:

| 항목 | 적용 방식 |
|---|---|
| Color semantics | primary/surface/danger 같은 의미 기반 토큰 저장 |
| Typography | 화면별 위계, line-height, density 기준 저장 |
| Layout | spacing scale, breakpoints, grid, responsive rule 저장 |
| Components | Button/Card/Dialog/Form/Table variant 저장 |
| Accessibility | contrast, focus, keyboard, aria 기준 저장 |
| Anti-patterns | 금지 palette, 둥근 카드 남발, hero misuse 등 저장 |

출처: Google Labs DESIGN.md announcement, 2026-05 확인
https://blog.google/innovation-and-ai/models-and-research/google-labs/stitch-design-md/

### 3.2 Figma MCP + Code Connect: 디자인-코드 왕복

Figma 공식 MCP 서버는 AI 에이전트가 Figma 디자인 파일에서 변수, 컴포넌트, 레이아웃 정보를 가져오고,
native Figma content를 canvas에 다시 쓸 수 있게 한다. Code Connect를 붙이면 Figma 컴포넌트가 실제 코드 컴포넌트와 연결되어,
에이전트가 임의 버튼을 만들지 않고 팀의 실제 `Button`, `Card`, `Input`을 사용하게 된다.

AADS 적용:

| 기능 | AADS 의미 |
|---|---|
| Extract design context | Figma frame을 design source로 수집 |
| Write to canvas | AADS가 생성한 방향안을 Figma native layer로 전달 |
| Code Connect snippet | 실제 코드 컴포넌트 import/props/usage를 agent context로 주입 |
| Custom MCP instructions | 접근성, variant, 팀 규칙을 컴포넌트별로 강제 |

주의:
공식 Figma MCP를 우선 사용하고, third-party MCP는 보안 검토 후 허용해야 한다.
GitHub Advisory 기준 `figma-developer-mcp <=0.6.2`에는 command injection/RCE 취약점이 보고되었다.

출처:
- Figma MCP Server Developer Docs, 2026-05 확인
  https://developers.figma.com/docs/figma-mcp-server/
- Figma Code Connect integration, 2026-05 확인
  https://developers.figma.com/docs/figma-mcp-server/code-connect-integration/
- GitHub Advisory GHSA-gxw4-4fc5-9gr5, 2025-09-30
  https://github.com/advisories/GHSA-gxw4-4fc5-9gr5

### 3.3 Storybook MCP: 구현 컴포넌트를 품질 기준으로 사용

Storybook MCP는 agent가 Storybook 문서, props, stories, interaction test, accessibility test를 조회하고 실행할 수 있게 한다.
공식 문서 기준 toolset은 development, docs, testing으로 나뉘며, 테스트 실패 시 agent가 수정 후 재실행하는 self-healing loop를 만들 수 있다.

AADS 적용:

| Storybook MCP toolset | AADS 품질 효과 |
|---|---|
| Development | 생성 UI를 story로 만들고 preview |
| Docs | 기존 컴포넌트 props와 사용법 재사용 |
| Testing | interaction/a11y test 실행 후 자동 수정 |
| Composition | 여러 프로젝트 Storybook을 공용 디자인 자산으로 통합 |

출처: Storybook MCP Server Docs, 2026-05 확인
https://storybook.js.org/docs/ai/mcp/overview

### 3.4 shadcn registry MCP: AI가 설치 가능한 디자인 시스템

shadcn registry MCP는 agent가 registry item을 검색하고 설치할 수 있게 하는 구조다.
공식 문서 기준 registry는 `registry.json` 또는 `registry` index를 제공해야 하고,
각 item은 설명, dependency, registry dependency, consistent naming이 중요하다.

AADS 적용:

| 적용 항목 | 내용 |
|---|---|
| AADS registry | 챗, 러너, 매니저, 운영 대시보드 block 제공 |
| 고객 registry | 고객별 컴포넌트/토큰/블록을 registry로 저장 |
| MCP search | intent=`design_generate`일 때 관련 block만 동적 로딩 |
| install command | agent가 검증된 컴포넌트만 설치/수정 |

출처: shadcn/ui MCP Server Docs, 2026-05 확인
https://ui.shadcn.com/docs/registry/mcp

### 3.5 오픈소스 바이브코딩 도구에서 가져올 패턴

| 도구 | 검증된 패턴 | AADS 적용 | 주의 |
|---|---|---|---|
| Dyad | local-first, BYOK, no lock-in | 고객 코드/키를 외부 SaaS에 덜 노출 | pro 영역 license 구분 |
| bolt.diy | WebContainer 기반 preview, prompt-run-edit-deploy | sandbox preview와 터미널 UX 참고 | 상업 사용 시 WebContainers license 확인 |
| Open Lovable | URL -> React app 재생성 | 참고 URL/경쟁사 분석 -> UI skeleton | clone 품질/저작권/브랜드 침해 검토 |
| Claude Code/OpenClaw 분석 | permission, compaction, MCP/skills/plugins/hooks, subagent | AADS agent runtime/approval 설계 참고 | 권한 모드와 prompt injection 방어 필수 |

출처:
- Dyad GitHub, 2026-05 확인
  https://github.com/dyad-sh/dyad
- bolt.diy GitHub, 2026-05 확인
  https://github.com/stackblitz-labs/bolt.diy
- Open Lovable GitHub, 2026-05 확인
  https://github.com/firecrawl/open-lovable
- arXiv 2604.14228, 2026-04
  https://arxiv.org/abs/2604.14228

## 4. 디자인 퀄리티 적용 방안

### 4.1 품질 점수 모델

AADS는 디자인 결과를 주관적 평가가 아니라 100점 기준의 다축 점수로 관리해야 한다.

| 축 | 배점 | 평가 방법 | 자동화 가능성 |
|---|---:|---|---|
| Brand fit | 20 | DESIGN.md 토큰/톤/금지스타일 일치 | 높음 |
| UX flow | 20 | task completion, route completeness, CTA hierarchy | 중간 |
| Visual craft | 20 | spacing, typography, density, hierarchy, polish | 중간 |
| Component reuse | 15 | Storybook/shadcn/Code Connect 사용률 | 높음 |
| Accessibility | 15 | WCAG contrast, keyboard, aria, focus | 높음 |
| Technical readiness | 10 | lint/build/test, responsive, no overlap | 높음 |

권장 기준:

| 점수 | 판정 | 처리 |
|---:|---|---|
| 90점 이상 | production candidate | 승인 가능 |
| 80~89점 | minor revision | 사용자 확인 후 적용 |
| 70~79점 | needs revision | agent 재작업 |
| 70점 미만 | reject | brief/source/design contract 재검토 |

### 4.2 최신 디자인 적용 원칙

| 원칙 | 구체 기준 | AADS 구현 |
|---|---|---|
| Context-first | 업종/사용자/업무 밀도에 맞춘 UI | intake에서 domain preset 선택 |
| Token-first | 색/간격/폰트는 의미 토큰으로 | DESIGN.md + CSS variables |
| Component-grounded | 검증된 컴포넌트 우선 | Figma Code Connect + Storybook MCP |
| Preview-first | 생성 즉시 실제 렌더링 확인 | sandbox iframe + screenshot |
| Accessibility-by-default | contrast/focus/keyboard 자동 검사 | axe/Storybook test |
| Multi-variant | 한 번에 2~3개 방향 비교 | design_generations variant |
| Diff-aware | 변경 전후 screenshot/code diff | visual regression |
| Human-in-loop | 적용 전 승인과 rollback | AADS Runner approval |

### 4.3 최신 디자인 참고 수집 방법

| 소스 | 수집 대상 | 사용 방식 |
|---|---|---|
| Google Stitch/DESIGN.md | design contract 구조 | AADS `DESIGN.md` generator |
| Figma Community/API | 업계별 components, variables | token/component context |
| Storybook public/private | 실제 사용 가능한 컴포넌트 | docs/test source of truth |
| shadcn registry | 검증된 primitives/blocks | registry 기반 설치 |
| 경쟁 서비스 URL | 실제 시장 UI 패턴 | screenshot -> style extraction |
| GitHub OSS builders | preview/runtime/agent UX | sandbox/agent loop 참고 |

### 4.4 시각 QA 체크리스트

| 검사 | 기준 |
|---|---|
| Responsive | 375px, 768px, 1440px에서 overflow/overlap 없음 |
| Typography | 버튼/카드/패널 내부 텍스트가 부모 안에 안정적으로 들어감 |
| Layout stability | hover/loading/error 상태에서 크기 튐 없음 |
| Contrast | WCAG AA 기준 통과 |
| Focus | 키보드 이동과 focus ring 확인 |
| Component conformance | registry/Storybook에 없는 임의 UI 최소화 |
| Visual originality | 참고 URL 복제 금지, 방향성만 추출 |
| Production readiness | lint/build/test 통과 |

## 5. AADS 구현 로드맵

### P0: 내부 MVP

1. `DESIGN.md` generator 추가
2. `design_*` intent 4종 추가: intake/generate/critique/apply
3. 디자인 preview iframe과 screenshot capture 저장
4. visual QA 최소 기준: responsive, overlap, contrast, build
5. Runner apply는 기존 승인 흐름 재사용

### P1: 검증된 시스템 연결

1. Storybook MCP connector
2. shadcn-compatible AADS registry
3. Figma MCP read connector
4. Code Connect mapping importer
5. design quality report DB 저장

### P2: 고급 디자인 운영

1. Figma canvas write-back
2. multi-variant 자동 평가
3. 업종별 DESIGN.md template marketplace
4. 고객별 design memory/provenance
5. 팀 협업 review/comment workflow

## 6. 리스크와 통제

| 리스크 | 영향 | 통제 |
|---|---|---|
| AI가 비슷한 UI만 반복 | 서비스 차별화 약화 | DESIGN.md + source context + critique loop |
| Figma/MCP 보안 문제 | RCE, data leak | 공식 MCP 우선, vault, allowlist, sandbox |
| 저작권/브랜드 침해 | 법적 리스크 | 참고 URL은 style extraction만, clone 금지 옵션 |
| 디자인-코드 불일치 | 개발 재작업 | Code Connect + Storybook MCP |
| 과도한 자동 적용 | 운영 장애 | Runner approval과 rollback 필수 |
| 품질 점수 주관성 | 고객 신뢰 저하 | measurable test와 screenshot diff 저장 |

## 7. 권장 결정

권장안은 P0부터 바로 구현하는 것이다.

첫 버전은 Figma write-back까지 욕심내지 말고,
`DESIGN.md -> preview -> quality report -> Runner apply`를 AADS 내부 기능으로 완성해야 한다.
그 다음 Storybook MCP와 shadcn registry를 붙이면, 단순한 화면 생성기가 아니라
검증 가능한 디자인 운영 서비스가 된다.
