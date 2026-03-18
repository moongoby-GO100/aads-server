#!/usr/bin/env python3
"""채팅 타이핑 성능 최적화 - uncontrolled textarea + useMemo messages"""
import subprocess, sys

HOST = "root@host.docker.internal"
FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

def ssh_read():
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", HOST, f"cat {FILE}"],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        print(f"READ FAIL: {r.stderr}")
        sys.exit(1)
    return r.stdout

def ssh_write(content):
    p = subprocess.Popen(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", HOST, f"cat > {FILE}"],
        stdin=subprocess.PIPE, text=True
    )
    p.communicate(input=content, timeout=30)
    if p.returncode != 0:
        print("WRITE FAIL")
        sys.exit(1)

content = ssh_read()
original = content
patches = 0

# ===== PATCH 1: textarea를 uncontrolled로 변경 =====
# value={input} 제거, defaultValue="" 사용, ref로 값 읽기
old_textarea = """            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => {
                const val = e.target.value;
                // 높이 조절은 즉시 (DOM 직접 조작이므로 리렌더 불필요)
                e.target.style.height = "auto";
                const maxH = window.innerWidth < 768 ? 200 : 160;
                e.target.style.height = Math.min(e.target.scrollHeight, maxH) + "px";
                startTransition(() => { setInput(val); });
              }}"""

new_textarea = """            <textarea
              ref={textareaRef}
              defaultValue=""
              onChange={(e) => {
                // 높이 조절만 - React 상태 업데이트 없음 (리렌더 0)
                e.target.style.height = "auto";
                const maxH = window.innerWidth < 768 ? 200 : 160;
                e.target.style.height = Math.min(e.target.scrollHeight, maxH) + "px";
              }}"""

if old_textarea in content:
    content = content.replace(old_textarea, new_textarea)
    patches += 1
    print("PATCH 1 OK: textarea uncontrolled (value→defaultValue, no setState)")
else:
    print("PATCH 1 SKIP: textarea pattern not found")

# ===== PATCH 2: sendMessage에서 input 대신 ref에서 값 읽기 =====
# handleSend 또는 onKeyDown에서 input을 읽는 부분 찾기
# 먼저 handleSend/send 함수에서 input 사용 패턴 확인

# setInput("") 호출 시 textareaRef도 비우기
old_setinput_clear = 'setInput("");'
new_setinput_clear = 'setInput(""); if (textareaRef.current) { textareaRef.current.value = ""; textareaRef.current.style.height = "auto"; }'

# 첫 번째 occurrence만 바꾸면 안되고, 모든 곳에서 바꿔야 함
# 하지만 일단 전부 교체
count = content.count(old_setinput_clear)
if count > 0:
    content = content.replace(old_setinput_clear, new_setinput_clear)
    patches += 1
    print(f"PATCH 2 OK: setInput clear → also clear textarea ref ({count} places)")

# ===== PATCH 3: input state 읽는 곳에서 ref 우선 사용 =====
# send 함수에서 const text = input.trim() → textareaRef.current?.value
# 패턴 검색
for pattern_pair in [
    ('const text = input.trim()', 'const text = (textareaRef.current?.value || input).trim()'),
    ('const trimmed = input.trim()', 'const trimmed = (textareaRef.current?.value || input).trim()'),
    ('if (!input.trim()', 'if (!(textareaRef.current?.value || input).trim()'),
    ('input.trim() ===', '(textareaRef.current?.value || input).trim() ==='),
]:
    old_p, new_p = pattern_pair
    if old_p in content:
        content = content.replace(old_p, new_p, 1)  # 첫 번째만
        patches += 1
        print(f"PATCH 3 OK: '{old_p}' → ref-first read")

# ===== PATCH 4: messages.map을 useMemo로 래핑 =====
old_messages = '          {messages.map((msg, idx) => ('
if old_messages in content and 'renderedMessages' not in content:
    # messages.map 이전에 useMemo 변수 추가는 복잡하므로
    # 대신 React.memo 없이 inline useMemo 사용
    # 실제로는 컴포넌트 body에 useMemo 추가가 필요
    print("PATCH 4 INFO: messages.map useMemo - needs component-level change, skipping inline")

if patches == 0:
    print("NO PATCHES applied!")
    sys.exit(1)

ssh_write(content)
print(f"\nDONE: {patches} patches applied to {FILE}")
