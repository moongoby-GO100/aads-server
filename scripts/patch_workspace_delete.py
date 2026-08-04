#!/usr/bin/env python3
"""Patch ChatSidebar.tsx and page.tsx to add workspace delete functionality."""
import sys

SIDEBAR = "/root/aads/aads-dashboard/src/app/chat/ChatSidebar.tsx"
PAGE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

# === 1. Patch ChatSidebar.tsx ===
with open(SIDEBAR, "r") as f:
    sb = f.read()

# 1a. Add deleteWorkspace to props interface
sb = sb.replace(
    "deleteSession: (id: string) => void;\n",
    "deleteSession: (id: string) => void;\n  deleteWorkspace?: (id: string) => void;\n",
    1,
)

# 1b. Add deleteWorkspace to destructuring
sb = sb.replace(
    "createSession, deleteSession, setShowAddProject, theme, toggleTheme,",
    "createSession, deleteSession, deleteWorkspace, setShowAddProject, theme, toggleTheme,",
    1,
)

# 1c. Replace workspace header: wrap in flex div and add delete button
OLD_HEADER = """{/* Workspace header */}
                <button
                  onClick={() => onWorkspaceToggle(ws.id)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    padding: "7px 10px",
                    fontSize: "11px",
                    fontWeight: 700,
                    letterSpacing: "0.5px",
                    textTransform: "uppercase",
                    background: "none",
                    border: "none",
                    borderRadius: "6px",
                    cursor: "pointer",
                    color: activeWs === ws.id
                      ? "var(--ct-accent)"
                      : expandedWorkspaceIds.includes(ws.id)
                        ? "var(--ct-text)"
                        : "var(--ct-text2)",
                    display: "flex",
                    alignItems: "center",
                    gap: "6px",
                  }}
                >
                  <span>{ws.icon || "📁"}</span>
                  <span style={{ flex: 1 }}>{ws.name}</span>
                  <span style={{ fontSize: "10px" }}>
                    {expandedWorkspaceIds.includes(ws.id) ? "▾" : "▸"}
                  </span>
                </button>"""

NEW_HEADER = """{/* Workspace header */}
                <div style={{ display: "flex", alignItems: "center", borderRadius: "6px" }} className="ws-header-row">
                  <button
                    onClick={() => onWorkspaceToggle(ws.id)}
                    style={{
                      flex: 1,
                      textAlign: "left",
                      padding: "7px 10px",
                      fontSize: "11px",
                      fontWeight: 700,
                      letterSpacing: "0.5px",
                      textTransform: "uppercase",
                      background: "none",
                      border: "none",
                      borderRadius: "6px",
                      cursor: "pointer",
                      color: activeWs === ws.id
                        ? "var(--ct-accent)"
                        : expandedWorkspaceIds.includes(ws.id)
                          ? "var(--ct-text)"
                          : "var(--ct-text2)",
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                    }}
                  >
                    <span>{ws.icon || "📁"}</span>
                    <span style={{ flex: 1 }}>{ws.name}</span>
                    <span style={{ fontSize: "10px" }}>
                      {expandedWorkspaceIds.includes(ws.id) ? "▾" : "▸"}
                    </span>
                  </button>
                  {deleteWorkspace && (
                    <button
                      title="프로젝트 삭제"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm(`"${ws.name}" 프로젝트와 모든 세션을 삭제하시겠습니까?`))
                          deleteWorkspace(ws.id);
                      }}
                      style={{
                        width: "22px", height: "22px",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        border: "none", borderRadius: "4px",
                        background: "none", color: "var(--ct-text2)",
                        cursor: "pointer", fontSize: "11px", opacity: 0.4,
                        flexShrink: 0, marginRight: "2px",
                      }}
                    >
                      🗑
                    </button>
                  )}
                </div>"""

if OLD_HEADER in sb:
    sb = sb.replace(OLD_HEADER, NEW_HEADER, 1)
    print("OK: sidebar workspace header replaced")
else:
    print("WARN: sidebar header pattern not found, skipping")

with open(SIDEBAR, "w") as f:
    f.write(sb)

# === 2. Patch page.tsx ===
with open(PAGE, "r") as f:
    pg = f.read()

# 2a. Add deleteWorkspace function after deleteSession
DELETE_WS_FN = """
  async function deleteWorkspace(id: string) {
    try {
      await chatApi(`/chat/workspaces/${id}`, { method: "DELETE" });
      setWorkspaces((prev) => prev.filter((w) => w.id !== id));
      if (activeWs === id) { setActiveSession(null); setMessages([]); }
    } catch { /* ignore */ }
  }
"""

# Insert after deleteSession's closing brace
ANCHOR = "    setContextMenu(null);\n  }\n\n  async function togglePin"
if ANCHOR in pg:
    pg = pg.replace(ANCHOR, "    setContextMenu(null);\n  }\n" + DELETE_WS_FN + "\n  async function togglePin", 1)
    print("OK: page deleteWorkspace function added")
else:
    print("WARN: page anchor for deleteWorkspace not found")

# 2b. Add deleteWorkspace prop to ChatSidebar usage
pg = pg.replace(
    'createSession={openCreateSessionModal} deleteSession={deleteSession}',
    'createSession={openCreateSessionModal} deleteSession={deleteSession}\n        deleteWorkspace={deleteWorkspace}',
    1,
)
print("OK: page ChatSidebar prop added")

with open(PAGE, "w") as f:
    f.write(pg)

print("DONE: all patches applied")
