# AADS 바이브코딩 디자인 서비스 프로세스 자료조사 보고서

**작성일**: 2026-05-11 09:47 KST
**작성자**: AADS CTO AI
**상태**: 자료조사 완료 / 구현 기획 초안
**대상**: AADS를 바이브코딩 서비스로 확장할 때의 디자인 서비스 프로세스

---

## 1. 결론

AADS 디자인 서비스는 `DESIGN.md` + `SKILL.md` + MCP + 샌드박스 미리보기 + 시각 QA를 결합한
에이전트형 디자인 스튜디오로 구현하는 것이 적합하다.

핵심 차별점은 "프롬프트로 예쁜 화면을 한번 뽑는 기능"이 아니라,
고객의 의도와 브랜드를 디자인 지식 파일로 고정하고, 에이전트가 반복 생성/검수/수정/코드 반영까지 수행하는
서비스 프로세스다.

---

## 2. 조사 범위와 출처 신뢰도

| 구분 | 확인한 자료 | 신뢰도 | AADS 적용 의미 |
|---|---|---:|---|
| Google Stitch / DESIGN.md | Google 공식 블로그, VoltAgent GitHub | 높음 | 디자인 규칙을 에이전트가 읽는 표준 파일로 저장 |
| Figma MCP / Code Connect | Figma 공식 MCP 가이드, Code Connect 문서 | 높음 | Figma와 코드 컴포넌트 매핑을 AI에 제공 |
| Storybook MCP | Storybook 공식 문서 | 높음 | 실제 구현 컴포넌트와 테스트를 AI가 재사용 |
| shadcn/v0 Registry | shadcn, v0 공식 문서 | 높음 | 컴포넌트/토큰/블록을 AI가 설치 가능한 registry로 배포 |
| Open Design | nexu-io/open-design GitHub | 중간~높음 | Claude Design 유사 프로세스의 오픈소스 구현 참고 |
| Dyad / bolt.diy | dyad-sh/dyad, stackblitz-labs/bolt.diy GitHub | 높음 | 바이브코딩 앱 빌더의 로컬/BYOK/멀티모델 구조 참고 |
| Huashu / Guizang / Design Skills | GitHub 및 보조 인덱스 | 중간 | 디자인 품질을 skill/checklist로 강제하는 패턴 참고 |
| OpenClaw 계열 | 검색 결과 다수, 공식/비공식 혼재 | 낮음~중간 | 로컬 에이전트 자동화 흐름은 참고하되 보안 검증 필요 |

---

## 3. 최신 동향 요약

### 3-1. DESIGN.md: 디자인 규칙을 에이전트가 읽는 파일로 고정

Google은 Stitch에서 `DESIGN.md` 초안 스펙을 공개했다. 공식 설명에 따르면
Stitch는 디자인 규칙을 프로젝트 간 export/import할 수 있고, AI 에이전트가 색상의 용도를 추측하지 않고
WCAG 접근성 규칙까지 검증할 수 있게 한다.

**출처**
- Google 공식: https://blog.google/innovation-and-ai/models-and-research/google-labs/stitch-design-md/
- Google Stitch 소개: https://blog.google/innovation-and-ai/models-and-research/google-labs/stitch-ai-ui-design/
- VoltAgent awesome-design-md: https://github.com/VoltAgent/awesome-design-md

**핵심 구조**

| DESIGN.md 섹션 | 내용 | AADS 적용 |
|---|---|---|
| Visual Theme & Atmosphere | 분위기, 밀도, 디자인 철학 | 서비스 intake에서 고객 톤을 구조화 |
| Color Palette & Roles | 색상, 의미, 사용처 | CSS 변수/토큰 자동 생성 |
| Typography Rules | 폰트, hierarchy | 대시보드/랜딩/모바일별 typography preset |
| Component Stylings | 버튼, 카드, 입력, nav 상태 | shadcn/Radix 컴포넌트 variant로 변환 |
| Layout Principles | spacing, grid, whitespace | 반응형 레이아웃 제약으로 사용 |
| Depth & Elevation | shadow, surface hierarchy | 다크/라이트 theme consistency |
| Do's and Don'ts | 금지 패턴, guardrail | AI slop 방지 checklist |
| Responsive Behavior | breakpoint, touch target | Playwright mobile QA 항목 |
| Agent Prompt Guide | 바로 쓰는 agent 지시문 | AADS PromptCompiler asset으로 저장 |

**판단**
- AADS는 `DESIGN.md`를 단순 첨부문서가 아니라 DB 엔티티와 prompt asset으로 저장해야 한다.
- 프로젝트별 `AGENTS.md`가 "어떻게 만들지"를 정의한다면, `DESIGN.md`는 "어떻게 보여야 하는지"를 정의한다.

### 3-2. Open Design: 오픈소스 Claude Design 대안의 구조

`nexu-io/open-design`은 로컬 우선, BYOK, 다중 CLI 에이전트 자동 감지, 디자인 skill과 design system을
결합한 Claude Design 대안이다. README 기준으로 Claude Code, Codex, Cursor Agent, Gemini CLI,
OpenCode 등 여러 CLI를 감지하고, 31개 skill과 다수의 design system을 사용한다.

**출처**
- GitHub: https://github.com/nexu-io/open-design
- 소개 글: https://firethering.com/open-design-claude-design-alternative/

**주요 프로세스**

| 단계 | Open Design 패턴 | AADS 반영안 |
|---|---|---|
| Brief 수집 | 생성 전 질문 폼으로 목적/톤/대상 확정 | AADS 디자인 intake schema |
| 방향 선택 | 5개 visual direction 제안 | 3~5개 시안 자동 생성 |
| 실행 계획 | TodoWrite plan streaming | AADS SSE task plan 카드 |
| 생성 | agent가 로컬 프로젝트 폴더에 artifact 작성 | design workspace sandbox |
| 미리보기 | sandboxed iframe artifact preview | 기존 ArtifactHtmlPreview 확장 |
| 검수 | 5차원 self-critique | AADS 디자인 QA rubric |
| 내보내기 | HTML/PDF/PPTX/ZIP/Markdown | React code, DESIGN.md, report, ZIP |

**핵심 배울 점**
- "디자인 엔진"을 직접 하나로 고정하지 않고, 이미 설치된 에이전트를 local daemon이 중계한다.
- 산출물은 prose가 아니라 `<artifact>`와 실제 파일이어야 한다.
- 생성 전에 질문 폼을 강제하는 것이 품질과 재작업 비용을 크게 줄인다.

### 3-3. Figma MCP + Code Connect: 디자인 소스와 코드 소스를 연결

Figma MCP는 AI 에이전트가 Figma 파일의 변수, 컴포넌트, 레이아웃 정보를 IDE/agent workflow에서
읽을 수 있게 한다. Code Connect는 Figma 컴포넌트를 실제 코드 컴포넌트와 연결해, AI가 임의 구현 대신
프로덕션 컴포넌트를 재사용하도록 돕는다.

2026년 기준 Figma MCP는 단순 읽기용 design context 제공을 넘어, 에이전트가 Figma canvas에 native
Figma content를 다시 작성하는 방향으로 확장되고 있다. OpenAI와 Figma의 Codex 연동 발표도 같은 흐름이다.
즉 AADS는 "Figma를 참고해 코드 생성"에 머물지 말고, 고객 코드/컴포넌트/토큰을 Figma canvas와 왕복시키는
code-to-design, design-to-code loop를 서비스 목표로 잡아야 한다.

**출처**
- Figma MCP guide: https://github.com/mcp/com.figma.mcp/mcp
- Figma Code Connect: https://developers.figma.com/docs/code-connect/
- Figma MCP Developer Docs: https://developers.figma.com/docs/figma-mcp-server/
- OpenAI + Figma Codex partnership: https://openai.com/index/figma-partnership/

**AADS 적용 포인트**

| 기능 | 의미 | 구현 우선순위 |
|---|---|---:|
| `get_design_context` | 선택 frame에서 구조/토큰 추출 | P1 |
| Design system search | 기존 Figma library 컴포넌트 검색 | P2 |
| Code Connect | Figma 컴포넌트와 실제 React 컴포넌트 매핑 | P1 |
| Code-to-design write-back | 코드/스펙을 Figma native layer로 재구성 | P2 |
| create design system rules | agent rule 파일 생성 | P0 |
| semantic layer naming | `Group 5` 대신 `CardContainer` 등 의미 이름 강제 | P0 |

**주의**
- Figma MCP/third-party MCP는 권한과 보안 리스크가 있다.
- Figma REST scope는 가능한 granular scope를 사용하고, 서버측 token은 workspace vault에 저장해야 한다.

### 3-4. Storybook MCP: 실제 구현 컴포넌트를 AI의 진실 소스로 사용

Storybook MCP는 AI 에이전트가 Storybook 문서, props, stories, interaction test, accessibility test를
조회하고 실행할 수 있게 한다. 생성된 UI가 기존 디자인 시스템과 컴포넌트 사용 규칙을 따르도록 강제한다.

**출처**
- Storybook MCP: https://storybook.js.org/docs/ai/mcp/overview

**AADS 적용 포인트**

| 기능 | 의미 | AADS 적용 |
|---|---|---|
| get-documentation | 컴포넌트 props와 사용법 조회 | 디자인 에이전트가 기존 UI 재사용 |
| preview-stories | 채팅 안에서 story preview | ArtifactPanel과 연계 |
| run-story-tests | interaction/a11y 테스트 실행 | QA agent 자동 검수 |
| composition | 여러 Storybook 통합 | AADS/KIS/GO100/NTV2 공용 디자인 자산화 |

**판단**
- AADS 자체 대시보드 디자인 시스템 구축 후 Storybook을 붙이면,
  "AADS 디자인 서비스"가 고객 프로젝트에도 같은 패턴을 제공할 수 있다.

### 3-5. shadcn/v0 Registry: AI가 설치 가능한 디자인 시스템 배포 방식

v0는 shadcn/ui와 Tailwind 기반의 high-fidelity UI 생성을 기본으로 삼고,
custom registry를 통해 컴포넌트, 블록, 디자인 토큰을 AI 모델에 전달한다.
shadcn은 MCP-compatible registry를 지원해 agent가 registry item을 조회하고 설치할 수 있게 한다.

**출처**
- v0 design systems: https://v0.app/docs/design-systems
- shadcn MCP registry: https://ui.shadcn.com/docs/registry/mcp
- Vercel Academy v0 workflow: https://docs.vercel.com/academy/ai-sdk/ui-with-v0

**AADS 적용 포인트**

| 구성 | 설명 | 산출물 |
|---|---|---|
| AADS Registry | AADS용 Button/Card/Input/Badge/Chat primitives | `/registry/*.json` |
| Design tokens | CSS variables + Tailwind tokens | `tokens.css` |
| Blocks | 채팅창, 작업상태, 보고서 카드, 그래프, 관리자 테이블 | reusable blocks |
| MCP endpoint | agent가 컴포넌트 검색/설치 | `/api/v1/design/registry/mcp` |

**판단**
- AADS가 바이브코딩 서비스가 되려면 내부 컴포넌트부터 registry화해야 한다.
- 단순 shadcn 설치보다 "AADS 운영/챗/에이전트 UI 블록"을 registry로 제공하는 편이 차별화된다.

### 3-6. Design Skill 생태계: 품질을 prompt가 아니라 procedure로 만든다

최근 design skill 저장소들은 `SKILL.md`와 `DESIGN.md`를 함께 제공한다.
`SKILL.md`는 AI가 실행할 절차, 품질 기준, 접근성 규칙을 담고,
`DESIGN.md`는 인간이 이해할 디자인 의도와 유지보수 맥락을 담는다.

**출처**
- awesome-design-skills: https://github.com/bergside/awesome-design-skills
- huashu-design: https://github.com/alchaincyf/huashu-design
- guizang-ppt-skill: https://github.com/op7418/guizang-ppt-skill

**AADS 적용 포인트**

| Skill 유형 | 역할 | 예시 |
|---|---|---|
| web-prototype | 웹/앱 화면 생성 | SaaS dashboard, landing, auth flow |
| dashboard | 데이터 밀도 높은 운영 UI | AADS/KIS/GO100 관리자 화면 |
| mobile-app | 모바일 UX flow | NTV2/SF 모바일 화면 |
| critique | 생성물 비평 | 5D design review |
| deck/report | 보고서/피치덱 | CEO 보고서 HTML/PPTX |
| motion/social | 영상/소셜 assets | SF 숏폼 썸네일/모션 |

**핵심 품질 장치**
- 5차원 리뷰: 브랜드 적합성, 시각 위계, 반응형, 접근성, 코드 품질
- P0/P1/P2/P3 checklist: 차단 결함과 개선 권고를 분리
- design philosophy presets: editorial, minimal, utility, brutalist 등 방향성 선택

### 3-7. 앱 빌더와 에이전트 런타임: Dyad, bolt.diy, Cline, Roo, Goose, Aider

바이브코딩 서비스의 공통 구조는 멀티모델/BYOK, 파일시스템 작업, 미리보기, Git/versioning,
사람 승인 루프다.

**출처**
- Dyad: https://github.com/dyad-sh/dyad
- bolt.diy: https://github.com/stackblitz-labs/bolt.diy
- bolt.diy docs: https://stackblitz-labs.github.io/bolt.diy/
- Cline: https://github.com/cline/cline/blob/main/README.md
- Roo Code: https://github.com/RooCodeInc/Roo-Code
- Goose: https://github.com/aaif-goose/goose
- Aider: https://github.com/Aider-AI/aider

**공통 패턴**

| 패턴 | 의미 | AADS 현재 자산 |
|---|---|---|
| Multi-provider | OpenAI/Anthropic/Gemini/Ollama/OpenRouter 등 선택 | `llm_models`, LiteLLM, Codex/Claude relay |
| Local/BYOK | 고객이 API key나 로컬 모델 사용 | Vault 필요 |
| Agent modes | Ask/Architect/Code/Debug/Custom | AADS role/intent prompt layer |
| Human approval | 파일/명령/배포 전 승인 | Pipeline Runner approval |
| Browser/visual QA | 브라우저 조작, screenshot, console log | `browser_*`, `visual_qa_test` |
| Git integration | diff, auto commit, rollback | Runner commit/approve/push |
| MCP extensibility | 외부 도구 연결 | AADS tool registry |

**판단**
- AADS는 이미 Runner, PromptCompiler, MCP tool registry, ArtifactPanel을 갖고 있어
  별도 앱 빌더를 복제하기보다 "디자인 전용 workflow layer"를 얹는 편이 빠르다.

---

## 4. AADS 디자인 서비스 제안: AADS Vibe Design Studio

### 4-1. 서비스 정의

**AADS Vibe Design Studio**는 고객이 자연어, URL, Figma 링크, 스크린샷, 기존 코드 저장소를 넣으면
다음 산출물을 생성/검수/반영하는 디자인 에이전트 서비스다.

| 입력 | 처리 | 출력 |
|---|---|---|
| 자연어 brief | 목표/대상/톤/제약 질문 | Design Brief JSON |
| URL/스크린샷 | 시각 스타일 추출 | DESIGN.md 초안 |
| Figma 링크 | MCP로 frame/component/context 추출 | component mapping |
| 기존 repo | Storybook/shadcn/components 분석 | 재사용 가능 컴포넌트 목록 |
| CEO/고객 피드백 | intent + diff 반영 | 새 artifact version |

### 4-2. 권장 프로세스

```text
1. Intake
   - 비즈니스 목표, 대상 사용자, 감정/톤, 화면 종류, 금지 스타일 수집

2. Design Context Build
   - DESIGN.md 생성 또는 선택
   - design skill 선택
   - Figma/Storybook/shadcn registry/context 수집

3. Direction Generation
   - 3~5개 visual direction 생성
   - 각 direction에 palette, typography, component rule, risk 표시

4. Artifact Build
   - HTML/React/Tailwind artifact 생성
   - sandbox iframe preview
   - 파일 단위 diff와 component usage 기록

5. Design QA
   - 5D self critique
   - Playwright desktop/mobile screenshot
   - a11y/lint/build/story tests

6. Human Review
   - CEO/고객이 시안 선택, 문장 피드백, 직접 수정 요청

7. Production Apply
   - 선택안 React component/route로 반영
   - Git diff, commit, approval, deploy

8. Memory
   - 결정된 DESIGN.md, 선택한 direction, 금지 패턴, QA 결과를 workspace memory로 저장
```

### 4-3. AADS 아키텍처 초안

```text
AADS Chat UI
  └─ Design Studio Panel
       ├─ Brief Form
       ├─ Source Connector: URL / Screenshot / Figma / Repo / Storybook
       ├─ Direction Picker
       ├─ Artifact Preview
       ├─ Design Review Report
       └─ Apply-to-Code Button

Backend
  ├─ design_projects
  ├─ design_sessions
  ├─ design_sources
  ├─ design_tokens
  ├─ design_artifacts
  ├─ design_reviews
  ├─ design_registry_items
  └─ compiled_prompt_provenance 연동

Agent Runtime
  ├─ PromptCompiler: DESIGN.md + SKILL.md + project AGENTS.md
  ├─ Tool Search: intent=design_* 일 때 Figma/Storybook/shadcn/browser tools 동적 로딩
  ├─ Pipeline Runner: production apply
  └─ Visual QA: Playwright screenshot + DOM + accessibility checks
```

### 4-4. DB 초안

| 테이블 | 역할 |
|---|---|
| `design_projects` | 고객/프로젝트별 디자인 작업 단위 |
| `design_sessions` | 한 번의 디자인 대화/시안 생성 세션 |
| `design_sources` | URL, Figma, screenshot, repo, uploaded asset |
| `design_tokens` | colors, typography, spacing, radius, shadow |
| `design_artifacts` | HTML/React/PNG/PDF/ZIP 산출물 및 version |
| `design_reviews` | 5D critique, QA result, approve/reject |
| `design_registry_items` | AADS/shadcn/custom registry blocks |
| `design_memory_facts` | 선택/금지/선호 패턴 memory 승격 |

### 4-5. PromptCompiler 연동

| 레이어 | 추가 asset |
|---|---|
| L1 Global | 디자인 안전/보안/검증 원칙 |
| L2 Project | 프로젝트별 brand/design constraints |
| L3 Role | Designer / Frontend Engineer / QA Designer |
| L4 Intent | `design_intake`, `design_generate`, `design_critique`, `design_apply` |
| L5 Model | 이미지/코드/리뷰 모델별 실행 제약 |

`compiled_prompt_provenance.applied_assets`에 어떤 `DESIGN.md`와 `SKILL.md`가 실제 적용됐는지 기록해야
디자인 품질 문제를 추적할 수 있다.

---

## 5. 기존 AADS 스마트 디자인 시스템 기획과의 관계

기존 문서 `docs/plans/AADS-SMART-DESIGN-SYSTEM.md`는 AADS 대시보드 내부 UI 품질 개선안이다.
이번 보고서는 그 위에 올라가는 "서비스 상품화 프로세스"다.

| 구분 | 기존 스마트 디자인 시스템 | 이번 디자인 서비스 프로세스 |
|---|---|---|
| 목적 | AADS 대시보드 UI 일관화 | 고객/프로젝트용 디자인 생성 서비스 |
| 핵심 자산 | 토큰, Button/Card/Input, lucide, ThemeProvider | DESIGN.md, SKILL.md, source connector, review loop |
| 범위 | 내부 dashboard | AADS/KIS/GO100/SF/NTV2/NAS 및 외부 고객 프로젝트 |
| 완료 기준 | 내부 컴포넌트 재사용/테마 통합 | artifact 생성, QA, 코드 반영, memory 저장 |
| 선행 필요 | 예 | 내부 디자인 시스템이 registry seed가 됨 |

**판단**
- P0는 내부 AADS 디자인 토큰/컴포넌트 registry 구축이다.
- P1부터 외부 디자인 서비스 UI를 붙여야 한다.

---

## 6. 구현 로드맵

### Phase 0: 내부 기반 정리

| 작업 | 산출물 | 검증 |
|---|---|---|
| AADS 디자인 토큰 통합 | `design-tokens.css` | dark/light screenshot |
| shadcn/Radix/lucide 기반 primitives | `components/ui/*` | lint/build |
| AADS registry seed | `registry/aads/*.json` | shadcn MCP 호환성 |
| `DESIGN.md` 초안 생성 | repo root 또는 docs/design | Codex/Claude에서 참조 확인 |

### Phase 1: 디자인 서비스 MVP

| 작업 | 산출물 | 검증 |
|---|---|---|
| Design Brief form | `/design-studio` 또는 Chat side panel | form schema test |
| DESIGN.md generator | URL/brief 기반 markdown 생성 | sample output review |
| Artifact preview | HTML iframe + version list | Playwright screenshot |
| 5D review agent | score/report JSON | 기준 미달 차단 |
| Apply-to-code | Runner 작업 생성 | diff/commit/approval |

### Phase 2: 외부 소스 연동

| 작업 | 산출물 | 검증 |
|---|---|---|
| Figma MCP connector | source type `figma` | token scope/audit |
| Storybook MCP connector | source type `storybook` | story test 실행 |
| shadcn custom registry | components/blocks/tokens | install test |
| Screenshot style extractor | screenshot→style brief | 비교 리포트 |

### Phase 3: 상품화

| 작업 | 산출물 |
|---|---|
| 고객 workspace billing/quotas | 작업 수, 모델비, 저장공간 제한 |
| 디자인 프로젝트 템플릿 | SaaS, admin, marketplace, mobile, deck |
| 공유/승인 링크 | 고객이 시안 선택/코멘트 |
| 브랜드 메모리 | 고객별 금지 패턴/선호 style 자동 반영 |

---

## 7. 리스크와 통제 방안

| 리스크 | 근거 | 통제 |
|---|---|---|
| AI slop / 유사한 UI 반복 | DESIGN.md 이전 도구들의 공통 문제 | DESIGN.md + skill + 5D review 필수 |
| Figma/MCP 권한 과다 | Figma MCP와 third-party MCP의 RCE/command injection 사례 존재 | official MCP 우선, vault, 최소 scope, sandbox |
| 생성물 저작권/브랜드 침해 | awesome-design-md는 public website inspired 문서 | "inspired by"와 고객 소유 디자인 분리, 상표 사용 금지 |
| 보안 취약 코드 생성 | vibe coding 보안 우려 | build/lint/a11y/security scan, human approval |
| 디자인-코드 불일치 | Figma export만으로는 실제 컴포넌트 추정 | Code Connect + Storybook MCP |
| 비용 폭증 | 다중 시안+이미지 생성+QA 반복 | phase별 budget, preview low-cost model, final high-quality model |
| 프롬프트 provenance 누락 | AADS prompt governance 핵심 리스크 | compiled_prompt_provenance에 DESIGN/SKILL 적용 기록 |

**보안 참고 출처**
- GitLab Advisory CVE-2025-53967: https://advisories.gitlab.com/npm/figma-developer-mcp/CVE-2025-53967/
- Figma REST scopes: https://developers.figma.com/docs/rest-api/scopes/

---

## 8. AADS에 즉시 반영할 설계 원칙

1. `DESIGN.md`를 모든 디자인 작업의 단일 시각 계약으로 둔다.
2. `SKILL.md`는 agent 실행 절차와 품질 gate로 분리한다.
3. 생성 전 질문 폼을 강제한다. 불충분한 brief로 바로 생성하지 않는다.
4. 모든 artifact는 iframe preview와 파일 version을 가진다.
5. 코드 반영은 Runner approval flow를 사용한다.
6. 디자인 산출물은 반드시 desktop/mobile screenshot QA를 통과해야 한다.
7. Figma/Storybook/shadcn은 optional connector로 두고, 기본은 로컬/DB 기반으로 동작한다.
8. provenance와 memory를 남겨 다음 세션에서 같은 취향을 반복 적용한다.

---

## 9. 권장 다음 작업

| 우선순위 | 작업 | 이유 |
|---|---|---|
| P0 | AADS 내부 `DESIGN.md` 생성 | 디자인 서비스의 seed이자 내부 UI 개선 기준 |
| P0 | 디자인 intent 4개 추가: intake/generate/critique/apply | PromptCompiler와 Tool Search 동적 로딩에 필요 |
| P0 | ArtifactHtmlPreview를 디자인 artifact version viewer로 확장 | 현재 AADS에 이미 유사 기반이 있음 |
| P1 | shadcn-compatible AADS registry 구축 | 서비스가 재사용할 컴포넌트 진실 소스 |
| P1 | 5D design review JSON schema와 QA runner 추가 | AI slop과 저품질 산출물 차단 |
| P2 | Figma MCP/Storybook MCP connector 추가 | 외부 고객 프로젝트 연동 |

---

## 10. 출처 목록

1. Google, "Stitch app's DESIGN.md format is now open-source for designers", 2026-05 확인
   https://blog.google/innovation-and-ai/models-and-research/google-labs/stitch-design-md/
2. Google, "Introducing vibe design with Stitch", 2026-03-18
   https://blog.google/innovation-and-ai/models-and-research/google-labs/stitch-ai-ui-design/
3. VoltAgent, `awesome-design-md`, GitHub, 2026-05 확인
   https://github.com/VoltAgent/awesome-design-md
4. nexu-io, `open-design`, GitHub, 2026-05 확인
   https://github.com/nexu-io/open-design
5. Figma MCP Server Guide, GitHub MCP Registry, 2026-05 확인
   https://github.com/mcp/com.figma.mcp/mcp
6. Figma Code Connect Developer Docs, 2026-05 확인
   https://developers.figma.com/docs/code-connect/
7. Figma MCP Developer Docs, 2026-05 확인
   https://developers.figma.com/docs/figma-mcp-server/
8. OpenAI, "OpenAI Codex and Figma launch seamless code-to-design experience", 2026-02-26
   https://openai.com/index/figma-partnership/
9. Storybook MCP Server Docs, 2026-05 확인
   https://storybook.js.org/docs/ai/mcp/overview
10. shadcn/ui MCP Server Docs, 2026-05 확인
   https://ui.shadcn.com/docs/registry/mcp
11. v0 Design Systems Docs, 2026-05 확인
   https://v0.app/docs/design-systems
12. dyad-sh, `dyad`, GitHub, 2026-05 확인
    https://github.com/dyad-sh/dyad
11. stackblitz-labs, `bolt.diy`, GitHub, 2026-05 확인
    https://github.com/stackblitz-labs/bolt.diy
12. cline, `cline`, GitHub, 2026-05 확인
    https://github.com/cline/cline
13. RooCodeInc, `Roo-Code`, GitHub, 2026-05 확인
    https://github.com/RooCodeInc/Roo-Code
14. aaif-goose, `goose`, GitHub, 2026-05 확인
    https://github.com/aaif-goose/goose
15. Aider-AI, `aider`, GitHub, 2026-05 확인
    https://github.com/Aider-AI/aider
16. bergside, `awesome-design-skills`, GitHub, 2026-05 확인
    https://github.com/bergside/awesome-design-skills
17. alchaincyf, `huashu-design`, GitHub, 2026-05 확인
    https://github.com/alchaincyf/huashu-design
18. op7418, `guizang-ppt-skill`, GitHub, 2026-05 확인
    https://github.com/op7418/guizang-ppt-skill
19. GitLab Advisory, CVE-2025-53967, 2026-05 확인
    https://advisories.gitlab.com/npm/figma-developer-mcp/CVE-2025-53967/
