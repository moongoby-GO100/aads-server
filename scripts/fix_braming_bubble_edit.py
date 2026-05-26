#!/usr/bin/env python3
"""Fix: braming bubble edit - saved content not shown on re-click.

Root causes:
1. NodeDetailPanel useEffect depends only on [node?.id], so re-clicking
   the same node after edit doesn't refresh local state.
2. handleNodeClick reads node.data from BramingCanvas internal state,
   which may lag behind page-level nodes state due to useEffect sync delay.
"""
import shutil, sys

PANEL = '/root/aads/aads-dashboard/src/app/braming/components/NodeDetailPanel.tsx'
PAGE = '/root/aads/aads-dashboard/src/app/braming/page.tsx'

errors = []

# --- Backup ---
for p in [PANEL, PAGE]:
    shutil.copy2(p, p + '.bak_aads')

# === Fix 1: NodeDetailPanel.tsx ===
with open(PANEL) as f:
    c = f.read()

old1 = '}, [node?.id]);'
new1 = '}, [node?.id, node?.label, node?.content]);'
if old1 in c:
    c = c.replace(old1, new1, 1)
    with open(PANEL, 'w') as f:
        f.write(c)
    print('OK Fix1: NodeDetailPanel useEffect deps updated')
else:
    errors.append('Fix1: pattern not found in NodeDetailPanel')
    print('ERR Fix1: pattern not found')

# === Fix 2: page.tsx ===
with open(PAGE) as f:
    c = f.read()

# 2a: Add useRef to import
old2a = 'import { useState, useCallback } from "react";'
new2a = 'import { useState, useCallback, useRef } from "react";'
if old2a in c:
    c = c.replace(old2a, new2a, 1)
    print('OK Fix2a: useRef import added')
else:
    errors.append('Fix2a: import pattern not found')

# 2b: Add nodesRef after newTopic state
old2b = '  const [newTopic, setNewTopic] = useState("");\n'
new2b = '  const [newTopic, setNewTopic] = useState("");\n\n  const nodesRef = useRef<Node[]>([]);\n  nodesRef.current = nodes;\n'
if old2b in c:
    c = c.replace(old2b, new2b, 1)
    print('OK Fix2b: nodesRef added')
else:
    errors.append('Fix2b: newTopic pattern not found')

# 2c: Update handleNodeClick to use nodesRef
old2c = (
    '  const handleNodeClick = useCallback((_nodeId: string, data: Record<string, unknown>) => {\n'
    '    setSelectedNode({\n'
    '      id: _nodeId,\n'
    '      label: (data.label as string) || "",\n'
    '      content: (data.content as string) || "",\n'
    '      nodeType: (data.nodeType as string) || "idea",\n'
    '      agentRole: (data.agentRole as string) || null,\n'
    '      cost: (data.cost as number) || 0,\n'
    '      createdAt: (data.createdAt as string) || "",\n'
    '    });\n'
    '  }, []);'
)
new2c = (
    '  const handleNodeClick = useCallback((_nodeId: string, data: Record<string, unknown>) => {\n'
    '    const pageNode = nodesRef.current.find(n => n.id === _nodeId);\n'
    '    const src = (pageNode?.data ?? data) as Record<string, unknown>;\n'
    '    setSelectedNode({\n'
    '      id: _nodeId,\n'
    '      label: (src.label as string) || "",\n'
    '      content: (src.content as string) || "",\n'
    '      nodeType: (src.nodeType as string) || "idea",\n'
    '      agentRole: (src.agentRole as string) || null,\n'
    '      cost: (src.cost as number) || 0,\n'
    '      createdAt: (src.createdAt as string) || "",\n'
    '    });\n'
    '  }, []);'
)
if old2c in c:
    c = c.replace(old2c, new2c, 1)
    print('OK Fix2c: handleNodeClick updated with nodesRef lookup')
else:
    errors.append('Fix2c: handleNodeClick pattern not found')

with open(PAGE, 'w') as f:
    f.write(c)
print('OK Fix2: page.tsx saved')

if errors:
    print(f'\nERRORS: {errors}')
    sys.exit(1)
else:
    print('\nALL FIXES APPLIED SUCCESSFULLY')
