#!/usr/bin/env python3
"""채팅창 메시지 사라짐 버그 수정: setMessages(filtered) → 병합 방식"""
import re

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    lines = f.readlines()

MERGE_REPLACE = """              setMessages(prev => {
                const freshIds = new Set(filtered.map(m => m.id));
                const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                const preserved = prev.filter(m => !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") && new Date(m.created_at || 0).getTime() < oldestFreshTime);
                return [...preserved, ...filtered];
              });
"""

# 3곳의 줄 번호 (0-indexed)
targets = [1272, 1304, 1354]  # 1273, 1305, 1355 in 1-indexed

replaced = 0
new_lines = []
for i, line in enumerate(lines):
    if i in targets and "setMessages(filtered);" in line:
        # 인덴트 보존
        indent = line[:len(line) - len(line.lstrip())]
        merge_code = MERGE_REPLACE.replace("              ", indent)
        new_lines.append(merge_code)
        replaced += 1
        print(f"  [OK] L{i+1}: setMessages(filtered) → merge 로직으로 교체")
    else:
        new_lines.append(line)

if replaced == 3:
    with open(FILE, "w") as f:
        f.writelines(new_lines)
    print(f"\n✅ {replaced}곳 수정 완료: {FILE}")
else:
    print(f"\n❌ {replaced}/3곳만 매칭됨. 파일 미수정. 줄 번호 재확인 필요.")
