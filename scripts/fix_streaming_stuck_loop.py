"""
AADS TODO #1, #7, #8: STREAMING-STUCK 발동 후 재연결 루프 방지
- stuckCooldownUntil 변수 추가
- 재연결 조건에 쿨다운 체크 추가
- STUCK 발동 시 90초 쿨다운 설정
"""
import sys

filepath = '/root/aads/aads-dashboard/src/app/chat/page.tsx'

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

changes = 0

# 1. Add stuckCooldownUntil variable after lastStreamingProgressKey
old1 = '    let lastStreamingProgressKey = ""; // 진행 변화가 없을 때만 stuck으로 본다.'
if content.count(old1) != 1:
    print(f"FAIL: old1 found {content.count(old1)} times, expected 1")
    sys.exit(1)
new1 = old1 + '\n    let stuckCooldownUntil = 0;'
content = content.replace(old1, new1)
changes += 1
print("OK: patch 1 - stuckCooldownUntil variable added")

# 2. Add cooldown check to re-attach condition (unique: only 1 occurrence)
old2 = 'if (ss.is_streaming && !_waitingBg && !_streaming) {'
if content.count(old2) != 1:
    print(f"FAIL: old2 found {content.count(old2)} times, expected 1")
    sys.exit(1)
new2 = 'if (ss.is_streaming && !_waitingBg && !_streaming && Date.now() >= stuckCooldownUntil) {'
content = content.replace(old2, new2)
changes += 1
print("OK: patch 2 - cooldown check added to re-attach condition")

# 3. Set cooldown when STUCK fires (unique context: setStreaming before streamingStuckCount reset)
old3 = '            setStreaming(false); setStreamBuf("");\n            streamingStuckCount = 0;\n          }\n        } else {'
if content.count(old3) != 1:
    print(f"FAIL: old3 found {content.count(old3)} times, expected 1")
    sys.exit(1)
new3 = '            setStreaming(false); setStreamBuf("");\n            streamingStuckCount = 0;\n            stuckCooldownUntil = Date.now() + 90000;\n          }\n        } else {'
content = content.replace(old3, new3)
changes += 1
print("OK: patch 3 - 90s cooldown set after STUCK fires")

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"\nAll {changes} patches applied successfully to {filepath}")
