#!/usr/bin/env python3
"""
streaming_placeholder 필터링 버그 수정 스크립트
- loadMessages filterPlaceholder=true 경로: null 제거 대신 표시
- 무한 스크롤 로드: null 제거 대신 표시
- 스트리밍 완료 후 interval 폴링: placeholder 표시 유지
- stopStreaming 후: placeholder 표시 유지
- stopBackgroundStreaming 후: placeholder 표시 유지
"""

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

import shutil, re

# 백업
shutil.copy(FILE, FILE + ".bak_bubble")
print(f"백업 완료: {FILE}.bak_bubble")

with open(FILE, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# ──────────────────────────────────────────────────────────────────────
# FIX 1: loadMessages() filterPlaceholder=true 경로 (L1089~1094)
# placeholder가 있으면 내용 없어도 표시 (null 제거 금지)
# ──────────────────────────────────────────────────────────────────────
OLD1 = """          const processed = filterPlaceholder
            ? msgs.map((m) => {
                if (m.intent !== "streaming_placeholder") return m;
                if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
                return null;
              }).filter(Boolean) as ChatMessage[]
            : msgs.map((m) =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." }
                  : m
              );"""

NEW1 = """          const processed = filterPlaceholder
            ? msgs.map((m) => {
                if (m.intent !== "streaming_placeholder") return m;
                // FIX: placeholder 삭제 금지 — 내용 있으면 recovered로, 없으면 생성 중 표시
                if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
                return { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." };
              })
            : msgs.map((m) =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." }
                  : m
              );"""

if OLD1 in content:
    content = content.replace(OLD1, NEW1)
    print("FIX 1 적용: loadMessages filterPlaceholder 경로 수정")
else:
    print("FIX 1 SKIP: 패턴 없음 (이미 수정됐거나 코드 변경됨)")

# ──────────────────────────────────────────────────────────────────────
# FIX 2: 무한 스크롤 로드 (L756~759) - 이전 메시지 로드 시 placeholder 처리
# ──────────────────────────────────────────────────────────────────────
OLD2 = """      const filtered = result.messages.map(m => {
        if (m.intent !== "streaming_placeholder") return m;
        if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
        return null;
      }).filter(Boolean) as ChatMessage[];"""

NEW2 = """      const filtered = result.messages.map(m => {
        if (m.intent !== "streaming_placeholder") return m;
        // FIX: placeholder 삭제 금지 — 내용 있으면 recovered로, 없으면 생성 중 표시
        if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
        return { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." };
      }) as ChatMessage[];"""

if OLD2 in content:
    content = content.replace(OLD2, NEW2)
    print("FIX 2 적용: 무한 스크롤 placeholder 처리 수정")
else:
    print("FIX 2 SKIP: 패턴 없음")

# ──────────────────────────────────────────────────────────────────────
# FIX 3: 스트리밍 완료 후 interval 폴링 (L1249~1255) - 세션 전환 후 timeout 로드
# ──────────────────────────────────────────────────────────────────────
OLD3 = """          if (msgs.length > 0) {
            setMessages(msgs.map((m) => {
              if (m.intent !== "streaming_placeholder") return m;
              if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
              return null;
            }).filter(Boolean) as ChatMessage[]);
          }"""

NEW3 = """          if (msgs.length > 0) {
            setMessages(msgs.map((m) => {
              if (m.intent !== "streaming_placeholder") return m;
              // FIX: placeholder 삭제 금지 — 내용 있으면 recovered로, 없으면 생성 중 표시
              if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
              return { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." };
            }) as ChatMessage[]);
          }"""

if OLD3 in content:
    content = content.replace(OLD3, NEW3)
    print("FIX 3 적용: 세션 timeout 재로드 placeholder 처리 수정")
else:
    print("FIX 3 SKIP: 패턴 없음")

# ──────────────────────────────────────────────────────────────────────
# FIX 4: 폴링 interval - streaming 아닐 때 placeholder 완전 필터링 (L1509)
# rawLatest.filter(m => m.intent !== "streaming_placeholder") 제거
# ──────────────────────────────────────────────────────────────────────
OLD4 = """        const latest = _waitingBg
          ? rawLatest.map((m) => m.intent === "streaming_placeholder" ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." } : m)
          : rawLatest.filter((m) => m.intent !== "streaming_placeholder");"""

NEW4 = """        const latest = _waitingBg
          ? rawLatest.map((m) => m.intent === "streaming_placeholder" ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." } : m)
          // FIX: placeholder 삭제 금지 — streaming 아닐 때도 placeholder는 표시 유지
          : rawLatest.map((m) => m.intent === "streaming_placeholder" ? { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." } : m);"""

if OLD4 in content:
    content = content.replace(OLD4, NEW4)
    print("FIX 4 적용: 폴링 interval placeholder 필터링 제거")
else:
    print("FIX 4 SKIP: 패턴 없음")

# ──────────────────────────────────────────────────────────────────────
# FIX 5: stopStreaming 후 DB 동기화 (L2638)
# ──────────────────────────────────────────────────────────────────────
OLD5 = """            const filtered = msgs.filter((m) => m.intent !== "streaming_placeholder");
            // stopped 메시지가 있으면 유지하면서 DB 메시지와 병합
            isNearBottomRef.current = false;"""

NEW5 = """            const filtered = msgs.map((m) => m.intent === "streaming_placeholder"
              ? { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." }
              : m
            );
            // FIX: placeholder 삭제 금지 — stopped 메시지가 있으면 유지하면서 DB 메시지와 병합
            isNearBottomRef.current = false;"""

if OLD5 in content:
    content = content.replace(OLD5, NEW5)
    print("FIX 5 적용: stopStreaming 후 DB 동기화 placeholder 처리 수정")
else:
    print("FIX 5 SKIP: 패턴 없음")

# ──────────────────────────────────────────────────────────────────────
# FIX 6: stopBackgroundStreaming 후 DB 동기화 (L2682)
# ──────────────────────────────────────────────────────────────────────
OLD6 = """          const filtered = msgs.filter((m: ChatMessage) => m.intent !== "streaming_placeholder");
          setMessages((prev) => {
            const stoppedMsgs = prev.filter((m) => m.id.startsWith("stopped-"));
            const dbIds = new Set(filtered.map((m) => m.id));
            const merged = [...filtered, ...stoppedMsgs.filter((m) => !dbIds.has(m.id))];
            return merged.sort(
              (a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
            );
          });
        })
        .catch(() => {});
    }, 1200);
  }"""

NEW6 = """          const filtered = msgs.map((m: ChatMessage) => m.intent === "streaming_placeholder"
            ? { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." }
            : m
          );
          // FIX: placeholder 삭제 금지
          setMessages((prev) => {
            const stoppedMsgs = prev.filter((m) => m.id.startsWith("stopped-"));
            const dbIds = new Set(filtered.map((m) => m.id));
            const merged = [...filtered, ...stoppedMsgs.filter((m) => !dbIds.has(m.id))];
            return merged.sort(
              (a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
            );
          });
        })
        .catch(() => {});
    }, 1200);
  }"""

if OLD6 in content:
    content = content.replace(OLD6, NEW6)
    print("FIX 6 적용: stopBackgroundStreaming 후 placeholder 처리 수정")
else:
    print("FIX 6 SKIP: 패턴 없음")

# ──────────────────────────────────────────────────────────────────────
# 결과 확인
# ──────────────────────────────────────────────────────────────────────
if content == original:
    print("\n경고: 변경 없음 — 모든 패턴이 이미 수정됐거나 코드가 다름")
else:
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\n파일 저장 완료: {FILE}")

print("\n변경 후 streaming_placeholder 관련 라인:")
import subprocess
result = subprocess.run(
    ["grep", "-n", "filter.*streaming_placeholder\\|streaming_placeholder.*filter", FILE],
    capture_output=True, text=True
)
print(result.stdout or "(필터링 패턴 없음 — 정상)")
