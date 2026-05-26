#!/usr/bin/env python3
"""P0 브라우저 멈춤 수정: React.memo 안정화 패치"""
import re

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    code = f.read()

original = code  # backup comparison

# ────────────────────────────────────────────────
# PATCH 1: Add stable useCallback definitions after activeArtifact
# ────────────────────────────────────────────────
anchor1 = "  const activeArtifact = filteredArtifacts[selectedArtifactIdx] || filteredArtifacts[0] || null;"
insert1 = """

  // PERF: React.memo 안정화 — 인라인 화살표 함수 제거로 MessageItem 불필요 재렌더 방지
  const filteredArtifactsRef = useRef(filteredArtifacts);
  useEffect(() => { filteredArtifactsRef.current = filteredArtifacts; }, [filteredArtifacts]);
  const handleViewArtifactStable = useCallback((artifactId: string) => {
    const idx = filteredArtifactsRef.current.findIndex((a: { id: string }) => a.id === artifactId);
    if (idx >= 0) setSelectedArtifactIdx(idx);
    setArtifactMode("full");
    setArtifactTab("report");
  }, []);
  const handleOpenLightboxStable = useCallback((srcs: string[], i: number) => {
    setLightboxSrcs(srcs);
    setLightboxIdx(i);
  }, []);
  const handleViewReportStable = useCallback(() => {
    setArtifactMode("full");
    setArtifactTab("report");
  }, []);"""

if anchor1 in code:
    code = code.replace(anchor1, anchor1 + insert1, 1)
    print("PATCH 1 OK: stable callbacks added")
else:
    print("PATCH 1 FAIL: anchor not found")

# ────────────────────────────────────────────────
# PATCH 2: MessageItemProps — allMessages → replyTarget
# ────────────────────────────────────────────────
old2 = "  allMessages?: ChatMessage[];"
new2 = "  replyTarget?: ChatMessage | null;"
if old2 in code:
    code = code.replace(old2, new2, 1)
    print("PATCH 2 OK: interface updated")
else:
    print("PATCH 2 FAIL: interface not found")

# ────────────────────────────────────────────────
# PATCH 3: MessageItem destructuring — allMessages → replyTarget
# ────────────────────────────────────────────────
old3 = "onRegenerate, onReplyTo, onBranch, allMessages,"
new3 = "onRegenerate, onReplyTo, onBranch, replyTarget,"
if old3 in code:
    code = code.replace(old3, new3, 1)
    print("PATCH 3 OK: destructuring updated")
else:
    print("PATCH 3 FAIL: destructuring not found")

# ────────────────────────────────────────────────
# PATCH 4: MessageItem body — remove allMessages.find, use replyTarget directly
# ────────────────────────────────────────────────
old4 = """  const replyTarget = msg.reply_to_id && allMessages
    ? allMessages.find((m) => m.id === msg.reply_to_id)
    : null;"""
new4 = """  const replyMsg = replyTarget || null;"""
if old4 in code:
    # Also replace all usages of replyTarget below in MessageItem with replyMsg
    code = code.replace(old4, new4, 1)
    print("PATCH 4 OK: replyTarget lookup removed")
else:
    print("PATCH 4 FAIL: replyTarget lookup not found")

# Now we need to update references to `replyTarget` within MessageItem.
# Since we renamed the variable to replyMsg, we need to find usages after the declaration.
# Let's find the MessageItem function body and replace `replyTarget` references with `replyMsg`
# But we need to be careful not to replace the prop name `replyTarget` in JSX.

# Actually, let's check what references to `replyTarget` exist in the MessageItem component:
# The variable was `replyTarget` (from allMessages.find). Now it's `replyMsg`.
# We need to find usages of `replyTarget` that refer to this local variable (not the prop).

# Let's find all occurrences of `replyTarget` after PATCH 4's location
patch4_pos = code.find("const replyMsg = replyTarget || null;")
if patch4_pos >= 0:
    before = code[:patch4_pos + len("const replyMsg = replyTarget || null;")]
    after = code[patch4_pos + len("const replyMsg = replyTarget || null;"):]
    
    # Find end of MessageItem component (it's defined with memo, ends before the next top-level const/function)
    # The MessageItem component ends at the closing of memo(). Let's find the return statement's closing.
    # Actually, let's just replace `replyTarget` with `replyMsg` in the next ~200 lines (within MessageItem body)
    # But we need to NOT replace the prop name in JSX like `replyTarget={...}`
    
    # Find the end of MessageItem - it ends before the next top-level component/function definition
    # Look for the line that starts with "});" which closes memo()
    memo_close = after.find("\n});\n")
    if memo_close >= 0:
        mi_body = after[:memo_close]
        mi_rest = after[memo_close:]
        # Replace variable references (not prop references in JSX)
        # Variable usage patterns: {replyTarget && , {replyTarget. , (replyTarget) , replyTarget?. etc.
        # Prop patterns: replyTarget={  or  replyTarget?:  (in type annotations)
        # Let's replace all `replyTarget` with `replyMsg` in the MessageItem body
        # EXCEPT for `replyTarget={` (JSX prop assignment) and `replyTarget?:` (type)
        mi_body_new = mi_body.replace("replyTarget", "replyMsg")
        # But we must NOT have replaced any prop assignments... Actually there shouldn't be any
        # `replyTarget={` assignments inside MessageItem body since it's the component definition.
        code = before + mi_body_new + mi_rest
        print("PATCH 4b OK: replyTarget → replyMsg references updated in MessageItem")
    else:
        print("PATCH 4b SKIP: couldn't find MessageItem end")

# ────────────────────────────────────────────────
# PATCH 5: First MessageItem render — replace allMessages + inline arrows
# ────────────────────────────────────────────────
# Replace allMessages={messages} with replyTarget={...}
old5a = "                    allMessages={messages}\n"
new5a = "                    replyTarget={msg.reply_to_id ? messages.find(m => m.id === msg.reply_to_id) : null}\n"
# This appears twice (line 6966 and 7022), replace both
count5a = code.count(old5a)
if count5a >= 1:
    code = code.replace(old5a, new5a)
    print(f"PATCH 5a OK: allMessages→replyTarget in {count5a} render locations")
else:
    print("PATCH 5a FAIL: allMessages={messages} not found")

# Replace first onViewArtifact inline arrow (main MessageItem)
old5b = """                    onViewArtifact={(artifactId) => {
                      const idx = filteredArtifacts.findIndex(a => a.id === artifactId);
                      if (idx >= 0) setSelectedArtifactIdx(idx);
                      setArtifactMode("full");
                      setArtifactTab("report");
                    }}"""
new5b = "                    onViewArtifact={handleViewArtifactStable}"
count5b = code.count(old5b)
if count5b >= 1:
    code = code.replace(old5b, new5b)
    print(f"PATCH 5b OK: onViewArtifact inline → stable ({count5b} locations)")
else:
    print("PATCH 5b FAIL: onViewArtifact inline not found")

# Replace onOpenLightbox inline arrow
old5c = '                    onOpenLightbox={(srcs, i) => { setLightboxSrcs(srcs); setLightboxIdx(i); }}'
new5c = '                    onOpenLightbox={handleOpenLightboxStable}'
count5c = code.count(old5c)
if count5c >= 1:
    code = code.replace(old5c, new5c)
    print(f"PATCH 5c OK: onOpenLightbox inline → stable ({count5c} locations)")
else:
    print("PATCH 5c FAIL: onOpenLightbox inline not found")

# Replace onViewReport inline arrow (main location)
old5d = """                    onViewReport={msg.intent === "pipeline_runner" ? () => { setArtifactMode("full"); setArtifactTab("report"); } : undefined}"""
new5d = '                    onViewReport={msg.intent === "pipeline_runner" ? handleViewReportStable : undefined}'
count5d = code.count(old5d)
if count5d >= 1:
    code = code.replace(old5d, new5d)
    print(f"PATCH 5d OK: onViewReport inline → stable ({count5d} locations)")
else:
    print("PATCH 5d FAIL: onViewReport inline not found")

# Replace onViewReport for hidden messages (hm variable)
old5e = """                      onViewReport={hm.intent === "pipeline_runner" ? () => { setArtifactMode("full"); setArtifactTab("report"); } : undefined}"""
new5e = '                      onViewReport={hm.intent === "pipeline_runner" ? handleViewReportStable : undefined}'
if old5e in code:
    code = code.replace(old5e, new5e, 1)
    print("PATCH 5e OK: hidden msg onViewReport → stable")
else:
    print("PATCH 5e FAIL: hidden msg onViewReport not found")

# Replace hidden messages onViewArtifact inline
old5f = """                      onViewArtifact={(artifactId) => {
                        const idx = filteredArtifacts.findIndex(a => a.id === artifactId);
                        if (idx >= 0) setSelectedArtifactIdx(idx);
                        setArtifactMode("full");
                        setArtifactTab("report");
                      }}"""
new5f = "                      onViewArtifact={handleViewArtifactStable}"
if old5f in code:
    code = code.replace(old5f, new5f, 1)
    print("PATCH 5f OK: hidden msg onViewArtifact → stable")
else:
    print("PATCH 5f FAIL: hidden msg onViewArtifact not found")

# Replace hidden messages onOpenLightbox
old5g = '                      onOpenLightbox={(srcs, i) => { setLightboxSrcs(srcs); setLightboxIdx(i); }}'
new5g = '                      onOpenLightbox={handleOpenLightboxStable}'
if old5g in code:
    code = code.replace(old5g, new5g, 1)
    print("PATCH 5g OK: hidden msg onOpenLightbox → stable")
else:
    print("PATCH 5g FAIL: hidden msg onOpenLightbox not found")

# ────────────────────────────────────────────────
# PATCH 6: Increase sync timer from 2s to 4s (reduce re-renders during streaming)
# ────────────────────────────────────────────────
old6 = "  // PERSIST-FIX: streaming 중 2초마다 streamBuf를 message.content에 동기화"
new6 = "  // PERSIST-FIX: streaming 중 4초마다 streamBuf를 message.content에 동기화 (PERF: 2s→4s)"
if old6 in code:
    code = code.replace(old6, new6, 1)
    print("PATCH 6a OK: sync comment updated")

# The actual timer interval
old6b = """    const syncTimer = setInterval(() => {
      const buf = streamBufRef.current;
      const thinking = thinkingBufRef.current;
      if (!buf && !thinking) return;
      setMessages(prev => {
        const ph = prev.find(m => m.intent === "streaming_placeholder");
        if (!ph || (ph.content === buf && (ph.thinking_summary || "") === thinking)) return prev;
        return prev.map(m =>
          m.intent === "streaming_placeholder" ? { ...m, content: buf || m.content, thinking_summary: thinking || m.thinking_summary } : m
        );
      });
    }, 2000);"""
new6b = """    const syncTimer = setInterval(() => {
      const buf = streamBufRef.current;
      const thinking = thinkingBufRef.current;
      if (!buf && !thinking) return;
      setMessages(prev => {
        const ph = prev.find(m => m.intent === "streaming_placeholder");
        if (!ph || (ph.content === buf && (ph.thinking_summary || "") === thinking)) return prev;
        return prev.map(m =>
          m.intent === "streaming_placeholder" ? { ...m, content: buf || m.content, thinking_summary: thinking || m.thinking_summary } : m
        );
      });
    }, 4000);"""
if old6b in code:
    code = code.replace(old6b, new6b, 1)
    print("PATCH 6b OK: sync timer 2000→4000ms")
else:
    print("PATCH 6b FAIL: sync timer not found")

# ────────────────────────────────────────────────
# Verify and write
# ────────────────────────────────────────────────
if code == original:
    print("\nERROR: No changes made!")
else:
    # Backup
    with open(FILE + ".bak_freeze_fix", "w") as f:
        f.write(original)
    with open(FILE, "w") as f:
        f.write(code)
    
    changes = sum(1 for a, b in zip(original.split('\n'), code.split('\n')) if a != b)
    new_lines = len(code.split('\n')) - len(original.split('\n'))
    print(f"\nDONE: {changes} lines changed, {new_lines} lines added")
    print(f"Backup: {FILE}.bak_freeze_fix")
