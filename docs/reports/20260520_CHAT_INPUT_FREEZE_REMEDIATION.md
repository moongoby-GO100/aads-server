# AADS 채팅창 입력 시 브라우저 멈춤 — 조치 계획

- **작성일**: 2026-05-20
- **대상**: `/app/aads-dashboard/src/app/chat/page.tsx` (6,588 라인)
- **증상**: AADS 채팅창에 지시를 입력/제출하면 브라우저가 잠시 멈춤
- **체감 구간**: "입력 제출 직후 ~ 스트리밍 시작" + "스트리밍 진행 중 긴 응답"

---

## 1. 원인 요약

거대 모놀리식 컴포넌트(state 82개 / hook 64개)가 스트리밍 텍스트 상태를 보유하여, 30 ms마다 발생하는 `setStreamBuf`가 전체 트리를 리렌더한다. 매 리렌더마다 다음이 반복된다.

1. `messages` 전체 정렬·그룹핑 재계산
2. 모든 `MessageItem`에 매번 새 inline 함수/객체가 전달되어 `React.memo` 무효화 → N개 메시지 전부 재렌더
3. 각 메시지의 `MarkdownBlock`이 누적 텍스트 전체를 다시 파싱

여기에 첨부 이미지의 동기 base64 인코딩과 `messages.length` 의존 effect 체인이 가산된다.

---

## 2. 확인된 병목 (코드 위치)

### A. 거대 컴포넌트 / 입력 시 전체 트리 리렌더
- `page.tsx:1170` `const [input, setInput] = useState("")`
- `page.tsx:5700` 매 렌더 `[...messages].filter().sort(...)` — 신규 배열 생성·정렬
- `page.tsx:5732` 매 렌더 `display.map(...)` — 그룹핑 루프 재실행
- `page.tsx:5775~5783` `MessageItem`에 inline 함수(`onViewArtifact`, `onOpenLightbox`, `onViewReport`, `onStopStreaming`) 매번 신규 전달
- `page.tsx:5776` `linkedArtifact={msg.artifact_id ? artifacts.find(...) : undefined}` — 메시지마다 선형 탐색 + 신규 객체 ref

### B. 스트리밍 텍스트 — 30 ms마다 전체 트리 리렌더 + Markdown 재파싱
- `page.tsx:3211~3216` `setInterval(..., 30 ms)`로 `setStreamBuf` 누적 호출
- `page.tsx:715` `<MarkdownBlock text={streamingContent} />` — 누적 텍스트 전체를 30 ms마다 재파싱
- `page.tsx:2445` 2초마다 `setMessages(prev => prev.map(...))` — messages 배열 신규 생성

### C. 첨부 파일 동기 인코딩
- `page.tsx:3122~3123` `btoa(String.fromCharCode(...new Uint8Array(arrBuf)))` — 수 MB 이미지에서 메인스레드 수백 ms 점유, 대용량에서 stack overflow 위험

### D. 메시지 길이 의존 effect 체인
- `page.tsx:2383` `useEffect(..., [activeSession?.id, messages.length, streaming])`
- `page.tsx:371~375` `MessageItem` 내부 `useEffect(..., [isLastAssistantMsg, ...])` — 마지막 메시지 변경 시 N개 effect 동시 발생

### E. 무관 패널 동반 리렌더
- `apiKeyInfo`, `sessionCost`, `artifactToast` 등이 동일 컴포넌트에 위치 → 스트림 리렌더와 무관해야 할 패널까지 함께 처리됨

> 외부 컴포넌트(`./ChatInput`, `./MarkdownRenderer`, `./ChatSidebar`, `./ChatArtifactPanel`)는 본 환경에 원본이 없어 내부 동작은 추정. `chatInputRef.current?.getValue()` 패턴으로 보아 ChatInput은 uncontrolled로 추정되며, **타이핑 자체보다 제출 직후 + 스트리밍 구간 멈춤이 본질**.

---

## 3. 조치 항목 (우선순위별)

### P0 — 즉시 효과 큰 항목 (1일 이내 적용 가능)

**P0-1. 스트리밍 텍스트 상태를 자식 컴포넌트로 격리**
- 신규 `<StreamingBubble />`를 만들고 내부에서만 `streamBuf` state 보유
- page에서는 ref/EventEmitter 또는 `useSyncExternalStore`로 토큰 push
- 효과: 30 ms 드레인이 단일 버블만 리렌더

**P0-2. MessageItem prop 안정화**
- `onViewArtifact`, `onOpenLightbox`, `onViewReport`, `onStopStreaming` → page 레벨 `useCallback`
- `linkedArtifact`는 `artifactById = useMemo(() => new Map(artifacts.map(a => [a.id, a])), [artifacts])` Map lookup으로 전환
- 효과: `React.memo(MessageItem)` 정상 동작 → 새 메시지 1건 추가 시 기존 메시지 재파싱 제거

**P0-3. `sorted` / `display` 메모이제이션**
- `const sorted = useMemo(() => [...messages].filter(...).sort(...), [messages])`
- 그룹핑 루프도 동일 메모

### P1 — 다음 단계 (2~3일)

**P1-4. MarkdownBlock 결과 캐싱 + 델타 파싱**
- 내부에 `useMemo(() => parse(text), [text])`
- 4 KB 초과 누적 시 마지막 N줄만 마크다운 파싱, 앞쪽은 캐시 재사용

**P1-5. drain 타이머 `rAF` 기반으로 변경**
- `setInterval(30ms)` → `requestAnimationFrame` + 누적 토큰 일괄 적용
- 효과: vsync 정렬, 프레임당 1회 setState

**P1-6. base64 인코딩 비동기화**
- `screenFile`은 `FileReader.readAsDataURL` 또는 `Blob` 그대로 FormData 첨부
- `btoa(String.fromCharCode(...))` 제거

### P2 — 구조 개선 (1~2주, 별도 TPP 권장)

**P2-7. page.tsx 컴포넌트 분리**
- Sidebar / MessageList / Composer / ArtifactPanel / OpsDock로 분할 → state locality 확보

**P2-8. 메시지 리스트 가상화**
- `react-virtuoso` 등으로 viewport 외 DOM 제거

**P2-9. Streaming text를 ref + `useSyncExternalStore` 패턴**
- React reconciliation 우회로 스트리밍 jank 완전 제거

---

## 4. 적용 순서 및 일정

| 순서 | 항목 | 예상 시간 | 기대 효과 |
|------|------|-----------|-----------|
| 1 | P0-2 + P0-3 | 30분 | 입력/메시지 추가 시 N건 재파싱 제거 |
| 2 | P0-1 (StreamingBubble 분리) | 1~2시간 | 스트리밍 중 jank 거의 제거 |
| 3 | P1-4 MarkdownBlock 메모/델타 | 2~3시간 | 긴 답변 마지막 구간 부드러움 |
| 4 | P1-5 rAF, P1-6 base64 비동기 | 1~2시간 | 프레임 일관성, 이미지 첨부 멈춤 제거 |
| 5 | P2-7 ~ P2-9 | 별도 TPP | 장기 안정성 |

---

## 5. 검증 방법

**Chrome DevTools Performance 탭** 기반:
1. 베이스라인 측정: "지시 입력 → Enter → 첫 토큰 도착"까지 Long Task(>50 ms) 개수/합계
2. 스트리밍 중 5초간 Long Task 합계
3. 각 단계 적용 후 동일 측정 → P0 적용 후 **Long Task 합계 80% 이상 감소** 목표
4. 사용자 체감 점검: "입력창에 글자 입력 → 표시 지연 < 16 ms", "Enter → 첫 토큰 < 200 ms"

**회귀 방지**:
- 메시지 100건 / 1,000건 세션에서 동일 메트릭 측정
- 첨부 이미지 5 MB / 20 MB 케이스 별도 테스트

---

## 6. 작업 분담 / 다음 단계

1. CEO 승인 후 `_todo/`에 P0 묶음 TPP 등록
2. P0 패치는 단일 PR(또는 단일 커밋 묶음)로 진행 — pre-commit hook 통과 확인 필수
3. 배포는 대시보드 무중단 배포(`docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard && up -d aads-dashboard`)로 적용

---

## 7. 참고

- 관련 스펙: `docs/chat/CHAT-FRONTEND-SPEC.md`, `docs/chat/CHAT-STREAMING-SPEC.md`
- 이전 경량화 시도: `docs/reports/20260506_CHAT_LIGHTWEIGHT_V2.md`
- 본 환경에 `./ChatInput.tsx`, `./MarkdownRenderer.tsx`, `./ChatSidebar.tsx`, `./ChatArtifactPanel.tsx` 원본이 없어 외부 컴포넌트 내부는 실 배포 코드 확인 후 보완 필요
