#!/usr/bin/env python3
"""Add manual TODO create/edit features to chat page.tsx"""
import sys

TARGET = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(TARGET, "r") as f:
    content = f.read()

changes = 0

# --- Patch 1: Add state variables (already applied from previous run) ---
if 'todoAdding' not in content:
    old1 = '  const [todoActionLoading, setTodoActionLoading] = useState<string | null>(null);'
    new1 = old1 + '''
  const [todoAdding, setTodoAdding] = useState(false);
  const [todoAddTitle, setTodoAddTitle] = useState("");
  const [todoEditingId, setTodoEditingId] = useState<string | null>(null);
  const [todoEditTitle, setTodoEditTitle] = useState("");'''
    if old1 in content:
        content = content.replace(old1, new1, 1)
        changes += 1
        print("PATCH1 OK: state variables added")
    else:
        print("PATCH1 FAIL: anchor not found")
        sys.exit(1)
else:
    print("PATCH1 SKIP: already applied")

# --- Patch 2: Add createTodoItem and saveTodoTitle handlers ---
if 'const createTodoItem' not in content:
    old2 = '  }, [runTodoAction]);\n  // BUG-2 FIX:'
    new2 = '''  }, [runTodoAction]);

  const createTodoItem = useCallback(async (title: string) => {
    const sid = activeSessionRef.current;
    if (!sid || !title.trim()) return;
    setTodoActionLoading("create");
    try {
      await chatApi(`/chat/sessions/${sid}/todos`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim() }),
      });
      await refreshTodos(sid);
    } catch (err) {
      console.error("TODO create failed", err);
    } finally {
      setTodoActionLoading(null);
      setTodoAdding(false);
      setTodoAddTitle("");
    }
  }, [refreshTodos]);

  const saveTodoTitle = useCallback(async (item: ChatTodoItem, newTitle: string) => {
    const sid = activeSessionRef.current;
    if (!sid || !newTitle.trim() || newTitle.trim() === item.title) {
      setTodoEditingId(null);
      setTodoEditTitle("");
      return;
    }
    setTodoActionLoading("edit-" + item.id);
    try {
      await chatApi(`/chat/sessions/${sid}/todos/${item.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: newTitle.trim() }),
      });
      await refreshTodos(sid);
    } catch (err) {
      console.error("TODO title edit failed", err);
    } finally {
      setTodoActionLoading(null);
      setTodoEditingId(null);
      setTodoEditTitle("");
    }
  }, [refreshTodos]);

  // BUG-2 FIX:'''
    if old2 in content:
        content = content.replace(old2, new2, 1)
        changes += 1
        print("PATCH2 OK: handlers added")
    else:
        print("PATCH2 FAIL: anchor not found")
        sys.exit(1)
else:
    print("PATCH2 SKIP: already applied")

# --- Patch 3: Add "+" button in TODO header ---
if 'setTodoAdding(true)' not in content:
    old3 = '                  <span style={{ fontSize: "12px", fontWeight: 700, whiteSpace: "nowrap" }}>TODO</span>'
    new3 = '''                  <span style={{ fontSize: "12px", fontWeight: 700, whiteSpace: "nowrap" }}>TODO</span>
                  {!todoCollapsed && (
                    <button
                      onClick={() => { setTodoAdding(true); setTodoAddTitle(""); }}
                      disabled={!!todoActionLoading}
                      title="\\uc0c8 TODO \\ucd94\\uac00"
                      style={{
                        background: "none", border: "none", cursor: todoActionLoading ? "default" : "pointer",
                        fontSize: "14px", lineHeight: 1, padding: "0 2px", color: "var(--ct-accent)",
                        opacity: todoActionLoading ? 0.6 : 1,
                      }}
                    >+</button>
                  )}'''
    if old3 in content:
        content = content.replace(old3, new3, 1)
        changes += 1
        print("PATCH3 OK: add button inserted")
    else:
        print("PATCH3 FAIL: TODO label not found")
        sys.exit(1)
else:
    print("PATCH3 SKIP: already applied")

# --- Patch 4: Add inline add form before visibleTodos rendering ---
if 'todoAdding && (' not in content:
    old4 = '                  {visibleTodos.length === 0 ? ('
    new4 = '''                  {todoAdding && (
                    <div style={{ display: "flex", gap: "4px", marginBottom: "4px" }}>
                      <input
                        autoFocus
                        value={todoAddTitle}
                        onChange={(e) => setTodoAddTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && todoAddTitle.trim()) createTodoItem(todoAddTitle);
                          if (e.key === "Escape") { setTodoAdding(false); setTodoAddTitle(""); }
                        }}
                        placeholder="\\uc791\\uc5c5 \\uc81c\\ubaa9 \\uc785\\ub825..."
                        style={{
                          flex: 1, fontSize: "11px", padding: "3px 6px", borderRadius: "4px",
                          border: "1px solid var(--ct-border)", background: "var(--ct-input)",
                          color: "var(--ct-text)", outline: "none", minWidth: 0,
                        }}
                      />
                      <button
                        onClick={() => todoAddTitle.trim() && createTodoItem(todoAddTitle)}
                        disabled={!todoAddTitle.trim() || !!todoActionLoading}
                        style={{
                          fontSize: "11px", padding: "2px 8px", borderRadius: "4px", border: "none",
                          background: todoAddTitle.trim() ? "var(--ct-accent)" : "var(--ct-hover)",
                          color: todoAddTitle.trim() ? "#fff" : "var(--ct-text2)", cursor: "pointer",
                        }}
                      >\\ucd94\\uac00</button>
                      <button
                        onClick={() => { setTodoAdding(false); setTodoAddTitle(""); }}
                        style={{
                          fontSize: "11px", padding: "2px 6px", borderRadius: "4px", border: "none",
                          background: "var(--ct-hover)", color: "var(--ct-text2)", cursor: "pointer",
                        }}
                      >\\ucde8\\uc18c</button>
                    </div>
                  )}
                  {visibleTodos.length === 0 ? ('''
    if old4 in content:
        content = content.replace(old4, new4, 1)
        changes += 1
        print("PATCH4 OK: add form inserted")
    else:
        print("PATCH4 FAIL: visibleTodos check not found")
        sys.exit(1)
else:
    print("PATCH4 SKIP: already applied")

# --- Patch 5: Make todo title editable (double-click) ---
if 'todoEditingId === item.id' not in content:
    old5 = '''                        <span
                          style={{
                            flex: 1,
                            minWidth: 0,
                            fontSize: "12px",
                            color: isDone ? "var(--ct-text2)" : "var(--ct-text)",
                            textDecoration: isDone ? "line-through" : "none",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                          title={item.title}
                        >
                          {item.title}
                        </span>'''
    new5 = '''                        {todoEditingId === item.id ? (
                          <input
                            autoFocus
                            value={todoEditTitle}
                            onChange={(e) => setTodoEditTitle(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") saveTodoTitle(item, todoEditTitle);
                              if (e.key === "Escape") { setTodoEditingId(null); setTodoEditTitle(""); }
                            }}
                            onBlur={() => saveTodoTitle(item, todoEditTitle)}
                            style={{
                              flex: 1, minWidth: 0, fontSize: "12px", padding: "1px 4px", borderRadius: "3px",
                              border: "1px solid var(--ct-accent)", background: "var(--ct-input)",
                              color: "var(--ct-text)", outline: "none",
                            }}
                          />
                        ) : (
                          <span
                            onDoubleClick={() => { setTodoEditingId(item.id); setTodoEditTitle(item.title); }}
                            title={item.title + " (\\ub354\\ube14\\ud074\\ub9ad\\uc73c\\ub85c \\uc218\\uc815)"}
                            style={{
                              flex: 1, minWidth: 0, fontSize: "12px",
                              color: isDone ? "var(--ct-text2)" : "var(--ct-text)",
                              textDecoration: isDone ? "line-through" : "none",
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                              cursor: "text",
                            }}
                          >
                            {item.title}
                          </span>
                        )}'''
    if old5 in content:
        content = content.replace(old5, new5, 1)
        changes += 1
        print("PATCH5 OK: inline title edit added")
    else:
        print("PATCH5 FAIL: title span not found")
        sys.exit(1)
else:
    print("PATCH5 SKIP: already applied")

with open(TARGET, "w") as f:
    f.write(content)

print(f"DONE: {changes} patches applied")
