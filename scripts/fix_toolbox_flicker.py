#!/usr/bin/env python3
"""P0-P2 fixes: chat toolbox flicker + bubble duplication + unnecessary re-renders"""
import shutil, sys

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r", encoding="utf-8") as f:
    src = f.read()

orig = src
applied = []
skipped = []

def patch(label, old, new):
    global src
    if old not in src:
        skipped.append(label)
        print(f"SKIP {label}")
        return
    src = src.replace(old, new, 1)
    applied.append(label)
    print(f"OK   {label}")

# ── P0-1a: Add toolsOpen state for controlled <details> ──
patch("P0-1a-toolsOpen-state",
    "  // P1: 긴 보고서 접이식 상태\n  const [contentCollapsed, setContentCollapsed] = useState(",
    "  const [toolsOpen, setToolsOpen] = useState(false);\n\n  // P1: 긴 보고서 접이식 상태\n  const [contentCollapsed, setContentCollapsed] = useState("
)

# ── P0-1b: <details> controlled open for tool box (18-space indent = tool box) ──
patch("P0-1b-details-controlled",
    "                  <details style={{marginBottom: '8px'}}>",
    "                  <details open={toolsOpen} onToggle={(e) => setToolsOpen((e.target as HTMLDetailsElement).open)} style={{marginBottom: '8px'}}>"
)

# ── P0-2a: hasToolSummary must also check streamingContent ──
patch("P0-2a-hasToolSummary",
    'const hasToolSummary = msg.role === "assistant" && !isActiveStreaming && messageHasToolSummary(msg);',
    'const hasToolSummary = msg.role === "assistant" && !isActiveStreaming && !streamingContent && messageHasToolSummary(msg);'
)

# ── P0-2b: render branch uses streamingContent as fallback during transition ──
patch("P0-2b-render-branch",
    "          ) : isActiveStreaming ? (",
    "          ) : (isActiveStreaming || Boolean(streamingContent)) ? ("
)

# ── P1-1a: stable placeholder ID in preservePartialAndContinueStreaming ──
patch("P1-1a-stable-id-preserve",
    "          id: `ai-streaming-${Date.now()}`,",
    "          id: `ai-streaming-${continuationSessionId}`,"
)

# ── P1-1b: stable placeholder ID in sendMessage ──
patch("P1-1b-stable-id-send",
    "    const streamingPlaceholderId = `ai-streaming-${Date.now()}`;",
    "    const streamingPlaceholderId = `ai-streaming-${sessionId}`;"
)

# ── P1-2: PERSIST-FIX conditional update — skip if content unchanged ──
patch("P1-2-persist-conditional",
    "    const syncTimer = setInterval(() => {\n"
    "      const buf = streamBufRef.current;\n"
    "      if (!buf) return;\n"
    "      setMessages(prev => prev.map(m =>\n"
    "        m.intent === \"streaming_placeholder\" ? { ...m, content: buf } : m\n"
    "      ));\n"
    "    }, 2000);",
    "    const syncTimer = setInterval(() => {\n"
    "      const buf = streamBufRef.current;\n"
    "      if (!buf) return;\n"
    "      setMessages(prev => {\n"
    "        const ph = prev.find(m => m.intent === \"streaming_placeholder\");\n"
    "        if (!ph || ph.content === buf) return prev;\n"
    "        return prev.map(m =>\n"
    "          m.intent === \"streaming_placeholder\" ? { ...m, content: buf } : m\n"
    "        );\n"
    "      });\n"
    "    }, 2000);"
)

# ── P2-1: memo deep comparison for streamToolLogs ──
patch("P2-1-memo-toolLogs",
    "  prev.streamToolStatus === next.streamToolStatus &&\n  prev.streamToolLogs === next.streamToolLogs",
    "  prev.streamToolStatus === next.streamToolStatus &&\n  prev.streamToolLogs?.length === next.streamToolLogs?.length &&\n  prev.streamToolLogs?.[prev.streamToolLogs.length - 1]?.text === next.streamToolLogs?.[next.streamToolLogs.length - 1]?.text"
)

print(f"\n{'='*50}")
if applied:
    shutil.copy2(FILE, FILE + ".bak_toolbox_fix")
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"SUCCESS: {len(applied)}/8 patches applied, {len(skipped)} skipped")
    for a in applied:
        print(f"  ✅ {a}")
    for s in skipped:
        print(f"  ⚠️  {s} (already applied or pattern changed)")
else:
    print("ERROR: No patches could be applied")
    sys.exit(1)
