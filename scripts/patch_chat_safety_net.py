#!/usr/bin/env python3
"""Apply 3 patches to aads-dashboard/src/app/chat/page.tsx:
1. Completion Safety Net: streaming→false 후 placeholder 잔류 방지 + 스크롤 복구
2. SSE done handler: 완료 후 스크롤 강제 하단 이동
3. Polling just_completed: 완료 후 스크롤 강제 하단 이동
"""
import sys

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# ── Patch 1: Completion Safety Net ──
ANCHOR1 = "  }, [streaming]);\n\n  // FIX-4: 브리핑 렌더 후 재스크롤 (브리핑이 DOM에 추가되면 scrollHeight 변경됨)"
REPLACE1 = """  }, [streaming]);

  // ── SAFETY-NET: streaming→false 전환 후 placeholder 잔류 방지 + 스크롤 복구 ──
  const prevStreamingForSafetyRef = useRef(false);
  useEffect(() => {
    const wasStreaming = prevStreamingForSafetyRef.current;
    prevStreamingForSafetyRef.current = streaming;
    if (!wasStreaming || streaming) return;
    const sid = activeSessionRef.current;
    if (!sid) return;
    const timer = setTimeout(async () => {
      if (activeSessionRef.current !== sid || streamingRef.current) return;
      try {
        const msgs = await chatApi<ChatMessage[]>(
          `/chat/messages?session_id=${sid}&limit=50&sort=desc&include_streaming=true`
        );
        if (activeSessionRef.current !== sid || streamingRef.current) return;
        const processed = surfaceDbSavedStreamingPlaceholders(msgs, {}).reverse();
        const latestAi = processed.filter(
          (m: ChatMessage) => m.role === "assistant" && m.intent !== "streaming_placeholder" && m.intent !== "rate_limited"
        ).pop();
        if (latestAi) {
          setMessages(prev => {
            const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
            if (hasPlaceholder) return replaceStreamingPlaceholderWithFinal(prev, latestAi);
            const alreadyHas = prev.some(m => m.id === latestAi.id);
            if (alreadyHas) return prev;
            return mergeServerMessagesPreservingLocal(prev, processed);
          });
        }
      } catch { /* polling fallback */ }
      isNearBottomRef.current = true;
      requestAnimationFrame(() => {
        const c = messagesContainerRef.current;
        if (c) c.scrollTop = c.scrollHeight;
      });
    }, 2000);
    return () => clearTimeout(timer);
  }, [streaming]);

  // FIX-4: 브리핑 렌더 후 재스크롤 (브리핑이 DOM에 추가되면 scrollHeight 변경됨)"""

if ANCHOR1 in content:
    content = content.replace(ANCHOR1, REPLACE1, 1)
    print("✅ Patch 1 applied: Completion Safety Net")
else:
    print("❌ Patch 1 FAILED: anchor not found")
    sys.exit(1)

# ── Patch 2: SSE done handler — scroll fix ──
ANCHOR2 = """              mergeCooldownUntilRef.current = Date.now() + 5000;
              setStreamBuf("");
              setThinkingBuf("");
              setStreaming(false);
            } else if (ev.type === "tool_use" && ev.tool_name) {"""
REPLACE2 = """              mergeCooldownUntilRef.current = Date.now() + 5000;
              setStreamBuf("");
              setThinkingBuf("");
              setStreaming(false);
              isNearBottomRef.current = true;
              requestAnimationFrame(() => { const c = messagesContainerRef.current; if (c) c.scrollTop = c.scrollHeight; });
            } else if (ev.type === "tool_use" && ev.tool_name) {"""

if ANCHOR2 in content:
    content = content.replace(ANCHOR2, REPLACE2, 1)
    print("✅ Patch 2 applied: SSE done scroll fix")
else:
    print("❌ Patch 2 FAILED: anchor not found")
    sys.exit(1)

# ── Patch 3: Polling just_completed — scroll fix ──
ANCHOR3 = """          setStreaming(false); setStreamBuf("");
          // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
          // freshMsgs는 ASC(시간순) → .slice().reverse()로 DESC(최신순) 후 최신 user/ai 기준 판단"""
REPLACE3 = """          setStreaming(false); setStreamBuf("");
          isNearBottomRef.current = true;
          requestAnimationFrame(() => { const c = messagesContainerRef.current; if (c) c.scrollTop = c.scrollHeight; });
          // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
          // freshMsgs는 ASC(시간순) → .slice().reverse()로 DESC(최신순) 후 최신 user/ai 기준 판단"""

if ANCHOR3 in content:
    content = content.replace(ANCHOR3, REPLACE3, 1)
    print("✅ Patch 3 applied: Polling just_completed scroll fix")
else:
    print("❌ Patch 3 FAILED: anchor not found")
    sys.exit(1)

# ── Patch 4: SSE disconnect handler — scroll fix ──
ANCHOR4 = """          setStreaming(false); setStreamBuf("");
          return;
        }
        // 서버에서 스트리밍 아님 + waitingBg=true → 강제 해제 (placeholder 삭제 등으로 stuck 방지)"""
REPLACE4 = """          setStreaming(false); setStreamBuf("");
          isNearBottomRef.current = true;
          requestAnimationFrame(() => { const c = messagesContainerRef.current; if (c) c.scrollTop = c.scrollHeight; });
          return;
        }
        // 서버에서 스트리밍 아님 + waitingBg=true → 강제 해제 (placeholder 삭제 등으로 stuck 방지)"""

if ANCHOR4 in content:
    content = content.replace(ANCHOR4, REPLACE4, 1)
    print("✅ Patch 4 applied: SSE disconnect scroll fix")
else:
    print("❌ Patch 4 FAILED: anchor not found")
    sys.exit(1)

# ── Patch 5: Execution replay done — scroll fix ──
ANCHOR5 = """              setToolStatus(null);
              setToolLogs([]);
              return;
            }
          } catch {
            // ignore malformed chunks"""
REPLACE5 = """              setToolStatus(null);
              setToolLogs([]);
              isNearBottomRef.current = true;
              requestAnimationFrame(() => { const c = messagesContainerRef.current; if (c) c.scrollTop = c.scrollHeight; });
              return;
            }
          } catch {
            // ignore malformed chunks"""

if ANCHOR5 in content:
    content = content.replace(ANCHOR5, REPLACE5, 1)
    print("✅ Patch 5 applied: Execution replay done scroll fix")
else:
    print("⚠️ Patch 5 skipped: anchor not found (non-critical)")

# ── Patch 6: Polling waitingBg final AI message — scroll fix ──
ANCHOR6 = """                requestAnimationFrame(() => { const c = messagesContainerRef.current; if (c && isNearBottomRef.current) c.scrollTop = c.scrollHeight; });
              }
            } catch { /* 재조회 실패 무시 */ }"""
REPLACE6 = """                isNearBottomRef.current = true;
                requestAnimationFrame(() => { const c = messagesContainerRef.current; if (c) c.scrollTop = c.scrollHeight; });
              }
            } catch { /* 재조회 실패 무시 */ }"""

if ANCHOR6 in content:
    content = content.replace(ANCHOR6, REPLACE6, 1)
    print("✅ Patch 6 applied: Polling waitingBg scroll fix")
else:
    print("⚠️ Patch 6 skipped: anchor not found (non-critical)")

if content == original:
    print("❌ No changes made!")
    sys.exit(1)

with open(FILE, "w", encoding="utf-8") as f:
    f.write(content)

print(f"\n✅ All patches applied. File: {FILE}")
print(f"   Lines: {content.count(chr(10)) + 1}")
