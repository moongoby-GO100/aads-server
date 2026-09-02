#!/usr/bin/env python3
# AADS-BUBBLE-FLASH-P0: 최종 완료 시 응답 버블이 사라졌다 다시 생성되는 현상 수정
import io, sys

P = "/root/aads/aads-dashboard/src/app/chat/page.tsx"
src = io.open(P, encoding="utf-8").read()
orig = src
applied = []

# P0-1: finalizeAssistantMessage render_id fallback 순서 (서버 UUID 승격 차단)
old1 = "      : (existingMessage.render_id || finalRenderId || existingMessage.id || finalMessage.id),"
new1 = (
    "      // AADS-BUBBLE-FLASH-P0: server UUID promotion changes React key -> bubble remount\n"
    "      : (existingMessage.render_id || existingMessage.id || finalRenderId || finalMessage.id),"
)
if old1 in src and src.count(old1) == 1:
    src = src.replace(old1, new1)
    applied.append("P0-1 render_id fallback order")

# P0-2: placeholder 생성 지점에 render_id 부여 (초기 React key 고정)
old2 = "{ id: streamingPlaceholderId, session_id: sessionId!, role: \"assistant\" as const,"
cnt2 = src.count(old2)
if cnt2:
    src = src.replace(old2, "{ id: streamingPlaceholderId, render_id: streamingPlaceholderId, session_id: sessionId!, role: \"assistant\" as const,")
    applied.append("P0-2 placeholder render_id x%d" % cnt2)

# P0-3: currentPlaceholder 재사용 경로 render_id 고정
old3 = "? { ...currentPlaceholder, session_id: sessionId!,"
cnt3 = src.count(old3)
if cnt3:
    src = src.replace(old3, "? { ...currentPlaceholder, render_id: currentPlaceholder.render_id || currentPlaceholder.id, session_id: sessionId!,")
    applied.append("P0-3 currentPlaceholder render_id x%d" % cnt3)

# P0-4: 낙관적 세션 생성 placeholder render_id
old4 = "{ id: _optPhId, session_id: _optimisticPending!, role: \"assistant\" as const,"
cnt4 = src.count(old4)
if cnt4:
    src = src.replace(old4, "{ id: _optPhId, render_id: _optPhId, session_id: _optimisticPending!, role: \"assistant\" as const,")
    applied.append("P0-4 optimistic placeholder render_id x%d" % cnt4)

# P1-1: 스트리밍 tail 렌더 한도 상향 (완료 전환 시 앞부분 급출현 점프 완화)
old5 = "  const LIVE_STREAM_RENDER_LIMIT = 3000;"
if old5 in src and src.count(old5) == 1:
    src = src.replace(old5, "  const LIVE_STREAM_RENDER_LIMIT = 8000;  // AADS-BUBBLE-FLASH-P1")
    applied.append("P1-1 LIVE_STREAM_RENDER_LIMIT 3000->8000")

if src == orig:
    print("NO_CHANGE")
    sys.exit(1)
io.open(P, "w", encoding="utf-8").write(src)
print("APPLIED: " + " | ".join(applied))
