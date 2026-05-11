# AADS 스마트 디자인 시스템 구축 기획서

**TASK_ID**: AADS-DESIGN-SYSTEM
**작성일**: 2026-05-11
**작성자**: CTO AI
**상태**: 기획 완료 → CEO 승인 대기
**우선순위**: P1

---

## 1. 배경 및 목적

### 1-1. 현황 문제

AADS 대시보드(Next.js 16 + React 19)는 디자인 시스템이 부재한 상태로 운영되고 있다.

| 문제 | 상세 |
|------|------|
| UI 라이브러리 없음 | shadcn/Radix/MUI 등 미사용, 모든 컴포넌트 자체 제작 |
| 디자인 토큰 분열 | globals.css(`:root`) + chat-theme.css(`--ct-*`) 2벌 운영, accent 색 불일치 |
| 스타일링 3혼용 | Tailwind className + CSS 변수 inline + 하드코딩 색상(#a78bfa 등) |
| 원자 컴포넌트 전무 | `src/components/ui/` 비어있음, Button/Card/Modal 매번 재작성 |
| 아이콘 이모지 대용 | 🏠💬📊⚙️ — 크기 조정, 색상 변경 불가 |
| 테마 적용 범위 한정 | 다크/라이트 채팅 UI에만 적용, 글로벌 대시보드 미적용 |
| 접근성 미흡 | ChatBubble aria 미적용, 키보드 탐색 미구현 |
| TypeScript 취약 | ChatInput @ts-nocheck, 빌드 시 ignoreBuildErrors: true |
| 백업 파일 방치 | .bak 파일 10개 이상 소스 트리에 존재 |

### 1-2. 목표

> "한 번 정의하면 전체에 일관 적용되는 토큰 기반 디자인 시스템"

- 색상 변경 1곳 → 전체 UI 즉시 반영
- 다크/라이트 전역 자동 전환
- 컴포넌트 재사용률 70%+ 달성
- 새 화면 개발 속도 2~3배 단축

---

## 2. 기술 스택 현황

| 항목 | 현재 상태 |
|------|----------|
| 프레임워크 | Next.js 16.1.6 + React 19.2.3 |
| 스타일링 | Tailwind CSS v4 (tailwind.config.ts 없음) |
| UI 라이브러리 | 없음 |
| 아이콘 | 이모지 대용 |
| 디자인 토큰 | 2벌 분열 (globals.css vs chat-theme.css) |
| 공용 컴포넌트 | src/components/ui/ 비어있음 |
| 테마 | ThemeContext → data-theme, 채팅 전용 |
| 백엔드 테마 API | 없음 (chat_workspaces.color/icon만 존재) |

### 핵심 파일 경로

- `/root/aads/aads-dashboard/src/app/globals.css` — 글로벌 CSS 변수
- `/root/aads/aads-dashboard/src/styles/chat-theme.css` — 채팅 전용 토큰
- `/root/aads/aads-dashboard/src/contexts/ThemeContext.tsx` — 테마 컨텍스트
- `/root/aads/aads-dashboard/src/components/chat/ChatBubble.tsx` — 578 LoC, 하드코딩 집중
- `/root/aads/aads-dashboard/src/components/Header.tsx` — bg-white 하드코딩
- `/root/aads/aads-dashboard/src/components/Sidebar.tsx` — 글로벌 사이드바
- `/root/aads/aads-dashboard/package.json` — 의존성 목록

### 채팅 컴포넌트 규모

| 파일 | LoC | 역할 |
|------|-----|------|
| TerminalPane.tsx | 874 | xterm.js 터미널 |
| DiscussionPanel.tsx | 760 | 멀티에이전트 토론 |
| ChatBubble.tsx | 578 | 메시지 버블 + 인라인 마크다운 |
| ArtifactReport.tsx | 461 | 보고서 아티팩트 |
| ChatOpsDock.tsx | 418 | 운영 독 |
| MemoryContextBar.tsx | 406 | 메모리 컨텍스트 |
| ChatInput.tsx | 391 | 입력창 |
| ChatStream.tsx | 335 | SSE 스트리밍 |

---

## 3. 아키텍처

```
┌─────────────────────────────────────────────────┐
│              Smart Design System                │
├─────────────────────────────────────────────────┤
│  L1. 디자인 토큰 (단일 진실 소스)                  │
│    colors / spacing / typography / radius / shadow│
│    → CSS 변수 + Tailwind 등록                     │
├─────────────────────────────────────────────────┤
│  L2. 원자 컴포넌트 (shadcn/ui 기반)               │
│    Button / Input / Badge / Card / Modal /       │
│    Select / Tooltip / Tabs / Avatar              │
├─────────────────────────────────────────────────┤
│  L3. 복합 컴포넌트 (AADS 전용)                    │
│    ChatBubble / ArtifactCard / StatusBadge /      │
│    ProjectCard / CostChart / PipelineStatus      │
├─────────────────────────────────────────────────┤
│  L4. 페이지 레이아웃                              │
│    DashboardLayout / ChatLayout / AdminLayout     │
├─────────────────────────────────────────────────┤
│  L5. 테마 엔진                                   │
│    ThemeProvider (전역) / 다크·라이트·카카오봇      │
│    + DB 기반 워크스페이스별 accent 커스터마이징      │
└─────────────────────────────────────────────────┘
```

---

## 4. Phase별 실행 계획

### Phase 1: 토큰 통합 + 기반 세팅 (P0, 3일)

| 작업 | 내용 | 산출물 |
|------|------|--------|
| 토큰 통합 | `:root` + `--ct-*` 2벌 → 단일 토큰 체계 병합 | `design-tokens.css` |
| Tailwind 연동 | CSS 변수를 Tailwind 유틸리티로 등록 | `tailwind.config.ts` |
| cn() 유틸 | `clsx` + `tailwind-merge` 도입 | `src/lib/utils.ts` |
| 테마 확장 | ThemeProvider 전역 적용 | `ThemeContext.tsx` 수정 |
| 하드코딩 제거 | ChatBubble 등의 하드코딩 색상 → CSS 변수 치환 | 주요 5개 파일 |

**토큰 설계안:**
```css
:root, [data-theme="dark"] {
  --ds-bg:          #0f172a;
  --ds-bg-card:     #1e293b;
  --ds-bg-hover:    #334155;
  --ds-bg-input:    #1a2332;
  --ds-text:        #e2e8f0;
  --ds-text-muted:  #94a3b8;
  --ds-accent:      #6C5CE7;
  --ds-accent-hover:#7c6ff7;
  --ds-success:     #22c55e;
  --ds-warning:     #f59e0b;
  --ds-danger:      #ef4444;
  --ds-info:        #3b82f6;
  --ds-radius:      0.5rem;
  --ds-radius-lg:   0.75rem;
}

[data-theme="light"] {
  --ds-bg:          #f8fafc;
  --ds-bg-card:     #ffffff;
  --ds-bg-hover:    #f1f5f9;
  --ds-text:        #1e293b;
  --ds-text-muted:  #64748b;
}
```

### Phase 2: 원자 컴포넌트 라이브러리 (P1, 5일)

| 컴포넌트 | 우선순위 | 현재 | 목표 |
|----------|---------|------|------|
| Button | P0 | 매번 재작성 | variant(primary/ghost/danger), size(sm/md/lg) |
| Input | P0 | 하드코딩 | 통합 입력 컴포넌트 |
| Badge | P0 | 인라인 스타일 | status 기반 자동 색상 |
| Card | P1 | div + inline | 헤더/바디/푸터 슬롯 |
| Modal/Dialog | P1 | 조건부 렌더링 | Radix Dialog 기반 |
| Select | P1 | HTML select | 검색 가능 셀렉트 |
| Tooltip | P2 | 없음 | Radix Tooltip |
| Tabs | P2 | 자체 구현 | Radix Tabs |
| Avatar | P2 | 이모지 | 이니셜/이미지 지원 |

**기술 선택**: shadcn/ui 방식 (Radix UI + Tailwind, 소스 복사)

### Phase 3: 아이콘 + 기존 컴포넌트 마이그레이션 (P1, 4일)

| 작업 | 내용 |
|------|------|
| 아이콘 라이브러리 | `lucide-react` 도입 |
| ChatBubble 리팩터링 | 578줄 → 원자 컴포넌트 조합 (300줄 목표) |
| Header 통합 | bg-white 하드코딩 → 토큰 기반 |
| Sidebar 통합 | 글로벌+채팅 Sidebar 토큰 기반 통합 |
| .bak 파일 정리 | 소스 트리 백업 파일 제거 |

### Phase 4: 테마 엔진 + DB 연동 (P2, 3일)

| 작업 | 내용 |
|------|------|
| 워크스페이스별 accent | chat_workspaces.color → CSS 변수 동적 주입 |
| 테마 설정 API | PUT /api/v1/design/theme |
| 테마 프리셋 | Dark / Light / High Contrast / 카카오봇 |
| 시스템 테마 감지 | prefers-color-scheme 자동 반응 |

### Phase 5: 품질 강화 (P2, 2일)

| 작업 | 내용 |
|------|------|
| TypeScript 정상화 | @ts-nocheck 제거, ignoreBuildErrors: false |
| 접근성 | aria-label, 키보드 탐색, 포커스 관리 |
| Storybook | 주요 원자 컴포넌트 문서화 (선택사항) |

---

## 5. 기술 선택 근거

| 결정 | 선택 | 이유 |
|------|------|------|
| 컴포넌트 기반 | shadcn/ui (Radix + Tailwind) | 소스 소유, 번들 최소, Next.js 생태계 표준 |
| 아이콘 | lucide-react | tree-shakable, shadcn 기본 호환 |
| CSS 유틸 | clsx + tailwind-merge | 조건부 클래스 합성, 중복 제거 |
| 토큰 관리 | CSS 변수 + Tailwind 매핑 | 런타임 테마 전환, 빌드 불필요 |
| 상태 관리 | 기존 Context 확장 | 추가 라이브러리 불필요 |

---

## 6. 예상 효과

| 지표 | 현재 | 목표 |
|------|------|------|
| 컴포넌트 재사용률 | ~10% | 70%+ |
| 하드코딩 색상 | 30+ 곳 | 0 |
| 새 화면 개발 속도 | 기준 | 2~3배 단축 |
| 테마 전환 범위 | 채팅만 | 전체 대시보드 |
| 디자인 토큰 세트 | 2벌 (불일치) | 1벌 (통합) |
| 접근성 | WCAG 미달 | WCAG 2.1 AA |

---

## 7. 일정 총괄

| Phase | 소요 | 누적 | 선행조건 | 병렬 가능 |
|-------|------|------|---------|----------|
| Phase 1: 토큰 통합 | 3일 | 3일 | — | — |
| Phase 2: 원자 컴포넌트 | 5일 | 8일 | Phase 1 | — |
| Phase 3: 마이그레이션 | 4일 | 12일 | Phase 2 | — |
| Phase 4: 테마 엔진 | 3일 | 15일 | Phase 1 | Phase 2/3과 병렬 가능 |
| Phase 5: 품질 강화 | 2일 | 17일 | Phase 2 | Phase 3과 병렬 가능 |

**병렬 진행 시 실질 13~14일 소요**

---

## 8. 리스크 및 대응

| 리스크 | 영향 | 대응 |
|--------|------|------|
| 마운트 권한 (ro) | Runner 대시보드 수정 불가 | write_remote_file 직접 수정 또는 마운트 rw 전환 |
| ChatBubble 리팩터링 | 기존 기능 회귀 | Phase별 점진 교체, 스냅샷 테스트 |
| ignoreBuildErrors 해제 | 빌드 실패 가능 | Phase 5에서 타입 오류 일괄 수정 후 전환 |
| 배포 중 UI 깨짐 | 사용자 영향 | 토큰 교체는 시각적 동등성 유지 후 전환 |

---

## 9. 주의사항

- Runner 마운트 문제로 대시보드 수정은 `write_remote_file`/`patch_remote_file` 직접 수정 방식 사용
- 대시보드 빌드/배포: `docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard && docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard`
- Phase 1의 토큰 교체 시 기존 UI와 시각적 동등성(visual parity)을 반드시 유지

---

*문서 끝*
