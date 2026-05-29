#!/usr/bin/env python3
"""
AADS 채팅 스트리밍 버그 패치
B1: 폴링 just_completed 경로에서 setStreaming(false) 미호출
B2: SSE done 이벤트 후 streamingSessionRef 미정리
B3: finally 블록 _isInvisibleRecovery 과다 조건 (gotFinal=true도 차단)
B4: resume_done 핸들러에서 streamingSessionRef 미정리
"""

import shutil

TARGET = "/root/aads/aads-dashboard/src/app/chat/page.tsx"
BACKUP = TARGET + ".bak_streaming_fix"

with open(TARGET, "r") as f:
    content = f.read()

shutil.copy2(TARGET, BACKUP)
print(f"백업 완료: {BACKUP}")

patches = []

# ── B1-FIX: 폴링 just_completed 경로 ──────────────────────────────
B1_OLD = (
    '              if (retryMsgs && retryMsgs.length > 0 && retryMsgs[retryMsgs.length - 1].role === "assistant") {\n'
    '                setWaitingBgResponse(false); setBgPartialContent("");\n'
    '              }\n'
    '              // 여전히 없으면 폴링이 계속 잡아줌 (60초 타임아웃)\n'
    '            });\n'
    '          }, 1500);\n'
    '          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);\n'
    '          waitingBgTimeoutRef.current = setTimeout(() => { setWaitingBgResponse(false); setBgPartialContent(""); }, 60000);\n'
    '        } else {\n'
    '          setWaitingBgResponse(false); setBgPartialContent("");\n'
    '        }'
)
B1_NEW = (
    '              if (retryMsgs && retryMsgs.length > 0 && retryMsgs[retryMsgs.length - 1].role === "assistant") {\n'
    '                setWaitingBgResponse(false); setBgPartialContent("");\n'
    '                // B1-FIX: 폴링 완료 감지 시 streaming 인디케이터 + sessionRef 즉시 해제 (30s 타이머 오버라이드 방지)\n'
    '                if (streamingSessionRef.current === fetchSid) { streamingSessionRef.current = null; setStreaming(false); setStreamBuf(""); }\n'
    '              }\n'
    '              // 여전히 없으면 폴링이 계속 잡아줌 (60초 타임아웃)\n'
    '            });\n'
    '          }, 1500);\n'
    '          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);\n'
    '          waitingBgTimeoutRef.current = setTimeout(() => { setWaitingBgResponse(false); setBgPartialContent(""); }, 60000);\n'
    '        } else {\n'
    '          setWaitingBgResponse(false); setBgPartialContent("");\n'
    '          // B1-FIX: 즉시 완료 경로에서도 streaming 해제\n'
    '          if (streamingSessionRef.current === fetchSid) { streamingSessionRef.current = null; setStreaming(false); setStreamBuf(""); }\n'
    '        }'
)
patches.append(("B1", B1_OLD, B1_NEW))

# ── B2-FIX: SSE done 이벤트 후 streamingSessionRef 정리 ──────────
B2_OLD = (
    '              setStreamBuf("");\n'
    '              setThinkingBuf("");\n'
    '              setStreaming(false);\n'
    '              // P0-FIX: SSE done 경로에도 완료 토스트 표시\n'
    '              showCompletionToast("응답이 완료되었습니다");'
)
B2_NEW = (
    '              setStreamBuf("");\n'
    '              setThinkingBuf("");\n'
    '              setStreaming(false);\n'
    '              streamingSessionRef.current = null;  // B2-FIX: done 이벤트 시 sessionRef 즉시 정리\n'
    '              // P0-FIX: SSE done 경로에도 완료 토스트 표시\n'
    '              showCompletionToast("응답이 완료되었습니다");'
)
patches.append(("B2", B2_OLD, B2_NEW))

# ── B3-FIX: finally 블록 _isInvisibleRecovery 조건 수정 ───────────
B3_OLD = "      const _isInvisibleRecovery = _invisibleRecoveryActivated || waitingBgRef.current;"
B3_NEW = "      const _isInvisibleRecovery = !gotFinal && (_invisibleRecoveryActivated || waitingBgRef.current);  // B3-FIX: gotFinal=true면 항상 cleanup"
patches.append(("B3", B3_OLD, B3_NEW))

# ── B4-FIX: resume_done 핸들러에서 streamingSessionRef 정리 ──────
B4_OLD = (
    '                      setStreamBuf("");\n'
    '                      setStreaming(false);\n'
    '                    } else {\n'
    '                      setStreamBuf(""); setStreaming(false);\n'
    '                    }\n'
    '                    resumed = true;\n'
    '                    break;'
)
B4_NEW = (
    '                      setStreamBuf("");\n'
    '                      setStreaming(false);\n'
    '                      streamingSessionRef.current = null;  // B4-FIX: resume 성공 시 sessionRef 정리\n'
    '                    } else {\n'
    '                      setStreamBuf(""); setStreaming(false);\n'
    '                      streamingSessionRef.current = null;  // B4-FIX: resume 성공 시 sessionRef 정리\n'
    '                    }\n'
    '                    resumed = true;\n'
    '                    break;'
)
patches.append(("B4", B4_OLD, B4_NEW))

# 패치 적용
errors = []
for name, old, new in patches:
    count = content.count(old)
    if count == 0:
        errors.append(f"❌ {name}: old_string을 찾을 수 없음")
    elif count > 1:
        print(f"⚠️ {name}: old_string이 {count}회 발견 — 첫 번째만 교체")
        content = content.replace(old, new, 1)
    else:
        content = content.replace(old, new)
        print(f"✅ {name} 패치 적용 완료")

for e in errors:
    print(e)

if not errors:
    with open(TARGET, "w") as f:
        f.write(content)
    print(f"\n✅ 파일 저장 완료: {TARGET}")
else:
    print("\n❌ 일부 패치 실패 — 파일 미저장")
