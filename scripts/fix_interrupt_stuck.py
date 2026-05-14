#!/usr/bin/env python3
"""Fix: interrupt queued=false 시 streaming stuck 해제 + 입력 복원"""
import pathlib

f = pathlib.Path("/root/aads/aads-dashboard/src/app/chat/page.tsx")
src = f.read_text()

OLD = """          if (!res?.queued) {
            const idx = msgQueueRef.current.indexOf(interruptContent);
            if (idx !== -1) msgQueueRef.current.splice(idx, 1);
            setQueueCount(msgQueueRef.current.length);
            setYellowWarning(res?.message || "현재 스트리밍이 아니어서 추가 지시를 대기열에서 제거했습니다.");
            return;
          }"""

NEW = """          if (!res?.queued) {
            const idx = msgQueueRef.current.indexOf(interruptContent);
            if (idx !== -1) msgQueueRef.current.splice(idx, 1);
            setQueueCount(msgQueueRef.current.length);
            // FIX: streaming stuck 해제 + 입력 복원 (메시지 유실 방지)
            streamingSessionRef.current = null;
            setStreaming(false); setStreamBuf("");
            setInput(interruptContent);
            setMessages(prev => prev.filter(m => !m.id.startsWith("interrupt-")));
            setYellowWarning("스트리밍이 종료되어 입력을 복원했습니다. 다시 전송해 주세요.");
            return;
          }"""

count = src.count(OLD)
if count == 0:
    print("ERROR: old_string not found")
elif count > 1:
    print(f"ERROR: old_string found {count} times")
else:
    src = src.replace(OLD, NEW)
    f.write_text(src)
    print(f"OK: patched 1 location")
