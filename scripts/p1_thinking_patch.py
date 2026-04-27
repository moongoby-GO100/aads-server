#!/usr/bin/env python3
"""P1 패치: thinkingBuf 분리 — streamBuf 덮어쓰기 방지"""
import sys

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    code = f.read()

patches = 0

# === Patch 1: thinkingBuf state 추가 (streamBuf 선언 직후) ===
old1 = '  const [streamBuf, setStreamBuf] = useState("");'
new1 = '  const [streamBuf, setStreamBuf] = useState("");\n  const [thinkingBuf, setThinkingBuf] = useState("");'
if "thinkingBuf" not in code:
    if old1 in code:
        code = code.replace(old1, new1, 1)
        patches += 1
        print("Patch 1: thinkingBuf state 추가 완료")
    else:
        print("Patch 1: WARNING - streamBuf 선언 패턴 미매치", file=sys.stderr)
else:
    print("Patch 1: SKIP - thinkingBuf 이미 존재")

# === Patch 2: thinking 핸들러 → thinkingBuf 사용 ===
old2 = '''            } else if (ev.type === "thinking" && ev.content) {
              setToolStatus("\U0001f4ad 사고 중...");
              // thinking 텍스트가 있으면 streamBuf에 즉시 표시 — delta가 오면 자동 교체됨
              if (!isStale() && !full) setStreamBuf(ev.content || "분석 중...");
            }'''
new2 = '''            } else if (ev.type === "thinking" && ev.content) {
              setToolStatus("\U0001f4ad 사고 중...");
              if (!isStale()) setThinkingBuf(prev => prev + (ev.content || ""));
            }'''
if old2 in code:
    code = code.replace(old2, new2, 1)
    patches += 1
    print("Patch 2: thinking 핸들러 thinkingBuf 전환 완료")
else:
    print("Patch 2: WARNING - thinking 핸들러 패턴 미매치", file=sys.stderr)

# === Patch 3 & 4: 스트리밍 시작 시 thinkingBuf 초기화 (2곳) ===
old34 = """    setStreaming(true);
    setStreamBuf("");
    setToolLogs([]);"""
new34 = """    setStreaming(true);
    setStreamBuf("");
    setThinkingBuf("");
    setToolLogs([]);"""
count34 = code.count(old34)
if count34 > 0:
    code = code.replace(old34, new34)
    patches += count34
    print(f"Patch 3-4: 스트리밍 시작 초기화 {count34}곳 완료")
else:
    print("Patch 3-4: WARNING - 스트리밍 시작 패턴 미매치", file=sys.stderr)

# === Patch 5: done 이벤트 1 — thinkingBuf 초기화 (setYellowWarning 있는 블록) ===
old5 = """              setStreamBuf("");
              setStreaming(false);
              setToolStatus(null);
              setToolLogs([]);
              setYellowWarning(null);"""
new5 = """              setStreamBuf("");
              setThinkingBuf("");
              setStreaming(false);
              setToolStatus(null);
              setToolLogs([]);
              setYellowWarning(null);"""
if old5 in code:
    code = code.replace(old5, new5, 1)
    patches += 1
    print("Patch 5: done 이벤트 1 thinkingBuf 초기화 완료")
else:
    print("Patch 5: WARNING - done 이벤트 1 패턴 미매치", file=sys.stderr)

# === Patch 6: done 이벤트 2 — thinkingBuf 초기화 (regenerate done) ===
old6 = """              setStreamBuf("");
              setStreaming(false);
              setToolStatus(null);
              setToolLogs([]);
              if (ev.session_cost) setSessionCost(ev.session_cost);"""
new6 = """              setStreamBuf("");
              setThinkingBuf("");
              setStreaming(false);
              setToolStatus(null);
              setToolLogs([]);
              if (ev.session_cost) setSessionCost(ev.session_cost);"""
if old6 in code:
    code = code.replace(old6, new6, 1)
    patches += 1
    print("Patch 6: done 이벤트 2 thinkingBuf 초기화 완료")
else:
    print("Patch 6: WARNING - done 이벤트 2 패턴 미매치", file=sys.stderr)

with open(FILE, "w") as f:
    f.write(code)

print(f"\n총 {patches} 패치 적용 완료")
if patches < 5:
    print("⚠️ 일부 패턴 미매치 — 수동 확인 필요", file=sys.stderr)
    sys.exit(1)
