#!/usr/bin/env python3
"""P0 프론트엔드 패치: 버블 깜빡임 제거 + 즉시 분석중 표시"""
import re

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    code = f.read()

original = code

# P0-1: done 핸들러에서 setStreamBuf/setThinkingBuf/setStreaming을 setMessages 뒤로 이동
# Step A: done 핸들러 시작부에서 3줄 제거
old_done = '''              gotFinal = true;
              _stopDrain();  // Phase4: 버퍼 즉시 플러시
              setStreamBuf("");
              setThinkingBuf("");
              setStreaming(false);
              setToolStatus(null);'''

new_done = '''              gotFinal = true;
              _stopDrain();  // Phase4: 버퍼 즉시 플러시
              setToolStatus(null);'''

if old_done in code:
    code = code.replace(old_done, new_done, 1)
    print("P0-1a: done 핸들러에서 3줄 제거 완료")
else:
    print("P0-1a: SKIP - old_done not found")

# Step B: mergeCooldownUntilRef 앞에 3줄 삽입
old_merge = '''              if (requestSessionId) {
                mergeCooldownUntilRef.current = Date.now() + 5000;
              }
              break; // done 이벤트 수신 → for 루프 탈출'''

new_merge = '''              // P0-FIX: setMessages 후 스트리밍 상태 클리어 (깜빡임 방지)
              setStreamBuf("");
              setThinkingBuf("");
              setStreaming(false);
              if (requestSessionId) {
                mergeCooldownUntilRef.current = Date.now() + 5000;
              }
              break; // done 이벤트 수신 → for 루프 탈출'''

if old_merge in code:
    code = code.replace(old_merge, new_merge, 1)
    print("P0-1b: setMessages 뒤에 3줄 삽입 완료")
else:
    print("P0-1b: SKIP - old_merge not found")

# P0-2: placeholder에 즉시 "분석 중..." 표시 (2곳)
old_ph1 = 'content: "", intent: "streaming_placeholder"'
new_ph1 = 'content: "\\u23F3 분석 중...", intent: "streaming_placeholder"'

count = code.count(old_ph1)
if count >= 2:
    code = code.replace(old_ph1, new_ph1)
    print(f"P0-2: placeholder content 변경 {count}곳")
else:
    print(f"P0-2: SKIP - found {count} matches (expected >=2)")

if code != original:
    with open(FILE, "w") as f:
        f.write(code)
    print(f"DONE: {FILE} patched")
else:
    print("NO CHANGES")
