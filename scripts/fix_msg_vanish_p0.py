#!/usr/bin/env python3
"""AADS-MSG-VANISH-P0-20260818
CEO 지시 버블 사라짐 P0 수정 (프론트 단독 패치).
P0-1: 메시지 조회 limit 40 -> 120 (숨김 러너 알림이 창을 잠식하는 문제)
P0-2: 렌더 상한 산정 기준을 raw messages -> 표시 대상(display) 기준으로 전환, 하한 150 유지
P0-3: 로컬 질문 보호 개수 5 -> 20
"""
from pathlib import Path
import shutil
import sys

TARGET = Path("/root/aads/aads-dashboard/src/app/chat/page.tsx")
src = TARGET.read_text(encoding="utf-8")
orig = src
report = []

# P0-1
n1 = src.count("&limit=40&")
src = src.replace("&limit=40&", "&limit=120&")
report.append(("P0-1 limit=40 -> 120", n1))

# P0-2
old_cap = "const MAX_RENDER = messages.length > 500 ? 40 : 150;"
new_cap = (
    "const MAX_RENDER = display.length > 400 ? 200 : 150;  "
    "// AADS-MSG-VANISH-P0: 숨김(러너/시스템) 메시지 제외한 표시 대상 기준으로 산정"
)
n2 = src.count(old_cap)
src = src.replace(old_cap, new_cap)
report.append(("P0-2 MAX_RENDER 기준 전환", n2))

# P0-3
old_prot = """    const protectedLocalQuestions = display
      .filter((item) => localQuestionEchoIdsRef.current.has(item.msg.id) && !cappedIds.has(item.msg.id))
      .slice(-5);"""
new_prot = """    const protectedLocalQuestions = display
      .filter((item) => (
        !cappedIds.has(item.msg.id) &&
        (
          localQuestionEchoIdsRef.current.has(item.msg.id) ||
          (item.msg.role === "user" && item.msg.intent !== "system_trigger")
        )
      ))
      .slice(-20);  // AADS-MSG-VANISH-P0: CEO 입력 질문 버블은 렌더 cap에서 절대 탈락시키지 않는다"""
n3 = src.count(old_prot)
src = src.replace(old_prot, new_prot)
report.append(("P0-3 질문 버블 보호 강화", n3))

if src == orig:
    print("NO_CHANGE")
    sys.exit(2)

shutil.copy2(TARGET, str(TARGET) + ".bak_msgvanish")
TARGET.write_text(src, encoding="utf-8")
for name, cnt in report:
    print(f"{name}: {cnt} occurrence(s)")
print("WROTE", TARGET)
