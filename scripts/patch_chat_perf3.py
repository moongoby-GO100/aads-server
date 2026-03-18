#!/usr/bin/env python3
"""채팅 성능 패치 3단계 수정: 버튼 활성화 문제 해결
- ChatInput에 onHasInput 콜백 추가 (boolean만 전달, 문자열 X)
- page.tsx에 hasInput state 추가 (boolean이므로 리렌더 최소화)
- input.trim() → hasInput으로 교체
"""
import subprocess, sys

HOST = "root@host.docker.internal"
DASHBOARD = "/root/aads/aads-dashboard/src/app/chat"
PAGE = f"{DASHBOARD}/page.tsx"
INPUT_COMP = f"{DASHBOARD}/ChatInput.tsx"

def ssh_run(cmd, timeout=30):
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", HOST, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r

def ssh_read(path):
    r = ssh_run(f"cat {path}")
    if r.returncode != 0:
        print(f"READ FAIL {path}: {r.stderr}")
        sys.exit(1)
    return r.stdout

def ssh_write(path, content):
    p = subprocess.Popen(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", HOST, f"cat > {path}"],
        stdin=subprocess.PIPE, text=True
    )
    p.communicate(input=content, timeout=30)
    if p.returncode != 0:
        print(f"WRITE FAIL {path}")
        sys.exit(1)

# ====== STEP 1: ChatInput.tsx 업데이트 - onHasInput 콜백 추가 ======
chat_input_component = '''"use client";
import { useState, useRef, useCallback, useImperativeHandle, forwardRef, memo } from "react";

export interface ChatInputHandle {
  getValue: () => string;
  setValue: (v: string) => void;
  clear: () => void;
  focus: () => void;
}

interface ChatInputProps {
  screenSize: string;
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  onHasInput?: (has: boolean) => void;
  placeholder?: string;
}

const ChatInput = memo(forwardRef<ChatInputHandle, ChatInputProps>(
  function ChatInput({ screenSize, onKeyDown, onHasInput, placeholder }, ref) {
    const [localInput, setLocalInput] = useState("");
    const taRef = useRef<HTMLTextAreaElement>(null);
    const hadInputRef = useRef(false);

    useImperativeHandle(ref, () => ({
      getValue: () => taRef.current?.value ?? localInput,
      setValue: (v: string) => {
        setLocalInput(v);
        if (taRef.current) {
          taRef.current.value = v;
          taRef.current.style.height = "auto";
          setTimeout(() => {
            if (taRef.current) {
              const maxH = window.innerWidth < 768 ? 200 : 160;
              taRef.current.style.height = Math.min(taRef.current.scrollHeight, maxH) + "px";
            }
          }, 0);
        }
        const has = v.trim().length > 0;
        if (has !== hadInputRef.current) {
          hadInputRef.current = has;
          onHasInput?.(has);
        }
      },
      clear: () => {
        setLocalInput("");
        if (taRef.current) { taRef.current.style.height = "auto"; }
        if (hadInputRef.current) {
          hadInputRef.current = false;
          onHasInput?.(false);
        }
      },
      focus: () => { taRef.current?.focus(); },
    }), [localInput, onHasInput]);

    const handleChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const val = e.target.value;
      e.target.style.height = "auto";
      const maxH = window.innerWidth < 768 ? 200 : 160;
      e.target.style.height = Math.min(e.target.scrollHeight, maxH) + "px";
      setLocalInput(val);
      // 부모에 빈칸 여부만 알림 (boolean 변경 시만 호출 → 리렌더 최소화)
      const has = val.trim().length > 0;
      if (has !== hadInputRef.current) {
        hadInputRef.current = has;
        onHasInput?.(has);
      }
    }, [onHasInput]);

    return (
      <textarea
        ref={taRef}
        value={localInput}
        onChange={handleChange}
        onKeyDown={onKeyDown}
        placeholder={placeholder || "메시지를 입력하세요... (Enter 전송, Shift+Enter 줄바꿈)"}
        rows={1}
        style={{
          flex: 1,
          padding: "10px 14px",
          fontSize: screenSize === "mobile" ? "16px" : "14px",
          resize: "none",
          overflow: "hidden",
          background: "var(--ct-input)",
          color: "var(--ct-text)",
          border: "1px solid var(--ct-border)",
          borderRadius: "12px",
          outline: "none",
          fontFamily: "inherit",
          lineHeight: "1.5",
          minHeight: screenSize === "mobile" ? "52px" : "44px",
          maxHeight: screenSize === "mobile" ? "200px" : "160px",
        }}
        onFocus={(e) => (e.target.style.borderColor = "var(--ct-accent)")}
        onBlur={(e) => (e.target.style.borderColor = "var(--ct-border)")}
      />
    );
  }
));

ChatInput.displayName = "ChatInput";
export default ChatInput;
'''

print("STEP 1: Updating ChatInput.tsx with onHasInput...")
ssh_write(INPUT_COMP, chat_input_component)
print("  OK")

# ====== STEP 2: page.tsx 수정 ======
print("\nSTEP 2: Patching page.tsx...")
content = ssh_read(PAGE)
patches = 0

# 2a: hasInput state 추가 (input state 근처)
input_state = '  const [input, setInput] = useState("");'
if input_state in content and 'hasInput' not in content:
    content = content.replace(
        input_state,
        input_state + '\n  const [hasInput, setHasInput] = useState(false);',
        1
    )
    patches += 1
    print("  2a OK: hasInput state added")

# 2b: ChatInput에 onHasInput prop 추가
old_chatinput = """            <ChatInput
              ref={chatInputRef}
              screenSize={screenSize}
              onKeyDown={onKeyDown}
            />"""
new_chatinput = """            <ChatInput
              ref={chatInputRef}
              screenSize={screenSize}
              onKeyDown={onKeyDown}
              onHasInput={setHasInput}
            />"""
if old_chatinput in content:
    content = content.replace(old_chatinput, new_chatinput, 1)
    patches += 1
    print("  2b OK: onHasInput prop added to ChatInput")

# 2c: input.trim() → hasInput 교체 (버튼 영역만)
# 패턴들:
replacements = [
    ('!input.trim() && pendingPreviewFiles.length === 0', '!hasInput && pendingPreviewFiles.length === 0'),
    ('input.trim() || pendingPreviewFiles.length > 0', 'hasInput || pendingPreviewFiles.length > 0'),
    ('input.trim() ? "대기 전송"', 'hasInput ? "대기 전송"'),
]
for old, new in replacements:
    count = content.count(old)
    if count > 0:
        content = content.replace(old, new)
        patches += 1
        print(f"  2c OK: '{old[:40]}...' → hasInput ({count}x)")

# 2d: setInput("") 시 hasInput도 false로
# chatInputRef.current?.clear()가 이미 onHasInput(false) 호출하므로 불필요
# 하지만 setInput("") 직접 호출하는 곳도 hasInput 동기화 필요
# → chatInputRef.clear()가 onHasInput 호출하므로 OK

# 2e: setInput(content) 시 hasInput도 동기화
# → chatInputRef.setValue()가 onHasInput 호출하므로 OK

# 2f: setInput((prev) => ...) 패턴 (chip 적용)
# 이건 chatInputRef.setValue를 안 쓰므로 수동 동기화 필요
old_chip = 'setInput((prev) => (prev ? `${prefix} ${prev}` : `${prefix} `));'
if old_chip in content:
    new_chip = old_chip + ' setHasInput(true);'
    content = content.replace(old_chip, new_chip, 1)
    patches += 1
    print("  2f OK: chip setInput → also setHasInput")

if patches > 0:
    ssh_write(PAGE, content)
    print(f"\nDONE: {patches} patches applied")
else:
    print("\nNO PATCHES needed")
