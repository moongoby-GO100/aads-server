"""Patch page.tsx to add task_plan SSE handler in all SSE readers."""
import sys

TARGET = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

# The block to insert (two variants: one with isStale(), one with activeSessionRef check)
TASK_PLAN_BLOCK_STALE = '''            } else if (ev.type === "task_plan") {
              if (!isStale()) {
                const planMsg = ev.content || "\\u2705 요청을 수신했습니다. 분석 중...";
                setStreamBuf(planMsg);
                setToolStatus(planMsg);
              }
              continue;'''

TASK_PLAN_BLOCK_SESSION = '''            } else if (ev.type === "task_plan") {
              if (activeSessionRef.current === attachSessionId) {
                const planMsg = ev.content || "\\u2705 요청을 수신했습니다. 분석 중...";
                setStreamBuf(planMsg);
                setToolStatus(planMsg);
              }
              continue;'''

with open(TARGET, "r") as f:
    content = f.read()

# Check if already patched
if 'ev.type === "task_plan"' in content:
    print("ALREADY_PATCHED: task_plan handler already exists")
    sys.exit(0)

lines = content.split("\n")
insertions = []  # (line_index, block_to_insert)

for i, line in enumerate(lines):
    # Find: continue; after model_fallback, followed by } else if (ev.type === "delta"
    if '} else if (ev.type === "model_fallback")' in line:
        # Next non-empty lines should be setToolStatus + continue
        for j in range(i + 1, min(i + 5, len(lines))):
            if lines[j].strip() == "continue;":
                # Check next line is delta handler
                for k in range(j + 1, min(j + 3, len(lines))):
                    if '} else if (ev.type === "delta"' in lines[k]:
                        # Determine which variant based on stale check
                        if "activeSessionRef" in lines[i + 1]:
                            insertions.append((j + 1, TASK_PLAN_BLOCK_SESSION))
                        else:
                            insertions.append((j + 1, TASK_PLAN_BLOCK_STALE))
                        break
                break

# Also check the resume reader: heartbeat → delta (no model_fallback)
for i, line in enumerate(lines):
    if 'ev.type === "heartbeat"' in line and "resetSseTimeout" not in lines[max(0, i-2):i+1][0]:
        # Look for pattern: heartbeat handler → delta handler with no model_fallback between
        for j in range(i, min(i + 10, len(lines))):
            if '(ev.type === "delta"' in lines[j]:
                # Check there's no model_fallback between i and j
                has_model_fallback = any("model_fallback" in lines[k] for k in range(i, j))
                if not has_model_fallback:
                    # Check if already handled by previous insertion
                    already = any(idx == j for idx, _ in insertions)
                    if not already:
                        insertions.append((j, TASK_PLAN_BLOCK_STALE))
                break

if not insertions:
    print("ERROR: Could not find insertion points")
    sys.exit(1)

# Sort by line number descending to preserve indices
insertions.sort(key=lambda x: x[0], reverse=True)

for idx, block in insertions:
    lines.insert(idx, block)

with open(TARGET, "w") as f:
    f.write("\n".join(lines))

print(f"SUCCESS: Inserted task_plan handler at {len(insertions)} location(s)")
for idx, _ in sorted(insertions, key=lambda x: x[0]):
    print(f"  - After line {idx}")
