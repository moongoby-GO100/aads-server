#!/usr/bin/env python3
"""Fix duplicate response bubble rendering in chat page.tsx — AADS-FIX-DEDUP"""
import sys

TARGET = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(TARGET, "r") as f:
    code = f.read()

# ── Patch 1: replaceStreamingPlaceholderWithFinal — 중복 append 차단 ──
OLD1 = """  if (replaced) return next;
  return [
    ...prev.filter((message) => !message.id.startsWith("ai-partial-")),
    messageWithStableRenderId,
  ].sort((a, b) => messageTime(a) - messageTime(b));
}"""

NEW1 = """  if (replaced) return next;
  // ★ DEDUP: placeholder 미존재 시 동일 메시지가 이미 있으면 append 차단 (다중 핸들러 경합 방지)
  const _fc = (finalMessage.content || "").trim();
  const _dupExists = prev.some((m) =>
    m.role === "assistant" &&
    !isStreamingPlaceholderMessage(m) && (
      (finalMessage.execution_id && m.execution_id === finalMessage.execution_id && (m.content || "").trim()) ||
      (_fc.length >= 20 && (m.content || "").trim() === _fc)
    )
  );
  if (_dupExists) return prev.filter((message) => !message.id.startsWith("ai-partial-"));
  return [
    ...prev.filter((message) => !message.id.startsWith("ai-partial-")),
    messageWithStableRenderId,
  ].sort((a, b) => messageTime(a) - messageTime(b));
}"""

# ── Patch 2: mergeServerMessagesPreservingLocal — content/execution_id 기반 최종 dedup ──
OLD2 = """  // ★ DEDUP: streaming_placeholder 최대 1개만 유지 (마지막 것 우선)
  let seenPh = false;
  for (let i = merged.length - 1; i >= 0; i--) {
    if (merged[i].intent === "streaming_placeholder" || isStreamingPlaceholderMessage(merged[i])) {
      if (seenPh) { merged.splice(i, 1); } else { seenPh = true; }
    }
  }
  return merged;
}"""

NEW2 = """  // ★ DEDUP: streaming_placeholder 최대 1개만 유지 (마지막 것 우선)
  let seenPh = false;
  for (let i = merged.length - 1; i >= 0; i--) {
    if (merged[i].intent === "streaming_placeholder" || isStreamingPlaceholderMessage(merged[i])) {
      if (seenPh) { merged.splice(i, 1); } else { seenPh = true; }
    }
  }
  // ★ DEDUP: assistant 최종 메시지 중복 제거 — execution_id 또는 content 앞 300자 기준
  const _seenExec = new Set<string>();
  const _seenCont = new Set<string>();
  for (let i = 0; i < merged.length; i++) {
    const _m = merged[i];
    if (_m.role !== "assistant" || isStreamingPlaceholderMessage(_m)) continue;
    const _ct = (_m.content || "").trim();
    if (!_ct) continue;
    let _isDup = false;
    if (_m.execution_id) {
      if (_seenExec.has(_m.execution_id)) _isDup = true;
      else _seenExec.add(_m.execution_id);
    }
    if (!_isDup && _ct.length >= 20) {
      const _ck = _ct.slice(0, 300);
      if (_seenCont.has(_ck)) _isDup = true;
      else _seenCont.add(_ck);
    }
    if (_isDup) { merged.splice(i, 1); i--; }
  }
  return merged;
}"""

patched = code
count1 = patched.count(OLD1)
if count1 != 1:
    print(f"FAIL: Patch1 target found {count1} times (expected 1)")
    sys.exit(1)
patched = patched.replace(OLD1, NEW1, 1)

count2 = patched.count(OLD2)
if count2 != 1:
    print(f"FAIL: Patch2 target found {count2} times (expected 1)")
    sys.exit(1)
patched = patched.replace(OLD2, NEW2, 1)

with open(TARGET, "w") as f:
    f.write(patched)

print(f"OK: 2 patches applied to {TARGET}")
print(f"  Patch1: replaceStreamingPlaceholderWithFinal dedup guard")
print(f"  Patch2: mergeServerMessagesPreservingLocal content/exec dedup")
