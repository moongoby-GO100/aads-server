#!/usr/bin/env python3
"""Optimistic UI: show user msg + AI bubble before session creation."""
import shutil, sys

F = "/root/aads/aads-dashboard/src/app/chat/page.tsx"
shutil.copy2(F, F + ".bak_opt")
lines = open(F, encoding="utf-8").readlines()
n = len(lines)
BT = chr(96)
NL = chr(10)
HG = chr(9203)

def L(*parts):
    return "".join(str(p) for p in parts) + NL

# P1: Find "if (!sessionId) {" near "Auto-create"
p1 = None
for i in range(n):
    if lines[i].strip() == "if (!sessionId) {":
        ctx = "".join(lines[max(0, i - 5):i])
        if "Auto-create" in ctx:
            p1 = i
            break
if p1 is None:
    print("P1 FAIL"); sys.exit(1)

d, e1 = 0, p1
for j in range(p1, min(p1 + 20, n)):
    d += lines[j].count("{") - lines[j].count("}")
    if d == 0 and j > p1:
        e1 = j; break

print("P1: lines %d-%d" % (p1 + 1, e1 + 1))

new1 = [
    L("    let _optimisticPending: string | null = null;"),
    L("    if (!sessionId) {"),
    L("      if (!activeWsRef.current) return;"),
    L("      setStreaming(true);"),
    L("      _optimisticPending = ", BT, "pending-${Date.now()}", BT, ";"),
    L("      const _optPhId = ", BT, "ai-streaming-${_optimisticPending}", BT, ";"),
    L("      if (!_existingMsgId) {"),
    L("        setMessages(prev => [...prev,"),
    L('          { id: ', BT, 'tmp-${Date.now()}', BT, ', session_id: _optimisticPending!, role: "user" as const, content, created_at: new Date().toISOString() },'),
    L('          { id: _optPhId, session_id: _optimisticPending!, role: "assistant" as const, content: "', HG, ' 세션 생성 중...", intent: "streaming_placeholder", created_at: new Date(Date.now() + 1).toISOString() }'),
    L("        ]);"),
    L("      } else {"),
    L("        setMessages(prev => [...prev,"),
    L('          { id: _optPhId, session_id: _optimisticPending!, role: "assistant" as const, content: "', HG, ' 세션 생성 중...", intent: "streaming_placeholder", created_at: new Date(Date.now() + 1).toISOString() }'),
    L("        ]);"),
    L("      }"),
    L("      const s = await createSession();"),
    L("      if (!s) { setStreaming(false); setMessages(prev => prev.filter(m => m.session_id !== _optimisticPending)); return; }"),
    L("      sessionId = s.id;"),
    L("      setMessages(prev => prev.map(m => m.session_id === _optimisticPending"),
    L('        ? { ...m, session_id: sessionId!, ...(m.role === "assistant" ? { id: ', BT, 'ai-streaming-${sessionId}', BT, ' } : {}) }'),
    L("        : m"),
    L("      ));"),
    L("    }"),
]

lines[p1:e1 + 1] = new1
print("P1 done: %d -> %d lines" % (e1 - p1 + 1, len(new1)))

# P2: Find the marker comment
p2 = None
for i in range(len(lines)):
    if "FIX: user" in lines[i] and "placeholder" in lines[i] and "setMessages" in lines[i]:
        p2 = i
        break
if p2 is None:
    print("P2 FAIL"); sys.exit(1)

if_start = p2 + 1
d, e2 = 0, if_start
for j in range(if_start, min(if_start + 30, len(lines))):
    d += lines[j].count("{") - lines[j].count("}")
    if d == 0 and j > if_start:
        e2 = j; break

print("P2: lines %d-%d" % (p2 + 1, e2 + 1))

orig_block = lines[if_start:e2 + 1]

new2 = [lines[p2]]
new2.extend([
    L("    if (_optimisticPending) {"),
    L("      setMessages(prev => freezeStreamingPlaceholders(prev, streamBufRef.current || bgPartialContent).map(m => {"),
    L('        if (m.session_id === sessionId && m.role === "user" && m.id.startsWith("tmp-")) return { ...userMsg };'),
    L("        if (m.id === streamingPlaceholderId) return { ...m };"),
    L("        return m;"),
    L("      }));"),
])
for k, ol in enumerate(orig_block):
    if k == 0:
        new2.append(ol.replace("if (!", "} else if (!"))
    else:
        new2.append(ol)

lines[p2:e2 + 1] = new2
print("P2 done: %d -> %d lines" % (e2 - p2 + 1, len(new2)))

open(F, "w", encoding="utf-8").writelines(lines)
print("SUCCESS: %d -> %d lines" % (n, len(lines)))
