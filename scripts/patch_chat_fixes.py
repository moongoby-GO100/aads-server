#!/usr/bin/env python3
"""P0 패치: 응답 버블 중복 + 스크롤 올라감 + 응답 미표시 수정"""
import sys

TARGET = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(TARGET, "r") as f:
    lines = f.readlines()

original_count = len(lines)
changes = []

# === Patch 1: SSE done empty-full → mergeLatestAssistantFromServer 호출 ===
# Line 4483 (0-indexed 4482): "              }" → else block
# Line 4485 (0-indexed 4484): 5000 → 8000
for i in range(len(lines)):
    if (i >= 4480 and i <= 4486 and
        lines[i].rstrip() == "              }" and
        i + 1 < len(lines) and "P0-FIX: setMessages" in lines[i + 1]):
        lines[i] = "              } else {\n                // P0-FIX: tools-only → DB에서 최종 메시지 fetch\n                mergeLatestAssistantFromServer(requestSessionId!).catch(() => {});\n              }\n"
        changes.append(f"Patch1: line {i+1} - added else branch for empty full")
        break

for i in range(len(lines)):
    if (i >= 4484 and i <= 4492 and
        "mergeCooldownUntilRef.current = Date.now() + 5000;" in lines[i] and
        i > 0 and "P0-FIX" in lines[i - 1]):
        lines[i] = lines[i].replace("Date.now() + 5000", "Date.now() + 8000")
        changes.append(f"Patch1b: line {i+1} - cooldown 5s→8s")
        break

# === Patch 2: Safety-net cooldown check ===
# Line 3426 (0-indexed 3425): after this line, insert cooldown check
for i in range(len(lines)):
    if (i >= 3424 and i <= 3430 and
        "if (activeSessionRef.current !== sid || streamingRef.current) return;" in lines[i] and
        i > 0 and "setTimeout" in lines[i - 1]):
        indent = "      "
        lines[i] = lines[i] + indent + "if (Date.now() < mergeCooldownUntilRef.current) return;\n"
        changes.append(f"Patch2: line {i+1} - safety-net cooldown guard")
        break

# === Patch 2b: Safety-net dedup by execution_id ===
for i in range(len(lines)):
    if (i >= 3438 and i <= 3448 and
        "const alreadyHas = prev.some(m => m.id === latestAi.id);" in lines[i]):
        lines[i] = lines[i].replace(
            "const alreadyHas = prev.some(m => m.id === latestAi.id);",
            "const alreadyHas = prev.some(m => m.id === latestAi.id || (latestAi.execution_id && m.execution_id === latestAi.execution_id));"
        )
        changes.append(f"Patch2b: line {i+1} - safety-net execution_id dedup")
        break

# === Patch 3: Scroll threshold 150→300 ===
for i in range(len(lines)):
    if "container.scrollHeight - 150" in lines[i] and i >= 3330 and i <= 3340:
        lines[i] = lines[i].replace("scrollHeight - 150", "scrollHeight - 300")
        changes.append(f"Patch3: line {i+1} - scroll threshold 150→300")
        break

# === Patch 4: Dedup enhancement - lower threshold + id/render_id check ===
for i in range(len(lines)):
    if "_fc.length >= 20" in lines[i] and i >= 494 and i <= 500:
        lines[i] = lines[i].replace("_fc.length >= 20", "_fc.length >= 10")
        changes.append(f"Patch4a: line {i+1} - dedup threshold 20→10")
        break

# Add id + render_id checks before execution_id line
for i in range(len(lines)):
    if (i >= 494 and i <= 500 and
        "finalMessage.execution_id && m.execution_id === finalMessage.execution_id" in lines[i]):
        indent = "      "
        new_checks = (
            indent + "(m.id === finalMessage.id) ||\n" +
            indent + "(finalMessage.render_id && m.render_id && m.render_id === finalMessage.render_id) ||\n"
        )
        lines[i] = new_checks + lines[i]
        changes.append(f"Patch4b: line {i+1} - added id/render_id dedup")
        break

with open(TARGET, "w") as f:
    f.writelines(lines)

print(f"Applied {len(changes)} patches to {TARGET}")
for c in changes:
    print(f"  ✅ {c}")
print(f"Lines: {original_count} → {len(lines)}")
