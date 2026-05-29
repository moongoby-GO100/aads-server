#!/usr/bin/env python3
"""
fix(chat): preserve interruption_notice bubbles with substantial content
- eitherInterrupted: 200자 미만 interrupted 메시지만 그룹핑 대상으로 제한
- keeperPriority: 500자 이상 interrupted 메시지는 priority 1 부여
"""
import sys

TARGET = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

OLD1 = '          const eitherInterrupted = (msg.intent === "interrupted_partial" || msg.intent === "interruption_notice" || next.intent === "interrupted_partial" || next.intent === "interruption_notice");'
NEW1 = '          const eitherInterrupted = ((msg.intent === "interrupted_partial" || msg.intent === "interruption_notice") && (msg.content || "").length < 200) || ((next.intent === "interrupted_partial" || next.intent === "interruption_notice") && (next.content || "").length < 200);'

OLD2 = """          const keeperPriority = (item: ChatMessage) => {
            if (item.intent !== "streaming_placeholder" && !isLocalTransientMessage(item) && item.model_used !== "interrupted") return 2;
            if (item.model_used === "recovered") return 1;
            if ((item.intent === "interrupted_partial" || item.intent === "interruption_notice") && (item.content || "").length < 200) return -1;
            return 0;
          };"""
NEW2 = """          const keeperPriority = (item: ChatMessage) => {
            if (item.intent !== "streaming_placeholder" && !isLocalTransientMessage(item) && item.model_used !== "interrupted") return 2;
            if (item.model_used === "recovered") return 1;
            if ((item.intent === "interrupted_partial" || item.intent === "interruption_notice") && (item.content || "").length >= 500) return 1;
            if ((item.intent === "interrupted_partial" || item.intent === "interruption_notice") && (item.content || "").length < 200) return -1;
            return 0;
          };"""

with open(TARGET, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# Patch 1
if OLD1 not in content:
    print("ERROR: OLD1 not found in file")
    sys.exit(1)
count1 = content.count(OLD1)
if count1 != 1:
    print(f"ERROR: OLD1 found {count1} times (expected 1)")
    sys.exit(1)
content = content.replace(OLD1, NEW1)
print(f"PATCH 1 applied: eitherInterrupted fix")

# Patch 2
if OLD2 not in content:
    print("ERROR: OLD2 not found in file")
    sys.exit(1)
count2 = content.count(OLD2)
if count2 != 1:
    print(f"ERROR: OLD2 found {count2} times (expected 1)")
    sys.exit(1)
content = content.replace(OLD2, NEW2)
print(f"PATCH 2 applied: keeperPriority fix")

# Backup
with open(TARGET + ".bak_interruption_fix", "w", encoding="utf-8") as f:
    f.write(original)

# Write
with open(TARGET, "w", encoding="utf-8") as f:
    f.write(content)

print("SUCCESS: Both patches applied and file saved.")
print(f"Backup saved to {TARGET}.bak_interruption_fix")
