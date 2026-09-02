"""Dashboard 모델 폴백 표시 패치 스크립트 — 3파일 수정."""
import sys

DASHBOARD = "/root/aads/aads-dashboard/src"
ok = 0
fail = 0

# ── 1. chatApi.ts: ChatMessage 인터페이스에 requested_model, fallback_reason 추가 ──
path1 = f"{DASHBOARD}/services/chatApi.ts"
with open(path1) as f:
    c = f.read()
old1 = '  confidence_label?: "db_realtime" | "ai_inference" | "mixed" | null;\n}'
new1 = '  confidence_label?: "db_realtime" | "ai_inference" | "mixed" | null;\n  requested_model?: string | null;\n  fallback_reason?: string | null;\n}'
if "requested_model" not in c and old1 in c:
    c = c.replace(old1, new1, 1)
    with open(path1, "w") as f:
        f.write(c)
    print(f"[OK] chatApi.ts patched")
    ok += 1
elif "requested_model" in c:
    print(f"[SKIP] chatApi.ts already has requested_model")
    ok += 1
else:
    print(f"[FAIL] chatApi.ts: old string not found")
    fail += 1

# ── 2. ChatBubble.tsx: 버블 하단 모델 표시 로직 변경 ──
path2 = f"{DASHBOARD}/components/chat/ChatBubble.tsx"
with open(path2) as f:
    c = f.read()
old2 = '            {message.model_used && <span>[{message.model_used}</span>}'
new2 = """            {message.model_used && <span>[{message.requested_model && message.requested_model !== message.model_used ? (
              <>
                <span style={{ textDecoration: "line-through", opacity: 0.6 }}>{message.requested_model}</span>
                <span style={{ color: "#f59e0b" }}>{" \\u2192 "}{message.model_used}</span>
                {message.fallback_reason && <span style={{ color: "#f59e0b" }}>{" \\u26A0\\uFE0F"}{message.fallback_reason}</span>}
              </>
            ) : message.model_used}</span>}"""
if old2 in c:
    c = c.replace(old2, new2, 1)
    with open(path2, "w") as f:
        f.write(c)
    print(f"[OK] ChatBubble.tsx patched")
    ok += 1
else:
    print(f"[FAIL] ChatBubble.tsx: old string not found")
    fail += 1

# ── 3. page.tsx: done 이벤트 메시지 생성에 requested_model, fallback_reason 추가 ──
path3 = f"{DASHBOARD}/app/chat/page.tsx"
with open(path3) as f:
    c = f.read()
old3a = "                    model_used: ev.model || undefined,\n                    intent: ev.intent || undefined,\n                    created_at: new Date().toISOString(),"
new3a = "                    model_used: ev.model || undefined,\n                    requested_model: ev.requested_model || undefined,\n                    fallback_reason: ev.fallback_reason || undefined,\n                    intent: ev.intent || undefined,\n                    created_at: new Date().toISOString(),"
old3b = "                    model_used: ev.model || undefined,\n                    intent: ev.intent || undefined,\n                    input_tokens: ev.input_tokens || undefined,"
new3b = "                    model_used: ev.model || undefined,\n                    requested_model: ev.requested_model || undefined,\n                    fallback_reason: ev.fallback_reason || undefined,\n                    intent: ev.intent || undefined,\n                    input_tokens: ev.input_tokens || undefined,"
patched = 0
if old3a in c:
    c = c.replace(old3a, new3a, 1)
    patched += 1
if old3b in c:
    c = c.replace(old3b, new3b, 1)
    patched += 1
if patched > 0:
    with open(path3, "w") as f:
        f.write(c)
    print(f"[OK] page.tsx patched ({patched} locations)")
    ok += 1
else:
    print(f"[FAIL] page.tsx: old strings not found")
    fail += 1

# ── 결과 ──
print(f"\nTotal: {ok} OK, {fail} FAIL")
sys.exit(1 if fail > 0 else 0)
