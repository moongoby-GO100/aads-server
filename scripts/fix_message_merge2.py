#!/usr/bin/env python3
"""4번째 setMessages 교체: L2230 stream-resume 후 메시지 교체"""

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    content = f.read()

OLD = """                if (freshMsgs) {
                  setMessages(freshMsgs.filter((m: ChatMessage) => m.intent !== "streaming_placeholder"));
                }
                // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
                const _lastUser1696"""

NEW = """                if (freshMsgs) {
                  const filtered = freshMsgs.filter((m: ChatMessage) => m.intent !== "streaming_placeholder");
                  if (filtered.length > 0) {
                    setMessages(prev => {
                      const freshIds = new Set(filtered.map(m => m.id));
                      const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                      const preserved = prev.filter(m => !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") && new Date(m.created_at || 0).getTime() < oldestFreshTime);
                      return [...preserved, ...filtered];
                    });
                  }
                }
                // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
                const _lastUser1696"""

count = content.count(OLD)
if count == 1:
    content = content.replace(OLD, NEW)
    with open(FILE, "w") as f:
        f.write(content)
    print(f"✅ L2230 수정 완료 (1곳)")
else:
    print(f"❌ 매칭 {count}건. 파일 미수정.")
