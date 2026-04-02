#!/usr/bin/env python3
"""스트리밍 버블 사라짐 버그 수정 — streaming_placeholder 내용 보존"""
import re

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    code = f.read()

changes = 0

# ─── 1. streamBufRef useEffect 추가 (streamBuf 스크롤 useEffect 뒤에) ───
# 찾기: }, [streaming, streamBuf]); 뒤에 추가
old1 = '  }, [streaming, streamBuf]);'
new1 = """  }, [streaming, streamBuf]);

  // ★ streamBufRef 동기화 — SSE finally에서 streamBuf 값 참조용
  useEffect(() => { streamBufRef.current = streamBuf; }, [streamBuf]);"""
if old1 in code:
    code = code.replace(old1, new1, 1)
    changes += 1
    print(f"[OK] Patch 1: streamBufRef useEffect 추가")
else:
    print(f"[SKIP] Patch 1: target not found")

# ─── 2. SSE finally 블록 — placeholder 삭제 → 내용 보존 ───
old2 = """        // streaming_placeholder 잔여물 정리 (Invisible Recovery 중이면 placeholder 유지 — 폴링이 교체함)
        setMessages((prev) => prev.filter((m) => m.intent !== "streaming_placeholder"));"""
new2 = """        // streaming_placeholder 잔여물 정리 — 내용 있으면 버블 유지 (사라짐 방지)
        setMessages((prev) => {
          const capturedBuf = streamBufRef.current;
          return prev.map((m) => {
            if (m.intent !== "streaming_placeholder") return m;
            const preserved = capturedBuf || m.content || "";
            if (preserved.trim()) {
              return { ...m, content: preserved, intent: undefined, model_used: "interrupted" };
            }
            return null;
          }).filter(Boolean) as ChatMessage[];
        });"""
if old2 in code:
    code = code.replace(old2, new2, 1)
    changes += 1
    print(f"[OK] Patch 2: SSE finally 내용 보존")
else:
    print(f"[SKIP] Patch 2: target not found")

# ─── 3. loadOlderMessages — placeholder 내용 보존 ───
old3 = """      const filtered = result.messages.filter(m => m.intent !== "streaming_placeholder");"""
new3 = """      const filtered = result.messages.map(m => {
        if (m.intent !== "streaming_placeholder") return m;
        if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
        return null;
      }).filter(Boolean) as ChatMessage[];"""
if old3 in code:
    code = code.replace(old3, new3, 1)
    changes += 1
    print(f"[OK] Patch 3: loadOlderMessages 내용 보존")
else:
    print(f"[SKIP] Patch 3: target not found")

# ─── 4. 안전장치 fallback (빈 배열 재시도) — placeholder 내용 보존 ───
old4 = """            setMessages(msgs.filter((m) => m.intent !== "streaming_placeholder"));"""
new4 = """            setMessages(msgs.map((m) => {
              if (m.intent !== "streaming_placeholder") return m;
              if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
              return null;
            }).filter(Boolean) as ChatMessage[]);"""
if old4 in code:
    code = code.replace(old4, new4, 1)
    changes += 1
    print(f"[OK] Patch 4: 안전장치 fallback 내용 보존")
else:
    print(f"[SKIP] Patch 4: target not found")

# ─── 5. 세션 로드 filterPlaceholder=true 경로 — 내용 보존 ───
old5 = """            ? msgs.filter((m) => m.intent !== "streaming_placeholder")
            : msgs.map((m) =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." }
                  : m
              );"""
new5 = """            ? msgs.map((m) => {
                if (m.intent !== "streaming_placeholder") return m;
                if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
                return null;
              }).filter(Boolean) as ChatMessage[]
            : msgs.map((m) =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." }
                  : m
              );"""
if old5 in code:
    code = code.replace(old5, new5, 1)
    changes += 1
    print(f"[OK] Patch 5: 세션 로드 filterPlaceholder 내용 보존")
else:
    print(f"[SKIP] Patch 5: target not found")

with open(FILE, "w") as f:
    f.write(code)

print(f"\n총 {changes}개 패치 적용 완료")
