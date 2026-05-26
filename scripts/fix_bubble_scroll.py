#!/usr/bin/env python3
"""Fix: bubble duplication + scroll jumping (6 patches, bottom-up)"""

filepath = '/root/aads/aads-dashboard/src/app/chat/page.tsx'
with open(filepath, 'r') as f:
    lines = f.readlines()

original_count = len(lines)
applied = []

# --- Patch 1: L3536 (0-idx 3535) - stuck merge: add cooldown guard ---
old1 = 'setMessages(prev => mergeServerMessagesPreservingLocal(prev, freshMsgs));'
new1 = 'if (Date.now() >= mergeCooldownUntilRef.current) { setMessages(prev => mergeServerMessagesPreservingLocal(prev, freshMsgs)); mergeCooldownUntilRef.current = Date.now() + 5000; }'
if old1 in lines[3535]:
    lines[3535] = lines[3535].replace(old1, new1)
    applied.append('P1:L3536-stuck-cooldown')

# --- Patch 2: L3496 (0-idx 3495) - SSE disconnect merge: add cooldown guard ---
old2 = 'setMessages(prev => mergeServerMessagesPreservingLocal(prev, filtered));'
if old2 in lines[3495]:
    lines[3495] = lines[3495].replace(old2, 'if (Date.now() >= mergeCooldownUntilRef.current) ' + old2)
    applied.append('P2:L3496-disconnect-cooldown')

# --- Patch 3: L3464 (0-idx 3463) - just_completed merge: add cooldown guard ---
if old2 in lines[3463]:
    lines[3463] = lines[3463].replace(old2, 'if (Date.now() >= mergeCooldownUntilRef.current) ' + old2)
    applied.append('P3:L3464-completed-cooldown')

# --- Patch 4: L3349 (0-idx 3348) - scroll only when messages grow ---
old4 = '} else if (isNearBottomRef.current) {'
new4 = '} else if (isNearBottomRef.current && _grew) {'
if old4 in lines[3348]:
    lines[3348] = lines[3348].replace(old4, new4)
    applied.append('P4:L3349-scroll-grew-guard')

# --- Patch 5: After L3332 (0-idx 3332) - insert grew tracking ---
grew_line = '    const _grew = messages.length >= prevMessagesCountRef.current; prevMessagesCountRef.current = messages.length;\n'
if '_grew' not in lines[3332]:
    lines.insert(3332, grew_line)
    applied.append('P5:L3332-grew-tracking')

# --- Patch 6: After L1978 (0-idx 1978) - insert prevMessagesCountRef ---
ref_line = '  const prevMessagesCountRef = useRef(0);\n'
if 'prevMessagesCountRef' not in ''.join(lines[1975:1985]):
    lines.insert(1978, ref_line)
    applied.append('P6:L1978-ref-declaration')

with open(filepath, 'w') as f:
    f.writelines(lines)

print(f'Applied {len(applied)}/{6} patches: {", ".join(applied)}')
print(f'Lines: {original_count} -> {len(lines)}')
