#!/usr/bin/env python3
"""Fix dashboard build errors:
1. DiscussionPanel.tsx line 705: remove redundant phase !== "setup" check
2. ChatArtifactPanel.tsx: add export button to artifact header
"""
import re

DASH = "/root/aads/aads-dashboard/src"

# --- Fix 1: DiscussionPanel.tsx ---
dp_path = f"{DASH}/components/chat/DiscussionPanel.tsx"
with open(dp_path, "r") as f:
    dp = f.read()

# The parent block at line 622 already narrows phase to exclude "setup",
# so `phase !== "setup"` at line 705 is always true → remove it
old_dp = '{error && phase !== "setup" && ('
new_dp = "{error && ("
if old_dp in dp:
    dp = dp.replace(old_dp, new_dp)
    with open(dp_path, "w") as f:
        f.write(dp)
    print(f"[OK] DiscussionPanel.tsx: removed redundant phase check")
else:
    print(f"[SKIP] DiscussionPanel.tsx: pattern not found")

# --- Fix 2: ChatArtifactPanel.tsx — add export button ---
cap_path = f"{DASH}/app/chat/ChatArtifactPanel.tsx"
with open(cap_path, "r") as f:
    cap = f.read()

# Check if export button already exists
if "내보내기" in cap:
    print("[SKIP] ChatArtifactPanel.tsx: export button already exists")
else:
    # Find the artifact header area — look for the "새 창" (new window) button
    # and add export button before it
    new_window_pattern = r'(onClick=\{.*?window\.open.*?activeArtifact\.content.*?\})'
    
    # Alternative: find the toolbar/header buttons area in the artifact detail view
    # Look for the pattern where action buttons are rendered near activeArtifact
    # Strategy: find "🔗 새 창" or the new window button block, add export before it
    
    if "새 창" in cap:
        # Add export button before the "새 창" button
        export_button = '''<button
                        onClick={async () => {
                          try {
                            const res = await fetch(`${BASE_URL}/chat/artifacts/${activeArtifact.id}/export`, {
                              method: "POST",
                              headers: { "Content-Type": "application/json", ...authHdrs() },
                              body: JSON.stringify({ format: "md" }),
                            });
                            if (!res.ok) return;
                            const data = await res.json();
                            const blob = new Blob([data.content], { type: data.mime || "text/markdown" });
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement("a");
                            a.href = url;
                            a.download = data.filename || "artifact.md";
                            a.click();
                            URL.revokeObjectURL(url);
                          } catch {}
                        }}
                        style={{
                          padding: "4px 10px", fontSize: "11px", borderRadius: "6px",
                          border: "1px solid var(--ct-border)", cursor: "pointer",
                          background: "var(--ct-hover)", color: "var(--ct-text2)",
                        }}
                      >
                        ⬇️ 내보내기
                      </button>
                      '''
        
        # Find first occurrence of "새 창" button and insert export before it
        # The button pattern: <button ... > ... 새 창 ... </button>
        idx = cap.find("새 창")
        if idx > 0:
            # Walk back to find the <button that contains "새 창"
            button_start = cap.rfind("<button", 0, idx)
            if button_start > 0:
                cap = cap[:button_start] + export_button + cap[button_start:]
                with open(cap_path, "w") as f:
                    f.write(cap)
                print(f"[OK] ChatArtifactPanel.tsx: export button added before '새 창' button")
            else:
                print("[WARN] Could not find <button before '새 창'")
        else:
            print("[WARN] '새 창' not found in ChatArtifactPanel.tsx")
    else:
        print("[WARN] No '새 창' button found — looking for alternative insertion point")
        # Fallback: look for the artifact toolbar area
        # Find "activeArtifact" detail render area
        toolbar_marker = "activeArtifact.content"
        idx = cap.find(toolbar_marker)
        if idx > 0:
            print(f"[INFO] Found activeArtifact.content at position {idx}")
        else:
            print("[SKIP] No suitable insertion point found")

print("\n--- Verification ---")
# Verify DiscussionPanel fix
with open(dp_path, "r") as f:
    dp2 = f.read()
if 'phase !== "setup"' not in dp2:
    print("[PASS] DiscussionPanel: no more phase !== setup")
else:
    # Check if remaining ones are in other contexts
    count = dp2.count('phase !== "setup"')
    print(f"[INFO] DiscussionPanel: {count} remaining phase !== setup (may be in other contexts)")

# Verify export button
with open(cap_path, "r") as f:
    cap2 = f.read()
if "내보내기" in cap2:
    print("[PASS] ChatArtifactPanel: export button present")
else:
    print("[FAIL] ChatArtifactPanel: export button NOT found")
