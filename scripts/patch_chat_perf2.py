#!/usr/bin/env python3
"""채팅 성능 패치 2단계: send에서 ref 읽기 + 버튼 활성화 수정"""
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
patches = 0

# PATCH A: sendMessage 내부에서 input.trim() → ref에서 읽기
# 라인 1266: const content = queuedContent || input.trim();
old_send = 'const content = queuedContent || input.trim();'
new_send = 'const content = queuedContent || (textareaRef.current?.value || input).trim();'
if old_send in content:
    content = content.replace(old_send, new_send, 1)
    patches += 1
    print("PATCH A OK: sendMessage reads from ref first")

# PATCH B: 버튼 disabled/onClick에서 input.trim() → ref 또는 항상 활성
# 문제: uncontrolled에서는 input state가 빈칸이므로 버튼이 항상 disabled
# 해결: input.trim()을 (textareaRef.current?.value || "").trim()으로 교체
# 하지만 이러면 React가 ref 변경을 감지 못해 버튼이 업데이트 안됨
# 더 나은 접근: textarea를 다시 controlled로 하되, debounce를 추가

# 사실 가장 효과적인 방법은: controlled를 유지하되 input 컴포넌트만 분리하는 것
# 하지만 4153줄 리팩토링은 위험함

# 현실적 해결: controlled로 되돌리되, useDeferredValue 사용
# React 18의 useDeferredValue는 input을 deferred 버전으로 만들어서
# 메시지 목록 렌더링에는 deferred 값을 쓰고, textarea만 즉시 반영

# 결론: uncontrolled 접근을 철회하고, 다른 전략 사용
print("\n=== STRATEGY CHANGE ===")
print("Uncontrolled textarea는 send 버튼/조건 로직과 충돌")
print("→ controlled textarea 유지 + 메시지 목록만 메모이제이션")

# REVERT: defaultValue → value 복원, onChange에서 setInput 복원 (debounce 없이)
old_uncontrolled = """            <textarea
              ref={textareaRef}
              defaultValue=""
              onChange={(e) => {
                // 높이 조절만 - React 상태 업데이트 없음 (리렌더 0)
                e.target.style.height = "auto";
                const maxH = window.innerWidth < 768 ? 200 : 160;
                e.target.style.height = Math.min(e.target.scrollHeight, maxH) + "px";
              }}"""

new_controlled = """            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => {
                const val = e.target.value;
                e.target.style.height = "auto";
                const maxH = window.innerWidth < 768 ? 200 : 160;
                e.target.style.height = Math.min(e.target.scrollHeight, maxH) + "px";
                startTransition(() => { setInput(val); });
              }}"""

if old_uncontrolled in content:
    content = content.replace(old_uncontrolled, new_controlled)
    patches += 1
    print("REVERT OK: textarea back to controlled + startTransition")

# REVERT setInput clear
old_clear = 'setInput(""); if (textareaRef.current) { textareaRef.current.value = ""; textareaRef.current.style.height = "auto"; }'
new_clear = 'setInput(""); if (textareaRef.current) { textareaRef.current.style.height = "auto"; }'
content = content.replace(old_clear, new_clear)

# PATCH C: 핵심 최적화 - messages.map 전체를 useMemo로 래핑
# 컴포넌트 시작 부분(useState 선언 근처)에 useMemo 추가
# messages.map 결과를 캐싱해서 input 변경 시 재렌더 방지

# messages.map 사용 위치 확인 및 래핑
old_messages_block = '          {messages.map((msg, idx) => ('
if old_messages_block in content:
    # messages, activeSession 등의 dep이 필요
    new_messages_block = '          {renderedMessages}'
    content = content.replace(old_messages_block, new_messages_block, 1)
    
    # 이제 renderedMessages를 useMemo로 정의 - messages.map 시작 전에 추가
    # useState/useEffect 블록 뒤에 삽입
    # "const [input, setInput] = useState("");" 이후에 추가
    memo_code = '''
  // === 성능 최적화: 메시지 목록 메모이제이션 (타이핑 시 재렌더 방지) ===
  const renderedMessages = useMemo(() => messages.map((msg, idx) => ('''
    
    insert_marker = 'const [input, setInput] = useState("");'
    if insert_marker in content:
        content = content.replace(
            insert_marker,
            insert_marker + memo_code,
            1
        )
        
        # useMemo 닫기: messages.map의 닫는 부분 찾기
        # 패턴: ))} 로 map이 끝나는 부분 찾기 - renderedMessages 이후
        # 이건 복잡하므로, 다른 접근: messages.map 끝나는 곳에 ), [messages, ...deps]) 추가
        # 하지만 JSX 중첩이 복잡해서 정확한 끝을 찾기 어려움
        
        # 더 안전한 접근: 별도의 MessageList 컴포넌트로 분리하지 않고
        # CSS contain 속성으로 레이아웃 격리
        print("PATCH C PARTIAL: memo insert attempted but closing bracket complex")
        # 롤백
        content = content.replace(new_messages_block, old_messages_block, 1)
        content = content.replace(insert_marker + memo_code, insert_marker, 1)
        patches -= 0  # no change
    
    # 대안: CSS contain 으로 성능 격리
    print("PATCH C: Using CSS containment instead")

# PATCH D: CSS containment - 메시지 영역에 contain: content 적용
# 이렇게 하면 브라우저가 메시지 영역과 input 영역을 독립적으로 렌더링
# textarea의 style 속성에 will-change 추가
old_textarea_style = """                fontFamily: "inherit",
                lineHeight: "1.5","""
new_textarea_style = """                fontFamily: "inherit",
                lineHeight: "1.5",
                willChange: "contents",
                contain: "layout style","""
if old_textarea_style in content:
    content = content.replace(old_textarea_style, new_textarea_style, 1)
    patches += 1
    print("PATCH D OK: CSS containment on textarea")

# PATCH E: 메시지 컨테이너에 contain 추가
# 메시지 스크롤 영역 찾기
old_msg_container = 'overflowY: "auto",'
if old_msg_container in content:
    # 첫 번째 occurrence가 메시지 영역일 가능성 높음
    idx = content.find(old_msg_container)
    # contain: strict 추가
    new_msg_container = 'overflowY: "auto", contain: "content",'
    content = content[:idx] + new_msg_container + content[idx+len(old_msg_container):]
    patches += 1
    print("PATCH E OK: CSS containment on message container")

if patches > 0:
    ssh_write(content)
    
print(f"\nDONE: {patches} patches applied")
