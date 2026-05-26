#!/usr/bin/env python3
"""
AADS Dashboard patch: 응답 버블 중복 + 스크롤 올라감 수정
4건 패치:
  P1: 폴링 merge 시 cooldown 재확인 (SSE done과 경합 방지)
  P2: auto-scroll >= → > (in-place 교체 시 불필요한 스크롤 방지)
  P3: 탭 포커스 refetch 시 cooldown 체크 추가
  P4: findLocalMatchForServerMessage에 execution_id 매칭 추가
"""
import sys

filepath = '/root/aads/aads-dashboard/src/app/chat/page.tsx'

with open(filepath, 'r') as f:
    content = f.read()

original = content
applied = []

# ── P1: 폴링 merge 시 cooldown 재확인 ──
old_p1 = """        setMessages((prev) => {
          const hasStoppedMsg = prev.some((m) => m.id.startsWith("stopped-"));
          if (hasStoppedMsg && !_waitingBg) return prev;
          // ★ FIX: active streaming 중 서버 메시지 병합 시 placeholder 중복 방지
          if (_streaming) return prev;
          return mergeServerMessagesPreservingLocal(prev, latest);
        });"""

new_p1 = """        setMessages((prev) => {
          if (Date.now() < mergeCooldownUntilRef.current) return prev;
          const hasStoppedMsg = prev.some((m) => m.id.startsWith("stopped-"));
          if (hasStoppedMsg && !_waitingBg) return prev;
          if (_streaming) return prev;
          return mergeServerMessagesPreservingLocal(prev, latest);
        });"""

if old_p1 in content:
    content = content.replace(old_p1, new_p1, 1)
    applied.append("P1: polling merge cooldown re-check")
else:
    print("WARNING: P1 target not found", file=sys.stderr)

# ── P2: auto-scroll >= → > ──
old_p2 = "const _grew = messages.length >= prevMessagesCountRef.current; prevMessagesCountRef.current = messages.length;"
new_p2 = "const _grew = messages.length > prevMessagesCountRef.current; prevMessagesCountRef.current = messages.length;"

if old_p2 in content:
    content = content.replace(old_p2, new_p2, 1)
    applied.append("P2: auto-scroll >= to >")
else:
    print("WARNING: P2 target not found", file=sys.stderr)

# ── P3: 탭 포커스 refetch 시 cooldown 체크 ──
old_p3 = """        const finalMsgs = processed.length > 0 ? processed : result.messages;
        setMessages((prev) =>
          prev.length > 0 ? mergeServerMessagesPreservingLocal(prev, finalMsgs) : finalMsgs
        );
      }).catch(() => {});"""

new_p3 = """        const finalMsgs = processed.length > 0 ? processed : result.messages;
        setMessages((prev) => {
          if (Date.now() < mergeCooldownUntilRef.current) return prev;
          return prev.length > 0 ? mergeServerMessagesPreservingLocal(prev, finalMsgs) : finalMsgs;
        });
      }).catch(() => {});"""

if old_p3 in content:
    content = content.replace(old_p3, new_p3, 1)
    applied.append("P3: tab refetch cooldown guard")
else:
    print("WARNING: P3 target not found", file=sys.stderr)

# ── P4: findLocalMatchForServerMessage에 execution_id 매칭 추가 ──
old_p4 = """function findLocalMatchForServerMessage(
  localMessages: ChatMessage[],
  serverMessage: ChatMessage,
  incomingMessages: ChatMessage[],
): ChatMessage | undefined {
  const serverContent = normalizedMessageContent(serverMessage);
  const exactContentMatch = localMessages.find((localMessage) =>
    localMessage.role === serverMessage.role &&
    normalizedMessageContent(localMessage) !== "" &&
    normalizedMessageContent(localMessage) === serverContent
  );
  if (exactContentMatch) return exactContentMatch;"""

new_p4 = """function findLocalMatchForServerMessage(
  localMessages: ChatMessage[],
  serverMessage: ChatMessage,
  incomingMessages: ChatMessage[],
): ChatMessage | undefined {
  if (serverMessage.execution_id) {
    const execMatch = localMessages.find((m) =>
      m.role === serverMessage.role && m.execution_id === serverMessage.execution_id
    );
    if (execMatch) return execMatch;
  }
  const serverContent = normalizedMessageContent(serverMessage);
  const exactContentMatch = localMessages.find((localMessage) =>
    localMessage.role === serverMessage.role &&
    normalizedMessageContent(localMessage) !== "" &&
    normalizedMessageContent(localMessage) === serverContent
  );
  if (exactContentMatch) return exactContentMatch;"""

if old_p4 in content:
    content = content.replace(old_p4, new_p4, 1)
    applied.append("P4: execution_id match in findLocalMatchForServerMessage")
else:
    print("WARNING: P4 target not found", file=sys.stderr)

if content == original:
    print("ERROR: No patches applied!", file=sys.stderr)
    sys.exit(1)

with open(filepath, 'w') as f:
    f.write(content)

print(f"Applied {len(applied)}/{4} patches:")
for p in applied:
    print(f"  ✅ {p}")
