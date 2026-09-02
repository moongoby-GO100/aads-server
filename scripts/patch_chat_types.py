#!/usr/bin/env python3
# AADS-BUBBLE-FLASH-P0b: ChatMessage 타입에 requested_model/fallback_reason 누락 -> 대시보드 빌드 차단 해소
import io, sys
P = "/root/aads/aads-dashboard/src/app/chat/types.ts"
src = io.open(P, encoding="utf-8").read()
old = "  render_id?: string;\n  model_used?: string;\n"
if old not in src or src.count(old) != 1:
    print("NO_MATCH"); sys.exit(1)
new = ("  render_id?: string;\n  model_used?: string;\n"
       "  requested_model?: string | null;   // AADS: CEO가 선택한 요청 모델\n"
       "  fallback_reason?: string | null;   // AADS: 실행 모델이 바뀐 사유\n")
io.open(P, "w", encoding="utf-8").write(src.replace(old, new))
print("APPLIED types.ts requested_model/fallback_reason")
