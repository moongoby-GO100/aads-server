#!/usr/bin/env python3
"""P0-FIX: isAssistantDraftMessage — 200자 이상 실질 응답은 draft 아닌 것으로 분류"""
import shutil

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

OLD = '''function isAssistantDraftMessage(message: ChatMessage): boolean {
  if (message.role !== "assistant") return false;
  return (
    isStreamingPlaceholderMessage(message) ||
    message.intent === "interrupted_partial" ||
    message.intent === "interruption_notice" ||
    message.model_used === "interrupted" ||
    message.model_used === "recovered"
  );
}'''

NEW = '''function isAssistantDraftMessage(message: ChatMessage): boolean {
  if (message.role !== "assistant") return false;
  if (isStreamingPlaceholderMessage(message)) return true;
  const isInterruptedType =
    message.intent === "interrupted_partial" ||
    message.intent === "interruption_notice" ||
    message.model_used === "interrupted" ||
    message.model_used === "recovered";
  if (!isInterruptedType) return false;
  return (message.content || "").trim().length <= 200;
}'''

shutil.copy2(FILE, FILE + ".bak_draft_fix")
content = open(FILE, "r").read()
if OLD not in content:
    print("ERROR: OLD pattern not found")
    exit(1)
content = content.replace(OLD, NEW, 1)
open(FILE, "w").write(content)
print("OK: patched isAssistantDraftMessage")
