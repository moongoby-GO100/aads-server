# AADS Chat Frontend 명세

_v1.0 | 2026-04-02 | 최초 작성_

## 1. 파일 구조

```
src/app/chat/
├── layout.tsx              (17L)   — 세그먼트 레이아웃, ThemeProvider
├── page.tsx                (4501L) — 메인 채팅 페이지 (핵심)
├── ChatInput.tsx           (406L)  — 메시지 입력 컴포넌트
├── ChatSidebar.tsx         (566L)  — 워크스페이스/세션 사이드바
├── ChatArtifactPanel.tsx   (513L)  — 아티팩트 패널 (보고서/코드/차트)
├── MarkdownRenderer.tsx    (361L)  — 마크다운 + 코드 하이라이팅
├── api.ts                  (48L)   — API 헬퍼 (fetch wrapper)
└── types.ts                (114L)  — TypeScript 타입 정의
                            ─────
                            총 6,509줄
```

## 2. 핵심 컴포넌트 상세

### 2.1 page.tsx (4,501줄) — 메인 페이지

채팅 시스템의 핵심. 모든 상태 관리, SSE 스트리밍, 메시지 CRUD, 복구 로직이 포함.

#### 주요 상태 (useState)

| State | 타입 | 용도 |
|-------|------|------|
| `messages` | `ChatMessage[]` | 현재 세션 메시지 목록 |
| `streamBuf` | `string` | SSE 스트리밍 중 AI 응답 버퍼 |
| `streaming` | `boolean` | 스트리밍 진행 중 여부 |
| `workspaces` | `Workspace[]` | 워크스페이스 목록 |
| `sessions` | `ChatSession[]` | 현재 워크스페이스의 세션 목록 |
| `curWs` | `string` | 현재 워크스페이스 ID |
| `curSession` | `string` | 현재 세션 ID |
| `theme` | `"dark" \| "light"` | 테마 |
| `artifactMode` | `"full" \| "mini" \| "hidden"` | 아티팩트 패널 표시 모드 |
| `toolStatus` | `string` | 도구 호출 상태 표시 텍스트 |
| `messagesLoading` | `boolean` | 메시지 로딩 중 (스켈레톤 UI) |

#### 핵심 함수

| 함수 | 줄 범위 (대략) | 기능 |
|------|---------------|------|
| `handleSend()` | — | 메시지 전송 → POST /chat/messages/send → SSE 연결 |
| `connectSSE()` | — | EventSource 생성 → 토큰 수신 → streamBuf 업데이트 |
| `invisibleRecovery()` | — | SSE 끊김 시 stream-resume → last-response → waitingBg |
| `loadMessages()` | — | 세션 메시지 로드 (cursor 기반 페이지네이션) |
| `handleSessionChange()` | — | 세션 전환 → 타이머 cleanup → 메시지 로드 |

#### SSE 이벤트 처리

```typescript
// EventSource에서 수신하는 이벤트 타입
type SSEEventType =
  | "token"           // LLM 생성 토큰
  | "tool_start"      // 도구 호출 시작
  | "tool_result"     // 도구 호출 결과
  | "done"            // 스트리밍 완료
  | "error"           // 에러 (recoverable 포함)
  | "heartbeat"       // 연결 유지용 (3초 간격)
  | "thinking"        // 사고 과정 요약
  | "cost"            // 비용 정보
  | "artifact"        // 아티팩트 생성
```

#### 끊김 복구 메커니즘 (A-1 ~ A-4, 2026-04-02 적용)

```
SSE 끊김 감지 (onerror)
  ↓
[A-1] 기존 AI 버블 유지 (streaming=true 해제 안 함)
  ↓
[A-2] 복구 상태 UI: "🔄 응답 복구 중..." (toolStatus)
  ↓
stream-resume (최대 5회, 120s 타임아웃)
  ├─ 성공 → [A-4] delta 토큰 이어붙이기 (같은 버블)
  └─ 실패 → last-response 폴링 (3회)
              ├─ 성공 → [A-1] requestAnimationFrame으로 깜빡임 없이 교체
              └─ 실패 → waitingBg (30s) → 백그라운드 완료 대기
```

### 2.2 ChatInput.tsx (406줄)

| 기능 | 상세 |
|------|------|
| 텍스트 입력 | auto-resize textarea, Shift+Enter 줄바꿈 |
| 파일 첨부 | 드래그&드롭 + 버튼, 이미지 미리보기 |
| 모델 선택 | 드롭다운 (claude-opus, claude-sonnet, gemini 등) |
| 답글 | reply_to_id 지정, 원본 메시지 미리보기 |
| 중지 버튼 | 스트리밍 중 → POST /chat/sessions/{id}/stop |

### 2.3 ChatSidebar.tsx (566줄)

| 기능 | 상세 |
|------|------|
| 워크스페이스 목록 | 아이콘 + 색상, CRUD |
| 세션 목록 | 시간순 정렬, 핀 고정, 태그 필터 |
| 세션 검색 | 제목/내용 필터링 |
| 새 세션 생성 | 자동 제목 생성 (versioned) |
| 세션 삭제 | 확인 대화상자 |

### 2.4 ChatArtifactPanel.tsx (513줄)

| 기능 | 상세 |
|------|------|
| 아티팩트 유형 | report, code, chart, dashboard, table, image, file, text |
| 표시 모드 | full (오른쪽 패널) / mini (하단) / hidden |
| 내보내기 | PDF, Markdown, JSON |
| 코드 하이라이팅 | 언어 자동 감지 |

### 2.5 MarkdownRenderer.tsx (361줄)

| 기능 | 상세 |
|------|------|
| 마크다운 렌더링 | react-markdown 기반 |
| 코드 블록 | 구문 하이라이팅 + 복사 버튼 |
| 테이블 | 스타일링된 표 |
| 수식 | KaTeX 지원 |
| 도구 호출 UI | 접기/펼치기 + 파라미터/결과 인라인 |

### 2.6 api.ts (48줄)

```typescript
// 핵심 함수
chatApi<T>(path, opts?)  // fetch wrapper + auth header + error handling
uploadChatFile(file, sessionId)  // FormData 파일 업로드
getToken()  // localStorage에서 JWT 토큰
authHdrs()  // Authorization 헤더 생성
```

### 2.7 types.ts (114줄)

| 타입 | 주요 필드 |
|------|----------|
| `Workspace` | id, name, description, icon, color |
| `ChatSession` | id, workspace_id, title, current_model, pinned, tags, message_count |
| `ChatMessage` | id, role, content, model_used, intent, tokens, cost, attachments, tools_called, reply_to_id, branch_id |
| `Artifact` | id, artifact_type, title, content, metadata |
| `Theme` | "dark" \| "light" |
| `ArtifactMode` | "full" \| "mini" \| "hidden" |

## 3. 테마 시스템

CSS 변수 기반 다크/라이트 모드. `types.ts`에서 `DARK`/`LIGHT` 객체 정의.  
`layout.tsx`의 `ThemeProvider`로 전역 적용. `chat-theme.css` 참조.

## 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.0 | 2026-04-02 | 초기 작성 — 8파일 전체 명세 |
